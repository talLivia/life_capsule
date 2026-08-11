# V1 (`video_clips`) removal — investigation and plan

**Written 2026-08-12. Investigation only — nothing has been removed.**
Scope: the v1 chunk-retrieval chat mode ONLY. Avatar mode is explicitly out
of scope and every avatar dependency here is treated as "must keep".

Everything below was traced to specific call sites on the current tree
(HEAD `9c888bc`), not inferred from module names. Line numbers are from that
tree and will drift; the function names won't.

---

## 1. Verified preconditions

- **No user is on v1.** Queried live 2026-08-12: zero rows with
  `chat_mode='video_clips'`. The only producer with recordings (15) is
  `video_clips_v2`; all other accounts are `'avatar'` with zero recordings.
  **No data migration is needed.**
- **V1 has no frontend of its own.** Both clip modes share the hook and both
  layouts; the only frontend traces are the `'video_clips'` string literals.
- **V1 has zero database surface.** The columns it reads
  (`transcript_chunks.*` incl. `embedding`, `raw_segments.embedding`) are
  all written by the shared ingestion pipeline and read by avatar mode
  and/or v2. Nothing DB-side changes in this removal.

## 2. The §A trace: `retrieval_service.py`, function by function

The module's own Prompt-12 header states the chunk path "is a PARALLEL path
for the video-clip mode only — none of it is called by, or changes the
behavior of, primary_match/expand_graph/retrieve above." The trace confirms
that claim AND its converse (the avatar path calls none of the chunk
helpers). Certainty here is from reading every call site, both directions.

### 2.1 SHARED — avatar's `retrieve()` path uses these. MUST STAY.

| symbol (line) | used by avatar via | also used by |
| --- | --- | --- |
| `_resolve_coreferences` (366) | `primary_match`:409 | v1 `primary_match_chunks`:643 |
| `_recent_turns` (348) + `_render_turn_for_history` (335) + `_COREFERENCE_RESOLVE_...` (317) | `_resolve_coreferences` | **v2** `select_units` (full_archive_retrieval:1546) + every eval script patches `_recent_turns` — the name must not change |
| `COREFERENCE_HISTORY_TURNS` (108) | ditto | **v2** (imported by name at far:1547) |
| `_classify_topic` (220) + its prompt (152) | `primary_match`:412 | v1 chunks:647 |
| `_extract_entity_names_from_question` (253) + prompt (161) | `primary_match`:413 | v1 chunks:648 |
| `_embed_question_for_primary_match` (274) | `primary_match`:414 | v1 chunks:649 |
| `_resolve_entity_names` (285) | `primary_match`:433 | v1 chunks:651 |
| `_parse_json_array` (240) | `_extract_entity_names...` | imported by `video_clip_assembler` (which stays as v2's assembly layer) |
| `_short_summary` (211) | `retrieve`/`expand_graph` | imported by `relevance_scorer` (avatar) |
| `primary_match` (395), `expand_graph` (464), `retrieve` (513) | `response_assembler.assemble_response`:161 | — |
| Constants 99–141 (`MIN_SHARED_ENTITY_COUNT`, `MAX_CANDIDATES`, `_SUMMARY_MAX_CHARS`, `MAX_PRIMARY_MATCHES`, `SEMANTIC_MATCH_THRESHOLD`) | segment path | some also read by chunk path |
| `RetrievalResult` / `RetrievedSegment` dataclasses | `retrieve()` return contract | — |

**Conclusion:** coreference handling, topic classification, entity-name
extraction/resolution, and question embedding are all shared machinery that
the chunk path *reuses* — none of it is v1's own. Avatar's `retrieve()`
needs every row above to keep working. Confirmed to certainty.

### 2.2 V1-EXCLUSIVE — no other caller exists. DELETE.

| symbol (line) | note |
| --- | --- |
| `retrieve_chunks` (757) | called only by v1 `assemble_video_clip_response` + tests + `compare_retrieval_modes.py` |
| `primary_match_chunks` (615) | called only by `retrieve_chunks` |
| `expand_graph_chunks` (675) | called only by `retrieve_chunks` |
| `_match_chunks` (577) | called only by `primary_match_chunks` (twice: strict + relaxed pass) |
| `_load_ready_chunks` (563) | called only by `primary_match_chunks`; ONE stale mention in a full_archive_retrieval COMMENT (line ~308) — fix the comment |
| `_normalize_to_first_person` (543) + `_PERSPECTIVE_NORMALIZE_...` (185) | called only by `primary_match_chunks` |
| `SEMANTIC_CHUNK_MATCH_THRESHOLD` (150) | alias of the shared threshold; read only by the chunk path; referenced in a video_clip_assembler COMMENT only |

### 2.3 `video_clip_assembler.py` split (from the audit, re-verified)

DELETE (v1 orchestration): `assemble_video_clip_response`,
`_split_question_into_clauses`, `_verify_and_pinpoint_chunk`,
`_expand_chunk_boundaries`, `_topics_overlap_or_similar`, `VerifiedChunk`,
and the constants `MAX_CLIP_DURATION_SEC` / `MAX_SILENCE_GAP_SEC` /
`TOPIC_DRIFT_EMBEDDING_THRESHOLD` (verified: referenced only inside this
module and its tests). Afterwards trim now-unused imports (`llm_service`,
`retrieval_service`, `_parse_json_array`, `NO_STORY_FALLBACK`, `embeddings`
— let the linter confirm the exact set).

KEEP (v2's assembly layer): `VideoClipResult` (minus one field, §4.3),
`ExpandedClip`, `CACHE_TTL_SECONDS`, `_clip_cache_key`,
`_assemble_and_upload_clip`, `photo_categories_for_segments`.

### 2.4 A pre-existing orphan found during the trace

`relevance_scorer.score_chunk_candidates` + `ScoredChunk` have **zero
production callers** — only their own tests and a mention in
`retrieve_chunks`'s docstring. Built for Prompt 12's scoring step,
superseded by Prompt 13's per-candidate verify/pinpoint, and never wired.
Delete with v1 (it is v1-era code stranded in an avatar module; removing it
does not touch avatar's `score_candidates`, which response_assembler uses).

## 3. The three decisions, resolved

### 3.1 `compare_retrieval_modes.py` — DELETE outright, do not salvage

The harness's design is the side-by-side; with one side gone it is a
misnamed shell. The repeat-run consistency capability (`COMPARE_REPEAT`) is
not worth extracting because `prompt_regression.py` is the maintained
instrument for exactly that risk: it already runs repeated arms (the 3/3,
5/5, 6/6 measurements throughout PROJECT_STATUS), carries the curated
MARGINAL panel and the state-bearing `uncle-then-more` cases, and gates
every prompt edit. A salvaged one-off consistency script would be a second,
unmaintained harness measuring the same thing — the "two places hold one
fact and drift" shape this repo keeps deleting. The historical consistency
FINDINGS (v1 12/12, v2 10/12) stay recorded in CLAUDE.md as the evidence
behind accepted non-determinism; they do not need to be re-measurable.
**If a dedicated run-to-run stability number is ever wanted again, add a
`--repeat` flag to `prompt_regression.py` rather than reviving this.**

### 3.2 WS dispatch — always v2; the v1-default test is REPLACED

Current: `assembler = v2 if chat_mode == 'video_clips_v2' else v1`
(websocket.py:765-770) — v1 is the fallback branch. New logic: the clip
handler **always** uses `full_archive_retrieval.assemble_video_clip_response_v2`;
the conditional and the `video_clip_assembler` name in that import line go.
The audio router (websocket.py:610, `chat_mode in ("video_clips",
"video_clips_v2")`) becomes `chat_mode == "video_clips_v2"` — unknown modes
keep failing toward the avatar text path exactly as today.

Tests: `test_video_clip_inner_selects_v1_assembler_for_default_video_clips_mode`
is deleted and `..._selects_v2_assembler_for_v2_mode` is rewritten as
*"the clip handler always dispatches to the v2 assembler"* — asserting v2 is
called and that nothing imports a v1 assembler, rather than asserting a
branch that no longer exists.

### 3.3 `uncovered_clauses` and the Redis visited-set — one removed, one kept

- **`uncovered_clauses`: REMOVE.** Produced only by v1's clause-coverage QA
  (v2 always sends `[]`). Definitively nothing consumes it: the only
  frontend reference is the type declaration in `types.ts:88` — no
  component renders it. Drop the field from `VideoClipResult`, the
  `video_clip_response` WS payload, and the frontend type.
- **Visited-set: KEEP, untouched.** Avatar mode READS it —
  `retrieve()` calls `cache_service.get_visited` (retrieval_service:521)
  and `response_assembler` writes it (line 218). v1's read (776) and write
  (video_clip_assembler:728) die with v1; v2's write
  (full_archive_retrieval:1765) stays as-is. Do not "clean up" the v2
  write: it costs nothing (Redis no-ops without a server) and mode is
  per-producer, so the semantics cannot cross. Revisit only if avatar mode
  is ever removed.

## 4. Doc audit — exhaustive

Re-grepped `video_clips` (excluding `_v2`) across README, CLAUDE.md, all of
`docs/`, `.env*.example`, and `*.toml`: **seven files, no new finds** beyond
the previous list. Split:

**Must change (they describe live behavior):**
- `CLAUDE.md`: the mode table row (line 15); "Both video-clip modes share
  one WS contract" phrasing; the Evals section (drop
  `compare_retrieval_modes.py`); "v1's coreference call and ingestion
  entity extraction are the next candidates" note; the "v1 was 12/12
  stable" sentences get a "(v1 since removed)" annotation, not deletion —
  they are the recorded evidence for accepted non-determinism.
- `PROJECT_STATUS.md`: the chat-modes list (~726); "In `video_clips` (v1)
  and `avatar` the merge IS load-bearing" (~1019) becomes avatar-only; the
  How-to-verify command list (drop the compare line).
- `docs/TRANSCRIPT_EDITING.md` (~53): "what v1 and the avatar path rank
  on" → avatar-only wording. The constraint itself survives via avatar.

**Annotate as history, do not rewrite:**
- `PROJECT_STATUS.md` ~103 (the seven-call-site audit table) and the
  "Evidence: v1 vs v2" measurement table — point-in-time records.
- `docs/ENTITY_DISAMBIGUATION.md` ~214 ("v1 untouched, as asked").
- `docs/poc-claude-code-prompts.md` (~613/648) — the prompt log.

Code-comment references (`models.py` User.chat_mode docstring, `users.py`
CHAT_MODES comment, migration 0011's docstring) are handled inside the code
steps; the migration docstring is historical and stays.

## 5. Removal plan — ordered, each step ends with the suite green

**Step 0 — pre-flight.** `git tag pre-v1-removal && git push --tags`.
Re-run the chat_mode query (a `'video_clips'` row appearing since this plan
aborts the plan). Full suite + frontend build green as the baseline.

**Step 1 — delete the orphan.** `relevance_scorer.score_chunk_candidates`
+ `ScoredChunk` + their test block (test_relevance_scorer ~280-370).
Independent of everything else; proves the removal loop works.

**Step 2 — dispatch goes v2-only.** websocket.py: always-v2 in
`_handle_video_clip_question_inner`; audio router `== "video_clips_v2"`;
rewrite the two dispatch tests per §3.2. After this commit v1 is
unreachable in production while all its code still exists — the safest
possible intermediate state.

**Step 3 — retire the mode string.** `users.CHAT_MODES` →
`{"avatar", "video_clips_v2"}` (+comment); SettingsPanel drops the option;
`talk/page.tsx` condition drops `'video_clips'`; any `'video_clips'`
literals in `types.ts` unions; `models.py` chat_mode docstring. Frontend
tsc/eslint/build + suite.

**Step 4 — delete v1 orchestration in `video_clip_assembler`.** The §2.3
DELETE list; drop `uncovered_clauses` end to end (VideoClipResult → WS
payload → frontend type); trim imports. Delete the v1-pipeline half of
`test_video_clip_assembler` (clause split / verify-pinpoint / boundary
expansion / topics-overlap / orchestration tests); the shared-assembly and
photo-categories tests stay. ⚠️ **Step-time check:** open
`test_video_clip_e2e.py` first — its contents were not read in this
investigation; if it drives the v1 chain, rewrite it against v2 or delete
it deliberately, not incidentally.

**Step 5 — delete the chunk path in `retrieval_service`.** The §2.2 list
(six functions, two prompt templates, one alias constant); fix the stale
`_load_ready_chunks` comment in full_archive_retrieval; delete the
chunk-path tests in `test_retrieval_service` (the shared-path tests stay
and are the proof avatar retrieval survived).

**Step 6 — delete `scripts/compare_retrieval_modes.py`**, updating
CLAUDE.md's Evals section in the same commit so the doc never lists a
script that doesn't exist.

**Step 7 — docs pass.** Everything in §4, plus a PROJECT_STATUS entry
recording the removal and the tag name.

**Step 8 — final verification.** Repo-wide grep for every deleted symbol
name (zero hits outside docs history); full suite; frontend build;
`git grep video_clips | grep -v _v2` returns only the annotated history
lines. Optional, credits-gated: one live /talk question as a smoke test —
nothing in this removal touches v2's path, so the suite is the real gate.

## 6. Explicit keep-list (things that look v1 and are not)

- All of §2.1 — the shared retrieval machinery avatar runs on.
- `video_clip_assembler`'s assembly layer (§2.3 KEEP) — v2's ffmpeg path.
- The Redis visited-set (§3.3) — avatar reads it.
- `transcript_chunks` (v2 builds units from its word timestamps; avatar
  scores on it) and both `embedding` columns (avatar reads, ingestion
  writes). **Zero DB/migration changes in this removal.**
- `scripts/seed_sweep.py`, `rebaseline_accuracy.py`, and the three eval
  scripts — their v1-module imports are all §2.1 shared symbols
  (`_recent_turns` patching, `ExpandedClip`). They survive unmodified.
- `scripts/backfill_transcript_chunks_prompt11.py` — historical backfill
  for a table that stays; leave as history.
- `NO_STORY_FALLBACK` / `TRANSIENT_FAILURE_FALLBACK` / `no_story_about` in
  `response_assembler` — v2 imports them; their home is an avatar-mode
  module but that module is out of scope and staying.

## 7. Residual risks, stated honestly

1. `test_video_clip_e2e.py` contents unread — bounded by the Step-4 check.
2. The eval scripts monkeypatch `retrieval_service._recent_turns` by name;
   a rename would break them only when they next run (credits-gated, so
   the failure would be delayed). Mitigation: the plan renames nothing.
3. CLAUDE.md's non-determinism section leans on v1 consistency numbers as
   corroboration; the annotation (not deletion) in §4 preserves the
   evidence chain.
4. This plan assumes avatar mode stays. If avatar removal is ever approved,
   §2.1's keep-list collapses and `retrieval_service` should be revisited
   wholesale — do not extend this plan piecemeal into that.
