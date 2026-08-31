# Production deployment plan — 2026-08-31

**STATUS: PLAN ONLY — nothing here is built or deployed.** Written after the
2026-08-29..31 rounds (bulk import at 162-file scale, core compression +
size budget, the 90s archive-read deadline, semantic answer cache +
speculative prefetch + pre-warm, Redis-verified ~9s cache hits). Every
"current state" claim below was verified against the repo and live
environment on 2026-08-31 except where marked **UNVERIFIED**.

The decisions this plan needs from the producer are collected in §8.

---

## 0. What "production" is today — the honest baseline

Everything that served traffic tonight runs on **the local dev box**:
uvicorn on `localhost:8000` against the **one shared Neon database**
(`ep-aged-firefly…, aws eu-central-1`), local-filesystem storage
(`USE_LOCAL_STORAGE=true` — the "10GB of video" lives on this machine's
disk, NOT in R2), and a user-space Redis started from a scratchpad zip
that will not survive a reboot. There is **no environment today that a
family member could reach from the internet.** This plan is therefore
closer to a first real deployment than to an upgrade.

## 1. Fly.io — current state and deployment steps

### What exists

- `backend/fly.toml` is complete and thoughtful: app `life-capsule-backend`
  (placeholder comment says override), `performance-2x` (2 vCPU/4GB),
  `/health` checks with a 120s first-boot grace, `min_machines_running=1`
  (WebSocket backend must not cold-start mid-conversation),
  `force_https`. `entrypoint.sh` waits for Postgres and runs
  `alembic upgrade head` on every boot — schema arrives automatically.
- **Evidence an app was actually deployed once**: PROJECT_STATUS (entity
  migration, chunk 5) says "production still has NEO4J_* secrets set" and
  prescribes `fly secrets unset` — someone ran a Fly app around late July.
- **UNVERIFIED**: whether that app still exists/runs. `flyctl` is not
  installed on this machine. **Step zero of any deployment: install
  flyctl, `fly auth login`, `fly apps list`, `fly status -a
  life-capsule-backend`, `fly secrets list`.** Until then, treat the Fly
  app as "probably exists, certainly stale" (it predates migrations
  0013–0032 and every feature since July).
- **No deploy automation exists for Fly.** `.github/workflows/deploy.yml`
  is the inherited AWS ECS path (manual-only), `release.yml` pushes GHCR
  images, `docker-compose.prod.yml` + `nginx/` + `deploy.sh` are the
  inherited AWS/GPU stack. Fly deploys are manual `fly deploy` from
  `backend/`. Fine for now; CI deploy is a later nicety.

### ⚠️ Known traps, all previously recorded, all still armed

1. **Leftover env vars crash the app on boot** (`Settings` is
   `extra="forbid"`). The old app still carries `NEO4J_*`/`GRAPHITI_*`
   secrets. `fly secrets unset NEO4J_URI NEO4J_USER NEO4J_PASSWORD
   NEO4J_DATABASE GRAPHITI_…` **before** deploying current code, and audit
   `fly secrets list` for anything else `config.py` no longer declares.
2. **`fly.toml` pins a moving alias**: `LLM_MODEL =
   "gemini-flash-lite-latest"` — exactly what the 2026-08-16 all-models-
   pinned policy forbids (that alias silently moved once already). Must
   become `gemini-3.5-flash-lite`, and the `[env]` block must gain the
   other pins (§6 table).
3. **Region mismatch**: `primary_region = "iad"` (US east) while Neon is
   **aws eu-central-1 (Frankfurt)**. Each turn makes many DB round trips
   (three gathered reads + persists); iad↔Frankfurt is ~90ms per trip vs
   ~1–5ms from `fra`. This is one line in fly.toml and is likely worth
   more real latency than most of what we tuned tonight. Users are in
   Israel; `fra` is right on both counts. **Set `primary_region = "fra"`.**
4. **Image size**: the Docker image still bundles Whisper/Chatterbox/
   MuseTalk (~10GB per release.yml). It deploys, but builds are slow and
   the VM needs the 4GB. Avatar mode is off by default and
   `GPU_SERVICE_URL` stays unset (CPU fallback only for the Whisper STT
   fallback path). Slimming the image is a separate cleanup, not a
   launch blocker.

### Backend deployment steps (in order)

1. Install flyctl; verify app state, region, and secrets (step zero above).
2. Unset dead secrets (trap 1). Fix fly.toml (traps 2–3, §6 env block).
3. Create prod Neon branch/database (§5) and Upstash Redis (§2).
4. `fly secrets set` the full secret list (§6).
5. `fly deploy` from `backend/`. entrypoint runs migrations 0013→0032
   against the fresh prod DB.
6. Verify: `/health` shows `healthy` + `redis=connected`; a WS smoke turn
   against a seeded producer; `fly logs` shows the pinned model ids.

### Frontend — currently has NO production home (decision needed, §8)

Next.js app; has a Dockerfile; no Vercel/Fly config. `NEXT_PUBLIC_API_URL`
and `NEXT_PUBLIC_WS_URL` are **baked at build time** (currently
localhost). Options: **Vercel (recommended** — native Next.js, free tier,
zero config beyond env vars) or a second Fly app from the Dockerfile.
Either way the build needs the prod API/WS URLs, and the backend needs
`CORS_ORIGINS`/`FRONTEND_URL` set to the frontend's domain.

## 2. Redis — Upstash via Fly (recommended)

The code wants exactly one thing: `REDIS_URL`
([cache.py](../backend/app/services/cache.py) — connects at boot, pings,
fail-soft no-op without it; blank = deliberately off; `/health` reports
`unreachable` as `degraded`). Consumers: the 24h clip-URL cache (the
thing that made cache hits ~9s instead of ~30s tonight), the visited-set,
entity-recency hashes. Tiny footprint — KBs, not GBs.

- **Fly's own offering IS Upstash** (`fly redis create` provisions an
  Upstash database inside Fly's network, billed through Fly; free tier /
  pay-as-you-go $0.20 per 100K commands / fixed from $10mo). Best
  latency to the app, one bill, zero extra accounts. **Recommended.**
- Direct Upstash account: same product, separate console/bill — only
  useful if we ever leave Fly.
- Self-hosted Redis on a Fly machine: more moving parts for no benefit
  at this footprint. No.

**Steps**: `fly redis create` (org, name, region **fra**, eviction ON) →
copy the `redis://default:…upstash.io` URL → `fly secrets set
REDIS_URL=…` → verify `/health` says `redis=connected`. Matches
`.env.prod.example:37` exactly.

## 3. Cloudflare — two real roles, one optional

1. **R2 (already the storage design)** — see §4.
2. **DNS for the domain** (wherever the domain is registered, Cloudflare
   DNS is fine and free; needed for §6 domain setup either way).
3. **CDN for video serving — the one place a CDN genuinely matters
   here.** `storage.py` serves R2 objects via `R2_PUBLIC_URL` when set
   (else presigned URLs). Attaching a **custom domain to the R2 bucket**
   (e.g. `media.<domain>`) puts Cloudflare's CDN in front of every video
   fetch: assembled answer clips get edge-cached near the family, and
   the app server is never in the video byte path. Presigned URLs
   (the fallback) bypass CDN caching — so set `R2_PUBLIC_URL`.
   NOT recommended for now: proxying the API/WebSocket itself through
   Cloudflare — it works but adds a moving part; Fly's own TLS + anycast
   is enough at this scale.

## 4. Storage: R2 vs Tigris, co-location, and the Google question

**Who talks to the bucket, and from where?** The hot path is **family
browsers ↔ storage** (watching videos) — and with `R2_PUBLIC_URL` +
CDN, that path never touches the app server. The app ↔ storage paths
are: presigned PUT on ingest (producer upload), source-video downloads
during clip assembly (once per *new* answer; repeat answers hit the
Redis URL cache and skip assembly entirely), and the assembled-clip
upload. So "co-locate the bucket with the app" only accelerates
assembly — a per-new-answer cost of pulling a few hundred MB, worth a
handful of seconds, already amortized by the answer+clip caches.

- **R2**: $0.015/GB-mo, **zero egress**, already integrated
  (`storage.py` R2Storage, `.env.prod.example`, fly.toml
  `STORAGE_PROVIDER=r2`), custom-domain CDN built in. Bucket location
  hint EU puts it network-close to `fra`.
- **Tigris**: Fly's native S3-compatible partner (`fly storage create`),
  $0.02/GB-mo, zero egress, globally distributed with local caching,
  runs inside Fly's infra (best possible app↔bucket latency). Would be
  a ~small integration change (S3-compatible; endpoint/creds swap), but
  still a migration of 10GB+ of video plus re-testing presign/serving.
- **Google Cloud (bucket and/or app) to sit "next to" Gemini**: the
  measured facts from tonight argue directly against bothering. The
  archive-read latency is **the model's own thinking/generation**
  (12–80s, swinging with Google-side load); network RTT to
  `generativelanguage.googleapis.com` is single-digit milliseconds from
  any of these providers. Co-locating compute with Gemini buys ~0.01%
  of the turn. GCS also charges egress (~$0.08–0.12/GB) — for a video
  archive served to families, that's the *worst* of the three options.
  And true single-network purity is unreachable anyway: the DB is Neon
  (AWS). **Moving the app server to GCP is a separate, much larger
  migration decision** (new deploy tooling, networking, secrets, TLS,
  pricing model) and does not belong in this plan; nothing tonight
  produced a reason to open it.

**Recommendation: stay on R2.** Cheapest, already integrated, zero
egress where egress is the real cost (video to families), CDN-fronted
via custom domain. Revisit Tigris only if assembly-time measurements on
the deployed app show app↔R2 transfer actually hurting (unlikely: it's
a background-ish, once-per-new-answer cost). Skip GCP entirely.

## 5. Neon — the database needs splitting before launch

**Current state**: ONE database (`ep-aged-firefly…`, eu-central-1),
`alembic` at head 0032, containing all test/experimental data — YOSI's
162-recording imported archive, Tal3, prodspot…, tonight's answer-cache
rows, sessions from every experiment. The local dev box points at it
with `DEBUG=true` (the `create_all` guard from the 0012 incident
protects the schema, but dev and "prod" data share one namespace).
**UNVERIFIED**: whether other branches already exist in the Neon
project — check the console.

**Plan (fresh-DB flavor, recommended)**:
1. In the Neon console, create a **new database/branch for production**
   from an empty state (or a new Neon project if billing separation is
   wanted). Copy the **pooled** connection string.
2. Point the Fly secret `DATABASE_URL` at it; first `fly deploy` runs
   0001→0032 and seeds relation types etc. via migrations.
3. Real producers onboard fresh (recording or bulk import — the 162-file
   path is verified). Tonight's data stays where it is, as the dev DB.
4. Rename mentally and in `.env` comments: current DB = **dev**, new =
   **prod**. The dev box keeps pointing at dev only.
5. Before every future prod deploy that includes a migration: create a
   Neon branch of prod first (instant, copy-on-write) — that branch IS
   the rollback for data (§7).

Alternative (only if YOSI-the-real-person's archive should carry over):
branch the current DB and **delete the test producers** from the branch
(account-reset path exists and cascades cleanly). Messier; only worth it
if re-importing the 162 files is unacceptable — it took 22 minutes
tonight, so it isn't.

## 6. Environment & toggle audit (the launch matrix)

Config defaults are already production-correct for almost everything —
the repo's inert-by-default discipline paying off. Explicit-set list:

**fly.toml `[env]` (non-secret):**

| var | value | note |
|---|---|---|
| ENVIRONMENT | production | JSON logging path |
| DEBUG | false | also disarms create_all |
| USE_LOCAL_STORAGE / STORAGE_PROVIDER | false / r2 | already there |
| LLM_PROVIDER / LLM_MODEL | gemini / **gemini-3.5-flash-lite** | FIX the moving alias |
| ARCHIVE_READ_MODEL | **gemini-3.6-flash** | pin, per policy (currently only in local .env) |
| ARCHIVE_READ_THINKING_BUDGET | 128 | as measured |
| LIVE_STT_PROVIDER / INGESTION_STT_PROVIDER | deepgram / deepgram | Whisper stays fallback-only |
| EMBEDDING_* | gemini / gemini-embedding-001 / 3072 | already there — NEVER change (invalidates stored vectors) |
| **ANSWER_CACHE** | **on** | validated live; code default stays off — prod opts in explicitly (or flip the config default in a small commit; either is fine, pick one and record it) |
| PREFILTER / PREFILTER_CHAR_BUDGET | *(leave unset)* | defaults on/300K — correct |
| GEMINI_CONTEXT_CACHE / SHOWN_STATE_PLACEMENT / UNIT_ID_SCHEME | *(leave unset)* | defaults on/message/global — correct; UNIT_ID_SCHEME flip is VETOED |
| CORE_COMPRESSION_* / ARCHIVE_READ_MAX_TOKENS / ARCHIVE_READ_TIMEOUT_SECONDS / ANSWER_CACHE_THRESHOLD | *(leave unset)* | defaults 150/90/8192 / 8192 / 90 / 0.95 — all as validated |

**Secrets (`fly secrets set`)**: DATABASE_URL (prod branch, pooled),
GEMINI_API_KEY, DEEPGRAM_API_KEY, REDIS_URL, SECRET_KEY, JWT_SECRET_KEY
(fresh values for prod — do NOT reuse dev's), R2_ACCOUNT_ID,
R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_ENDPOINT,
R2_PUBLIC_URL (the CDN custom domain, §3), CORS_ORIGINS, FRONTEND_URL,
BACKEND_URL. **Not needed**: ANTHROPIC_API_KEY / OPENAI_API_KEY
(provider is gemini). **Unset**: every NEO4J_*/GRAPHITI_* leftover.

## 7. Domain, monitoring, rollback

- **Domain/SSL**: register/point domain (Cloudflare DNS). `api.<domain>`
  → `fly certs add` (auto-TLS). Frontend domain per hosting choice.
  `media.<domain>` → R2 custom domain. Update CORS_ORIGINS/FRONTEND_URL
  and the frontend's NEXT_PUBLIC_* at build.
- **Monitoring — the known gap is still open**: `/health` deliberately
  returns HTTP 200 with `"degraded"` in the body (so Fly's checks don't
  pull machines), which means **nothing acts on a dead Redis or DB
  today**. Minimum viable: an external monitor (UptimeRobot/Better
  Stack, free tier) hitting `/health` and alerting on
  `"status":"healthy"` absent from the body. `fly logs` for live
  tailing; ENVIRONMENT=production already switches to JSON logs. Sentry
  integration is desirable but is new code — post-launch.
- **Rollback**:
  - App: `fly releases` → `fly deploy --image <previous>`; config-level
    kill-switches first (`ANSWER_CACHE=off`, `PREFILTER=off`,
    `GEMINI_CONTEXT_CACHE=off` are all designed inert-off — cheaper
    than a code rollback for feature-shaped regressions).
  - DB: roll FORWARD by preference. entrypoint auto-upgrades, so a code
    rollback across a migration boundary needs care: take the §5 Neon
    branch before each migration deploy — restoring = pointing
    DATABASE_URL at the branch. `alembic downgrade` exists but is the
    last resort.
  - Storage/Redis: additive only; nothing to roll back. Answer-cache
    entries orphan themselves via the version fingerprint.

## 8. Decisions needed from the producer before executing

1. **Verify/claim the Fly app** (install flyctl; does
   `life-capsule-backend` exist and under whose account?) — everything
   sequences after this.
2. **Region**: approve `primary_region = "fra"` (recommended; Neon is
   Frankfurt, users are in Israel).
3. **Prod database**: fresh empty branch/project (recommended) vs
   carrying YOSI's archive over.
4. **Frontend hosting**: Vercel (recommended) vs second Fly app.
5. **Domain name** (and whether Cloudflare is the registrar/DNS).
6. **ANSWER_CACHE prod stance**: explicit env `on` (recommended) vs
   flipping the code default.
7. **Budget note**: Fly performance-2x ~$60/mo class + Upstash ($0–10) +
   Neon (existing plan) + R2 (pennies at 10GB) + LLM ~$5/family/mo
   (doubles Jan 1 2027 — calendar the 3.7-flash re-baseline decision).

## 9. Execution order (once §8 is decided)

1. flyctl install → verify app/secrets (§1 step zero).
2. Neon prod branch + fresh SECRET_KEY/JWT_SECRET_KEY.
3. `fly redis create` (fra) → REDIS_URL.
4. R2 bucket custom domain → R2_PUBLIC_URL.
5. fly.toml fixes (region, model pins, ANSWER_CACHE) — one commit.
6. `fly secrets unset` dead keys; `fly secrets set` the §6 list.
7. `fly deploy`; verify health/WS smoke/log pins.
8. Frontend deploy with prod NEXT_PUBLIC_*; CORS check end-to-end.
9. External health monitor.
10. First real producer onboarding (or bulk import) on prod; pre-warm
    fires on ingest; spot-check a cached family question ≈ seconds.
