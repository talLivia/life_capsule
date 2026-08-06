# Editing a recording's transcript, and re-processing it

**Written 2026-08-06. Nothing built.** Scoping the proposal to make
`RawSegment.transcript` editable and re-runnable, so a correction feeds the
existing extractor rather than needing a separate tree-editing feature.

**Headline: the two questions asked have inverted answers.** Adding words that
were never said is *structurally* safe in a way that policy could not achieve —
and correcting a genuine mishearing is *less* effective than it looks, because
it does not reach the surface that plays the clip.

---

## 1. What re-processing actually touches — traced, not assumed

### 1.1 The transcript is stored TWICE, and the copies feed different things

| stored where | fed by | consumed by |
| --- | --- | --- |
| `RawSegment.transcript` | STT, once at ingest | entity extraction, relation proposal, topic tags, the segment embedding, the extraction panel |
| `TranscriptChunk.text` + `word_timestamps` | the same STT pass | **everything `/talk` does** |

This is the hazard `PROJECT_STATUS.md` already records from the Graphiti era:
*"The transcript is stored twice and the copies can drift. חיל האוויר survived
in the graph on a transcript that no longer existed in Postgres."* Editing one
copy institutionalises exactly that drift.

### 1.2 `/talk` never reads `RawSegment.transcript`

Traced through `full_archive_retrieval`:

- `_load_archive` selects segments **and their chunks**, and drops any segment
  with no chunks: *"A segment with no chunks yet contributes nothing readable."*
- `_split_segment_into_units` builds every unit from `word_timestamps`:
  `text=" ".join(w for w, _, _ in words)`, `start_sec=words[0][1]`,
  `end_sec=words[-1][2]`.

So **every word `/talk` displays, and every second of video it plays, comes
from `word_timestamps`** — never from `RawSegment.transcript`.

### 1.3 Re-processing does NOT rebuild chunks

`transcribe_node` short-circuits when a transcript already exists and returns
`{"transcript": ...}` with **no `phrases`** — and its own comment says what
follows: *"create_transcript_chunks_node creates no chunks for it this pass."*

So a re-run over an edited transcript touches:

| | re-run? |
| --- | --- |
| entity extraction + relation proposals | ✅ re-extracted from the edited text |
| topic tags | ✅ re-tagged |
| `RawSegment.embedding` | ✅ re-embedded from the edited text |
| importance score | ✅ re-scored |
| entities / mentions / relations | ✅ **replaced** (`entity_store` replaces per segment; relations replace scoped to `origin="recording"`) |
| **chunks, `word_timestamps`, units** | ❌ **untouched** |
| **what `/talk` shows and plays** | ❌ **untouched** |

Confirmed idempotent by design — `entity_store` replaces a segment's mentions
rather than appending — so re-processing is not a duplicating operation.

---

## 2. Case 2 — adding words that were not said

### 2.1 It cannot corrupt a clip, and that is structural

A unit's text and its play range both come from `word_timestamps`. **A word
that was never spoken has no timestamp**, so it cannot enter a unit, cannot be
displayed, and cannot extend a clip. There is no code path by which invented
text reaches `/talk`.

That is a much stronger guarantee than a rule saying "don't do that" — the
feared failure ("the words on screen won't match the words in the video") is
not merely discouraged, it is unrepresentable.

### 2.2 So what does an added sentence actually do?

It changes what the **extractor** reads, which is precisely the intent. Editing
*"אנחנו חמישה משפחה"* to *"יש לי חמישה אחים"* and re-processing would produce
the sibling relations the never-infer rule correctly declined to guess — and
`/talk` would carry on quoting the original words, because that is what was
said.

The transcript stops being a record of what was said and becomes **a record of
what was meant, used to derive structure**. That is a real change of kind, and
it should be visible rather than implied.

### 2.3 Two consequences that do need handling

**An edit is silently destroyed by re-ingestion.**
`scripts/reingest_archive.py` *clears* `segment.transcript` and re-transcribes
— its docstring says it must, or the run is "a silent no-op reusing the bad
text". Any edit is wiped, with no warning and no way to tell it ever existed.
This is the single most important thing to handle: an edit must be durable, or
recoverable, across a re-transcription.

**The extraction panel would show words the audio does not contain.** Whoever
reads it later — a grandchild, or the producer in five years — has no way to
know which sentence was spoken and which was typed.

Both point the same way: **store the edit, do not overwrite the original.**

```
raw_segments.transcript            -- STT output, never edited
raw_segments.transcript_edited     -- nullable; the producer's version
raw_segments.transcript_edited_at
```

Extraction reads `COALESCE(transcript_edited, transcript)`. `/talk` is
unaffected either way. Re-ingestion replaces `transcript` and leaves the edit
alone — the bug in §2.3 disappears rather than being mitigated. And the
extraction panel can show both, marking the edited sentences honestly.

---

## 3. Case 1 — correcting a genuine mishearing

### 3.1 This is the one that does NOT work as expected

Correcting `ליאן` → `אליאן` in the transcript fixes:

- the entity that extraction creates ✅
- the relations pointing at it ✅
- the topic tags and segment embedding ✅

and does **not** fix:

- `TranscriptChunk.text` ❌
- `word_timestamps` ❌
- therefore what `/talk` displays and searches ❌

So after the correction, the archive says `אליאן` in the tree and still says
`ליאן` in every clip. A family member asking about אליאן gets the moments only
because the ENTITY matches — the text `/talk` reads still carries the old
spelling, and would still be what appears on screen.

**Transcript editing therefore does not replace the name-propagation work
scoped earlier this session** — it is a different half of the same problem.
Offering it as a fix for mishearings would be worse than not offering it,
because the correction would look complete and be half-applied.

### 3.2 What case 1 actually needs

The chunk-level change already scoped earlier: rewrite the token inside
`TranscriptChunk.text` and inside the matching `word_timestamps` entry, keeping
its start/end, then re-embed the affected chunk. Feasible for a single token of
the same count; refused with a clear message otherwise, because a correction
that changes token count has no defined timing.

That is a separate, larger piece of work and should not be smuggled inside
this one.

---

## 4. Recommendation

**Allow editing. Do not restrict it to case 1, and do not require a "this was
not said" marker as a gate.**

- Case 2 is structurally safe for `/talk` (§2.1), so the risk it was feared for
  does not exist.
- Case 1 is not fully served by this feature at all (§3.1), so restricting to
  it would leave the feature doing nothing useful.

Instead:

1. **Keep the original.** `transcript_edited` alongside `transcript`, never in
   place of it. This is what makes the edit durable and honest, and it removes
   the re-ingestion bug rather than warning about it.
2. **Say what an edit is for**, in the UI: *"Rewrite this so the archive
   understands it correctly. What you type is used to work out people and
   relationships — your family will always hear the original recording."* That
   is the accurate description of what the two copies do, and it sets the
   expectation without a policy gate.
3. **Show both in the extraction panel**, with the edited version marked.
4. **Do not offer it as a spelling fix.** Name corrections keep going through
   the existing per-entity name edit, which lands on the entity. Direct people
   at the right tool rather than one that half-works.

## 5. Does this replace tree editing?

**Partly — and the remainder is the interesting part.**

| case | transcript editing | tree editing |
| --- | --- | --- |
| The recording says it; the extractor missed it | ✅ the right tool | overkill |
| The recording says it; the extractor got it wrong | ✅ re-process replaces the segment's relations | also works |
| A relation NO recording states ("חן is רז's child", never said aloud) | ✍️ requires inventing a sentence | ✅ the honest tool |
| A relation between people from two DIFFERENT recordings | ❌ no single transcript contains it | ✅ |
| Correcting an entity created by another recording | ❌ | ✅ |

So transcript editing covers the case that prompted it, and covers it better —
the extractor stays the single place relations are derived, and no second
write path into `entity_relations` is created. Tree editing remains the answer
for relations that no recording states and for anything spanning recordings.

**Recommendation: build transcript editing first**, use it for a while, and see
whether the remaining tree-editing cases still feel worth the second write
path. `docs/TREE_EDITING.md` stays as the plan for them, not deleted.

## 6. Build order

| Phase | Work |
| --- | --- |
| **1** | Migration: `transcript_edited`, `transcript_edited_at` |
| **2** | Extraction reads `COALESCE(transcript_edited, transcript)` — one change, in `check_entities_node` |
| **3** | `PATCH /segments/{id}/transcript` + `POST /segments/{id}/reprocess` |
| **4** | Extraction panel: edit the text, show both versions, re-process button |
| **5** | Progress + the confirmation questions the re-run raises (it pauses exactly as a first ingest does) |

Phase 5 is not an afterthought: re-processing goes through `human_confirm`, so
an edited recording will raise its questions again and they surface in the
bell. That is correct, and it should be expected rather than surprising.

## 7. Open decisions

| # | Decision | Recommendation |
| --- | --- | --- |
| 1 | Can a family member (`/talk`) ever see the edited text? | No. They hear the recording; the edit is a producer-side derivation aid. |
| 2 | Should re-processing be automatic on save, or an explicit button? | Explicit. It costs several LLM calls and raises questions — a save that quietly does that is a surprise. |
| 3 | Keep an edit history, or just the latest? | Latest. The original is always preserved separately, which is the version that matters. |
| 4 | What if the producer edits and the segment is mid-analysis? | Refuse while `status` is not settled, for the reason the stranded-segment bug taught: a write racing the pipeline lands somewhere unpredictable. |
