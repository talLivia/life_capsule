"""
Photos on entities and periods — the Phase 2 foundation (docs/MEDIA_GALLERY.md).

What matters here and nowhere else:

- the ONE-OWNER rule (entity XOR category), at presign, at the row-write, and
  in the schema's own CHECK;
- the row-write trusting only the STORAGE KEY for ownership — a client cannot
  be issued a key for one owner and write a row claiming another;
- the server-side existence + size check at the row-write, because in R2 mode
  it is the only size enforcement there is;
- primary-photo bookkeeping: first entity photo becomes the face, deleting
  the face promotes a successor, category photos never get one.
"""

from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from app.api.v1 import media as media_module
from app.models import Entity, MediaAsset


def _entity(test_user, **kw):
    defaults = dict(
        producer_id=test_user.id,
        name="אמנון",
        normalized_name="אמנון",
        type="person",
    )
    defaults.update(kw)
    return Entity(**defaults)


@pytest.fixture
def storage_mocks(monkeypatch):
    """The endpoints' storage calls, mocked at the media module's reference.

    file_size defaults to a small object that exists; tests override
    side_effect/return_value for the absent and oversized cases.
    """
    mocks = {
        "upload_file": AsyncMock(return_value="http://test/uploads/x"),
        "file_size": AsyncMock(return_value=1234),
        "delete_file": AsyncMock(),
        "serving_url": AsyncMock(side_effect=lambda key, **_: f"http://media/{key}"),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(media_module.storage_service, name, mock)
    return mocks


async def _presign(client, auth_headers, **overrides):
    payload = {"content_type": "image/jpeg", **overrides}
    return await client.post("/api/v1/media/presign", json=payload, headers=auth_headers)


# ── presign ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_presign_requires_exactly_one_owner(client: AsyncClient, auth_headers, db_session, test_user):
    entity = _entity(test_user)
    db_session.add(entity)
    await db_session.commit()

    neither = await _presign(client, auth_headers)
    both = await _presign(client, auth_headers, entity_id=entity.id, category="childhood")
    assert neither.status_code == 400
    assert both.status_code == 400


@pytest.mark.asyncio
async def test_presign_for_an_entity_returns_a_scoped_key(
    client: AsyncClient, auth_headers, db_session, test_user
):
    entity = _entity(test_user)
    db_session.add(entity)
    await db_session.commit()

    resp = await _presign(client, auth_headers, entity_id=entity.id)
    assert resp.status_code == 200
    data = resp.json()
    assert data["storage_key"].startswith(f"photos/{test_user.id}/entity/{entity.id}/")
    assert data["storage_key"].endswith(".jpg")
    assert "/api/v1/media/upload-local/" in data["upload_url"]


@pytest.mark.asyncio
async def test_presign_rejects_someone_elses_entity_with_404(
    client: AsyncClient, auth_headers, db_session, test_user
):
    """404 not 403 — a 403 would confirm the id exists (the entities rule)."""
    from app.api.v1.users import get_password_hash
    from app.models import User

    other = User(
        email="other@example.com",
        username="other",
        hashed_password=get_password_hash("pw12345678"),
    )
    db_session.add(other)
    await db_session.flush()
    entity = _entity(other, producer_id=other.id)
    db_session.add(entity)
    await db_session.commit()

    resp = await _presign(client, auth_headers, entity_id=entity.id)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_presign_accepts_a_real_category_and_rejects_an_invented_one(
    client: AsyncClient, auth_headers
):
    """The category value must come from interview_config, never be trusted
    from the client (§2.2) — a renamed category orphaning photos is bad
    enough without invented ones joining it."""
    from app import interview_config

    real = interview_config.get_categories("he")[0]["category"]
    ok = await _presign(client, auth_headers, category=real)
    invented = await _presign(client, auth_headers, category="not-a-category")
    assert ok.status_code == 200
    assert ok.json()["storage_key"].startswith(f"photos/")
    assert "/period/" in ok.json()["storage_key"]
    assert invented.status_code == 400


@pytest.mark.asyncio
async def test_presign_rejects_heic_with_a_message_that_names_it(
    client: AsyncClient, auth_headers, db_session, test_user
):
    """Open decision 2: HEIC is what an iPhone produces by default, so the
    rejection has to say what to do about it, not just 'unsupported'."""
    entity = _entity(test_user)
    db_session.add(entity)
    await db_session.commit()

    resp = await _presign(client, auth_headers, entity_id=entity.id, content_type="image/heic")
    assert resp.status_code == 400
    assert "HEIC" in resp.json()["detail"]

    pdf = await _presign(client, auth_headers, entity_id=entity.id, content_type="application/pdf")
    assert pdf.status_code == 400
    assert "photo" in pdf.json()["detail"].lower()


# ── the local PUT ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_local_upload_writes_only_keys_the_user_owns(
    client: AsyncClient, auth_headers, test_user, storage_mocks
):
    foreign = await client.put(
        "/api/v1/media/upload-local/photos/someone-else/period/childhood/x.jpg",
        content=b"jpegbytes",
        headers={**auth_headers, "content-type": "image/jpeg"},
    )
    assert foreign.status_code == 403

    ok = await client.put(
        f"/api/v1/media/upload-local/photos/{test_user.id}/period/childhood/x.jpg",
        content=b"jpegbytes",
        headers={**auth_headers, "content-type": "image/jpeg"},
    )
    assert ok.status_code == 204
    storage_mocks["upload_file"].assert_awaited_once()


@pytest.mark.asyncio
async def test_local_upload_enforces_the_photo_cap(
    client: AsyncClient, auth_headers, test_user, storage_mocks, monkeypatch
):
    monkeypatch.setattr(media_module.settings, "MAX_PHOTO_UPLOAD_BYTES", 1024)
    resp = await client.put(
        f"/api/v1/media/upload-local/photos/{test_user.id}/period/childhood/big.jpg",
        content=b"x" * 4096,
        headers={**auth_headers, "content-type": "image/jpeg"},
    )
    assert resp.status_code == 413
    storage_mocks["upload_file"].assert_not_awaited()


@pytest.mark.asyncio
async def test_local_upload_validates_content_type_itself(
    client: AsyncClient, auth_headers, test_user, storage_mocks
):
    """Presign is not the only door — the PUT validates rather than trusting
    that presign already did (the segment-upload rule)."""
    resp = await client.put(
        f"/api/v1/media/upload-local/photos/{test_user.id}/period/childhood/x.jpg",
        content=b"%PDF-1.4",
        headers={**auth_headers, "content-type": "application/pdf"},
    )
    assert resp.status_code == 400
    storage_mocks["upload_file"].assert_not_awaited()


# ── the row-write ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_reads_the_owner_from_the_key_alone(
    client: AsyncClient, auth_headers, db_session, test_user, storage_mocks
):
    entity = _entity(test_user)
    db_session.add(entity)
    await db_session.commit()

    key = f"photos/{test_user.id}/entity/{entity.id}/abc.jpg"
    resp = await client.post(
        "/api/v1/media",
        json={"storage_key": key, "caption": "בחתונה", "taken_year": 1984},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["entity_id"] == entity.id
    assert body["category"] is None
    assert body["caption"] == "בחתונה"
    assert body["taken_year"] == 1984
    # The client never sees a storage key — only a resolved URL.
    assert "storage_key" not in body
    assert body["url"] == f"http://media/{key}"


@pytest.mark.asyncio
async def test_create_rejects_a_key_issued_to_someone_else(
    client: AsyncClient, auth_headers, storage_mocks
):
    resp = await client.post(
        "/api/v1/media",
        json={"storage_key": "photos/not-this-user/period/childhood/x.jpg"},
        headers=auth_headers,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_rejects_a_malformed_key(client: AsyncClient, auth_headers, storage_mocks):
    for bad in [
        "segments/u/sess/0/x.webm",  # wrong prefix entirely
        "photos/u/entity/x.jpg",  # too few parts
        "photos/u/gallery/e1/x.jpg",  # unknown owner kind
        "photos/u/entity/e1/x.exe",  # extension not in the vocabulary
    ]:
        resp = await client.post(
            "/api/v1/media", json={"storage_key": bad}, headers=auth_headers
        )
        assert resp.status_code in (400, 403), bad


@pytest.mark.asyncio
async def test_create_requires_the_object_to_exist(
    client: AsyncClient, auth_headers, db_session, test_user, storage_mocks
):
    """A row must never point at a file that was never uploaded — that is a
    broken image on every surface that renders it."""
    entity = _entity(test_user)
    db_session.add(entity)
    await db_session.commit()
    storage_mocks["file_size"].side_effect = FileNotFoundError("nope")

    resp = await client.post(
        "/api/v1/media",
        json={"storage_key": f"photos/{test_user.id}/entity/{entity.id}/x.jpg"},
        headers=auth_headers,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_enforces_the_cap_after_the_fact_and_removes_the_object(
    client: AsyncClient, auth_headers, db_session, test_user, storage_mocks, monkeypatch
):
    """In R2 mode the PUT bypasses this backend entirely, so this check is
    the only server-side size enforcement — and refusing the row without
    deleting the object would park the oversized file in storage forever."""
    entity = _entity(test_user)
    db_session.add(entity)
    await db_session.commit()
    monkeypatch.setattr(media_module.settings, "MAX_PHOTO_UPLOAD_BYTES", 1024)
    storage_mocks["file_size"].return_value = 4096

    key = f"photos/{test_user.id}/entity/{entity.id}/big.jpg"
    resp = await client.post(
        "/api/v1/media", json={"storage_key": key}, headers=auth_headers
    )
    assert resp.status_code == 413
    storage_mocks["delete_file"].assert_awaited_once_with(key)


@pytest.mark.asyncio
async def test_first_entity_photo_becomes_primary_second_does_not(
    client: AsyncClient, auth_headers, db_session, test_user, storage_mocks
):
    entity = _entity(test_user)
    db_session.add(entity)
    await db_session.commit()

    first = await client.post(
        "/api/v1/media",
        json={"storage_key": f"photos/{test_user.id}/entity/{entity.id}/a.jpg"},
        headers=auth_headers,
    )
    second = await client.post(
        "/api/v1/media",
        json={"storage_key": f"photos/{test_user.id}/entity/{entity.id}/b.jpg"},
        headers=auth_headers,
    )
    assert first.json()["is_primary"] is True
    assert second.json()["is_primary"] is False


@pytest.mark.asyncio
async def test_make_primary_swaps_the_face_in_one_transaction(
    client: AsyncClient, auth_headers, db_session, test_user, storage_mocks
):
    """Clicking the portrait circle means 'this is the face now' (§9.6) — a
    new portrait that stayed invisible would read as the upload failing."""
    entity = _entity(test_user)
    db_session.add(entity)
    await db_session.commit()

    first = await client.post(
        "/api/v1/media",
        json={"storage_key": f"photos/{test_user.id}/entity/{entity.id}/a.jpg"},
        headers=auth_headers,
    )
    second = await client.post(
        "/api/v1/media",
        json={
            "storage_key": f"photos/{test_user.id}/entity/{entity.id}/b.jpg",
            "make_primary": True,
        },
        headers=auth_headers,
    )
    assert second.json()["is_primary"] is True

    old = await db_session.get(MediaAsset, first.json()["id"])
    await db_session.refresh(old)
    assert old.is_primary is False


@pytest.mark.asyncio
async def test_category_photos_never_get_a_primary(
    client: AsyncClient, auth_headers, test_user, storage_mocks
):
    """Primary means 'the face on the entity's card'; a period has no face."""
    from app import interview_config

    cat = interview_config.get_categories("he")[0]["category"]
    resp = await client.post(
        "/api/v1/media",
        json={"storage_key": f"photos/{test_user.id}/period/{cat}/a.jpg"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["is_primary"] is False
    assert resp.json()["category"] == cat


@pytest.mark.asyncio
async def test_create_rejects_a_category_key_for_an_invented_category(
    client: AsyncClient, auth_headers, test_user, storage_mocks
):
    """The row-write re-validates the owner rather than trusting that the key
    came from presign — the PUT endpoints already follow this rule."""
    resp = await client.post(
        "/api/v1/media",
        json={"storage_key": f"photos/{test_user.id}/period/not-a-category/x.jpg"},
        headers=auth_headers,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_the_one_owner_check_holds_in_the_schema_itself(db_session, test_user):
    """ck_media_one_owner, exercised directly — the API can never produce
    such a row, which is exactly why the constraint has to exist below it."""
    from sqlalchemy.exc import IntegrityError

    db_session.add(
        MediaAsset(
            producer_id=test_user.id,
            storage_key=f"photos/{test_user.id}/period/childhood/x.jpg",
            entity_id=None,
            category=None,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


# ── listing ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_requires_exactly_one_owner_filter(client: AsyncClient, auth_headers):
    neither = await client.get("/api/v1/media", headers=auth_headers)
    both = await client.get(
        "/api/v1/media",
        params={"entity_id": "e1", "category": "childhood"},
        headers=auth_headers,
    )
    assert neither.status_code == 400
    assert both.status_code == 400


@pytest.mark.asyncio
async def test_list_returns_the_owners_gallery_with_resolved_urls(
    client: AsyncClient, auth_headers, db_session, test_user, storage_mocks
):
    entity = _entity(test_user)
    db_session.add(entity)
    await db_session.flush()
    for name, primary in [("a.jpg", False), ("b.jpg", True)]:
        db_session.add(
            MediaAsset(
                producer_id=test_user.id,
                storage_key=f"photos/{test_user.id}/entity/{entity.id}/{name}",
                entity_id=entity.id,
                is_primary=primary,
            )
        )
    await db_session.commit()

    resp = await client.get(
        "/api/v1/media", params={"entity_id": entity.id}, headers=auth_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    # Primary leads its own gallery.
    assert body[0]["is_primary"] is True
    assert all(item["url"].startswith("http://media/photos/") for item in body)
    assert all("storage_key" not in item for item in body)


@pytest.mark.asyncio
async def test_list_is_scoped_to_the_requesting_producer(
    client: AsyncClient, auth_headers, db_session, test_user, storage_mocks
):
    """Another producer's category gallery must come back empty, not 403 —
    the category namespace is shared, the photos are not."""
    from app.api.v1.users import get_password_hash
    from app.models import User

    other = User(
        email="other2@example.com",
        username="other2",
        hashed_password=get_password_hash("pw12345678"),
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        MediaAsset(
            producer_id=other.id,
            storage_key=f"photos/{other.id}/period/childhood/x.jpg",
            category="childhood",
        )
    )
    await db_session.commit()

    resp = await client.get(
        "/api/v1/media", params={"category": "childhood"}, headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json() == []


# ── deletion ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_removes_the_row_then_the_file(
    client: AsyncClient, auth_headers, db_session, test_user, storage_mocks
):
    key = f"photos/{test_user.id}/period/childhood/x.jpg"
    asset = MediaAsset(
        producer_id=test_user.id, storage_key=key, category="childhood"
    )
    db_session.add(asset)
    await db_session.commit()

    resp = await client.delete(f"/api/v1/media/{asset.id}", headers=auth_headers)
    assert resp.status_code == 204
    assert await db_session.get(MediaAsset, asset.id) is None
    storage_mocks["delete_file"].assert_awaited_once_with(key)


@pytest.mark.asyncio
async def test_delete_of_someone_elses_photo_is_a_404(
    client: AsyncClient, auth_headers, db_session, storage_mocks
):
    from app.api.v1.users import get_password_hash
    from app.models import User

    other = User(
        email="other3@example.com",
        username="other3",
        hashed_password=get_password_hash("pw12345678"),
    )
    db_session.add(other)
    await db_session.flush()
    asset = MediaAsset(
        producer_id=other.id,
        storage_key=f"photos/{other.id}/period/childhood/x.jpg",
        category="childhood",
    )
    db_session.add(asset)
    await db_session.commit()

    resp = await client.delete(f"/api/v1/media/{asset.id}", headers=auth_headers)
    assert resp.status_code == 404
    assert await db_session.get(MediaAsset, asset.id) is not None
    storage_mocks["delete_file"].assert_not_awaited()


@pytest.mark.asyncio
async def test_deleting_the_primary_promotes_the_earliest_survivor(
    client: AsyncClient, auth_headers, db_session, test_user, storage_mocks
):
    """An entity with photos must never render faceless — the same reasoning
    as auto-primary on first upload, applied to the delete path."""
    import datetime

    entity = _entity(test_user)
    db_session.add(entity)
    await db_session.flush()
    base = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    face = MediaAsset(
        producer_id=test_user.id,
        storage_key=f"photos/{test_user.id}/entity/{entity.id}/face.jpg",
        entity_id=entity.id,
        is_primary=True,
        created_at=base,
    )
    older = MediaAsset(
        producer_id=test_user.id,
        storage_key=f"photos/{test_user.id}/entity/{entity.id}/older.jpg",
        entity_id=entity.id,
        created_at=base + datetime.timedelta(minutes=1),
    )
    newer = MediaAsset(
        producer_id=test_user.id,
        storage_key=f"photos/{test_user.id}/entity/{entity.id}/newer.jpg",
        entity_id=entity.id,
        created_at=base + datetime.timedelta(minutes=2),
    )
    db_session.add_all([face, older, newer])
    await db_session.commit()

    resp = await client.delete(f"/api/v1/media/{face.id}", headers=auth_headers)
    assert resp.status_code == 204

    await db_session.refresh(older)
    await db_session.refresh(newer)
    assert older.is_primary is True
    assert newer.is_primary is False


@pytest.mark.asyncio
async def test_a_failed_file_delete_does_not_resurrect_the_row(
    client: AsyncClient, auth_headers, db_session, test_user, storage_mocks
):
    """Row first, file best-effort: an orphaned file is invisible and
    re-sweepable, a live row with a dead file is a broken image."""
    asset = MediaAsset(
        producer_id=test_user.id,
        storage_key=f"photos/{test_user.id}/period/childhood/x.jpg",
        category="childhood",
    )
    db_session.add(asset)
    await db_session.commit()
    storage_mocks["delete_file"].side_effect = RuntimeError("storage down")

    resp = await client.delete(f"/api/v1/media/{asset.id}", headers=auth_headers)
    assert resp.status_code == 204
    assert await db_session.get(MediaAsset, asset.id) is None


# ── role gate ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_family_accounts_cannot_upload_photos(
    client: AsyncClient, db_session, test_user, storage_mocks
):
    """Producer-only for now. Family READ access arrives with Phase 8
    (§9.4) as a /talk read path, not through these endpoints."""
    from app.api.v1.users import create_access_token, get_password_hash
    from app.models import User

    fam = User(
        email="fam@example.com",
        username="fam",
        hashed_password=get_password_hash("pw12345678"),
        role="family",
        producer_id=test_user.id,
    )
    db_session.add(fam)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {create_access_token(data={'sub': fam.id})}"}

    resp = await client.post(
        "/api/v1/media/presign",
        json={"category": "childhood", "content_type": "image/jpeg"},
        headers=headers,
    )
    assert resp.status_code == 403


# ── photo_url on the render surfaces (Phase 3) ───────────────────────────────


@pytest.mark.asyncio
async def test_tree_nodes_carry_the_primary_photo_url(
    db_session, test_user, storage_mocks
):
    """The node's small circle shows the face when one exists and the
    placeholder when none does — absence is not an error (§9.6)."""
    from app.services import family_tree

    root = _entity(test_user, name="Tal", normalized_name="tal", is_self=True)
    uncle = _entity(test_user, name="אמנון", normalized_name="אמנון")
    db_session.add_all([root, uncle])
    await db_session.flush()
    db_session.add(
        MediaAsset(
            producer_id=test_user.id,
            storage_key=f"photos/{test_user.id}/entity/{uncle.id}/face.jpg",
            entity_id=uncle.id,
            is_primary=True,
        )
    )
    await db_session.commit()

    tree = await family_tree.build_tree(db_session, test_user.id)
    by_id = {p["id"]: p for p in tree["unplaced"]}
    by_id.update(
        {p["id"]: p for g in tree["generations"] for p in g["people"]}
    )
    assert by_id[uncle.id]["photo_url"] == (
        f"http://media/photos/{test_user.id}/entity/{uncle.id}/face.jpg"
    )
    assert by_id[root.id]["photo_url"] is None


@pytest.mark.asyncio
async def test_extraction_entities_carry_id_and_photo_url(
    db_session, test_user, storage_mocks
):
    """The panel's portrait upload needs a real entity id to attach to — a
    name is not a handle, two people can share one."""
    from app.models import EntityMention, InterviewSession, RawSegment
    from app.services.segment_extraction import _load_entities

    session = InterviewSession(user_id=test_user.id, status="active")
    db_session.add(session)
    await db_session.flush()
    segment = RawSegment(
        interview_session_id=session.id,
        question_asked="ספר לי על הדוד",
        question_index=0,
        status="ready",
    )
    entity = _entity(test_user)
    db_session.add_all([segment, entity])
    await db_session.flush()
    db_session.add(
        EntityMention(
            entity_id=entity.id, raw_segment_id=segment.id, summary="דוד של הדובר"
        )
    )
    db_session.add(
        MediaAsset(
            producer_id=test_user.id,
            storage_key=f"photos/{test_user.id}/entity/{entity.id}/face.jpg",
            entity_id=entity.id,
            is_primary=True,
        )
    )
    await db_session.commit()

    loaded = await _load_entities(db_session, segment.id, test_user.id)
    assert len(loaded) == 1
    assert loaded[0].entity_id == entity.id
    assert loaded[0].photo_url == (
        f"http://media/photos/{test_user.id}/entity/{entity.id}/face.jpg"
    )
    assert loaded[0].summary == "דוד של הדובר"
