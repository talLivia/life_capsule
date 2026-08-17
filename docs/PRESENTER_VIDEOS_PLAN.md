# Presenter videos on /record — investigation + plan (2026-08-18)

Replace the per-question "Read aloud" TTS with a pre-recorded presenter
video per question (129) plus one Intro walkthrough video (130 files
total). The video plays in the same screen region the recorder uses, then
that region becomes the recording UI. **Plan only — nothing here is
built.**

## 1. Where the current pieces live (traced, not assumed)

**Read aloud** is `frontend/components/record/ReadAloudButton.tsx`:
fetches `api.getQuestionAudio(questionId)` → backend
`GET /api/v1/interview/questions/{id}/audio`
(`backend/app/api/v1/interview.py:91`), which synthesizes with edge-tts
and caches files under `uploads/question_audio`
(`scripts/warm_question_audio.py` pre-warms). It is rendered from
`RecordPanel.tsx:303` and carries two invariants the presenter video MUST
inherit, because they exist to keep the question's voice out of the
recording and out of entity extraction:

  * while it plays, the record button is held disabled
    (`readingAloud` state in RecordPanel);
  * while the camera captures, the control is **absent**, not disabled
    (`capturing` state).

**Question navigation** is `RecordPanel.tsx` + `useInterviewFlow`:
there is no Next button — steps, position, completeness and progress all
come from the server (`get_interview_flow`,
`backend/app/api/v1/interview.py:436`); finishing a recording refetches
the flow and the recomputed position is the advance. `viewingStep` is
`{kind: 'question' | 'gate', id, text}`; question ids are the FROZEN
`childhood_q01`-style ids from `backend/app/interview_questions.json`
(generated file — ids never change once a recording references them).

**The video area** is `VideoRecorder.tsx`'s `bg-black aspect-video`
region (line ~402). VideoRecorder acquires the camera in a mount effect,
so whatever mounts it decides when the permission prompt / camera light
happens. When a question already has takes, `RecordingList` occupies the
slot instead and the recorder appears only after "Add another take".

**Components that change**: `RecordPanel.tsx` (orchestration of the
shared slot), one new `PresenterVideo.tsx`, `InterviewAccordion.tsx` (the
Intro entry point). `VideoRecorder` itself is untouched. `ReadAloudButton`
stays as the fallback (see §5).

## 2. No mapping at all: keys by convention (REVISED 2026-08-18)

First draft proposed a separate mapping file, rationalized partly by
incremental rollout. **Premise corrected: all 129 videos exist today and
ship together.** Re-examined, the right simplification is not moving the
mapping into `interview_questions.json` — it is **deleting the mapping**:

  * The storage key is derived from the frozen question id by
    convention: `presenter/{question_id}.mp4`, plus
    `presenter/intro.mp4`. A mapping earns its existence only when keys
    can't be derived (per-language variants, versioned re-takes); none
    of that exists at launch, and a mapping can be introduced the day it
    does without unwinding anything.
  * One new endpoint, `GET /api/v1/interview/presenter-videos`, loads
    the question ids it already has (from `interview_questions.json`)
    and returns `{intro: url, questions: {id: url}}` with **presigned
    serving URLs** (`storage_service.serving_url(key)` — the bucket is
    private, raw keys can't go to the client). No config to read, no
    config to drift.
  * Completeness is enforced at UPLOAD time, not runtime: the script in
    §5 refuses to upload unless the local set covers every question id
    plus the intro — a missing file fails loudly with the list of
    missing ids, before anything lands.

Why NOT a `video_key` field inside `interview_questions.json`, even with
all videos ready: that file is **generated** (by
`scripts/convert_interview_content.py`) — hand-editing it gets wiped on
regeneration, and teaching the converter means coupling the content
pipeline to asset presence forever. When every key is mechanical, a
field that always holds `presenter/{its own id}.mp4` is 129 lines of
redundancy with a maintenance cost and no information.

## 3. The interaction: one slot, two modes, in RecordPanel

A small state machine per question step, living in RecordPanel (which
already owns the slot's contents — recorder vs takes list):

  * `mode: 'presenter' | 'record'`, reset to `'presenter'` on
    `viewingStep.id` change **iff** the question has a mapped video and
    no takes yet; otherwise straight to today's behavior.
  * `'presenter'`: new `PresenterVideo` component in the same
    `aspect-video` slot — plays the mapped URL, `onEnded` → `'record'`.
    Controls: **Skip to recording** (always visible) and **Replay**.
    Autoplay honestly handled: the click that selected the question is a
    user gesture, so `play()` with sound normally succeeds; if the
    browser rejects it (deep-link, reload mid-question), the component
    shows a play button instead of pretending — same graceful-degrade
    philosophy as ReadAloudButton.
  * `'record'`: exactly today's `VideoRecorder`/`RecordingList` block.
    The camera is acquired only on this transition — the permission
    prompt and camera light never run while the presenter talks.
  * The TTS invariants carry over by construction: the record button
    cannot exist while the presenter plays (the recorder isn't mounted),
    and the presenter cannot be replayed while capturing (its controls
    are absent in `'record'` mode while `capturing` — a "Watch the
    question again" link renders only when not capturing, flipping mode
    back to `'presenter'`).
  * Questions with existing takes keep the takes list as the default
    view (unchanged), with the "watch the question again" link available.

**TTS Read-aloud is demoted to the failure path, not deleted (REVISED
2026-08-18).** With all 129 videos at launch there is no *rollout*
fallback — but two real scenarios still want a net: a future
content-regeneration adds a NEW question id whose video hasn't been
produced yet, and a runtime playback failure (expired URL on a stale
tab, codec/network error). In both, blocking recording on a broken
video would be the real harm. So: ReadAloudButton disappears from the
normal flow entirely — it renders only when a question has no video URL
or the player's `onError` fires, exactly where it used to render, with
its two voice-never-in-the-recording invariants intact. Not kept as a
parallel "accessibility option": the presenter video IS the audio
reading of the question, so a second speaker button next to a working
video is redundant chrome. The backend TTS endpoint and warm script
stay as-is (they cost nothing and are the net's other half).

Why one swapping slot rather than two stacked components: the request is
"same screen space", the slot is already conditionally occupied
(recorder vs takes), and stacking would run camera and video
side-by-side — the exact voice-into-recording hazard the TTS design
spent two states preventing.

## 4. The Intro entry — purely client-side, confirmed assumptions

The Intro is **not a flow step and never touches the server**: it gets
no question id, is never sent to any endpoint, records no answer. All
counting is server-side (`get_interview_flow` computes position/progress
from real question steps; "answered" is `COUNT(DISTINCT question_index)`
per CLAUDE.md), so exclusion from counts, progress, and
confirmation/extraction is automatic as long as the intro never becomes
a step — which this design guarantees by construction.

UI: a "How recording works" card above the accordion (InterviewAccordion
header area) that plays the intro video in the shared slot. Assumption
to confirm: **one-time explainer, replayable on demand** — I'd add a
`localStorage` seen-flag so it auto-opens on the very first visit and is
a one-click replay afterwards. If it should instead auto-play on every
visit, or gate the first recording, that's a different (heavier) design
— flagging rather than assuming.

## 5. Storage/upload: bulk script, not an upload UI

130 files, produced offline, all ready now. The presign→PUT browser
pattern (`/interview/segments/presign`, `/media/presign`) earns its
complexity for end-user uploads; for a one-time producer-side batch it's
ceremony. Proposal: `scripts/upload_presenter_videos.py` —

  * reads a local directory where files are named by frozen id
    (`childhood_q01.mp4`, …, `intro.mp4`);
  * validates the set BOTH ways against `interview_questions.json`
    before uploading anything: unknown filenames fail loudly (typos),
    and missing ids fail loudly (completeness — the runtime has no
    mapping, so the script is where "every question has a video" is
    enforced);
  * uploads via `storage_service.upload_file` to `presenter/<id>.mp4`.

Re-running is idempotent (same keys, overwrites); replacing one video is
dropping one file in the directory and re-running with
`--allow-partial` (replacement runs shouldn't require all 130 files
present locally — the flag skips the completeness check, never the
typo check). No new upload endpoints, no new auth surface, no config
file to regenerate.

## 6. Scope + branch

* Backend: config file + one GET endpoint (+tests) — small.
* Frontend: `PresenterVideo.tsx`, RecordPanel orchestration, accordion
  Intro card (+tsc; behavior verified live) — the real work, moderate.
* Script: upload + config generation — small.
* Content: 130 videos to produce — outside the code plan, and the
  incremental-rollout fallback (§2) means code can land first.

**Branch: `presenter-videos` off `light-mode`**, parallel to
`avatar-shared-engine` — this is /record-only work, orthogonal to the
avatar/engine stack, and the two branches touch disjoint files (engine
work never touches RecordPanel/record components; this never touches
websocket/engine). Off `main` would miss the light-mode tokens the
/record UI now uses. If the stack merges first, rebase is trivial for
the same disjointness reason.

## Open questions before building

1. Intro behavior: auto-open on first visit + replayable after
   (recommended), or something heavier (gate the first recording)?
2. Already-answered questions: takes list stays the default with a
   "watch the question again" link (recommended), or presenter video
   again on every visit?
3. ~~TTS fallback during rollout~~ — RESOLVED by the 2026-08-18
   revision: no rollout, so no rollout fallback; Read-aloud survives
   only as the missing-video/playback-failure degradation path (§3) and
   is invisible in the normal day-one flow.
