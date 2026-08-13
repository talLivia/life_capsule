# Project status

**Updated:** 2026-08-13 · **Branch:** `main` (all work commits directly to
main and pushes; no feature branches unless asked — two exceptions pending
review: the v1 removal on `remove-v1`, and the v2-primary/avatar-dormant
inversion on `avatar-dormant`, which is stacked ON `remove-v1`)

Working-state snapshot. Standing rules and architecture invariants live in
[CLAUDE.md](../CLAUDE.md); this file is "where we are right now" and should be
updated as work lands.

---

# NEXT UP: move entities from Graphiti/Neo4j into Postgres

**COMPLETE.** Plan settled 2026-07-28, all five chunks landed 2026-07-28/29,
signed off after a live end-to-end run. Graphiti and Neo4j are gone from the
codebase. Kept here as the record of what was decided and why — read it before
touching anything entity-related.

## Where it stands right now

- **Chunk 1a has landed** (`df25171`): the merge key
  (`app/services/entity_names.py`, 21 direct tests) and the four ORM models
  (`Entity`, `EntityMention`, `RelationType`, `EntityRelation`).
- **Chunk 1b has landed**: the write path. `entity_extraction.py` (structured
  `{name, type, alternative_type, summary}`) and `entity_store.py` (the
  merge-on-`normalized_name` writer) replace `add_episode` in
  `finalize_ingest_node`. **Newly ingested recordings now write entities to
  Postgres and nothing else.**
- **Chunk 2 has landed**: the 11 graph entities are imported (10 rows — `עכבר`
  dropped). `scripts/import_graph_entities.py` is the record of what was
  decided and is idempotent.
- **Chunk 3 has landed**: all seven read call sites are on Postgres.
- **Chunk 5 has landed**: `graph_memory`, `neo4j_client`, their tests, four
  Graphiti-era scripts, the `neo4j` compose service, the `/health` probe and
  every `NEO4J_*`/`GRAPHITI_*` setting are gone, as are the `neo4j` and
  `graphiti-core` dependencies. **The AuraDB instance can now be shut down —
  see "Shutting down AuraDB" below.**
- **Chunk 4 has landed**: batched confirmation. One screen per recording
  covering identity AND type, one submit; `confirm-entities` replaces
  `confirm-entity`.
- **Chunks 1-3 are REVIEWED AND SIGNED OFF** (2026-07-28). The import was
  approved as-is, including the deliberate NULL summary.
- **Migration `0012` is APPLIED** to the live Neon database (`alembic current`
  = `0012 (head)`). Verified afterwards: 4 tables, 5 self-entities (1 via the
  `full_name → username` fallback), 20 relation types with 6 tree-bearing,
  every CHECK and UNIQUE constraint present, `pg_trgm` installed, and a
  before/after schema diff showing the 14 pre-existing tables **unchanged** in
  columns, constraints, indexes and row counts.
- Exactly **one** producer has `full_name IS NULL` (`prodspot1784624977`), so
  the username fallback applied to that one row. (An earlier note here said
  "several producers" — that was wrong.)

### ⚠️ The migration could not be applied as-is — read this before the next one

`alembic current` said `0011`, but all four tables **already existed** on Neon.
They had been created by `Base.metadata.create_all`, which `main.py` ran
because `DEBUG=true` locally while `DATABASE_URL` pointed at the live database.
Any local `uvicorn main:app` after chunk 1a added the ORM models did it.

They were the ORM's tables, not the migration's — **empty, and missing every
invariant**: no CHECK constraints, no `uq_entities_producer_normalized` (which
IS the entity merge rule), no `UNIQUE NULLS NOT DISTINCT` on mentions, no
trigram index, no `pg_trgm`, no seeded relation types, no self-entities. Index
names gave it away (`ix_entities_producer_id`, SQLAlchemy's `index=True`
naming, rather than the migration's `ix_entity_mentions_entity`).

**This is worse than the tables being absent.** Chunk 2 would have imported
into a schema with nothing enforcing the merge, and `מונטריאול` could have
landed as two rows without anything failing.

Resolved by dropping the four (verified empty inside the same transaction that
dropped them, so a row arriving first would have aborted rather than been
destroyed) and then running `alembic upgrade head` normally.

**Guarded against recurring:** `main._is_local_database` now gates `create_all`
on the database being local as well as `DEBUG` — an allowlist of hosts, failing
closed on anything unparseable or hostless. `tests/test_startup_schema_guard.py`
covers it, including the exact Neon URL shape that caused this.

## Why we're doing it

- **Latency.** `_build_entity_map` was 45% of a turn and 100% of the latency
  variance (1.35s–9.55s across identical passes), while accuracy was 0.991
  with *and* without the entity map.
- **The capability that would justify a graph is unused.** Zero `RELATES_TO`
  edges exist — verified on live data. Graphiti can extract relationships; on
  this data it never has.
- **The transcript is stored twice** and the copies can drift. `חיל האוויר`
  survived in the graph on a transcript that no longer existed in Postgres.

## Call-site audit — SEVEN, not three · ALL MOVED

The original assumption was three uses. There were seven, spanning **all three
chat modes**, so this was not a v2-only change. All are now on Postgres:

| Call site | Was | Now | Mode |
| --- | --- | --- | --- |
| `full_archive_retrieval._build_entity_map` | `get_episode_entity_names` ×N | `get_entity_names_for_segments` (**1 query**) | v2 |
| `segment_deletion` (+ `transcribe_node`, removed in 1b) | `remove_episodes_for_segment` | FK cascade + `delete_orphaned_entities` | all |
| `analysis_graph.check_entities_node` | `get_entity_candidates` | `entity_store.get_entity_candidates` (pg_trgm) | ingestion |
| `analysis_graph.finalize_ingest_node` | `add_episode` (**write path**) | `write_segment_entities` | ingestion |
| `segment_extraction._load_entities` | direct Cypher | `get_segment_entities` | panel |
| `retrieval_service` ×4 | `find_related_episodes`, `..._scored`, `get_episode_entity_names`, `get_entity_candidates` | `find_segments_mentioning[_scored]`, bulk names, candidates | **v1 `video_clips`** |
| `relevance_scorer` + `response_assembler` ×3 | `get_episode_entity_names` ×N | `get_entity_names_for_segments` (**1 query each**) | **`avatar`** |

All seven reduced to two primitives, both plain joins on `entity_mentions`:
"entity names for segment X" and "segments mentioning entity Y".

> *(2026-08-12: the `retrieval_service ×4` row's mode — v1 `video_clips` —
> has since been removed entirely; docs/V1_REMOVAL_PLAN.md. The row stays
> as the record of what the migration moved.)*

Two findings that made this smaller than it looked, both confirmed while doing it:
- **`max_hops` was 1 at every call site**, making the Cypher `RELATES_TO*0..0`
  — matching only the origin node. No traversal happened anywhere, so the
  parameter was **deleted rather than reimplemented**: carrying it forward
  would have advertised a capability that never worked.
- **`find_related_episodes_scored`'s "score" was `COUNT(DISTINCT entity)`** —
  a `GROUP BY` with `ORDER BY count DESC`, now stated directly in SQL.

Two things got BETTER rather than merely moving:
- **Lookups match on `normalized_name`**, so a final-letter or spacing variant
  now finds its entity. The graph matched names exactly and missed those.
- **`names_are_similar` moved to `entity_names.py`** — it never had anything
  to do with the graph, and its being a purely lexical gate is exactly why the
  graph's hybrid vector search bought nothing.

## Final schema decisions

**`summary` lives on `entity_mentions`, NOT on `entities`.** A summary
describes what ONE recording said. Ten recordings naming Gila = ten mention
rows. This removes summary regeneration **entirely** rather than relocating
it: ingest inserts a row, delete drops a row, no existing row is ever
rewritten, so a summary cannot go stale relative to the recording it
describes. Where something needs "a summary for Gila", list the mention
summaries in the recordings' chronological order — no LLM call, no
concatenation logic, correct by construction.

**`type` stays on `entities`** (`person` / `place` / `organisation` / `event` /
`other`). Gila is a person regardless of which recording names her. Same for
`year_start` / `year_end` and `is_self`: properties of the thing, not of one
telling of it.

**`alternative_type` drives confirmation.** The extraction call returns per
entity `{name, type, alternative_type, summary}`. `alternative_type` is null
when the classification is clear, and set to the runner-up when genuinely
torn. **Ask the human if and only if it is non-null.** Deliberately NOT a
confidence score — self-reported confidence is uncalibrated, whereas "which
two are you torn between" is concrete, checkable, and populates the UI with
exactly two options. `ניר` ("brother of the speaker") → no alternative →
silent. `הכפר הירוק` → place vs organisation → asks once.

**Confirmation is batched.** One screen per recording covering identity AND
type questions together, one submit — never a sequence of modals. Asking about
everything trains the producer to click yes without reading, which is worse
than not asking. Reuses the existing `pending_confirmation` payload and
`EntityConfirmModal`.

**`relation_types` is a LOOKUP TABLE, not a `category` column.** Category is a
function of relation type — `sibling` is always family — so a per-row column
would let a row claim `type='sibling', category='professional'`: the same "two
places hold one fact and drift" problem this migration exists to undo. It
earns its keep twice more: the FK constrains the vocabulary (an LLM inventing
`"brother-ish"` fails loudly instead of being stored), and it holds symmetry
and inverses, which would otherwise be a hardcoded dict.

**`is_tree_edge` is authoritative for the family tree**; `category` is for
display, and the two are allowed to diverge — `aunt_uncle`, `cousin` and the
in-laws are `category='family'` but `is_tree_edge=false`, because a tree that
drew them stops being readable. Six types are tree-bearing; flipping one is a
data update, not a schema change. **The tree page never guesses.**

**One directed row per relation, never two.** Two rows means every edit and
delete has to keep a pair in sync, and they will eventually disagree. The
inverse is derived at read time from `inverse_type` / `is_symmetric`.

**`UNIQUE (producer_id, normalized_name)` is the merge rule.** A second
recording mentioning `מונטריאול` adds a mention, never a second row. Without
it the import would create two rows each with one mention, and the deletion
safety check would conclude neither was shared and delete both.

**`entity_mentions` uses `UNIQUE NULLS NOT DISTINCT`** (Postgres 15+; we run
18.4). Segment-level mentions have `chunk_id IS NULL`, and under default NULL
semantics those rows are all distinct — the same entity would accumulate a
duplicate mention row on every re-ingest.

**`is_self`, one per producer** (partial unique index). Every extracted
summary is phrased relative to `הדובר` (the speaker); without a row for that
person, "I have four brothers" cannot be expressed as relations at all —
there is no node for them to be brothers OF. The family tree roots here.

**`entity_mentions.raw_segment_id` is REQUIRED and cascades**; `chunk_id` is
OPTIONAL and repopulated on re-ingest (moving to Deepgram turned one chunk
into eight, invalidating every stored chunk id).

**DECIDED — `normalized_name` does NOT fold ת/ט.** An earlier draft of this
plan said it should, to make תבריה/טבריה merge. Overruled on review, and the
reasoning is worth keeping because the folding version looks obviously right:

- ט and ת are different letters that distinguish real names. Folding them
  merges `טל` (the producer's own name) with `תל`.
- תבריה/טבריה is a **transcription error**, not an orthographic variant. The
  fuzzy layer already handles it — pg_trgm scores the pair very highly and
  `names_are_similar` passes it to human confirmation.
- Folding it into the unique key instead makes it a **silent automatic merge
  with no human check**, which contradicts the governing rule that when the
  system isn't sure, it asks.

The rule, written into `entity_names.py`: **normalise only differences that
are ALWAYS meaningless, never merely usually meaningless.** A false merge
silently attributes one person's story to another with nothing in the UI to
reveal it; a false split shows up in the extraction panel as two similar names
and is fixable by confirming them the same. Do not "improve" this by adding
confusable-letter pairs.

## Build chunks

1. **Models + normalisation + the write path.**
   - ✅ **1a DONE** (`df25171`) — `entity_names.normalize_entity_name` and the
     four ORM models.
   - ✅ **1b DONE** — `entity_extraction.py` + `entity_store.py`, wired into
     `check_entities_node` (one extraction, used twice) and
     `finalize_ingest_node` (writes entities, no longer calls `add_episode`).
     See "What 1b decided that the plan did not" below.
2. ✅ **DONE — the 11 existing entities are imported.** 10 rows, 11 mentions;
   `מונטריאול` verified as ONE row with TWO mentions by direct query.
   `scripts/import_graph_entities.py`, idempotent, dry-run by default.
3. ✅ **DONE — all seven read call sites moved off `graph_memory`.** No
   functional reference to it remains in `app/`. The read primitives live in
   `entity_store.py` alongside the write path. Folded in as planned: the
   entity map now emits **recording ordinals** instead of raw segment UUIDs.
4. ✅ **DONE — batched confirmation covering identity + type.** ONE interrupt
   per recording carrying both question kinds, ONE screen, ONE submit. The
   self-edge that made `human_confirm` loop is gone, and
   `POST /segments/{id}/confirm-entities` replaces the per-name
   `confirm-entity`. See "Chunk 4 decisions" below.
5. ✅ **DONE — `graph_memory` deleted, Neo4j/Graphiti dependencies and config
   removed.** See "Chunk 5" below, including two things it nearly broke.

## What 1b decided that the plan did not

Three things the plan did not settle, decided while building and worth
knowing before chunk 2:

**Neo4j is now FROZEN — nothing writes to it.** `transcribe_node`'s
`remove_episodes_for_segment` call was **removed**, not just `add_episode`.
It existed to stop a re-ingest duplicating an episode, which was correct for
as long as a rewrite followed it. With no rewrite it would delete and never
replace — and until chunk 2 imports them, the graph holds the ONLY copy of the
entity summaries, so re-analysing a recording would have silently destroyed
them. `segment_deletion` keeps its own removal call; there the deletion is the
point. Re-ingest is still idempotent one layer down: `entity_store` replaces a
segment's mentions rather than appending.

**A confirmed identity is applied by RENAMING.** `same_as_uuid` is a Neo4j
uuid and means nothing to Postgres, where the merge is not a suggestion to an
engine but `UNIQUE (producer_id, normalized_name)`. So "Moshe is the Moshe
Cohen you already have" is applied by writing the entity under the fuller
name, landing it on that row by the merge key. The opposite answer — *same
name, different person* — **cannot be expressed yet** and is chunk 4's job:
two entities with one name need a distinguishing name first. That is the same
single row the archive has today, not a regression.

**Known gap for the duration of chunks 2–3:** extraction and the WRITE path
are Postgres while the disambiguation READ (`get_entity_candidates`) still
queries the frozen graph. So a name first introduced after this change is not
offered as a confirmation candidate for later recordings, and new recordings
do not appear in the entity map until the read sites move. Accepted: accuracy
measured 0.991 with *and* without the entity map.

## Chunk 4 decisions

**The `pending_confirmation` payload changed shape**, from one question to
all of them:

```json
{"identity_questions": [{"name", "candidates", "question"}],
 "type_questions":     [{"name", "type", "alternative_type", "question"}]}
```

Safe to change outright because nothing was mid-flight: zero segments were in
`pending_confirmation` when it landed (checked against the live database
first). Had there been any, they would have needed draining or a shim — worth
checking again before changing it a second time.

**`POST /segments/{id}/confirm-entity` is REPLACED by `confirm-entities`**
(plural). Not versioned, not kept alongside: the old endpoint answered one
question and let the graph pause again, which is the exact behaviour being
removed. Frontend and backend deploy together here, so there is no window
where one calls the other's shape.

**A partial submit is a 400, never a default.** Both plausible defaults are
wrong in opposite directions — taking "same as existing" silently merges two
people, taking "someone new" silently splits one — and neither leaves a trace
in the UI. The producer is looking at the whole screen; the answer to ask for
is all of it. The button stays disabled until every question is answered, and
the server re-checks rather than trusting that.

**Inside the node, an unanswered identity question resolves to "someone
new".** That looks like it contradicts the above, and does not: the API can
never send one, so this is the node's own last-resort default, and it is the
recoverable direction. A false split shows up in the extraction panel as two
similar names and can be merged; a false merge silently attributes one
person's story to another with nothing to reveal it.

**A type answer must be one of exactly the two offered.** A third value could
only come from a client inventing one, and it would land in a column with a
CHECK constraint on it. `alternative_type` is cleared once asked, either way,
so the writer never re-raises a question already answered.

**`needs_confirmation` reaching `entity_store` is now a WARNING**, not
information. Confirmation runs before the write and clears what it asked
about, so a non-empty list means a torn classification was stored without the
producer ever being asked.

## Chunk 5 — what it removed, and the two things it nearly broke

Deleted: `services/graph_memory.py`, `services/neo4j_client.py`, their tests,
four one-off Graphiti-era scripts, the `neo4j` docker-compose service and
volume, the `/health` neo4j probe, all `NEO4J_*` / `GRAPHITI_*` settings, and
`neo4j` + `graphiti-core` from requirements.

Verified by BLOCKING both packages at import time and importing the whole app,
rather than by grepping — a transitive import would not have shown up in a
grep.

### ⚠️ `embeddings.py` imported from `graphiti_core`

Its docstring said it was "Independent module from graph_memory.py on
purpose", and it was — of the graph. Not of the *package*: it used
graphiti-core's `GeminiEmbedder`/`OpenAIEmbedder` wrappers and the
`GRAPHITI_EMBEDDING_*` settings. Deleting the dependency would have broken
every embedding in the app: chunk embeddings, transcript embeddings, and the
semantic term in both retrieval paths.

Worse, it would have broken them SILENTLY on a re-embed. **Every vector in the
database was produced by that exact model at that exact dimensionality**, and
cosine similarity across two different embedding spaces still returns a
plausible number — retrieval would just have quietly degraded.

Reimplemented directly against `google-genai` / `openai`, reproducing the call
graphiti-core made **exactly**: `contents` as a LIST containing the string,
and `output_dimensionality` passed explicitly. Both matter; either one changes
the vector. Proven rather than assumed — a stored vector re-embedded through
the new path scores **cosine 1.000000** against itself. Settings renamed
`EMBEDDING_PROVIDER` / `EMBEDDING_MODEL` / `EMBEDDING_DIM`, values unchanged,
now carrying a loud warning about what changing them costs.
`tests/test_embeddings.py` (new, 12 tests) pins the call shape.

### ⚠️ `google-genai` was only installed transitively

`llm.py` does `from google import genai` directly — the archive-read call and
every Gemini LLM call go through it — but it was never declared, arriving via
`graphiti-core[google-genai]`. Removing graphiti-core would have taken Gemini
with it. Now declared explicitly.

The `fastapi`/`starlette`/`httpx` pins existed only to satisfy that same
extra. They are now free-standing; **left pinned deliberately**, since
unpinning them is its own change with its own testing.

### 🚨 Leftover env vars now CRASH the app on boot

`Settings` uses pydantic-settings' default `extra="forbid"`, so an env var the
code no longer declares is a hard failure at import — `config.py` already
carried a comment about this trap for `DEEPGRAM_API_KEY`. This migration hits
it from the other direction: production still has `NEO4J_*` secrets set.

**Unset the secrets BEFORE deploying this**, in that order — it is safe
because the OLD code has defaults for all of them, so the running image
tolerates their absence:

```bash
fly secrets unset NEO4J_URI NEO4J_USER NEO4J_PASSWORD NEO4J_DATABASE \
  GRAPHITI_LLM_PROVIDER GRAPHITI_LLM_MODEL GRAPHITI_LLM_SMALL_MODEL \
  GRAPHITI_EMBEDDER_PROVIDER GRAPHITI_EMBEDDING_MODEL GRAPHITI_EMBEDDING_DIM
fly deploy
```

The local `.env` was cleaned of the same keys (it blocked startup outright).

## Shutting down AuraDB

**Yes — it can be shut down. Nothing reads or writes it.** Verified by
blocking `neo4j` and `graphiti-core` at import time and importing the whole
app, not by grepping.

Recommended order, because it is the one that cannot break the running app:

1. `fly secrets unset NEO4J_URI NEO4J_USER NEO4J_PASSWORD NEO4J_DATABASE` and
   the `GRAPHITI_*` ones (see the chunk 5 section — the deployed image must
   lose them BEFORE it gains code that no longer declares them).
2. Deploy.
3. **Pause** the AuraDB instance rather than deleting it, and leave it paused
   for a while. Pausing is free or near-free on Aura, reversible in a click,
   and buys a window in which "we deleted the only copy" is still recoverable.
4. Delete it once you are satisfied. Nothing here depends on that ever
   happening.

**What is lost when it goes, and why that is acceptable:** the graph held the
pre-import entity data — 11 entities and their summaries. All of it is
preserved in the repository, verbatim, in `scripts/import_graph_entities.py`:
the `IMPORT` table lists every name, type and summary as literal text, and
`SKIPPED` records `עכבר` and why. So the graph's content survives its
deletion in a form that is readable, diffable and version-controlled — which
the graph itself never was.

The one thing genuinely unrecoverable is Graphiti's own bookkeeping
(episode uuids, embeddings of entity names, `valid_at`/`invalid_at`). None of
it was ever read: the bi-temporal fields were unused, and the entity
embeddings were discarded downstream by a purely lexical gate.

## Known gap in the EVAL ITSELF: a failed API call scores 0.00

Found while running the chunk 5 gate, and it is not about chunk 5.

`_read_archive_for_ranges` catches **any** exception, logs a warning and
returns `[], None` — a no-story. That is correct fail-soft behaviour for a
live turn (a family member gets "I don't have a story about that" rather than
an error), but it means the eval **cannot distinguish "the model correctly
chose nothing" from "the API call failed"**. Both score 0.00.

`ARCHIVE_READ_MODEL=gemini-flash-latest` is the model `.env.prod.example`
already notes hit sustained 503s, so this is a live possibility, not a
theoretical one. It looks exactly like an accuracy regression: one cell of the
matrix drops to 0.00 and drags the mean down.

**When a rebaseline shows a single question at 0.00 while its neighbours are
fine, re-run before believing it**, and check the logs for
"Archive-read LLM call failed". A real regression is systematic; a 503 is one
cell. Worth fixing properly by having the harness distinguish the two — an
empty selection and a raised error are different events and the eval should
not average them together.

## Review points — DO NOT SKIP

- **After chunk 2: entities must be visible for review before going further.**
  Show the imported rows (name, type, mentions, per-mention summaries).
- ✅ **DONE — eval re-run after chunk 3: v2 = 0.999, stdev 0.000 over 5 runs,
  every question returning ONE distinct answer. Identical to the baseline, so
  moving all seven read sites cost nothing.**
- 🛑 **CHUNK 5 IS BLOCKED ON AN EXPLICIT GO-AHEAD, AND ON A LIVE END-TO-END
  RUN.** Stated by the producer on 2026-07-28: *"I want to use the app end to
  end on the new tables before `graph_memory` is deleted, since that's the one
  step I can't walk back."* Deleting `graph_memory` and dropping Neo4j is the
  only irreversible step in this plan — Neo4j still holds the pre-import
  entity data, which is the sole way back if the import turns out to have lost
  something the review did not catch.

  Nothing in chunks 1–4 depends on chunk 5 happening. **Do not start it, and
  do not "tidy up" the leftover `graph_memory` references, on the assumption
  that it is next.**

  What the live run needs to cover, since the review only exercised the
  read/write paths through tests and scripts: record a take and watch the
  batched confirmation screen appear and submit; check the "extracted from
  this" panel shows per-recording summaries; ask questions in `/talk`; and
  delete a take, confirming a shared entity survives.

## Chunk 1b verified against real data

`097b606b` was re-run through the real ingestion path after the import —
deliberately on ONE segment, before chunk 3 makes the read sites depend on
this. It exercised extraction, disambiguation and the write path end to end
against the live database:

- extraction returned `{מונטריאול, place, alternative_type=None}` and correctly
  **omitted `תכנות` (a common noun) and `לארץ` (a generic reference)** — the
  `עכבר` rule working on a case it had never seen. On
  `gemini-flash-lite-latest`, the weakest model in the config.
- `check_entities_node` auto-resolved against the frozen graph (exact match),
  and `_apply_entity_resolutions` carried it into the write.
- the merge held: still **ONE** entity row with **TWO** mentions, totals
  unchanged at 10 entities / 11 mentions — no duplicate, no orphan swept.
- the NULL below became a real per-recording summary.

## The one imported summary that was NULL — now filled

`מונטריאול` is the only entity two recordings mention, and Graphiti stored one
summary per entity. That summary — "the place he flew to for a year and a half
right after discharge from the air force" — is a restatement of `5d128933`
("right after discharge I flew to Montreal for a year and a half") and contains
nothing from `097b606b` ("when I was in Montreal I studied programming"). It
was never really consolidated: Graphiti kept one account and dropped the other.

So it is attributed to `5d128933`, and **`097b606b`'s mention has `summary =
NULL`**. Copying it to both would make the career recording claim something it
never said — the exact failure per-mention summaries exist to prevent.

**RESOLVED** by re-running that segment through the normal ingestion path (see
above). It now reads "המקום שבו למד הדובר תכנות". No special tooling was
needed — the extractor produces a per-recording summary by construction.

Note the priority this was handled at, which is the right one: summaries feed
only the extraction panel, never the archive read. `_format_entity_map` is
names and ids, with no summaries at all, so a NULL here cost a blank line in a
popup and nothing else. The re-run was worth doing to exercise the write path
on real data, not to fill the field.

## Type backfill for the 11 existing entities

Classified from the stored summaries — no LLM needed at this scale, and 9 of
11 are stated outright in the summary text:

| Type | Entities |
| --- | --- |
| `person` (6) | אילנה, צבי, ניר, חן, עדי, רז — summaries read אמא/אבא/אח של הדובר |
| `place` (2) | טבריה, מונטריאול |
| `organisation` (2) | חיל האוויר, **הכפר הירוק** — see below |
| skip (1) | **עכבר** — a common noun, not a named entity. Confirmed decision: extraction should have skipped it. Now in the extraction prompt: *if it fits no category, it is not a named entity — omit it.* |

**`הכפר הירוק` is `organisation` on the PRODUCER'S knowledge, not on the
text.** Its summary reads "where the speaker studied from age 14", which on
its face describes a place; the producer confirmed it is a boarding school.
**Do not "correct" this to `place` from the transcript** — the transcript
cannot settle it, which is exactly why `alternative_type` exists and why this
entity is the plan's worked example of a torn classification. If the extractor
ever proposes `place` for it, that is the confirmation flow doing its job, not
a regression.

## Chunk 3 results: the latency claim, measured

`_build_entity_map` over the same 12 recordings, 6 identical passes:

| | mean | range | spread |
| --- | --- | --- | --- |
| Neo4j, one round trip per recording | **4.13s** | 1.35–9.55s | 8.20s |
| Postgres, ONE bulk query | **0.372s** | 0.351–0.475s | **0.124s** |

**~11x faster, and the run-to-run variance is gone** — which was always the
bigger prize. It was 100% of a turn's latency variance and the reason seek
mode could not be measured at all (see "After this migration").

Note what actually fixed it: the per-segment SHAPE, not the database. The
graph had no bulk form, so the fix is `get_entity_names_for_segments` doing
one query, and the same fix applied to `relevance_scorer` and
`response_assembler`, which each looped a round trip per candidate.

## RESOLVED: the entity map's ids were unresolvable

Rendering both prompt blocks side by side showed that
`_format_annotated_transcript` labels recordings `RECORDING 1..12` while
`_format_entity_map` labels the same recordings by **raw segment UUID**. The
model has no mapping between them — it can read `אילנה: 502fb283…` and cannot
discover that this is `RECORDING 1`.

This is very likely why accuracy measured 0.991 with *and* without the entity
map: the pointers were unresolvable, so the only surviving signal was the bare
existence and spelling of names. It is also inconsistent with a deliberate
decision made elsewhere — the transcript formatter goes out of its way NOT to
print segment UUIDs, because the model would return one where a unit id was
expected and silently produce a no-story.

**FIXED in chunk 3.** `_format_entity_map` now emits recording ordinals, from
the SAME `_recording_ordinals` helper `_format_annotated_transcript` uses for
its headings — so the two cannot drift back apart. Verified against the live
archive:

```
- מונטריאול: RECORDING 5, RECORDING 7
- חיל האוויר: RECORDING 3
```

RECORDING 5 is "what did you do right after discharge" and 7 is "how did you
start your professional path" — the two recordings that actually name it.

An entity whose recordings were all filtered out of the transcript block is
now **omitted** rather than printed with a dangling pointer: a name pointing
at a recording the model cannot see is the same unresolvable reference in a
quieter form.

Note also that `_format_entity_map` contains **no summaries at all** — it is
purely name → recordings. Per-mention summaries therefore cost the prompt
nothing.

### And the answer, now that the measurement is clean

Re-ran the scored set 3× with and 3× without the map, against the FIXED
version:

| | mean | per-run |
| --- | --- | --- |
| WITH map (ordinals) | **0.9987** | 0.999, 0.999, 0.999 |
| WITHOUT map | **0.9987** | 0.999, 0.999, 0.999 |

**Identical on every one of the 7 questions**, `montreal` included — the
cross-recording case the map most plausibly helps. So the original
0.991-either-way finding survives the fix: the map was not being ignored
because its pointers were broken, it simply is not needed on an archive this
size. The model reads all 12 recordings in one call and finds the connection
itself.

Two caveats before drawing a conclusion:
- **The scored set has no broad question**, the known eval gap, and that is
  where a name→recording index is most likely to earn its keep.
- The archive is ~2.1K tokens. A map matters when the model cannot hold
  everything at once, which is exactly the regime v2 does not yet reach.

**OPEN DECISION — do not drop it unilaterally.** It now costs 0.372s and one
query, so keeping it is nearly free and it is the obvious thing to want when
the archive grows past a full context. Dropping it would remove code that
measures as useless TODAY on a set that cannot test it properly. Recommend:
keep, and revisit together with the coarse pre-filter at the ~150K-token
threshold.

## What we give up (honest list)

1. ~~**Automatic entity-summary consolidation**~~ — **NOT GIVEN UP, because it
   never worked.** This was listed as the one real capability being traded
   away. Chunk 2 disproved it on the only entity where it could be tested.

   `מונטריאול` is the sole entity two recordings mention, so it is the sole
   case where Graphiti had anything to consolidate. Its stored summary — "the
   place he flew to for a year and a half right after discharge from the air
   force" — is a restatement of `5d128933` and contains **nothing** from
   `097b606b` ("when I was in Montreal I studied programming"). It did not
   merge two accounts; it kept one and dropped the other, silently, with the
   result still presented as the entity's summary.

   The replacement is strictly better, and measurably so: re-running
   `097b606b` through chunk 1b's path produced "המקום שבו למד הדובר תכנות"
   ("the place where the speaker studied programming") — exactly the content
   the consolidation had discarded. **Two mentions now hold two accounts where
   the graph held one.**

   Worth remembering as a general caution: this capability was on the "honest
   list" for a year on the strength of the feature existing, never on evidence
   of it working. The archive contained exactly one case that could test it,
   and it failed.
2. **Hybrid semantic entity search** (embeddings + BM25 + RRF) in
   `get_entity_candidates`. Costs nothing today because `names_are_similar` (a
   purely lexical gate) discards it downstream — but we lose the *option* of
   matching "my dad" → `צבי` by meaning. pgvector 0.8.1 is installed and
   `embeddings.py` already runs the same model, so an embedding column is
   additive later.
3. **Bi-temporal fact validity** (`valid_at`/`invalid_at`). Never used;
   `year_start`/`year_end` serves the timeline case better.
4. **Graphiti's write-time dedup across name variants** — replaced by
   `normalized_name` + human confirm. More predictable, but only as good as
   the normalisation function.

Relationship extraction is deliberately NOT on this list: Graphiti is capable
of it and has produced zero edges on this data.

## Deletion becomes one transaction

`entity_mentions.raw_segment_id` cascades, so deleting a recording removes its
mentions atomically; orphaned entities go in the same transaction via one
`DELETE FROM entities WHERE NOT EXISTS (SELECT 1 FROM entity_mentions …)`.
Graphiti's MENTIONS-count-of-1 rule becomes that `NOT EXISTS` — same
semantics, now enforced by the engine.

**Caveat:** the stored video file remains outside the transaction. No
transaction spans Postgres and object storage. But the failure degrades from
*ghost entity in a graph nobody is watching* to *orphaned file on disk* —
invisible to answers, and re-runnable.

## Not in scope for this work

**No self-entity is created at signup.** Migration 0012 creates one per
EXISTING producer and its own comment says new producers get theirs "at
signup (application code)" — that code does not exist yet. Nothing breaks
today (`entity_store` never needs the row, and its orphan sweep skips
`is_self` rather than requiring it), but a producer who signs up after the
migration has no tree root, and relations cannot be expressed without one.
Belongs with the relation capture flow, not before it.

`entity_relations` and the year columns are created by migration 0012 but
**nothing populates them**. The capture flow (LLM proposes relations at
ingestion → producer confirms in the batched step) and the family-tree page
are separate, later work. The schema is settled now only so the tables do not
have to move twice.

## After this migration

Queued, in no fixed order — both are blocked on this work landing:

- **Re-evaluate seek mode.** Player-side seeking (select ranges, seek the
  original video, skip the ffmpeg trim/concat) was built and then discarded:
  its ~3s saving was inside the Neo4j entity-map variance (1.35s–9.55s), so it
  could not be told apart from noise. With that variance gone the measurement
  becomes meaningful, and the idea deserves a second look on its own merits
  rather than being written off on a measurement that could never have shown
  it. Note the discarded implementation had a real bug worth not repeating: a
  `setTimeout` measured WALL time rather than MEDIA time, so a paused or
  buffering clip cut in the wrong place.
- **Family-tree page.** Built from `entity_relations` once they are populated:
  `type='person'` entities as nodes, `is_tree_edge` relations as edges, the
  `is_self` entity as root, `year_start`/`year_end` as lifespan. The property
  worth designing around is that every edge carries `source_segment_id`, so a
  relationship links back to the recording where it was said — clicking
  "brother" can play the producer saying "I have four brothers". That is a
  materially better artifact than a tree drawn from a form, and it falls out
  of the schema for free.

  Needs first: the relation capture flow (LLM proposes at ingestion → producer
  confirms in the batched step from chunk 4), and note that relations cannot
  be expressed at all without the `is_self` root, which migration 0012
  creates.

---

## Working

**Recording → ingestion**
- `/record` interview flow, now an in-shell view (`RecordPanel`) alongside
  Settings rather than a standalone route. Old `/record` URL redirects.
  Styled with the app's own dark design system (`btn-primary`, `glass-card`,
  …); the warm "calm" theme is now `/talk`-only — see the note in
  `globals.css` before reintroducing it anywhere.
- **Deepgram nova-3** for both ingestion and the live path, behind
  `INGESTION_STT_PROVIDER` / `LIVE_STT_PROVIDER` (`deepgram|local`).
  `smart_format=False` for ingestion. Local Whisper remains the fallback.
- Word-level timestamps on every `TranscriptChunk` (migration `0010`).
- **One question can hold SEVERAL recordings.** Ingest APPENDS; replacing a
  take is delete + record. `DELETE /segments/{id}` is the only thing that
  destroys a recording. Count DISTINCT `question_index` for "answered", never
  segment rows. Uploading a video reuses the recording entry point exactly
  (presign → PUT → `/segments/ingest`) — there is no second ingestion path.
- Takes of one question are grouped in the prompt and marked `(take N of M)`;
  a single-take question prints byte-identically to before.
- "Extracted from this" panel per recording (`segment_extraction.py` →
  `GET /segments/{id}/extraction`), read-only. `_load_entities` is the single
  seam the Postgres migration replaces.
- Entity extraction per segment now writes `entities` / `entity_mentions` in
  Postgres (`entity_extraction.py` → `entity_store.py`). Reads still go
  through Graphiti until chunk 3 — **see NEXT UP above.**

**Chat modes** (producer-level `User.chat_mode`, migration `0011`)
- `video_clips_v2` — whole-archive read, single LLM call, utterance-unit
  selection. **The default and primary mode** (2026-08-13, migration
  `0027`); needs no avatars row anywhere.
- `avatar` — LLM → TTS → MuseTalk. Optional, off by default; enabling it
  in Settings is the one place the app requires a ready avatar.
- `video_clips` (v1, chunk retrieval, multi-step) was REMOVED 2026-08-12
  after the A/B settled it — docs/V1_REMOVAL_PLAN.md; tag
  `pre-v1-removal` holds the last tree that had it.

**v2 capabilities**
- Utterance-unit selection — mid-sentence cuts are structurally impossible.
- Cross-recording answers (Montreal correctly returns two recordings).
- Follow-up referent resolution via the interview question (the wife case).
- Shown-unit memory in `Message.message_metadata`; history renders what was
  actually played, so pronouns have an antecedent.
- Proactive follow-up suggestions with Yes/No, validated against real unseen
  units, accepted via the normal question path.

**Frontend**
- `/talk` (family) and the producer chat screen share one behaviour hook
  (`useVideoClipChat`) behind two different layouts.
- Avatar/voice setup hidden outside `avatar` mode.
- Hands-free mic with clip-playback gating; `SILENCE_DURATION_MS=1000`,
  `MIN_SPEECH_MS=400`. Gating is enforced **during** a recording, not just at
  its start — see the mic section below for why that mattered.

**Tests:** 541 backend passing; frontend `tsc`, `eslint`, `next build` clean.

---

## Evidence: v1 vs v2

> **Historical record (2026-07-25).** This A/B is what settled the v1
> removal (2026-08-12, docs/V1_REMOVAL_PLAN.md) — the numbers stay as the
> decision's evidence; the v1 arm and the harness that measured it no
> longer exist in the tree.

Retrieval-decision phase only (excludes the shared ffmpeg trim/concat + upload,
which is identical code in both modes).

| | v1 chunk-retrieval | v2 archive-read |
| --- | --- | --- |
| Accuracy (IoU vs known-correct, 7 scored questions) | **0.849** | **0.991** (stdev 0.000 over 5 runs) |
| LLM calls per question | **5.9 avg** | **1.0** |
| Latency per question | **10.76s avg** | **5.75s avg** (range 4.15–7.10s) |
| Tokens per question (est.) | ~1,927 | ~2,512 |
| Montreal (cross-recording) | **0.00** — misses the career recording entirely | **1.00** |
| Repeated-run consistency (3 runs × 12 questions) | **12/12 stable** | **10/12 stable** |

Measured 2026-07-25 via `compare_retrieval_modes.py` (3 runs per question,
12 questions), with `ARCHIVE_READ_MODEL=gemini-flash-latest` and
`ARCHIVE_READ_THINKING_BUDGET=128`.

v2 wins on accuracy mainly because it reads the whole archive at once and can
join material across recordings; v1's multi-step chain drops the second
recording on Montreal entirely. v2 is also **~2x faster and ~6x cheaper in
call count** (one big call instead of ~6 small ones), at ~30% more tokens —
and it depends on the archive fitting in context (see the scaling gap below).

**The two questions v2 varied on were `family` and `army-broad`** — i.e. both
of the broad questions, and only those. That is direct corroboration of the
accepted-non-determinism characterisation in CLAUDE.md: core and narrow
questions were stable across all 3 runs; only broad questions moved, by 1–2
peripheral units.

Caveat on the thinking-budget latency claim: pinning `budget=128` moved the
harness average **6.89s → 5.75s (~17%)**, not the ~2x an earlier isolated
single-question benchmark suggested. The isolated figure was measured under
lighter load; the harness average is the one to trust.

---

## Turn latency: where it stands (profiled 2026-08-01)

**~6.9s typed / ~9.0s spoken**, warm archive cache. Cold cache (first question
after a restart or re-ingest) adds ~1.4s.

Full end-to-end profile of one `video_clips_v2` turn, warm, against the live
database and the real Gemini call:

| stage | time | share |
| --- | --- | --- |
| **archive-read LLM call** | **~4.0-4.6s** | **~63%** |
| DB round trips | ~0.9s | ~14% |
| ffmpeg trim + concat | ~1.35s | ~20% |
| storage / redis / glue | ~0.02s | ~0% |
| STT (spoken turns only, Deepgram) | ~2.1s | — |

**Two caveats on those numbers.** Storage is LOCAL here (`USE_LOCAL_STORAGE`),
so the download/upload rows read ~0.02s; in production `_assemble_and_upload_clip`
moves ~2.8MB down and ~1MB up over R2 per turn and will look materially worse.
And Redis is not running locally, so the clip-cache rows read 0.000s — with
Redis live an exact repeat question skips ffmpeg and the upload entirely,
though it never touches the LLM call, which is the actual bottleneck.

### Landed

- **The three independent pre-LLM reads now run concurrently** (`select_units`).
  The archive bundle, the shown-unit history and the recent turns share no
  data and were sequential only by writing order. Measured on the live DB:
  **1.083s → 0.358s at steady state, ~0.70s per turn.** This is round-trip
  latency, not query work — a trivial `SELECT 1` to Neon costs ~0.17s.
- `pool_pre_ping=True` costs **+0.156s per session checkout**, still paid on
  every one. Worth less now that three of the four overlap (~0.15-0.3s left).
  Not taken: it exists so a connection Neon dropped while idle fails fast.

### 🛑 DECLINED — the thinking-token lever. Do not re-open without reading this

The archive-read call is ~63% of the turn and its latency tracks **generated**
tokens at ~7ms each, of which thinking is 320-1,525 (mean ~600) against ~100
tokens of actual output. That makes "spend less thinking" the obvious lever.
It was investigated in full on 2026-08-01 and **declined by the producer.**

**There is no budget lever at all.** On `gemini-3.6-flash` the call already
sits at the model's floor: `budget=1`, `budget=128` and `thinking_level='low'`
all spend an identical **586** thinking tokens, `budget=0` is rejected (400),
and only `budget=-1`/`'high'` goes UP (1918). Turning the budget down does
nothing. The only lever is a different model.

**The model swap is worth ~1.4s and costs the narrow-vs-broad distinction.**
Side-by-side, 3 runs per arm, real questions and real answers:

| question | `gemini-3.6-flash` (kept) | `gemini-3.5-flash-lite` |
| --- | --- | --- |
| `army-narrow` (which ROLE?) | `u10-u13`, **6.1s** | `u10-u17`, **13.7s** — the broad answer |
| `family` | `u1-u5,u37`, 15.6s | `u1-u5`, 11.0s — drops the grandchildren line |
| `school` | no story | `u6,u7` — answers *where*, not *what* |
| montreal, brothers, ilana, army-broad, wife-pronoun, no-answer | — | **identical** |

`army-narrow` is the disqualifying one: a narrow role question getting the
whole army recording is the "breadth falls out of the question" mechanism
failing, which is the core of the design, not a tuning detail.

Recorded honestly: flash-lite was **faster** and **more stable** (3/3 where
flash varied on `army-narrow` and `family`). Declined anyway.

**Prompt caching is not a lever either.** Gemini implicit caching does not
fire on this call at all (0/12 hits). Explicit caching works — 99.7% of the
prompt served from cache — and buys **~0.15s**, because prefill is not this
call's cost. On matched pairs with identical generated-token counts the
cached/uncached delta was +0.05s, −0.31s, +0.19s: mean ≈ 0, sign flips.
`cache_read_tokens` is now on the `llm_usage` log line so this stays visible.

**What is actually left**, in rough order of size: the unprofiled R2
download/upload in production, ~1.35s of ffmpeg (already `-preset veryfast`;
player-side seeking was measured and rejected, ceiling ~1.3s), ~2.1s of
Deepgram STT, and the remaining `pool_pre_ping` overhead. None of them is the
LLM call, and the LLM call has no lever left that does not cost answer
quality.

---

## Not done / in progress

- **Retest after the mic fixes.** The clip-echo and gate-stranding fixes below
  are committed and build clean but have not been exercised live.
- **Follow-up suggestions have not been seen live**, only verified against the
  real archive through `select_units`.

### DECIDED — not a bug: `מה עשית בצבא?` returns the whole army recording

Returns u6-u9 (19s), including "I'd go home every two weeks". Traced and
**stable 5/5**, so it is a genuine relevance judgment, not the accepted
marginal variance.

**This is correct behaviour and must not be "fixed".** A broad question
deserves a broad answer. The mechanism narrows correctly when the question
narrows — asked about the corps/role specifically ("which corps did you serve
in?") it returns u6 alone. Both halves of that are the same mechanism working,
not one working and one failing.

What produces it: the model reads the question as a paraphrase of that
recording's own interview question ("tell me about your military service…")
and matches at RECORDING level rather than unit level. That is the
interview-question anchor — the same thing that fixed the unnamed-spouse case
— applied to a broad question.

Anyone tempted to narrow this should note that the only way to do so is to
weaken the question-level match, which breaks the narrow case in exchange.
See CLAUDE.md's "breadth falls out of the question" rule: there is
deliberately no duration cap, no question-type classifier and no length
heuristic anywhere in the code.

### Resolved: the mic bugs that were corrupting live testing

The earlier hypothesis for the send-wiring bug — `?? avatars[0]` handing
`create_session` a non-`ready` avatar — was **WRONG**. Verified against the DB:
the producer has exactly one avatar, `status='ready'`, and every recent session
created successfully against it. Sessions connect and messages flow.

What was actually happening (found in the persisted `Message` rows):

1. **The app was recording SYSTEM OUTPUT and submitting it as the user's
   questions.** Clips came back as verbatim "questions" that the system then
   answered — which is what made several "retrieval bugs" look real. Retrieval
   was correct at every step. Proven from the backend raw trace: real `audio`
   messages (~1 MB) arrived *with the physical mic disconnected*, and STT
   segment timings showed recordings of 29s/43s/48s that were **~30 seconds of
   silence followed by the clip**.
   **Root cause:** when no real input device exists, `pickPreferredAudioDevice`
   returns undefined and the old code dropped the `deviceId` constraint and let
   the browser choose — selecting a **loopback device (Stereo Mix) that records
   system output**. Exactly the failure `audioDevices.ts` was written to
   prevent: it filtered loopback devices out of the candidate list, then fell
   through to "whatever the browser offers" when the list came back empty.
   **Secondary cause:** end-of-turn is only detected *after* speech is heard, so
   a recorder that hears nothing never stops — hence the 48-second segments.
   **Fixes:** (a) REFUSE to open a stream when there's no real input device and
   surface it as `micUnavailable: 'no-input-device'` in the UI; (b)
   `MAX_SEGMENT_MS = 20000` ceiling — discard if no speech was heard, send
   normally if some was; (c) abort and discard any in-flight segment the moment
   playback starts (defence in depth).
   Note `echoCancellation` stays **off**: it was briefly switched on under an
   acoustic-echo theory the trace disproved (a digital loopback can't be
   cancelled), and it can shift the calibrated ambient threshold on a real mic.
2. **Producer screen only: `isClipPlaying` could strand at `true`.** That
   screen replaces one keyed `<video>` in place; unmounting a *playing*
   element fires neither `onPause` nor `onEnded`, so the gate never reopened
   ("I speak and nothing gets in"). **Fix:** reset the gate when the clip id
   changes. `/talk` mounts a player per answer and cannot hit this.
3. **StrictMode creates two sessions per mount — investigated, benign.**
   Effects re-run *sequentially on one component instance* with shared refs,
   and the `cancelled` guard stops any late-resolving `getUserMedia`, so there
   is **no** second live stream and **no** second `isClipPlaying`. The only
   artifact is an orphaned, empty session row in dev; it has no WebSocket and
   no messages, so it cannot pollute the shown-unit history either. No fix.

---

## Identity confirmation across concurrent recordings (2026-08-06)

**Fixed.** A recording analysed while an EARLIER one was still awaiting
confirmation could not see that earlier recording's people, so the same person
named two ways silently became two entities with no question asked.

Measured on the live archive:

| time | event |
| --- | --- |
| 08:36:20 | recording 1 ingested ("חבר בשם איציק") |
| ~08:36:30 | recording 1 **pauses** on a type question |
| 08:36:51 | recording 2 ingested ("עם איציק כהן") — its identity check runs here |
| 08:37:51 | producer answers → recording 1 finalizes → `איציק` **written** |
| 08:38:14 | recording 2 finalizes → `איציק כהן` written as a second person |

The matching logic was never at fault: `get_entity_candidates` returns `איציק`
for `איציק כהן`, and `_names_are_similar` scores them similar. At the moment
of asking there was genuinely nothing to match against.

**A write-lock cannot fix this, and the reason is the graph order:**

```
check_entities → [human_confirm PAUSE] → score_importance → finalize_ingest ← entities written
```

The entity write is DOWNSTREAM of the human pause, so "wait for the previous
recording's write" means "wait for the producer's answer" — 91 seconds in the
case above. Considered and rejected for that reason; it would only help when
the earlier recording raises no questions at all.

**The fix:** `entity_store.pending_entity_candidates` also offers names from
the producer's other recordings that are still awaiting confirmation. It works
because **a confirmed identity is applied by RENAMING** —
`_apply_entity_resolutions` reads `resolved_name` and treats `same_as_uuid` as
nothing but a boolean gate. So a candidate needs no entity row: answering "yes,
the same" writes this recording's entity under the other name, and both land on
one row via `UNIQUE (producer_id, normalized_name)` whenever that row appears.
Candidate ids carry a `pending:` prefix so they can never be mistaken for real
entity ids.

Known cost: if the producer then renames that person on the earlier recording's
screen, this answer points at a name that no longer exists and a third row
appears. Rare, visible in the extraction panel, and preferable to silent
fragmentation.

`איציק` / `איציק כהן` were merged by hand afterwards — one row, two mentions,
both per-recording summaries intact.

### 🛑 SETTLED — this does NOT change `/talk` answers in `video_clips_v2`

Do not re-litigate. Traced through the code and confirmed by A/B measurement:

- The only retrieval consumer of confirmed identity in v2 is
  `full_archive_retrieval._build_entity_map` (line ~835), which now emits one
  line instead of two.
- **That block does not drive selection.** Same question, three map variants
  (real / removed entirely / both names forced onto one line), two runs each:
  byte-identical unit selections. Retrieval matches on text similarity between
  the question and unit content. This is a sixth data point agreeing with the
  0.9987-with-and-without measurement already recorded above.
- The prompt introduces the block only as `ENTITY MAP (entity name -> the
  RECORDINGS that mention it)` — an index, with no instruction that separate
  lines mean separate people.

So the fix corrects the **entity table, family tree, timeline, repeat-question
avoidance (`get_entity_candidates` for later recordings), and deletion safety**
— and changes nothing about what `/talk` answers in v2.

Two things worth keeping straight, because they were conflated during the
investigation:

- **The entity map is not the family tree.** Different systems, different
  consumers.
- In `avatar` mode the merge IS load-bearing —
  `retrieval_service.find_segments_mentioning[_scored]` finds other recordings
  sharing an entity, and `relevance_scorer`/`response_assembler` use entity
  names as a ranking signal. Only v2 is insensitive to it. (This was also
  true of v1 `video_clips` while it existed.)

## Two people with one name — SHIPPED 2026-08-08

Full write-up in [ENTITY_DISAMBIGUATION.md](ENTITY_DISAMBIGUATION.md), including
§8 on what the plan got wrong. Summary of what changed and what it cost.

**The problem was never in retrieval.** `/talk` was conflating an uncle and an
army friend both called `אמנון` because the ARCHIVE was: `UNIQUE (producer_id,
normalized_name)` means the merge key IS the name, so the second `אמנון` merged
onto the first by construction. One row held both people, and the family tree
showed the friend's army recordings as the uncle's moments.

**Step 1 — capture (`9e71adb`, `3e14428`).** Two separate defects, not one:

- "Someone new, not listed" about a colliding name was ACCEPTED and then
  silently did the opposite — no distinguishing name meant the new entity
  landed on the old row. Now `new_name` is required exactly when the merge key
  collides, and rejected if it collides with anything else.
- That validation was unreachable for the actual case. `check_entities_node`
  auto-resolved whenever exactly one candidate matched VERBATIM, so two people
  both called exactly `אמנון` never raised a question at all. Now a verbatim
  match always asks, gated by `identity_asked_at` (migration 0021) so it fires
  once per person rather than once per mention.

`identity_asked_at` is deliberately NOT backfilled. Stamping the existing rows
would have made the change invisible on exactly the archive it was built for;
unstamped, the next mention of each person asks once and never again, and that
one pass is the only thing that can surface a conflation that already happened.

Two older defects surfaced while tracing, both live at the time:

- Every "someone new" answer crashed with an `UnboundLocalError`.
- **Identity questions had been rendering an EMPTY legend since 64eef15** —
  `_confirmation_question` stopped being called when chunk 4 batched the
  interrupt. The options still read sensibly on their own, which is why it
  survived for weeks.

**Step 2 — the repair, done by the producer.** Rather than the migration script
the plan proposed, the affected recordings were deleted and re-uploaded, letting
step 1 raise the question naturally. It worked; the archive now holds `אמנון`
(friend, 2 recordings) and `אמנון נחום` (uncle, 1 recording with the tree edges).

⚠️ **Deleting a recording cascades TWICE, and the second path is easy to miss.**
Once from `raw_segments` via `source_segment_id`, and once from `entities` via
`from_entity_id`/`to_entity_id` when the orphan sweep removes a person nobody
mentions any more. **The second path destroys MANUAL tree edges**, which
migration 0020's docstring calls permanent ("it survives deleting every
recording about that person"). It survives the segment cascade; it does not
survive the person's last mention being deleted. Counting only the first path
under-reported the blast radius as 7 relations when it was 15, 9 of them
hand-placed. Check both paths before quoting a number.

**Step 3 — retrieval (`fcd5054`).** Inline entity tags in the transcript block
at query time, plus a `clarify` output. Nothing is written to the archive:
`TranscriptChunk.text`, `word_timestamps` and `RawSegment.transcript` are
untouched, and the video is cut from unit ids and word times that never saw a
tag. A clarification REPLACES the answer rather than accompanying it, and is
sent before the no-story branch so an archive holding two `אמנון`s never reports
holding none.

Two design points worth keeping:

- **Stricter confusability than the confirmation screen.** `names_are_similar`
  has a character-similarity fallback that grouped `אירה` with `יאיר` — fine
  when a false positive costs one question a human answers in a second, wrong
  when it costs asking a LISTENER to choose between two people nobody could
  confuse.
- **The prompt is byte-identical when no two people share a name**, asserted by
  a test. An archive without duplicates provably cannot over-ask.

Measured, 12 questions x 3 runs x 2 arms plus 5 same-name cases: clarification
rate 0 on the unambiguous set (PASS), 5/5 on the same-name cases.

### 🛑 The clarification gate PASSED while the feature was destroying answers

The single most important thing this work produced, and it generalises well
beyond it.

The agreed precondition was "clarification rate on the existing unambiguous
questions must be 0". Four prompt edits passed it and each broke something
unrelated:

| edit | intended target | what actually moved |
| --- | --- | --- |
| clarify JSON at end of Rules | output format | `school` 8 -> 0, another case 4 -> 0 |
| example `"my friend from the army"` | which person is meant | `army-narrow` 2 -> 4 |
| swap to `"my neighbour"` | fix the above | `school` 8 -> 0 |
| tag on RECORDING line vs inline | annotation placement | `school` 8 -> 0 |

None of those questions involves a name, a tag or a clarification. What caught
them was diffing SELECTED UNITS between arms on unrelated questions — now
generalised into `scripts/prompt_regression.py`, which should gate every future
prompt edit. A feature's own gate tests whether it misbehaves on its own terms;
it says nothing about what it does to everything else.

**The `army-narrow` cause, isolated (n=6, tag and instruction varied
independently):** the tags do nothing on their own; the INSTRUCTION TEXT does
it, specifically its example `"my friend from the army"` sitting in the prompt
while the question asks what role you served in. Swapping four words fixes it
6/6 — and costs `school` entirely (8 -> 0, 4/4). Every configuration measured
costs exactly one question, always the same two. The shipped one is the only
one that keeps `school`; losing a whole answer beats gaining two units on a
narrow question. All five same-name cases are IDENTICAL across both examples,
so this wording is a pure collateral-damage knob.

**Do not reword `_DISAMBIGUATION_BLOCK` without running the regression check on
both sides.**

**UPDATE 2026-08-09 — the `army-narrow` cost is gone, paid off by an unrelated
change.** The tailored no-story line (below) added an `about` field to the
empty-selection rule, and that restored `army-narrow` to `u11,u12` at 6/6.
`family` gained two units in the same change that are genuinely about the
family. Both were flagged by `prompt_regression.py` as known-marginal and
confirmed real at n=6 per arm. Leakage is not always damage — but it is always
worth measuring, and it was found by the harness rather than by noticing.

## The no-story line names who the question was about — SHIPPED 2026-08-09

`"אין לי סיפור על זה"` is true but reads as the system failing to understand.
Asked "what else did you do together?" about a specific person, the honest and
much warmer answer is `"זה כל מה שיש לי על אמנון"`.

**v2 only.** The subject-resolution machinery exists only in
`full_archive_retrieval`; v1 and avatar keep the generic string.

Three constraints shaped it:

- **No generated prose.** `response_assembler.py:42` records that the no-story
  answer is "never an LLM-generated apology/filler, exactly per the project
  plan". So this follows the BRIDGE_PHRASE_TEMPLATES pattern twelve lines
  below it: fixed Hebrew templates, `{entity}` the only thing injected. The
  model returns a NAME and nothing else.
- **The name must already exist in the archive.** "I don't have another story
  about X" is a claim about the archive, so X is checked against the real
  entity list and the archive's own spelling is what gets shown. A model
  answering "what pets did you have?" by naming a plausible-sounding pet would
  otherwise have us assert it — worse than the generic line, not better.
- **"another" vs "any" is decided from the session record, not by the model.**
  Whether that person's recordings were already played is in `shown_units`.
  Two template banks; saying "I have nothing about אמנון" after playing three
  clips about him reads as forgetting the conversation.

Why a lexical match on the question could not do this: the question that
prompted it — "מה עוד עשיתם ביחד?" — contains no name at all. The subject comes
from the previous turn, which the model already resolves.

Measured (`scripts/eval_no_story_subject.py`, 6 cases x 4 runs): 6/6 PASS. All
four existing no-story questions return the untouched generic line; the
control still plays a clip; the follow-up case returns
`"זה כל מה שיש לי על אמנון"`.

### 🚨 It told the listener there was nothing more when there was — 2026-08-09

Reported live, and the most serious defect this feature produced. Five of
אמנון's twelve units had played; the archive-read found nothing for
"מה עוד עשיתם יחד?" and the reply went out as `"אין לי עוד סיפור על אמנון"`
while `u11-u13` — the entire army story — sat unplayed.

**The `about` change was NOT the cause, and measuring said so before anything
was written.** Replayed with the exact live history window, n=5 per arm:

```
WITHOUT the `about` sentence   empty selection   5/5
WITH it (shipped)              u11, u12, u13     5/5
```

The empty selection predates the change; the change actually answers this turn
correctly in replay. What it changed was the WORDING of the miss — from a
vague `"אין לי סיפור על זה"` to a specific false claim. That is a real
regression in its own right: a specific falsehood tells the listener to stop
asking about someone the archive still has stories about.

Two fixes, neither of which depends on how the model happens to judge a
marginal question:

- **The archive refuses to assert what it can disprove.** `_no_story_line`
  will not name a subject while ANY of that person's units are unplayed. An
  empty selection means "nothing answers THAT question", never "there is
  nothing more about this person", and which of those is true is a fact we
  hold rather than a judgement to delegate. Deliberately NOT fixed by playing
  the leftover units: "never invent, force, or approximate a selection to
  avoid an empty answer" applies just as much when the motive is a nicer
  sentence.
- **A follow-up offer now survives an empty answer.** The no-story branch
  dropped `follow_up` on the floor — so a turn that found no direct answer
  while KNOWING about related material still said only "I don't have a story
  about that". Pre-existing, and exactly the shape of the complaint.

Consequence worth noting: the "I have nothing about X at all" template bank is
now unreachable and was deleted rather than left as decoration. Every entity in
the map has units, so a subject can only be named once everything about them
has been played — which is what the surviving bank says.

⚠️ **Two replays were needed, and the first one lied.** Injecting an
approximate history window (an extra user turn, omitting the question being
asked) made the turn return units in BOTH arms — no reproduction, and it would
have supported "the change is innocent" for the wrong reason. `_recent_turns`
runs AFTER the user's question is persisted and takes the last 2 MESSAGE ROWS,
so the real window is *the previous assistant reply plus the question itself*.
A replay that does not reconstruct that exactly is not a replay.

⚠️ **The first version of that eval could not reproduce the bug.** Injecting
history by patching `_recent_turns` leaves the shown-unit record empty, so the
archive still had unplayed אמנון material and correctly played it — the case
reported `<played a clip>` and looked like a failure of the feature. "Already
shown" lives in the assistant Message's metadata, keyed by session, so the
eval now seeds a real session. A follow-up case that does not seed one is
testing nothing.

## REVERTED 2026-08-10: 323f88d's backward-passage bullet

The "TAKE THE PASSAGE IT SITS IN" bullet is gone from the archive-read
prompt. It passed the regression panel clean and still caused the worst
defect this project has seen live: with the uncle just discussed and all of
his units already shown, "יש עוד סיפור על אמנון?" answered with the OTHER
אמנון — the army friend's two passages, presented as one person's story.
Faithful replay (exact `_recent_turns` window, real unit texts, the session's
real shown-unit keys): friend's units 5/5 WITH the bullet, the correct
empty-selection + "זה כל מה שיש לי על אמנון נחום" 3/3 WITHOUT it.

Mechanism: everything about the uncle is [ALREADY SHOWN], the question asks
for MORE, the ALREADY SHOWN rule says to look for the same subject in a
DIFFERENT recording — and the bullet then tells the model a unit naming
"אמנון" is "the middle of a story about them; take the passage". The friend's
naming units match the bare name literally, and both passages around them are
exactly what the live turn returned. The disambiguation tags were in view and
lost.

Costs of the revert, measured and accepted:

- `about-a-person` returns to u14+u19 (2 units) on the FIXTURE phrasing — the
  mid-sentence-mention problem the bullet was written for. On the LIVE
  phrasing the bullet was actually WORSE than nothing (u13,u14,u19 vs
  u11-u14,u18,u19, both 3/3), so what is lost is one phrasing's improvement,
  not the fix as a whole.
- `school` regains u41 ("I grew up in Tiberias"), `family` returns to its
  pre-bullet 15-unit shape. Exact inverses of 323f88d's own measurements.

If passage completeness is worth pursuing again, do it as a DETERMINISTIC
post-step (expand a selection to passage boundaries in code) rather than
prompt wording — the same "structurally impossible beats prompt-guaranteed"
argument that produced unit selection in the first place.

### Passage completeness rebuilt as CODE — 2026-08-10, ⚠️ prompt gates NOT yet run

`_expand_about_passages` replaces what the reverted bullet tried to do with
wording. The gap it closes: "breadth falls out of the question" works through
the interview-question anchor, and a person who appears INCIDENTALLY in
recordings has no recording whose interview question is about them — so a
generic "ספר לי על אמנון" anchors only on name-bearing units and returns
fragments, while a specific question ("היה איתך בצבא?") matches passage
content and gets the full story. Backwards from what a listener expects.

How it works: the prompt's `about` field may now accompany a NON-empty
selection ("With that empty selection ONLY" → "With or without units
selected" — a five-word diff, position unchanged). When it resolves against
the real entity map, the selection is completed deterministically to whole
passages — contiguous units up to the nearest pause longer than
`_PASSAGE_GAP_SECONDS` (2.0s; measured on this archive: within-passage gaps
≤1.6s, the family-enumeration→uncle-story boundary 3.3s) — but ONLY inside
recordings the model already selected from AND that the archive attributes
to the named entity. Expansion can amplify an answer, never redirect one;
a bare "אמנון" naming the friend while the selection sits in the uncle's
recording intersects to nothing. 9 unit tests pin all of this, including the
person-boundary guarantee and follow-up revalidation against the expanded
answer.

**⚠️ Verification state: 847 tests pass; `prompt_regression.py`,
`eval_name_disambiguation.py` and `eval_no_story_subject.py` have NOT been
run on the `about` wording change (Gemini credits). The producer is
live-testing manually first. Before the next prompt edit, run the panel
against the baseline saved at 13bb9c6 — `uncle-then-more` and `about-a-person`
are the two cases most likely to move, and an `about` now arriving alongside
units on unrelated questions is harmless by construction (it only ever
completes passages of the person it names).**

**The panel now has a state-bearing case.** `uncle-then-more` in
`prompt_regression.py` reconstructs the live session's shown-unit state and
history window (verbatim, with runtime guards that refuse to run against a
changed archive rather than silently testing different words). Verified: with
the bullet re-added it reports the friend's units 3/3 against a baseline of
[] — a hard DRIFT. Every other case runs against an empty session, which is
exactly why four gates stayed green while this bug shipped. Plausible
simplifications of the state (whole segment as one turn, "everything but the
friend shown") do NOT reproduce it — 0/2 each where the verbatim state was
5/5 — so do not "clean up" that fixture.

## ⚠️ Leakage between prompt instructions is a GROWING structural risk

Not a quirk of the disambiguation work — a property of the design, worth
planning around before it bites again.

Every instruction is in context for every question; there is no routing. The
instruction text is English and the transcript is Hebrew, so concrete domain
nouns in the instructions act as soft retrieval cues against it. That is why
EXAMPLES leak harder than rules: examples are where the nouns live. The current
prompt already carries ~15 occurrences of nouns that name interview topics
(`army` x2, `family` x2, `wife` x2, `husband` x2, `commander` x2, `mother`,
`uncle`, `childhood`, `military`, `career`). Position matters independently —
the same clarify rule primed empty answers purely by sitting last.

Exposure is roughly **(instruction blocks) x (marginal questions)**, and the
second factor is identifiable in advance: leakage lands on questions whose
answer was already a close call, which is approximately the set already known
to vary run to run. `school` asks what he STUDIED while the units say WHERE he
studied; `army-narrow` is named in CLAUDE.md as one flash varies on. That is
what makes `prompt_regression.py`'s `MARGINAL` panel worth curating.

**What genuinely isolates today:** conditional inclusion. The disambiguation
block exists only when the archive actually has confusable names, so archives
without them are provably unaffected. Gate each new feature on a data
precondition the same way. Its limit is the case that matters — when the
precondition IS met, it is one flat prompt again.

### DECISION POINT: reconsider routing / separate calls at 4-5 instruction blocks

Recorded now so it is a planned decision rather than a surprise.

The disambiguation block alone added ~2,000 chars to ~8,300 of instructions —
a 24% increase in the non-transcript surface, from ONE feature. At roughly
**four to five independent conditional blocks**, a cheap router (decide which
blocks apply, then send only those) or per-concern calls stop looking like
overkill.

The costs are real and already documented here, which is why the threshold is
not lower:

- **Latency.** CLAUDE.md records that ~1.4s of a ~9s turn was judged not worth
  a model swap. An extra call spends that again.
- **Prompt caching.** The system/user split is deliberately ordered so the
  large static prefix caches across questions. Routing fragments it.
- **A new silent-failure mode.** A misroute turns a feature off with nothing
  in the UI to show for it — the exact shape this project keeps finding.

Below that threshold, conditional inclusion plus the regression check is the
better trade. Revisit when the third or fourth block is proposed, not before.

## Family tree: couples, sides, and a latent sign bug — 2026-08-10

Four reported tree defects, three roots. All landed together.

**🚨 The real find: `set_relation_by_hand`'s replacement math had its
generation sign FLIPPED since the function existed.** The tree walks an edge
as gen(from) = gen(to) + delta; the replacement target and the implied
generations were both computed with minus. Latent because a same-delta
replacement cancels the flip, and the tested mixed-delta cases disagreed
under both conventions — "replace" was right for the wrong reason. It
surfaces on the first AGREEING mixed-delta neighbourhood: placing somebody
as a parent's child computed their target a generation ABOVE the parent and
deleted their agreeing sibling, spouse and children edges as contradictions.
Fixed with a regression test
(`test_an_agreeing_edge_with_a_different_delta_is_kept`); the two new
features below would each have tripped it on their first real use.

**Manual uncle/aunt placement now asks the side of the family** — the manual
twin of the questionnaire's side question, same wording, same resulting edge
(`sibling` of the chosen parent; `parent` of them for a grandparent).
`SetRelationRequest.side_parent_id`, validated against the other end's
recorded parents. Optional, exactly like the questionnaire's version.

**The layout now understands couples.** A couple = a spouse edge OR two
people with a recorded child in common — recorded facts only, never
inference. Three consequences: couples are pulled adjacent after the
barycentric sort (a spouse with no neighbours above used to sort to the end
of the row alphabetically); two branch heads who form a couple share ONE band
(אילן & רחל were two bands with אמנון נחום's free to land between them, and
their shared children were silently claimed by whichever head's walk ran
last); and a recorded marriage is drawn as the genealogy double line between
adjacent partners — solid where the sibling dash is dashed. A head married
into the spine is not banded at all; their place is beside their spouse.

**The five-way sibling fork was a DATA gap, not a renderer bug.** The
renderer groups children by exact recorded parent set, order-independently —
but רז/עדי/חן were children of אילנה alone (the tree editor writes one
relation per save, so the second parent was never stated) and ניר had no
parent edges at all. `scripts/complete_sibling_parentage.py` (dry-run by
default, the run's record) added the five missing child edges; all five
children of צבי+אילנה now hang off one bus. Worth remembering: a
single-relation editor plus a two-parent family produces half-linked
children as its NORMAL output — the questionnaire's parentage question
exists precisely to close this, and the manual flow still has no equivalent
for siblings.

## Tree round 2: layout as a testable module — 2026-08-10

Follow-up report on 93bad05, three items, and a method change worth keeping:
**the layout now lives in `frontend/lib/treeLayout.ts` as pure functions**,
and `frontend/scripts/tree_layout_report.mts` (npx tsx) runs it against a
tree JSON and prints every node position and connector segment. "There is an
extra line between X and Y" is now answered by a list, not a screenshot.

- **A generation-0 person with a family of their own heads a side band.**
  ניר and אירה (plus their three children) were inlined in the producer's
  row, which buried it; they now render as "ניר & אירה's family" like the
  other couple branches. The sibling arc and the recorded parent-descent
  drop still tie ניר back to the main family — placement moved, no claim
  changed.
- **The reported "4 lines between ניר and אירה" had NO duplicate edge behind
  it.** Harness-verified: the marriage double-line (2) was sandwiched between
  the five-sibling bus above and their children's bus below — four stacked
  horizontals in one narrow gap. The band move dissolves it (the strip now
  contains exactly the marriage line). Same-row connectors and arcs are also
  deduplicated by unordered PAIR now: the questionnaire records symmetric
  relations once per direction (ניצן↔יובל exists both ways in live data),
  and per-edge drawing gave one fact two connectors — two arcs in different
  lanes, reading as two relations.
- **A person's card now lists their recorded relations, each removable**
  (two clicks, second confirms). DELETE /entities/{id}/relations/{rel_id},
  scoped to an owned entity the edge touches; tree edges now carry the row
  id. This is the way out for an edge that is WRONG with nothing true to put
  in its place — set_relation can only replace a contradiction with a
  different claim. ⚠️ Removing a recording-origin edge does not stop
  re-analysis of that recording from proposing it again; accepted, noted in
  the endpoint docstring.
  - The report's motivating case ("אילן mistakenly linked to ג'ולי")
    turned out to have NO edge in the data — the "line" was אילן's sibling
    arc and ג'ולי's descent both converging on אילנה. The relations list
    makes exactly this checkable from the page.
- **Hovering (or selecting) a person highlights their own connectors** —
  their drop, the shared trunk/bar/bus, parent stems, marriage and sibling
  links — while their siblings' drops stay dim, which is the distinction a
  shared fork erases. Hovering a parent lights the whole family's fork.
- **Band order encodes closeness, not discovery** (follow-up, same day): a
  direct sibling's own family (generation-0 head) seats to the LEFT of the
  producer's line, beside the sibling fork; aunt/uncle branches seat to the
  right. First-appearance ordering had put ניר & אירה past every extended
  band purely because row 0 is scanned after row -1. Dividers moved to each
  band's MAIN-facing side so a left-seated band still reads as separate.
- **A sibling arc redundant with a drawn fork is suppressed** (round 3): the
  ניר~Tal arc lane-routed into the gap above row 0 — which is where the
  parent fork lives — and sliced across every other sibling's drop as a long
  stray dash. Two recorded children of the same parents already share a
  trunk and a bus; that fork IS the sibling statement, so the arc says
  nothing the chart does not. Arcs survive only where no fork ties the ends
  (an uncle beside a parent with no recorded parents of their own).
- **אילן & רחל's marriage is now recorded** (manual spouse edge, written
  2026-08-10 on the producer's stated fact from the couple-adjacency
  report). Their "double line" had been the joining bar + children's bus
  33px apart — a different claim than the 5px marriage glyph — and the two
  couples now carry identical glyphs. The producer separately removed
  אילן's questionnaire-era sibling edge to אילנה with the new per-edge
  Remove — first live use of the feature, and the right call: he is married
  in, not blood.
- **Hover asymmetry between the two couples' children is a DATA gap, not a
  renderer bug**: אליאן/מעיין/יאיר are recorded children of ניר alone, so
  their fork has one stem and hovering them cannot light a line to אירה
  that does not exist — the same never-guess-parenthood-from-marriage rule
  as the descent itself. ניצן/יובל hold edges to both parents and light
  both stems. If אירה is the children's mother, that is three saves in the
  editor, not a code change.

## Photos: the media_assets foundation — SHIPPED 2026-08-11

Phase 2 of [MEDIA_GALLERY.md](MEDIA_GALLERY.md) (§2.5 records the build
decisions): migration `0026` (`media_assets`, one-owner CHECK, partial
unique primary-per-entity index — all verified present on live Neon after
`alembic upgrade`, 0 rows, existing tables untouched) and
`app/api/v1/media.py` (presign → PUT → row-write, list, delete; same flow
shape as segment video, no second storage path). 25 tests; suite at 897.

§9.4 (`/talk` photo surfacing) is APPROVED and part of the plan, sequenced
as Phase 8 after 1–6; §3.1/§4.1 were corrected for the tag-bubble timeline
(§9.5) — Phase 3 onward builds against those corrections. Phase 6's
merge-safety rule stands: any manual entity merge must repoint
`media_assets.entity_id` before deleting the losing row, or the cascade
destroys the photos.

**Phase 3 SHIPPED the same day** (MEDIA_GALLERY.md §3.4, placement
decided by the producer first — §9.6): `photo_url` on tree nodes and
extraction-panel entities via the `media_store` seam (one bulk query);
ONE shared portrait control (photo-or-initials circle, click to upload,
new photo becomes the face via `make_primary`) used by the extraction
panel and the tree's person card; the photo swaps into the tree node's
existing small SVG circle, same size and position; and a persistent
per-CATEGORY photo zone below the recording area uploading
category-owned photos. Verified live by the producer 2026-08-11.

**Phases 4 and 5 SHIPPED 2026-08-11.** Phase 4 was CLOSED as satisfied
by Phase 3, decision by the producer: §3.1's "entity list
(`/api/v1/entities`)" surface was never built — no such endpoint exists
and nothing renders an entity list (verified against the router and the
frontend) — and the timeline face had already moved into Phase 5's
gallery per §9.5. Phase 5 (MEDIA_GALLERY.md §4.4): the period photo
gallery in the timeline's side panel — hover a period card to surface
its CATEGORY's gallery, click a bubble to pin it, one gallery per
category never per bubble/entity — plus `PhotoLightbox` (shared, built
for Phase 8 reuse) and the §5 "Add photos" period entry point. Frontend
only; the gallery reads the existing `GET /media?category=`. Suite at
900; tsc/eslint/build clean. Phase 5 not yet exercised live in a
browser. Remaining: Phase 6 (merge-tool repoint rule — no merge script
exists yet to carry it), Phase 7 (year attribution, blocked on
TIMELINE_YEAR_ATTRIBUTION.md decisions), Phase 8 (/talk surfacing,
approved, after 1–6).

**Phase 8 SHIPPED 2026-08-11** (MEDIA_GALLERY.md §9.4 build note): a
/talk answer's WS message now carries `photo_categories` — the life
periods its footage came from, a LOOKUP (`raw_segment_id → question_id →
category`) made from the same clips that make the video, in both clip
modes; the /talk layout unions those categories' galleries under the
player via the existing `GET /media?category=` and opens the shared
PhotoLightbox. Access: `GET /media` (list ONLY) now accepts family
accounts scoped by the same `producer_id` linkage sessions.py checks —
confirmed against the existing model, no new one. Suite at 905;
tsc/eslint/build clean.

**RESOLVED, second round of the same report: the test was running on the
producer's own chat screen, where the gallery was deliberately not
mounted.** The failing sessions all belonged to the producer account
(`Tal3`), which cannot even reach the family /talk layout (the redeem
gate) — so the surface Phase 8 built was never on screen, while
`photoCategories` arrived unrendered in the shared hook's view-model. A
byte-exact comparison also closed the string-mismatch theory for good
(stored and resolved both `'childhood'`, hex-identical). Fixed by
producer decision: both layouts now mount the one `TurnPhotoGallery`
(a `variant` prop keeps the calm theme /talk-only). Lesson worth keeping:
"two layouts, one behaviour" means a feature scoped to one layout is
INVISIBLE from the other while the shared hook carries its data — say
which screen a surface lands on, and test on that screen.

**Live-debugged 2026-08-11 after a "gallery never appears" report — no
code defect at any layer.** Verified against the RUNNING servers, not the
repo: category resolution matches the stored photos exactly (`childhood`
both sides); a real family-account WS session showed the raw
`video_clip_response` carrying `photo_categories`; `GET
/media?category=childhood` with a family token returned all 4 photos
(200); the dev server had compiled `TurnPhotoGallery`. The failing turns
decomposed into (a) turns sent BEFORE the 19:54 backend restart, against
a pre-Phase-8 process that never sent the field, and (b) a /talk browser
tab whose loaded bundle predated Phase 8 — a "fresh session" in the SPA
mints a new conversation WITHOUT reloading the page's JS, so the old hook
dropped the new field silently. Hard-refresh the tab after deploying
frontend changes before concluding anything.

⚠️ A shape that will read as this bug again, and is not one: a clip's
gallery follows the RECORDING the footage came from, not what the answer
sounds like. Live probe: "מה אהבת לעשות כשהיית ילד?" plays footage whose
words are about childhood but which lives in the adolescence recording
(the producer opened their teenage-hobbies answer with "כשהייתי ילד…"),
so the turn resolves to `adolescence_highschool` — which has no photos —
and no gallery renders while childhood's photos sit unshown. That is
§9.4's sourcing rule working as approved (the clip's category, a lookup,
never an inference from content). If it grates, the alternative —
inferring categories from answer TEXT — is a classification with the
misattribution risks this project keeps declining; decide deliberately
if ever.

⚠️ Found while landing it: `test_full_archive_retrieval.py`'s tests were
quietly running `_recent_turns` (and anything else off
`retrieval_service.AsyncSessionLocal`) against the REAL configured
database — surfacing as an order-dependent "attached to a different
loop" failure once another file had used the real engine's pool on its
own event loop. The file's session-factory fixture is now autouse and
pins ar + video_clip_assembler + retrieval_service to the test engine.
The same hazard pattern (a service opening its module-level
AsyncSessionLocal under tests that mock everything else) is worth
checking when adding DB calls inside service success paths.

## V1 (`video_clips`) REMOVED — 2026-08-12, on branch `remove-v1`

Executed per [V1_REMOVAL_PLAN.md](V1_REMOVAL_PLAN.md), all eight steps,
suite green after each; tag `pre-v1-removal` holds the last tree with it.
What went: the chunk-retrieval orchestration in `video_clip_assembler`
(the module is now purely the shared assembly layer), the chunk path in
`retrieval_service` (avatar's segment-level `retrieve()` machinery is
untouched), the `score_chunk_candidates` orphan, the mode string
everywhere (validation now rejects it; zero rows held it), the
`uncovered_clauses` contract field (consumed by nothing), and
`compare_retrieval_modes.py` (prompt_regression.py is the stability
instrument). The v1 e2e was rewritten against v2 rather than dropped —
the proof it carries (a WS question becomes a genuinely trimmed clip) is
mode-independent. Zero database changes. Suite 905 → 841, every deleted
test belonging to deleted code.

## V2 primary, avatar dormant — 2026-08-13, on branch `avatar-dormant` (stacked on `remove-v1`)

Executed per [V2_PRIMARY_AVATAR_DORMANT_PLAN.md](V2_PRIMARY_AVATAR_DORMANT_PLAN.md)
(including its §0 re-verification addendum), seven commits, suite green
after each. What changed: sessions are producer-keyed
(`sessions.producer_id`, migration `0027`, backfilled from the avatar
join); `avatar_id` is nullable avatar-mode cargo with ON DELETE SET NULL —
**deleting an avatar no longer deletes conversations**, which the old
CASCADE did; the WS resolves the producer from the session row, not
through an avatar; `create_session` derives the archive from the caller
and takes no avatar in v2; `talk-availability` requires an avatar only in
avatar mode; `chat_mode` defaults to `video_clips_v2` (existing `avatar`
rows flipped only where no ready avatar exists — i.e. where the mode was
already non-functional); and switching to avatar mode in Settings is the
single activation gate (400 without a ready avatar; the Settings card
routes to Avatar Studio, which is no longer bounced away from in v2).
Nothing avatar-mode-internal changed: `ChatInterface`/`TalkInterface`,
voices, MuseTalk, animator, gpu_client are untouched and reachable once
the mode is on.

✅ **Migration `0027` is APPLIED to live Neon (2026-08-13)** — manually,
via `alembic upgrade head`, after live testing surfaced an empty History
panel: the branch's code selects `sessions.producer_id` on every session
query, and the local-uvicorn-against-Neon workflow runs no migrations
(only Fly's `entrypoint.sh` does — the original deploy note here covered
only that path, which was the gap). Verified afterwards: 422/422 sessions
carry `producer_id` with **zero** mismatches against the avatar join and
zero rows deleted; `avatar_id` nullable with the FK now `ON DELETE SET
NULL`; `chat_mode` server default is v2; the backfill flipped exactly the
accounts with no ready avatar (4 family + 3 empty producers), kept
`avatar` on the one producer who owns a ready avatar, and did not touch
the real producer (already v2). A Fly deploy will find `0027` already
applied and no-op.

⚠️ **Not yet exercised live:** the defining smoke test — a freshly
registered producer records, invites family, and family gets a clip
answer with zero rows in `avatars` — needs a running stack and should be
run before/at merge review. The suite covers every layer of it
individually.

## Family unified shell — 2026-08-13, on branch `family-unified-shell` (stacked on `avatar-dormant`)

Executed per [FAMILY_UNIFIED_SHELL_PLAN.md](FAMILY_UNIFIED_SHELL_PLAN.md),
steps 1–6, suite green after each. Family accounts use the regular app
shell: exactly three views (Chat full, Timeline and Family tree
view-only), backend reads opened via `require_archive_owner` (media's
access model applied to entities), every write still `require_producer`
and test-pinned. `/talk` is a redirect stub honoring old invite links;
new links generate as `/?invite=`; the shell owns register-first auth,
auto-redeem, and the unlinked-family redeem surface. Settings' Family
access panel shows the one invite lifecycle in two sections — Pending
(copy/revoke) and Active users (`GET /family/members`, sourced from the
account linkage so it cannot drift from real access).

**Step 7 DECIDED and built (2026-08-13): remove-access = full account
deletion**, the producer's explicit choice over the recommended unlink
after the tradeoff was flagged. `DELETE /family/members/{id}` tears down
open WebSockets first (WS auth checks session ownership only at
handshake), nulls the redeemed invite's reference (the invite row itself
survives as history), then deletes the account — sessions, messages and
conversations cascade. The Active-users Remove button confirms with
"Delete account + history?" because it is permanent.

⚠️ **Flagged for live review:** the calm-themed family chat now renders
inside the dark shell — the exact configuration globals.css's own history
warns reads as a bug (it did for /record). If it grates, restyle the
family chat onto the shell system; never resurrect a standalone route.
Not yet exercised live: the whole family flow end to end (invite →
register → redeem → three views).

## Known gaps / tech debt

- 🚨 **KNOWN GAP, deliberately unfixed (2026-08-10): a "עוד" question can
  switch to the other same-named person once both are exhausted.** Live
  (session `70305082`, turn 8): friend fully played, then the uncle resolved
  via clarify and fully played, then "יש עוד סיפור על אמנון?" — and the
  answer replayed the FRIEND in full. Traced without model calls; the
  mechanism, precisely:
  - The history window was CORRECT (last 2 rows: the uncle answer + the
    question). The friend entered through the transcript, not history.
  - The model's selection is the defect. Three instructions converge on the
    wrong answer in this state: ALREADY SHOWN's "look for OTHER units on
    that same subject — very often in a DIFFERENT recording" (for same-named
    people "same subject" degrades to same NAME); "if the units that best
    answer it are already shown, select them anyway"; and the disambiguation
    block's closing "NEVER return an empty selection because of any of
    this". The correct output is `unit_ids: []` + `about: "אמנון נחום"` →
    "זה כל מה שיש לי על אמנון נחום".
  - Passage expansion then AMPLIFIED the wrong fragments into the complete
    12-unit replay: bare "אמנון" exact-matches the friend's entity row (the
    uncle, "אמנון נחום", is unreachable from the bare name), so `about`
    targeted the friend. Expansion held its can-amplify-never-redirect
    contract; this state shows amplifying a wrong selection is itself a
    cost. The bare-name hazard now has TWO consumers (no-story line +
    expansion), so its blast radius grew with `bd6597d`.
  - This is a NEIGHBOURING state to the guarded `uncle-then-more` panel case
    (there the friend is unshown, and post-revert behaviour is correct 3/3).
    Now expressible as `uncle-then-more-exhausted` in the panel — ⚠️ with NO
    baseline entry yet (credits): the first `--save` pins the BUG as the
    drift reference. Read that variant as "the gap, pinned", and when a fix
    lands, this case flipping to `[]` is the intended drift.
  - Decided not to fix now: any fix lives either in prompt wording (ALREADY
    SHOWN / disambiguation block — both carry measured leakage history and
    cannot be verified until credits return) or as an expansion guard (only
    shrinks the replay; cannot fix the person error). Revisit with the
    regression panel available.

- 🚨 **`seed_sweep.py`'s references are DEAD and every accuracy number built
  on them is unscoreable.** They name segment uuids (`502fb283…`, `1d32a9b5…`,
  `097b606b…`) that no longer exist — the archive was re-recorded on 2026-08-07.
  Unit ids are positional across the whole archive, so re-recording renumbers
  everything after the changed segment. `rebaseline_accuracy.py` and
  `seed_sweep.py` cannot produce a comparable figure until the references are
  re-derived against the current archive. This is the same failure the header
  of `seed_sweep.py` warns about from the Deepgram re-ingest, happening again.
- 🚨 **`question_index` restarts per interview CATEGORY, but take-grouping keys
  on it alone** (`_group_siblings`). The live archive therefore presents three
  unrelated questions as one answer given in three sittings — "tell me about
  your father" (childhood), "your roles in the army" (military) and
  "post-secondary studies" (academic) all sit at `idx=1` and are printed as
  "take 1/2/3 of this question", with the prompt instructing the model to read
  them together and apply the FIRST one's interview question to all of them.
  `question_id` carries the real identity. Affects every question, not only
  ambiguous ones. Not fixed: it would move the baseline the 2026-08-08
  measurements were taken against, so it wants its own change and its own
  before/after.
- **No regression test guards "the name-correction field never touches
  `TranscriptChunk`"** — dropped over a `MissingGreenlet` fixture problem and
  stated plainly in `bbda871`. The behaviour is correct; nothing pins it.
- **The eval's scored set contains no broad question**, which is exactly where
  v2's run-to-run variance lives. `stdev=0.000` therefore says the reference
  cases are solid, not that nothing varies. Closing this needs an agreed
  reference range for something like "tell me about your army period".
- **v2 does not scale past a full-context archive.** `_load_archive` is
  deliberately uncapped (~2.1K tokens today). A coarse pre-filter is marked as
  a TODO at the ~150K-token threshold and should not be built speculatively.
- **Redis is not running locally**, so `cache_service` (clip cache, segment
  visited-set) silently no-ops in dev. v2's shown-unit memory deliberately
  avoids it, but the clip cache is effectively untested locally.
- **v1's coreference call and ingestion entity extraction still use
  flash-lite**, which was measured weak at exactly the coreference task. Only
  the archive-read call was upgraded. Note that upgrading entity extraction
  would *not* have helped the unnamed-spouse case (verified: flash-lite, flash
  and pro all return `[]` — there is no name to extract).
- **`/health` is now honest about Redis, but nothing watches it.** A failed
  connect used to report `"not configured"` with status `healthy` —
  indistinguishable from deliberately running without a cache — so the whole
  clip cache could silently no-op in production with only one `logger.error`
  at boot to show for it. It now reports `"unreachable: <cause>"` and
  `degraded`. **The signal exists; no alerting consumes it.** `/health` still
  returns HTTP 200 when degraded (deliberately — `backend/fly.toml` checks it
  every 30s and `docker-compose` uses `curl -fsS`, so a degraded body must not
  take machines out of rotation), which means Fly's own check cannot act on
  it. Catching a dead cache automatically needs a monitor that parses the
  response body. Not built; decide when someone is actually on call.
- **STT is ~2.1s per spoken question** (Deepgram, measured 2026-08-01 over 4s
  of real audio). The ~9s figure recorded here previously was **local Whisper
  `medium` on CPU**, which is now only the fallback — `LIVE_STT_PROVIDER` and
  `INGESTION_STT_PROVIDER` are both `deepgram`. Sending audio-only rather than
  the full video webm made no difference (2.17s vs 2.11s), so the cost is
  Deepgram's processing, not upload size.
- **Suggestions can be topically loose** — Montreal offered "what I discovered
  about myself after the army", linked only via "the period after the army".
  Defensible but worth watching before tightening.
- **`ilana`/`tzvi` sit at 0.976, not 1.0** — the unit boundary is a hair wider
  than the hand-picked reference range. Not worth chasing.

---

## Open decisions

1. **Producer video-clip layout.** Currently the producer screen keeps its own
   side-chat + single-video-panel layout (as requested). If the two screens
   drift further, decide whether to keep two layouts or converge.
2. **Whether to tighten follow-up suggestion relevance** (see above).
3. **Whether to add a broad question to the scored eval set**, accepting that
   its reference will be fuzzier than the existing ones.
4. **When to introduce routing or per-concern LLM calls** for the /talk
   prompt — see the decision point above. Trigger is 4-5 independent
   conditional instruction blocks; there is 1 today.
5. **Whether to re-derive the accuracy references** against the current
   archive, or retire IoU scoring in favour of the drift-based check. The
   references have now gone stale twice for the same structural reason.
6. **Whether `avatar` mode is still a supported product path** or effectively
   superseded by the video-clip modes — it still carries MuseTalk, TTS, and
   voice-cloning surface area that nothing else needs.

---

## How to verify

```bash
cd backend
python scripts/prompt_regression.py --save # BEFORE any /talk prompt edit
python scripts/prompt_regression.py        # AFTER — diffs unrelated answers
python scripts/eval_name_disambiguation.py # same-name clarification, both arms
python scripts/eval_no_story_subject.py   # tailored no-story line, v2 only
python scripts/rebaseline_accuracy.py      # ⚠️ references are STALE — see known gaps
python scripts/seed_sweep.py               # single-run IoU vs known-correct
python -m pytest -q                        # 905 tests
```
