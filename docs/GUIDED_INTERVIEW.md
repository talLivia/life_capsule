# The Interview — guided hands-free recording

> ## ⚠️ Part One is SUPERSEDED (2026-08-04)
>
> The separate interview screen was dropped before any of it was built. The
> same experience is being folded into `/record` instead — **see PART TWO at
> the end of this file, which is the live plan.**
>
> Part One is kept because its reasoning is still load-bearing: the bell
> design, the poll-versus-WebSocket call, the evidence-phrase requirement and
> the hard constraint all carry forward. Its CAPTURE design does not, and §11
> lists exactly what dissolved and why.


**Part One written 2026-08-04. Nothing built. Superseded the same day.**

A NEW screen alongside `/record`, not a replacement. `/record` keeps every
behaviour it has today — browsing questions, recording, uploading, deleting,
and an immediate confirmation popup after each recording. None of that changes.

The new screen lets an elderly storyteller sit down, press one button and talk:
never picking a question, never pressing record, never uploading, never seeing
129 questions laid out in front of them.

This document is written to be picked up cold by a session that was not here
when it was written. Where it says **VERIFY**, the claim was inferred from a
previous session's knowledge and should be re-checked against the code before
it is relied on.

---

## 1. What exists, and what genuinely does not

The honest summary: **the pipeline is entirely reusable and the capture surface
is entirely new.** No part of ingestion, extraction, or the six confirmation
question classes changes.

### Carries over untouched

| Component | Where | Note |
| --- | --- | --- |
| Question set, gates, branching | `app/interview_config.py` | 16 categories, 129 questions, step tree |
| Position, reachability, `can_record` | `app/services/interview_flow.py` | Derived, no stored cursor |
| Presign → PUT → `/segments/ingest` | `app/api/v1/interview.py` | THE ingest path; there is no second one and must not be |
| Analysis graph, all six question classes | `app/analysis_graph.py` | `build_confirmation_payload` stays the single source |
| Write paths | `entity_store` | Unchanged |
| Confirmation payload + answer contract | `EntityBatchConfirmRequest` | See the hard constraint, §7 |
| `EntityConfirmModal` question rendering | `components/record/EntityConfirmModal.tsx` | The bell reuses the SECTIONS, not the modal shell |
| TTS | `app/services/tts.py` | Exists for the avatar path |
| MediaRecorder capture | `components/record/VideoRecorder.tsx` | The mechanics; not the UI |

### Genuinely new

| Piece | Why nothing today does this |
| --- | --- |
| **Session orchestrator** | A state machine over speak → record → detect silence → save → advance. `/record` has no notion of a session that moves on its own. |
| **Automatic segmentation** | `VideoRecorder` is explicit start/stop with a review step. Here a segment begins and ends without a decision. |
| **Question audio** | TTS exists but is wired to avatar replies. Reading the question set is new wiring (and see §2.3 — it should be pre-generated, not synthesised live). |
| **Silence timer** | `lib/useContinuousVoiceInput.ts` uses Silero VAD for `/talk`. This needs something much cruder — see §2. |
| **Global notification bell** | No global pending-state component exists. `EntityConfirmModal` is mounted inside `RecordPanel` only. |
| **Interview resume point** | New column, new semantics — see §4. |
| **Recording origin marker** | New column. Powers both the resume point AND the popup/bell split — see §3.4. |

---

## 2. The silence timer

### 2.1 The reasoning holds — with one correction

**Confirmed: this is a far easier problem than sentence-level end-of-speech
detection**, and for the reason given. That problem is hard because it must
decide, in a few hundred milliseconds, whether a pause is a comma or a full
stop — and an elderly speaker recalling 1948 pauses mid-sentence for seconds.
Nobody pauses for 30 seconds mid-story, so a fixed timeout sidesteps the
judgement entirely.

The correction: **a silence timer still needs to know whether sound is
happening.** "If they keep talking, the blinking stops" requires detecting
speech. But the bar is far lower — an **RMS energy threshold over the mic
stream** (Web Audio `AnalyserNode`) is enough, because the question is "is
there sound now", not "was that the end of a sentence". No model, no ONNX, no
tuning against a speaker.

So: keep Silero out of this screen. Using it would import a hard problem to
solve an easy one.

### 2.2 Thresholds and what they mean

| Event | Threshold | Behaviour |
| --- | --- | --- |
| Silence begins | — | Timer starts |
| Silence continues | **7s** | Main button (red) starts blinking. Nothing is saved. |
| Sound resumes | any | Timer resets, blinking stops |
| Silence continues | **30s** | Auto-advance, identical to pressing the button |

Both are generous on purpose. Erring towards waiting is right: a false advance
costs a truncated answer, a late advance costs a few seconds.

**Open decision (2.A):** is the 30s measured from the start of silence, or 30s
*after* the 7s blink (i.e. 37s total)? The spec reads as the former. Worth
confirming — it changes how long a thinking pause can be.

### 2.3 Question audio must not land in the recording

**Not in the spec, and it will bite.** If recording starts the moment the
question appears and TTS reads it aloud, the synthetic voice is captured in the
video AND in the transcript. Consequences:

- Entity extraction runs over the transcript. A question mentioning a name
  would produce entities the storyteller never said.
- The stored video opens with a robot voice, which is not what a grandchild
  should be handed.

Three options:

1. **Start recording when TTS finishes.** Clean transcript, clean video. Costs
   a beat of dead air; the storyteller may start talking over the tail of the
   question and lose their first words.
2. **Record throughout, trim the TTS span before upload.** The exact duration
   is known, so the trim is deterministic. More moving parts.
3. **Record throughout, accept it.** Cheapest, and wrong for the reason above.

**Recommendation: (1)**, with the mic opening slightly before the audio ends to
catch an eager start. Flagged as **open decision 2.B**.

### 2.4 A silent segment is not an answer

If the storyteller says nothing and 30s elapses, option (1) above yields a 30s
video of silence. That must not be ingested as an answer: it would count as a
question answered, produce an empty transcript, and sit in the archive.

**Rule: if no sound crossed the threshold at any point, treat the advance as a
SKIP.** Discard the segment, record nothing.

---

## 3. The global bell

### 3.1 Why it exists

Extraction takes tens of seconds. Surfacing clarifications inline would either
stall the session in silence or interrupt a story mid-flow. Accumulating them
and letting the storyteller answer at their own pace is the right shape.

### 3.2 Staying in sync — polling, and specifically the poll that already exists

`EntityConfirmModal` already polls `/segments/pending-confirmations` every 8s
and opens itself when one appears. The bell needs exactly that signal, globally.

**Recommendation: promote the existing poll into a provider mounted in the app
shell; do not add a WebSocket.**

- The endpoint, the auth and the shape all exist and are proven.
- A poll survives reconnects, sleep and navigation with no reconnection logic.
- Latency does not matter here — this is a badge that can be seconds stale.
- A WebSocket would be a second transport carrying one integer.

Two refinements:

- **Faster while a segment is in flight.** After an ingest, poll at ~2s until
  it settles, then back off to 8s. The same reasoning as the extraction
  screen's progress poll.
- **One poller for the whole app.** Two components polling the same endpoint is
  how the counts drift apart.

### 3.3 What a pending question must show

Time may have passed, so each item needs the phrase it came from — *"you said:
'my uncles are אמנון and קובי'"*. Relation questions already carry `evidence`
(**VERIFY** — confirmed present for relation proposals; the other five classes
may not have an equivalent and probably need the segment's `question_asked` as
the fallback context).

The bell should also group by recording, since answers submit per segment.

### 3.4 The popup and the bell must not both fire

**The most important detail in this document, and not in the spec.**

`EntityConfirmModal` polls globally and opens on ANY pending confirmation. Today
that is fine because it is mounted only inside `RecordPanel`. But the spec says
`/record` keeps its immediate popup while interview recordings accumulate in the
bell — so the two must be told apart, or a producer who records in the interview
and then visits `/record` gets the popup anyway.

**The origin marker on the recording is what splits them**, and it must reach
the pending-confirmation payload so the client can filter:

- popup shows pending confirmations whose segment origin is `record`
- bell counts and shows those whose origin is `interview`

**Open decision (3.A):** should the bell show BOTH — i.e. also count anything
left unanswered from `/record` — so it is genuinely "everything outstanding"?
Argument for: a producer who dismisses the popup currently has no way back to
those questions at all. Argument against: it changes `/record`'s behaviour,
which the spec says stays identical. My recommendation is that the bell counts
everything but only AUTO-OPENS nothing, and the popup keeps its current
auto-open for `record`-origin segments only. That fixes a real existing gap
without changing what `/record` does.

---

## 4. The resume point

### 4.1 What is stored

**A `question_id`, not an index, and not a count.**

Phase 1b of `FAMILY_TREE_TIMELINE.md` established this for recordings and the
same reasoning applies exactly: `question_index` is positional and moves when
the question set is edited; `question_id` is stable and survives reordering.
Storing "the interview got to question 40" would silently point at a different
question after any edit to the JSON.

Store on the interview session (**open decision 4.A** — session row vs. a small
new table):

| Field | Meaning |
| --- | --- |
| `last_presented_question_id` | The last question the INTERVIEW put on screen |
| `last_presented_at` | For "you were last here on…" and for stale-session handling |

Note this is **the last presented, not the last answered** — a skipped question
still moves the interview forward, which is the whole point of skip.

### 4.2 Resuming

Resolve the stored id against the CURRENT question set via
`interview_config`, then present the next reachable question after it.

| Situation | Behaviour |
| --- | --- |
| Id still live | Resume at the next reachable question after it |
| Questions added before it | Resume position is unchanged — it is an id, not an offset. New earlier questions are simply not presented; they remain answerable in `/record`. **Open decision 4.B:** should the interview double back for them? |
| Questions reordered | Resume follows the id to its new place. The interview continues in the FILE's order from there. |
| **Id retired** | Cannot be resolved. Fall back to the first reachable unanswered question, and say so on screen rather than silently jumping. `get_retired()` can confirm the id existed, which distinguishes "retired" from "corrupt". |
| No stored id | First reachable question |

### 4.3 Independent of `/record`

The interview tracks its own position and does not skip questions answered
manually. A question can therefore end up with several recordings, which is
already supported and normal — `/record` counts DISTINCT `question_index` for
"answered" precisely because takes are many-to-one.

### 4.4 Gates are unsolved and must be

**A gap in the spec.** The 16 categories contain **gate steps** — screening
questions ("did you serve in the army?") that are answered yes/no and are not
recordable. `interview_flow` already treats them as a distinct kind.

A hands-free interview cannot present a gate the way it presents a question: it
has no recording to make, and its answer changes which questions come next.

Options:

1. **Answer gates by voice** — "did you serve?" → yes/no via speech. Needs the
   constrained-matching machinery this design otherwise avoids entirely.
2. **Answer gates by button** — the only two-button moment in the session.
   Contradicts hands-free, but only for a handful of steps.
3. **Skip gated categories entirely in the interview** and let them be opened
   in `/record`. Simplest, and quietly drops a lot of the questionnaire.

**Recommendation: (2).** It is honest, it needs no new understanding layer, and
a yes/no is the one interaction an elderly user manages easily. **Open decision
4.C** — this materially changes what "hands-free" means and needs your call.

---

## 5. Edge cases

| Case | Behaviour |
| --- | --- |
| **Segment mid-ingestion when they pause** | Ingestion is server-side and already detached from the client — pausing must not cancel it. The resume point already advanced, so the answer is safe. |
| **They leave the screen mid-session** | Same. The upload completes or fails on its own; nothing about the session state depends on the page staying open. |
| **They leave mid-RECORDING** | The in-progress segment is lost. **Open decision 5.A:** upload the partial answer, or discard it? A partial answer is probably better than none, but it will be truncated mid-sentence. |
| **Interview started before a questionnaire change** | Handled by §4.2 — the id resolves or it does not, and the fallback says so. |
| **Pending question whose recording was deleted from `/record`** | `pending_confirmation` lives on the segment row, so deleting the recording removes the question with it. The bell count must therefore be derived from a live query, never cached client-side, or it will show a badge for something that no longer exists. |
| **Ingest fails** | The question is already behind them. Surface it in the bell as a failed recording rather than silently losing an answer. **VERIFY** what the failure path currently does. |
| **No camera or mic permission** | Cannot start; say so before the session rather than at the first question. |
| **Device sleeps mid-session** | Treat as pause on wake, never as an advance. |
| **Two tabs** | Two orchestrators, one resume point. Last write wins and the earlier tab desyncs. Probably acceptable; worth a single-session guard if cheap. |

---

## 6. Things not in the spec that will bite

1. **§2.3 — TTS audio in the recording.** The most consequential. It pollutes
   the transcript that entity extraction reads.
2. **§3.4 — popup and bell both firing.** Requires the origin marker to reach
   the payload, not just the recordings table.
3. **§4.4 — gates.** A whole class of step the design has no answer for.
4. **§2.4 — silent segments** becoming answers.
5. **Language.** TTS must speak the producer's `recording_language`. **VERIFY**
   `tts.py` supports Hebrew at acceptable quality — if it does not, this feature
   does not work for the actual storyteller, and that is a go/no-go finding
   worth establishing in Phase 0 rather than Phase 3.
6. **Upload concurrency.** Answer N uploads while answer N+1 records. Two
   MediaRecorder streams must not collide, and a slow upload must not delay the
   next question.
7. **Progress percentage — of what?** 129 questions is discouraging and
   misleading once gates rule some out. Suggest: percentage of REACHABLE
   questions, recomputed as gates are answered. **Open decision 6.A.**
8. **Nothing to review.** `/record` shows the take and allows re-record before
   ingest. The interview has no review step by design — so a fumbled answer is
   only fixable afterwards in `/record`. Worth being deliberate about; the spec
   accepts it, and it is the right trade for hands-free.

---

## 7. The hard constraint

> The bell must POST the identical `EntityBatchConfirmRequest`. The server must
> not be able to tell whether an answer came from the popup or the bell.

This session added six question classes, and each reached both the graph router
and the client automatically because both read one shared function
(`build_confirmation_payload` / `countQuestions`). Every bug that cost real
recordings came from a second place that had to be updated and was not.

Concretely, for the bell:

- Reuse the confirmation SECTIONS from `EntityConfirmModal`, do not reimplement
  them. Extract them into shared components if the modal shell is in the way.
- The bell must count questions the way the client already does — **every array
  in the payload except the known non-question keys** — so a seventh class
  appears with no change here.
- No new answer endpoint, no new answer shape.

---

## 8. Build order

| Phase | Work | Reused | New |
| --- | --- | --- | --- |
| **0** | Feasibility: Hebrew TTS quality; RMS silence detection against a real slow speaker; confirm §2.3 and §4.4 decisions | — | Throwaway |
| **1** | Migration: recording origin marker + interview resume point | — | 1 migration, 2 columns |
| **2** | Origin reaches `pending-confirmations`; popup filters to `record` origin | Endpoint, payload | Filter + field |
| **3** | Global bell: shared poll provider, badge, full-screen list reusing the modal's sections | Poll, sections, answer contract | Provider, badge, list shell |
| **4** | Interview screen shell: preview, question text, progress, two-button state machine — **no audio yet**, advance by button only | `interview_flow`, ingest | Screen, orchestrator |
| **5** | Question audio (TTS, pre-generated) + Read Again | `tts.py` | Cache, playback |
| **6** | Silence timer, blink, auto-advance, silent-segment skip | — | RMS detector, timers |
| **7** | Resume point wiring + gates per decision 4.C | `interview_config` | Resolver |
| **8** | Edge cases from §5, failure surfacing | — | — |

Phases 1–3 deliver the bell and are independently useful — they close the
existing gap where a dismissed popup loses its questions permanently. Phases
4–6 are the screen. **Phase 0 gates everything**, because a bad Hebrew TTS
voice or unusable silence detection changes whether this is worth building at
all.

---

## 9. Open decisions — your input before implementing

| # | Decision | My recommendation |
| --- | --- | --- |
| 2.A | Is 30s from the start of silence, or 30s after the 7s blink? | From the start of silence |
| 2.B | TTS audio: start recording after it, trim it, or accept it? | Start recording after it finishes |
| 3.A | Does the bell count `/record` questions too, or interview-only? | Count everything, auto-open nothing; popup keeps auto-open for `record` origin |
| 4.A | Resume point on the interview session row, or its own table? | Session row, unless a producer can run several interviews |
| 4.B | Does the interview double back for questions added earlier than the resume point? | No — surface them in `/record` instead |
| 4.C | **Gates: voice, button, or skip gated categories?** | Button — the one two-button moment |
| 5.A | Leaving mid-recording: upload the partial answer or discard it? | Upload; a truncated answer beats none |
| 6.A | Progress percentage over all 129, or over reachable questions? | Reachable, recomputed as gates are answered |

---

# PART TWO — folded into `/record` (2026-08-04, SUPERSEDES Part One)

The separate screen is dropped. The same guided experience is built into
`/record`, which already exists.

## 10. Why

A second capture surface means every future question class gets built twice.
That is precisely the duplication that produced the silent bugs of 2026-08-03:
a router that never learned about three question classes, a client render guard
that counted only two, a payload shape that crashed a screen it had outgrown.
Each was a second place that had to be updated and was not.

And almost none of the guided experience actually required a new screen. What
made it feel like one was a set of UI properties — one question at a time, a
sense of progress, the question read aloud, nothing interrupting — and every
one of those is reachable by changing the screen that already exists.

**Part One is kept as history**, not deleted. Its analysis of the bell, the
poll-versus-WebSocket call, the evidence-phrase requirement and the hard
constraint all carry forward unchanged. Its capture design does not.

## 11. What dissolves — and it is most of Part One

Manual capture on an existing screen removes the hard parts wholesale.

| Part One | Status now | Why |
| --- | --- | --- |
| §2 Silence timer, 7s blink, 30s auto-advance, RMS detector | **Gone** | The storyteller presses record and stop, as today. Nothing runs on a timer, so nothing has to decide when they finished. |
| §2.3 TTS audio inside the recording | **Gone, and better** | Read-aloud is only offered BEFORE recording and disables the record button while it plays. The guarantee is now structural — enforced by control state, not by getting a sequence right. Stronger than Part One's recommendation. |
| §2.4 Silent segment becoming an answer | **Gone** | No auto-advance, and `/record` still has its review step. |
| §3.4 Popup and bell both firing | **Gone** | The popup is removed. Everything goes to the bell, so there is nothing to tell apart. |
| §4 Resume point, `last_presented_question_id` | **Gone** | `interview_flow` already derives position with no stored cursor. The progress bar reads the same derived state. |
| §4.4 Gates | **Gone** | `/record` already handles gates through `GateStep`. No new interaction. |
| Origin marker column | **Gone** | It existed to power the resume point and split popup from bell. Neither survives. |
| Session orchestrator, automatic segmentation | **Gone** | — |
| §5 Upload concurrency, mid-recording exit, two tabs, sleep | **Gone** | These were properties of an unattended session. `/record`'s existing behaviour covers all of them. |

**Consequence worth stating plainly: there is no migration.** Part One opened
with two new columns; this needs none. Nothing in the schema changes, and
nothing in ingestion, extraction, the six question classes or any write path
changes either.

## 12. What carries forward unchanged

- **§3.1–3.3 The bell** — its purpose, the promote-the-existing-poll call over
  a WebSocket, one poller for the whole app, faster while a segment is in
  flight.
- **The evidence phrase**, and it is now *load-bearing rather than nice to
  have*. With the popup gone, nobody answers while the recording is fresh —
  every answer happens later, with less context. "You said: 'my uncles are
  אמנון and קובי'" is the only thing carrying that context.
- **§5 The deleted-recording case** — `pending_confirmation` lives on the
  segment row, so deleting a recording removes its questions. The badge must be
  derived from a live query, never cached, or it counts things that no longer
  exist.
- **§7 The hard constraint**, verbatim.
- **§6.5 Hebrew TTS quality** — still worth checking early, but the stakes drop:
  if it is poor you lose one optional button, not the feature.
- **§6.7 Progress percentage** — still an open decision (6.A).

## 13. What is new

### 13.1 `/record` UI

| Change | Note |
| --- | --- |
| **Collapsible question panel** | Slides right and disappears, leaving a single-question view. Expanding restores it. The panel already has its own scroll container. |
| **Takes as an accordion** | One video area; further takes listed below as collapsed rows that expand into the player. With one take, no list at all. |
| **Progress bar** | Percentage through the questionnaire, at the top. |
| **Read question aloud** | TTS, available only before recording starts; disables record while playing. |
| **Pause / Resume** | Toggle; record greys out while paused. |
| **Persistent explainer** | Always-visible summary of what each control does. |

### 13.2 The bell

As Part One §3, minus the origin filter. A top-bar component on every screen,
count badge, full-screen list, answerable one at a time, persisting across
navigation.

**Superseded in one respect — see §17.** The bell opens a LIST, not a
question, and the list is a general notification surface rather than a
confirmations surface.

### 13.3 Second entry point

A **Show questions** button inside the extraction panel when that recording has
pending questions. The extraction payload already carries
`awaiting_confirmation`, so this needs no new field.

### 13.4 The nudge

Non-blocking, never a modal: on leaving the recording screen with pending
questions, or after a long session — *"20 questions are waiting for you."*

## 14. The risk in this change, and where it actually lives

**Removing the popup is the substantive change here, not the UI work.** You have
already named the behavioural risk — people never answer — and the nudge is the
mitigation.

The implementation risk is elsewhere, and it is not in your list:

**The extraction screen was built to hand off to the popup.** That machinery is
live and interlocking: `ExtractionModal` locks itself while work is in flight
and unlocks only by handing off (`onNeedsConfirmation`) or by confirming there
is nothing to ask; `RecordPanel` owns the sequence through `reviewingSegmentId`
and `confirmNudge`; `EntityConfirmModal` signals back through `onResolved` so
the extraction screen can reopen and show what was finally captured.

Remove the popup naively and **the extraction screen sits locked forever**,
waiting for a handoff that never comes. That is the single thing most likely to
go wrong.

The correct unwind: when a segment reaches `awaiting_confirmation`, the
extraction screen closes itself and the bell takes over — the same terminal
state it already has for "nothing to ask", reached by a different route. The
lock, the timeout escape and the failure escape all stay.

### ✅ DONE (2026-08-05) — and the lock was never the danger it looked like

Unwound as described. The lock, the 90s timeout escape and the failure escape
are all untouched. What changed: `onNeedsConfirmation` is deleted, and
`awaiting_confirmation` now just falls through to the close this screen
already did for "nothing to ask" — on a longer dwell, because that state has
a sentence to READ (where the questions went) rather than a result to notice.

**The "locked forever" risk was already structurally prevented, and not by
anything in the frontend.** `segment_extraction` computes
`still_processing = status not in (ready, analyzed, failed,
pending_confirmation)` — so the flag the lock reads is *already false* by the
time the pipeline pauses. Removing the handoff could not have stranded the
screen. Verified against a real segment driven to its pause through the actual
pipeline, not by reading the code:
`test_extraction_unlocks_when_the_pipeline_pauses_for_answers` asserts
`still_processing is False` on the extraction endpoint's own response, so
adding `pending_confirmation` back to the processing statuses now fails loudly
instead of silently trapping a producer on a screen with no close button.

Worth stating plainly because the reasoning generalises: the risk was named
correctly, and the mitigation turned out to live one layer down from where the
plan looked for it.

**Two things phase 1 built that nothing consumed**, both load-bearing once the
popup goes, both now wired:

- `setActive` (2s polling while a recording is in flight). With the popup gone
  the badge is the *only* signal that questions arrived; at the idle 8s cadence
  it appears seconds after the extraction screen has already closed, which
  reads as nothing having happened. `RecordPanel` turns it on for exactly the
  window its extraction screen is open.
- `autoOpenPending` / decision 16.C. Removing the popup removed the only thing
  that ever put these questions in front of anyone, so the once-per-producer
  open is what stops the bell being a badge nobody has been taught to look at.

**16.C needed one qualification the decision did not anticipate.** A producer's
first pending item almost always arrives *while the extraction screen is still
up* — so a naive auto-open pops the question list over the top of it, which is
the stacked-dialogs problem the handoff existed to prevent, reintroduced from
the other side. `autoOpenPending` is therefore also gated on `!active`: the one
teaching moment waits the few seconds for the extraction screen to close and
lands on an empty screen. Gated inside the provider rather than negotiated
between the two screens, because cross-component sequencing is precisely what
this phase deleted.

**What is genuinely lost, and is phase 4's to restore.** Answering used to
reopen the extraction screen (`onResolved`), so the producer saw the entities
their answers had just written — entities are only persisted once the answers
land, so that was the first moment there was anything to show. Nothing does
that now: the bell's list advances to the next recording or reports that there
is nothing left. `onResolved` is deleted rather than left dangling. §13.3's
**Show questions** button re-links the two screens from the other direction.

`EntityConfirmModal` also gained a close button and an Escape handler. As an
auto-opened popup it was a dead end deliberately — answering was the only way
out, which is defensible when it appears the moment the recording is fresh. The
same dead end reached by clicking a bell is just a trap.

### ✅ AND THEN — the extraction screen stopped auto-opening too (2026-08-05)

Decided immediately after the above, and it removes most of what the unwind
had just carefully preserved.

**Finishing a recording now goes straight to the next question.** Nothing
opens, nothing has to be dismissed, nothing is waited for. The recording is
read on the server and anything it raises appears in the bell.

The reasoning is the same argument one step further. The extraction screen's
lock, its progress bar and its handoff all existed to hold the producer in
place until the confirmation questions were ready. The bell does that job
asynchronously now, so holding them there serves nothing — it is purely an
interruption between one question and the next.

**The screen itself stays**, opened on demand by "Extracted from this" on a
recording. It is no longer something that appears on its own.

#### What that made redundant, and what earned its place

| Machinery | Verdict |
| --- | --- |
| **The lock** (`locked`, no close button, no Escape, no backdrop) | **Gone.** It existed so nobody wandered off mid-processing and met the questions later against a recording they had moved past. That is now the *design* — questions arrive later, in the bell, by choice. The only way in is a button the producer pressed on a recording they picked, and trapping someone in a read-only panel they opened out of curiosity was never defensible. |
| **The 90s stall escape** (`STUCK_AFTER_MS`, `escapable`) | **Gone.** It only ever released the lock. No lock, nothing to escape. |
| **The failure escape** (`status === 'failed'` → `escapable`) | **Gone as an escape — replaced by an actual failure state.** This is the one that needed care: `failed` was handled in exactly one place, and all it did was unlock the modal. Deleting it would have left failure entirely unsurfaced, and the sections below would render empty — reading as "nothing was found in your recording", which is a much worse claim than "we could not read it". There is now a banner that says so. |
| **The progress bar + 2s poll** | **Kept, reduced.** Nobody is held here waiting, but the panel can still be opened ON a recording the pipeline has not finished, and then an empty entity list reads as a finished, disappointing result rather than an unfinished one. The instructional copy ("Stay here…") is gone — closing it changes nothing. |
| **The `live` prop, `DONE_DWELL_MS`, `HANDOFF_DWELL_MS`, self-close** | **Gone.** All of it described a screen that opened itself. |
| **The banner naming the bell** | **Kept**, and it is the whole handoff now: a producer who came looking is told where the questions are. |

**A fifth piece went with them, not in the original list: the provider's 2s
`setActive` cadence** (§3.2's "faster while a segment is in flight"). Its
justification was that the producer is watching for the badge. They are not,
by design — they have moved on to the next question. One 8s poll, no second
cadence for screens to turn on and off, and `setActive` is gone from the
context.

**Decision 16.C needed re-gating, for a worse reason than before.** The
stacked-dialogs risk is gone with the auto-open, but the replacement is
sharper: a producer's first pending questions land seconds after their first
recording, i.e. while they are on the record screen and possibly part-way
through recording the NEXT answer. A modal opening over a live camera could
cost a take. Auto-open is therefore suppressed on the record view entirely
(gated in `page.tsx`, which owns `view`, rather than in the provider — WHEN to
interrupt is the screen's knowledge, not the poller's). The flag survives
until they leave, and the bell is visible the whole time.

**Second-order:** `EntityConfirmModal` currently polls globally and opens
itself. It becomes a rendered-on-demand list with no auto-open. Its polling
moves to the shared provider; its question SECTIONS are reused as-is, which is
what keeps the hard constraint honest.

## 15. Build order

The bell must exist **before** the popup is removed, or there is a window with
no way to answer anything.

| Phase | Work | Reused | New |
| --- | --- | --- | --- |
| **1** | ✅ Shared pending-confirmations provider: one poller, count, list, faster while in flight | Endpoint, poll logic | Provider |
| **2** | ✅ Bell in the top bar + full-screen list reusing `EntityConfirmModal`'s sections | Sections, answer contract | Badge, shell |
| **3** | ✅ **DONE 2026-08-05** — popup auto-open removed, extraction handoff unwound per §14. Read §14's DONE note before phase 4: it records what the unwind actually cost. | — | Careful deletion |
| **4** | ✅ **DONE 2026-08-05** — **Show questions** in the extraction panel | `awaiting_confirmation` | Button |
| **5** | ✅ **DONE 2026-08-05** — nudge on leaving `/record` with pending questions | — | View-change hook |
| **6** | `/record` UI: collapsible panel, takes accordion, progress bar, explainer | `interview_flow` | UI |
| **7** | Read aloud + record disabled while playing | `tts.py` | Playback, cache |


Phase 8 (Pause) was dropped — see 16.A.

Phases 1–5 are the behavioural change and are independently useful — they close
the existing gap where dismissing the popup loses its questions permanently.
Phases 6–7 are presentation and can land in any order.

## 16. Open decisions

| # | Decision | Recommendation |
| --- | --- | --- |
| 6.A | Progress over all 129 questions, or over reachable ones? | Reachable, recomputed as gates are answered — 129 is discouraging and wrong once gates rule some out |
| 16.A | Pause | **RESOLVED 2026-08-04 — DROPPED.** It existed to interrupt an automatic flow and there is no automatic flow. A button that suspends nothing is another control to explain. If a visible "nothing is listening" state is wanted later, that is a label, not a button. Phase 8 is removed from the build order. |
| 16.B | Does the nudge fire on leaving `/record` only, or on any navigation with pending questions? | `/record` only at first; anywhere risks becoming wallpaper |
| 16.C | Bell auto-open | **RESOLVED 2026-08-04 — YES, once per producer, never again.** Otherwise nobody discovers the mechanism exists. Persisted client-side; a flag that resets on a new device costs one extra open, which is harmless. **Built 2026-08-05 with one qualification: never while a recording is being processed — see §14's DONE note.** |
| 16.D | Read-aloud: pre-generate all 129 per language, or synthesise on demand and cache? | Pre-generate — one cost, no latency, and it fails loudly at build time rather than quietly for a storyteller |

---

# 17. Notifications, not confirmations (2026-08-05)

The bell opened a question. It now opens a **list**, and the list is a general
notification surface that pending confirmations happen to be the only current
occupant of.

## 17.1 The interaction

| Action | Result |
| --- | --- |
| Click the bell | A dropdown beneath it: one row per waiting item, each saying what it is asking and which recording it is about |
| Click a row | That recording's questions, in the popup, exactly as before |
| Answer | Row gone, badge down, popup closed, back to the list — or the dropdown closes if that was the last one |
| **See all** | Full-screen page, same list, roomier |

Going straight into a question made the bell a shortcut to one arbitrary
recording, with no sense of what else was waiting or which recording was about
to be discussed. An item is now chosen rather than served up.

## 17.2 Why the full screen exists when the dropdown does the same job

Because notifications are meant to be a general surface. Today the only item
is a pending confirmation; a second kind must be **a data change, not a
rebuild** — the same bargain `countQuestions` makes with the six question
classes, and for the same reason: every bug that cost real recordings came
from a second place that had to be updated and was not.

So the split is:

- `lib/notifications.ts` — `NotificationItem` carries its own `title`,
  `detail`, `count` and `icon`. A builder turns a source into items;
  `useNotifications` composes the sources. **This is where a second kind is
  added.**
- `NotificationList` — renders items. Knows nothing about recordings,
  segments or questions, and must not learn: it may not switch on `kind` to
  decide how to draw a row. `roomy` is the entire difference between the
  dropdown and the page, so the two cannot drift into showing different
  things.
- `NotificationCenter` — owns the bell, the dropdown, the page and whatever a
  row opens. **The only place in the feature that switches on `kind`**, because
  what opens is the one thing a generic list genuinely cannot derive.

Verified structurally rather than asserted: the three list surfaces contain no
code referencing confirmations, segments or questions, and `kind` is branched
on in exactly one line.

## 17.3 What this changed in the popup

`EntityConfirmModal` no longer chooses a recording. It is handed a
`segmentId`, answers that one, and closes — where it previously took the head
of the pending list and advanced through it. Auto-advancing now contradicts
the list the producer just chose from and takes them somewhere they did not
ask to go. The "N of M recordings" counter went with it: it described a queue
nobody is in any more.

The questions themselves are untouched — same component, same sections, same
`EntityBatchConfirmRequest`, same six classes. Only the way in changed.

`countQuestions` moved to `lib/pendingQuestions.ts` because it now has two
callers: the popup deciding whether it has anything to show, and the row
saying how many things need checking. Those two disagreeing is the same class
of bug as a badge that disagrees with the list it opens, so there is one
function rather than a second implementation to remember.

Correspondingly, `count` is gone from the pending-confirmations provider. The
badge counts NOTIFICATIONS; a count published from one source would be right
today and quietly wrong the moment there is a second.

## 17.4 Phases 4 and 5 (2026-08-05)

**Show questions** sits in the extraction panel's `awaiting_confirmation`
banner — no new field, as predicted. It renders `EntityConfirmModal` directly
rather than routing through the notification layer: this is not a notification
being opened, it is the panel for a known recording opening its own questions,
so no `kind` is involved and the single switch in `NotificationCenter` stays
single.

**This is what closes the loop §14 recorded as lost.** Entities are only
written once answers land, so answering from the panel already showing that
recording is the one place a producer can watch a name they just confirmed
appear in it. The panel refetches quietly on close — a refresh of something
already on screen should not blank it, which `load` would.

Two things the stacking needed, both easy to get wrong:

- The questions screen is a **sibling** of the extraction panel, not a child.
  Nested inside, every click in it bubbles to the panel's backdrop and closes
  the thing underneath — the panel the producer is about to be returned to.
- The extraction panel **ignores Escape while the questions are open**, or one
  keypress dismisses both.

**The nudge** fires on leaving the record view with items waiting, and nowhere
else (16.B). A toast with a "Show me" action, never a modal — the producer has
just finished recording, and interrupting them at the door is how a prompt
becomes something dismissed without reading. One toast id, so bouncing in and
out replaces rather than stacks. Suppressed when the once-ever auto-open is
about to fire: that is the stronger signal, and both at once is two
interruptions for one event.

It counts ITEMS, matching the badge, where §13.4's sketch said "20 questions".
A nudge saying "20 questions" beside a badge saying "3" invites the producer to
work out which is wrong. The per-recording count still shows on every row.

**Not built: the "after a long session" trigger** also mentioned in §13.4. The
build order only listed the leaving-`/record` one, and a duration threshold
nobody has picked is a knob invented rather than needed.

## 17.5 Decision 16.C, again

The once-ever auto-open now opens **the dropdown**, not a question. Opening a
question teaches nothing about where questions live; opening the dropdown
points at the bell it hangs from, which is the thing that has to be learned.
Still suppressed on the record view, for the reason in §14's second note.
