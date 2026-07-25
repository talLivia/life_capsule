from pathlib import Path
from typing import List, Optional, Union

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings

# Resolve .env path relative to this file's location (always project root)
_ENV_FILE = str(Path(__file__).resolve().parent.parent.parent / ".env")

_WEAK_SECRETS = {"change-this-secret-key", "change-this-jwt-secret", "change-this-jwt-secret-key"}


class Settings(BaseSettings):
    # Application
    APP_NAME: str = "AI Avatar System"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    LOG_LEVEL: str = "INFO"
    SECRET_KEY: str

    # Database — app tables (users, sessions, segments, alembic checkpoint
    # store) live here. Points at Neon Postgres in every deployed
    # environment (Neon's pooled connection string works as a drop-in
    # here; local dev can still point this at docker-compose's postgres).
    # Neon requires `?sslmode=require` on the connection string.
    DATABASE_URL: str = "postgresql://avatar_user:password@localhost:5432/avatar_db"
    DATABASE_HOST: str = "localhost"
    DATABASE_PORT: int = 5432
    DATABASE_NAME: str = "avatar_db"
    DATABASE_USER: str = "avatar_user"
    DATABASE_PASSWORD: str = "password"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379

    # Graph / associative memory — Graphiti, backed by Neo4j AuraDB. Wired up
    # in Prompt 3 (graph_memory.py); declared here so the connection and
    # health-check land in Prompt 2 without another settings pass.
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = ""
    # Some AuraDB instances use a non-default database name (matching the
    # instance id rather than the usual "neo4j") — check your AuraDB
    # connection details before assuming the default is correct.
    NEO4J_DATABASE: str = "neo4j"

    # Graphiti's OWN internal LLM (entity/relationship extraction) and
    # embedder — independent of LLM_PROVIDER above, which is the main
    # app's topic-classification/importance-scoring model (Prompts 5-7).
    # The project plan calls for Claude here; "gemini" is a fully
    # cloud-based, cheaper default for iteration (no local model to run —
    # swap to "anthropic" before relying on real ingestion quality, since
    # Claude's extraction is more reliable).
    # "gemini-2.0-flash"/"text-embedding-001" (Graphiti's own docs example)
    # turned out to be unavailable to new API keys — verified against a
    # live key which google.genai's models.list() only actually serves
    # for these two rolling-alias / current names.
    GRAPHITI_LLM_PROVIDER: str = "gemini"  # anthropic | gemini
    GRAPHITI_LLM_MODEL: str = "gemini-flash-latest"
    # graphiti-core's GeminiClient uses a separate, cheaper "small_model"
    # for some internal calls, defaulting to "gemini-2.5-flash-lite" if
    # unset — also unavailable to new API keys, so set explicitly.
    GRAPHITI_LLM_SMALL_MODEL: str = "gemini-flash-lite-latest"
    GRAPHITI_EMBEDDER_PROVIDER: str = "gemini"  # openai | gemini
    GRAPHITI_EMBEDDING_MODEL: str = "gemini-embedding-001"
    GRAPHITI_EMBEDDING_DIM: int = 3072
    # Used by both GRAPHITI_LLM_PROVIDER=gemini and
    # GRAPHITI_EMBEDDER_PROVIDER=gemini — one key covers both.
    GEMINI_API_KEY: str = ""

    # Storage — local by default; set USE_LOCAL_STORAGE=false to use a
    # remote provider, selected by STORAGE_PROVIDER ("r2", the default
    # for this project, or "s3" for the base project's original AWS path).
    USE_LOCAL_STORAGE: bool = True
    STORAGE_PROVIDER: str = "r2"  # r2 | s3
    LOCAL_STORAGE_PATH: str = "uploads"

    # AWS (retained for the base project's Terraform/EC2 GPU deploy path —
    # NOT used for object storage in this project; see R2_* below)
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = "us-east-1"
    S3_BUCKET_NAME: str = "avatar-system-storage"
    CLOUDFRONT_DOMAIN: Optional[str] = None

    # Cloudflare R2 — object storage for raw interview video/audio (S3-
    # compatible API). R2_ENDPOINT is the account's S3 API endpoint:
    # https://<account_id>.r2.cloudflarestorage.com. Presigned-URL
    # upload/download helpers are wired in Prompt 2.
    R2_ACCOUNT_ID: str = ""
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_BUCKET_NAME: str = "life-capsule-segments"
    R2_ENDPOINT: str = ""
    # Optional public/custom domain for reads, if the bucket is exposed via
    # an R2 custom domain instead of always going through presigned GETs.
    R2_PUBLIC_URL: Optional[str] = None

    # API Keys
    # Whichever of Anthropic/OpenAI/Gemini LLM_PROVIDER selects below is the
    # ONLY model used for anything that touches the storyteller's actual
    # content — Graphiti's entity extraction (Prompt 3), topic/importance
    # classification and entity-name NER (Prompt 5), and retrieval-time topic
    # matching (Prompt 6). Every one of those calls passes an explicit,
    # narrowly-scoped system prompt (see services/llm.py) — never a general
    # chat persona. Anthropic was the original project-plan default; Gemini
    # (LLM_PROVIDER=gemini, reusing GEMINI_API_KEY below) is a fully
    # supported cheaper alternative for deployments that want a single paid
    # provider instead of separately funding Anthropic too.
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    ELEVENLABS_API_KEY: Optional[str] = None

    # Point the OpenAI-compatible client at a different server — Ollama
    # (http://localhost:11434/v1), vLLM, LM Studio, OpenRouter, etc.
    # Used when LLM_PROVIDER is "openai" or "ollama".
    OPENAI_BASE_URL: Optional[str] = None

    # LLM Configuration
    # Anthropic models (2026): claude-opus-4-7 (most capable), claude-sonnet-4-6
    # (balanced — current default), claude-haiku-4-5 (fastest). OpenAI users
    # should override LLM_MODEL via .env (e.g. gpt-4o, gpt-4o-mini).
    # "ollama" runs fully local & free: set LLM_MODEL to e.g. llama3.1 and
    # optionally OPENAI_BASE_URL (defaults to http://localhost:11434/v1).
    # "gemini" needs GEMINI_API_KEY set and LLM_MODEL overridden to a real
    # Gemini model name (e.g. gemini-flash-latest — see GRAPHITI_LLM_MODEL's
    # comment above for why the "gemini-2.0-flash" docs example doesn't work
    # on new API keys).
    LLM_PROVIDER: str = "anthropic"  # anthropic, openai, ollama, gemini
    LLM_MODEL: str = "claude-sonnet-4-6"
    LLM_TEMPERATURE: float = 0.7
    LLM_MAX_TOKENS: int = 2000

    # Model for the video_clips_v2 archive-read call ONLY (Prompt 15's single
    # whole-archive reasoning call). Every OTHER llm_service caller is a short
    # classification/extraction task that the small default model handles
    # fine; this one call reads the entire archive and has to resolve
    # follow-up referents across recordings, which is a genuinely harder job.
    # Model strength is therefore a per-call decision, not a global one.
    # Empty string => fall back to LLM_MODEL (no override).
    ARCHIVE_READ_MODEL: str = ""

    # Avatar Engine
    AVATAR_ENGINE: str = "musetalk"  # musetalk, simple
    AVATAR_RESOLUTION: int = 512
    AVATAR_FPS: int = 25
    MUSETALK_PATH: str = "models/MuseTalk"

    # STT Configuration
    # large-v3-turbo: best 2026 sweet spot — ~216x real-time on GPU, multilingual,
    # only ~1% lower WER than large-v3. Falls back to base/small if VRAM is tight.
    STT_PROVIDER: str = "whisper"  # whisper, google, azure
    WHISPER_MODEL: str = "large-v3-turbo"  # tiny, base, small, medium, large-v3, large-v3-turbo
    # Separate, independently-loaded model for analysis_graph.py's ingestion
    # pipeline ONLY (Prompt 11's TranscriptChunk creation) — never the live
    # /talk conversation path (WHISPER_MODEL above). Ingestion runs offline,
    # so it can afford a bigger/slower/more accurate model even though
    # WHISPER_MODEL must stay fast for live turns. Confirmed directly why
    # these can't just be the same model: benchmarked "medium" against real
    # short (2-7s) live-question-length clips — best case ~3x slower than
    # "small" (already a noticeable conversational delay), and on one real
    # clip it reproducibly took 31-62s AND produced a hallucinated, looping
    # wrong transcription, which "small" did not. Fine offline (a 24.6s real
    # segment transcribed cleanly in 14.9s and fixed real name-transcription
    # errors "small" made), unacceptable live.
    WHISPER_MODEL_INGESTION: str = "medium"  # tiny, base, small, medium, large-v3, large-v3-turbo

    # TTS Configuration
    # chatterbox: Resemble AI's open-source SOTA TTS (default, voice cloning + 23 langs)
    TTS_PROVIDER: str = "chatterbox"
    TTS_VOICE: str = "default"

    # Security
    # Union[..., str] lets pydantic-settings keep a non-JSON env value as a
    # raw string instead of failing the JSON parse, so both formats work:
    #   CORS_ORIGINS=["http://a","http://b"]   (JSON)
    #   CORS_ORIGINS=http://a,http://b         (comma-separated)
    CORS_ORIGINS: Union[List[str], str] = ["http://localhost:3000", "http://localhost:8000"]
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRATION_HOURS: int = 24

    # Auth cookie. Login sets the JWT in an httpOnly cookie (not readable by
    # JS → not stealable via XSS). `Secure` should be true in production
    # (HTTPS only); SameSite=lax works across same-site ports (localhost:3000
    # → :8000 in dev) and blocks cross-site POST, which covers most CSRF.
    AUTH_COOKIE_NAME: str = "access_token"
    AUTH_COOKIE_SECURE: bool = False  # set true in production (.env.prod)
    AUTH_COOKIE_SAMESITE: str = "lax"  # lax | strict | none
    AUTH_COOKIE_DOMAIN: Optional[str] = None

    # Rate Limiting
    RATE_LIMIT_PER_MINUTE: int = 60
    RATE_LIMIT_PER_HOUR: int = 1000

    # WebSocket
    WS_MAX_CONNECTIONS: int = 1000
    WS_PING_INTERVAL: int = 30
    WS_PING_TIMEOUT: int = 10

    # Celery
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # File Upload
    MAX_UPLOAD_SIZE: int = 10485760  # 10MB
    ALLOWED_EXTENSIONS: Union[List[str], str] = ["jpg", "jpeg", "png", "webp"]

    # Video Settings
    VIDEO_FPS: int = 25
    VIDEO_CODEC: str = "h264"
    VIDEO_BITRATE: str = "2000k"

    # Monitoring
    SENTRY_DSN: Optional[str] = None
    PROMETHEUS_ENABLED: bool = True

    # Distributed tracing (OpenTelemetry). Off by default — when enabled,
    # requires the optional `opentelemetry-*` packages (see
    # requirements-otel.txt). Spans are no-ops when disabled, so the
    # instrumentation in the hot path costs nothing in the default build.
    OTEL_ENABLED: bool = False
    OTEL_SERVICE_NAME: str = "avatar-backend"
    # OTLP/gRPC collector endpoint, e.g. "http://otel-collector:4317".
    OTEL_EXPORTER_OTLP_ENDPOINT: Optional[str] = None

    # URLs
    FRONTEND_URL: str = "http://localhost:3000"
    BACKEND_URL: str = "http://localhost:8000"

    # GPU-inference network split (Prompt 9) — the Fly.io/Runpod topology
    # the project plan calls for. When GPU_SERVICE_URL is set, STT/TTS/
    # animate calls (app/services/gpu_client.py) proxy to a separate
    # GPU-backed deployment of this SAME codebase (a persistent Runpod pod)
    # over HTTP, hitting its /internal/gpu/* endpoints, instead of running
    # inference in-process. Left unset (the default), everything runs
    # in-process exactly as it does today — this is a config toggle, not a
    # code fork, so local dev and a CPU-only Fly.io deployment need no
    # changes at all.
    GPU_SERVICE_URL: Optional[str] = None
    # Shared-secret auth for /internal/gpu/* — every deployment of this
    # codebase exposes those routes, but they only matter (and are only
    # reachable in practice) on whichever instance is actually running on
    # the GPU pod. Required at request time (not app startup) so a
    # CPU-tier deployment that never sets GPU_SERVICE_URL and never gets
    # hit on these routes doesn't need this secret configured either.
    GPU_SERVICE_SHARED_SECRET: str = ""
    GPU_SERVICE_TIMEOUT_SECONDS: float = 60.0

    @field_validator("CORS_ORIGINS", "ALLOWED_EXTENSIONS", mode="before")
    @classmethod
    def _split_comma_separated(cls, value):
        """Accept comma-separated env strings (.env.example style) as lists."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _validate_secrets(self) -> "Settings":
        for field, value in (
            ("SECRET_KEY", self.SECRET_KEY),
            ("JWT_SECRET_KEY", self.JWT_SECRET_KEY),
        ):
            if value in _WEAK_SECRETS:
                raise ValueError(
                    f"{field} is set to an insecure default — set a strong random value in .env"
                )
            if len(value) < 32:
                raise ValueError(f"{field} must be at least 32 characters")
        return self

    model_config = {
        "env_file": _ENV_FILE,
        "case_sensitive": True,
    }


settings = Settings()
