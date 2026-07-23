# POC Build Plan — Step-by-Step Claude Code Prompts (Live Deployment)

## Product surface (clarified)
The deliverable is a single polished website with two distinct modes/pages, 
sharing the same design system:

1. **Recording mode** (`/record`, producer-only access) — the interview 
   experience where the storyteller records their life-story segments. 
   Needs full, non-janky video controls: live camera preview before 
   recording, start/pause/stop, re-record before confirming, playback 
   review, upload progress indicator, and clear navigation through the 
   guided question sequence (with ability to go back to a previous 
   question/segment). This is not a bare-bones dev UI — it should feel 
   like a real consumer product, since the storyteller may be recording 
   emotionally difficult content and a clunky interface adds friction.
2. **Avatar mode** (`/talk`, family/consumer access) — appears only once 
   segments are ingested and ready (Prompt 5 complete). Family members 
   ask questions via text or voice and see/hear the avatar respond with 
   synced video. No access to recording, tagging, or entity-confirmation 
   tools from this view.

Access control: the account that owns the recorded segments (producer) 
sees `/record`; invited family members see only `/talk`, scoped to that 
person's archive.

---

## Architecture decision
Use **Graphiti** (github.com/getzep/graphiti) as the associative-memory / knowledge-graph engine instead of building entity/relationship storage from scratch. It natively models entities, relationships, facts, episodes, and temporal validity — exactly the "story A relates to story B via shared entity, at what point in time" structure needed here.

**Stack for live deployment:**
- App server: FastAPI (from ai-avatar-system base) on Fly.io or AWS ECS
- Database: Neon Postgres (already used in another of your projects) — for app data + Graphiti's relational layer
- Graph/memory: Graphiti, backed by Neo4j (or FalkorDB as lighter alt) — hosted on a small managed instance (Neo4j AuraDB free/small tier is fine for POC)
- Object storage: Cloudflare R2 or S3 — raw video/audio
- GPU inference (MuseTalk + TTS): Runpod or AWS g5.xlarge, on-demand pod, called via internal API from the FastAPI backend
- Orchestration logic: LangGraph, run inside the FastAPI backend
- Redis: Upstash (managed, serverless-friendly) for session state / visited-set

---

## Prompt 1 — Repo setup and stripping unneeded logic

```
Clone https://github.com/PunithVT/ai-avatar-system into a new repo. 
Goal: keep the STT (Whisper), TTS (Chatterbox), lip-sync (MuseTalk), 
FastAPI backend, WebSocket streaming, Docker/AWS deployment scaffolding, 
and auth. 

Remove or disable the existing "free chat" LLM logic that lets the model 
answer from general knowledge. We will replace the dialogue/answer layer 
entirely with a constrained retrieval-based system built in later steps.

Set up environment config for: Neon Postgres connection, Neo4j AuraDB 
connection (for Graphiti), Cloudflare R2 credentials, Anthropic API key. 
Document required env vars in .env.example.
```

## Prompt 2 — Live infrastructure provisioning

```
Set up deployment configuration (not local-only) for:
1. FastAPI backend as a Docker container deployable to Fly.io — provide 
   fly.toml and deployment instructions.
2. Neon Postgres — provide migration scripts (alembic) for app-level 
   tables: users, interview_sessions, raw_segments (video_url, transcript, 
   question_asked, created_at, status).
3. Neo4j AuraDB free tier — connection setup and health-check endpoint.
4. Cloudflare R2 bucket — upload/download helper functions for raw video 
   files, using presigned URLs so the frontend can upload directly.
5. Upstash Redis — session state helper (visited-set per conversation).

Provide a single `docker compose up` path for local dev that mirrors 
the same service topology, but the target is live cloud deployment.
```

## Prompt 3 — Graphiti integration

```
Install and configure Graphiti (pip install graphiti-core) connected to 
the Neo4j AuraDB instance from Prompt 2. 

Build a wrapper service `graph_memory.py` with three functions:
- `add_episode(segment_id, transcript, topic_tags, timestamp)` — ingests 
  a story segment into Graphiti as an episode, letting Graphiti extract 
  entities and relationships automatically via its built-in LLM extraction 
  (use Claude via Anthropic API as the extraction model).
- `find_related_episodes(entity_names: list[str], exclude_ids: list[str], 
  max_hops=1)` — queries Graphiti for other episodes sharing the given 
  entities, excluding already-visited segment ids, limited to 1 hop.
- `get_entity_candidates(name: str)` — returns existing entities in the 
  graph that fuzzy-match a given name (for the disambiguation step in 
  Prompt 5).

Write integration tests that ingest 3 sample Hebrew story segments and 
verify shared-entity retrieval works correctly.
```

## Prompt 4 — Guided interview flow (polished recording UI + backend)

```
Build a polished `/record` page in the existing Next.js frontend, 
restricted to the producer/owner account (add basic role-based access 
control if not already present in the base project's auth).

Interview flow:
- A fixed sequence of life-period prompts (childhood, military service, 
  post-military, relationships, career — configurable list in a JSON 
  config file), shown one at a time with clear progress indication 
  (e.g. "Question 3 of 12") and the ability to navigate back to a 
  previous question/segment to re-record it later.
- Full video recording controls: live camera preview before recording 
  starts, start/pause/resume/stop, a playback review step after stopping 
  (watch what was just recorded before accepting it), and an explicit 
  re-record option that discards the previous take.
- On accept: upload directly to R2 via presigned URL with a visible 
  progress indicator, then call backend endpoint `/segments/ingest` with 
  the R2 url + the question asked + session id.
- Handle interruptions gracefully (e.g. browser refresh mid-interview 
  should resume at the correct question, not restart from zero).
- Visual design should feel like a real consumer product (clean layout, 
  calm color palette appropriate for emotionally sensitive content) — 
  not a bare developer test page.

Backend `/segments/ingest` endpoint: save raw segment row to Postgres 
with status='pending_transcription', enqueue a background job (Celery, 
already in the base project) to run transcription.
```

## Prompt 5 — Analysis pipeline with human-in-the-loop entity resolution

```
Build a LangGraph flow `analysis_graph.py` triggered by the Celery job 
from Prompt 4, with these nodes:

1. `transcribe` — run Whisper on the segment, save transcript to Postgres.
2. `extract_topics` — call Claude to classify the *actual content* of the 
   transcript into topic tags, independent of the interview question asked. 
   Output: list of topic strings.
3. `check_entities` — call Graphiti's `get_entity_candidates` (from Prompt 3) 
   for each person/place/event name found during Graphiti's own extraction. 
   If a fuzzy match to an existing entity is found with ambiguous confidence, 
   trigger a LangGraph `interrupt()`.
4. `human_confirm` — the interrupt pauses the graph and returns a question 
   to the frontend, e.g. "Is the 'Gila' mentioned here the same Gila from 
   your military service story?". Store the graph's checkpoint state 
   (LangGraph's built-in persistence, backed by Postgres) so the flow can 
   resume exactly where it paused once the user answers via a small API 
   endpoint `/segments/{id}/confirm-entity`.
5. `score_importance` — call Claude once per segment (offline, not real-time) 
   to assign an importance score 0-10 for the segment, following the 
   Generative Agents (Park et al. 2023) approach: "how significant/
   memorable is this event, on a scale where routine daily events score 
   low and major life events (marriage, loss, major decisions) score high." 
   Store this score on the segment record — it will be reused at retrieval 
   time in Prompt 7 without any additional LLM call.
6. `finalize_ingest` — call Graphiti's `add_episode` with the confirmed/
   merged entity mapping and the importance score, mark segment status='ready'.

Build the small frontend modal that surfaces pending confirmation 
questions between interview steps (poll `/segments/pending-confirmations`).
```

## Prompt 6 — Retrieval pipeline (real-time query path)

```
Build `retrieval_service.py` implementing this pipeline, called when the 
avatar receives a question during a live conversation:

1. `primary_match(topic_query)` — deterministic Postgres query: segments 
   whose topic_tags overlap the query's classified topic (classify the 
   incoming question's topic via a lightweight Claude call, temperature=0).
2. `expand_graph(primary_segment_ids, session_visited_set)` — call 
   Graphiti's `find_related_episodes` with entities from the primary 
   segment(s), max_hops=1, excluding session_visited_set (fetched/updated 
   in Upstash Redis, keyed by conversation session id).
3. Filter candidates by a minimum edge-weight/confidence threshold 
   (expose this as a tunable constant, default conservative).
4. Cap candidates to at most 2 per turn.

Return: primary segment(s) + up to 2 candidate related segments, each 
with only a short summary (not full transcript) for the next step.
```

## Prompt 7 — Relevance scoring (recency + importance + relevance, real-time)

```
Build `relevance_scorer.py` implementing the Generative Agents 
(Park et al. 2023) memory-scoring formula instead of a single binary 
LLM judge call:

score = w_recency * recency_score + w_importance * importance_score 
        + w_relevance * relevance_score

- recency_score: exponential decay based on how many turns ago (or 
  minutes ago) this segment's shared entity was last mentioned in the 
  current conversation session (0 if never mentioned this session, 
  higher for more recent mentions). Compute in-code, no LLM call.
- importance_score: precomputed during ingestion (see Prompt 5 update) 
  and stored on the segment/entity record — no LLM call at retrieval time.
- relevance_score: cosine similarity between the current question's 
  embedding and the candidate segment's embedding. Compute with a 
  standard embedding model, no LLM call.

Default weights w_recency=1, w_importance=1, w_relevance=1 (normalize 
each to 0-1 via min-max scaling first, per the original paper). Make 
weights configurable constants for tuning during QA (Prompt 10).

Only candidates scoring above a configurable threshold proceed to 
Prompt 8. Note: this function requires no real-time LLM call at all — 
it's a deterministic scoring function over precomputed values, which is 
both cheaper and more debuggable than an LLM-judge call per candidate.
```

## Prompt 8 — Bridge phrase generation and response assembly

```
Build `response_assembler.py`:
- For the primary segment: use its transcript verbatim.
- For each approved related segment (from Prompt 7): select a bridge 
  phrase from a fixed template bank (Hebrew), e.g. "זה מזכיר לי גם...", 
  "אגב, יש עוד סיפור על...", injecting only the entity name — never 
  generated content about what happened.
- Assemble final text: primary transcript + bridge phrase(s) + related 
  segment transcript(s) verbatim.
- If no primary segment is found above the topic-match threshold, return 
  a fixed fallback: "אין לי סיפור על זה" — never call the LLM to fill 
  the gap.
- Update the session's visited-set in Redis with all segment ids used.
```

## Prompt 9 — Polished avatar UI, wire to avatar output, and deploy live

```
Build a polished `/talk` page for family/consumer access (scoped to the 
relevant producer's archive, gated by invite/permission). It should only 
become available once at least some segments have status='ready'. 
Interface: a calm, simple conversation view — text or voice question 
input, the avatar video panel, and a lightweight conversation history 
for that session. This should feel like a finished product page, not a 
debug console — no exposed technical controls, thresholds, or scores 
from Prompts 6-8 visible to the family user.

Connect response_assembler's final text output (from Prompt 8) to the 
existing TTS (Chatterbox) → MuseTalk pipeline from the base project. 
The final assembled text is what gets spoken/lip-synced — no other text 
should reach this pipeline.

Deploy: FastAPI backend to Fly.io, GPU inference service to Runpod 
(persistent pod for POC, not per-request spin-up, to avoid cold-start 
latency), frontend to Vercel. Wire WebSocket connection between Fly.io 
backend and Runpod GPU service for the real-time avatar stream. 
Provide a single live URL for end-to-end testing.
```

## Prompt 10 — QA harness

```
Write a test script that runs ~30 predefined Hebrew questions against 
the live retrieval pipeline (Prompts 6-8) using the sample segments from 
Prompt 3's tests, and outputs a report: which questions got a primary 
match, which triggered a related-segment bridge, and the relevance judge's 
score/reason for each. Flag any response where the LLM appears to have 
introduced content not present in the source transcripts (manual review 
checklist, not automated).
```
# Prompts 11–14 — Original-video-clip chat mode (parallel to the avatar, not a replacement)

> Continues `docs/poc-claude-code-prompts.md` (Prompts 1–10). Give these to
> Claude Code ONE AT A TIME, in order — each depends on the previous one's
> work actually existing. Don't paste all four at once.

## Shared context (applies to all four prompts below)

Recordings are long, free-form, topic-based interviews — a single
recording can span many unrelated topics, and even a single Whisper-
detected phrase/sentence can itself contain more than one distinct idea
(e.g. "אחרי הצבא חיפשתי עבודה והייתי נגר במשך כמה שנים" is one continuous
phrase with no pause, but only "הייתי נגר" actually answers "what did you
do for work?"). We need to retrieve and assemble the exact moment(s) that
answer a question — down to the specific words, not just the surrounding
sentence — inside a much longer recording, and play back real video
clip(s) instead of (or alongside) the avatar.

**Hard constraint, applies to ALL FOUR prompts — read this first: do NOT
remove, replace, degrade, or refactor away the existing avatar pipeline
(TTS + MuseTalk + Runpod, Prompt 9). This is an ADDITIONAL chat mode,
selected by the user, living alongside the avatar as a second option.
Both modes must keep working independently. We may use both avatar and
video-clip modes going forward, so neither is being deprecated.**

---

## Prompt 11 — Data foundation: word-level timestamps, TranscriptChunk model, ingestion

New feature, unrelated to any other work in progress. This is the first of
four prompts building an original-video-clip chat mode alongside the
existing avatar — see "Shared context" above before starting.

### 1. Preserve STT timestamps at BOTH phrase and word level (currently discarded entirely)

`stt.py` already gets phrase-level segments with `.start`/`.end` from
faster-whisper internally, but `_transcribe_sync` collapses them into one
joined string (`" ".join(seg.text for seg in segments)`), discarding all
timing. Change this to also preserve, per phrase, faster-whisper's
word-level timestamps (`word_timestamps=True`, giving each word its own
`.start`/`.end`). Return both: the list of `(start_sec, end_sec, text)`
phrase tuples, AND per-phrase word-level timing. Keep the joined string
for any existing caller that needs plain text. Word-level timing is needed
by Prompt 13 to pinpoint an exact sub-phrase answer, not just the whole
phrase — it isn't used yet in this prompt, just captured and stored.

### 2. New model: `TranscriptChunk` (sentence/phrase-level, NOT fixed-length windows)

Add a table (with an Alembic migration) representing individual
transcript phrases/sentences from a `RawSegment`'s recording — one row per
Whisper-detected phrase, not grouped into fixed-duration windows:

- `id`, `raw_segment_id` (FK to raw_segments, cascade delete)
- `start_sec`, `end_sec` (float) — this phrase's own timing
- `text` (this phrase's own text, verbatim)
- `word_timestamps` (JSON: list of `{word, start_sec, end_sec}`) — from
  step 1, needed later for precise sub-phrase pinpointing (Prompt 13)
- `embedding` (JSON list of floats) — see note below on what text this is
  actually computed from
- `topic_tags` (JSON, nullable)
- `sequence_index` (int) — this chunk's position among its parent
  segment's chunks in chronological order, so neighbors can be looked up
  cheaply (for context embedding here, and for boundary expansion in
  Prompt 13) without a timestamp range query

**IMPORTANT — embedding uses surrounding context, storage does not:** a
short phrase in isolation can be too ambiguous on its own for a good
embedding match. Compute each chunk's embedding from that phrase's text
PLUS a small window of the immediately preceding and following phrase(s)
(e.g. ±1-2 neighboring phrases via `sequence_index`, or ±10-15 seconds),
so the embedding captures enough context to be found by a semantically
related question. But store `start_sec`/`end_sec`/`text`/`word_timestamps`
for the phrase ITSELF only — the context window used for embedding must
never itself be what gets returned or played back.

### 3. Update `analysis_graph.py`

After `transcribe_node`, add a step that creates one `TranscriptChunk` per
Whisper-detected phrase/sentence from step 1's now-preserved timestamps —
do NOT pre-group into fixed-duration windows; each natural phrase boundary
from Whisper becomes its own chunk. For each chunk, compute its embedding
using the contextual window described in step 2, and run topic tagging
per-chunk. Keep the existing whole-segment embedding/topics too, since the
avatar path (Prompts 6-8 as they exist today) still depends on them —
don't break that.

Entity extraction (`check_entities`) can stay at the whole-segment level
as today — but each entity's Graphiti episode/mention should also record
which chunk id(s) it appeared in, so a matched entity/topic can be traced
back to an exact moment, not just "somewhere in this recording."

### 4. Tests

Add tests for: phrase-level chunk creation with correct phrase AND
word-level timestamps, contextual-embedding computation (verify it uses
neighboring phrases but stores only the chunk's own boundaries), and
correct `sequence_index` ordering. Include a case verifying the avatar-
mode/existing Prompt 5 tests still pass unchanged.

This prompt does NOT touch retrieval, response assembly, or the frontend —
scope is data model + ingestion only. Stop here; Prompt 12 builds on this.

---

## Prompt 12 — Retrieval: perspective normalization, chunk-level matching

New feature, continuing the original-video-clip chat mode (see "Shared
context" above). This assumes Prompt 11 is done — `TranscriptChunk` rows
with phrase/word timestamps and embeddings now exist.

### 1. Perspective normalization (2nd-person question vs 1st-person transcript)

The storyteller narrates in first person ("עבדתי כנגר" / "I worked as a
carpenter"), but a family member's question is naturally phrased in
second person ("מה עבדת?" / "what did you do for work?") or third person
about the storyteller. Before running step 2's topic/entity/semantic
signals, add a preprocessing step that generates a first-person-normalized
version of the question (simple pronoun/verb-form substitution, or a
lightweight LLM rewrite if substitution rules prove insufficient for
Hebrew's verb conjugation) purely for search purposes. Use this normalized
form for the embedding and topic classification calls in step 2, while
keeping the ORIGINAL question text available for Prompt 13 (which must
answer the question as actually asked, not the search-normalized version).

### 2. Update `retrieval_service.py` and `relevance_scorer.py`

Same three-signal logic (topic / entity / semantic) and the same
Generative Agents scoring formula (recency + importance + relevance) as
the existing avatar-mode retrieval — completely unchanged conceptually.
Add a parallel path that operates over `TranscriptChunk` rows instead of
whole `RawSegment` rows, for the new video-clip mode only, using the
perspective-normalized question from step 1. Don't alter the existing
avatar-mode functions or their behavior at all — add new functions
(`primary_match_chunks`, `score_chunk_candidates`, or similar) rather than
modifying the existing ones in place.

### 3. Second-pass leniency (only if literally nothing matched)

If step 2 returns zero candidate chunks across all three signals: before
giving up, retry once with the semantic similarity threshold relaxed (e.g.
half the normal bar) — surfacing borderline candidates a strict first pass
filtered out. (Note: these candidates will still go through verification
in Prompt 13 — this step only widens the net, it never fabricates or
overrides content.) If still nothing qualifies after this retry, this is
where Prompt 13's `NO_STORY_FALLBACK` path will eventually trigger — but
that's built in the next prompt, not this one.

### 4. Tests

Add tests for: perspective normalization (Hebrew and English examples),
chunk-level primary matching against each of the three signals
independently and combined, and second-pass leniency triggering only when
the first pass truly found nothing. Include a case verifying existing
avatar-mode retrieval tests still pass unchanged.

This prompt does NOT touch response assembly, ffmpeg, or the frontend —
scope is retrieval only, returning candidate `TranscriptChunk`s. Stop
here; Prompt 13 builds on this.

---

## Prompt 13 — Answer assembly: verification, pinpointing, clause coverage, ffmpeg

New feature, continuing the original-video-clip chat mode (see "Shared
context" above). This assumes Prompts 11-12 are done — chunk-level
retrieval now returns candidate `TranscriptChunk`s for a given question.

### 1. Per-candidate analysis: verification + sub-phrase pinpointing + clause coverage

For each matched chunk from Prompt 12, run ONE lightweight, temperature=0
LLM call (combine these three sub-tasks into a single call if practical,
to save latency/cost — flag this as the preferred approach but use your
judgment on the actual prompt design) that, given the chunk's `text` and
the ORIGINAL (non-normalized) question, returns:

  a. **Relevance verification** — is this chunk actually a good answer to
     this question, or just a loose topic/embedding match? Reject chunks
     below a configurable relevance bar rather than assembling video
     around a weak/false match.
  b. **Sub-phrase pinpointing** — the exact contiguous substring of `text`
     that answers the question (or the whole chunk verbatim if no
     meaningful narrowing applies — never invent text not present in the
     chunk). Locate that substring's position in the chunk's
     `word_timestamps` (from Prompt 11) to get precise
     `answer_start_sec`/`answer_end_sec`, narrower than the full chunk's
     boundaries.
  c. **Clause coverage** (only relevant for multi-part questions, e.g.
     "what did you do for work, did you enjoy it, tell me stories") —
     which distinct clause(s) of the question this chunk actually
     addresses.

Fail-soft: if this call fails or returns text not found verbatim in the
chunk, fall back to treating the whole chunk as relevant with its own full
boundaries, rather than blocking the response.

After processing all candidates: if the question had multiple clauses (1c)
and one has zero coverage across every verified chunk, do NOT invent or
force anything for it — just note internally (logging/QA, optionally a
subtle UI cue like "no story found about X specifically") that it wasn't
covered — same never-invent principle as `NO_STORY_FALLBACK`.

### 2. Video clip assembly via ffmpeg

Add a new function parallel to `response_assembler.assemble_response`
(e.g. `assemble_video_clip_response`) that:

- Takes the verified, pinpointed candidates from step 1.
- For each, expand playback boundaries OUTWARD FROM THE PINPOINTED
  SUB-RANGE (`answer_start_sec`/`answer_end_sec`, not the whole chunk's
  boundaries) by walking to neighboring chunks via `sequence_index`
  (Prompt 11), extending while: (a) under a configurable max clip
  duration, (b) no topic drift detected (compare topic_tags or embedding
  similarity against a threshold), (c) not crossing a long silence gap if
  derivable from the timestamps.
- Trims each expanded clip from its parent recording via ffmpeg
  (`-ss`/`-to` on the source video, re-encode or stream-copy as
  appropriate) and concatenates the resulting clips into a single output
  video file, in primary→bridge order (mirroring the existing avatar
  path's text-stitching order).
- Uploads the assembled clip to storage (same storage service/bucket
  convention as `RawSegment.video_url`) and returns its URL.
- If no candidate survived retrieval (Prompt 12) or verification (step 1
  above), return the same "no story" signal as the avatar path (reuse
  `NO_STORY_FALLBACK` or an equivalent) — if nothing qualifies, no video
  is assembled, full stop.
- Consider whether assembled clips should be cached/reused for identical
  question-derived chunk sets, since ffmpeg processing has real latency
  and cost compared to the avatar path's near-instant text assembly —
  flag this tradeoff and propose an approach rather than assuming one.

Expose this via a new or extended API/WebSocket endpoint alongside the
existing avatar one — don't repurpose the existing avatar endpoint's
contract. (Prompt 14 will wire this into the actual UI.)

### 3. Tests

Add tests for: per-candidate verification/pinpointing/clause-coverage
against a chunk containing multiple distinct ideas, fail-soft behavior
when the LLM call fails or hallucinates non-verbatim text, boundary
expansion from a pinpointed sub-range, and ffmpeg-based multi-chunk clip
assembly (including a non-contiguous, multi-recording case). Include a
case verifying the avatar-mode response assembler's tests still pass
unchanged.

This prompt does NOT touch the settings screen or frontend chat UI — scope
is backend assembly only, exposed via API/WebSocket. Stop here; Prompt 14
wires this to the UI.

---

## Prompt 14 — Settings toggle, frontend chat component, end-to-end tests

New feature, continuing the original-video-clip chat mode (see "Shared
context" above). This assumes Prompts 11-13 are done — there's now a
working backend endpoint that returns an assembled video-clip response (or
`NO_STORY_FALLBACK`) for a question.

### 1. Settings screen

Add a setting (per producer/family) with two mutually exclusive modes:

- **"Avatar"** (existing behavior, default, unchanged) — TTS + MuseTalk
  talking head, powered by `response_assembler`'s existing verbatim-text-
  stitching output exactly as it works today.
- **"Original video clips"** (new) — the family member's question returns
  a real video clip (or a stitched sequence of clips from non-contiguous
  moments, possibly across different recordings) instead of a synthesized
  avatar, via Prompt 13's new endpoint.

The `/talk` UI reads this setting and renders the corresponding chat
component.

### 2. Frontend: new chat component

Add a new chat UI variant (alongside the existing avatar chat component)
that, on receiving a clip response, plays the assembled video with normal
playback controls, and falls back to a clear "no story about that" message
if `NO_STORY_FALLBACK` was returned. Wire the settings toggle from step 1
to choose which chat component renders on `/talk`.

### 3. End-to-end tests

Add an end-to-end test (mirroring `test_e2e_flow.py`'s style) that
exercises the full path: record/ingest a sample long, multi-topic
recording → ask a question naming something buried mid-recording → verify
the returned clip's time range plausibly contains the right moment. Also
verify: switching the settings toggle actually changes which chat
component renders, and the existing avatar end-to-end test still passes
unchanged with the setting left on "Avatar" (the default).

This is the last of the four prompts — after this, both modes should be
fully wired and independently testable end-to-end.
---

## Notes on sequencing
- Prompts 1-3 can run in parallel (infra + Graphiti integration).
- Prompt 4 depends on Prompt 2 (needs live upload endpoint).
- Prompt 5 depends on Prompts 3 and 4.
- Prompts 6-8 depend on Prompt 5 having real ingested data to query against.
- Prompt 9 depends on Prompts 6-8 all working against test data first.
- Run Prompt 10 continuously as you iterate on thresholds in Prompts 6-7.
- Prompts 11-14 (original-video-clip mode) depend on Prompt 9 being complete 
  (the avatar path must exist first, since 11-14 add a parallel mode 
  alongside it, not before it). Run 11 → 12 → 13 → 14 strictly in order — 
  each assumes the previous one's work already exists in the codebase.

