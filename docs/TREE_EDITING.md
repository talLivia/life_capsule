# Editing relations from the family tree

**Written 2026-08-06. Nothing built.** A permanent correction path: click a
person on the tree — floating or placed — and set who they belong to, at any
time, not only in the one-shot confirmation screen after a recording.

This closes the "no way back" gap flagged earlier: extraction proposals are
one-shot, and today nothing lets a producer add or fix a relation later. The
extraction panel can already REMOVE a relation; nothing can add one.

---

## 1. The governing rule: a manual edit always wins

**A relation chosen by hand from the tree REPLACES whatever contradicts it. No
dialog, no warning, no silent drop.**

This is the decision that shapes everything below, and it is the same rule the
confirmation screen already follows. When a producer answers the parentage
question by naming somebody who is not one of their own parents, the sibling
relation is not kept alongside the new parent edge — it is replaced, because
the two cannot both be true and the producer just said which one is
(`human_confirm_node`, "the parentage answer OWNS whether they are a sibling").

A tree edit is a stronger signal than that: it is unprompted, deliberate, and
made while looking at the tree the edit will change. Asking "are you sure?"
would be asking someone to confirm the thing they just went out of their way
to say.

### 1.1 What "replaces" means precisely

Setting **"חן is the child of רז"** when חן currently has `sibling → SELF`:

1. write `רז -parent-> חן`;
2. delete the sibling edge between חן and the producer;
3. both in ONE transaction, so the tree is never momentarily contradictory.

The rule generalises: **delete the relations that cannot coexist with the new
one, and nothing else.** Concretely, for a new `parent` edge onto a person, the
conflicting set is the edges that place them in a different generation
relative to the same anchor — a `sibling` to the producer, a `grandparent`, an
`aunt_uncle`. A `spouse` edge is untouched, because it says nothing about
generation and can be true at the same time.

### 1.2 What this deliberately does NOT do

- **It does not warn.** §1's whole point.
- **It does not silently keep both.** That is the איציקו bug: two true-looking
  edges the tree cannot honour, one dropped at render, looking exactly like the
  edit failing to save.
- **It does not touch relations between two OTHER people.** "חן is רז's child"
  says nothing about who רז's parents are.

---

## 2. Answers to the three scoping questions

### 2.1 How much is reusable?

More backend than frontend, and the reusable parts are the ones that took the
longest to get right.

| piece | verdict |
| --- | --- |
| `EntityRelation`, the FK vocabulary, `is_tree_edge`, `generation_delta` | reuse unchanged |
| `entity_store.people_for_correction` | **reuse unchanged** — it already returns every person in the archive, which is exactly this picker |
| `entity_store.get_relation_vocabulary` | reuse unchanged |
| `DELETE /relations/{id}` | already exists (the extraction panel's Remove) |
| `PersonSelect` + the type `<select>` from "Not quite — fix this" | reuse the components; new container |
| `family_tree.build_tree` | reuse — the edit refetches it |
| `human_confirm_node`'s acceptance/correction logic | **not reusable** — it is graph state, tied to a paused pipeline |
| The conflict rule (§1.1) | **new**, and it is the only real logic to write |
| Tree node click handler | new — today it opens the moments modal |

Roughly 60% reuse. The genuinely new surface is one endpoint and one panel.

### 2.2 Does it reuse `EntityBatchConfirmRequest`? **No.**

That request is *answers to a paused pipeline*. It is validated against one
segment's stored `pending_confirmation`, every key names a question that
segment asked, and submitting it RESUMES a LangGraph thread. A tree edit has no
segment, no pause and no thread. Forcing it through would mean fabricating a
pending payload for a recording that is not waiting on anything — inventing a
second meaning for a field that already has one, which is the shape this
codebase has been bitten by repeatedly.

So: its own endpoint.

```
POST /entities/{entity_id}/relations
    { "relation_type": "parent", "other_entity_id": "...", "direction": "incoming" }
    -> 200 { "written": {...}, "replaced": [ ...deleted relations... ] }
```

`replaced` is returned rather than swallowed. The producer is not asked in
advance (§1), and they should still be able to SEE what the edit did — a
toast saying "חן is now רז's child; the sibling link to you was replaced" is
honest without being a dialog.

Validation reuses the confirmation screen's rules, because they are the same
rules: the type must be one `get_relation_vocabulary` returns, both endpoints
must be entities of this producer, and the two must differ
(`ck_entity_relations_not_self`).

### 2.3 Does it re-create the silent-contradiction risk?

**No — because §1 removes the mechanism rather than guarding it.** The
contradiction only existed while two conflicting edges could both be stored;
if the new edge deletes what conflicts with it, there is never a pair for the
tree to choose between.

Two things it DOES introduce, both worth naming:

**Ask-once stamps do not gate direct writes.** `parentage_asked_at` and
`side_asked_at` gate the QUESTIONS. A tree edit can overwrite an answer the
producer already gave, which is correct — it is a later, more deliberate
statement — but it means the stamp no longer describes the current state. The
edit should therefore also SET the relevant stamp, so the question does not
come back later and contradict the edit.

**Re-analysis can resurrect a replaced relation.** `write_segment_relations`
replaces a segment's relations scoped to `origin="recording"`, so re-analysing
the recording that first said "חן is my sister" would rewrite that edge — and
the manual edit would lose, silently, exactly the shape this document exists to
prevent. Mitigation: **a manual edit writes `origin="manual"`**, and
`write_segment_relations` must not re-create a `recording` edge that a `manual`
edge contradicts. This is the one part of the design that touches ingestion,
and it must not be skipped.

---

## 3. `source_segment_id` is nullable, and what that costs

Every tree edge today carries the recording where it was said, which is what
lets the tree offer to play the moment. A tree-authored edge has no such
recording.

- Make `entity_relations.source_segment_id` **nullable** (migration).
- The tree's "play the moment" affordance must handle its absence — showing
  nothing rather than a dead control.
- `origin` already distinguishes `recording` from `confirmation`; add
  `manual`. A producer looking at their tree can then be told *why* an edge
  exists: something they said, something they confirmed, or something they set
  by hand.

**The deletion consequence, which is not obvious.** `source_segment_id`
cascades — deleting a recording deletes the relations it established. A manual
edge with a NULL source is therefore permanent: it survives the deletion of
every recording about that person. That is the correct behaviour for a
hand-made statement, and it must be deliberate rather than discovered, because
it breaks the current invariant that deleting a recording removes everything it
produced.

---

## 4. The UI

Reachable from the tree, for any person — not only floating ones. A wrong
relation is at least as worth fixing as a missing one, and restricting the
control to unplaced people would make the fix unreachable exactly when the tree
looks confidently wrong.

```
┌─ חן ────────────────────────────── not yet placed ──┐
│  Moments  ·  4 recordings mention חן                │
│                                                     │
│  How is חן related?                                 │
│    [ child        ▾ ]  of  [ רז            ▾ ]      │
│                          every person in the archive │
│                                                     │
│  ⓘ חן is currently your sibling. Saving replaces    │
│    that with this.                                  │
│                              [ Cancel ]  [ Save ]   │
└─────────────────────────────────────────────────────┘
```

The ⓘ line is **not a confirmation dialog** — it is a statement of what the
button will do, shown before the click, on the same screen. §1 rules out
interrupting the producer to ask; it does not rule out telling them.

---

## 5. Build order

| Phase | Work |
| --- | --- |
| **1** | Migration: nullable `source_segment_id`, `origin='manual'` |
| **2** | `POST /entities/{id}/relations` with the §1.1 conflict rule + validation |
| **3** | Guard `write_segment_relations` against overwriting a manual edge (§2.3) |
| **4** | Tree panel: the edit control, the picker, the replacement notice |
| **5** | Set the ask-once stamps on manual edit (§2.3) |

Phase 3 is not optional and not last-if-time-permits: without it, the first
re-analysis silently undoes the feature.

## 6. Open decisions

| # | Decision | Recommendation |
| --- | --- | --- |
| 1 | Can a manual edit DELETE a relation with no replacement? | Yes — the extraction panel can already remove one, and the tree is where you notice. |
| 2 | Should the conflict set be computed from `generation_delta`, or listed? | Computed. A relation type added to the table later would otherwise be missing from a hardcoded list, which is the drift the lookup table exists to prevent. |
| 3 | Show `origin` on the tree? | Not as a badge on every edge. Surface it in the person's panel, where "you set this by hand" is useful context. |
| 4 | Does a manual edit need a confirmation for DESTRUCTIVE cases (replacing several edges at once)? | No — §1. But the ⓘ notice should list all of them, not just the first. |

## 7. Not in scope

- Creating a person who is not already an entity. Every picker option is an
  existing entity; inventing people from the tree is a separate feature.
- Editing years, names or types from the tree — see
  `docs/TIMELINE_YEAR_ATTRIBUTION.md` for years.
- Photos — see `docs/MEDIA_GALLERY.md`.
