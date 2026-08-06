# Timeline year ranges — attributing a year to whose life it is about

**Written 2026-08-06. Nothing built.** A plan for showing each timeline
category a year range ("1984 to 1995") derived only from years about the
PRODUCER, not from every year mentioned in that category's recordings.

Its own file rather than folded into an existing one: it spans two systems
that neither doc owns. `FAMILY_TREE_TIMELINE.md` documents the timeline that
was built; `GUIDED_INTERVIEW.md` covers the `/record` experience. This needs a
change to the QUESTIONNAIRE's metadata and a change to how a year is stored,
and sits across both.

---

## 1. The problem, measured on the live archive

Every year currently held, with the category of the recordings mentioning it:

| entity | type | year | categories |
| --- | --- | --- | --- |
| טבריה | place | **1984** | childhood ×4 |
| צבי | person | 1956 | childhood |
| מרוקו | place | 1920 | childhood |
| עיראק | place | 1915 | childhood |
| ארליך | organisation | **1995** | childhood |

A naive range over the `childhood` category is **1915–1995**: eighty years, for
a childhood. The two years actually about the producer's childhood are **1984**
(born in טבריה) and **1995** (started school at ארליך). The rest are a father's
birth and two grandparents' countries of origin.

This is exactly the failure described. It is also worse than expected, because
of §2.

## 2. Two cheap options are already dead

Both were considered and are ruled out by the table above, not by argument.

**Per-CATEGORY attribution does not work.** The obvious idea — "years from
recordings in this category are about this period" — fails because *all five*
years above came from `childhood` recordings. The category cannot separate the
producer's birth from their grandmother's emigration, because the interview
asks about both under "childhood".

**Entity TYPE does not work either.** "Years on people are about that person;
years on places are about the producer's involvement" is tempting and wrong:
טבריה (1984, the producer's birth) and מרוקו (1920, where grandparents came
from) are both `place`.

## 3. What DOES separate them: the question, not the category

Look at which question elicited each year:

| year | elicited by | about |
| --- | --- | --- |
| 1984 | `childhood_q01` — "when and where were you born?" | **the producer** |
| 1995 | `childhood_q06` — "the city you grew up in" | **the producer** |
| 1956 | `childhood_q02` — "tell me about your father" | someone else |
| 1920 | `childhood_q05` — "your family's roots… grandparents" | ancestors |
| 1915 | `childhood_q05` | ancestors |

The split is clean and it is at QUESTION granularity. `childhood_q01` and
`childhood_q05` live in the same category and ask about different people's
lives. Nothing in the current data model records that difference.

## 4. What it would take

Two additions. Neither is large; the second is the one with real cost.

### 4.1 Record which recording a year came from

`Entity.year_start` is a property of the entity, and the year question is asked
**once per entity, ever** (`year_asked_at`). So the recording that elicited a
year is not recoverable today — `year_asked_at` is a timestamp, not a link.

Add `Entity.year_source_segment_id` (nullable FK to `raw_segments`, ON DELETE
SET NULL), written by the same code that writes `year_start`. One column, one
migration.

**Backfill:** the earliest mention's recording is the best available guess for
the five existing rows, and it happens to be correct for all five. Say so in
the migration rather than implying it is derived — it is a guess, and a
producer editing a year later should overwrite it with the real source.

### 4.2 Classify the 129 questions by whose life they ask about

A new field in `interview_questions.json`:

```json
{ "id": "childhood_q01", "text": "…", "subject": "speaker" }
```

Proposed vocabulary, deliberately small:

| value | meaning | example |
| --- | --- | --- |
| `speaker` | an event in the producer's own life | "when were you born?" |
| `other` | about another named person's life | "tell me about your father" |
| `context` | background, ancestry, or open reflection | "your family's roots", "your philosophy of life" |

Only `speaker` years feed a range. `other` and `context` years are still
captured, still shown on the entity, and still power the family tree — they
just do not define the producer's own chronology.

**This is the expensive part, and it is a content change rather than a code
change:** 129 manual judgements. It should be done as a data migration with the
producer's review, not inferred by an LLM — a misclassification silently shifts
a life period by decades, which is precisely the class of error the archive is
built to avoid.

`interview_schema` should require the field, so a question added later cannot
omit it and quietly fall into a default.

### 4.3 Deriving the range

```
for each category:
    years = [ e.year_start for e in entities
              if e.year_source_segment_id is in this category's recordings
              and question_subject(that recording) == "speaker" ]
    range = (min(years), max(years))   # omitted entirely when years is empty
```

Consistent with the timeline's existing rules: a category with no qualifying
years shows **no range at all** rather than a guessed one — the same call
`FAMILY_TREE_TIMELINE.md` §8.4 makes for question counters and the tree makes
for unplaced people. No invented denominators, no invented dates.

## 5. The deeper issue, stated so it is a choice

`Entity.year_start` conflates two different facts:

- **when the thing existed** — מרוקו did not begin in 1920;
- **when the producer was involved with it** — 1920 is when their grandparents
  left.

טבריה's 1984 is not "when טבריה began" either; it is when the producer was born
there. So the field already means "the year of the event connecting this entity
to this story", and the schema does not say so anywhere.

A timeline wants **periods of a life**, which is a property of a STORY, not of
an entity. The alternative design is therefore to stop deriving ranges from
entity years and instead ask, once per recording, *"roughly when did this
happen?"* — a per-segment year, on exactly the axis a timeline needs.

| | entity years + §4 | per-recording year |
| --- | --- | --- |
| new schema | 1 column | 1 column (`raw_segments.happened_year`) |
| new questionnaire metadata | **129 classifications** | 129 classifications, still |
| asks the producer more | no — reuses answers already given | yes — one more question per recording |
| existing answers usable | yes, 5 years already captured | no, starts empty |
| semantics | "year of the entity", reinterpreted | "year of the story", direct |

Both need §4.2. The per-recording version is the cleaner model and the more
honest question; the entity version ships without asking for anything new.
**Recommendation: §4 now, because it uses answers that already exist**, and
treat the per-recording year as the thing to build if ranges later need to be
finer than one year per entity.

## 6. Open decisions

| # | Decision | Recommendation |
| --- | --- | --- |
| 1 | `subject` vocabulary — three values, or just `speaker` / not? | Three. `context` and `other` differ in whether a year is about a *person* at all, which the family tree may want later. |
| 2 | Who classifies the 129? | The producer, in a review pass. An LLM draft is fine as a starting point; a silent misclassification moves a life period by decades. |
| 3 | A range from ONE year — show "1984" or nothing? | Show the single year. It is true, and a range needing two points is a display rule, not a fact. |
| 4 | `year_end` — currently never populated. Does a range need it? | No. min/max across a category's `year_start` values is the range; `year_end` stays for entities that genuinely have a span. |
| 5 | Backfill the 5 existing rows from earliest mention? | Yes, and record in the migration that it is a guess. |

## 7. Not in scope

- Ordering the timeline by year. `timeline.py` states the rule already:
  *"Years decorate a sub-bubble; they never move it"* — a page ordered two ways
  disagrees with itself.
- Editing a year after the fact. There is no edit path for `year_start` today;
  that is its own change.
- Anything about photos or galleries — see `docs/MEDIA_GALLERY.md`.
