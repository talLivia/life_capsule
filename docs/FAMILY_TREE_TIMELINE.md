# Family tree + timeline — build plan

**Written:** 2026-08-01 · **Status:** Phases 1 and 1b DONE; **rest PAUSED**
· **Branch:** `main`

> ⏸ **Paused 2026-08-02 for the interview restructure**
> ([INTERVIEW_RESTRUCTURE.md](INTERVIEW_RESTRUCTURE.md)), which is now the
> priority. That work replaces `interview_questions.json` with 16 categories
> and 129 questions and adds data-driven gating — so **§2A and §3 of this
> document (the category source and the milestone→people join) will need
> re-checking against the new schema before Phase 5 is built.** Phases 1 and
> 1b are unaffected and stay done. Note also that the restructure retires
> every current question id; the plan there carries the migration that keeps
> these 16 recordings resolvable to a category.

Two read-only producer-facing pages: a **family tree** built from person
entities and family relations, and a **timeline** of life milestones that
expands into the people, places and moments from each period.

This document is written to be picked up cold. Everything in "What exists
today" was measured against the live database on 2026-08-01, not assumed —
re-measure before trusting it if much time has passed. Standing architecture
rules live in [CLAUDE.md](../CLAUDE.md); the entity schema's reasoning lives in
[PROJECT_STATUS.md](PROJECT_STATUS.md)'s "NEXT UP" section and should be read
before touching anything entity-shaped.

**🛑 Section 3 is an open decision that changes the shape of half this plan.
Get an answer before starting Phase 3 or later.**

---

## 1. What exists today (measured 2026-08-01)

### Schema — all present, migration `0012` applied

| Table | State |
| --- | --- |
| `entities` | 19 rows. `type` ∈ person/place/organisation/event/other, `year_start`/`year_end`, `is_self`, unique on `(producer_id, normalized_name)` |
| `entity_mentions` | one row per (entity, recording), carries the per-recording `summary`, cascades from `raw_segments` |
| `relation_types` | 20 seeded. **6 tree-bearing** (parent, child, sibling, spouse, grandparent, grandchild). Holds `is_symmetric`, `inverse_type`, `category`, `label_en`, `label_he` |
| `entity_relations` | **0 rows.** `from_entity_id`, `to_entity_id`, `relation_type` (FK), `source_segment_id` (FK, cascades), unique on all four |

### The actual data

```
entities by type:  person 13   place 4   organisation 2   event 0
                   is_self 5   with year_start 0
entity_relations:  0 rows
producers 5,  self-entities 5   (migration 0012 backfilled every existing one)
```

Three facts that matter more than they look:

1. **Zero `event` entities exist, and that is not an accident.** The extractor
   is a *named*-entity extractor whose prompt says outright that an unnamed
   school is omitted. "My army service" has no name, so it is correctly not an
   entity. The archive's nearest things are `חיל האוויר` and `הכפר הירוק`,
   both typed `organisation`. **The timeline as specified has no data source.**
   See §3.
2. **Zero relations, zero years.** Nothing writes either. Expected — both were
   explicitly out of scope for the entity migration.
3. **Every existing producer has a self-entity, but signup creates none.**
   Migration 0012 backfilled the 5 that existed. A producer who signs up now
   gets no root, and relations cannot be expressed without one. Phase 1.

### Confirmation flow — the thing both capture paths extend

- `check_entities_node` runs **one** extraction, used twice (disambiguation +
  the write). Do not add a second extraction call; the names the producer
  confirms must be the names that get stored.
- `human_confirm_node` raises **ONE** `interrupt()` per recording carrying
  `{identity_questions, type_questions}`. One screen, one submit.
- `POST /segments/{id}/confirm-entities` resumes it. **A partial submit is a
  400**, deliberately — both plausible defaults are wrong in opposite
  directions.
- Frontend: `components/record/EntityConfirmModal.tsx`, payload typed in
  `lib/types.ts` as `PendingConfirmation`.

### Frontend shell

`app/page.tsx` holds a `View` union and a `navItems` array; each panel is a
lazy `dynamic()` import. There are only three routes (`/`, `/record`,
`/talk`) — **the tree and timeline are new panels in this shell, not new
routes.** Design system: `btn-primary`, `btn-secondary`, `glass-card`,
`card-glow` for the dark app; the warm `calm-*` theme is **`/talk` only**
(see the note in `globals.css` before reusing it).

Recording playback already exists: `RecordingList.tsx` renders
`<video src={segment.video_url}>`. That is the clip-serving path to reuse.

### The interview question set — load-bearing for §3

`backend/app/interview_questions.json`, bilingual he/en, 12 questions in
**5 categories, already in chronological life order**:

| category | label (en / he) | questions |
| --- | --- | --- |
| `childhood` | Childhood / ילדות | q0–q2 |
| `military_service` | Military Service / שירות צבאי | q3–q4 |
| `post_military` | After the Military / אחרי הצבא | q5–q6 |
| `relationships` | Relationships & Family / זוגיות ומשפחה | q7–q9 |
| `career` | Career / קריירה | q10–q11 |

Every recording carries `question_index`, so **every recording already belongs
to exactly one life period, with zero new capture.**

---

## 2. Decisions — settled 2026-08-02

**2.1 — Option C approved.** Ship the timeline on the existing question
categories; add `event`-entity milestones as an additive second source later.

**2.2 — Phase 2 is family-only.** `friend`/`commander`/`colleague` are stored
fine by the schema and can be widened to later; they are not proposed now.

**2.3 — Two classes of question in one modal.** Identity and type stay
mandatory (partial submit still 400). Relations and years are explicitly
skippable, and must be *visually distinct* so the difference is obvious.

**Self-entity name** — the producer's `full_name` from their `User` row, with
the existing `full_name → username` fallback. No new producer input.

**2.4 remains open** (empty-state tree vs hidden nav item). My recommendation
stands: show it with a real empty state, because a hidden feature is
undiscoverable and the empty state is where you explain that relations come
from recording. Not a blocker for any phase.

The original reasoning for each is preserved below.

### 2.1 What is a timeline milestone? (RESOLVED — Option C)

The brief says milestones are `event` entities with `year_start`. That data
does not exist and is awkward to create: the extractor deliberately refuses
unnamed periods, and "school → high school → army" are periods, not names.

**Option A — question categories as milestones (recommended).** The five
categories above become the five bubbles. Already exists, already ordered,
already bilingual, attached to every recording, needs no capture and no
producer effort. Years become optional decoration rather than the backbone.
- *Against:* the milestone set is fixed at five and cannot express anything
  the interview does not ask about. A producer with a distinctive life chapter
  outside the script cannot add one.

**Option B — `event` entities as specified.** Requires teaching extraction to
emit unnamed life periods (a real change to what the extractor is for), plus
year capture to order them. Fully general; the producer's own chapters appear.
- *Against:* a lot of new capture burden on a producer who just wants to
  record, and it fights the "named entity" rule the extractor is built on.

**Option C — A now, B later.** Ship the timeline on categories; add
`event`-entity milestones as an additive second source once relations and
years have proven themselves in Phase 2. The page merges both sources by year.

**My recommendation: C.** It gets a working timeline with no new capture,
and does not close the door on producer-defined chapters. But this is a
product-shape call about how prescriptive the interview should be, so it is
yours.

**Everything from Phase 3 onward assumes A/C. If you pick B, Phase 3 grows a
whole extraction-and-year-capture stage first and the estimates change.**

### 2.2 How aggressive should relation proposal be?

An LLM reading "I have four brothers: Nir, Chen, Adi, Raz" should clearly
propose four `sibling` relations to the self-entity. Less clear: "my commander
Roni" (a `commander` relation? or just a mention?), "I met my wife on an app"
(`spouse` — but she is never named, so there is no entity to relate to).

**Recommendation:** propose only when BOTH endpoints are already extracted
entities and the text states the relation explicitly. No inference across
recordings, no guessing an unnamed endpoint. A missed relation is recoverable;
a wrong one in a family tree is visible and damaging.

**Your call:** should non-family relations (`friend`, `commander`, `colleague`)
be proposed at all in Phase 2, or is Phase 2 family-only with the rest later?
They are stored fine either way — the question is confirmation-screen noise.

### 2.3 Skippability — what does "skip" mean for a relation?

The constraint is that a producer must never be blocked. But
`confirm-entities` currently **rejects a partial submit with a 400**, on the
reasoning that both silent defaults are wrong.

Those two rules collide. The resolution I propose: relation and year questions
are **explicitly skippable, and skipping is a real answer** — a "Skip" control
that records "not answered" rather than an absent key. Identity and type
questions keep their current all-or-nothing rule, because their defaults are
genuinely dangerous; an unanswered relation just is not stored, which is the
status quo and harmless.

**Confirm this is the shape you want** — it means the modal has two classes of
question with different submit rules, which needs to be obvious in the UI.

### 2.4 Do we need a "no relations yet" tree, or no tree at all?

With 0 relations today, the tree page renders one node: the producer. Is that
the right empty state (a single node plus an explanation), or should the nav
item be hidden until at least one relation exists? I lean toward showing it
with a clear empty state — a hidden feature is undiscoverable, and the empty
state is where you explain that relations come from recording.

---

## 2A. The category list is DATA, never code (constraint added 2026-08-02)

The category set is about to change — roughly 5 → 10 categories with many more
questions each. Nothing may hardcode today's categories, their names, their
count, or their `question_index` ranges. After the JSON is updated the timeline
must pick up the new categories **with zero code changes**.

### ✅ Re-checked 2026-08-03 against the 16-category schema

The constraint HELD through the restructure. `interview_config` is still the
only runtime reader of the question file, nothing hardcodes a category name or
count, and the interview went 5 → 16 categories and 12 → 129 questions with no
code change to any of it — which is the property this section demanded.

Two small drifts in the table below, neither affecting the rule:
* `get_categories()` now also returns `steps` (the gating tree), alongside
  `category` / `category_label` / `question_ids`.
* the `category_label` render site moved when `/record` was rebuilt — it is now
  `RecordPanel.tsx` (`openCategory.label`) and `InterviewAccordion.tsx`, not
  `RecordPanel.tsx:224`.

**But §3's queries did NOT survive — see the correction there.**

### Blast radius — grepped 2026-08-02, and it is small

| What | Where | Verdict |
| --- | --- | --- |
| Category name strings (`childhood`, `military_service`, …) | `interview_questions.json` **only** | ✅ nothing in code |
| Category count / list | nowhere | ✅ nothing assumes 5 |
| Question count | `interview.py:117` uses `len(get_questions(...))`; `RecordPanel.tsx` uses `state.questions.length` | ✅ already derived |
| JSON readers | `interview_config.py` **only** (`get_questions`, `lru_cache`) | ✅ single seam |
| `category` / `category_label` in code | `schemas.py` + `types.ts` (field declarations), `RecordPanel.tsx:224` (renders the label) | ✅ pass-through, no logic |

**No existing code hardcodes any of this.** `interview_config.py` is already
the single reader, so it is the natural and only home for category logic.

### The rule for new code

`interview_config.py` gains category accessors derived from the JSON at read
time — e.g. `get_categories(language)` returning ordered
`{category, category_label, question_ids}`. Order comes from first appearance
in the file, so **the JSON's own order is the chronology**. Every consumer
(timeline API, the milestone join, the frontend) reads from that one function.
No constants, no migration, no lookup table, no duplicated list.

### 🚨 But `question_index` is NOT a stable key — and the fix has a deadline

`raw_segments` stores `question_index` (positional) and `question_asked`
(text). It does **not** store the question's stable `id`, which the JSON has
had all along (`childhood_home`, `military_service`, …).

So when the JSON gains questions or reorders, **every existing recording's
`question_index` silently points at a different question.** Today's q3 is
`military_service`; insert three childhood questions ahead of it and q3 becomes
childhood. A timeline joining `question_index → category` would quietly
reassign historical recordings to the wrong milestone — no error, wrong tree.

This is exactly the silent breakage the no-hardcoding rule is meant to prevent,
and it survives that rule, because it is a *data* problem rather than a code
one.

**Fix:** persist the stable `question_id` on `raw_segments` at ingest, and join
category via `question_id`. See Phase 1b.

**⏳ The backfill window closes when the JSON changes.** Verified 2026-08-02:
all 12 distinct questions in the archive recover their `id` by exact
`question_asked` text match, 12/12, with no index drift yet. That recovery
works **only while the JSON still contains today's text**. Reword or remove a
question and its historical recordings become unattributable.

**✅ RESOLVED 2026-08-02 — Phase 1b landed and all 16 recordings were
backfilled with 0 unmatched. `interview_questions.json` is now safe to edit.**
Recordings made from here carry their `question_id` at ingest, so the window
never reopens.

---

## 3. Milestone → sub-bubble linkage (the §"design this" item)

**Chosen mechanism: derive from the recordings, do not store a new relation.**

A milestone is a question category. A category is a set of `question_index`
values. The people, places and organisations "during" that milestone are
exactly the entities mentioned in the recordings answering those questions.
This is a plain join over data that already exists.

Rejected: a new `occurred_during` relation type. It would need a migration
(the `relation_types.category` CHECK allows only family/social/professional/
other), a new extraction concept, and producer confirmation — to store a fact
already derivable from `question_index`. It is the "same fact in two places
and they drift" failure the entity migration exists to undo.

Rejected: inferring from co-mention across recordings. Vaguer, more expensive,
and no more correct.

### The exact queries

**Milestones** (ordered, with counts — drives the bubbles):

```sql
SELECT s.question_id,
       COUNT(DISTINCT s.id) AS recordings,
       MIN(s.created_at)    AS first_recorded
FROM raw_segments s
JOIN interview_sessions i ON i.id = s.interview_session_id
WHERE i.user_id = :producer_id
  AND s.status = 'ready'
GROUP BY s.question_id;
```

Group by category in application code via `interview_config.get_categories()`
(§2A) — **never by a category list held anywhere else**. Ordering comes from
the JSON's own question order, so adding or reordering categories needs no code
change. A category with zero recordings renders as an unfilled bubble; that is
useful, it shows what is still un-recorded.

Note both this and the sub-bubble query key on `question_id`, not
`question_index` — see §2A for why the positional index cannot be trusted
across a question-set edit. Until Phase 1b lands there is no such column.

**Sub-bubbles for one milestone** (entities mentioned during it):

```sql
SELECT e.id, e.name, e.type,
       COUNT(DISTINCT m.raw_segment_id) AS mention_count
FROM entities e
JOIN entity_mentions m ON m.entity_id = e.id
JOIN raw_segments   s ON s.id = m.raw_segment_id
JOIN interview_sessions i ON i.id = s.interview_session_id
WHERE i.user_id     = :producer_id
  AND s.status      = 'ready'
  AND s.question_id = ANY(:question_ids)          -- the category's STABLE ids
  AND NOT e.is_self                               -- the producer is every bubble
GROUP BY e.id, e.name, e.type
ORDER BY mention_count DESC, e.name;
```

**Moments behind a sub-bubble** (and the same query powers the family tree's
person panel — one endpoint, two callers):

```sql
SELECT s.id, s.question_asked, s.question_index, s.video_url,
       s.transcript, m.summary
FROM entity_mentions m
JOIN raw_segments s ON s.id = m.raw_segment_id
WHERE m.entity_id = :entity_id
ORDER BY s.created_at;
```

`video_url` + `transcript` + `question_asked` give the player, the transcript
and the title with no new serving path. **Do not trim clips for this** —
player-side seeking was measured and rejected (see PROJECT_STATUS "Turn
latency"), and per-moment ffmpeg would be far worse. Play the recording.

### 🚨 CORRECTION 2026-08-03 — these queries drop every retired recording

Re-checked against the 16-category schema after the interview restructure
cut over. **The queries above are wrong as written**, and the earlier cutover
verification did not catch it because it used a different code path.

`get_categories()['question_ids']` contains **live ids only**. Every one of the
12 outgoing questions is now retired, so `WHERE s.question_id = ANY(:question_ids)`
matches nothing at all. Measured:

```
PATH A  per-recording category_for_question_id()   (what the cutover check did)
        career 2, childhood 3, military_service 4, post_military 4, relationships 3
        => 16 of 16 recordings placed

PATH B  iterate get_categories(), match its question_ids   (what §3 does)
        => 0 of 16 recordings placed, all 16 categories empty
```

Two distinct causes, both needing a fix in Phase 5:

**1. Retired ids are not in any category's id set.** A recording of a
withdrawn question resolves fine through `category_for_question_id`, but the
timeline never asks that — it asks each category for its questions. Fix:
expose the retired ids per category, e.g. a `retired_question_ids` field
alongside `question_ids`, and have the timeline match on the union. Keeping
them in a SEPARATE field matters: `question_ids` must stay live-only so
nothing can accidentally offer a retired question to a producer.

**2. A retired-only category is never yielded at all.** `post_military` has no
equivalent in the new set, so `get_categories()` does not emit it and its 4
recordings have nowhere to appear — even once cause 1 is fixed. Per §8.2 those
recordings are meant to stay visible until the producer rehomes them, so the
timeline must append retired-only categories after the live ones. They have no
position in the new chronology, which is honest: they are historical.

Sketch, staying data-driven:

```python
buckets = {c["category"]: c for c in interview_config.get_categories(lang)}   # live, ordered
for item in interview_config.get_retired():                                   # historical
    buckets.setdefault(item["category"], {"category": item["category"],
                                          "category_label": item["category"],
                                          "question_ids": [], "retired_only": True})
# per bucket, match on live ids + retired ids carrying that category
```

**Do not "fix" this by putting retired ids into `question_ids`.** That single
field then means two different things depending on the caller, which is the
class of bug the whole `question_id` design exists to avoid.

### These queries were run, not just written

All five (the three above plus the two in Phase 4) executed against the live
database on 2026-08-01 and returned sensible rows. Actual output for the
`military_service` milestone:

```
sub-bubbles (q3,q4):   person איציק כהן  mentions=1
                       organisation חיל האוויר  mentions=1
moments for איציק כהן: q4  video=yes  "מהו הרגע הכי בלתי נשכח..."
                       q5  video=yes  "מה עשית מיד אחרי השחרור מהצבא?"
tree nodes:            9 person nodes, self = Tal Nahum
tree edges:            0  (as expected — nothing writes relations yet)
```

Note what the moments row shows: **איציק כהן spans two milestones** (q4 in
`military_service`, q5 in `post_military`). That is correct and the design
handles it without special-casing — a person is not owned by one period.

---

## 4. Build order

Each phase is independently shippable and leaves the app working.

### Phase 1 — self-entity at signup ⚠️ blocks everything

**Why first:** relations cannot be expressed without a root, and a producer
who signs up today has none. Migration 0012 backfilled the existing 5 and its
own comment says new producers get theirs "at signup (application code)" —
that code does not exist.

- Create the `is_self` entity when a producer account is created, mirroring
  migration 0012's logic exactly.
- **Name = `User.full_name`, falling back to `username`** when it is null or
  blank — the same `COALESCE(NULLIF(TRIM(full_name), ''), username)` the
  migration used, and one existing producer needed it. A display label the
  producer can correct later; what must exist now is the ROW.
- Backfill anyone created between the migration and this landing.
- Test: a fresh producer has exactly one `is_self` entity; the fallback fires
  for a null/blank `full_name`; the partial unique index still holds; the
  orphan sweep still skips it.

**Small, and nothing else can start without it.**

### ✅ Phase 1b — persist the stable `question_id` — DONE 2026-08-02

**Landed, and the deadline is met — `interview_questions.json` is now safe to
edit.** Migration `0013` applied to live Neon (`alembic current` = `0013
(head)`), and all **16 recordings backfilled, 0 left NULL**.

`interview_config` gained the single-source accessors: `get_categories()`
(ordered by first appearance in the file), `category_for_question_id()`,
`question_id_for_text()`, `is_valid_question_id()`. Nothing else holds a
category list.

Verified end to end against live data — the timeline's own grouping, derived
entirely from the JSON at read time:

```
ילדות           3 recording(s)  ->  אילנה, הכפר הירוק, חן, טבריה
שירות צבאי      4 recording(s)  ->  איציק כהן, חיל האוויר, רוני כהן
אחרי הצבא       4 recording(s)  ->  איציק כהן, בוליביה, דרום אמריקה, מונטריאול
זוגיות ומשפחה   3 recording(s)  ->  (none)
קריירה          2 recording(s)  ->  מונטריאול
```

`זוגיות ומשפחה` having recordings but no people is correct, not a bug: the
spouse is never named, and the extractor is a *named*-entity extractor. It is
the documented unnamed-spouse case, and the timeline shows it honestly.

**What the original plan said**, kept for the reasoning:

**Do this before editing `interview_questions.json`** (§2A). Independent of
everything else, and the only phase with an expiring window.

- Migration `0013`: add `raw_segments.question_id` (nullable String, indexed).
  Nullable because uploads outside the guided set have no question id.
- Backfill by exact `question_asked` text match against every language in the
  JSON — verified 12/12 recoverable on 2026-08-02. Report anything unmatched
  rather than guessing; leave those NULL.
- Populate at ingest: `SegmentIngestRequest` gains `question_id`, sent by the
  frontend from the question it just displayed. Validate it against
  `interview_config` so a client cannot invent one.
- Timeline reads category via `question_id`; `question_index` keeps its
  existing job (ordering the record flow, replacing a re-record) and is not
  removed.
- Test: a reordered question set leaves historical rows attributed to the same
  category. That is the whole point of the column, so it needs a direct test.

### ✅ Phase 2 — relation capture — DONE 2026-08-03

Extraction proposes family relations in the SAME call as entities; the batched
confirmation screen gained a third, skippable class; confirmed relations are
written in the same transaction as the entities.

- **Vocabulary comes from `relation_types`**, not a hardcoded list — the table
  is the source (the FK proves it), so adding a type needs no prompt edit.
  Family-only per decision 2.2; widening is passing a different category.
- **Direction is `from` = subject**, tested on the STORED ROW resolved to real
  entities, not on "a parent row exists". An inverted tree renders perfectly.
- **One directed row**, no mirror; inverses derive from `inverse_type`.
- **Re-analysis replaces**, like mentions — a relation must not outlive the
  sentence that established it.

Two things found while building, both of which failed silently:

1. **The entity regex is greedy.** `\[.*\]` spans first bracket to LAST, so
   the moment a second array existed it swallowed both, parsed neither, and
   returned ZERO entities from a good extraction — indistinguishable from a
   recording that mentioned nobody. The parser now splits on the marker first.
2. **`relation_types` is seeded by migration 0012 only.** A database built by
   `Base.metadata.create_all` (a fresh dev DB, or a test fixture) has the table
   but no rows, which turns relation capture off with nothing to show for it.
   `get_relation_vocabulary` now warns rather than returning an empty list
   quietly.

16 new tests (651 total).

### Phase 2 — relation capture (original plan)

**2a. Extraction proposes relations.** Extend `entity_extraction` to return,
alongside entities, a `relations` list of
`{from, to, relation_type, evidence}` where `from`/`to` are names already in
the entity list (or the literal self-marker) and `relation_type` is drawn from
the **seeded vocabulary only** — the FK will reject anything invented, which
is the point. `evidence` is the phrase that supports it, for the confirm UI.

Direction and symmetry (the brief's question): **one directed row, never two.**
- Symmetric types (`sibling`, `spouse`, `cousin`, `friend`) — store one row in
  whichever direction the sentence gave. The reader treats it as undirected;
  `relation_types.is_symmetric` says so.
- Directional types (`parent`/`child`, `grandparent`/`grandchild`) — store one
  row and derive the inverse at read time from `inverse_type`. Never write the
  mirror. Two rows means every edit and delete must keep a pair in sync and
  they will eventually disagree.
- The prompt must therefore fix a convention and state it: **`from` is the
  subject of the sentence.** "Nir is my brother" → `(Nir, sibling, self)`.
  "Tzvi is my father" → `(Tzvi, parent, self)` — Tzvi is the parent OF self.

  🚨 **Direction needs an explicit test, not a passing assertion.** Getting
  `from`/`to` backwards inverts the entire tree and does so *silently* — every
  node still renders, the generations are just upside down, and a reviewer
  glancing at a tree with the right names in it will not notice. The test must
  assert on a case where the two directions are distinguishable: feed "צבי is
  my father" and assert the stored row is `(צבי, parent, self)` AND that the
  rendered tree places צבי in the generation ABOVE the root — not merely that
  a `parent` row exists.

**2b. Confirmation screen shows relations.** Extend the `interrupt()` payload
to `{identity_questions, type_questions, relation_questions}` and the modal to
render a third group, in the same one-screen-one-submit flow. Per §2.3,
relation questions are skippable while identity/type stay mandatory.

⚠️ **The payload shape changed once before and was safe only because zero
segments were mid-flight.** Check `SELECT count(*) FROM raw_segments WHERE
pending_confirmation IS NOT NULL` before landing; drain or shim if non-zero.

**2c. Write confirmed relations.** In `finalize_ingest_node`, alongside the
entity write, insert `entity_relations` rows with `source_segment_id` set. Only
confirmed ones. Re-ingest must replace, not duplicate — the unique constraint
covers `(from, to, type, source_segment)`.

### ✅ Phase 3 — year capture — DONE 2026-08-03

`app/services/year_parsing.py` + a fourth, skippable class on the confirmation
screen. Free text in ("1973", "בערך 1973", "in 1973"); one integer out, or a
refusal with a reason.

**"Refuse rather than guess" is the whole design.** A wrong year silently
reorders a life and nothing on the page looks broken, so anything needing a
judgement call comes back to the producer:

| input | outcome |
| --- | --- |
| `1973`, `בערך 1973`, `73` | accepted |
| `early 70s`, `שנות ה-70` | refused — a span, not a year |
| **`mid 1970s`** | refused — contains a real 4-digit year, still a span |
| `1973-1975` | refused — two years, neither of them chosen |
| `20` | refused — 1920 or 2020, both real answers |

Refusals are **reported to the producer**, never dropped and never rounded —
the same lesson as the discarded type answers.

**WIDENED 2026-08-03** from `event` to `person`, `place`, `organisation` and
`event` — a person has a birth year, a place a year you moved there, an
organisation a year you joined it. `other` stays excluded: it is the fallback
for a name the extractor could not classify, and asking the year of something
we do not understand is noise on a screen whose value is only asking what
genuinely needs an answer.

**Widening made "ask once" load-bearing**, and it needed migration `0015`
(`entities.year_asked_at`). `year_start IS NULL` is true both for an entity
nobody has been asked about and for one the producer was asked about and
skipped — and those must behave differently. Skipping is a real answer ("I do
not know"); without the stamp, every later recording mentioning ניר would ask
again until the producer learned to click past the whole screen.

The stamp is set when the question is PUT, answered or not, and never moved.
Deliberately not backfilled, so the 14 existing entities each get exactly one
offer rather than being excluded forever.

Years fill in but never overwrite: ingest order cannot re-decide one the
producer already gave.

28 new tests (684 total), most of them feeding the parser things it must
refuse.

### Phase 3 — year capture (original plan)

Depends on §2.1. Under Option A/C this is **optional decoration**, so it can
slip without blocking the timeline.

- When an entity that would carry a year has none, add a year question to the
  same batch. Skippable, always.
- Free-text-to-year parsing must be forgiving ("1973", "בערך 73", "early 70s")
  and must **refuse rather than guess** — a wrong year silently reorders a life.
- Writes `year_start`/`year_end` on `entities`.

### Phase 4 — family tree page

Read-only. New `View` + nav item + lazy panel, dark design system.

**Backend:** one endpoint returning nodes + edges.

```sql
-- nodes
SELECT e.id, e.name, e.year_start, e.year_end, e.is_self
FROM entities e
WHERE e.producer_id = :producer_id AND e.type = 'person';

-- edges (entity_relations has no producer_id — scope via the entity join)
SELECT r.from_entity_id, r.to_entity_id, r.relation_type, r.source_segment_id,
       rt.is_symmetric, rt.inverse_type, rt.label_en, rt.label_he
FROM entity_relations r
JOIN relation_types rt ON rt.relation_type = r.relation_type
JOIN entities f        ON f.id = r.from_entity_id
WHERE rt.is_tree_edge AND f.producer_id = :producer_id;
```

**Layout:** standard genealogy, generations as rows, rooted at `is_self`.
Assign generation by walking `parent`/`child` edges from the root; `sibling`
and `spouse` stay on the same row.

**The honest cases the brief asks about:**
- **No relations yet** → render the single self node with an explanation of
  where relations come from. Not a spinner, not an error, not a blank page.
- **Unreachable people** — an `aunt_uncle` whose own parent link was never
  captured has no generation. `is_tree_edge` is false for those types precisely
  so the tree does not have to place them, but a `sibling`-only person with no
  path to the root can still occur. Render them in a clearly separated
  "related, not yet placed" area rather than guessing a generation or dropping
  them silently. **The tree never guesses.**
- **Cycles / contradictions** (A parent of B and B parent of A, from two
  recordings) — detect, drop the later edge from the layout, and surface it
  rather than looping forever.

**Click a person** → the moments panel, from the third query in §3. Video,
transcript, question-as-title. Every edge carries `source_segment_id`, so
"brother" can link to the producer *saying* it — that is the property worth
designing around, and it falls out of the schema for free.

### Phase 5 — timeline page

Read-only. Same shell treatment.

- Bubbles = categories, ordered by first `question_index` (chronological by
  construction). Under Option C, `event` entities with `year_start` merge in
  as additional bubbles sorted by year.
- Click → expand to sub-bubbles (§3 query 2), capped at a handful with a
  "more" affordance; `mention_count DESC` puts the most-present people first.
- Click a sub-bubble → the same moments panel Phase 4 built. **One component,
  two entry points.**
- Empty states: a category with no recordings renders unfilled and invites
  recording it.

---

## 5. Constraints that hold across every phase

- **Read-only.** No editing the tree or timeline. If producers want that
  later it is a separate feature with its own confirmation semantics.
- **Never blocked.** A producer who just wants to record must be able to skip
  every relation and year question, on every recording, forever.
- **Reuse the design system.** `btn-primary` / `glass-card` / `card-glow`; the
  `calm-*` theme is `/talk`-only. No new visual patterns.
- **Nothing is auto-applied.** Relations follow the identity-merge rule: a
  silent wrong relation is worse than an unanswered one.
- **One directed row per relation.** Inverses are derived at read time.
- **Relations cascade with their recording.** `source_segment_id` is
  `ON DELETE CASCADE` — deleting a recording removes what it taught us, which
  is deliberate.
- **Bilingual.** `relation_types` carries `label_en`/`label_he` and the
  question set is bilingual; neither page should hardcode a language.

## 6. Explicitly out of scope

- Editing, adding or deleting relations from the tree UI.
- Importing a tree from GEDCOM or any external genealogy source.
- Inferring relations across recordings (only what one recording states).
- Photos or avatars on tree nodes — the archive stores video, not portraits.
- Anything about the `avatar` chat mode.

## 7. Before the first commit of any phase

- `python -m pytest -q -m 'not integration'` — currently 549 passing.
- Frontend `tsc`, `eslint`, `next build` clean.
- Check the mid-flight `pending_confirmation` count before changing that
  payload (Phase 2b).
