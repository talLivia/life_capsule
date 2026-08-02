# Interview restructure — 16 categories, 129 questions, dynamic gating

**Written:** 2026-08-02 · **Status:** PLAN ONLY, nothing built · **Branch:** `main`

Replaces `interview_questions.json` (5 categories, 12 questions) with the
finalized content in `docs/interview_content_source.json`, adds two kinds of
data-driven gating, and rebuilds `/record`'s question flow as an accordion
panel.

**This is the current priority. The family-tree/timeline work
([FAMILY_TREE_TIMELINE.md](FAMILY_TREE_TIMELINE.md), Phases 1 and 1b done)
is paused until this lands.**

Written to be picked up cold. Everything under "Source verification" and
"Consumer inventory" was measured on 2026-08-02, not assumed.

---

## 1. Source verification ✅

`docs/interview_content_source.json` reads cleanly. **16 categories, 129
questions — counts match `meta` exactly.**

| where the questions live | count |
| --- | --- |
| `categories[].questions` | 110 |
| `relationships.questions_intro` | 4 |
| `relationships.branching.yes_branch.shared_questions` | 9 |
| `relationships.branching.yes_branch.status_options.*` | 6 |
| **total content questions** | **129** ✅ |

Separately, **8 gating prompts** that are *not* part of the 129: 6 category
screening questions, the `relationships` screening question, and its
`status_question`. Whether these count toward the on-screen progress
indicator is §7.1 — the brief says they do.

Gating is internally consistent: exactly 6 categories are `gated: true`, all 6
carry a `screening_question`, and no non-gated category carries one.

---

## 2. Three findings that shape this plan

### 2.1 🚨 Source questions have NO ids — and Phase 1b depends on ids

Every question in the source is a bare string. `raw_segments.question_id`
(Phase 1b, landed 2026-08-02) exists precisely so a recording's life period
survives the question set being edited — and it is keyed on ids the new
content does not have.

**The final JSON must assign a stable `id` to every question**, authored once
and never changed or reused. An id must not be derived from position
(`childhood_01`), because reordering within a category would then silently
re-point historical recordings — the exact failure Phase 1b was built to
prevent, reintroduced through the back door.

### 2.2 🚨 Zero of the 12 existing question ids survive — 16 recordings would lose their category

Checked every existing question against the new content: **0/12 match, by id
or by verbatim text.** The whole question set is new wording.

So the moment `interview_questions.json` is replaced,
`category_for_question_id()` returns `None` for all 16 existing recordings and
they disappear from the timeline that Phase 1b just made work.

**Fix: a `retired` block in the new JSON** mapping each old question id to its
category (and its old text, for the record). `category_for_question_id()`
consults it; `get_questions()` never offers those questions to a producer.
History resolves, nobody is asked a retired question, and it stays data — no
special-casing in code. Details in §4.

Related hazard: the old *question* id `military_service` is also a new
*category* id. Ids must not share a namespace across kinds, or a lookup will
silently resolve the wrong thing. §3.4.

### 2.3 🚨 The source is Hebrew-only; today's schema is bilingual

`interview_questions.json` is `{"he": [...], "en": [...]}` and
`get_questions(language)` falls back to `DEFAULT_LANGUAGE`. The new content
contains **zero Latin characters** — it is Hebrew only.

Replacing the file as-is means an English-recording producer silently gets 129
Hebrew questions. That is a product decision, not a technical one — see §8.1.

---

## 3. The JSON schema

### 3.1 One mechanism, not two

The brief describes category gating and sub-section branching as two patterns.
They are the same operation at different scopes: **ask a prompt, and let the
answer choose which steps run next — possibly none.**

Modelling them separately would mean two code paths, two sets of edge cases,
and a third pattern needing a third. Modelling them once means the
`relationships` structure is not special, and a future category can gate,
branch, or nest without touching code.

### 3.2 A category is a list of STEPS

Two step kinds:

```jsonc
// record an answer
{ "kind": "question", "id": "childhood_birthplace", "text": "ספר לי מתי ואיפה נולדת?" }

// ask a prompt, branch on the answer
{
  "kind": "gate",
  "id": "gate_military_served",
  "text": "האם שירתת בצבא, בארגון או במחתרת?",
  "options": [
    { "value": "yes", "label": "כן", "steps": [ /* …steps… */ ] },
    { "value": "no",  "label": "לא", "steps": [] }
  ]
}
```

`options` is a **list**, never a yes/no boolean. That single choice is what
makes the 3-way status question the same shape as a 2-way screening question,
and lets a 4-way one be added later with no code change. `"steps": []` is a
valid, meaningful outcome: the branch ends, nothing is recorded, the category
is complete.

Nesting is by construction — a gate's `steps` may contain further gates.

### 3.3 Both required patterns, expressed

**Category gating** (`holocaust`, `military_service`, `aliyah`,
`academic_studies`, `retirement`, `assisted_living`) — the whole category
body sits inside the gate's `yes` branch:

```jsonc
{
  "id": "military_service",
  "name": "שירות צבאי",
  "steps": [
    { "kind": "gate", "id": "gate_military_served",
      "text": "האם שירתת בצבא, בארגון או במחתרת?",
      "options": [
        { "value": "yes", "label": "כן", "steps": [ /* the 8 questions */ ] },
        { "value": "no",  "label": "לא", "steps": [] }
      ] }
  ]
}
```

**Relationships** — intro questions outside the gate, then a nested gate:

```jsonc
{
  "id": "relationships",
  "name": "זוגיות",
  "steps": [
    { "kind": "question", "id": "rel_courtship", "text": "…" },   // 4 intro
    { "kind": "question", "id": "rel_first_love", "text": "…" },
    { "kind": "question", "id": "rel_early_partners", "text": "…" },
    { "kind": "question", "id": "rel_heartbreak", "text": "…" },
    { "kind": "gate", "id": "gate_rel_significant",
      "text": "האם הייתה לך זוגיות משמעותית בחייך?",
      "options": [
        { "value": "no", "label": "לא", "steps": [] },
        { "value": "yes", "label": "כן", "steps": [
            /* the 9 shared questions */
            { "kind": "gate", "id": "gate_rel_status",
              "text": "מה מתאר בצורה הכי טובה את המצב היום?",
              "options": [
                { "value": "together",          "label": "…", "steps": [] },
                { "value": "widowed",           "label": "…", "steps": [ /* 3 */ ] },
                { "value": "separated_divorced","label": "…", "steps": [ /* 3 */ ] }
              ] }
        ] }
      ] }
  ]
}
```

Note `together` legitimately has **zero** follow-ups: the category simply ends.
That is why an empty `steps` list has to be a first-class outcome rather than
an error.

### 3.4 Envelope, ids and retired questions

```jsonc
{
  "schema_version": 2,
  "languages": {
    "he": { "categories": [ /* … */ ] }
  },
  "retired": [
    { "id": "childhood_home", "category": "childhood",
      "text": "ספר לי על הבית שבו גדלת…" }
    // …the other 11
  ]
}
```

- **`schema_version`** so a loader can refuse a file it does not understand,
  rather than half-reading it.
- **Id namespaces must not collide.** Category ids, question ids and gate ids
  are distinct kinds; the old question id `military_service` is a new category
  id. Proposal: gates are prefixed `gate_`, and question ids are prefixed with
  their category (`rel_`, `childhood_`). Validated by the linter in §6.1 —
  uniqueness across ALL kinds, checked in CI, not by convention alone.
- **`retired`** is category-lookup-only (§2.2). Never returned by
  `get_questions()`.

---

## 4. Migrating the existing 16 recordings

They carry old ids that do not exist in the new set (§2.2).

1. Ship the 12 old ids in `retired` with their categories.
2. `category_for_question_id()` checks live questions first, then `retired`.
3. Their old categories (`childhood`, `military_service`, `post_military`,
   `relationships`, `career`) must still resolve for display. Four of the five
   exist in the new set by the same id; **`post_military` does not.** Either
   keep it as a retired-only category label, or remap those 4 recordings to a
   new category. **Open — §8.2.**
4. No data migration on `raw_segments`: the ids stay as recorded. This is
   deliberate — rewriting historical rows to point at questions the producer
   was never asked would be a lie in the archive.

Verify after: all 16 recordings still resolve to a category, and the timeline
grouping from FAMILY_TREE_TIMELINE §3 returns the same five buckets it does
today.

---

## 5. Consumer inventory — everywhere that assumes a flat, ungated list

Every one of these was found by grep on 2026-08-02.

### Backend

| Location | Assumption | Needs |
| --- | --- | --- |
| `interview_config.get_questions()` | flat `List[question]` per language | walk the step tree; return the resolvable set |
| `interview_config.get_categories()` | groups a flat list by `category` field | read `categories[]` directly; steps replace the grouping |
| `interview_config._by_id/_by_text` | index over a flat list | recurse into `steps`, include gates, include `retired` |
| `interview_config.category_for_question_id()` | flat lookup | + `retired` fallback |
| `interview_config.is_valid_question_id()` | flat lookup | must also accept gate ids, or gain a sibling for them |
| `interview.py:_questions_with_index()` | `enumerate()` → positional index | positions are no longer linear; see §7.2 |
| `interview.py:list_questions()` | returns the whole flat list | must return the tree, or the resolved path |
| `interview.py:update_interview_session()` | `max_index = len(get_questions())-1` | bound is path-dependent, not a length |
| `interview.py` ingest | resolves `question_id` from text | unchanged in shape; text index must cover the tree |
| `schemas.InterviewQuestion` | `{index, id, category, category_label, text}` | needs step kind, gate options |
| `schemas.InterviewSessionUpdate` | `current_question_index: int` | a flat int cannot address a step in a tree |
| `models.InterviewSession.current_question_index` | flat int | replace with a position (category id + step id) |
| `models.RawSegment.question_index` | positional | keep (take-grouping still uses it), but it stops being a flow pointer |
| `full_archive_retrieval.py` (4 sites) | groups takes by `question_index` | review only — grouping still works, but indices become sparse under gating |

### Frontend

| Location | Assumption |
| --- | --- |
| `RecordPanel.tsx:32` | `currentIndex` is a single flat cursor |
| `RecordPanel.tsx:84-86,98` | `questions.length` is the total; `countAnswered >= length` means done |
| `RecordPanel.tsx:93,151` | segments filtered by `question_index === currentIndex` |
| `RecordPanel.tsx:148,153-154` | `total`, `isLast`, `isFirst` from a flat array |
| `RecordPanel.tsx:200,209` | "Question X of {total}" and progress bar span ALL questions — must become per-category (§7.1) |
| `RecordPanel.tsx:224` | renders `question.category_label` |
| `RecordPanel.tsx:271,279` | `goTo(currentIndex ± 1)` — forward nav must go |
| `types.ts:InterviewQuestion` | flat shape |
| `api.ts:updateInterviewSession` | sends `current_question_index` |
| `VideoRecorder`/`SegmentUpload` | `questionIndex` + `questionId` props (Phase 1b) — keep `questionId`, `questionIndex` becomes advisory |

**Nothing hardcodes a category name or count** — that property from Phase 1b
holds and must be preserved. The new gating must not break it: no code may
name a category, a gate, or an option value.

---

## 6. Storage for gate answers

A gate answer is not a recording, and it cannot be inferred: "skipped because
the producer said no" and "not reached yet" look identical from the segments
table. It must be stored explicitly.

**Proposal — a new table:**

```
interview_gate_answers(
  id, interview_session_id FK CASCADE, gate_id, value, created_at,
  UNIQUE (interview_session_id, gate_id)
)
```

Not a JSON blob on `InterviewSession`: the accordion needs to query "is this
category complete" per category, changing one answer must not rewrite the
others, and re-answering a gate (the producer goes Back and changes it) is a
plain upsert.

**Re-answering a gate invalidates the steps below it.** Changing "yes" to "no"
on `gate_military_served` leaves 8 answered questions belonging to a branch
that is no longer taken. The recordings must NOT be deleted — they are real
footage. Proposal: they stay, the category shows as complete-by-skip, and the
orphaned recordings remain visible in the review UI. **Open — §8.3.**

### 6.1 A schema linter, run in CI

The whole design rests on the JSON being well-formed, and it is now big enough
(16 categories, 129 questions, nested gates) that a typo is likely and would
surface as a broken flow rather than an error. A validator should assert:
every id unique across all kinds; every gate has ≥2 options; every option has
a `steps` list; no question text is empty; `retired` ids do not collide with
live ids; and the total question count matches a declared `meta` figure.

---

## 7. The `/record` accordion panel

Right-side panel, category by category, replacing the current linear flow.
Uses the existing dark design system (`btn-primary`, `glass-card`,
`card-glow`) — no new visual patterns.

### 7.1 Progress is per-category

"Question 2 of 9" counts the **current category's own steps**, including
gating questions, never a running total across 129.

Under branching the denominator is not known until the gate is answered — the
`relationships` category is 5 steps, or 15, or 18, depending on answers.
**Open — §8.4.**

### 7.2 Position is a path, not an index

The flow cursor becomes `(category_id, step_id)`. `current_question_index`
cannot address a step inside a branch, and any attempt to keep it will produce
a subtly wrong resume-after-refresh.

### 7.3 Components

| Component | Responsibility |
| --- | --- |
| `InterviewAccordion` | the category list; exactly one expanded; completed collapsed but re-openable; unreached inert (not merely disabled-looking — no click handler at all) |
| `CategorySection` | one category's steps; per-category progress; Back control |
| `GateStep` | a gate rendered as a distinct simple prompt — one button per `option`, driven by the data, never a hardcoded Yes/No pair |
| `QuestionStep` | wraps the existing `VideoRecorder` / `SegmentUpload`; unchanged recording behaviour |
| `useInterviewFlow` | resolves the step tree + gate answers + recordings into: current position, what is complete, what is reachable, what Back does |

Auto-advance on finishing a recording, auto-collapse a finished category,
auto-expand the next — all consequences of `useInterviewFlow` recomputing
position, not separate imperative steps.

**Back** moves within the current or any completed category. **Forward past
the current position is not reachable from the UI** — and must be enforced in
the hook, not only by hiding a button.

---

## 7A. Free navigation — a Settings toggle (CONFIRMED IN SCOPE 2026-08-02)

The accordion is **locked and sequential by default** (§7.3). A new checkbox
in the Settings screen lets the producer turn that lock off.

With it **on** (the default), unreached categories are inert. With it **off**,
**every category becomes clickable and openable regardless of progress**, so
the producer can jump straight to any category and record or upload into it
out of order.

This is not a convenience toggle bolted on the side — it is the escape hatch
two other decisions depend on. It is how the 4 `post_military` recordings get
rehomed (§8.2) and how a producer adds content to a category they previously
screened out (§8.3). Both of those decisions are "no code, use free
navigation", so this must ship with the accordion, not after it.

**Shape**, mirroring the existing `chat_mode` producer setting:

- `User.free_navigation` — boolean, `default=False`, `server_default=false`,
  in migration `0014` alongside the gate-answers table. Off by default keeps
  the guided experience intact for a new producer.
- Updated through the existing `api.updateProfile({...})` path in
  `SettingsPanel.tsx` — same pattern as the chat-mode control, no new
  endpoint.
- The accordion reads it and drops the reachability restriction. **Enforced in
  `useInterviewFlow`, not by hiding a click handler** — the same rule as
  forward navigation in §7.3, for the same reason.

Note what it does NOT change: auto-advance, auto-collapse and per-category
progress all behave the same. It only decides whether a category can be
opened out of order.

---

## 8. Decisions — settled 2026-08-02

**8.1 English.** The source is Hebrew-only. Options: (a) Hebrew-only for now
and block/hide English recording; (b) ship `en` as a copy of the Hebrew and
translate later; (c) translate all 129 before launch. This decides whether the
`languages` envelope in §3.4 carries one key or two. *Recommend (a) — an
English producer silently receiving Hebrew questions is the worst outcome, and
the schema already supports adding `en` later with no code change.*

**8.2 `post_military` — RESOLVED: no mapping code at all.** The 4 orphaned
recordings will be rehomed **manually by the producer** using free navigation
(§7A) — opening whichever new category they choose and recording or uploading
into it. Do not build an automatic `post_military → career` remap, and do not
prompt for one.

What this still requires: `post_military` stays in the `retired` block as a
category label so those 4 recordings keep resolving to *something* until the
producer moves them. A recording whose category cannot resolve would vanish
from the timeline silently, which is the one outcome to avoid. The retired
label is a holding position, not a destination.

**8.3 Re-answering a screening "no" — RESOLVED: no gate-invalidation logic.**
Answering "no" leaves the category greyed out and collapsed. Nothing is
deleted, nothing is permanently blocked. To add content there later the
producer enables free navigation (§7A), opens the category and records or
uploads into it like any other.

The orphan problem §6 worried about **does not exist for this path**: a "no"
means nothing was ever recorded behind it, so there is nothing to strand. Do
not build re-answer handling, branch invalidation, or an orphan-review UI.

One case this scoping leaves open, recorded so nobody assumes it was handled:
answering "yes", recording several answers, then navigating Back and switching
to "no" *would* leave recordings under an untaken branch. The governing rule
covers it adequately — **recordings are never deleted** — so those stay on
disk and in the archive. If that path should be prevented outright in the UI
rather than merely tolerated, say so; it is not built either way.

**8.4 Progress denominator under branching.** Options: (a) count the resolved
path so far, so the total grows when a gate opens a branch; (b) show the
maximum possible; (c) drop the "of N" and show a bar. *Recommend (a) —
honest, and the growth happens right at the moment the producer answered the
question that caused it.*

**8.5 `aliyah` — RESOLVED: two independent yes/no questions.** The single
either/or is replaced by:

1. "Did you make aliyah to Israel?" — yes/no
2. "Were you born in Israel?" — yes/no

They are **not** complements; both "no" is a valid, expected state (born
elsewhere, never made aliyah).

**Only the aliyah question gates.** Working through all four combinations
shows the birthplace answer never changes whether the 9 aliyah questions
apply:

| made aliyah | born in Israel | category |
| --- | --- | --- |
| yes | no | **runs** — the ordinary case |
| no | yes | skipped — native-born, nothing to tell about immigrating |
| no | no | **skipped** — as you expected; never immigrated, so the questions do not apply |
| yes | yes | **runs** — see below |

So the rule is simply **the category runs iff "made aliyah" is yes**, and
both-no skipping falls straight out of it rather than needing its own case.

Structurally that makes birthplace a **fact-capture gate**: a gate whose
options both carry `steps: []`. The answer is stored, nothing branches. It
needs no new step kind — one mechanism still holds (§3.1) — and the linter's
"≥2 options" rule is satisfied. Ordering in the JSON puts birthplace first,
outside the aliyah gate, so both are always asked exactly once; nesting it
inside would force it to be duplicated in both branches.

**On yes/yes, which you asked me to flag:** it does not block, because it
resolves to "runs" under the rule above without needing a special case. It is
also a real if uncommon situation — someone born in Israel whose family
emigrated and who later formally made aliyah back. Asking is the recoverable
direction: a producer with nothing to say about immigrating can move past the
questions, whereas skipping would hide a chapter they may actually have. **If
you want yes/yes to skip instead, it is a one-line change to the JSON gate and
no code change at all — say so and I will flip it.**

Still needed from you: the exact Hebrew wording for both questions and their
option labels. I have used placeholders in the converter and marked them, and
I will not reword your content myself.

**8.6 Existing in-flight interview sessions.** Any session with a
`current_question_index` pointing into the old 12-question set becomes
meaningless. Reset them to the start of the new set, or leave them and let the
producer re-navigate? Check the live count before deciding.

---

## 9. Build order

1. ✅ **Schema + content conversion — DONE 2026-08-02.**
   `scripts/convert_interview_content.py` → `app/interview_questions_v2.json`
   (16 categories, **129 questions** matching the source exactly, 9 gates, 12
   retired). `app/interview_schema.py` validates it; 15 tests, most of which
   feed it broken documents and assert it complains.

   The live `interview_questions.json` is untouched — cutover is step 6, so
   the app keeps working throughout.

   **Ids are frozen.** Numbering is positional at conversion (`childhood_q01`)
   but written literally into the file and never recomputed, so reordering
   later cannot re-point history. Re-running is id-preserving: verified 129
   reused, 0 renumbered.

   Two things the linter caught that are worth knowing:
   - The predicted collision is real — the outgoing question id
     `military_service` is also an incoming *category* id. Downgraded to a
     warning after reasoning it through: questions and categories live in
     different indexes and never compete, so nothing resolves wrongly. A
     retired id shadowing a live QUESTION or GATE stays an error, because
     `category_for_question_id` searches live-then-retired and would return
     whichever it hit first.
   - **Three prompts carry wording I wrote, not yours** — both aliyah
     questions and the relationships status question (the source gives its
     options as `together`/`widowed`/`separated_divorced` with no Hebrew
     labels). They are flagged `needs_wording_confirmation` in the file and
     surface as warnings on every validation run, so they cannot ship
     unnoticed. **Send the wording and I will swap it in — JSON only.**
2. ✅ **`interview_config` extension — DONE 2026-08-03.** Step-tree walking,
   gate lookup, retired fallback. Invisible to the running app: it still loads
   v1 and serves the same 5 categories, 12 questions and English set as before.

   **One internal shape, not two code paths.** A v1 file is *adapted* into the
   v2 shape at load time (each question becomes a `question` step, categories
   from the `category` field), so every function below works on one
   representation and the step-6 cutover is a file swap with no code change
   and no second path left to delete.

   New primitives, all category-agnostic:
   - `resolve_steps(steps, answers)` — the flow primitive. A gate is always
     included (it must be asked) but its branch unfolds only once answered, so
     an unanswered gate **terminates** the list: nothing behind it is knowable.
   - `resolve_questions` — just what gets recorded.
   - `category_is_settled` — no reachable gate is still open. Distinct from
     "complete": a settled category may still have questions to record, but
     its SHAPE is known, which is what the progress denominator (§8.4) needs.
   - `get_gate` / `is_valid_gate_id` / `gate_option_values` — so the API can
     validate an answer without naming an option anywhere in code.
   - `cache_clear()` — one helper for every memoised view, so a test swapping
     the catalog cannot silently keep testing the previous question set.

   **A real bug the tests caught:** `resolve_steps` treated an answer naming a
   no-longer-existent option as unanswered, but `category_is_settled` only
   checked the key was present — so a stale answer resolved to zero questions
   while still reporting the category finished, silently marking a category
   done that had never been asked. Both now share `_chosen_option`, which is
   the fix that keeps them from drifting again.

   23 new tests, run against the REAL v2 file rather than a toy, enumerating
   every gating combination: all four aliyah cases, `together`'s empty branch
   settling the category, an unanswered nested gate, stale answers, and gate
   ids being rejected as question ids.
3. **Gate-answer persistence + the free-navigation flag.** Migration `0014`:
   the table in §6 and `User.free_navigation` (§7A) together, since both are
   prerequisites of the accordion. No re-answer or invalidation logic (§8.3).
4. **Flow API.** Replace `current_question_index` with the path cursor;
   endpoints for resolving the current position and recording a gate answer.
5. **The accordion panel + the Settings checkbox.** §7 and §7A, on top of a
   backend that already answers "where am I, what is complete, what is
   reachable". The checkbox ships WITH the accordion, not after it — §8.2 and
   §8.3 both resolve to "the producer uses free navigation", so the accordion
   is incomplete without it.
6. **Cutover + verification.** Replace `interview_questions.json`, confirm all
   16 existing recordings still resolve to a category and the timeline
   grouping is unchanged.

Phases 1–4 are invisible to the producer; the flow keeps working on the old
content until step 6.

## 10. Verification for every phase

- `python -m pytest -q -m 'not integration'` — currently **566** passing.
- Frontend `tsc`, `eslint`, `next build` clean.
- The Phase 1b guarantee still holds: reorder the question set in a test
  fixture and assert categories are unchanged.
- After cutover: all 16 existing recordings resolve to a category.
