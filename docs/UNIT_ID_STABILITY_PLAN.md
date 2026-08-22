# Stable unit ids — plan (2026-08-22, branch `unit-id-stability`)

**Problem.** Unit ids are one global monotonic sequence over the whole
archive in its load order — and that order is `(question_index,
created_at)`, so a new recording inserts MID-archive and renumbers every
unit after it (measured 2026-08-20: the career recording shifted
everything from the old u23 by +13). Every recording therefore voids
every unit-id artifact — baselines, fixtures, annotations — and forces a
full re-baseline cycle. Adding content is expensive forever, and gets
worse as the eval surface grows.

**Goal.** A new recording only ever ADDS ids. Nothing existing renumbers.
Deleting or re-analyzing a recording invalidates only ITS OWN ids.

**STATUS 2026-08-22 — MEASURED, FLIP VETOED (steps 1-2 + §4 executed).**
Steps 1-2 are built and committed (recording_no + high-water assignment;
the UNIT_ID_SCHEME toggle, byte-proven inert under the default). The §4
A/B ran in full: 20 cases × 5 runs under `scoped`, key-level comparison.

* **Copy reliability: PERFECT — 0 malformed ids across 100 reads.** The
  original open question is answered: the model copies compound ids
  flawlessly. Size cost: +178 chars (~3%), negligible.
* **But the scheme is NOT judgment-neutral, and that vetoes the flip**:
  five non-marginal drifts (brothers/ilana/tzvi/army follow-up presence;
  spouse-pronoun now mostly ANSWERS instead of empty), plus family
  selecting an entirely different stable answer (both nickname takes,
  dropping parents+roots — overturning the just-accepted core-vs-offer
  verdict), childhood-broad stably dropping two recordings, and the two
  discrimination canaries destabilized (army-narrow 2/4-wobble, school
  8/0-flicker). The id labels are themselves semantic context; changing
  them re-anchors marginal judgments archive-wide — the documented
  instruction-leak mechanism at its largest possible scale (~108
  rendered atoms + every header).
* **What stays**: recording_no + assignment (harmless, ready),
  the inert toggle, the malformed-id instrument, the A/B harness
  (eval_id_scheme_ab.py), and the history renderer's render-time key→id
  resolution (an independent win, live-verified: 51/51 persisted keys
  resolve under both schemes).
* **DISPLACEMENT A/B (2026-08-22, the decisive experiment)**: the third
  option — bare u<int> ids appended at the high-water mark, format,
  order, headers and rule text all UNCHANGED, nothing altered but
  thirteen id strings (career u23-35 rendered as u109-121 in place) —
  ALSO fails: family destabilized outright (one 30-unit and four 5-unit
  runs), school wobbling 7/0 with a new variant shape, career's own
  offer presence collapsing 5/5→1/5, and three non-marginal follow-up
  drifts (brothers/tzvi/army). Narrow selections, empties, and the
  disambiguation cases all held. CONCLUSION, now measured from three
  directions: ANY change to the rendered id bytes — format, headers, or
  mere numeric continuity — re-anchors marginal judgments. No
  rendering-visible id-stability scheme is judgment-safe. The deeper
  reframe this forces: this week's own baselines show that ADDING
  CONTENT alone moves marginal judgments too (the family selection
  changed when career/relationships were recorded, before any scheme
  work) — so re-measurement after an archive change is epistemically
  necessary regardless of ids, and id stability could only ever have
  removed the mechanical renumbering chore, not the re-measurement.
  The rational investment is therefore re-measurement CHEAPNESS: a
  baseline auto-remap tool (old id → segment:start key → new id for all
  surviving units, new-content cases flagged for re-measurement), which
  the key infrastructure already enables and which mechanizes the one
  genuinely expensive part (fixture re-derivation) permanently.
* **⚠️ THE A/A CONTROL (2026-08-22, producer-requested, run AFTER the
  verdicts above) REFRAMES BOTH A/B RESULTS.** Today's exact production
  format — zero code or prompt changes, byte-identical rendering,
  pinned model still echoing gemini-3.6-flash — drifted 11/20 against
  its own day-old baseline: 6 non-marginal (brothers/ilana/tzvi/army
  follow-up presence — THE SAME cases both A/Bs flagged — plus
  uncle-then-more now clarifying 4/5 and the exhausted case swinging
  back), family's composition moving wholesale, and the family
  conservation contract failing under pure variance (roots offered,
  violating the just-accepted annotation). What held even here: every
  narrow SELECTION, every empty, about-a-person, career-broad,
  army-broad, spouse. CONCLUSION: the system's own day-scale variance
  envelope covers most of what the format experiments "measured" — the
  A/B fail verdicts are CONFOUNDED and must be read as NOT PROVEN
  (in either direction), not as "format changes cause drift".
  Surviving hard results: 0 malformed compound ids in 100 reads (not
  variance-dependent), the byte-identity proofs, and the stable core
  band (narrow selections, empties, disambiguation) which never moved
  in any arm. LIKELY MECHANISM (unconfirmed): temperature-0 +
  prompt-cached prefix pins one thinking path per cache epoch — stable
  5/5 within any run block, a fresh draw when the cache rotates
  (time) or the prefix changes (any byte edit) — which would make
  every single-block measurement a sample of ONE draw, not of the
  distribution. METHODOLOGY CONSEQUENCE: single-block panels can gate
  the stable band only; the marginal band (broad-case composition,
  follow-up presence, state-corner behavior) requires multi-window
  sampling (blocks separated across cache epochs/days) before any
  drift there means anything. This applies retroactively to the week's
  product verdicts too: the core-vs-offer conservation results were
  validated within one draw and the family contract already failed
  under today's draw.
* **Recommended path**: revisit the flip WHEN a model upgrade forces a
  full re-baseline anyway — at that moment the re-annotation/re-verdict
  cost is already being paid, so adopting scoped ids is nearly free; the
  A/B harness re-measures the new model's judgment shift in one run.
  Until then the re-baseline tax on new recordings remains, unchanged.

## 1. The new scheme

`r<recording_no>u<local_index>` — e.g. `r7u3` = recording 7's third unit.

* **`recording_no` is a persisted, ingestion-assigned integer** — a new
  column on `raw_segments`, set to `max+1` at ingestion, never reused,
  never renumbered. This is the load-bearing choice: anchoring the prefix
  to the recording's *position in archive order* would just recreate
  today's instability one level up (a mid-order insertion would shift
  every later recording's prefix). Anchored to the ROW, the prefix is
  append-only by construction. Deletion leaves a gap (r4 missing) — gaps
  are correct, not a problem: they are how deletions stay local.
* **`local_index` is 1..k within the recording**, in speech order. Stable
  because unit boundaries are computed from the recording's own word
  timestamps and its own 90th-percentile pause threshold — nothing
  outside the recording can move them. The one thing that CAN move them
  is re-analyzing that recording (re-transcription/re-ingestion), which
  correctly invalidates only `r<n>u*` for that recording.
* Migration backfill: existing 18 recordings get `recording_no` 1..18 in
  the current archive order (so v18's mental model carries over); all
  future assignments are by ingestion time.

## 2. Every current dependent of global ids, and what changes

| Site | Today | Change |
|---|---|---|
| `_build_units` / `_split_segment_into_units` (full_archive_retrieval.py:495, 511) | `unit_id=f"u{index}"`, one `next_index` across the archive | id becomes `f"r{seg.recording_no}u{local}"`; keep a global `index` field ONLY as an in-memory ordering key (computed per load, never rendered, never persisted) |
| Prompt: id-form rule (line ~245 "unit ids of the form u<number>") and the JSON contract example ("u3", "u4", "u9") | bare-number form is IN the prompt text | rule + examples change to the new form — **this makes the whole redesign a prompt edit**, with everything §4 implies |
| Bare-int tolerance in the id normalizer (line ~1241: a lone `7` becomes `"u7"`) | rescues a model that outputs numbers | DELETE the rescue — a bare int is ambiguous under the new scheme (r?u7); tolerate instead `rXuY` with stray whitespace/case. Malformed-rate is measured in §4 |
| `_group_selected_runs` contiguity (":1439 same segment AND next global index") | global index adjacency | same-segment check already exists; adjacency becomes local-index +1. Behavior-identical (runs never span segments); the oracle test battery re-proves it mechanically |
| `_recording_ordinals` (:586) + `RECORDING N` headers + entity-map labels | positional ordinals recomputed per load | print `recording_no` instead — headers may show gaps after deletions (RECORDING 3, 5, 6…), which is honest; the prompt already forbids the model outputting recording numbers |
| History block (`_format_history_block`) quoting `unit_id` from PERSISTED message metadata | stale after ANY renumbering (true today too) | resolve the rendered id from the persisted `key` (`segment_id:start_sec`) at render time instead of trusting the stored `unit_id` — makes history rendering immune to this migration AND to any future re-analysis |
| Persisted `shown_units` metadata (`{key, unit_id, text}`) | `key` is the identity; `unit_id` is informational | **no DB rewrite needed**: all matching/marking keys off `segment_id:start_sec`, which this plan does not touch — conversation history and already-shown memory survive verbatim (confirmed: `_load_shown_units` builds marks from keys; `_unit_key` unchanged). New writes carry new-form ids naturally |
| Eval artifacts: `prompt_regression_baseline.json`, uncle/two-hop fixtures, `core_offer_annotations.py` | v18 global ids | one-time mechanical rewrite via the key-level mapping (old id → key → new id), by script; the fixtures' semantic guards re-verify the result, as they did on 08-20 |
| `eval_prompt_reply` panel, presenter videos, pending-prompt, frontend | no unit ids anywhere | untouched (verified by grep) |

Out of scope, unchanged: question ids, segment uuids, `_unit_key`,
clip assembly (times, not ids), everything client-side.

## 3. Migration path (v18 → stable ids), ordered

1. Alembic migration: add `raw_segments.recording_no` (nullable →
   backfilled 1..18 in current archive order → NOT NULL + unique);
   ingestion assigns `max+1` in the same transaction as the row.
2. Engine changes per §2, behind a temporary env toggle
   `UNIT_ID_SCHEME=global|scoped` (default `global`) so both renderers
   coexist for the measurement phase — the toggle selects id
   construction + prompt id-form text + normalizer, nothing else.
3. §4 measurement on `scoped`. Only if it passes:
4. Flip the default, rewrite eval artifacts via the mapping script,
   `--save` the new baseline (5 runs) + stability compare, update the
   worksheet/annotation headers, then DELETE the toggle and the global
   path in the same series (no permanent dual scheme).
5. Docs: CLAUDE.md's unit-numbering paragraph, VALIDATION plan §4's
   constraint text (recordings stop voiding baselines; re-analysis of a
   recording voids only its own ids), PROJECT_STATUS landing record.
6. Freeze rule: no new recordings between step 3's measurement and step
   4's re-baseline (one last time that this rule ever matters).

Rollback at any point before step 4 completes: flip the toggle back;
nothing persisted depends on the new scheme except the inert
`recording_no` column.

## 4. The gated validation cycle (in-plan, not optional)

The open empirical question flagged when this was first proposed: does
the model copy compound ids (`r12u7`) as reliably as bare ones (`u84`)?
Bare ids were chosen originally FOR copy reliability. This is measured,
never assumed:

* **Key-level equivalence is the metric.** Selections are compared as
  `segment_id:start_sec` KEY SETS, not id strings — scheme-independent
  by construction. The acceptance test: the full 20-case panel at 5
  runs under `scoped`, mapped to keys, must equal the current v18
  baseline mapped to keys (same tolerance discipline: MARGINAL cases
  judged over more runs; the exhausted case's two pinned variants
  compared as variant sets).
* **Malformed-output rate**, new counter: model outputs that fail id
  parsing (wrong prefix, bare numbers now that the rescue is gone,
  invented recording numbers). Baseline expectation: ~0 today; anything
  persistent above 0 under `scoped` is a finding that blocks the flip.
* **Conservation re-checked** against the annotations (key-mapped), and
  the **classifier panel re-run** (should be untouched — different
  model, no unit ids — but "should" is what panels exist to check).
* **Token/latency delta** recorded honestly: compound ids cost more
  tokens per unit line and per output id (~4-6 chars extra × ~108
  units); expected noise-level against a ~7.6s turn, but recorded.
* Panel runs use `--labels` for the deadline-prone giants if the 504
  storms recur (the mechanism added 2026-08-21).

Anything drifting at the KEY level that is not known-marginal vetoes the
flip — same standard as any prompt edit, because §2 makes this literally
a prompt edit.

## 5. Honest costs and risks

* **It IS a full gated cycle** — the prompt's id-form rule changes, so
  no shortcut exists. The payoff is that it is the LAST forced
  archive-wide re-baseline: after the flip, recordings stop invalidating
  anything.
* **Copy-reliability is genuinely open.** If the model degrades on
  compound ids (drops, truncations, invented prefixes), options in
  order: (a) shorter compound form (`7.3`), (b) letter prefixes
  (`g7u3`), (c) abandon — the plan explicitly reserves abandonment; the
  toggle makes it cheap.
* **History rendered from OLD conversations**: solved structurally by
  the render-time key→id resolution (§2), which is worth doing even if
  the scheme change is abandoned — it fixes today's staleness too.
* **Deletion semantics improve but need one test**: deleting a recording
  today renumbers everything after it (same class of pain); under the
  new scheme it only kills its own ids. A panel-adjacent test should pin
  that (delete-a-recording fixture on a scratch archive, not the live
  one).
* **The eval-artifact rewrite is mechanical but must be guarded**: the
  mapping script goes old-id → key → new-id and refuses on any key
  mismatch, and the fixtures' semantic guards (uncle coverage,
  family-answer membership) re-verify the rewrite exactly as they
  verified the 08-20 re-derivation.

## 6. Execution estimate

Engine + toggle + normalizer + ordinals: one careful session. Migration
+ backfill: small. Mapping script + artifact rewrite: small. Measurement:
two panel cycles (one `scoped` A/B, one post-flip re-baseline) — the
dominant wall-clock cost, subject to API weather. Total: roughly the
size of tonight's core-vs-offer cycle, executed once, to never pay the
re-baseline tax again.
