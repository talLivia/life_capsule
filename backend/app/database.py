from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from app.config import settings


def _to_asyncpg_url(url: str) -> tuple[str, dict]:
    """
    Neon's dashboard hands out connection strings with `?sslmode=require
    &channel_binding=require` appended (psycopg-style query params) — but
    SQLAlchemy's asyncpg dialect passes unrecognized query params straight
    through as **kwargs to asyncpg.connect(), which has no `sslmode` or
    `channel_binding` parameter at all (only `ssl`), so it fails immediately
    with "connect() got an unexpected keyword argument 'sslmode'". Strip
    those two and translate sslmode into asyncpg's own `ssl=` connect arg
    instead (asyncpg parses "require"/"verify-full"/etc. itself).
    """
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query)
    sslmode = next((v for k, v in pairs if k == "sslmode"), None)
    kept = [(k, v) for k, v in pairs if k not in ("sslmode", "channel_binding")]
    new_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))
    connect_args = {"ssl": sslmode} if sslmode else {}
    return new_url, connect_args


# Create async engine. SQLite (NullPool) doesn't accept pool_size/max_overflow,
# which only apply to server-based DBs like Postgres.
_db_url, _asyncpg_connect_args = _to_asyncpg_url(
    settings.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")
)
# pool_recycle keeps pooled-but-idle connections younger than the
# provider's idle reaper; pool_pre_ping guards checkout. Neither
# protects a connection HELD across slow work - nodes must not do that
# (transcribe_node / create_transcript_chunks_node, 2026-08-28).
_engine_kwargs = {"echo": settings.DEBUG, "pool_pre_ping": True, "pool_recycle": 240}
if not _db_url.startswith("sqlite"):
    _engine_kwargs.update(pool_size=10, max_overflow=20)
if _asyncpg_connect_args:
    _engine_kwargs["connect_args"] = _asyncpg_connect_args

engine = create_async_engine(_db_url, **_engine_kwargs)

# Create session factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)

# Create base class for models
Base = declarative_base()


async def get_db():
    """Dependency for getting database session"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
