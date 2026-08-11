# One correction tool, for both kinds of fix

**Rewritten 2026-08-06. Supersedes the two-tool plan.** The chunk-level
correction shipped (`transcript_correction.correct_token`) and currently
refuses any edit that changes the word count. This extends it to accept those
too, in the same endpoint and the same UI, rather than adding a second editor
with different rules.

**Verdict: merging is straightforward, and better than two tools.** There is no
structural reason to keep them apart. What differs is a CONSEQUENCE of each
edit, which should be shown after the fact — not a mode the producer has to
choose in advance.

---

## 1. Why it merges cleanly — verified, not assumed

`_split_segment_into_units` builds a unit's text from `word_timestamps`, and
falls back to `chunk.text` ONLY when a chunk has no timings at all. So when
timings exist, `chunk.text` is not what `/talk` reads.

Confirmed against the live archive, in a rolled-back transaction: rewriting
chunk 0's text to *"יש לי חמישה אחים בדיוק"* and rebuilding units produced

```
u1: 'אנחנו חמישה משפחה'   [2.32-4.56]     <- unchanged, from word_timestamps
```

So the two behaviours the producer wants already fall out of which fields an
edit touches. No new mechanism is needed — only a rule about when to touch
which.

## 2. The rule

> **The archive's understanding always updates. Your family's answers update
> only when the words still line up one-to-one.**

| edit | `RawSegment.transcript` | `chunk.text` + `word_timestamps` | reaches |
| --- | --- | --- | --- |
| same word count (`יוכרת` → `יוכבד`) | rewritten | rewritten, timings kept, window re-embedded | extraction **and** `/talk` |
| word count changes (`אנחנו חמישה משפחה` → `יש לי חמישה אחים`) | rewritten | **untouched** | extraction only |

### 2.1 Why a count-changing edit touches NO chunk, rather than just no timings

This is the one place I would not do what was proposed. The suggestion was to
rewrite `chunk.text` and leave the added words without timestamps. That works,
and two things make "leave the chunks entirely alone" better:

- **There is no principled answer to WHICH chunk gained the words.** "אנחנו
  חמישה משפחה" may sit in one chunk, or straddle two. Inserting "יש לי" means
  choosing a chunk for words that were never spoken in any of them.
- **Chunk text feeds chunk EMBEDDINGS**, which the avatar path ranks on
  (v1 `video_clips` also did, until its 2026-08-12 removal). Adding unspoken
  words there makes a clip retrievable *because of words it does not
  contain* — a subtler version of exactly the mismatch this whole design
  avoids. The current producer is on `video_clips_v2`, which does not use
  those embeddings, so the hazard is latent rather than live — which is the
  best time to close it.

Leaving chunks untouched keeps every chunk a faithful record of the audio, and
confines the producer's rewording to the field that drives understanding.

### 2.2 What the producer sees

The distinction is never presented as a choice. It is reported as an outcome,
once the edit is saved:

```
✓ Saved. Your family will hear the original wording in this clip —
  the archive now understands it as "יש לי חמישה אחים".

✓ Saved and corrected everywhere, including what your family hears.
```

## 3. The faithfulness warning

Shown beside the editor, always — not as a blocking dialog:

> **Keep this faithful to what you said.** Rewording so the archive understands
> you properly is exactly what this is for — it is how people and relationships
> are worked out. But if you want to say something *different*, delete this
> recording and record it again. Your family always hears the original.

Held as one constant in the codebase rather than typed into a component, so
the endpoint's own docs and the UI cannot drift apart on what the tool is for.

## 4. What this replaces

`docs/TREE_EDITING.md` stays unbuilt and stays on file. The cases it covers
that this does not are unchanged: a relation no recording states at all, and a
relation between people named in two different recordings. Neither is
reachable by editing one transcript, and neither is urgent.

The "second tool" (structure-only transcript editing) is **cancelled** — it is
now this tool's count-changing branch.

## 5. Scope of the change

Small, because the hard part shipped:

| # | Work |
| --- | --- |
| 1 | `correct_token` gains `allow_reword`; when counts differ it rewrites `RawSegment.transcript` only and re-embeds the segment |
| 2 | The refusal becomes conditional — same message, only when the caller has not opted in |
| 3 | Result reports which kind of edit happened, so the UI can say the right sentence (§2.2) |
| 4 | Endpoint passes it through; the `ready`/`analyzed` guard and the 404 rule are unchanged |
| 5 | The faithfulness copy as a shared constant |
| 6 | UI: an editable transcript in the extraction panel, the warning, and the outcome message |

Tests to add: a count-changing edit leaves chunks and timings untouched;
`RawSegment.transcript` and the segment embedding do change; units built after
one are byte-identical to before; and the same-count path keeps every existing
behaviour.

## 6. The durability hazard, stated plainly

**`scripts/reingest_archive.py` clears `segment.transcript` and
re-transcribes, which rebuilds chunks from scratch.** Every correction — the
one already shipped as much as anything added here — is destroyed by a re-ingest,
silently and with no record it existed.

That is a pre-existing consequence of the shipped feature, not something this
change introduces, and it is the strongest argument for a corrections LOG:

```sql
segment_corrections(id, segment_id, old_text, new_text, kind, created_at)
```

Written on every correction, replayed after a re-transcription. It also buys
provenance — "this word was corrected by the producer" is worth knowing when
reading a transcript years later.

**Not in this change**, but it should be next, and it should not wait for a
re-ingest to be scheduled before anyone remembers.

## 7. Open decisions

| # | Decision | Recommendation |
| --- | --- | --- |
| 1 | Should a count-changing edit be opt-in per request, or just allowed? | Opt-in flag from the UI, so a same-count typo cannot silently become a reword when a word is accidentally deleted. |
| 2 | Re-run extraction automatically after a reword? | No — explicit, for the reason already settled: it costs several LLM calls and raises confirmation questions. |
| 3 | Should the extraction panel show that a transcript was edited? | Yes, once the corrections log exists. Until then there is nothing to show it from. |
| 4 | Cap the size of a reword? | No cap, but the warning copy carries the intent. A producer rewriting a whole answer should be told to re-record, not blocked by a character count. |
