"""
Phase 1 of docs/FAMILY_TREE_TIMELINE.md — the producer's self-entity.

Migration 0012 created one per producer existing at the time and left new
producers to application code that did not exist. Without a root, family
relations cannot be expressed at all: every extracted summary is phrased
relative to הדובר, so "I have four brothers" has nothing to be brothers OF.

These pin the behaviours that would otherwise fail silently — a missing root
produces no error anywhere, just an empty tree much later.
"""

import pytest

from app.models import Entity, User
from app.services import entity_store


def _producer(**kw):
    defaults = dict(
        id="u-test", email="t@example.com", username="tester",
        hashed_password="x", role="producer", full_name="Tal Nahum",
    )
    defaults.update(kw)
    return User(**defaults)


@pytest.mark.asyncio
async def test_creates_self_entity_for_a_producer(db_session):
    user = _producer()
    db_session.add(user)
    await db_session.flush()

    entity, created = await entity_store.ensure_self_entity(db_session, user)

    assert created is True
    assert entity.is_self is True
    assert entity.type == "person"          # ck_entities_self_is_person
    assert entity.name == "Tal Nahum"       # verbatim, not normalised
    assert entity.producer_id == user.id


@pytest.mark.asyncio
async def test_falls_back_to_username_when_full_name_is_blank(db_session):
    """One existing producer needed this fallback when migration 0012 ran, so
    it is a real case rather than a defensive one."""
    for i, blank in enumerate((None, "", "   ")):
        user = _producer(id=f"u-blank-{i}", email=f"b{i}@x.com",
                         username=f"prodspot{i}", full_name=blank)
        db_session.add(user)
        await db_session.flush()

        entity, created = await entity_store.ensure_self_entity(db_session, user)
        assert created is True
        assert entity.name == f"prodspot{i}"


@pytest.mark.asyncio
async def test_is_idempotent(db_session):
    """Called on every registration AND re-run as a backfill — a second call
    must not create a second root. The partial unique index would reject it,
    but this must not depend on hitting a constraint."""
    user = _producer()
    db_session.add(user)
    await db_session.flush()

    first, created_first = await entity_store.ensure_self_entity(db_session, user)
    second, created_second = await entity_store.ensure_self_entity(db_session, user)

    assert created_first is True
    assert created_second is False
    assert first.id == second.id


@pytest.mark.asyncio
async def test_ignores_non_producers(db_session):
    """A family member has no archive of their own and must get no root."""
    user = _producer(id="u-family", email="f@x.com", username="fam", role="family")
    db_session.add(user)
    await db_session.flush()

    entity, created = await entity_store.ensure_self_entity(db_session, user)
    assert entity is None and created is False


@pytest.mark.asyncio
async def test_promotes_an_existing_person_with_the_same_name(db_session):
    """A transcript naming the producer before this runs creates a plain
    person entity under the same merge key. It IS them — promote it rather
    than colliding on uq_entities_producer_normalized."""
    user = _producer()
    db_session.add(user)
    await db_session.flush()

    from app.services.entity_names import normalize_entity_name

    prior = Entity(
        producer_id=user.id, name="Tal Nahum",
        normalized_name=normalize_entity_name("Tal Nahum"), type="person",
    )
    db_session.add(prior)
    await db_session.flush()

    entity, created = await entity_store.ensure_self_entity(db_session, user)

    assert created is False
    assert entity.id == prior.id
    assert entity.is_self is True


@pytest.mark.asyncio
async def test_refuses_rather_than_retyping_a_non_person_collision(db_session):
    """Same key held by a place/organisation. Promoting would violate
    ck_entities_self_is_person, and retyping someone's archive because a place
    shares their name is worse than having no root. Must refuse, not raise —
    a registration cannot fail over this."""
    user = _producer(full_name="Montreal")
    db_session.add(user)
    await db_session.flush()

    from app.services.entity_names import normalize_entity_name

    db_session.add(Entity(
        producer_id=user.id, name="Montreal",
        normalized_name=normalize_entity_name("Montreal"), type="place",
    ))
    await db_session.flush()

    entity, created = await entity_store.ensure_self_entity(db_session, user)
    assert entity is None and created is False


@pytest.mark.asyncio
async def test_uses_the_application_normaliser_not_plain_lowercase(db_session):
    """The migration normalised in SQL with LOWER(TRIM(...)) because the
    Hebrew normaliser lives in Python. Here it is available, and using it is
    what lets a transcript that names the producer land on THIS row instead of
    creating a duplicate person."""
    from app.services.entity_names import normalize_entity_name

    user = _producer(id="u-he", email="he@x.com", username="he", full_name="טל נחום")
    db_session.add(user)
    await db_session.flush()

    entity, _ = await entity_store.ensure_self_entity(db_session, user)
    assert entity.normalized_name == normalize_entity_name("טל נחום")


@pytest.mark.asyncio
async def test_registration_endpoint_creates_the_root(client, db_session):
    """The wiring, not just the helper. A producer who registers today must
    come out of it with a root — that is the entire point of Phase 1, and
    nothing downstream errors if it is missing."""
    from sqlalchemy import select

    resp = await client.post(
        "/api/v1/users/register",
        json={
            "email": "rooted@example.com",
            "username": "rooted",
            "full_name": "Rooted Producer",
            "password": "securepassword123",
        },
    )
    assert resp.status_code == 201
    user_id = resp.json()["id"]

    roots = (
        await db_session.execute(
            select(Entity).where(Entity.producer_id == user_id, Entity.is_self)
        )
    ).scalars().all()

    assert len(roots) == 1
    assert roots[0].name == "Rooted Producer"
    assert roots[0].type == "person"
