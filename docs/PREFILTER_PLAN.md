# Recording pre-filter for large archives — plan (2026-08-25)

**STATUS: PLAN ONLY — explicitly NOT needed at current scale, and this
doc's first design rule is that the built feature must be provably
inert until scale demands it.** Current whole-archive prompt measures
~6.5K tokens (live `usage_metadata`, 2026-08-24/25 smokes) against the
~150K ceiling `_load_archive`'s TODO gates on — **4-5% of the
threshold, ~20× headroom** (the docstring's own estimate: 1-2h of
recording ≈ 35-40K tokens, so the ceiling sits around 4-6 hours of
recorded material). Plan now, build when the metric says so (§6).

## 0. What this is

Once a producer's archive outgrows the single-prompt ceiling, the
whole-archive read (the design's core: "the model reads everything,
breadth falls out of the question") needs a COARSE, CHEAP pre-selection
that picks which RECORDINGS are worth reading for this question. It
must stay coarse: the fine judgment (which units answer) remains the
archive-read model's job; the pre-filter only decides what the model
gets to read. That division is what keeps the design's mechanisms
(narrow-vs-broad, empties, disambiguation) intact.

## 1. Interaction with context caching (the caching plan's §4 debt)

A per-QUESTION pre-filter changes the transcript per question and
destroys the shared cached prefix — full-archive explicit caching and
per-question filtering are incompatible at the extreme. Resolution,
in order of preference:

1. **Inert below threshold, by construction** (load-bearing): if the
   full archive fits the token budget, the pre-filter DOES NOTHING —
   not "ranks and keeps everything", literally skips. Below the
   ceiling nothing changes, the stable prefix survives, Phase B
   caching keeps working exactly as shipped. This is the same
   inert-default discipline as UNIT_ID_SCHEME and
   SHOWN_STATE_PLACEMENT.
2. **Above threshold: recording granularity + stable archive order +
   per-CONVERSATION pinning.** Selection is whole recordings only,
   rendered in unchanged archive order (so unit ids/ordinals stay
   coherent), and the selected recording-set is PINNED for the
   conversation: computed on the first question of a session, reused
   for subsequent questions. The prefix is then stable per
   (conversation, archive_version) — the explicit cache keys gain the
   recording-set hash and caching keeps paying exactly where it pays
   most (multi-question conversations). The trade, stated honestly:
   question 3 of a conversation can drift topically outside the
   pinned set. Mitigation: a cheap per-question check of the NEW
   question's top-ranked recordings against the pinned set; on a
   miss, EXPAND the set (new cache entry, one full-price call) rather
   than answer from the wrong material. Expansion is a price event,
   like cache expiry — never silent wrong-content.
3. **Rejected: per-question sets with no pinning** — maximal
   relevance, zero cache value, and unit ids/composition churn
   between consecutive questions of one conversation.

## 2. Mechanics — cheap and coarse, from signals that already exist

**Ranking signal (verified live 2026-08-25):** `RawSegment.embedding`
— one 3072-dim transcript embedding per recording, computed at
ingestion by analysis_graph's embed_transcript node, **18/18 coverage
on the live archive**, fail-soft (an embedding failure leaves None,
never blocks ingest). Query side: `embeddings.embed_text(question)` +
`embeddings.cosine_similarity` — both existing, in production use.
One embedding call per question (~100ms-class, cheap), similarity
against N recordings is trivial arithmetic even at N=1000.

**Selection is budget-driven, not fixed-K:** rank recordings by
similarity, admit whole recordings in rank order until the token
budget (~100K, leaving headroom under the ceiling) is filled, then
render the admitted set in ARCHIVE ORDER. K falls out of the budget
and recording sizes; there is no magic K to tune.

**Force-include rules (correctness, not relevance):**
* Recordings with `embedding IS NULL` are always admitted (fail-soft
  inclusion — a recording must never be invisible because one
  ingestion step failed).
* Same-name disambiguation: if the question (or history) surfaces a
  name in the entity map, ALL recordings mentioning ANY same-named
  entity are admitted — the disambiguation feature needs both
  Amnons' recordings present to clarify between them, and the
  entity map already knows which those are. This is exact and cheap.
* The recordings referenced by the conversation's shown-state (their
  units appear in the grouped ALREADY SHOWN block) are admitted, so
  the model can still reason about what was played.

**Low-confidence fallback:** if the similarity distribution is flat
(no recording clears a floor, or the top scores are statistically
indistinguishable from the rest — thresholds to be measured at build
time, not guessed now), the filter admits by budget from the top
anyway BUT flags the read as `prefiltered_low_confidence`; that flag
must (a) suppress the specific no-story line (§below) and (b) be
visible in eval output so flat-question behavior is measurable.
There is no fall-back-to-whole-archive above the ceiling — it does
not fit; the honest fallback is "widest budget + suppressed
exhaustion claims", never a silent narrow read.

**Empty-answer integrity (the no-story trap):** `_no_story_line`
currently asserts "nothing MORE about X" from `shown_keys` vs the
person's units — sound only when every unit was in the prompt. Under
an active pre-filter that check MUST also require that all of X's
recordings (entity map) were admitted; if any were excluded, the
generic line is used. Same principle for any future exhaustion-style
claim: **a filtered read can never assert archive-wide absence.**

## 3. Gated validation cycle (when built)

1. **Inertness proof (the §1.1 rule)**: below-budget archives render
   byte-identically with the feature enabled — same control as the
   UNIT_ID_SCHEME byte proof. Blocks everything else.
2. **Synthetic large-archive fixtures**: the live archive is 20×
   under threshold, so exclusion behavior can only be tested against
   a constructed archive (e.g. the live 18 recordings + padding
   recordings). Tests: narrow questions still resolve when their
   recording ranks low-but-admitted; a question about EXCLUDED
   content produces the generic empty (never the specific no-story
   line); both same-name people's recordings admitted on an
   ambiguous question (force-include rule).
3. **Full 20-case panel** with the filter forced active at an
   artificially tiny budget (so exclusion actually occurs on the
   real archive) — measured for what changes, with the fresh-epoch
   replication protocol for anything marginal; plus the standard
   panel under real settings proving inertness end-to-end.
4. **Conservation checks** unchanged (they run below threshold,
   where the filter is inert).
5. **Live smoke**: a pinned-set conversation crossing an expansion
   (question outside the pinned set) — verifying expansion happens,
   answers stay correct, and the cache re-keys rather than serving
   the stale set.

## 4. Cost/latency at the threshold

At ~150K tokens unfiltered: the read call's prefill dominates
(seconds-class) and input cost scales linearly with archive size.
With the filter at a ~100K budget the prompt is bounded regardless of
archive growth; the added cost is one question embedding (~100ms,
negligible) and the ranking (microseconds). With per-conversation
pinning (§1.2), explicit caching continues to apply to the pinned
prefix, so multi-question conversations keep the ~99.8% cached-token
rate observed at ship time — the two features compose instead of
conflicting, at the price of occasional expansion events. Exact
numbers belong to build time; the shape is: bounded prompt, bounded
latency, cache value preserved within conversations.

## 5. Explicitly not urgent — and how we'll know when it is

Current archive: 18 recordings, ~6.5K prompt tokens, 4-5% of the
ceiling. Nothing here should be built now. The Phase B activation
made the trigger metric OBSERVABLE for free: `prompt_token_count` is
captured on every read. **Build trigger: live prompt size sustained
above ~100K tokens** (two-thirds of ceiling — enough runway to build
calmly), checked casually alongside the cache health signals the
caching plan's §5.3 already designates as metrics-to-watch. Until
then this doc is the design of record, and the caching plan's §4
debt ("preserving cache value means selecting whole recordings in
stable archive order — a design note for the pre-filter's own plan")
is hereby paid.

## 6. Verify-at-build list

* Embedding model/dimensionality still matches what ingestion stores
  (3072-dim today; a model upgrade that changes the embedding space
  invalidates stored vectors — re-embed-all is a migration, plan it
  with the upgrade).
* Similarity floor / flatness thresholds measured on the real
  then-current archive, not guessed.
* Budget constant vs the then-current model's context window and
  the ceiling note in `_load_archive`.
* Pinned-set cache keying folded into gemini_cache's identity
  (producer, archive_version, recording_set_hash).
* The `_no_story_line` admitted-recordings guard, wired and tested.

## STATUS 2026-08-25/26 — BUILT, GATED, default OFF

Implementation landed (05faef0), synthetic 139K-token archive + gate
10/10 (0ac7304; the unfiltered read returned EMPTY on an army question
at that size — the motivating pathology observed directly; filtered
answered with 28 units). Forced-budget characterization on the real
archive (budget 800 chars ≈ 40% admitted), TWO blocks across separate
epochs:

* REPLICATED: every narrow/state selection identical to baseline in
  both blocks (brothers/ilana/tzvi/army/army-broad/school/spouse/
  about-a-person/two-hop + empties) — coarse-filter/fine-judgment
  division holds even starved.
* REPLICATED FINDING: career-broad 0 units in 10/10 runs — at starved
  budgets the same-name force-includes consume the budget and the
  large career recording no longer FITS, so its own question finds
  nothing. Refinement if tiny effective budgets ever become real:
  relevance-guaranteed admission before force-includes. Not reachable
  at the 300K default (375x the stress setting); synthetic gate showed
  correct admission at 30% scale.
* Draw-dependent as always: family composition (35-40 vs 17 across
  epochs), childhood 8-13, spouse-pronoun/uncle corner flicker.

Toggle stays OFF; flip decision parked until the build trigger (§5)
or a model upgrade re-tests the 139K pathology. The synthetic
producer is kept dormant as the standing large-archive instrument.
