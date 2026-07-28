"""entities, mentions and relations in Postgres (replacing Graphiti/Neo4j)

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-28

Moves entity data out of Neo4j/Graphiti and into Postgres, and settles the
schema for typing, relationships and event years in ONE migration so the
tables don't have to move twice.

Scope note: `entity_relations` and the year columns are created here but
NOTHING populates them yet — that capture flow is a separate piece of work.
They exist now only so the schema is settled.

Why the move (measured, not assumed):
  * _build_entity_map was 45% of a turn and 100% of the latency variance
    (1.35s-9.55s across identical passes), while accuracy was 0.991 with
    AND without the entity map.
  * Zero RELATES_TO edges exist in the graph — verified on live data. The
    one capability a graph engine buys us has never been used.
  * The transcript was stored twice (Graphiti episode body + Postgres), and
    the copies could drift: חיל האוויר survived in the graph on a transcript
    that no longer existed in Postgres.

Every query this replaces is a plain join. See the audit in the migration
plan: seven call sites across three chat modes, all reducing to
"entity names for segment X" and "segments mentioning entity Y".
"""
from alembic import op
import sqlalchemy as sa


def _rt(relation_type, category, is_tree_edge, inverse_type, is_symmetric, label_en, label_he):
    """One relation-type seed row, positionally — the bulk_insert below is
    long enough that keyword dicts would hide the tree-edge column, which is
    the one that actually decides what the family tree draws."""
    return {
        "relation_type": relation_type,
        "category": category,
        "is_tree_edge": is_tree_edge,
        "inverse_type": inverse_type,
        "is_symmetric": is_symmetric,
        "label_en": label_en,
        "label_he": label_he,
    }


revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Trigram index support for the disambiguation lookup that today goes
    # through Graphiti's hybrid search. Lexical is the right tool: the
    # candidates that search returns are already filtered by
    # names_are_similar (a purely lexical gate), so semantic recall is
    # discarded downstream anyway.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "entities",
        sa.Column("id", sa.String(), primary_key=True),
        # One archive per producer. Named producer_id rather than group_id:
        # group_id was Graphiti's vocabulary and always held a user id.
        sa.Column(
            "producer_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # `name` is what the storyteller actually said, preserved verbatim —
        # it is what gets shown back to them.
        sa.Column("name", sa.String(), nullable=False),
        # `normalized_name` is the match key: final-letter forms folded,
        # ט/ת and other confusable pairs normalised. This is what makes
        # תבריה/טבריה resolve without fuzzy guessing at query time.
        sa.Column("normalized_name", sa.String(), nullable=False),
        # NO summary column here — deliberately. A summary describes what ONE
        # recording said, so it lives on the mention. See entity_mentions.
        sa.Column(
            "type",
            sa.String(),
            nullable=False,
            server_default="other",
        ),
        # Mainly meaningful on events ("the air force, 1975-1978"). Nullable
        # everywhere: most entities have no year and guessing one would be
        # worse than leaving it open.
        sa.Column("year_start", sa.Integer(), nullable=True),
        sa.Column("year_end", sa.Integer(), nullable=True),
        # The producer themselves. Every extracted summary is phrased
        # relative to "הדובר" (the speaker) — "brother of the speaker",
        # "mother of the speaker" — and without a row for that person,
        # "I have four brothers" cannot be expressed as relations at all:
        # there is no node for them to be brothers OF. The family tree roots
        # here.
        sa.Column(
            "is_self", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "type IN ('person','place','organisation','event','other')",
            name="ck_entities_type",
        ),
        sa.CheckConstraint(
            "NOT is_self OR type = 'person'", name="ck_entities_self_is_person"
        ),
        # THE merge rule. One row per real-world thing per producer: a second
        # recording mentioning מונטריאול adds a MENTION, never a second row.
        # Without this the migration would import that entity twice and the
        # deletion safety check would then think each copy had one mention
        # and delete both.
        sa.UniqueConstraint(
            "producer_id", "normalized_name", name="uq_entities_producer_normalized"
        ),
    )
    op.create_index(
        "ix_entities_normalized_trgm",
        "entities",
        ["normalized_name"],
        postgresql_using="gin",
        postgresql_ops={"normalized_name": "gin_trgm_ops"},
    )
    op.create_index("ix_entities_producer_type", "entities", ["producer_id", "type"])
    # Exactly one self-entity per producer, enforced rather than assumed —
    # two roots would make the tree ambiguous in a way no page could resolve.
    op.create_index(
        "uq_entities_one_self_per_producer",
        "entities",
        ["producer_id"],
        unique=True,
        postgresql_where=sa.text("is_self"),
    )

    op.create_table(
        "entity_mentions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "entity_id",
            sa.String(),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # REQUIRED. This column is both halves of the load-bearing work: the
        # entity map ("which recordings mention this name") and the deletion
        # safety check ("is any other recording still mentioning it"). It
        # cascades, which is what makes deleting a recording a single
        # transaction instead of a two-database dance.
        sa.Column(
            "raw_segment_id",
            sa.String(),
            sa.ForeignKey("raw_segments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # OPTIONAL, and repopulated on every re-ingest rather than migrated.
        # When ingestion moved to Deepgram one chunk became eight, so every
        # previously-recorded chunk id pointed at a row that no longer
        # existed. Losing chunk-level precision on re-ingest is acceptable;
        # losing the entity-to-recording link is not — hence one required
        # column and one optional one, not two of the same kind.
        sa.Column(
            "chunk_id",
            sa.String(),
            sa.ForeignKey("transcript_chunks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # What THIS recording said about this entity — "a fellow soldier in
        # her unit", not a merged portrait.
        #
        # On the mention rather than the entity, which removes the need to
        # regenerate a summary entirely rather than relocating it. Adding a
        # recording inserts a row; deleting one drops a row; no existing row
        # is ever rewritten, so no summary can go stale relative to the
        # recording it describes — the same one-source-of-truth rule this
        # migration exists to enforce, applied one level down.
        #
        # Where something needs "a summary for Gila", it lists the mention
        # summaries in the recordings' chronological order. No LLM call, no
        # concatenation logic, correct by construction on insert and delete.
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    # NULLS NOT DISTINCT (Postgres 15+; we are on 18.4) — without it, the
    # segment-level mention rows, whose chunk_id is NULL, would be considered
    # distinct from each other and the same entity could be recorded against
    # the same recording repeatedly on every re-ingest.
    op.execute(
        "ALTER TABLE entity_mentions "
        "ADD CONSTRAINT uq_entity_mentions UNIQUE NULLS NOT DISTINCT "
        "(entity_id, raw_segment_id, chunk_id)"
    )
    op.create_index("ix_entity_mentions_segment", "entity_mentions", ["raw_segment_id"])
    op.create_index("ix_entity_mentions_entity", "entity_mentions", ["entity_id"])

    # ── Relation vocabulary ──────────────────────────────────────────────
    #
    # A LOOKUP TABLE rather than a `category` column on entity_relations,
    # because a category is a FUNCTION of the relation type: 'sibling' is
    # always family. Storing it per-row would let a row claim
    # type='sibling', category='professional' — the same "two places hold
    # the same fact and can drift" problem this whole migration is undoing.
    # Here relation_type is the only thing stored on a relation, and
    # everything about that type is one join away and cannot contradict it.
    #
    # It also earns its keep twice more:
    #   * the FK from entity_relations constrains the vocabulary, so an LLM
    #     that invents "brother-ish" fails loudly instead of being stored;
    #   * it is where symmetry and inverses live, which the tree needs and
    #     which would otherwise be a hardcoded dict in application code.
    #
    # is_tree_edge is AUTHORITATIVE for the family-tree page — it never has
    # to guess. `category` is for display grouping and is deliberately NOT
    # the same question: in-laws and cousins are family by category but a
    # tree may legitimately exclude them, so the two are allowed to diverge.
    op.create_table(
        "relation_types",
        sa.Column("relation_type", sa.String(), primary_key=True),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("is_tree_edge", sa.Boolean(), nullable=False),
        # NULL for symmetric types: 'sibling' inverted is still 'sibling'.
        sa.Column("inverse_type", sa.String(), nullable=True),
        sa.Column("is_symmetric", sa.Boolean(), nullable=False),
        sa.Column("label_en", sa.String(), nullable=False),
        sa.Column("label_he", sa.String(), nullable=False),
        sa.CheckConstraint(
            "category IN ('family','social','professional','other')",
            name="ck_relation_types_category",
        ),
        sa.CheckConstraint(
            "is_symmetric = (inverse_type IS NULL)",
            name="ck_relation_types_symmetry_consistent",
        ),
    )

    # One directed row per relation, never two. Storing both directions
    # would mean every edit and delete has to keep a pair in sync, and they
    # will eventually disagree; the inverse is derived at read time from
    # inverse_type / is_symmetric above.
    relation_types = sa.table(
        "relation_types",
        sa.column("relation_type", sa.String()),
        sa.column("category", sa.String()),
        sa.column("is_tree_edge", sa.Boolean()),
        sa.column("inverse_type", sa.String()),
        sa.column("is_symmetric", sa.Boolean()),
        sa.column("label_en", sa.String()),
        sa.column("label_he", sa.String()),
    )
    op.bulk_insert(
        relation_types,
        [
            # ── Family, and tree-bearing ──
            _rt("parent", "family", True, "child", False, "Parent", "הורה"),
            _rt("child", "family", True, "parent", False, "Child", "ילד/ה"),
            _rt("sibling", "family", True, None, True, "Sibling", "אח/ות"),
            _rt("spouse", "family", True, None, True, "Spouse", "בן/בת זוג"),
            _rt("grandparent", "family", True, "grandchild", False, "Grandparent", "סבא/סבתא"),
            _rt("grandchild", "family", True, "grandparent", False, "Grandchild", "נכד/ה"),
            # ── Family by category, but NOT tree edges. This is exactly the
            # divergence the two columns exist to express: they are real
            # family relations worth storing and showing, while a tree that
            # drew them would stop being readable. Flip is_tree_edge if you
            # decide otherwise — no schema change needed.
            _rt("aunt_uncle", "family", False, "niece_nephew", False, "Aunt/Uncle", "דוד/ה"),
            _rt("niece_nephew", "family", False, "aunt_uncle", False, "Niece/Nephew", "אחיין/ית"),
            _rt("cousin", "family", False, None, True, "Cousin", "בן/בת דוד"),
            _rt("parent_in_law", "family", False, "child_in_law", False, "Parent-in-law", "חם/חמות"),
            _rt("child_in_law", "family", False, "parent_in_law", False, "Child-in-law", "חתן/כלה"),
            _rt("sibling_in_law", "family", False, None, True, "Sibling-in-law", "גיס/ה"),
            # ── Not family: stored and worth showing, never tree edges ──
            _rt("friend", "social", False, None, True, "Friend", "חבר/ה"),
            _rt("neighbour", "social", False, None, True, "Neighbour", "שכן/ה"),
            _rt("acquaintance", "social", False, None, True, "Acquaintance", "מכר/ה"),
            _rt("colleague", "professional", False, None, True, "Colleague", "עמית/ה"),
            _rt("commander", "professional", False, "subordinate", False, "Commander", "מפקד/ת"),
            _rt("subordinate", "professional", False, "commander", False, "Subordinate", "פקוד/ה"),
            _rt("teacher", "professional", False, "student", False, "Teacher", "מורה"),
            _rt("student", "professional", False, "teacher", False, "Student", "תלמיד/ה"),
        ],
    )

    op.create_table(
        "entity_relations",
        sa.Column("id", sa.String(), primary_key=True),
        # A relation, not a column on the person: "uncle" is a property of a
        # PAIR. The same person is a sibling to one and a parent to another,
        # which a column could never hold.
        sa.Column(
            "from_entity_id",
            sa.String(),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "to_entity_id",
            sa.String(),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # FK, not free text: an LLM proposing "brother-ish" must fail loudly
        # rather than quietly storing a type nothing knows how to render.
        sa.Column(
            "relation_type",
            sa.String(),
            sa.ForeignKey("relation_types.relation_type"),
            nullable=False,
        ),
        # The recording we learned this from, cascading — so a relation can
        # never outlive the recording that established it. That is the same
        # ghost problem חיל האוויר had, and it would be worse here: a wrong
        # relation in a family tree is more visible than a stale entity.
        sa.Column(
            "source_segment_id",
            sa.String(),
            sa.ForeignKey("raw_segments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "from_entity_id <> to_entity_id", name="ck_entity_relations_not_self"
        ),
        sa.UniqueConstraint(
            "from_entity_id",
            "to_entity_id",
            "relation_type",
            "source_segment_id",
            name="uq_entity_relations",
        ),
    )
    op.create_index("ix_entity_relations_from", "entity_relations", ["from_entity_id"])
    op.create_index("ix_entity_relations_to", "entity_relations", ["to_entity_id"])
    op.create_index(
        "ix_entity_relations_segment", "entity_relations", ["source_segment_id"]
    )

    # ── One self-entity per existing producer ────────────────────────────
    #
    # Created here rather than lazily on first use, so the tree's root is a
    # guaranteed invariant instead of something every caller has to remember
    # to create. New producers get theirs at signup (application code).
    #
    # The name falls back through full_name -> username, because full_name is
    # nullable and several existing producers have none. It is a display
    # label the producer can correct later; what must exist now is the ROW.
    #
    # normalized_name uses plain lower/trim here, not the application's
    # Hebrew normaliser (final letters, ט/ת), which lives in Python and is
    # not available to SQL. That is safe for this row specifically: it is
    # matched by is_self, never by fuzzy name lookup. Should a transcript
    # ever name the producer and normalise to the same key, the unique
    # constraint makes the import attach mentions to THIS row — which is the
    # correct outcome, not a collision.
    op.execute(
        """
        INSERT INTO entities
            (id, producer_id, name, normalized_name, type, is_self, created_at)
        SELECT
            gen_random_uuid()::text,
            u.id,
            COALESCE(NULLIF(TRIM(u.full_name), ''), u.username),
            LOWER(TRIM(COALESCE(NULLIF(TRIM(u.full_name), ''), u.username))),
            'person',
            true,
            NOW()
        FROM users u
        WHERE u.role = 'producer'
        """
    )


def downgrade() -> None:
    op.drop_table("entity_relations")
    op.drop_table("relation_types")
    op.drop_table("entity_mentions")
    op.drop_index("ix_entities_producer_type", table_name="entities")
    op.drop_index("ix_entities_normalized_trgm", table_name="entities")
    op.drop_table("entities")
    # pg_trgm is deliberately NOT dropped: other things may come to depend on
    # it, and dropping an extension is not the inverse of "IF NOT EXISTS".
