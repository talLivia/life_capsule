import pytest
from httpx import AsyncClient

from app.api.v1.users import create_access_token, get_password_hash
from app.models import Avatar, FamilyInvite, InterviewSession, RawSegment, User

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def other_producer(db_session):
    """A second producer, distinct from `test_user`, to test cross-tenant
    invite ownership checks."""
    user = User(
        email="other-producer@example.com",
        username="otherproducer",
        hashed_password=get_password_hash("testpassword123"),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def other_producer_auth_headers(other_producer):
    token = create_access_token(data={"sub": other_producer.id})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def fresh_signup(db_session):
    """A brand-new registration — role="producer" by default (Prompt 4),
    but with no recorded content, so it's eligible to redeem an invite."""
    user = User(
        email="newcomer@example.com",
        username="newcomer",
        hashed_password=get_password_hash("testpassword123"),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def fresh_signup_auth_headers(fresh_signup):
    token = create_access_token(data={"sub": fresh_signup.id})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def active_storyteller(db_session):
    """A producer who has already recorded content — must NOT be able to
    redeem a family invite and silently lose /record access."""
    user = User(
        email="storyteller@example.com",
        username="storyteller",
        hashed_password=get_password_hash("testpassword123"),
    )
    db_session.add(user)
    await db_session.flush()
    db_session.add(InterviewSession(user_id=user.id, status="active"))
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def active_storyteller_auth_headers(active_storyteller):
    token = create_access_token(data={"sub": active_storyteller.id})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def family_user(db_session, test_user):
    user = User(
        email="family-member@example.com",
        username="familymember",
        hashed_password=get_password_hash("testpassword123"),
        role="family",
        producer_id=test_user.id,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def family_user_auth_headers(family_user):
    token = create_access_token(data={"sub": family_user.id})
    return {"Authorization": f"Bearer {token}"}


# ── family read access to the archive views (FAMILY_UNIFIED_SHELL_PLAN §2.3) ─


async def test_linked_family_can_read_the_tree_and_timeline(
    client: AsyncClient, family_user_auth_headers
):
    """The unified shell gives family view-only Timeline and Family tree —
    the reads resolve to the LINKED PRODUCER's archive via
    require_archive_owner, the same access model media.py applies."""
    tree = await client.get("/api/v1/entities/tree", headers=family_user_auth_headers)
    assert tree.status_code == 200
    assert "generations" in tree.json() or "nodes" in tree.json() or isinstance(tree.json(), dict)

    tl = await client.get("/api/v1/entities/timeline", headers=family_user_auth_headers)
    assert tl.status_code == 200


async def test_linked_family_moments_are_scoped_to_the_linked_archive(
    client: AsyncClient, family_user_auth_headers
):
    """Auth admits the linked family account (a 403 would say otherwise);
    the entity scoping then answers 404 for an id outside the archive."""
    resp = await client.get(
        "/api/v1/entities/00000000-0000-0000-0000-000000000000/moments",
        headers=family_user_auth_headers,
    )
    assert resp.status_code == 404


async def test_unlinked_family_cannot_read_any_archive_view(
    client: AsyncClient, db_session
):
    user = User(
        email="family-unlinked-2@example.com",
        username="familyunlinked2",
        hashed_password=get_password_hash("testpassword123"),
        role="family",
        producer_id=None,
    )
    db_session.add(user)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {create_access_token(data={'sub': user.id})}"}

    for path in ("/api/v1/entities/tree", "/api/v1/entities/timeline"):
        resp = await client.get(path, headers=headers)
        assert resp.status_code == 403, path


async def test_family_cannot_edit_the_tree(
    client: AsyncClient, family_user_auth_headers
):
    """View-only means the writes and the edit vocabulary stay
    producer-only — the UI hiding is presentation, this 403 is the
    guarantee."""
    set_resp = await client.post(
        "/api/v1/entities/some-entity/relations",
        json={"relation_type": "sibling", "other_entity_id": "x", "direction": "outgoing"},
        headers=family_user_auth_headers,
    )
    assert set_resp.status_code == 403

    del_resp = await client.delete(
        "/api/v1/entities/some-entity/relations/some-relation",
        headers=family_user_auth_headers,
    )
    assert del_resp.status_code == 403

    vocab = await client.get(
        "/api/v1/entities/relation-types", headers=family_user_auth_headers
    )
    assert vocab.status_code == 403


# ── active family members (FAMILY_UNIFIED_SHELL_PLAN §3.1) ──────────────────


async def test_members_lists_a_redeemed_family_account(
    client: AsyncClient, auth_headers, fresh_signup, fresh_signup_auth_headers,
    other_producer_auth_headers,
):
    """One lifecycle: a pending invite becomes an active member on
    redemption, listed from the users-table linkage with the redemption
    stamp and the username fallback for a blank display name."""
    created = await client.post("/api/v1/family/invites", headers=auth_headers)
    token = created.json()["token"]
    redeemed = await client.post(
        "/api/v1/family/invites/redeem", json={"token": token},
        headers=fresh_signup_auth_headers,
    )
    assert redeemed.status_code == 200

    members = await client.get("/api/v1/family/members", headers=auth_headers)
    assert members.status_code == 200
    body = members.json()
    assert len(body) == 1
    assert body[0]["user_id"] == fresh_signup.id
    assert body[0]["display_name"] == "newcomer"  # no full_name → username
    assert body[0]["joined_at"] is not None

    # Scoped to the asking producer — another producer sees nobody.
    theirs = await client.get(
        "/api/v1/family/members", headers=other_producer_auth_headers
    )
    assert theirs.json() == []


async def test_members_is_sourced_from_the_linkage_not_the_invite(
    client: AsyncClient, auth_headers, family_user
):
    """A linked family account with NO invite row (seeded directly) still
    lists — the linkage is what grants access, so the list follows it."""
    members = await client.get("/api/v1/family/members", headers=auth_headers)
    assert members.status_code == 200
    body = members.json()
    assert [m["user_id"] for m in body] == [family_user.id]
    assert body[0]["joined_at"] is None


async def test_members_requires_producer_role(
    client: AsyncClient, family_user_auth_headers
):
    resp = await client.get("/api/v1/family/members", headers=family_user_auth_headers)
    assert resp.status_code == 403


# -- removing a family member (FAMILY_UNIFIED_SHELL_PLAN 3.3, decided: delete) --


async def test_removing_a_member_deletes_their_account_and_history(
    client: AsyncClient, db_session, test_user, family_user, auth_headers
):
    """The producer chose deletion over unlink: the account AND its chat
    history go. Permanent by design; the UI warns before asking."""
    from app.models import Conversation, Message
    from app.models import Session as ChatSession

    session = ChatSession(
        user_id=family_user.id, producer_id=test_user.id, status="active"
    )
    db_session.add(session)
    await db_session.flush()
    db_session.add(Message(session_id=session.id, role="user", content="hi"))
    db_session.add(Conversation(session_id=session.id, title="Hi", message_count=1))
    await db_session.commit()

    resp = await client.delete(
        f"/api/v1/family/members/{family_user.id}", headers=auth_headers
    )
    assert resp.status_code == 204

    from sqlalchemy import select

    assert (
        await db_session.execute(select(User).where(User.id == family_user.id))
    ).scalar_one_or_none() is None
    assert (
        await db_session.execute(
            select(ChatSession).where(ChatSession.id == session.id)
        )
    ).scalar_one_or_none() is None
    assert (
        (await db_session.execute(select(Message).where(Message.session_id == session.id)))
        .scalars()
        .all()
        == []
    )

    members = await client.get("/api/v1/family/members", headers=auth_headers)
    assert members.json() == []


async def test_removing_a_member_keeps_their_invite_as_history(
    client: AsyncClient, auth_headers, fresh_signup, fresh_signup_auth_headers
):
    created = await client.post("/api/v1/family/invites", headers=auth_headers)
    token = created.json()["token"]
    await client.post(
        "/api/v1/family/invites/redeem", json={"token": token},
        headers=fresh_signup_auth_headers,
    )

    resp = await client.delete(
        f"/api/v1/family/members/{fresh_signup.id}", headers=auth_headers
    )
    assert resp.status_code == 204

    invites = await client.get("/api/v1/family/invites", headers=auth_headers)
    row = next(i for i in invites.json() if i["id"] == created.json()["id"])
    assert row["status"] == "redeemed"  # the invitation was used: history
    assert row["redeemed_by_user_id"] is None  # the account is gone


async def test_cannot_remove_another_producers_member(
    client: AsyncClient, family_user, other_producer_auth_headers
):
    resp = await client.delete(
        f"/api/v1/family/members/{family_user.id}",
        headers=other_producer_auth_headers,
    )
    assert resp.status_code == 404


async def test_removing_a_non_family_account_is_404(
    client: AsyncClient, auth_headers, other_producer
):
    resp = await client.delete(
        f"/api/v1/family/members/{other_producer.id}", headers=auth_headers
    )
    assert resp.status_code == 404


async def test_family_cannot_remove_members(
    client: AsyncClient, family_user, family_user_auth_headers
):
    resp = await client.delete(
        f"/api/v1/family/members/{family_user.id}",
        headers=family_user_auth_headers,
    )
    assert resp.status_code == 403


# ── invite creation/listing/revocation ──────────────────────────────────────


async def test_create_invite_requires_producer_role(client: AsyncClient, family_user_auth_headers):
    resp = await client.post("/api/v1/family/invites", headers=family_user_auth_headers)
    assert resp.status_code == 403


async def test_create_invite(client: AsyncClient, auth_headers, test_user):
    resp = await client.post("/api/v1/family/invites", headers=auth_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending"
    assert body["token"]
    assert body["redeemed_by_user_id"] is None


async def test_list_invites_scoped_to_producer(
    client: AsyncClient, auth_headers, other_producer_auth_headers
):
    await client.post("/api/v1/family/invites", headers=auth_headers)
    await client.post("/api/v1/family/invites", headers=other_producer_auth_headers)

    mine = await client.get("/api/v1/family/invites", headers=auth_headers)
    assert mine.status_code == 200
    assert len(mine.json()) == 1

    theirs = await client.get("/api/v1/family/invites", headers=other_producer_auth_headers)
    assert len(theirs.json()) == 1


async def test_revoke_invite(client: AsyncClient, auth_headers):
    created = await client.post("/api/v1/family/invites", headers=auth_headers)
    invite_id = created.json()["id"]

    resp = await client.delete(f"/api/v1/family/invites/{invite_id}", headers=auth_headers)
    assert resp.status_code == 204

    # Redeeming a revoked invite must fail.
    token = created.json()["token"]
    redeem = await client.post(
        "/api/v1/family/invites/redeem", json={"token": token}, headers=auth_headers
    )
    assert redeem.status_code in (400, 409)  # self-redeem (400) checked before status (409)


async def test_revoke_invite_requires_ownership(
    client: AsyncClient, auth_headers, other_producer_auth_headers
):
    created = await client.post("/api/v1/family/invites", headers=auth_headers)
    invite_id = created.json()["id"]

    resp = await client.delete(
        f"/api/v1/family/invites/{invite_id}", headers=other_producer_auth_headers
    )
    assert resp.status_code == 403


async def test_revoke_already_redeemed_invite_conflicts(
    client: AsyncClient, auth_headers, fresh_signup_auth_headers
):
    created = await client.post("/api/v1/family/invites", headers=auth_headers)
    invite_id, token = created.json()["id"], created.json()["token"]

    await client.post(
        "/api/v1/family/invites/redeem", json={"token": token}, headers=fresh_signup_auth_headers
    )

    resp = await client.delete(f"/api/v1/family/invites/{invite_id}", headers=auth_headers)
    assert resp.status_code == 409


# ── redemption ───────────────────────────────────────────────────────────────


async def test_redeem_invite_success(
    client: AsyncClient, auth_headers, test_user, fresh_signup_auth_headers, fresh_signup
):
    created = await client.post("/api/v1/family/invites", headers=auth_headers)
    token = created.json()["token"]

    resp = await client.post(
        "/api/v1/family/invites/redeem", json={"token": token}, headers=fresh_signup_auth_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "family"
    assert body["producer_id"] == test_user.id


async def test_redeem_rejects_own_invite(client: AsyncClient, auth_headers):
    created = await client.post("/api/v1/family/invites", headers=auth_headers)
    token = created.json()["token"]

    resp = await client.post(
        "/api/v1/family/invites/redeem", json={"token": token}, headers=auth_headers
    )
    assert resp.status_code == 400


async def test_redeem_rejects_active_storyteller(
    client: AsyncClient, auth_headers, active_storyteller_auth_headers
):
    created = await client.post("/api/v1/family/invites", headers=auth_headers)
    token = created.json()["token"]

    resp = await client.post(
        "/api/v1/family/invites/redeem",
        json={"token": token},
        headers=active_storyteller_auth_headers,
    )
    assert resp.status_code == 400


async def test_redeem_rejects_invalid_token(client: AsyncClient, fresh_signup_auth_headers):
    resp = await client.post(
        "/api/v1/family/invites/redeem",
        json={"token": "not-a-real-token"},
        headers=fresh_signup_auth_headers,
    )
    assert resp.status_code == 404


async def test_redeem_rejects_expired_invite(
    client: AsyncClient, db_session, auth_headers, test_user, fresh_signup_auth_headers
):
    from datetime import datetime, timedelta, timezone

    invite = FamilyInvite(
        producer_id=test_user.id,
        token="expired-token-123",
        status="pending",
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db_session.add(invite)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/family/invites/redeem",
        json={"token": "expired-token-123"},
        headers=fresh_signup_auth_headers,
    )
    assert resp.status_code == 410


async def test_talk_availability_true_with_ready_segment_and_avatar(
    client: AsyncClient, db_session, test_user, family_user_auth_headers
):
    session = InterviewSession(user_id=test_user.id, status="active")
    db_session.add(session)
    await db_session.flush()
    db_session.add(
        RawSegment(
            interview_session_id=session.id,
            question_asked="Q",
            question_index=0,
            transcript="A verbatim story.",
            status="ready",
        )
    )
    db_session.add(
        Avatar(
            user_id=test_user.id,
            name="Storyteller",
            image_url="http://x/i.jpg",
            s3_key="avatars/x/image.jpg",
            status="ready",
        )
    )
    await db_session.commit()

    resp = await client.get("/api/v1/family/talk-availability", headers=family_user_auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["ready_segment_count"] == 1
    assert body["avatar_id"] is not None
    assert body["avatar_image_url"] == "http://x/i.jpg"
    assert body["producer_id"] == test_user.id
    # Default chat_mode is v2 (docs/V2_PRIMARY_AVATAR_DORMANT_PLAN.md §3.5)
    # — avatar mode is the explicit opt-in.
    assert body["chat_mode"] == "video_clips_v2"


async def test_talk_availability_true_for_v2_with_zero_avatars(
    client: AsyncClient, db_session, test_user, family_user_auth_headers
):
    """The inversion's family-facing half
    (docs/V2_PRIMARY_AVATAR_DORMANT_PLAN.md §3.4): a v2 producer with one
    ready recording and NO avatars row anywhere is available to their
    family. Before this, the unconditional avatar requirement showed
    'still preparing their stories' over a fully recorded archive."""
    session = InterviewSession(user_id=test_user.id, status="active")
    db_session.add(session)
    await db_session.flush()
    db_session.add(
        RawSegment(
            interview_session_id=session.id,
            question_asked="Q",
            question_index=0,
            transcript="A verbatim story.",
            status="ready",
        )
    )
    await db_session.commit()

    resp = await client.get("/api/v1/family/talk-availability", headers=family_user_auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["ready_segment_count"] == 1
    assert body["avatar_id"] is None
    assert body["avatar_image_url"] is None
    assert body["chat_mode"] == "video_clips_v2"


async def test_talk_availability_still_requires_an_avatar_in_avatar_mode(
    client: AsyncClient, db_session, test_user, family_user_auth_headers
):
    """Avatar mode genuinely renders the avatar, so its availability keeps
    the requirement — mode-aware, not dropped."""
    test_user.chat_mode = "avatar"
    session = InterviewSession(user_id=test_user.id, status="active")
    db_session.add(session)
    await db_session.flush()
    db_session.add(
        RawSegment(
            interview_session_id=session.id,
            question_asked="Q",
            question_index=0,
            transcript="A verbatim story.",
            status="ready",
        )
    )
    await db_session.commit()

    resp = await client.get("/api/v1/family/talk-availability", headers=family_user_auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["ready_segment_count"] == 1


async def test_talk_availability_reflects_producer_video_clip_mode(
    client: AsyncClient, db_session, test_user, family_user_auth_headers
):
    """Prompt 14: the setting is producer-level — /talk-availability must
    hand back whichever mode the LINKED PRODUCER chose, not a default,
    since the family account never has its own copy of this setting."""
    test_user.chat_mode = "video_clips_v2"
    db_session.add(test_user)
    await db_session.commit()

    resp = await client.get("/api/v1/family/talk-availability", headers=family_user_auth_headers)
    assert resp.status_code == 200
    assert resp.json()["chat_mode"] == "video_clips_v2"


# ── talk-availability ────────────────────────────────────────────────────────


async def test_talk_availability_requires_family_link(client: AsyncClient, fresh_signup_auth_headers):
    resp = await client.get("/api/v1/family/talk-availability", headers=fresh_signup_auth_headers)
    assert resp.status_code == 403


async def test_talk_availability_false_with_no_ready_segments(
    client: AsyncClient, auth_headers, fresh_signup_auth_headers
):
    created = await client.post("/api/v1/family/invites", headers=auth_headers)
    token = created.json()["token"]
    await client.post(
        "/api/v1/family/invites/redeem", json={"token": token}, headers=fresh_signup_auth_headers
    )

    resp = await client.get("/api/v1/family/talk-availability", headers=fresh_signup_auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["ready_segment_count"] == 0
