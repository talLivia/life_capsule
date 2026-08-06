# Timeline year ranges — attributing a year to whose life it is about

**Rewritten 2026-08-06 after the first draft was rejected. Nothing built.**

Goal: each timeline period shows a year range ("1984 to 1995") derived only
from years about the PRODUCER — not from every year mentioned in that period's
recordings, so a grandmother's 1920 does not stretch a childhood to eighty
years.

Its own file rather than folded into another: it needs a change to how a year
is STORED and a judgement the system does not currently make, and sits across
the timeline and the interview.

---

## 1. What the first draft proposed, and why it is withdrawn

It proposed classifying each of the 129 questions once, as
`speaker` / `other` / `context`, and counting only years from `speaker`
questions.

**That is the wrong model.** The same question yields answers about different
people depending on what was actually said — "tell me about your family's
origins" can produce "where I grew up" or "where my grandmother came from", and
the question cannot tell you which.

The live archive demonstrates it more sharply than the counter-example does.
טבריה is ONE entity with ONE year and **four mentions with four different
subjects**:

| question | what this recording said about טבריה | about |
| --- | --- | --- |
| `childhood_q01` | מקום הולדתו של הדובר — the speaker's birthplace | **the speaker** |
| `childhood_q03` | המקום בו נולדה אמו של הדובר — where his mother was born | his mother |
| `childhood_q04` | המקום שבו הכירו ההורים — where his parents met | his parents |
| `childhood_q06` | המקום שבו גדל הדובר — where the speaker grew up | **the speaker** |

Four subjects, one entity, and `childhood_q01` and `childhood_q03` are both
"childhood" questions that differ only in whose life they happened to be
answered about. A per-question label would have to be wrong for at least two
of these four rows.

## 2. The structural finding: the year is at the wrong GRAIN

Mention-level attribution is the right instinct, and it does not rescue this on
its own — because **there is no year at mention level to attribute.**

`Entity.year_start` is one value per entity. טבריה's is `1984`. That is the
speaker's birth year, so it belongs to the `childhood_q01` mention — but
nothing records that, and the other three mentions have no year of their own
and no claim on this one.

So the field already means something the schema never says: not "the year of
this place", but **"the year of one particular event connecting the speaker to
this place"** — and which event is not stored anywhere.

Anything built on `Entity.year_start` inherits that ambiguity. Attribution at
mention level therefore requires the year to move to mention level FIRST:

```
entity_mentions.year_start   -- the year THIS recording attached to this entity
entity_mentions.year_subject -- whose life that year belongs to
```

That is a migration and a change to the year question (asked once per entity
today; it would become once per entity per recording, or once and then
attributable). Neither is large. Both are prerequisites, and the first draft
missed them entirely.

## 3. Can the subject be derived without judgement? No.

Worth stating plainly, because the summaries look like they contain the answer.

They do contain it — as free text, in the producer's language:

```
טבריה  q01: "מקום הולדתו של הדובר"                       -> the speaker
מרוקו  q05: "מקום מוצאם של סבא וסבתא של הדובר מצד האמא"   -> his grandparents
צבי    q02: "האבא של הדובר שמתעסק בפיתוח מוצרים רפואיים"  -> his father
```

**The obvious lexical shortcut does not work, and here is the proof rather than
the assertion.** "Is the speaker the subject?" cannot be answered by looking
for הדובר ("the speaker"), because the phrase appears in every one of these —
`של הדובר` is how a summary says "his", so it is present when the subject is
the speaker AND when the subject is the speaker's grandparents. The
distinguishing word is `מוצאם` / `הולדתו` / `האבא`, which is a sentence to be
understood, not a token to be matched.

There is no `is_self` to join through either: `is_self` marks the producer's own
entity, and none of these mentions are OF the producer — they are of places and
people, with the producer as an implied participant or not.

**So subject attribution is an LLM judgement.** No structural derivation exists
at any grain. Saying otherwise would produce a design that reads cleanly and
fails on the first real transcript, which is the failure mode of the draft this
replaces.

## 4. The three real options

### 4.1 Ask the producer — RECOMMENDED

Add one control to the year question the confirmation screen already shows:

```
Roughly what year was "טבריה"?   [ 1984 ]
   ( ) something that happened to me
   ( ) something about someone else
```

- **Zero misclassification.** The only person who knows is answering.
- **It matches the governing rule.** PROJECT_STATUS states it for the merge
  key: *"when the system isn't sure, it asks"*, and the parentage design says
  *"NOTHING IS INFERRED… only ever written from a ticked box."* Subject
  attribution is exactly that kind of fact.
- **The volume is tiny.** The whole archive has FIVE years. This is one extra
  tap on a question already being asked, not a new screen.
- Still needs §2's grain change if a producer should be able to give טבריה a
  year in more than one recording.

### 4.2 An LLM classifies each mention

Feasible, and the honest cost is higher than it looks:

- It is a **per-mention call** on every recording, forever — not a one-off.
- Ingestion runs `gemini-flash-lite`, which CLAUDE.md already records as
  *"measured weak at exactly the coreference task"*. Subject attribution IS a
  coreference task, in Hebrew, on one-line summaries.
- Today's evidence is not encouraging: on this same archive, that model
  silently returned zero entities for a recording naming a person and a place
  (see `docs/PROJECT_STATUS.md`, the gershayim bug — the model was fine there,
  but the pipeline's tolerance for its output is what saved it).
- A wrong answer here is **invisible**. It does not fail loudly; it shifts a
  life period by decades on a page nobody will double-check.

Defensible as a DRAFT the producer confirms — which is §4.1 with a pre-filled
answer, and strictly better than either alone.

### 4.3 Do not attribute — ask when the STORY happened

Sidestep subject entirely: one question per recording, *"roughly when did this
happen?"*, and a period's range is the min/max over its recordings.

Cleaner in principle, and it does not survive contact with this archive: the
family-roots recording's honest answer is 1920, so `childhood` still stretches
to eighty years. The subject problem reappears unchanged, because a producer
telling their grandparents' story is still telling it inside their own
childhood chapter.

**Rejected** — it moves the question without answering it.

## 5. Recommendation

1. **Move the year to mention grain** (§2) — without it there is nothing to
   attribute, whatever the mechanism.
2. **Ask the producer** (§4.1), optionally pre-filled by an LLM draft (§4.2) if
   the tap ever becomes tedious. Five years today; revisit at fifty.
3. **Derive the range** as min/max over mentions marked "about me", within the
   period's recordings. No qualifying years means **no range shown at all** —
   the same rule §8.4 of `FAMILY_TREE_TIMELINE.md` sets for question counters
   and the tree sets for unplaced people. No invented dates.

## 6. Open decisions

| # | Decision | Recommendation |
| --- | --- | --- |
| 1 | Year at mention grain — new columns, or a `year_source_mention_id` on the entity? | Columns on the mention. The entity pointer keeps one year per entity, which is the thing §2 says is wrong. |
| 2 | Ask subject for every year, or only where it is ambiguous? | Every year. "Ambiguous" is the judgement we just established cannot be made. |
| 3 | Backfill the five existing years? | Ask once, in the UI, next time each is seen. Guessing them re-introduces the problem the doc rejects. |
| 4 | Two subject values or three? | Two — "me" / "someone else". The third value in the withdrawn draft existed to classify questions, and questions are no longer being classified. |
| 5 | A period with one qualifying year — show "1984" or nothing? | Show the year. It is true; needing two points is a display rule, not a fact. |

## 7. Not in scope

- Ordering the timeline by year. `timeline.py` states the rule already:
  *"Years decorate a sub-bubble; they never move it."*
- Editing a year after the fact — there is no edit path for `year_start` today.
- Photos and galleries — see `docs/MEDIA_GALLERY.md`.
