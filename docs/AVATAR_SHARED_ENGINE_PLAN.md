# Avatar mode on the v2 engine — investigation and plan

**Written 2026-08-15; re-verified the same day at the request's risk
points — sections 1.1, 1.2 and 5a plus the two locked decisions in
section 5 are that pass's product. EXECUTED the same day on branch
`avatar-shared-engine`, steps 1-4 (through the ChatInterface clarify
affordance), suite green; step 5 remains gated on the live avatar-mode
turn, step 6's docs updated for what landed. See PROJECT_STATUS.md.**
The referenced AVATAR_ON_V2_ENGINE_INVESTIGATION.md does not exist; this
is the investigation, done fresh against branch `light-mode` (the current
stack tip). Line numbers are from that tree and will drift; function
names won't.

The goal, restated as the design rule: **one engine, two renderers.**
Everything upstream of "what to say" — retrieval, coreference,
disambiguation, unit selection, the never-invent guarantee — is one code
path for both modes. Only after unit selection do they diverge: v2 cuts
the selected units into a real video; avatar renders the same units as
narrated text (verbatim unit content + bridging transitions), then speaks
it through TTS → MuseTalk.

---

## 1. The seam — it already exists, exactly where it needs to be

**`select_units()` (full_archive_retrieval.py:1527) IS the engine, and it
already ends before any video-specific work.** Its return type,
`UnitSelection` (:1501), carries everything a renderer of either kind
needs:

| field | content | video-specific? |
| --- | --- | --- |
| `selected_units: List[UtteranceUnit]` | the model's picks in stitch order, each with **verbatim `.text`** (built from real word timestamps at ingestion), `segment_id`, global `index`, `start_sec`/`end_sec` | no |
| `clips: List[ExpandedClip]` | the same units merged into playable ranges | **yes — the only video-shaped field** |
| `clarify` | "which אמנון?" question + validated options; mutually exclusive with an answer | no |
| `no_story_text` | the tailored "זה כל מה שיש לי על X" line, subject-validated | no |
| `follow_up` | validated against genuinely-unseen units | no |
| `read_failed` | outage ≠ empty answer | no |

Everything inside `select_units` is renderer-agnostic: the three
concurrent reads (archive bundle, shown units, `_recent_turns` history),
the annotated transcript with disambiguation name-tags, the entity map,
the single archive-read LLM call, clarify-blocks-the-answer, passage
expansion (`_expand_about_passages`), follow-up validation, and the
no-story subject rule. **No refactoring is needed to expose the engine —
`read_and_validate_ranges` (:1490) already exposes it separately for the
eval harness, which is proof the seam holds under a second consumer.**

The video renderer is `assemble_video_clip_response_v2` (:1708): ~70
lines that consume `UnitSelection` and do photo-category lookup, clip
cache, ffmpeg assembly/upload (`video_clip_assembler`), and the
`shown_units` payload for the WS handler to persist. Nothing in it leaks
back into selection.

One genuinely shared post-selection computation to factor rather than
duplicate: the consecutive-unit merging inside `resolve_units_to_clips`
(:1428). Sections 1.1-1.2 below (added by the 2026-08-15 re-verification
pass) pin its exact behavior and the provably-behavior-preserving
extraction.

### 1.1 Behavioral spec of `resolve_units_to_clips` — documented before touching

Inputs: `unit_ids` (the model's stitch order — may contain unknown ids
and duplicates) and `units` (the archive's full unit list). Output:
`List[ExpandedClip]` (a value-comparable dataclass: `raw_segment_id`,
`start_sec`, `end_sec`, `source_chunk_id`). Every case, verified against
the code and the five existing tests (test_full_archive_retrieval.py
:369-403):

1. **Empty input → `[]`.** Unknown id → dropped with one warning log,
   never a clip (`["nope"] → []`).
2. **Duplicate id → only the FIRST occurrence survives**; later
   repetitions are skipped entirely (no clip, no extension) — a repeated
   id would duplicate footage. `["u4","u1","u4"]` → seg-b clip, seg-a
   clip.
3. **Order is the model's stitch order, never re-sorted** —
   `["u4","u1"]` keeps seg-b before seg-a.
4. **Merge rule:** a unit EXTENDS the previously emitted clip iff it has
   the same `segment_id` AND `index == previous-selected-unit.index + 1`
   — adjacency is judged against the previous unit IN SELECTION ORDER,
   strictly forward. A chain u1,u2,u3 is one clip spanning
   `u1.start_sec → u3.end_sec`; intra-run pauses are INCLUDED (they play
   naturally).
5. **Non-consecutive, same segment** (`["u1","u3"]`) → two clips.
   **Reversed adjacency** (`["u2","u1"]`) → two clips (the +1 check is
   directional). ⚠️ No existing test pins the reversed case — the oracle
   battery in 1.2 adds it.
6. **Cross-segment index adjacency** (`["u3","u4"]`, consecutive global
   ints but different recordings) → two clips; merging would splice
   unrelated footage (pinned by its own test).
7. **Clip fields:** `raw_segment_id`/`start_sec` from the run's head
   unit, `end_sec` from its last, `source_chunk_id` the synthetic
   `archive-read:{segment_id}` marker taken from the head and retained
   through extensions.
8. Upstream interaction: `select_units` (:1599) separately computes
   `selected_units` via `dict.fromkeys` — a dedupe consistent with rule
   2, so the renderer-facing unit list and the clip list can never
   disagree about which units participate.

### 1.2 The extraction — shape, and the proof it changes nothing

**Shape (refined, lower-risk than the original sketch):** the split
happens IN PLACE in `full_archive_retrieval.py` — no new module, no
signature change, no import churn:

- `_group_selected_runs(selected: List[UtteranceUnit]) ->
  List[List[UtteranceUnit]]` — the pure grouping (rules 4-6) over
  already-resolved units. This is what the spoken renderer consumes,
  fed straight from `UnitSelection.selected_units`.
- `resolve_units_to_clips` keeps its name, signature, id-resolution
  loop, dedupe, and warning exactly as-is, then maps
  `_group_selected_runs(...)`'s runs to `ExpandedClip`s (rule 7) — a
  ~10-line body over the helper.

**Proof of behavior preservation — the old implementation is the
oracle:** the function is deterministic and LLM-free (its own
docstring's claim), so unit-level equality IS the whole proof — no live
data, no credits, unlike V1's retrieve() diff-reading, because this seam
is pure code. In the SAME commit as the split:

1. The five existing tests pass UNMODIFIED — they are the primary pins.
2. A new oracle test embeds a frozen copy of the pre-extraction function
   body (`_reference_resolve_units_to_clips`) and asserts elementwise
   equality of new vs. reference over: every named case above (including
   the un-pinned reversed-adjacency and `["u1","nope","u2"]`
   interleaving), plus ALL 2- and 3-permutations of the fixture's five
   units generated programmatically (80 sequences, milliseconds to run).
3. `select_units` is not edited at all in this step.

Exhaustive call-site trace (grepped, not assumed):
`resolve_units_to_clips` has exactly ONE production caller —
`select_units:1600` — plus its five direct tests.
`read_and_validate_ranges` and every eval script reach it only THROUGH
`select_units`, so the one-caller proof covers them.

## 2. What v2 has that avatar lacks — and what adoption inherits for free

The avatar's current chain: `websocket._response_producer` (:929) →
`response_assembler.assemble_response` (:158) →
`retrieval_service.retrieve` (:479) → `primary_match`/`expand_graph`,
with `relevance_scorer.score_candidates` filtering and whole-segment
transcripts joined by entity-injected bridge templates.

| capability | v2 implementation | avatar today | inherited automatically? |
| --- | --- | --- | --- |
| Unit-level answers (no mid-sentence cuts, no whole-recording dumps) | `_split_segment_into_units` + selection by unit id | whole `RawSegment.transcript` per match (`_fetch_transcripts`, response_assembler:120) — a narrow question speaks the entire recording | **yes** — `selected_units` replaces whole transcripts |
| Same-name disambiguation + clarify | `_build_name_tags` (:646), `_DISAMBIGUATION_BLOCK`, `clarify` in `UnitSelection`; byte-identical prompt when no names collide | nothing — `_resolve_entity_names` (retrieval_service:252) resolves names but conflates namesakes silently | **engine yes; presentation no** — the renderer must decide how a spoken clarify sounds (§4.3) |
| Coreference / follow-ups ("ומה עוד?") | the history block: `_recent_turns` + per-turn shown units rendered into the one prompt (`_format_history_block`) | a separate `_resolve_coreferences` LLM call (retrieval_service:333) rewriting the question — flash-lite, recorded in CLAUDE.md as "measured weak at exactly the coreference task" | **yes** — and one avatar LLM call disappears with it |
| Shown-unit memory / "another vs any" | `_load_shown_units` reads `shown_units` from assistant `Message.message_metadata` | Redis visited-set (`cache_service.get_visited`), segment-granularity, silently no-ops without Redis | **engine yes; bookkeeping port** — the avatar WS handler must persist `shown_units` metadata exactly as the v2 handler does (§5 step 3). The visited-set loses its last reader (§6) |
| Tailored no-story line | `_no_story_line` (:1618) → `no_story_text` | generic `NO_STORY_FALLBACK` always | **yes** — with one new fact: in avatar mode this line is SPOKEN. Template + archive-validated name is the rule as already practiced (NO_STORY_FALLBACK is spoken today; `no_story_about`'s bank exists for exactly this shape) |
| Follow-up suggestions | validated `follow_up`, rendered as chat text with Yes/No in the clip UI | nothing | **engine yes; presentation no** — ChatInterface has no follow-up contract (§4.3) |
| Never-invent, structurally | unit ids → times derived from stored word timestamps; model never emits text | honored (verbatim transcripts + fixed bridges) but by whole-segment blocks | **yes**, tightened to unit grain |
| Outage ≠ no-story | `read_failed` → `TRANSIENT_FAILURE_FALLBACK` | any exception → `NO_STORY_FALLBACK` (websocket.py:969) — the exact false-statement failure response_assembler.py's own comment documents | **yes** |
| Interview-question anchoring, take grouping, archive cache, passage expansion | all inside the prompt/bundle | absent | **yes** |

Also inherited, stated honestly as a BEHAVIOR CHANGE rather than a
feature: v2's breadth rule ("breadth falls out of the question") and its
accepted run-to-run variance on broad questions. A broad question in
avatar mode will select many units and produce a long spoken answer —
TTS + MuseTalk render time scales with it (CPU fallback: 30–90s/sentence;
GPU ~real-time). That is the design working, but the avatar's latency
profile changes shape, and on CPU-only deployments long answers get
expensive. Named here so nobody reads it as a regression later.

## 3. The bridging mechanism — the one genuinely new piece

Nothing produces inter-run transitions today in either mode. v2 doesn't
need them (a cut is visible and honest in video; the jump-cut IS the
transition). Spoken audio has no visual cut, so non-consecutive runs read
as a non-sequitur without connective tissue.

**Where it lives:** a new renderer module, `spoken_answer.py` (working
name), sitting exactly where `assemble_video_clip_response_v2` sits for
video — consuming `UnitSelection`, never reaching into the engine. Shape:

```
render_spoken_answer(selection, group_id) -> SpokenAnswer{text, shown_units, follow_up, ...}
  runs = _group_selected_runs(selection.selected_units)   # shared helper, §1
  parts = [join(run unit texts)]                          # verbatim, in stitch order
  between runs: a bridge phrase                           # the new piece
```

**How it respects never-invent — the recommendation is deterministic
templates, not an LLM call, at least first:** the codebase already has
the exact precedent twice over. `BRIDGE_PHRASE_TEMPLATES`
(response_assembler:65) and `NO_MORE_STORY_ABOUT_TEMPLATES` (:100) both
enforce the same rule: *the phrase never varies with what happened; the
only injected value is a name the archive really holds, validated before
injection.* The bridging bank extends this:

- Between runs from the SAME recording: a continuation phrase
  ("ועוד משהו מאותו סיפור...").
- Between runs from DIFFERENT recordings: a transition phrase, optionally
  naming what connects them — the target segment's entity
  (`entity_store.get_entity_names_for_segments`, one bulk query, the
  same lookup today's avatar bridges use) or its stored `topic_tags`
  (already written at ingestion — reuse-ingestion-outputs applies).
  "אגב, יש לי עוד סיפור על {entity}..." with `{entity}`/`{tag}` validated
  against archive-held values, or the generic variant when nothing
  validated is available.
- Phrase choice is a STABLE function of position (the existing
  `_pick_bridge_phrase` rotation), so evals don't get flaky wording.

**The LLM variant, and why it is deliberately second:** a small call
("here are the run texts in order; write one ≤8-word transition per
boundary") would read more naturally — and "no invented facts" cannot be
verified mechanically against free text. Every prompt-adjacent lever in
this project has needed measured gates (the disambiguation block's
example phrase silently destroyed an unrelated answer; the
backward-passage bullet passed its panel and caused the worst live
defect). A constrained middle exists if templates grate in practice: the
LLM *selects* a template id + one connector value from a whitelist
(entities/tags of the adjacent runs), so its entire output is validated
enum choices — expressive enough to pick the aptest transition,
structurally unable to invent. Recommend: ship templates; record the
constrained-LLM upgrade as the named follow-up, gated on listening to
real answers.

## 4. What TTS/MuseTalk consume — drop-in, verified

**4.1 The contract today:** `_response_producer` computes ONE full string
(`assemble_response`'s return), then chunks it into a sentence queue
paced like a token stream; `_animate_from_queue` (websocket.py:~1010)
pops sentences and runs `tts.synthesize(text, language, voice_wav)` →
`animator.animate` per sentence. The pipeline consumes **plain text and a
language**, nothing else. The full answer is already known up front —
there is no true token streaming to preserve.

**4.2 Under this design:** `_response_producer` swaps one call:
`response_assembler.assemble_response(...)` →
`spoken_answer.render_spoken_answer(await select_units(...))`, returning
the bridged text. Same string-in-a-queue, same TTS, same MuseTalk, same
`language = producer.recording_language`. Drop-in confirmed — the only
consumer-visible difference is better text.

**4.3 The two presentation gaps (renderer work, not engine work):**
`clarify` and `follow_up` have no avatar-mode surface. The engine hands
them over regardless; the renderer/WS/UI must present them:

- **Clarify, spoken:** the model's own clarify question is generated
  prose — speaking it would cross the only-verbatim-or-template line as
  practiced. But its OPTIONS are archive-validated names, so a template
  form is available: "יש לי סיפורים על שניים בשם {name} — למי התכוונת?"
  (same class as `no_story_about`). Send the buttons as chat text via the
  existing avatar WS `message` event; ChatInterface gains option buttons
  (the v2 UI's `chooseClarification` pattern, ported).
- **Follow-up:** chat-text only (never spoken — same rule v2 applies),
  ChatInterface gains the Yes/No affordance or, minimally, phase 1 drops
  follow-ups in avatar mode (the engine field is simply unread — no
  regression vs today, which has none).

## 5. The unification plan — ordered, each step suite-green

**Step 0 — pre-flight.** This lands after the current branch stack
(`remove-v1` → `avatar-dormant` → `family-unified-shell` → `light-mode`)
merges, or stacked on its tip — it touches `websocket.py` and
`retrieval_service.py`, both reshaped by that stack. Baseline suite +
build green. ⚠️ Avatar mode is dormant-by-default now: there is no
avatar-mode producer to live-test against without enabling the mode on a
test account (the activation gate requires a ready avatar).

**Step 1 — factor the shared run-grouping** per 1.2: the in-place
split, the untouched five tests, and the oracle-equality battery, all in
one commit. This is the ONLY edit to working v2 code in the entire build
phase (steps 1-4) — see 5a.

**Step 2 — the spoken renderer.** `app/services/spoken_answer.py`:
`render_spoken_answer(selection, group_id)` per §3, with the bridge bank
+ validated injection + stable rotation. The no-story/read-failed/clarify
branches mirror `assemble_video_clip_response_v2`'s ordering exactly
(clarify before no-story before anything). Unit tests: run joining,
bridge placement only between non-consecutive runs, entity/tag
validation (an unvalidated name never renders), template stability,
verbatim-text invariant (output text == unit texts + bank phrases and
nothing else — assertable mechanically, and the test that keeps
never-invent structural).

**Step 3 — the WS swap.** `_response_producer` calls the engine + spoken
renderer; exception handling routes `read_failed` →
`TRANSIENT_FAILURE_FALLBACK` (fixing the documented outage-as-no-story
bug in avatar mode). The avatar handler persists `shown_units` on the
assistant Message exactly as `_handle_video_clip_question_inner` does —
that single line is what turns on shown-unit memory and history
coherence for avatar turns. Clarify: template-spoken + options as chat
text (§4.3). Dispatch tests updated; a new test pins "avatar turn
persists shown_units".

**Step 4 — frontend affordances.** ChatInterface: clarify option buttons
(port of `chooseClarification`), optionally follow-up Yes/No. Scoped
small; the avatar UI keeps its own layout.

**Step 5 — retire the superseded avatar retrieval.** Only after a live
avatar-mode turn on the new path (credits + enabling the mode):

| symbol | verdict |
| --- | --- |
| `retrieval_service.retrieve`, `primary_match`, `expand_graph`, `RetrievalResult`, `RetrievedSegment` | DELETE — sole consumer was `assemble_response` |
| `_classify_topic`, `_extract_entity_names_from_question`, `_embed_question_for_primary_match`, `_resolve_entity_names`, `_resolve_coreferences` + their prompts | DELETE — the engine's prompt does these jobs; the weak flash-lite coreference call dies here |
| `retrieval_service._recent_turns`, `_render_turn_for_history`, `COREFERENCE_HISTORY_TURNS`, `_parse_json_array` | **KEEP, names frozen** — the engine imports the first three; eval scripts monkeypatch `_recent_turns` by name; `_parse_json_array` still feeds `_extract_entity_names…` today and remains generally used |
| `relevance_scorer` (whole module) | DELETE — `score_candidates`' only consumer is `assemble_response` |
| `response_assembler.assemble_response`, `_fetch_transcripts`, `_entity_names_for`, `_shared_entity_name` | DELETE |
| `NO_STORY_FALLBACK`, `TRANSIENT_FAILURE_FALLBACK`, `no_story_about`, both template banks | **KEEP, IN PLACE — decided (re-verification pass)**: the banks stay in `response_assembler`; `spoken_answer` imports them. Nothing moves, so v2's imports are never disturbed |
| Redis visited-set (`get_visited`/`add_visited`) | loses its LAST READER (`retrieve():487`). **Refined (re-verification pass): DEFERRED out of this plan.** Deleting the v2-side write (full_archive_retrieval:1766) would be the plan's only other edit to working v2 code, for zero functional gain (Redis no-ops without a server). Delete the avatar-side read/write with their modules in this step; leave the v2 write and record the full visited-set removal as its own later cleanup |
| Query-time embedding use | ends entirely (`_embed_question_for_primary_match` was the last). `embeddings.py` and the stored columns STAY — ingestion writes them, and dropping columns is its own decision (do not fold in) |

**Step 6 — docs pass.** CLAUDE.md's mode table ("LLM reply → TTS" becomes
"engine-selected units → bridged text → TTS"); the "v1's coreference
call…next candidates" note resolves; PROJECT_STATUS landing record;
open-decision #6 (avatar mode's future) gets its answer changed — the
mode stops being a second retrieval system and becomes a second renderer,
which materially shrinks the argument for removing it.

**Step 7 — verification.** Full suite; repo grep for every deleted
symbol; the prompt-regression panel is NOT implicated (the engine prompt
is untouched — this plan adds zero prompt edits, which is a deliberate
property: the whole thing lives on the renderer side of the seam).

## 6. Honest walls and frictions — none fatal, four real

1. **Bridge quality is capped by the template bank** until the
   constrained-LLM upgrade — transitions will be serviceable, not
   literary. This is the never-invent tax, paid deliberately.
2. **Spoken clarify/no-story push the only-verbatim-or-template rule to
   its edge.** The rule as practiced already includes fixed templates
   with validated names; a listener will still hear the avatar "say"
   sentences the storyteller never recorded. If that grates on the
   producer, the fallback is clarify-as-chat-text-only with a generic
   spoken line — decide on first live listen, not in this doc.
3. **Long answers get expensive in avatar mode.** v2 breadth × TTS ×
   MuseTalk means a broad question can take minutes to render on CPU.
   Real-time avatar use effectively presumes the GPU deployment
   (deployment.md §6) — true today, more visible after this change.
4. **Avatar mode is dormant**, so every live verification step needs the
   mode deliberately enabled on an account with a ready avatar first.
   The engine swap cannot be smoke-tested from the default path.

## 5a. Complete enumeration of edits to working v2 code (re-verification pass)

Demanded explicitly before approval, and the answer is short:

1. **The 1.2 in-place split** of `resolve_units_to_clips` — required,
   oracle-proven, one file, one function.
2. **Nothing else.** The visited-set write removal was the only other
   candidate and is now deferred (the section-5 table). The bridge banks
   stay where they are. `select_units`, `assemble_video_clip_response_v2`,
   `UnitSelection`, the prompt, `_expand_about_passages`,
   `_no_story_line`, and the WS v2 handler
   (`_handle_video_clip_question_inner`) are all untouched in place.

Everything avatar-side is ADDITIVE: `spoken_answer.py` is a new module
calling `select_units` + `_group_selected_runs` + the existing banks;
the `websocket.py` edits are confined to the avatar path
(`_response_producer`, avatar-turn `shown_units` persistence); the
ChatInterface affordances are additions. Step 5's deletions remove only
avatar-superseded code, under the kept-names rule for the four shared
symbols.

What this plan deliberately does NOT do: touch the engine prompt (zero
prompt-regression exposure), change v2 behavior in any way (one
oracle-proven internal split aside), or revisit avatar mode's on/off
status — it makes the dormant mode cheaper to keep, not more prominent.
