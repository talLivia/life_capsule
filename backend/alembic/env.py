import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.config import settings
from app.database import Base, _to_asyncpg_url
from app.models import (
    Avatar,
    Conversation,
    InterviewSession,
    Message,
    RawSegment,
    Session,
    User,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Async URL from settings — _to_asyncpg_url strips/translates the
# sslmode/channel_binding query params Neon's dashboard appends (see its
# docstring in database.py); async_engine_from_config's config-dict path
# can't carry connect_args, so the engine is built directly below instead.
_alembic_db_url, _alembic_connect_args = _to_asyncpg_url(
    settings.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")
)
config.set_main_option("sqlalchemy.url", _alembic_db_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    connectable = create_async_engine(
        _alembic_db_url,
        poolclass=pool.NullPool,
        connect_args=_alembic_connect_args,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
