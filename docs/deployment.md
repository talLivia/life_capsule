# Live infrastructure — Prompt 2

This is the actual deployed topology for this project (see
`docs/poc-claude-code-prompts.md` for the full plan). `SETUP_GUIDE.md` at
the repo root is inherited from the forked base project and describes an
AWS/EC2/RDS path instead — treat it as legacy reference, not the target
for this project.

| Piece | Service | Local dev equivalent |
|---|---|---|
| App server | Fly.io (Docker) | `docker compose up` (`backend` service) |
| App database | Neon Postgres | `postgres` container |
| Graph / associative memory | Neo4j AuraDB (via Graphiti) | `neo4j` container |
| Object storage (raw video) | Cloudflare R2 | local filesystem (`USE_LOCAL_STORAGE=true`, default) or live R2 |
| Session state / visited-set | Upstash Redis | `redis` container |

## 1. FastAPI backend on Fly.io

Config lives at `backend/fly.toml`. Secrets are never stored in the repo —
set them individually:

```bash
cd backend
fly launch --no-deploy --copy-config --name <your-app-name>   # first time only, picks a region/org

fly secrets set \
  DATABASE_URL="postgresql://<user>:<password>@<host>.neon.tech/<db>?sslmode=require" \
  NEO4J_URI="neo4j+s://<instance-id>.databases.neo4j.io" \
  NEO4J_USER="neo4j" \
  NEO4J_PASSWORD="<auradb-generated-password>" \
  R2_ACCOUNT_ID="<cloudflare-account-id>" \
  R2_ACCESS_KEY_ID="<r2-access-key-id>" \
  R2_SECRET_ACCESS_KEY="<r2-secret-access-key>" \
  R2_BUCKET_NAME="life-capsule-segments" \
  R2_ENDPOINT="https://<cloudflare-account-id>.r2.cloudflarestorage.com" \
  REDIS_URL="<upstash-redis-url>" \
  ANTHROPIC_API_KEY="sk-ant-..." \
  SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')" \
  JWT_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"

fly deploy
```

`entrypoint.sh` runs `alembic upgrade head` automatically before the
server starts, so a fresh Neon database gets the full schema (including
`interview_sessions`/`raw_segments` from this prompt) on first deploy.

**GPU tradeoff, flagged explicitly:** the Docker image still bundles
Whisper/Chatterbox/MuseTalk in-process (inherited from the base project).
Deployed on a standard Fly machine (no GPU), that inference runs in CPU
fallback mode — slow, but sufficient to exercise `/health`, auth, and the
WebSocket plumbing this prompt targets. Prompt 9 splits GPU inference out
to a persistent Runpod pod; until then, don't expect real-time STT/TTS/
lip-sync performance from this deployment.

## 2. Neon Postgres

1. Create a project at https://console.neon.tech.
2. Copy the **pooled** connection string (Dashboard → Connect → Pooled
   connection) and append `?sslmode=require` if it isn't already there.
3. Set it as `DATABASE_URL` (locally in `.env`, or via `fly secrets set`
   above).
4. Apply the schema:
   ```bash
   cd backend
   alembic upgrade head
   ```
   (Fly's `entrypoint.sh` does this automatically on every deploy —
   manual `alembic upgrade head` is only needed for a one-off local run
   against Neon.)

New in this prompt: `interview_sessions` (one producer's pass through the
guided-interview question sequence, Prompt 4) and `raw_segments` (one
recorded answer, carrying `video_url`/`transcript`/`question_asked`/
`status` through the Prompt 5 analysis pipeline). See migration
`0004_interview_sessions_and_raw_segments.py`.

## 3. Neo4j AuraDB

1. Create a free/small instance at https://console.neo4j.io.
2. AuraDB shows the connection URI (`neo4j+s://...`) and an
   auto-generated password exactly once at creation — save both
   immediately.
3. Set `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD`.
4. Verify connectivity via `GET /health` — the `services.neo4j` field
   reports `connected`, `disconnected`, or `not configured`.

This prompt only wires up the raw connection + health check
(`app/services/neo4j_client.py`). Graphiti itself — entity/relationship
extraction, episodes, temporal tracking — is Prompt 3's `graph_memory.py`,
built on top of this same connection.

## 4. Cloudflare R2 (object storage)

1. Create a bucket in the Cloudflare dashboard (R2 → Create bucket).
2. Create an API token scoped to that bucket (R2 → Manage API tokens →
   Create API token), which gives you `R2_ACCESS_KEY_ID` /
   `R2_SECRET_ACCESS_KEY`.
3. `R2_ENDPOINT` is `https://<account_id>.r2.cloudflarestorage.com` — the
   account ID is shown on the R2 overview page.
4. Set `USE_LOCAL_STORAGE=false` and `STORAGE_PROVIDER=r2` to actually use
   it (leave `USE_LOCAL_STORAGE=true`, the default, for quick local
   filesystem-backed dev).

`app/services/storage.py`'s `R2StorageService` provides:
- `upload_file(...)` / `download_file(...)` — server-side, used for e.g.
  avatar images.
- `presigned_upload_url(key, content_type, ttl_seconds)` — a presigned PUT
  URL so the `/record` frontend (Prompt 4) can upload raw interview video
  straight to R2 without it passing through the FastAPI backend.
- `presigned_url(key, ttl_seconds)` / `serving_url(key, ttl_seconds)` —
  presigned GET URLs for playback/review.

## 5. Upstash Redis

1. Create a database at https://console.upstash.com (choose the region
   closest to your Fly.io app).
2. Copy the `rediss://` connection string it gives you into `REDIS_URL`.
   Upstash speaks the Redis protocol directly — no code changes needed
   beyond pointing `REDIS_URL` at it; `app/services/cache.py`'s
   `CacheService` already handles the rest.
3. This prompt adds the per-conversation **visited-set** helper
   (`add_visited` / `get_visited` / `clear_visited`) that Prompts 6-7's
   graph-expansion step will use to avoid re-surfacing a segment already
   used earlier in the same conversation. It's TTL-bounded (6h) so
   abandoned sessions don't accumulate forever.

## 6. GPU inference: Runpod pod + the Fly.io/Runpod split (Prompt 9)

Family/consumer conversations on `/talk` need real-time STT → retrieval →
TTS → lip-sync, and that last leg (Whisper, Chatterbox, MuseTalk) needs a
GPU to run at usable latency. Fly.io doesn't offer GPU machines in the
regions this project targets, so the split is:

- **Fly.io** (section 1 above) runs the FastAPI app — auth, sessions,
  `/api/v1/family`, the WebSocket endpoint, retrieval/LLM calls — on a
  standard CPU machine.
- **Runpod** runs a **persistent pod** (not per-request serverless — a POC
  goal is avoiding cold-start latency mid-conversation) using the exact
  same Docker image, with a GPU attached. It serves three endpoints under
  `/internal/gpu/*` (`app/api/v1/gpu_internal.py`): `transcribe`,
  `synthesize`, `animate` — shared-secret authenticated
  (`X-GPU-Service-Secret` header), never exposed to the frontend.
- `app/services/gpu_client.py` is what `websocket.py` actually calls. When
  `GPU_SERVICE_URL` is unset (the Fly default, and always unset on the
  Runpod pod itself), it calls the in-process STT/TTS/animator services
  directly — so local dev and this Runpod pod both run real inference
  in-process, unchanged. Only the Fly.io deployment sets
  `GPU_SERVICE_URL` to point at Runpod, turning those same three calls
  into HTTP proxy requests instead. The split is a config toggle, not a
  code fork.

### Setting up the Runpod pod

1. Create a **persistent GPU pod** (not serverless) at
   https://www.runpod.io/console/pods, using this repo's `backend/Dockerfile`
   (same CUDA 11.8 image the base project already builds for MuseTalk/
   Whisper/Chatterbox — no separate GPU-specific Dockerfile needed). Any
   RTX 4090 / A4000-class GPU is enough for one concurrent conversation;
   size up if you expect concurrent family sessions.
2. Override the pod's **container start command** to
   `./entrypoint.gpu.sh` instead of the image's default (`./entrypoint.sh`).
   The GPU variant skips the Postgres-wait-and-migrate steps — Fly.io
   already owns migrations against the shared Neon database, and running
   `alembic upgrade head` from two deployments on every boot just races
   without adding anything.
3. Set the pod's environment variables — it needs the **same secrets** as
   the Fly.io deployment (`DATABASE_URL`, `NEO4J_*`, `R2_*`, `GEMINI_API_KEY`,
   `SECRET_KEY`, `JWT_SECRET_KEY`, ...) since it's the same codebase and
   the rest of the app initializes at import time regardless of which
   routes actually get hit, **plus**:
   ```
   GPU_SERVICE_SHARED_SECRET=<same value you'll set on Fly — generate once with:
     python -c 'import secrets; print(secrets.token_hex(32))'>
   ```
   Leave `GPU_SERVICE_URL` **unset** on the Runpod pod itself — it should
   never proxy anywhere; it's the target, not a caller.
4. Expose port 8000 (Runpod's proxy gives you a public
   `https://<pod-id>-8000.proxy.runpod.net` URL) and confirm
   `GET /health` reports `"gpu": "<GPU name> (...GB used)"` instead of
   `"not available (CPU mode)"`.
5. Point the Fly.io deployment at it:
   ```bash
   cd backend
   fly secrets set \
     GPU_SERVICE_URL="https://<pod-id>-8000.proxy.runpod.net" \
     GPU_SERVICE_SHARED_SECRET="<the same value from step 3>"
   fly deploy
   ```
6. Verify end-to-end: open a `/talk` conversation and confirm responses
   come back lip-synced at real-time-ish latency rather than the slow
   CPU-fallback pace from section 1.

## 7. Frontend on Vercel

1. Import the repo at https://vercel.com/new, set the **Root Directory**
   to `frontend/` (this is a monorepo — Vercel needs to be told not to
   build from the repo root).
2. Framework preset: Next.js (auto-detected from `frontend/next.config.js`).
3. Environment variables (Project Settings → Environment Variables) —
   these are inlined into the client bundle at **build time**, so they
   must be set before each deploy, not just at runtime:
   ```
   NEXT_PUBLIC_API_URL=https://<your-fly-app>.fly.dev
   NEXT_PUBLIC_WS_URL=wss://<your-fly-app>.fly.dev
   ```
4. Deploy. Vercel gives you a `https://<project>.vercel.app` URL — that's
   the single live URL for end-to-end testing (family members hit
   `<that-url>/talk?invite=<token>`; the producer signs in at `<that-url>/`
   and generates invites from Settings).
5. On the Fly.io backend, make sure `CORS_ORIGINS` (an existing setting,
   not new to this prompt) includes the Vercel URL, or the browser will
   block the API/WebSocket calls.

## 8. Wiring it together

With all three pieces deployed, the request path for a family member's
`/talk` conversation is:

```
Vercel (Next.js /talk)
  → Fly.io (FastAPI: auth, sessions, family access, retrieval, LLM)
      → Runpod pod (GPU: Whisper STT / Chatterbox TTS / MuseTalk lip-sync)
  ← WebSocket stream of token/video_chunk/message frames back through Fly.io to the browser
```

The WebSocket itself is a direct browser ↔ Fly.io connection
(`wss://<fly-app>.fly.dev/ws/session/{id}`, built by
`frontend/lib/api.ts`'s `buildSessionWsUrl`) — Runpod is never contacted
directly by the browser, only by Fly.io's `gpu_client.py` on the backend
side.

## Local dev: `docker compose up`

`docker-compose.yml` mirrors the same five-piece topology so local dev
behaves like the deployed environment, without requiring live Neon/AuraDB/
Upstash accounts:

```bash
cp .env.example .env   # fill in ANTHROPIC_API_KEY at minimum; SECRET_KEY/JWT_SECRET_KEY
                        # need real random values — see the comments in .env.example
docker compose up
```

This brings up `postgres`, `redis`, `neo4j` (Neo4j Community Edition —
functionally equivalent to AuraDB for graph operations, just
self-hosted), `backend`, `celery-worker`, `flower`, and `frontend`.
Object storage defaults to the local filesystem (`USE_LOCAL_STORAGE=true`)
so you don't need a live R2 bucket just to run the stack end-to-end; set
`USE_LOCAL_STORAGE=false` + the `R2_*` vars in `.env` if you want local
dev to exercise real R2 uploads too.
