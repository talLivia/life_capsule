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

### ✅ Phase 4 — family tree page — DONE 2026-08-03

`app/services/family_tree.py`, `GET /api/v1/entities/tree`,
`GET /api/v1/entities/{id}/moments`, and `FamilyTreePanel` as a new shell view.
Read-only throughout.

Renders from the live archive today:

```
row -1  (parents)         אילנה, צבי
row  0  (you + siblings)  ▶ Tal Nahum, חן, ניר, עדי, רז
not yet placed            איציק כהן, רוני כהן
```

**Generation offsets live in `relation_types.generation_delta`** (migration
`0016`), not in a map in the layout code. `is_tree_edge` was already in that
table, so how far a relation moves belongs there too — and adding a
tree-bearing type must not require editing a layout module to make it draw.
Not derivable from the existing columns: `is_symmetric` gets sibling and
spouse to zero, but parent and grandparent are both directional and differ.

**Every "never guess" case is real, not defensive:**

| case | what happens |
| --- | --- |
| no family path to the root | own **"Mentioned, not yet placed"** section — hits the live archive today |
| tree type with no `generation_delta` | person left unplaced, type reported; assuming 0 would put a step-parent in the producer's own row |
| recordings that disagree | first (shortest-path) placement stands, conflicting edge **reported** |
| a cycle | terminates |
| no self-entity | empty tree, everyone unplaced — generation is meaningless without a root |
| no relations at all | empty state explaining that family comes from recording |

Edges are walked in **both** directions, since the schema stores one directed
row and derives the inverse at read time — a from→to-only walk would never
reach a parent from their child and every ancestor would come back unplaced.

**§2.4 resolved:** the nav item shows even with an empty tree. Hiding it would
hide the only place a producer learns that family comes from recording.

Clicking a person plays their moments — video, transcript, and the interview
question as the title. That endpoint is deliberately the one the timeline's
sub-bubbles will use (§3), so the two pages cannot drift.

14 new tests (707 total).

#### Phase 4a — drawn as a node graph, not grouped cards — 2026-08-03

`FamilyTreeGraph` replaced the per-generation card groups with hand-built SVG:
nodes positioned by generation, connections drawn as lines. **Rendering layer
only** — `family_tree.py` decides who sits where and was not touched.

**No graph library, deliberately.** A family tree is a DAG (two parents, one
child), so `d3-hierarchy` and `react-d3-tree` — which assume one parent per
node — do not model it. The hard part, generation assignment, already happened
server-side; what is left is arithmetic over rows. Scale is ~9 nodes. A library
would flip this only for pan/zoom over hundreds of nodes with automatic overlap
avoidance. No new dependency.

**The never-guesses rule reappeared at the rendering layer**, which was not
obvious until the geometry was checked against live data. The producer's four
siblings are each recorded as a sibling *of the producer*, and the producer
sits at column 0 with the siblings at 1-4. Drawn as straight horizontal lines,
three of those connectors pass **behind** the nodes in between, and what a
reader sees is a chain חן–ניר–עדי–רז — four relations nobody recorded.
Occlusion is not neutral: a hidden line segment still reads as a connection
between the two things it visibly touches. Same-row edges were therefore
routed below the row — and then **removed entirely in 4b**, which deleted the
problem instead of solving it.

Siblings connect to the **producer**, not up to the parents. "ניר is my
brother" and "צבי is my father" are separate facts, and nothing states that
ניר is צבי's child. A conventional genealogy chart would draw those edges
anyway.

Verified by replicating the layout maths against the live tree rather than by
eye: no overlapping nodes, no edge with an unplaced endpoint, no connector
drawn through a node, and the panel's row-label pitch equal to the SVG's row
pitch (162px — they are separate constants and would silently drift apart).

#### Phase 4b — full page, pan/zoom, no same-row lines — 2026-08-03

Three changes, all rendering-layer. `family_tree.py` still untouched.

**Same-generation connectors removed.** Siblings and partners are shown by
sharing a row. The row already carries that, and with four siblings all
recorded as siblings *of the producer* the lines fanned from one node and
needed the 4a detour routing to avoid reading as a chain. Deleting the lines
deleted the routing, the `row`/`col` bookkeeping, and the occlusion problem.

Two consequences, both real and both accepted:

- A recorded **marriage** between two people in the same row is no longer
  visible as a marriage. Stated in the caption under the chart rather than
  left for someone to notice.
- Anyone whose only relation is same-generation now has **no drawn line at
  all**. On the live archive that is 4 of the 5 people in row 0.

**Viewport is a library; nodes and edges are not.** `react-zoom-pan-pinch`
(~13KB gz) handles pointer-anchored wheel zoom, trackpad and touch pinch, and
drag panning. This reverses the 4a "no library" call *for that piece only*,
because pan/zoom went from hypothetical to required. The reasoning that ruled
libraries out — a family tree is a DAG that `d3-hierarchy` cannot model — was
about LAYOUT, and layout is still hand-built. The library never sees a node.

A 5px movement threshold separates a click on a node from a pan that happened
to start on one; without it, nudging the canvas opens somebody's moments.

**Row labels moved inside the SVG** so they pan and zoom with the rows they
name. This also deleted a constant that had to be kept equal to `NODE_H +
ROW_GAP` in a separate file — drift there would have been silent.

**Full page.** The chart gets the width and the moments open in a modal over
it. A tree is a shape you read across; giving it 60% so an empty aside could
hold the other 40% had it backwards.

##### Stress test — 12 aunts/uncles + 15 nieces/nephews

Temporary synthetic rows (`scripts/synthetic_tree_data.py --seed/--clean`),
`entities` and `entity_relations` only, no fabricated recordings. Seeded,
measured, deleted; `raw_segments`/`entity_mentions`/`interview_sessions`/
`messages` verified identical at 17/25/2/391 before and after.

Geometry held at 34 nodes and **2774×422px**: no overlapping nodes, no line
crossing a node it is not attached to, descent lines median 522px / max
1131px of horizontal travel. Fits a 1440px viewport at scale 0.51 — too small
to read, which is what pan and zoom are for.

Two things did NOT hold, both pre-existing and exposed by the width:

1. **Row labels become false.** Row −1 labelled "Parents" held 12 people who
   are not parents; row +1 labelled "Children" held 15 nieces and nephews and
   zero children. The labels name a row after the producer's *closest*
   relation in it, which stops being true as soon as anyone else is there.
   Fix is generation-relative wording ("your parents' generation").
2. **Floating nodes.** 12 of 14 in row −1 had no drawn line, because
   sibling-of-parent is a same-generation relation and those are no longer
   drawn. They read as disconnected rather than as aunts and uncles.

#### Phase 4c — trunk descents, zoom steps, portrait page — 2026-08-03

**Descents are one trunk per parent group.** Children sharing a parent set hang
off one bus: stem from each parent, joining bar, one trunk down, branch to each
child. Sibling connectors stay removed.

This does not reintroduce the 4a occlusion bug, and the reason is a property
rather than a hope: that bug existed because a same-row line runs at the row's
**vertical centre**, the band the node boxes occupy. Every trunk segment lives
in the **row gap**, which holds no nodes by construction. Verified by
segment-vs-node-box intersection over every drawn segment, not by eye.

The same class of error does have a new form: several groups descending into
one gap put their buses at the same height, and two overlapping buses would
merge into what reads as a single family. Groups are given different bus depths
when their spans overlap. **This fired on the stress data** — three sibling fans
in one gap, one pushed from busY 310 to 297.

A joining bar between two parents states "both are parents of these children",
which is recorded. It is not a marriage line.

*Known limit:* an edge spanning more than one generation drops through the row
between. No such relation exists today; the check reports it if one appears.

**Zoom.** `smooth={false}`. The library computes
`zoomStep = smooth ? step * Math.abs(event.deltaY) : step`, and a Windows wheel
click reports `deltaY: 100` — so the previous `step: 0.08` became 8 and one
click saturated `maxScale`. Constant steps (0.1 wheel, 0.15 button) are the
only way to get intermediate levels from a wheel. Opening scale also has a
**readable floor** of 0.6: a fitted view of a wide tree is unreadable, and a
legible view you pan beats a complete one you cannot read.

**Portrait page.** `max-w-4xl` and `h-[calc(100vh-13rem)]`, min 620px. Height is
what shows several generations at once; width is what panning is for.

**The moments dialog rendered transparent.** Two changes: the card was made
opaque instead of `glass-card` (which carries `backdrop-blur-xl` — a
backdrop-filtered element nested inside another filters against the outer
backdrop ROOT, sampling the page as it was *before* the overlay darkened it),
and the overlay was portalled to `document.body`, since `position: fixed`
resolves against any ancestor with a transform, filter or backdrop-filter
rather than the viewport.

⚠️ **Both are sound, and neither was demonstrated to be the cause.** See 4d —
this was written as a diagnosis when it was a hypothesis.

#### Phase 4d — width, backdrop verified, sibling parentage confirmed — 2026-08-03

**Width** is `max-w-7xl`, matching the chat view.

**The backdrop, verified instead of reasoned about.** 4c asserted a cause it had
not checked, so this time every layer was inspected in the built output:

| checked | result |
| --- | --- |
| new markup in the built JS chunk | present; old `glass-card` markup gone |
| `bg-surface-950/95` in production CSS | `background-color:#05050af2` — 95% opaque |
| same class in the **dev-server** CSS | present, generated 15:38 |
| `z-[60]`, `backdrop-blur-md`, `animate-scale-in` | all generated |
| card `backdrop-filter` | none — no nesting left |
| portalled to `document.body` | yes |

So the shipped code is correct at every layer that can be inspected without a
browser, in both dev and production builds. The nested-backdrop-filter story in
4c was a plausible mechanism stated as a finding; it was never confirmed to be
what was on screen, and the class it blamed was compiling correctly all along.

`scratchpad/verify_modal_backdrop.py` re-runs the whole check and writes an HTML
page that inlines the real compiled CSS and renders the exact same markup over a
busy fake page. The class strings are **read out of `FamilyTreePanel.tsx`**
rather than copied, so the check cannot drift from the component.

**Sibling parentage already works — zero changes.** Confirmed three ways: three
new tests, and a live-data seed (`--seed-siblings`) rendered through the real
layout maths.

| case | result |
| --- | --- |
| sibling given the producer's own two parents | joins the producer's trunk — 2 parents, 3 drops, no contradiction |
| half-sibling with a *different* recorded parent | that parent lands in the parents' row, reached only through the sibling |
| sibling with no recorded parent | placed in the row, no line, nothing inferred |

The second case works only because the walk follows every edge in both
directions — the new parent is reachable from the root exclusively via the
sibling.

**The gap is bigger than "an unmentioned half-sibling parent".** No sibling in
the live archive has *any* parent edge: they are recorded as siblings OF THE
PRODUCER and nothing says whose children they are. Rendering is not the problem
and needs no change; capture is. Not built — see the open question at the end
of this section.

**Pre-existing flaky test fixed.** `test_contradicting_recordings_...` pinned
which of two contradicting edges wins. Both are written in one transaction, so
`created_at` ties and `_load_edges` falls through to `id` — a fresh uuid4 per
run, making the assertion a coin flip. It now asserts what is actually
guaranteed: the disagreement is reported, and the person is drawn once. Six
consecutive runs pass. Production ordering was left alone: for a given database
the ids are fixed, so the tree does not flip between page loads.

### Phase 4 — family tree page (original plan)

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

---

## 8. ✅ Phase 6 — recording review flow + sibling parentage — BUILT 2026-08-03

Two requests folded into one design, because they land on the same surface.
The sibling-parent question is **not a third mechanism**: it is a fifth
question class inside the confirmation payload that already exists, rendered in
the same popup as identity, type, relation and year questions.

> ⚠️ **The extraction-screen message never arrived.** This is written from the
> three-item summary quoted in the follow-up — *auto-open after recording,
> progress indicator, separate confirmation popup*. Everything in §8.2 marked
> **[assumed]** is my reading, not your spec. Correct those on review.

### 8.1 What exists today

```
record -> presign -> PUT -> /segments/ingest -> Celery -> analysis_graph
                                                             |
   transcribe -> chunks -> embed -> topics -> check_entities  |
                                 -> human_confirm -- interrupt(one payload)
                                 -> score -> finalize
```

| piece | today |
| --- | --- |
| `RawSegment.status` | `processing` -> `ready` / `pending_confirmation` / `failed`. **No per-stage detail.** |
| `EntityConfirmModal` | polls `/segments/pending-confirmations` every **8 s**, opens itself when one appears |
| `ExtractionModal` | read-only, opens **only** when the producer clicks a recording in `RecordingList` |
| `human_confirm_node` | **exactly one** `interrupt()` per recording, carrying four question classes |
| ask-once precedent | `Entity.year_asked_at` — a NULL year cannot distinguish "never asked" from "asked, didn't know" |

The single-interrupt rule is a documented decision, not an accident: a sequence
of modals gives the producer no idea how many are coming, and each answer is
given without seeing the others. **Both halves below preserve it.**

### 8.2 Recording review flow — CONFIRMED 2026-08-03

Full spec received; the `[assumed]` version this replaces was written from a
three-word summary. Two guesses were wrong and are corrected below: the flow is
**gated on the extraction screen**, not fire-and-forget, and there is **no
concurrency to design for** — nothing supports batch upload, so exactly one
extraction is ever in flight.

**Today:** after a recording or upload the interview advances immediately,
extraction runs silently, and confirmation questions surface only if the
producer later clicks "extracted from this" — disconnected from the recording
they were about.

**New flow:**

1. On completion of a recording or upload, the extraction screen opens by
   itself — same content as the manual panel.
2. A progress indicator runs while extraction does, so the producer can tell
   the app is working rather than stuck.
3. On finish: pending questions open as a **separate popup**, never merged into
   the extraction screen's content. With nothing to confirm the extraction
   screen closes itself and the interview advances.
4. The manual "extracted from this" button stays, for reviewing past
   recordings.

#### Decision 1 — the screens do not stack

The extraction screen **closes, then** the confirmation popup opens.

- One surface at a time. Two stacked dialogs make it ambiguous which one the
  Escape key and the backdrop click belong to.
- Stacking would nest two `backdrop-filter` layers — the exact structure behind
  the transparent-dialog investigation in 4c/4d. The overlay is 95% opaque, so
  the screen behind would be invisible anyway: the stack costs a known risk and
  buys nothing anyone can see.
- The confirmation popup already carries its own context — each question shows
  the name and the evidence phrase — so it does not need the extraction screen
  visible behind it.

**After submit the extraction screen reopens** with the finished result, then
closes itself and the interview advances. That is not a decorative extra step:
entities are only written at `finalize_ingest`, which runs *after* the answers
come back, so this is the first moment the screen can show what was actually
captured. The reopened screen is the payoff for the questions.

#### Decision 2 — partial reveal, not a spinner

The screen fills in as results land, in pipeline order, rather than showing a
generic processing state.

This is not a UI trick: **the pipeline already persists in stages**, and
`/segments/{id}/extraction` reads straight from the database with no status
gate, so polling it mid-run returns exactly what exists so far.

| lands | committed by | when it becomes visible |
| --- | --- | --- |
| transcript | `transcribe_node` — commits immediately | early |
| topic tags | `extract_topics_node` — commits | middle |
| entities, relations | `finalize_ingest` — **after** confirmation | only at the end |

So the honest sequence is **transcript → topics → [confirmation] → entities**.

Why partial beats generic:

- The transcript is what a producer most wants to check — *did it hear me
  right?* — and it is the first thing ready.
- A spinner held for the length of a real extraction reads as stuck, which is
  the exact failure mode the spec names.
- Partial reveal makes the progress claim self-evidencing. A stage label has to
  be trusted; a transcript appearing on screen does not.
- It also explains the popup that follows: entities are absent from the screen
  precisely because they are what is about to be asked about.

Nothing is shown that later changes. Transcript and topics are final once
written; entities are not displayed until they exist.

#### Never blocked

The extraction screen is dismissible. Closing it early advances the interview,
extraction continues, and the confirmation popup appears when ready — exactly
as it does today through the poll. That keeps §5's "a producer who just wants
to record must be able to skip" true, which a hard gate would not.

On the nothing-to-confirm path the screen auto-closes after a short dwell so
the finished state is perceptible instead of a flash.

#### Mechanics

- `RawSegment.progress_stage`, a nullable string written at the top of each
  node. Chosen over a WebSocket because the app already polls, a column
  survives a reload, and the graph resumes from a checkpoint a socket would
  have to re-derive.
- Poll at **2 s while a segment is in flight**, 8 s otherwise. A progress
  indicator that updates every 8 s reads as frozen.
- One extraction at a time — no queue, no per-segment tracking map.

| node | shown as |
| --- | --- |
| `transcribe` | Listening to your recording |
| `create_transcript_chunks`, `embed_transcript` | Reading it back |
| `extract_topics` | Finding the themes |
| `check_entities` | Finding the people and places |
| `human_confirm` | Waiting for you |
| `score_importance`, `finalize_ingest` | Filing it away |

### 8.3 Sibling parentage — a fifth question class

**Confirmed first, per your instruction: rendering already works and needs
nothing.** Three tests plus a live-data seed (Phase 4d) show that a sibling
given the producer's own parents joins the producer's trunk, and that a
half-sibling with a *different* recorded parent puts that parent in the
parents' row. **The gap is capture, not drawing.** No sibling in the live
archive has any parent edge at all — they are recorded as siblings OF THE
PRODUCER and nothing says whose children they are.

**No extraction-prompt change is needed.** You expected this to touch the
prompt; it does not. The question is derived from the DATABASE — siblings that
exist, lack parent edges, and have not been asked — not from anything the model
reads. That removes a whole risk class: no prompt re-tuning and no re-measuring
breadth, which CLAUDE.md forbids doing casually anyway.

**Trigger**, evaluated in `human_confirm_node` alongside the other four:

```
ask about sibling S when ALL hold:
  - S is in the producer's generation 0 via a confirmed sibling relation
  - the producer has >= 1 recorded parent   (nothing to offer otherwise)
  - S has no parent edge of their own
  - S.parentage_asked_at IS NULL            (ask once, ever)
```

Deriving from the DB rather than from this recording's proposals is what keeps
the single interrupt. A sibling confirmed *on this screen* is not yet in the
database, so it is asked about on the **next** recording — a one-recording lag,
in exchange for not adding a second pause. It also clears the existing backlog
of four, which an on-confirm trigger never would.

> *Alternative considered and REJECTED 2026-08-03:* ask inline under the
> sibling proposal on the same screen. No lag, but it needs conditional UI, a
> discard path when the relation is rejected, and a second code path for the
> backlog. The one-recording lag was accepted instead, explicitly to keep
> one-interrupt-per-recording intact. Do not revisit without a reason to
> break that rule.

**The question**, one card per sibling, all of them on the one screen:

```
Whose child is ניר?           (optional — skip and we won't ask again)
  [x] אילנה     [x] צבי        <- checkbox per recorded parent
  [ ] someone not mentioned yet -> ____________
```

Checkboxes rather than "same as you / different", because a half-sibling shares
*one* parent — a binary cannot express "same father, different mother", which
is the exact case you raised.

**Answers ->**

| answer | written |
| --- | --- |
| one or more boxes ticked | a `parent` edge from each ticked parent to the sibling |
| a name typed | a normal new entity + a `parent` edge, same path as any relation capture |
| nothing / skip | `parentage_asked_at = now()`, never asked again |

Ticking nothing but typing a name is legitimate (both parents unmentioned).

**Ask-once needs a column**: migration `0017`, `entities.parentage_asked_at`,
exactly parallel to `year_asked_at` and for the same reason — "no parent edges"
cannot distinguish never-asked from asked-and-skipped, and without the
distinction every future recording re-asks until the producer learns to click
past the whole screen.

**Open question — provenance.** `entity_relations.source_segment_id` is NOT
NULL and means "the recording that established this". A parentage answer is
given *while confirming* a recording that may never mention the parent.
Pointing at it is slightly false, and the tree offers "play the recording where
this was said". Three options, no recommendation yet:

- **(a)** point at the current segment and accept it — simplest, mildly untrue;
- **(b)** point at the segment where the *sibling relation* was established —
  truer, still not where the parentage was stated;
- **(c)** add `entity_relations.origin` (`recording` | `confirmation`) so the
  tree can decline to offer a recording for answers that came from a screen.

**RESOLVED 2026-08-03 — (c).** `entity_relations.origin` is added. An answer
given on a confirmation screen is not something a recording says, and the
tree offers to play the recording where a relation was stated; pointing at a
segment that never mentions the parent would make that offer a lie. The
column costs one migration and keeps the provenance honest.

### 8.4 How the two halves meet

The parentage question has **no surface of its own**. It arrives in the same
`pending_confirmation` payload, as `parentage_questions`, and renders as
another skippable section in the confirmation popup from 8.2C — below
relations, above years. One screen, one submit, one resume, exactly as today.

### 8.5 Build order — all landed

Verified against the live archive: `parentage_candidates` returns all four
siblings with אילנה and צבי offered as parents, so the question fires on the
real backlog rather than only in tests. 14 new tests, 724 passing.

Two things the build changed from the plan, both discovered in the writing:

- **`write_segment_relations` now deletes only `origin="recording"` rows.** It
  replaces this segment's relations on every re-analysis, which would have
  wiped a parentage answer the producer typed. Re-reading a transcript can
  never re-produce that answer, so it must not be in scope of the replace.
  `origin` earns its place twice over — provenance *and* this.
- **Progress is applied by wrapping the nodes at graph-construction time**, not
  by a line inside each. Eight call sites are eight chances for a new node to
  land without a stage; a wrapper makes that impossible.

| step | change |
| --- | --- |
| 1 | migration `0017` — `parentage_asked_at`, plus `origin` if (c) is chosen |
| 2 | `progress_stage` column + writes in each node; progress exposed on the segment |
| 3 | `parentage_questions()` builder + `human_confirm_node` wiring + resume-handler validation (staleness both directions, as the other four do) |
| 4 | `entity_store` write path for parentage answers |
| 5 | frontend: progress surface, auto-open, faster in-flight poll |
| 6 | frontend: parentage section in the confirmation popup |
| 7 | tests — ask-once, half-sibling subset, skip, backlog, staleness |

Steps 2/5 and 3/4/6 are independent; either can land first.

### 8.6 Explicitly not in this phase

- **No tree rendering changes.** No dashed lines, no inferred edges, no
  styling for "derived" relations. The tree draws real recorded parent-child
  edges and nothing else — it already does this correctly.
- No inference. "Your sibling is probably your parents' child" is never
  written without the producer ticking the box.
- No editing of parentage once answered; that is the read-only rule in 5.
- No back-fill script. The backlog clears through the normal flow.

### 8.7 🚨 The router that never learned about the questions — fixed 2026-08-03

Found by a real recording that raised ten relations and asked about none of
them. `_has_confirmation_questions`, the gate deciding whether the graph enters
`human_confirm_node`, checked **identity and type only** — unchanged since
"Chunk 4: batched entity confirmation", while relations (Phase 2), years
(Phase 3) and parentage (Phase 6) were each added to the interrupt payload.

A recording raising no identity or type ambiguity routed straight past
confirmation. And because that node is what narrows `proposed_relations` to the
**accepted** subset, `finalize_ingest` then wrote every proposed relation
unasked. Measured on the live archive:

- **10 relations written with no consent**, `pending_confirmation` NULL.
- **0 entities had ever been asked for a year** — 19 entities, months after
  year capture shipped. Phase 3 had never once fired in production.
- Parentage could never have fired either.

It looked exactly like success: a recording that ingests cleanly and shows no
questions. Nothing to notice.

**The fix is structural, not a longer list.** `build_confirmation_payload` is
now the single source both the router and the node read, so a class that is
asked about necessarily gates the route. A sixth class added anywhere else does
not exist. Same shape, and same reason, as sharing `_chosen_option` between
`resolve_steps` and `category_is_settled`.

Guarded by `test_the_router_asks_about_every_class_the_payload_carries`, which
walks every key in the payload and fails if the router disagrees about any one
of them, plus a pipeline-level test that a year question **alone** pauses.

Verified by replaying the exact recording: before, routed `skip` and wrote ten
relations; after, paused with 10 relation, 10 year and 4 parentage questions
and 10 editable names, without reaching finalize.

**Two tests had been encoding the bug.** `test_a_recording_with_no_ambiguity_
never_pauses` asserted precisely the broken behaviour and passed throughout.
Renamed to `..._that_raises_nothing_at_all_...`: "no ambiguity" was never a
reason not to pause, and naming it that way is what let three phases of
questions go missing without a single red test.

### 8.8 Correcting a misheard name — 2026-08-03

`ליאן` for `אליאן`. A brand-new name has nothing similar to disambiguate
against, so it raised no identity question — the extractor was confident and
wrong, and there was nowhere to say so. Identity questions only ever offered
"same as X / someone new", never free text.

Every extracted name is now editable on the confirmation screen. It rides in
the payload as `editable_entities` and is deliberately **not** part of
`build_confirmation_payload`: counting it as a question would pause on every
recording that named anybody.

A rename rewrites the entity **and every relation endpoint pointing at it**.
Endpoints are names resolved by lookup at write time, so renaming without that
would leave them unresolvable and the relation dropped with a log line nobody
reads — the same silent-failure shape as everything else in this file.

*Known limit:* a recording that raises no questions shows no screen, so there
is nowhere to correct a name on it. Rare now that any unsettled year pauses,
but real.

### 8.9 `aunt_uncle` is a tree edge — 2026-08-03

"יש לי דודים אמנון ועדל" extracted correctly and both landed in "not yet
placed", because the type had `is_tree_edge=False` and a NULL delta. An aunt
or uncle is a sibling of a parent — the parents' row, one generation up.
Migration `0018`. This is a GENERATION offset and not a claim about parentage:
the row is shared, the edges are not.

### 8.10 🚨 The same omission again, one layer up — fixed 2026-08-03

The router fix (§8.7) made the pipeline pause correctly. The screen still
showed nothing, and the cause was the same shape in the client.

`EntityConfirmModal` guarded its render with:

```ts
const totalCount = identityQuestions.length + typeQuestions.length
if (!pending || totalCount === 0) return null
```

Live segment `ac4e221f`: **10 relation questions, 10 year questions, 10
editable names — fetched, in state, and thrown away**, because the two oldest
classes happened to be empty. Written when identity and type were the only
classes; three later classes never reached it. Exactly the router bug, one
layer up, and it survived the router fix because fixing a gate does not fix a
copy of that gate somewhere else.

Meanwhile the extraction screen said *"hang on a moment, we'll ask as soon as
this finishes"* — indefinitely — because `still_processing` was
`status not in (ready, analyzed, failed)`, so `pending_confirmation` counted as
processing. **Awaiting a person is not working**, and the manual panel has no
handoff, so it had no other words available.

Both fixes are structural rather than longer lists:

- `countQuestions` counts **every array in the payload except a named
  non-question set** (`editable_entities`). A class added on the server is
  counted here without this file being touched.
- `awaiting_confirmation` is its own field, and `still_processing` now excludes
  `pending_confirmation`. The extraction panel says which state it is in, on
  the manual path as well as the live one.

Also fixed: the footer read "0 of 0 answered" when nothing was required. It now
says "Everything here is optional", because only identity and type are
compulsory and a zero-of-zero counter reads as an error.

Verified against the live payload: old guard 0 → renders nothing; new guard
20 questions → renders, with the 10 editable names correctly NOT counted as
questions.
