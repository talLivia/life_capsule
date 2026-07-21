#!/usr/bin/env bash
# Runpod GPU-pod entrypoint (Prompt 9) — same image as entrypoint.sh, but
# for the persistent GPU pod that serves /internal/gpu/* over the
# Fly.io/Runpod split (app/api/v1/gpu_internal.py, app/services/gpu_client.py).
#
# Skips the Postgres wait-and-migrate steps: the Fly.io deployment already
# owns migrations against the shared Neon database, and running
# `alembic upgrade head` from two deployments on every boot just races
# without adding anything. This pod still needs a valid DATABASE_URL (the
# same one Fly uses) because the rest of the FastAPI app initializes a DB
# engine at import time — it just never runs migrations itself.
#
# Runpod: set this as the pod's container start command (overriding the
# image's default CMD, which is entrypoint.sh).
set -e

mkdir -p voice_profiles /tmp/avatars /tmp/videos /tmp/audio

echo "[startup] GPU pod: starting uvicorn (no migration step — see comment above)..."
exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers "${UVICORN_WORKERS:-1}"
