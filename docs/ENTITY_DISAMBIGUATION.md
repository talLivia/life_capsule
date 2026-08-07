# Two people, one name

**Written 2026-08-07. Nothing built.** Asked for: `/talk` should ask "which
אמנון?" when a question is ambiguous, and use only one of them when it is
specific.

**The retrieval change cannot be built yet, and the reason is not in
retrieval.** `/talk` is not conflating two people — the archive is. There is
one entity row, and it already holds both of them.

---

## 1. What the archive actually contains

```
ENTITY 'אמנון'   normalized 'אמנונ'   ONE row
  seg fd3237f1 — "חבר טוב שהכיר הדובר בשירות הצבאי"        friend
  seg 1ffc53b7 — "חבר של הדובר שעשה איתו השלמות בגרויות"   friend
  seg 43cb70bb — "דודו של הדובר מצד אבא, שני ילדים בר ודור" UNCLE
```

Three mentions of one row. Two describe a friend from the army, one describes
an uncle. The per-mention summaries are correct and distinguish them perfectly
— they are just hanging off a single entity.

And that entity is in the family tree as the **uncle**:

```
אמנון -aunt_uncle-> Tal Nahum   (recording)
אמנון -sibling->    צבי          (confirmation)
בר    -child->      אמנון
דור   -child->      אמנון
```

So clicking the uncle in the tree offers the friend's army recordings as
moments. That is the same defect the question describes, one layer below where
it appeared.

## 2. Why it happened, and why nothing could have stopped it

`UNIQUE (producer_id, normalized_name)` is the merge rule. Both people are
literally `אמנון`, normalising to `אמנונ`, so **the schema cannot hold them as
two rows.** The second recording's `אמנון` merged onto the first by
construction.

`_apply_entity_resolutions` has said so since the Postgres migration:

> The opposite answer — same name, DIFFERENT person — is deliberately not
> handled here, and cannot be: the merge key is the name, so two entities with
> one name need a distinguishing name before Postgres can hold them apart.

### 2.1 The confirmation screen accepts an answer it cannot honour

This is the part worth fixing first, and it is a silent-discard of exactly the
kind found repeatedly this week.

When the second `אמנון` was extracted, the identity question offered
**"Someone new, not listed"**. Choosing it sets `same_as_uuid = None`, so
`_apply_entity_resolutions` leaves the name as `אמנון` — and
`write_segment_entities` then merges it onto the existing row by the merge key.

**The producer says "this is a different person" and the archive records the
opposite.** No error, no warning, and the screen reports success.

## 3. So the fix order is inverted from the request

| # | | why it must come first |
| --- | --- | --- |
| **1** | Let the archive HOLD two people with one name | Retrieval cannot distinguish what the database merged. Everything else is built on this. |
| **2** | Repair the existing `אמנון` | The tree is currently wrong about a real person. |
| **3** | Teach retrieval to use the distinction | The feature actually asked for. |

Building step 3 first would produce a prompt that asks "which אמנון?" and then
finds one entity covering both — a clarifying question with no answer behind it.

## 4. Step 1 — a distinguishing name at capture

The design already anticipated this: two entities with one name need a
distinguishing name **before** they can be stored. So when the producer answers
"someone new" and the name collides with an existing entity, the screen must
ask for something to tell them apart:

```
You already have an אמנון — your uncle, אמנון (בר and דור's father).
Is this the same person?
   ( ) Yes, the same
   ( ) Someone different — what should I call them?
       [ אמנון נחום            ]   ← required, and must not collide
```

Notes that matter:

- **The typed name is what gets stored**, so the merge key separates them for
  every future recording. This is the same mechanism a confirmed identity uses,
  run in the other direction.
- **It must be required.** "Someone different" with no name is the state that
  cannot be represented — which is what happens silently today.
- **Collision must be checked** against the producer's entities before writing,
  or the second name merges too.
- The producer names the NEW person, not the old one, so no existing row is
  renamed and nothing already in the tree moves.

## 5. Step 2 — repairing the existing אמנון

Not a migration; a one-off with the producer present, because only they know
which mention is whom. The three mentions are already labelled by their
summaries, so the repair is: create `אמנון נחום` (or whatever they call the
friend), move `fd3237f1` and `1ffc53b7`'s mentions to it, and leave `43cb70bb`
— the uncle — on the original row with its tree edges intact.

Worth doing through a script that shows each mention's summary and asks, rather
than guessing from the text.

## 6. Step 3 — the retrieval change, and how it would be measured

Only meaningful once two rows exist.

### 6.1 Mechanism

**Not** the entity map. That block is already in the prompt and is inert —
measured this session: real map / no map / both-names-merged produced
byte-identical unit selections across two runs each. Adding to a block the
model ignores would change nothing.

The distinction lives in `entity_mentions`: each mention points at exactly one
entity, so the archive knows which אמנון each RECORDING means. So annotate the
transcript block itself, where the model is already reading:

```
RECORDING 3 — interview question: … [mentions: אמנון (your uncle)]
  u12 …
RECORDING 7 — interview question: … [mentions: אמנון נחום (army friend)]
  u20 …
```

Plus a new optional output alongside `unit_ids` and `follow_up`:

```json
{"unit_ids": [], "clarify": {"question": "…", "options": ["…", "…"]}}
```

`clarify` is chat text only, like `follow_up` — never spoken, never in the
video. Answering it re-asks through the normal path, so the second turn gets
the same validation and assembly as any question.

### 6.2 What the eval needs BEFORE the prompt changes

The scored set is 7 questions with no ambiguous name, so today a prompt change
cannot be shown to fix anything. Four cases are needed, and three of them are
about NOT clarifying:

| case | question | expected |
| --- | --- | --- |
| genuinely ambiguous | "ספר לי על אמנון" | `clarify`, no units |
| specific by name | "ספר לי על אמנון נחום" | units from the friend's recordings ONLY |
| resolved by context | "מה אמנון עשה בצבא איתי" | units from the friend, **no clarify** |
| resolved by history | ask the friend question, then "ומה עוד?" | units from the friend, **no clarify** |

The third and fourth are the ones that catch the failure in §6.3.

### 6.3 The over-asking risk, and how to catch it before shipping

The risk named in the request is real and is the likeliest way this makes
things worse: a model taught to ask "which one?" starts asking when the answer
was obvious.

**It is measurable with the eval that already exists, at no cost.** Every one
of the 7 scored questions and the 12 comparison questions is unambiguous, so:

> **Clarification rate on the 19 existing questions must be exactly 0.**

That is a hard gate, not a judgement call, and it runs before any of the new
cases matter. A prompt that handles "which אמנון?" beautifully and clarifies on
2 of 19 unambiguous questions fails.

Per CLAUDE.md, each arm runs **multiple times** — the archive-read call is
non-deterministic on marginal judgements, and "asked for clarification" is
exactly the kind of marginal judgement that will not be stable at n=1. Three
runs per arm minimum, reporting the rate rather than a single pass/fail.

Two further guards worth having:

- **A no-clarify control**: an archive with no duplicate names at all must
  never produce `clarify`, whatever is asked.
- **Report which entity was chosen**, not just the units, so "used only the
  friend" can be asserted directly instead of inferred from unit ids.

### 6.4 Honest expectation

The entity map has measured **0.9987 accuracy with and without**, identical on
all seven questions, and PROJECT_STATUS carries an open decision about dropping
it. That is the track record of the last attempt to make the model use entity
data. Inline annotation is a materially different intervention — it sits where
the model is already reading rather than in a side block — but it should be
approached expecting it may not work, with the measurement built first.

## 7. Open decisions

| # | Decision | Recommendation |
| --- | --- | --- |
| 1 | Does `clarify` block the answer, or accompany a best guess? | Block. A guess plus a question is the conflation this exists to remove. |
| 2 | Should the tree show the two אמנונs distinctly once split? | Yes, automatically — two rows are two nodes, no tree change needed. |
| 3 | Repair the existing אמנון before or after step 1 ships? | After. The repair uses the same "distinguishing name" path, so build it once. |
| 4 | v1 `video_clips`? | Untouched, as asked. It consumes entities directly (`find_segments_mentioning`) and would benefit for free from the split in step 1. |
