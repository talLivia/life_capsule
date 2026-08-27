# Bulk import of legacy videos — plan (2026-08-26)

**STATUS 2026-08-27 — COMPLETE AND SHIPPED. All 9 items done (record
below); PREFILTER=on globally per §7 with both proofs passed at flip
time; suite 916 green.**

DONE:
* Plan finalized and producer-approved through `5f99f20` (all rulings
  in §9/§10: no size caps; CSV order = take order; bulk import always
  auto-confirms via import_batch_id, UNCONDITIONALLY, independent of
  the Settings toggle — no blocking, no toggle read in the batch path).
* `User.auto_extraction` (Boolean, default false) and
  `RawSegment.import_batch_id` (nullable, indexed) added to models.py;
  **migration 0029 written and APPLIED to live Neon** (alembic head =
  0029).
* Verified mechanism for the auto path: `human_confirm_node`
  (analysis_graph.py:1219) is the ONE interrupt site; its downstream
  answer-application keys off entries in the returned dict, so
  **`answer = {}` (skip the interrupt) applies no corrections and
  keeps the extraction as produced** — identical shape to the existing
  zero-questions fast path. The branch condition:
  `producer.auto_extraction OR segment.import_batch_id is not None`.

MILESTONE 1 COMPLETE (2026-08-26): the human_confirm_node auto branch
is live (`answer = {} if auto_accept else interrupt(...)`, flowing
through the REAL application path whose silence-defaults keep the
extraction as produced; `_auto_confirm` fail-soft helper; missing
segment_id = manual). Three §10 unit tests added; full suite 903
passed.

REMAINING (in order; run the full pytest suite green after each
milestone; report per milestone):
4. DONE (2026-08-26): CSV template + GET /api/v1/bulk-import/
   template.csv (app/api/v1/bulk_import.py; producer-only, generated
   live via interview_config.get_questions in the producer's
   recording_language, BOM-prefixed; 3 tests; suite 906 green).
5. DONE (2026-08-27): BulkImportBatch model + migration 0030
   (applied live); staging PUT to bulk_staging/{producer}/{batch}/
   (re-upload overwrites = resume-by-reselect; any new file voids a
   prior validation); §3 pure validator (unknown id / missing file /
   duplicate / non-video / empty errors, unmapped-file warnings, plan
   in CSV order); mapping upload endpoint stores report+plan and
   flips staging<->validated; owner-scoped batch list/get for resume.
   8 tests; suite 911 green.
6. DONE (2026-08-27): bulk_import_runner.py — worker pool
   (semaphore, concurrency 2) AWAITING run_segment_analysis per file
   (bounds the pipeline AND yields per-file truth); the shared
   create_segment_row extracted from /segments/ingest so both callers
   are literally one code path (import_batch_id stamped, recording_no
   FOR-UPDATE serialized); question_index = GLOBAL catalog position
   (unique per question — the cross-category collision bug cannot be
   triggered by imports); continue-and-report with
   done/done_with_failures; per-file retry via the existing deletion
   service; file_states writes serialized by an in-process lock
   (measured lost-update fixed); start + retry endpoints; §5
   warm-debounce in finalize_ingest_node behind
   _another_segment_in_flight (fail-soft to warming). 3 orchestrator
   tests + debounce test; suite 914 green. (Test note: SQLite's
   single shared connection forces pool-of-1 in unit tests; real
   concurrency is the §8 integration batch's job.)
7. DONE (2026-08-27): auto_extraction wired through UserUpdate/
   UserResponse + the update endpoint (free_navigation pattern);
   Settings gains the "Extraction review" toggle card (with the
   bulk-always-auto caveat in its copy) and BulkImportPanel.tsx —
   template download, multi-file staging, mapping upload with the
   error/warning report rendered, start, 4s progress poll while
   running, per-file retry buttons, and mount-time resume via GET
   /batches (the server batch row is the truth; closed tabs lose
   nothing). tsc clean; backend suite 914 green.
8. DONE (2026-08-27): **PREFILTER=on globally** — both proofs
   re-run at flip time and PASSED: inertness byte-proof (pf=None on
   the real archive with the toggle on; 18,652 chars,
   4de2869d1b0b9ddf exact) and the synthetic gate 10/10. Punch list
   landed with it: pin-dict FIFO eviction (_PIN_CAP=500, tested);
   the per-read filtered/admitted log line stands as the metric until
   an ops surface exists; crowd-out re-checked at a REAL ratio
   (150K/203K budget: 40/54 admitted, army 6/6 for its own question —
   the failure mode remains unreachable outside starved stress
   settings). Suite 916 green.
9. DONE (2026-08-27) — FEATURE COMPLETE. Integration batch PASS on
   the REAL pipeline (throwaway producer, 2 real-speech takes + 1
   corrupt file, toggle OFF + batch id): both takes ready via real
   STT/entities/embeddings (24 chunks), ZERO pending confirmations
   (unconditional auto path proven end-to-end), corrupt file failed
   with the real error, batch done_with_failures, cascade cleanup to
   zero rows (also settles the populated-producer cascade question).
   Caught en route: upload_file arg-order inversion in runner+staging
   (unit-test mock had mirrored the wrong order) — fixed everywhere,
   mock corrected. Panel no-change control: every unit selection
   baseline-identical 5/5; only the documented day-scale strata moved
   (offer flickers, exhausted clarify 0-4/5 wobble, known marginals)
   — bulk import touches no prompt bytes, confirmed. Suite 916 green.

Producer reviews before any code beyond this list.**

## 0. Goal and non-negotiables

A producer migrating videos from an old system maps each file to a real
interview question and pushes everything through the **existing
ingestion pipeline** — presign → PUT → `/segments/ingest` → the full
analysis graph (transcription, entity extraction, embeddings,
recording_no assignment, cache refresh). CLAUDE.md already states the
invariant this plan obeys: *"Uploading a video reuses the recording
entry point exactly… There is no upload endpoint and no second
ingestion path; if you find yourself writing one, something is wrong."*
Bulk import is a MAPPER + DISPATCHER around that path, never a bypass.

## 1. The per-file path (traced, current code)

1. `POST /segments/presign` (interview.py:272) — storage key + URL.
2. Client PUTs the file bytes (local-storage dev: direct upload path).
3. `POST /segments/ingest` (interview.py:356) — creates the RawSegment
   (producer locked `FOR UPDATE`, `recording_seq += 1` →
   `recording_no`), then `asyncio.create_task(run_segment_analysis)`.
4. The graph does STT → chunks → entities → embedding → finalize
   (archive-cache invalidate + warm + gemini-cache drop).

A bulk import therefore needs NO new ingestion machinery — it needs a
**batch orchestrator** that walks the mapping and performs steps 1-3
per file with controlled concurrency (§5), plus a batch status record.

## 2. Mapping template (CSV, downloaded from Settings)

Generated from `app/interview_questions.json` (129 questions, 16
categories, schema v2 — the REAL current catalog, generated at download
time so ids can never go stale):

```csv
question_id,category,question_text,filenames
childhood_q01,ילדות,ספר לי על הבית שגדלת בו,"tape1.mp4"
childhood_q02,ילדות,מה הזיכרון הראשון שלך?,"tape2a.mp4;tape2b.mp4"
...
```

* One row per catalog question, pre-filled except `filenames`.
* `filenames` = semicolon-separated list — **multiple takes per
  question are first-class** (ingest APPENDS; that is the existing
  contract). Empty = skip.
* CSV not XLSX: openable in Excel, no new dependency server-side.
  (Excel-friendly UTF-8 BOM on download.)

## 3. Validation — all-or-nothing BEFORE any ingest starts

On upload of (mapping file + selected videos), the server validates the
WHOLE batch and returns a single report; **nothing ingests until the
producer confirms a fully-valid mapping**:

* every `question_id` exists in the current catalog (row-level error);
* every referenced filename is present among the uploaded files
  (missing-file error) and every uploaded file is referenced
  (unmapped-file warning — listed, producer chooses ignore/fix);
* duplicate filename references, empty batch, non-video extensions,
  per-file size cap — row-level errors;
* the report is returned as structured JSON rendered in the UI; fix =
  re-upload the mapping only (videos are already staged, addressed by
  filename).

Partial/confusing mid-batch failures are designed out: validation is
pure (no side effects), and ingestion starts only on explicit confirm.

## 4. Settings UI/UX

* Settings → "ייבוא הקלטות" card: (1) download template, (2) multi-file
  select + mapping upload, (3) validation report view, (4) Start
  button, (5) progress panel.
* Upload staging: files PUT via the existing presign flow to a
  `bulk_staging/{producer}/{batch_id}/` prefix as they are selected —
  so the browser upload and the ingestion are DECOUPLED.
* **Closed tab mid-upload**: staged files persist; reopening Settings
  shows the batch draft (server-side `bulk_batches` row: id, producer,
  state = staging|validated|running|done, per-file states). Uploading
  resumes by re-selecting missing files (validated by name+size).
* **Closed tab mid-ingestion**: irrelevant by design — ingestion runs
  server-side off the batch row, not the browser session; the progress
  panel is a poll of batch state, resumable from any session.

## 5. Concurrency / throttling (the finding from the caching round)

Today each ingest fires `asyncio.create_task(run_segment_analysis)` —
50 files at once = 50 concurrent graphs: STT contention, LLM 429/503s,
and 50 redundant archive-cache warms (each finalize warms; measured
2-4s each, harmless alone, wasteful ×50).

* The batch orchestrator ingests with a **small worker pool
  (concurrency 2-3)**, sequentialish by design — bulk import is
  offline work; correctness and rate-limit civility beat speed.
* **Warm-debounce** (the ~5-line change already identified in the
  caching round): `finalize_ingest_node` skips the archive-cache warm
  when another segment for the same producer is still `processing`;
  the LAST segment's finalize warms once. Landed as part of this
  feature, gated by the normal suite.
* recording_no stays correct under concurrency — assignment is already
  `FOR UPDATE` on the producer row (traced above).

## 6. Error handling: continue-and-report

One file failing (bad codec, STT failure, LLM outage) must not kill a
50-file batch: the worker marks that file `failed` with the reason,
continues, and the final report lists successes/failures with a
per-file "retry" action (re-runs just that file through the same
path — the graph is already resumable per segment). The batch is
`done_with_failures`, never silently partial: the report is explicit,
and retry is idempotent (a failed segment row is reset/replaced, never
duplicated — delete-and-reingest via the existing deletion path).

## 7. PREFILTER flip — required launch step (finding of 2026-08-25/26)

A bulk-imported archive can exceed the ~150K-token single-prompt
ceiling on DAY ONE, and the measured behavior of an over-ceiling
UNFILTERED archive is not graceful degradation but broken answers
(empty selection on a direct question at 139K tokens). Therefore:

* **`PREFILTER=on` globally is a REQUIRED item of this feature's
  launch checklist** — shipped in the same gated cycle, not later. The
  per-request budget check self-selects per producer: small archives
  remain byte-identically untouched (proven), over-budget archives
  filter from their first question.
* Immediately before the flip, re-run against then-current code/model:
  (1) the **inertness byte-proof** (small-archive prompt hash
  unchanged with the toggle on); (2) the **synthetic gate script**
  (`scripts/gate_prefilter_synthetic.py`, 10 checks against the
  standing synthetic producer). Both must pass; either failing blocks
  the launch.
* Also folded in at flip time (from the plan's punch list): pin-dict
  eviction, the per-read `filtered/admitted` metric, and a re-check of
  the crowd-out finding at the real budget ratio.

## 8. Validation cycle for the feature itself

* Unit: template generation matches the live catalog; validator cases
  (bad id, missing file, unmapped file, duplicates); orchestrator
  pool/continue-on-failure; warm-debounce.
* Integration: a 5-file batch against a THROWAWAY test producer runs
  the real pipeline end-to-end (real STT/LLM), archive answers
  questions afterwards; one deliberately-corrupt file → batch
  completes with 1 failure + working retry.
* The standard prompt_regression panel — bulk import must not touch
  prompt bytes at all (it only feeds the existing pipeline), so the
  panel is a no-change control, plus the PREFILTER flip proofs (§7).
* Synthetic producer stays the large-scale instrument; a bulk-imported
  REAL large archive eventually supersedes it (then `--delete`).

## 9. Producer decisions (ruled 2026-08-26)

1. **No batch/file size caps** — 150+ files in one upload is fine; the
   §5 worker pool is the queuing mechanism regardless of batch size.
2. **CSV row order = take order = created_at order = archive order.**
3. The "skip confirmation" proposal is REPLACED by the auto-extraction
   toggle (§10).

## 10. Auto-extraction toggle (separate Settings feature, consumed here)

**Data model**: `User.auto_extraction` (Boolean, default false =
today's manual behavior), per-producer, one migration, one Settings
switch.

**Branch point — investigated, genuinely isolated**: the entire
confirmation flow funnels through ONE site, `human_confirm_node`
(analysis_graph.py:1219) — one interrupt per recording carrying all
its questions, which already has a zero-questions fast path. Auto mode
adds a single branch at that node: when the producer's flag is on,
skip the `interrupt(...)` and resolve the payload with its
default/as-extracted answers (each question type keeps whatever the
extraction pipeline produced), then continue the graph. Because
`pending_confirmation` is never persisted in auto mode, the bell
notification and the "answer the questionnaire" button disappear with
no frontend changes to those components — they render off pending
state that never exists. The extraction panel itself stays accessible
read/edit-anytime (names, dates, relationships editable as today;
transcript text never editable — unchanged).

**Independence — REVISED 2026-08-26 (producer ruling, supersedes the
earlier require-the-toggle design)**: the Settings toggle controls
regular /record uploads ONLY. **Bulk import always runs auto,
unconditionally** — the producer's stored preference is neither read,
required, nor changed by a batch, and there is no "batch refuses to
start in manual mode" blocking. Mechanically this is the same single
branch at `human_confirm_node`, with the condition widened to:
producer's `auto_extraction` is on OR the segment carries an
`import_batch_id` — a nullable RawSegment column the §4/§5
orchestrator stamps at ingest anyway for per-file batch state and the
final report, so the auto path costs no new mechanism. Imported
segments therefore never persist `pending_confirmation` (no bell, no
questionnaire) regardless of the toggle; the extraction panel remains
reviewable/editable afterward exactly as in §10's auto mode.

**Validation additions**: unit tests for the auto branch (no interrupt,
defaults applied, panel data intact; the import_batch_id condition
fires regardless of the toggle); one integration case in §8's batch
run with the producer's toggle OFF (the batch's segments still reach
`ready` with no pending confirmations — proving the unconditional
auto path); a regular-upload control confirming today's manual flow
is unchanged when the toggle is off and no batch id is present.
