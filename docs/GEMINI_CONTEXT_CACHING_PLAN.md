# Gemini context caching — plan (2026-08-23)

**STATUS 2026-08-23, FINAL: SHIPPED. Phase A default = `message`
(cycle-2 grouped rendering, full §6 gate PASSED); Phase B ACTIVE
(GEMINI_CONTEXT_CACHE=on), live-verified. The cycle-1 FAIL below is
kept as the record of why the rendering is grouped.**

* **Cycle 2 (producer-approved option 1)**: the shown block is grouped
  per recording with completeness stated outright ("RECORDING N: ids —
  ALL units of this recording already shown"), handing the model the
  exhaustion fact instead of asking it to derive one by counting ids.
  Gate results: byte-identity invariants re-proven; full panel the
  quietest block of any arm (18 clean + 2 storm-retried clean; school
  stable 8×5, family 2 variants; conservation PASS ×2, family FAIL =
  the known pure-variance failure); cycle-1's re-serve shape GONE in
  both blocks; fresh-epoch two-arm replication shows NO mode effect
  (exhausted corner: same variants similar mix, clarify 0/5 vs 1/5 —
  within the corner's 1-4/5 inline range; brothers/two-hop flickers
  landed in OPPOSITE arms). Live three-turn smoke through the real DB
  path: PASS under both placements (answer → no-reserve+shown-block →
  standalone repeat).
* **Phase B activation, live-verified during a real 504 storm**: first
  referenced call billed 6,474 of 6,487 prompt tokens at the cached
  rate (99.8%); the storm then hit a cached call and the fail-soft
  wrapper retried uncached, answered correctly, and dropped the
  handle; drop_cache cleaned up. Evals pin GEMINI_CONTEXT_CACHE=off
  (eval_common) and pytest forces it off (conftest autouse) — no
  measurement or test can create billable caches.
* **Honest cost finding**: implicit-cache hits measured ZERO across
  all smoke calls even on byte-identical prefixes — the implicit-cache
  benefit assumed in §5 is unproven at this archive size; the explicit
  cache is the mechanism that demonstrably delivers cached tokens.
* Baseline re-saved under the new default (message placement, caches
  off) so future panel runs compare like-with-like.

* Panel block 1 under `message`: stable band + spouse-pronoun canary
  held; offer flickers and family/school within the A/A envelope; NEW
  answer shapes on the two uncle state-corners (9-unit and 12-unit
  re-serves of the fixture's already-shown army material; exhausted
  clarify 4/5→0/5).
* Fresh-epoch replication (2.5h gap, 5 cases × 5 runs × both arms):
  the re-serve REPRODUCED under `message` (then-more 9-unit 1/5;
  exhausted 12-unit 3/5, clarify 0/5) and NEVER appeared under the
  same-epoch inline control (then-more 0×5; exhausted [5,5,0,0,5] —
  the pre-existing corner variants only). Same-epoch flickers
  (brothers fu 1/5, two-hop fu 1/5) appeared identically in BOTH arms
  — epoch noise, not mode. Attribution clean: **the id-list
  representation weakens the exhaustion inference exactly where many
  units are shown and rules collide.** The inline mark sits adjacent
  to the unit text; the list requires a join by id, and the "all of
  this is already shown → don't re-serve, clarify/decline" judgment
  degrades under the join.
* What this does NOT touch: every narrow selection, empties,
  childhood/career compositions, two-hop selection, spouse-pronoun —
  all byte-stable under `message` in both blocks.
* Consequence: `inline` stays the default (byte-frozen, nothing
  shipped); Phase B stays unwired (its activation was contingent on
  this gate). The fix space — a strengthened shown-list rendering
  (e.g. grouped per recording, or an explicit exhaustion summary
  line) vs accepting corner regression vs abandoning `message` — is a
  producer decision; any rewording is a NEW gated cycle of its own.

**Original plan follows (recommendation block superseded where the
status above says so).** Written after the unit-id-stability
round closed; inherits its methodology verdicts (fresh-epoch replication
for anything touching prompt bytes; single-block panels gate the stable
band only). Recommendation up front, because the phases have very
different cost/benefit:

* **Phase A — make the prefix actually stable** (move shown-state out of
  the transcript into the per-turn user message): justified NOW-ish. It
  is a prerequisite for any caching at all, it makes the *implicit*
  (free, automatic) prefix cache work mid-conversation where today it
  breaks on the first played clip, and it needs no new infrastructure.
  It IS a prompt-surface change and gets the full gated cycle (§6).
* **Phase B — explicit per-producer `cachedContents` management**
  (REVISED 2026-08-23, producer decision — the original pure deferral
  is superseded): build **now, in two stages**. The producer's
  risk-reduction argument was accepted: billing/lifecycle/expiry
  machinery is exactly the category where mistakes at scale are
  expensive and mistakes at today's scale cost pennies, so the value
  of building early is live derisking, not savings. Sequencing:
  1. The **model-agnostic skeleton** (registry, version-fingerprint
     keying, deletion hooks, fail-soft read wrapper, llm.py
     cachedContent plumbing) is built IN PARALLEL with Phase A's
     multi-day gate window — zero interaction with prompt bytes, no
     activation.
  2. **Activation is flag-ON for the current producer immediately
     after Phase A clears its gate — live, not dormant.** Dormant
     infrastructure doesn't derisk (TTL races, expiry-mid-
     conversation, billing shape only surface under real traffic);
     live-at-small-scale does, for cents. Activation strictly AFTER
     Phase A: a cache pinned on today's marks-bearing transcript
     would silently serve turns 2+ a prefix whose shown-marks don't
     match the conversation — an UNGATED prompt change (§1.2's canary
     case is exactly the behavior at risk).
  3. **§5.3's scale gate is repurposed**: no longer "when to build"
     but "when the metrics start mattering" — until then, hit rate
     and storage-hours are health signals, not KPIs.
  4. **Cache-semantics re-verification joins the model-upgrade
     checklist** (min cacheable tokens, storage pricing, TTL-update
     API are per-model surface; the upgrade's mandatory re-baseline
     re-validates the cache path too — a planned line item, not a
     surprise).

## 0. Current state, measured

The one LLM call (`_read_archive_for_ranges`,
`full_archive_retrieval.py:1357`) is already assembled cache-first, per
the PROMPT-CACHING ORDERING comment at :97-108:

* **system prompt** = instructions + `{transcript_block}` +
  `{entity_map_block}` + `{disambiguation_block}` + id atoms.
  Measured on the live archive (18 recordings / 108 units):
  **16,695 chars total** — instructions 9,446, transcript 6,574,
  entity map 675. Rough token estimate (Hebrew ≈2 chars/token,
  English ≈4): **~6-8K tokens**. VERIFY at build from
  `usage_metadata.prompt_token_count` — do not trust the estimate.
* **user message** = history block + question (small, per-turn).

Stability audit of the system prompt's inputs:

| component | changes when | prefix-stable? |
| --- | --- | --- |
| instructions + id atoms | code change | yes |
| transcript text, headers, name tags | archive version change | yes (per version) |
| entity map | archive version change | yes (per version) |
| `disambiguation_block` | `bool(name_tags)` — archive-derived (:1692) | yes (per version) |
| **`[ALREADY SHOWN]` marks** | **every turn a clip plays** (:836-838) | **NO — the blocker** |

So there is exactly ONE per-turn mutation source. Today, in any
conversation, the prefix is stable until the first clip plays and
diverges on every subsequent turn — which means the implicit Gemini
prefix cache (and Anthropic's `cache_control` path in llm.py) works for
question 1 of a session and is dead from question 2 on. That is
backwards: multi-question conversations are exactly where prefix reuse
pays.

(Side note recorded, not relied on: this also explains why the eval
harness — fresh sessions, empty shown-state — always ran with a stable
prefix, which is the substrate of the cache-epoch draw-pinning
observed in UNIT_ID_STABILITY_PLAN's A/A finding.)

## 1. Phase A — move shown-state out of the cached prefix

### 1.1 Design

Render the transcript with `shown_keys=∅` always (byte-stable per
archive version), and carry shown-state in the VARIABLE user message,
where the history block already lives:

```
Recent conversation:
{history_block}

Already shown in this conversation (do not re-offer; a standalone
question may still re-answer with them):
u5, u39, u40, u41

Question:
{question}
```

* Ids in the list are CURRENT ids resolved from the persisted
  `segment_id:start_sec` keys — the same render-time key→id resolution
  the history block already does (`_format_history_block`'s
  `key_to_id`). Nothing persisted changes.
* Server-side consumers of `shown_keys` (`_validate_follow_up`,
  `_no_story_line`, offer filtering) are CODE, not prompt — untouched.
* The prompt rules that name the inline convention must be reworded to
  reference the list instead: the ALREADY SHOWN rule at :185, the
  follow-up rule at :207, and the comment-level convention note at
  :678 (the same-name tag convention says "same convention as
  [ALREADY SHOWN]" — the tags STAY inline; only the wording that
  anchors them to the shown-marks convention needs adjusting).

### 1.2 Why this shape and not another

* **Rejected: keep marks inline, cache per shown-state.** Shown-state
  is monotonically growing per conversation; every turn is a new
  prefix. Caching per state caches nothing.
* **Rejected: two transcript copies (clean cached + marks appended as
  a diff).** Duplicates the contract; the model reads two
  authorities for the same units.
* **Chosen shape minimizes semantic change**: the model still gets the
  same fact ("these units were played earlier in THIS conversation")
  with the same instructed behavior (withhold from OFFERS, never from
  standalone answers). What changes is only WHERE the fact lives:
  per-unit annotation → per-turn list. That is a real representational
  change (marks sit adjacent to the unit text; a list requires the
  model to join by id) and is exactly what §6 must measure — the
  spouse-pronoun case is the canary: it is THE shown-state-sensitive
  panel case, and the unit-id round proved it flips under prompt-side
  representation changes (8/10 under scoped ids vs 0/15 control).

### 1.3 Invariants to prove before any panel run

* **Byte-identity when nothing was shown**: for `shown_keys=∅` (every
  first question, every fresh session, every eval fixture without a
  shown builder) the rendered system prompt AND user message must be
  byte-identical to today's — same control as the unit-id branch's
  template proof. This bounds the blast radius to conversations where
  a clip already played.
* Unit tests: list renders current ids from keys (renumber-safe);
  empty list renders nothing (no vestigial header); tag convention
  note still consistent.

## 2. Phase B — explicit per-producer cachedContents

Implicit caching is best-effort and unobservable. Explicit caching
(`cachedContents` API) buys guaranteed hits and a measurable discount,
at the price of storage-hour billing and lifecycle management. Design
so Phase A's output is the cached object and nothing else changes:

### 2.1 Keying and creation

* One cache per **(producer, archive_version)**. Name/display_name
  carries both: `archive:{producer_id}:{version_hash}` where
  `version_hash` = hash of the existing `_archive_version` tuple
  (count, max updated_at, max created_at) — the SAME fingerprint the
  in-process `_ARCHIVE_CACHE` keys on. One mechanism, two consumers.
* **Created lazily on first question**, not at ingest. Most producers
  are idle most days; creating at ingest bills storage-hours for
  caches nobody reads. The first question of a session pays the
  uncached price and kicks off creation (fail-soft, like
  `warm_archive_cache`); subsequent questions hit it.
* Registry: a small in-process dict next to `_ARCHIVE_CACHE`
  (`producer_id -> (cache_name, version, expires_at)`), because the
  correctness story (§3) never depends on the registry being right —
  wrong registry = one wasted create or one full-price call.

### 2.2 TTL and expiry

* Initial TTL modest (30-60 min — a conversation's length), extended
  on activity via the TTL-update API rather than recreated (VERIFY the
  pinned model/API supports TTL update at build time).
* **Expiry is a price event, never a failure**: any
  cache-not-found/expired error on a read → retry the same request
  uncached at full price (the answer must never fail over a cache),
  then recreate in the background. This is the same fail-soft posture
  as every other cache in this codebase.

### 2.3 Scale and cost posture (hundreds/thousands of producers)

Storage-hour billing means the live cache population is bounded by
**concurrently ACTIVE producers**, not total producers — lazy creation
+ short TTL does this automatically. An infrequently-active producer
never has a cache and simply pays full price, which §5 shows is the
correct trade at small archive sizes anyway. No eviction logic needed
beyond TTL.

### 2.4 Privacy

Explicit caching persists the producer's full transcript server-side
at Google for the TTL (today it transits per-request; caching makes it
resident). Same data, longer residence — flag for a privacy pass
before Phase B ships. Per-producer keying is load-bearing for
isolation: no cache is ever shared across producers, and version
keying means a deleted recording's text stops being referenced by the
NEXT question (best-effort delete shortens the tail, §3).

## 3. Invalidation correctness

**Correctness by construction, deletion only for billing.** The cache
identity INCLUDES the archive version fingerprint. When a recording is
added/edited/deleted the fingerprint moves, the key no longer matches,
and the next question creates/uses the new cache — a stale cache can
be paid for but never SERVED, even if every delete call fails.

* Hook best-effort deletion of the old cache into the two places that
  already handle this exact moment: `finalize_ingest_node`
  (analysis_graph.py:1774-1779) and `segment_deletion` — alongside the
  existing `invalidate_archive_cache` + `warm_archive_cache` calls.
* The per-request staleness check is the existing `_archive_version`
  read that `_archive_bundle` already performs (~350ms, already paid):
  the version that renders the prompt is the version that keys the
  cache, in the same request. No new consistency mechanism.
* Multi-worker: version-keyed names make concurrent creates idempotent
  in effect (two workers may create two caches for the same version —
  wasted storage until TTL, zero correctness impact).

## 4. Interaction with the ~150K-token pre-filter (future)

`_load_archive`'s TODO gates a coarse pre-filter (embedding-rank, read
top-K) at ~150K tokens. Caching and pre-filtering are **complementary
in sequence, substitutes at the extreme**:

* Below the threshold (today → mid-scale): whole-archive prompt,
  prefix caching applies cleanly. This plan's regime.
* Above it: a per-question pre-filter changes the transcript per
  question, which destroys the shared prefix — a full-archive cache
  stops being what's sent. If/when the pre-filter is built, preserving
  cache value means selecting WHOLE recordings in stable archive
  order (per-conversation prefix stability at least). That is a
  design note for the pre-filter's own plan, not something to build
  for now.
* Phase A is useful in both regimes (shown-state doesn't belong in the
  reread block either way). Phase B's cache manager is
  whole-archive-regime machinery.

## 5. Cost/latency, honestly

### 5.1 Current scale

~7K-token prefix (VERIFY from usage_metadata), a handful of questions
per session, one active family. Indicative flash-tier arithmetic (fill
in the pinned model's real prices at build; treat these as shapes, not
quotes): uncached prefix cost per question ≈ 7K × input rate ≈
**fractions of a cent**. A 75%-class cached-token discount saves
fractions of that; explicit storage adds $/token-hour against it.
**Net at today's scale: pennies per month, possibly negative.** This is
why Phase B defers — and why Phase A's justification is NOT cost
today: it is (a) correctness of the existing implicit-cache intent,
(b) the prerequisite paid down before archives grow, (c) shown-state
arguably belonging in the variable section anyway.

### 5.2 Projected scale

At the docstring's ~35-40K tokens (1-2h of recording) with active
multi-question conversations, cached-prefix savings become real per
conversation, and TTFT/prefill latency starts to matter (~seconds of
prefill at 100K+; measure, don't extrapolate). This is the regime
Phase B exists for.

### 5.3 The Phase B gate (decide with numbers, not vibes)

Build Phase B when BOTH: median active archive ≥ ~30K prompt tokens
(from live usage_metadata), AND session shape shows ≥3 questions per
conversation regularly. Until then, implicit caching on Phase A's
stable prefix captures most of the available win for free.

## 6. Gated validation cycle (Phase A is a prompt-surface change)

Full rigor, same as the unit-id round — and using its methodology
verdicts:

1. **Byte-identity control** (empty shown-state renders today's exact
   bytes) — blocks everything else if red.
2. **Full 20-case panel, 5 runs vs the saved baseline** — narrow-case
   invariance, empties, disambiguation cases byte-stable as always.
3. **Shown-state-bearing cases get the scrutiny**: spouse-pronoun
   (the canary), both uncle state cases, two-hop-roots (accepted-offer
   state), plus a NEW panel fixture that exercises a non-empty shown
   list rendered by the new path (today's fixtures fake shown-state
   through `_load_shown_units`; the fixture must flow through the new
   user-message rendering).
4. **Conservation check** (`--check-annotations`) on the three
   annotated cases.
5. **Any marginal-band diff → fresh-epoch replication protocol**
   (blocks on separate days/cache epochs, same-format control
   alongside) before reading it as caused — single-block panels gate
   the stable band only. Expect SOME re-rolls: any byte change
   re-draws marginal judgments; the question §5's protocol answers is
   whether deviations replicate.
6. **Live two-turn WS smoke** on the dev stack: play a clip, ask a
   follow-up, verify the shown list renders, the offer withholds shown
   units, and a standalone repeat still re-answers.
7. **Cost/latency measured, not asserted**: prompt_token_count +
   cached_content_token_count from response metadata before/after;
   TTFT deltas over ≥5 turns. For Phase B later: cache hit rate,
   storage-hours billed, spend per conversation.
8. Malformed-id instrument stays 0 throughout.

Reproducibility side-effect to RECORD (not claim): a stable prefix —
and Phase B's pinned cache more so — plausibly extends cache epochs,
i.e. FEWER marginal re-draws within a conversation/TTL. If the A/A
mechanism hypothesis is right, caching slightly stabilizes marginal
judgments as a byproduct. Measure it in passing (§6.5 blocks double as
data); do not sell it as a feature until measured.

## 7. Build order (when approved)

1. Phase A behind `SHOWN_STATE_PLACEMENT=inline|message` (default
   `inline` = byte-frozen today), mirroring the UNIT_ID_SCHEME toggle
   pattern: build, prove byte-identity under the default, measure the
   flip, keep the default until the gate passes.
2. Gates §6.1-6.6; flip default on green; PROJECT_STATUS + CLAUDE.md
   records.
3. Phase B parked behind its §5.3 gate with this doc as the design of
   record; verify-at-build list: pinned-model min cacheable tokens,
   storage pricing, TTL-update API, llm_service support for passing a
   cachedContent handle (llm.py change), registry survival across the
   dev reload cycle.
