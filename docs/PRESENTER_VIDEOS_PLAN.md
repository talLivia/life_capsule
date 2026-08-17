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

## 2. Config: a NEW file, not an extension of interview_questions.json

`interview_questions.json` is a **generated** file (by
`scripts/convert_interview_content.py`, from
`docs/interview_content_source.json`) with frozen ids and a schema built
for interview *content*. Presenter videos are presentation *assets* that
will land incrementally and be re-recorded independently of wording —
wiring them into the generated file means teaching the converter about
videos and regenerating content to swap a video. Separate file, keyed by
the frozen ids:

```json
{
  "schema_version": 1,
  "intro": { "key": "presenter/intro.mp4" },
  "questions": {
    "childhood_q01": { "key": "presenter/childhood_q01.mp4" },
    "childhood_q02": { "key": "presenter/childhood_q02.mp4" }
  }
}
```

Location: `backend/app/presenter_videos.json`, served by one new
endpoint `GET /api/v1/interview/presenter-videos` that returns
`{intro: url|null, questions: {id: url}}` with **presigned serving URLs**
(`storage_service.serving_url(key)` — the pattern every other stored
asset already uses; raw keys can't go to the client because the bucket is
private). A question absent from the file simply has no video — the UI
falls back to Read-aloud, which is what makes incremental rollout of 129
videos safe. `values are objects, not bare strings`, so later per-video
metadata (duration, language) needs no schema break.

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

130 files, produced offline, uploaded roughly once. The presign→PUT
browser pattern (`/interview/segments/presign`, `/media/presign`) earns
its complexity for end-user uploads; for a one-time producer-side batch
it's ceremony. Proposal: `scripts/upload_presenter_videos.py` —

  * reads a local directory where files are named by frozen id
    (`childhood_q01.mp4`, …, `intro.mp4`);
  * validates every name against `interview_questions.json` ids (typos
    fail loudly, BEFORE upload);
  * uploads via `storage_service.upload_file` to `presenter/<id>.mp4`;
  * regenerates `presenter_videos.json` from what actually uploaded.

Re-running is idempotent (same keys, overwrites); replacing one video is
dropping one file in the directory and re-running. No new upload
endpoints, no new auth surface.

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
3. During rollout, unmapped questions fall back to today's TTS
   Read-aloud (recommended) — or should the button disappear entirely
   once presenter videos exist?
