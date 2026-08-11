"""
Photos on entities and life periods — docs/MEDIA_GALLERY.md Phase 2.

The upload flow is deliberately the same shape as recording a segment:
presign → PUT → a POST that writes the row (§5). There is no second storage
path — CLAUDE.md's rule for video ("if you find yourself writing one,
something is wrong") applies here for the same reason.

What this file guards, and where:

- **Owner** — exactly one of entity/category, checked at presign AND encoded
  in the storage key itself, so the row-write cannot be told a different
  owner than the key it was issued for. The entity must be the producer's
  own; the category must exist in interview_config (live or retired — a
  retired-only category still renders on the timeline).
- **Type** — jpeg/png/webp only. HEIC — what an iPhone actually produces —
  is rejected WITH A MESSAGE THAT SAYS SO rather than accepted and stored
  as something no browser can render (§5, open decision 2).
- **Size** — enforced at the local PUT where the bytes pass through us, and
  re-checked at the row-write via a HEAD on the object, because in R2 mode
  the browser PUTs straight to storage and a presigned URL carries no size
  condition. The row-write check is the one that always holds.
- **Photos are not questions** — nothing here touches confirmation,
  ingestion, or `pending_confirmation` (§5).

Deletion removes the row first, then best-effort deletes the file: no
transaction spans Postgres and object storage, and an orphaned file is the
accepted direction of that trade (§2.3) — the reverse (live row, dead file)
would be a broken image on a page.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import interview_config
from app.api.v1.interview import require_producer
from app.api.v1.users import require_current_user
from app.config import settings
from app.database import get_db
from app.models import Entity, MediaAsset, User
from app.schemas import (
    MediaAssetResponse,
    MediaCreateRequest,
    MediaPresignRequest,
    MediaPresignResponse,
)
from app.services.storage import storage_service

logger = logging.getLogger(__name__)
router = APIRouter()

_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}

# Named separately so the rejection can say what the actual problem is —
# "unsupported type" on the format every iPhone produces by default would
# read as the app being broken, not the photo needing an export.
_HEIC_TYPES = {"image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence"}


def _validated_content_type(content_type: str) -> str:
    base_type = (content_type or "").split(";", 1)[0].strip().lower()
    if base_type in _HEIC_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                "iPhone HEIC photos aren't supported yet — please export or "
                "convert the photo to JPEG and upload that instead"
            ),
        )
    if base_type not in _EXT_BY_CONTENT_TYPE:
        raise HTTPException(
            status_code=400,
            detail=(
                "That file type isn't supported — please upload a photo "
                "(.jpg, .png or .webp)"
            ),
        )
    return base_type


def _photo_key(user_id: str, entity_id: str | None, category: str | None, ext: str) -> str:
    # A fresh uuid per upload, same reasoning as segment keys: nothing is
    # ever overwritten, so an in-flight GET of a previous photo cannot break.
    if entity_id is not None:
        return f"photos/{user_id}/entity/{entity_id}/{uuid.uuid4()}.{ext}"
    return f"photos/{user_id}/period/{category}/{uuid.uuid4()}.{ext}"


def _parse_owned_key(storage_key: str, user_id: str) -> tuple[str | None, str | None]:
    """(entity_id, category) from a key THIS user was issued, or 400/403.

    The key is the contract between presign and the row-write: parsing the
    owner out of it (rather than accepting owner fields again) makes
    "row claims a different owner than the upload" structurally impossible.
    """
    parts = storage_key.split("/")
    valid_shape = (
        len(parts) == 5
        and parts[0] == "photos"
        and parts[2] in ("entity", "period")
        and all(parts[1:])
        and "." in parts[4]
        and parts[4].rsplit(".", 1)[1] in _EXT_BY_CONTENT_TYPE.values()
    )
    if not valid_shape:
        raise HTTPException(status_code=400, detail="Unrecognised storage key")
    if parts[1] != user_id:
        raise HTTPException(status_code=403, detail="Not authorised to use this key")
    if parts[2] == "entity":
        return parts[3], None
    return None, parts[3]


async def _to_response(asset: MediaAsset) -> MediaAssetResponse:
    return MediaAssetResponse(
        id=asset.id,
        kind=asset.kind,
        caption=asset.caption,
        taken_year=asset.taken_year,
        is_primary=asset.is_primary,
        entity_id=asset.entity_id,
        category=asset.category,
        url=await storage_service.serving_url(asset.storage_key),
        created_at=asset.created_at,
    )


async def _validate_owner(
    db: AsyncSession, user: User, entity_id: str | None, category: str | None
) -> None:
    """Exactly one owner, and it must be real — the same rule the CHECK
    constraint enforces on the row, applied where the message can be
    specific."""
    if (entity_id is None) == (category is None):
        raise HTTPException(
            status_code=400,
            detail="A photo belongs to exactly one of an entity or a category",
        )
    if entity_id is not None:
        entity = await db.scalar(
            select(Entity).where(
                Entity.id == entity_id, Entity.producer_id == user.id
            )
        )
        # 404 not 403: distinguishing "not yours" from "doesn't exist" would
        # confirm the id exists — same rule as the entities endpoints.
        if entity is None:
            raise HTTPException(status_code=404, detail="Entity not found")
    else:
        # Never from client input unvalidated (§2.2): the value must be a
        # category interview_config actually knows, live or retired.
        if not interview_config.is_valid_category(category):
            raise HTTPException(status_code=400, detail="Unknown category")


@router.post("/presign", response_model=MediaPresignResponse)
async def presign_photo_upload(
    payload: MediaPresignRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """Presigned upload target for one photo — same flow as segment video.

    In R2 (or S3) mode this is a real presigned PUT straight to object
    storage. In local-storage dev mode there is no such thing, so we hand
    back our own PUT endpoint below, which behaves identically from the
    frontend's point of view.
    """
    content_type = _validated_content_type(payload.content_type)
    await _validate_owner(db, user, payload.entity_id, payload.category)
    storage_key = _photo_key(
        user.id, payload.entity_id, payload.category, _EXT_BY_CONTENT_TYPE[content_type]
    )

    if getattr(settings, "USE_LOCAL_STORAGE", True):
        upload_url = f"{settings.BACKEND_URL}/api/v1/media/upload-local/{storage_key}"
    else:
        upload_url = await storage_service.presigned_upload_url(
            storage_key, content_type=content_type
        )

    return MediaPresignResponse(
        upload_url=upload_url, storage_key=storage_key, content_type=content_type
    )


@router.put("/upload-local/{storage_key:path}", status_code=status.HTTP_204_NO_CONTENT)
async def upload_local_photo(
    storage_key: str,
    request: Request,
    user: User = Depends(require_producer),
):
    """Local-storage-only counterpart to a presigned PUT — mirrors
    /interview/segments/upload-local, including the asymmetric size
    enforcement note there: here the bytes pass through us so the cap is
    real; in R2 mode the row-write's HEAD check is the guard instead."""
    if not getattr(settings, "USE_LOCAL_STORAGE", True):
        raise HTTPException(status_code=404, detail="Not found")
    if not storage_key.startswith(f"photos/{user.id}/"):
        raise HTTPException(status_code=403, detail="Not authorised to write this key")

    max_bytes = settings.MAX_PHOTO_UPLOAD_BYTES
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"That photo is too large (max {max_bytes // (1024 * 1024)} MB)",
        )

    content_type = _validated_content_type(
        request.headers.get("content-type", "image/jpeg")
    )
    body = await request.body()
    if len(body) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"That photo is too large (max {max_bytes // (1024 * 1024)} MB)",
        )

    await storage_service.upload_file(body, storage_key, content_type=content_type)


@router.post("", response_model=MediaAssetResponse, status_code=status.HTTP_201_CREATED)
async def create_media_asset(
    payload: MediaCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """Write the row for an uploaded photo — step 4 of the §5 flow.

    The owner comes out of the storage key, never from the request body, so
    the row can only ever claim the owner the presign was issued for. The
    object must actually exist and respect the size cap before a row points
    at it — in R2 mode this HEAD is the only server-side size enforcement
    there is.
    """
    entity_id, category = _parse_owned_key(payload.storage_key, user.id)
    await _validate_owner(db, user, entity_id, category)

    try:
        size = await storage_service.file_size(payload.storage_key)
    except FileNotFoundError:
        raise HTTPException(
            status_code=400,
            detail="No uploaded file found for this key — upload the photo first",
        )
    if size > settings.MAX_PHOTO_UPLOAD_BYTES:
        # Refuse the row AND remove the object: without this, an oversized
        # upload parked via presigned PUT would sit in storage forever with
        # nothing pointing at it and nothing enforcing the cap.
        await storage_service.delete_file(payload.storage_key)
        raise HTTPException(
            status_code=413,
            detail=(
                "That photo is too large "
                f"(max {settings.MAX_PHOTO_UPLOAD_BYTES // (1024 * 1024)} MB)"
            ),
        )

    is_primary = False
    if entity_id is not None:
        # The first photo of an entity becomes its face automatically — the
        # tree and cards need ONE portrait (§3.2), and an entity with photos
        # but no primary would render faceless for no reason a producer
        # could see.
        current_primary = await db.scalar(
            select(MediaAsset).where(
                MediaAsset.entity_id == entity_id,
                MediaAsset.is_primary,
            )
        )
        is_primary = current_primary is None
        if payload.make_primary and current_primary is not None:
            # Same transaction as the insert: the partial unique index means
            # there is never a moment with two faces, and a failed insert
            # rolls the demotion back with it.
            current_primary.is_primary = False
            is_primary = True

    asset = MediaAsset(
        producer_id=user.id,
        storage_key=payload.storage_key,
        kind="photo",
        caption=payload.caption,
        taken_year=payload.taken_year,
        is_primary=is_primary,
        entity_id=entity_id,
        category=category,
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return await _to_response(asset)


def _archive_owner_id(user: User) -> str:
    """Whose photos this account may READ — the §9.4 access model.

    The same producer-scoping /talk already answers for video: a producer
    reads their own archive; a family account (invite/redeem flow) reads the
    producer it is linked to — the identical `producer_id` linkage
    sessions.py checks before letting them start a session against that
    producer's avatar. An unlinked family account has no archive to read.
    Writes stay producer-only; this widens nothing but the list.
    """
    if user.role == "producer":
        return user.id
    if user.role == "family" and user.producer_id:
        return user.producer_id
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="This account is not linked to a story archive",
    )


@router.get("", response_model=list[MediaAssetResponse])
async def list_media_assets(
    entity_id: str | None = None,
    category: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_current_user),
):
    """One owner's gallery — the read every later phase consumes (§3, §4,
    and /talk's §9.4 union, which calls this once per category).

    Readable by the producer AND their linked family accounts (see
    `_archive_owner_id`) — the only /media route that is; every write stays
    producer-only.

    Ordering per §4.1/§4.3: primary first (an entity's face leads its own
    gallery), then `taken_year` when set, `created_at` otherwise — and the
    timeline itself never orders by these (its rule: years decorate, they
    never move).
    """
    if (entity_id is None) == (category is None):
        raise HTTPException(
            status_code=400, detail="Pass exactly one of entity_id or category"
        )

    query = select(MediaAsset).where(MediaAsset.producer_id == _archive_owner_id(user))
    if entity_id is not None:
        query = query.where(MediaAsset.entity_id == entity_id)
    else:
        query = query.where(MediaAsset.category == category)
    query = query.order_by(
        MediaAsset.is_primary.desc(),
        MediaAsset.taken_year.asc().nulls_last(),
        MediaAsset.created_at.asc(),
    )

    assets = (await db.scalars(query)).all()
    return [await _to_response(a) for a in assets]


@router.delete("/{media_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_media_asset(
    media_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """Remove a photo: the row transactionally, then the file best-effort.

    Row first — a failed file delete leaves an orphaned object (invisible,
    costs storage, re-sweepable), where the reverse order would leave a live
    row serving a broken image. If the deleted photo was an entity's
    primary, the earliest remaining photo is promoted so an entity with
    photos never renders faceless — the same reasoning as auto-primary on
    first upload.
    """
    asset = await db.scalar(
        select(MediaAsset).where(
            MediaAsset.id == media_id, MediaAsset.producer_id == user.id
        )
    )
    if asset is None:
        raise HTTPException(status_code=404, detail="Photo not found")

    storage_key = asset.storage_key
    was_primary_of = asset.entity_id if asset.is_primary else None
    await db.delete(asset)

    if was_primary_of is not None:
        successor = await db.scalar(
            select(MediaAsset)
            .where(
                MediaAsset.entity_id == was_primary_of,
                MediaAsset.id != media_id,
            )
            .order_by(MediaAsset.created_at.asc(), MediaAsset.id.asc())
            .limit(1)
        )
        if successor is not None:
            successor.is_primary = True

    await db.commit()

    try:
        await storage_service.delete_file(storage_key)
    except Exception as e:  # noqa: BLE001 — the row is gone; log, don't fail
        logger.warning(f"Photo file delete failed for {storage_key}: {e}")
