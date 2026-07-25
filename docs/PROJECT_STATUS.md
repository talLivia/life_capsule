# Project status

**Updated:** 2026-07-25 · **Branch:** `feat/video-clip-chat-modes` (pushed to origin)

Working-state snapshot. Standing rules and architecture invariants live in
[CLAUDE.md](../CLAUDE.md); this file is "where we are right now" and should be
updated as work lands.

---

## Working

**Recording → ingestion**
- `/record` interview flow, now an in-shell view (`RecordPanel`) alongside
  Settings rather than a standalone route. Old `/record` URL redirects.
- Whisper `medium` for both ingestion and the live path; word-level timestamps
  on every `TranscriptChunk` (migration `0010`).
- Graphiti entity extraction per segment, feeding the entity map.

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

**Tests:** 379 backend passing; frontend `tsc`, `eslint`, `next build` clean.

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
- **`מה עשית בצבא?` returns the whole army recording** (u6-u9, 19s), including
  "I'd go home every two weeks". Traced and **stable 5/5**, so this is a
  genuine relevance judgment, not the accepted marginal variance. The model
  reads the question as a paraphrase of that recording's own interview
  question ("tell me about your military service…") and matches at RECORDING
  level rather than unit level — asked about the *role* specifically it
  correctly returns u6 alone. Undecided how (or whether) to address; note it
  is the interview-question anchor (which fixed the wife case) working
  against us here.
- **Follow-up suggestions have not been seen live**, only verified against the
  real archive through `select_units`.

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
2. **Merge strategy for this branch.** Five commits, not yet reviewed or
   merged to `main`.
3. **Whether to tighten follow-up suggestion relevance** (see above).
4. **Whether to add a broad question to the scored eval set**, accepting that
   its reference will be fuzzier than the existing ones.
5. **Whether `avatar` mode is still a supported product path** or effectively
   superseded by the video-clip modes — it still carries MuseTalk, TTS, and
   voice-cloning surface area that nothing else needs.

---

## How to verify

```bash
cd backend
python scripts/rebaseline_accuracy.py      # v2 accuracy as a MEAN over runs — quote this
python scripts/compare_retrieval_modes.py  # v1 vs v2: consistency, latency, calls, tokens
python scripts/seed_sweep.py               # single-run IoU vs known-correct
python -m pytest -q                        # 379 tests
```
