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

---

## Notes on sequencing
- Prompts 1-3 can run in parallel (infra + Graphiti integration).
- Prompt 4 depends on Prompt 2 (needs live upload endpoint).
- Prompt 5 depends on Prompts 3 and 4.
- Prompts 6-8 depend on Prompt 5 having real ingested data to query against.
- Prompt 9 depends on Prompts 6-8 all working against test data first.
- Run Prompt 10 continuously as you iterate on thresholds in Prompts 6-7.
