"""Tests for the create_all guard at startup.

This exists because of a real incident: a local run with DEBUG=true and
DATABASE_URL pointing at the shared Neon database created the four entity
tables THERE, from the ORM models — so without a single CHECK constraint,
without the unique constraint that IS the entity merge rule, and without the
seeded relation types, while Alembic still reported the migration unapplied.

A schema that looks present and enforces nothing is worse than one that is
absent: nothing fails until the data is already wrong.
"""

import pytest

from main import _is_local_database as is_local


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@localhost:5432/app",
        "postgresql://u:p@127.0.0.1:5432/app",
        "postgresql+asyncpg://u:p@localhost/app",
        "postgresql://u:p@db:5432/app",  # docker-compose service name
        "postgresql://u:p@postgres:5432/app",
        "sqlite+aiosqlite:///:memory:",
        "sqlite:///./local.db",
    ],
)
def test_local_databases_may_be_built_from_the_orm(url):
    assert is_local(url)


@pytest.mark.parametrize(
    "url",
    [
        # The exact shape that caused the incident.
        "postgresql://u:p@ep-aged-firefly-as4oquk0-pooler.c-4.eu-central-1.aws.neon.tech/neondb?sslmode=require",
        "postgresql://u:p@some-rds.eu-west-1.rds.amazonaws.com:5432/app",
        "postgresql://u:p@10.0.0.5:5432/app",
        "postgresql://u:p@staging.internal:5432/app",
    ],
)
def test_remote_databases_are_refused(url):
    assert not is_local(url)


def test_an_unreadable_url_is_treated_as_remote():
    """An allowlist, not a blocklist: a URL we cannot parse is exactly the
    case where guessing wrong is expensive, so it must not qualify."""
    assert not is_local("postgresql://u:p@[unclosed:5432/app")
    assert not is_local("")
    assert not is_local("not a url at all")


def test_the_guard_is_an_allowlist_not_a_provider_blocklist():
    """The failure being guarded against is a remote host nobody thought to
    exclude, so an unfamiliar host must fail closed rather than open."""
    assert not is_local("postgresql://u:p@a-provider-invented-tomorrow.example/app")
