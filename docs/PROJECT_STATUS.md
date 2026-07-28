# Project status

**Updated:** 2026-07-28 · **Branch:** `main` (all work commits directly to
main and pushes; no feature branches unless asked)

Working-state snapshot. Standing rules and architecture invariants live in
[CLAUDE.md](../CLAUDE.md); this file is "where we are right now" and should be
updated as work lands.

---

# NEXT UP: move entities from Graphiti/Neo4j into Postgres

**This is the agreed next piece of work.** Plan settled 2026-07-28; the build
has NOT started. Read this section before touching anything entity-related.

## Where it stands right now

- Migration `0012_entities_in_postgres.py` is **written, committed and pushed**
  — and **NOT APPLIED**.
- ⚠️ **`entrypoint.sh` runs `alembic upgrade head` on startup, so the next
  deploy applies it automatically.** Judged safe: it is purely additive (new
  tables only, no existing table altered) and nothing reads or writes them yet.
- Validated by executing `upgrade()` **and** `downgrade()` against the real
  database inside a rolled-back transaction: 5 self-entities created, 20
  relation types seeded, 6 tree-bearing, CHECK constraints firing, database
  unchanged. That run caught a real bug — `users.full_name` is NULL for
  several producers, so the self-entity name falls back `full_name → username`.

## Why we're doing it

- **Latency.** `_build_entity_map` was 45% of a turn and 100% of the latency
  variance (1.35s–9.55s across identical passes), while accuracy was 0.991
  with *and* without the entity map.
- **The capability that would justify a graph is unused.** Zero `RELATES_TO`
  edges exist — verified on live data. Graphiti can extract relationships; on
  this data it never has.
- **The transcript is stored twice** and the copies can drift. `חיל האוויר`
  survived in the graph on a transcript that no longer existed in Postgres.

## Call-site audit — SEVEN, not three

The original assumption was three uses. There are seven, spanning **all three
chat modes**, so this is not a v2-only change:

| Call site | Function | Mode |
| --- | --- | --- |
| `full_archive_retrieval._build_entity_map` | `get_episode_entity_names` | v2 |
| `segment_deletion` + `analysis_graph.transcribe_node` | `remove_episodes_for_segment` | all |
| `analysis_graph.check_entities_node` | `get_entity_candidates` | ingestion |
| `analysis_graph.finalize_ingest_node` | `add_episode` (**the write path**) | ingestion |
| `segment_extraction._load_entities` | direct Cypher (name + summary) | panel |
| `retrieval_service` ×4 | `find_related_episodes`, `..._scored`, `get_episode_entity_names`, `get_entity_candidates` | **v1 `video_clips`** |
| `relevance_scorer` + `response_assembler` ×3 | `get_episode_entity_names` | **`avatar`** |

All seven reduce to two primitives, both plain joins on `entity_mentions`:
"entity names for segment X" and "segments mentioning entity Y".

Two findings that make this smaller than it looks:
- **`max_hops` is 1 at every call site**, making the Cypher `RELATES_TO*0..0`
  — matching only the origin node. No traversal happens anywhere today.
- **`find_related_episodes_scored`'s "score" is `COUNT(DISTINCT entity)`** —
  a `GROUP BY` with `ORDER BY count DESC`.

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

## Build chunks

1. **Models + normalisation + the write path.** Extraction returns
   `type`/`alternative_type`; writes `entities` + `entity_mentions` (with the
   per-mention summary). Hebrew normalisation (final letters, ט/ת) for
   `normalized_name`.
2. **Import the 11 existing entities.** Verify the `מונטריאול` merge lands as
   ONE row with TWO mentions.
3. **Move the seven read call sites off `graph_memory`.**
4. **Batched confirmation** covering identity + type.
5. **Delete `graph_memory`**, drop the Neo4j/Graphiti dependencies and config.

## Review points — DO NOT SKIP

- **After chunk 2: entities must be visible for review before going further.**
  Show the imported rows (name, type, mentions, per-mention summaries).
- **After chunk 3: re-run the eval.** `python scripts/rebaseline_accuracy.py`.
  Current baseline **v2 = 0.999, stdev 0.000 over 5 runs**.
- **Nothing irreversible before explicit confirmation.** In particular chunk 5
  (deleting `graph_memory`, dropping Neo4j) does not happen until the earlier
  chunks are reviewed and signed off. Neo4j data is the only copy of the
  entity summaries until chunk 2 has been verified.

## Type backfill for the 11 existing entities

Classified from the stored summaries — no LLM needed at this scale, and 9 of
11 are stated outright in the summary text:

| Type | Entities |
| --- | --- |
| `person` (6) | אילנה, צבי, ניר, חן, עדי, רז — summaries read אמא/אבא/אח של הדובר |
| `place` (2) | טבריה, מונטריאול |
| `organisation` (2) | חיל האוויר, **הכפר הירוק** (confirmed: boarding school) |
| skip (1) | **עכבר** — a common noun, not a named entity. Confirmed decision: extraction should have skipped it. Add to the extraction prompt: *if it fits no category, it is not a named entity — omit it.* |

## Open finding: the entity map's ids are unresolvable

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

**Action (fold into chunk 3):** emit recording ordinals — `- אילנה: RECORDING
1`. Then re-run the eval with and without the map, so the decision to keep or
drop it rests on evidence rather than a confound.

Note also that `_format_entity_map` contains **no summaries at all** — it is
purely name → ids. Per-mention summaries therefore cost the prompt nothing.

## What we give up (honest list)

1. **Automatic entity-summary consolidation** — solved better by per-mention
   summaries; no longer a loss.
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
- Graphiti entity extraction per segment, feeding the entity map —
  **being replaced; see NEXT UP above.**

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

**Tests:** 424 backend passing; frontend `tsc`, `eslint`, `next build` clean.

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
- **STT is ~9s per spoken question** on CPU. Deliberate accuracy trade;
  the archive-read call is no longer the bottleneck.
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
python -m pytest -q -m 'not integration'   # 424 tests
```
