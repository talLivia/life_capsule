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
