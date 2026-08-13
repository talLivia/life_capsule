# life_capsule — engineering notes

A family story archive. A **producer** records life-story segments at `/record`;
their **family** talk to the archive in the same app shell — a family
account gets exactly three views: Chat (full), Timeline and Family tree
(view-only; docs/FAMILY_UNIFIED_SHELL_PLAN.md — the dedicated `/talk`
page is a redirect stub for old invite links). Answers are always the
producer's own recorded footage — the system never generates speech on their
behalf.

## Chat modes

The producer's `User.chat_mode` selects one mode for everyone talking to them:

| mode | how an answer is produced |
| --- | --- |
| `video_clips_v2` | **the default.** Prompt 15 whole-archive read → trimmed original clips |
| `avatar` | optional, off by default. LLM reply → TTS → MuseTalk lip-sync (`ChatInterface`, `TalkInterface`) |

**v2 stands fully on its own — no avatars row exists for a producer who
never enables avatar mode.** Sessions are producer-keyed
(`sessions.producer_id`); `avatar_id` is optional avatar-mode cargo with
ON DELETE SET NULL (deleting an avatar must never destroy conversation
history). The ONE place the app demands an avatar is switching
`chat_mode` to `avatar` in Settings, which 400s without a ready one.
See docs/V2_PRIMARY_AVATAR_DORMANT_PLAN.md before re-coupling anything
to an avatar's existence.

A third mode, `video_clips` (v1, Prompt 11-14 chunk retrieval), was removed
on 2026-08-12 after the A/B against v2 settled it — see
docs/V1_REMOVAL_PLAN.md, and the `pre-v1-removal` tag for the last tree
that had it. Measurements below that name v1 are that A/B's record, kept
as the evidence they are.

The clip mode's WS contract and behaviour hook
(`frontend/lib/useVideoClipChat.ts`) drive ONE layout for both roles:
`ProducerVideoClipChat` (single video panel + side chat + polaroid
gallery), mounted directly for the producer and via `FamilyChatView`
(availability gate, `producerName` copy) for family. The separate
/talk-style scrolling layout was removed 2026-08-13.

## video_clips_v2: selection is by utterance UNIT, not by time

`backend/app/services/full_archive_retrieval.py` splits every recording into
**utterance units** at the pauses the speaker actually took. The pause
threshold is the 90th percentile of *that recording's own* inter-word gap
distribution — not a constant — so it adapts to how fast someone speaks.

The model returns **unit ids**; final times are derived from the units. Because
half a unit cannot be selected, **cutting mid-sentence is structurally
impossible**, which no prompt wording could guarantee back when the model
emitted raw `{start_sec, end_sec}`. Units are numbered across the *whole*
archive so one answer can draw on several recordings.

Two things follow from this design and should not be "fixed":

- **Breadth falls out of the question.** A narrow question selects one unit, a
  broad one selects many. There is deliberately no duration cap, no
  question-type classifier, and no length heuristic anywhere in the code.
- **Repetition across different questions is expected.** A standalone question
  always gets a real answer even if its footage was shown earlier — people
  revisit a subject from different angles ("tell me about your family", then
  "who is your mother?"). Already-shown material is only withheld from
  *follow-up suggestions*, where the point is to offer something *more*.

## One question, several recordings

A question holds a LIST of takes, not one video. A storyteller comes back and
adds what they left out. Consequences that are easy to get wrong:

- **Ingest APPENDS.** Recording again never destroys a previous take.
  Replacing one is delete + record, both explicit. `DELETE /segments/{id}`
  is the only thing that destroys a recording, and it delegates to
  `segment_deletion.delete_segment_data` — the same implementation account
  reset uses. An entity is dropped only when no recording mentions it any
  more (one `NOT EXISTS` sweep in the same transaction), so a shared entity
  survives a sibling's deletion.
- **Count DISTINCT `question_index` for "answered"**, never segment rows —
  three takes on one question is one question answered.
- **Uploading a video reuses the recording entry point exactly** (presign →
  PUT → `/segments/ingest`). There is no upload endpoint and no second
  ingestion path; if you find yourself writing one, something is wrong.
- Takes are **grouped together in the prompt** and marked `(take N of M)`.
  `created_at` order alone separates them. A question with ONE take prints
  byte-identically to before, so existing archives are untouched.

The **interview question** above each recording is load-bearing context, not
metadata: it identifies people the words never name ("the right person", "my
commander"). Unnamed referents cannot be resolved via the entity map — the
extractor is a *named*-entity extractor, and it correctly returns `[]` for a
spouse who is never named (verified identical across flash-lite, flash, and
pro). The interview question is the anchor that works instead.

## ACCEPTED: the archive-read call is non-deterministic on marginal units

`ARCHIVE_READ_MODEL=gemini-flash-latest` is a **thinking** model, and Gemini's
`seed` does not control the thinking path. Measured over 6 identical requests
per question (seed and `temperature=0` fixed):

- Thinking-token counts vary run to run at **every** setting (e.g. 1475 vs
  1917), and where a unit choice is marginal the answer varies with them.
- `ARCHIVE_READ_THINKING_BUDGET` does **not** fix this. Gemini treats it as a
  soft hint, and **the call is already at the model's thinking FLOOR** —
  `budget=1`, `budget=128` and `thinking_level='low'` each spent exactly 586
  thinking tokens, while `budget=-1` and `thinking_level='high'` each spent
  1918 and `budget=0` is rejected (400). One call per setting, so read that as
  **two levels, not two exact numbers** — the run-to-run variance above still
  applies within each. The point stands either way: `low` is what we already
  get, so **turning the budget down buys nothing** (re-measured 2026-08-01 on
  gemini-3.6-flash). It is pinned for LATENCY, not for reproducibility.

**What varies:** ±1-2 *peripheral* units on broad questions — e.g. `family`
gains/loses `u4`/`u16`, `army-broad` gains/loses `u10`.
**What does not:** core answers. `brothers`, the pronoun follow-up, and
`montreal` were identical across ~96 calls, and the 7 scored eval questions
show stdev **0.000** over 5 runs.

Independently corroborated by the comparison harness (3 runs × 12
questions, run while the v1 mode still existed as the comparison arm):
v2 was **10/12 stable**, and the two that varied were exactly `family` and
`army-broad` — both broad questions, and only those. v1 — deterministic
multi-step retrieval, since removed — was 12/12, which is what makes the
corroboration meaningful: the variance lives in the archive-read call,
not the shared assembly.

This is accepted deliberately — but the REASON has changed, and the old one
here was measurably false by 2026-08-01. Corrected rather than deleted,
because the way it went stale is the lesson:

**⚠️ `gemini-flash-latest` and `gemini-flash-lite-latest` are moving aliases.**
They resolve to `gemini-3.6-flash` and `gemini-3.5-flash-lite` *today*
(`response.model_version` reports it — check there, don't assume). Anything
measured about "the model" expires silently when the alias moves. Everything
below is dated for that reason.

This section previously claimed flash-lite had **0 thinking tokens**, was
**6/6 identical everywhere**, **lost the wife-pronoun follow-up**, and
**regressed Montreal**. Re-measured 2026-08-01, 3 runs per arm: it spends
**300-800** thinking tokens, it **resolves the pronoun follow-up correctly**
(finds `u38`/`u39`), and Montreal is **byte-identical** to flash. All four
claims were wrong, and a decision made from them would have been wrong.

**The real trade, and why we still keep flash** (declined 2026-08-01): the
model swap is worth ~1.4s of a ~9s spoken turn, and it costs the
**narrow-vs-broad discrimination** that this whole design exists to produce.
Asked `באיזה תפקיד שירתת בצבא?` — a NARROW role question — flash returns
`u10-u13` (6.1s) while flash-lite returns `u10-u17` (13.7s), i.e. the broad
army answer. It also drops `u37` from `family`, the closing line about wanting
the grandchildren to know. See "breadth falls out of the question" above:
losing that is losing the mechanism, not a tuning detail.

Note honestly that flash-lite was **more stable** (3/3 where flash varied on
`army-narrow` and `family`) and faster. Stability and speed were still judged
not worth the breadth trade — the same call this section has always made,
now for a reason that is actually true.

**Do not re-tune prompt wording to chase breadth changes.** Three apparent
"narrowings" were investigated; the dominant cause was this run-to-run
variance, not the wording. Reword only with a measured before/after across
multiple runs.

## Evals

```bash
python scripts/rebaseline_accuracy.py     # v2 accuracy as a MEAN over runs (use this)
python scripts/seed_sweep.py              # single-run accuracy vs known-correct (IoU)
```

`compare_retrieval_modes.py` (v1-vs-v2, repeated-run consistency) was
deleted with the v1 mode (docs/V1_REMOVAL_PLAN.md §3.1); run-to-run
stability is `prompt_regression.py`'s job now, and if a dedicated
consistency number is ever wanted again the path is a `--repeat` flag
there, not a revival.

Quote accuracy as a **mean over runs**, not one figure. Current baseline:
**v2 = 0.999 (stdev 0.000 over 5 runs, 7 scored questions)**. Note the scored
set contains no *broad* question, which is exactly where the variance lives —
a known gap in the eval, not a claim that nothing varies.

References were **rebased on 2026-07-27** after the archive was re-ingested
with Deepgram — scores from before that date are measured against different
unit boundaries and are not comparable. See the header of `seed_sweep.py`.

## Local environment gotchas

- **Redis is not running locally.** `cache_service` silently no-ops, so
  anything built on it does nothing in dev. v2's shown-unit memory therefore
  lives in `Message.message_metadata` (Postgres), not the Redis visited-set —
  it also needs unit granularity, which the segment-level visited-set lacks.
- **Whisper `medium` on CPU** costs ~9s per spoken question. Deliberate:
  `small` garbles Hebrew badly enough to break retrieval. Loaded once at
  startup (`main.py`), not per question.
- Model strength is a **per-call** decision (`generate_response(model=...)`).
  Only the archive-read call is upgraded; every other LLM call site stays on
  `LLM_MODEL`. The retrieval coreference call (shared machinery, used by the
  avatar path) and ingestion entity extraction are the next candidates if
  they ever matter.
- Windows: `asyncio.create_subprocess_exec` is unavailable under the pinned
  event-loop policy — shell out via `asyncio.to_thread(subprocess.run, ...)`.
