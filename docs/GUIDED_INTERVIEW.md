# The Interview — guided hands-free recording

**Plan written 2026-08-04. Nothing built.**

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
