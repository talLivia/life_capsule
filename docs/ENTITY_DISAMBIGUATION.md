# Two people, one name

**Written 2026-08-07. All three steps SHIPPED 2026-08-08.** Asked for: `/talk`
should ask "which אמנון?" when a question is ambiguous, and use only one of
them when it is specific.

**The retrieval change could not be built first, and the reason was not in
retrieval.** `/talk` was not conflating two people — the archive was. There was
one entity row, and it held both of them. Everything below was written before
anything was built; §8 records what actually happened, including the two places
this document was wrong.

## Status

| step | | |
| --- | --- | --- |
| **1** | Let the archive HOLD two people with one name | shipped — two commits, see §8.1 |
| **2** | Repair the existing `אמנון` | done by the producer, NOT by the script this document proposed (§8.2) |
| **3** | Teach retrieval to use the distinction | shipped — measured, §8.3 |

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

## 7. Open decisions (as written, before building — see §8 for outcomes)

| # | Decision | Recommendation |
| --- | --- | --- |
| 1 | Does `clarify` block the answer, or accompany a best guess? | Block. A guess plus a question is the conflation this exists to remove. |
| 2 | Should the tree show the two אמנונs distinctly once split? | Yes, automatically — two rows are two nodes, no tree change needed. |
| 3 | Repair the existing אמנון before or after step 1 ships? | After. The repair uses the same "distinguishing name" path, so build it once. |
| 4 | v1 `video_clips`? | Untouched, as asked. It consumes entities directly (`find_segments_mentioning`) and would benefit for free from the split in step 1. |

---

# 8. What actually happened

Written 2026-08-08, after all three steps shipped. Kept in the same file as the
plan so the two can be read against each other — including where the plan was
wrong.

## 8.1 Step 1 took TWO changes, not one

§2.1 found the confirmation screen accepting an answer it could not honour:
"someone new" about a colliding name left the name unchanged, and the merge key
IS the name, so the archive recorded the opposite of what was said. That was
fixed first — `new_name`, required exactly when the extracted name's merge key
collides, and rejected if it collides with anything else in the archive.

**It did not close the case that produced the אמנון conflation**, and the plan
did not see this. `check_entities_node` auto-resolved whenever exactly one
candidate matched VERBATIM, so for two people both called exactly `אמנון` the
identity question was never asked at all — the validation above was only
reachable with 2+ candidates. The producer chose always-ask over a post-hoc
split tool, so a verbatim match now raises the question, gated by
`identity_asked_at` (migration 0021) so it fires once per person rather than
once per mention. Deliberately not backfilled: stamping the 28 existing rows
would have made the change invisible on exactly the archive it was built for.

Two live defects surfaced on the way, both older than this work:

- Every "someone new" answer crashed with an `UnboundLocalError` —
  `corrected_name` was called from the identity loop while `name_edits` was
  defined below it.
- **Identity questions had been asking nothing in words since 64eef15.**
  `_confirmation_question` was called from the per-name interrupt; when chunk 4
  batched them, `names_to_check` began passing straight through, and the modal
  rendered an empty legend above the options. It survived because the options
  still read sensibly on their own.

## 8.2 Step 2 was done by deletion, not by the repair script

§5 proposed a script to move two mentions onto a new row. The producer chose
instead to delete the affected recordings and re-upload them, letting step 1
raise the question naturally on the second אמנון. It worked: the archive now
holds `אמנון` (army friend, 2 recordings) and `אמנון נחום` (uncle, 1 recording,
carrying the `aunt_uncle` and both parent-of edges).

**The blast radius of that deletion was larger than first reported, and the
under-report is the lesson.** Deleting a recording cascades twice — once from
`raw_segments` via `source_segment_id`, and once from `entities` via
`from_entity_id`/`to_entity_id` when the orphan sweep removes a person nobody
mentions any more. The second path takes MANUAL edges with it, which migration
0020's docstring calls permanent ("it survives deleting every recording about
that person"). It survives the segment cascade; it does not survive the
person's last mention being deleted. Counting only the first path put the
figure at 7 relations when it was 15, 9 of them hand-placed from the tree.

## 8.3 Step 3 — measured, and the gate was not sufficient

Built as §6.1 proposed except for placement: the tag is written INLINE next to
each mention rather than on the recording header, at the producer's request.

Three deviations from the plan, each forced by a measurement:

**A stricter confusability rule than the confirmation screen's.**
`names_are_similar` includes a character-similarity fallback for spelling
variants, which grouped `אירה` (ניר's wife) with `יאיר` (ניר's child) — three
shared letters. Right when a false positive costs one question a human answers
in a second; wrong when it costs asking a LISTENER to choose between two people
nobody could confuse. The rule is now "same name, or one a more specific
version of the other".

**The prompt is byte-identical when no two people share a name.** Both the tags
and the instruction appear only when `confusable_entities` returns something,
and `clarify` is ignored outright otherwise. §6.3's "no-clarify control" is
therefore structural rather than measured — an archive without duplicates
cannot over-ask, because it is handed the same bytes as before the feature
existed. Asserted by a test, because "empty placeholder leaves an extra
newline" is invisible in review and would silently invalidate the arm.

**THE GATE PASSED WHILE THE FEATURE WAS BREAKING ANSWERS.** §6.3 proposed the
zero-clarification gate as the hard precondition. It is necessary and it is not
sufficient. The first version placed the `clarify` JSON form at the end of the
Rules section, immediately after "if nothing answers the question, output an
empty unit_ids" — two consecutive empty-answer examples just before the
transcript. Measured: `school` fell from 8 units to 0 and one same-name case
from 4 to 0, both 3/3, returning EMPTY SELECTIONS rather than clarifications.
Neither question involved an ambiguous name. Clarification rate: 0. Gate: pass.

What caught it was diffing the SELECTED UNITS between arms on the unambiguous
questions — a check this document did not ask for and should have. It lives in
`scripts/eval_name_disambiguation.py` and should stay there. Moving the JSON
form inside the disambiguation block fixed it.

### Final measurement (12 questions x 3 runs x 2 arms, plus 5 same-name cases)

```
GATE   0 clarifications across 12 questions x 3 runs        PASS
CASES  ambiguous            clarify 3/3
       specific-by-name     uncle only,  3/3
       specific-by-role     uncle only,  3/3
       resolved-by-context  friend only, 3/3, no clarify
       resolved-by-history  friend only, 3/3, no clarify     5/5
```

On "19": §6.3 says 19 existing questions (7 scored + 12 comparison). They are
not disjoint — the 7 scored are a SUBSET of the 12. The real gate is 12
distinct questions, and is reported as 12.

`resolved-by-context` was first written as "מה אמנון עשה בצבא?" and that case
was mis-specified: the archive says the speaker was in the air force, served
three years, and has a friend אמנון from there. Nothing says what אמנון did. A
case whose answer is not in the archive measures over-reach, not
disambiguation — the pre-feature arm "passed" it by returning the SPEAKER's own
service in answer to a question about someone else.

### The one accepted cost — and what actually causes it

`army-narrow` ("באיזה תפקיד שירתת בצבא?") broadens from `u11,u12` to `u11-u14`,
picking up "I had good friends there" and "there's אמנון, still my friend".

**THE FIRST EXPLANATION HERE WAS WRONG.** It said the tag makes אמנון salient
enough to pull those units into a role question. That was asserted, not
measured, and it does not survive the obvious objection: `u13` is
"והיה לי שם חברים טובים שהכרתי" — it contains no name at all, carries no tag,
and is byte-identical in both arms. A tag on `u14` cannot explain `u13` moving.

Isolated properly, n=6 per condition, tag and instruction varied independently:

```
1  no tag, no instruction          u11,u12       6/6
2  no tag, INSTRUCTION only        u11-u14       6/6   <- broadens
3  inline tag, no instruction      u11,u12       6/6   <- tag alone: NO effect
4  inline tag + instruction        u11-u14 5/6, u11,u12 1/6
5  header tag + instruction        u11,u12       6/6   <- suppresses it

control "מה עשית בצבא?"            u11-u14 in all five conditions
```

So the cause is the INSTRUCTION TEXT, not the annotation. The tag has no
measurable effect on this question in either direction; the block does it on
its own, and the header placement happens to cancel it.

Two corrections to what was reported before this was isolated: condition 4 is
NOT stable (5/6, not 5/5 — a wider n found the variance), and the placement
choice was made on a mechanism that turned out to be wrong.

**The cause is the block's own EXAMPLE, and that is confirmed.** The text
reads: names their role ("my uncle", "my friend from the army"). `army-narrow`
asks what role you served in. Swapping four words, tags on in both arms:

```
army-narrow   example "my friend from the army"   u11-u14   6/6
              example "my neighbour"              u11,u12   6/6
army control  both examples                       u11-u14   6/6
```

So the phrase primes "I had good friends there" into being read as
role-relevant. Nothing to do with אמנון, who does not appear in the question.

**And the fix costs more than the bug.** Confirmed at n=4 per case per arm,
with retries and a hard failure on exhausted retries — an earlier pass on this
same comparison was destroyed by 429s that fail-soft turned into "0 units, no
clarify", which reads exactly like a result:

```
                       "my friend from the army"        "my neighbour"
ambiguous              clarify 4/4                      clarify 4/4
specific-by-name       uncle,  5 units                  uncle,  5 units
specific-by-role       uncle,  5 units                  uncle,  5 units
resolved-by-context    friend, 4 units                  friend, 4 units
resolved-by-history    friend, 8 units                  friend, 8 units
army-narrow            u11-u14  (broadened)             u11,u12   FIXED
school                 8 units                          0 units   LOST
brothers / family / army                identical in both arms
```

`school`'s eight units are genuinely about schooling — "I studied at Erlich
school from grade 1 to 6, then moved to Tachkemoni, then to Hakfar Hayarok" —
so 0 is a real loss, not a stricter reading of a question the archive cannot
answer.

**Every variant measured costs exactly one question, and it is always the same
two.** Four configurations tried: shipped inline (`school` 8, `army-narrow` 4),
neutral example (`school` 0, `army-narrow` 2), §6.1 header placement (`school`
0, `army-narrow` 2), and clarify-rule-at-end-of-Rules (`school` 0, plus worse).
The shipped one is the only one that keeps `school`, and losing a whole answer
is worse than gaining two units on a narrow question.

Note what the same table shows about the feature itself: all five same-name
cases are IDENTICAL across both examples. The disambiguation behaviour is
insensitive to this wording; only the collateral moves. So the wording is a
pure collateral-damage knob, and the shipped setting is the best of four
measured.

**Do not reword this block without re-running both sides.** The example phrase
is load-bearing in a direction nobody would predict from reading it, and three
of the four configurations tried silently destroyed an unrelated answer while
leaving the clarification gate green.

## 8.4 Unrelated defect found while tracing, NOT fixed

`question_index` restarts per interview CATEGORY, but take-grouping keys on it
alone (`_group_siblings`). The live archive therefore presents three unrelated
questions as one answer given in three sittings:

```
idx=1  "tell me about your father"  (childhood)  take 1 of 3
idx=1  "your roles in the army"     (military)   take 2 of 3
idx=1  "post-secondary studies"     (academic)   take 3 of 3
```

...and the prompt instructs the model to read takes together, with the first
one's interview question applying to all of them. `question_id` carries the
real identity. This predates all of the above, affects every question rather
than only ambiguous ones, and fixing it would move the baseline the numbers in
§8.3 were measured against — so it was left alone rather than folded into an
unrelated change.
