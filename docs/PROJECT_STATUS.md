# Project status

**Updated:** 2026-07-28 · **Branch:** `main` (all work commits directly to
main and pushes; no feature branches unless asked)

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
- `avatar` — LLM → TTS → MuseTalk.
- `video_clips` (v1) — chunk retrieval, multi-step.
- `video_clips_v2` — whole-archive read, single LLM call, utterance-unit
  selection. **This is the mode under active development.**

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
- In `video_clips` (v1) and `avatar` the merge IS load-bearing —
  `retrieval_service.find_segments_mentioning[_scored]` finds other recordings
  sharing an entity, and `relevance_scorer`/`response_assembler` use entity
  names as a ranking signal. Only v2 is insensitive to it.

## Known gaps / tech debt

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
4. **Whether `avatar` mode is still a supported product path** or effectively
   superseded by the video-clip modes — it still carries MuseTalk, TTS, and
   voice-cloning surface area that nothing else needs.

---

## How to verify

```bash
cd backend
python scripts/rebaseline_accuracy.py      # v2 accuracy as a MEAN over runs — quote this
python scripts/compare_retrieval_modes.py  # v1 vs v2: consistency, latency, calls, tokens
python scripts/seed_sweep.py               # single-run IoU vs known-correct
python -m pytest -q -m 'not integration'   # 541 tests
```
