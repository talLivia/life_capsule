# Comprehensive validation plan — every behavior, real transcripts (2026-08-19)

Written before any prompt edit, per producer instruction: inventory every
behavior needing regression protection, construct test cases from the REAL
archive, name the content gaps that need new recordings, and place the
core-vs-offer change as one item inside this picture — not a quick fix.
**Plan only; nothing here is built or run yet.**

## 0. What exists today — the honest starting point

The protection story is stronger than the "7 scored questions" shorthand
suggests. Live-model harnesses (all under `backend/scripts/`):

* **`prompt_regression.py`** — 14 cases against the live archive, diffed
  unit-by-unit against `prompt_regression_baseline.json`: narrow questions
  (`tzvi`, `ilana`, `brothers`, `army-narrow`, `school`), broad (`family`,
  `army-broad`, `about-a-person` — all marked MARGINAL with the reason
  each earned it), no-story (`montreal`, `no-answer`, `influence-1`),
  coreference follow-up (`influence-2`), and two STATE-BEARING multi-turn
  replays of live bugs (`uncle-then-more` — correct 5/5; and
  `uncle-then-more-exhausted` — a pinned KNOWN GAP, baseline = the bug).
  It hard-fails on outages (read_failed ≠ empty result) and refuses to run
  across a changed archive fingerprint.
* **`eval_name_disambiguation.py`** — ON/OFF arms for same-name clarify.
* **`eval_no_story_subject.py`** — does the tailored no-story line name
  the right subject, measured harder on the negative case.
* **`rebaseline_accuracy.py`** — the 7-question IoU accuracy mean.

Deterministic pytest suites (no live model, already green at 865): the
spoken renderer's verbatim invariant, the clip assembler's frozen oracle,
the pending-prompt fast path + classifier orchestration (LLM mocked),
voice-capture logic, WS handlers both modes.

**The live archive** (16 ready recordings, 28 entities, producer
79820a49): parents (2 recordings), roots/מרוקו-עיראק, parents' meeting,
birth/Tiberias/childhood home/first memory (4), grandmothers' food +
name-origin (2, both mentioning סבתא יוכבד; the food one also ג'ולי),
languages, nickname question with TWO TAKES (the 08-07 take: siblings +
ניר's kids + uncle אמנון + בר/דור; the 08-10 take: רחל/אילן + ניצן/יובל
— the real multi-take grouping case, and the source of the broad-family
digression), hobbies, army (friend אמנון), studies (same friend). Two
same-named people: uncle אמנון vs friend אמנון נחום — the live-tested
disambiguation pair.

## 1. Behavior inventory → protection → real-archive case → gap

| # | Behavior | Protected today by | Real-archive case (proposed) | Gap |
|---|----------|--------------------|------------------------------|-----|
| 1 | Narrow-question discrimination | panel: tzvi/ilana/brothers/army-narrow/school | unchanged — these ARE real | none |
| 2 | Broad-question breadth | panel: family, army-broad (MARGINAL) | ADD `childhood-broad` ("ספר לי על הילדות שלך") — 4 real childhood recordings make it the third broad domain | thin: only 3 broad domains exist (family, army is 1 recording, childhood); career/relationships have zero material → **gap A** |
| 3 | **Core-vs-offer split (the proposed change)** | nothing — behavior doesn't exist yet | family: core = parents recordings (u4-10, u23-25) + sibling names, offer = extended-family digression; childhood-broad: annotated core; conservation metric §3 | needs producer core-annotations per broad case; needs ≥1 more domain with a separable side-branch → **gap B** |
| 4 | Same-name disambiguation (clarify fires) | eval_name_disambiguation + panel `clarified` counters | unchanged (אמנון pair) | only ONE pair in the archive — generalization untested → **gap C** |
| 5 | Clarify-resolved answer | panel: about-a-person (live-bug repro 4/4) | unchanged | none |
| 6 | Coreference via history (pronoun follow-up) | panel: influence-2, uncle-then-more | ADD "ספר לי על אירה" → "ספרי לי עוד עליה" (brother's wife; 1 unit — thin but real) | the classic spouse-pronoun case impossible: zero relationship/spouse recordings → **gap D** |
| 7 | Already-shown tie-break + exhausted no-story | uncle-then-more (5/5) + uncle-then-more-exhausted (pinned gap) | unchanged — but see §4 fixture-invalidation constraint | none (the exhausted case stays a pinned known gap by choice) |
| 8 | No-story honesty (absent topic) | panel: montreal/no-answer/influence-1 + eval_no_story_subject | unchanged; pets/career remain genuinely absent topics | none |
| 9 | Follow-up offer generation + validity | **NOTHING live** (only unit tests of `_validate_follow_up` with mocked LLM) | assert per panel case: follow_up presence pattern, offered unit_ids ⊆ archive ∧ ⊄ shown; family → roots offer is the observed live behavior to pin | none content-wise — pure harness work |
| 10 | Follow-up acceptance (two-hop) | nothing live | state-bearing case: family answered → accept "roots" offer → roots units selected (mirrors uncle-fixture mechanics) | chains deeper than 2 hops untestable: roots is 1 recording → **gap E** |
| 11 | Prompt-reply classifier (live labels) | **NOTHING live** (unit tests mock the LLM) | new mini-panel §3.2: real offers ("רוצה לשמוע על השורשים…?") × fixed utterances incl. the live "כאן" mishearing → expected labels | none — needs no archive content |
| 12 | Multi-take grouping (take N of M) | ingestion tests; implicitly panel `family` (both nickname takes selected) | keep an explicit assertion: family selection spans BOTH takes of the nickname question | none |
| 13 | Verbatim/never-invent rendering | pytest (mechanical residue tests), both renderers | n/a — deterministic, stays in pytest | none |
| 14 | Voice capture / STT chain | pytest (logic) + live testing | manual live checklist §5 (not LLM-panel material) | none |

## 2. Content gaps — what to record to close them

Each gap names exactly what's missing and the minimal recording that fixes
it. **All five are append-only additions — see §4 before recording.**

* **Gap A — a third/fourth broad domain.** Record the career category
  (2-3 questions: jobs held, one workplace story) — currently ZERO career
  material, so broad-question and core-vs-offer generalization rests on
  family+childhood alone.
* **Gap B — a separable side-branch outside family.** When recording
  career (gap A), deliberately include one digression (e.g., inside the
  jobs answer, a side story about a colleague or a different period) — the
  career analogue of the nickname recording's family enumeration. This is
  what lets the core-vs-offer panel prove the mechanism generalizes.
* **Gap C — a second same-name pair.** Mention a second person sharing an
  existing name in a NEW recording (e.g., a second רחל or a second יובל,
  in a different context: "חבר שלי יובל מהעבודה"). One sentence suffices;
  disambiguation then has two pairs to be tested against.
* **Gap D — spouse/relationship material.** Record relationships_q-style
  content (how you met, about her) — enables the classic "ספר לי עליה"
  pronoun case that CLAUDE.md's old measurements reference but THIS
  archive cannot currently express.
* **Gap E — depth behind an offer.** Record one more roots/grandparents
  detail recording (e.g., a story about the Iraqi grandfather) so a
  follow-up chain of length ≥2 exists to test (family → roots → the
  grandfather story).

Total new material: ~5-7 short recordings, all in existing categories,
all answering real interview questions through /record as usual.

## 3. The measurement design

### 3.1 prompt_regression.py — EXPANDED, not replaced

Same file, same baseline mechanism, same MARGINAL discipline. Additions:

* New cases: `childhood-broad`, `ira-pronoun` (thin until gap D), and —
  after gap recordings — `career-broad`, `spouse-pronoun`,
  `second-pair-clarify`, `two-hop-roots` (state-bearing, built like the
  uncle fixtures).
* **Follow-up assertions on every case** (new comparison fields, saved in
  the baseline): did a follow_up appear (yes/no/either), and are its
  unit_ids real, un-shown units. Baselines then catch "offers stopped
  appearing" and "offers point at shown/invented units" — behavior #9,
  currently unprotected.
* **Core-vs-offer conservation** (activates WITH the prompt change, for
  the annotated broad cases): `selected ∪ follow_up.unit_ids` must equal
  the pre-change selection (± the documented 1-2-unit marginal variance),
  no contiguous run split across the boundary, and the core must contain
  the producer-annotated minimum. The annotation — which units are "core"
  for each broad question — is the producer's product judgment, recorded
  once as data. Narrow cases must be byte-stable throughout: the
  discrimination property stays the first thing checked.
* Runs: 5 per case for anything MARGINAL or new, 3 elsewhere (current
  default), before AND after any prompt edit.

### 3.2 New: `eval_prompt_reply.py` — the classifier's live panel

`_classify_prompt_reply` has zero live-model coverage. Small fixed panel,
separate baseline (different prompt, different failure modes): real
archive offers × ~15 utterances with expected labels — accepts ("כן,
תספר לי", "אה בטח, למה לא", "ספר"), declines ("לא בא לי כרגע", "אולי
אחר כך, תודה"), unrelateds ("לא, תספר לי על הצבא", "לא סיפרת לי על
הבית", "מה השעה?", and the live-observed STT mishearing "כאן" — pinned
as `unrelated`, documenting that fail-open cost honestly). Temperature 0,
5 runs, any label flip = finding.

### 3.3 Untouched

`eval_name_disambiguation.py`, `eval_no_story_subject.py`,
`rebaseline_accuracy.py`, and every deterministic pytest suite stay
exactly as they are.

## 4. ⚠️ The ordering constraint that makes or breaks this plan

Unit ids are positional across the whole archive, and the harness refuses
to compare across a changed fingerprint — this is a feature (it is how
seed_sweep's references died and were caught). ⚠️ CORRECTED 2026-08-20
during execution: the original claim here that "new recordings sort AFTER
all existing ones" was WRONG — archive order is (question_index,
created_at), so the gap recordings inserted MID-archive (career at
u23-35, spouse at u39-44) and shifted every unit id after their insertion
points. The constraint is therefore STRONGER than first written: ANY new
recording invalidates every stored unit-id reference, wherever it lands.
The uncle fixtures pin an explicit `archive_version` (which caught
exactly this). Therefore the sequence is fixed:

1. **Record all gap content first** (producer, §2), ingest, confirm.
2. **Verify existing unit ids unchanged** (append-only check), update the
   fixtures' recorded archive_version, extend the harness (§3.1 cases +
   follow-up assertions), then `--save` a fresh baseline and prove its
   stability (5 runs, drift only inside MARGINAL).
3. **Baseline the classifier panel** (§3.2).
4. **Only then** the core-vs-offer prompt edit: one subject-neutral
   paragraph amending the breadth bullet + FOLLOW-UP block; before/after
   runs; the conservation metric (§3.1) is the acceptance test; narrow
   invariance is the veto.
5. Landing report: per-case before/after tables, PROJECT_STATUS record.

Any other order measures the prompt change against baselines the new
recordings would immediately invalidate.

## 5. Manual live checklist (per release, not automatable in the panel)

Voice: natural-pace "כן"/"לא" to an offer in both modes and TalkInterface;
"לא, תספר לי על X" answers X; clarify by spoken name; the "כאן"
mishearing degrades to fresh-question (not a wrong accept). Mic: re-arms
after multi-chunk avatar answers; tail-overlap accepted; no echo capture
during playback. Presenter videos: question plays, skip→record, single
replacement via `--allow-partial`.

## 6. Cost/scope estimate

Harness work: extend prompt_regression (~1 session), classifier panel
(small), fixtures/annotations (small). Model cost: a full 5-run expanded
panel ≈ 150-200 archive reads on the pinned flash — pennies, minutes.
Producer work: §2's recordings (~30-60 minutes of recording), plus the
core annotations for broad cases (a one-time judgment per question).
