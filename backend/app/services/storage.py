"""
Storage service — local filesystem (default), Cloudflare R2, or AWS S3.

Set USE_LOCAL_STORAGE=false to switch to a remote provider, selected via
STORAGE_PROVIDER ("r2", the default, or "s3" for the base project's
original AWS path). In local mode files are saved under
LOCAL_STORAGE_PATH and served via FastAPI StaticFiles at /uploads/...
"""

import logging
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


def _local_path(key: str) -> Path:
    return Path(settings.LOCAL_STORAGE_PATH) / key


def _local_url(key: str) -> str:
    return f"{settings.BACKEND_URL}/uploads/{key}"


# ── Local storage ─────────────────────────────────────────────────────────────


class LocalStorageService:
    """Store files on the local filesystem and serve them via /uploads/."""

    async def initialize(self):
        Path(settings.LOCAL_STORAGE_PATH).mkdir(parents=True, exist_ok=True)
        logger.info(f"Local storage initialised at {settings.LOCAL_STORAGE_PATH}")

    async def upload_file(
        self,
        file_data: bytes,
        key: str,
        content_type: str = "application/octet-stream",
        metadata: Optional[dict] = None,
    ) -> str:
        dest = _local_path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(file_data)
        url = _local_url(key)
        logger.debug(f"Saved {key} → {url}")
        return url

    async def download_file(self, key: str) -> bytes:
        p = _local_path(key)
        if not p.exists():
            raise FileNotFoundError(f"Local file not found: {key}")
        return p.read_bytes()

    def get_local_path(self, key: str) -> str:
        return str(_local_path(key).resolve())

    async def delete_file(self, key: str):
        _local_path(key).unlink(missing_ok=True)

    def get_url(self, key: str) -> str:
        return _local_url(key)

    async def serving_url(self, key: str, ttl_seconds: int = 3600) -> str:
        """URL the browser can actually fetch. Local files are public at /uploads/."""
        return _local_url(key)

    async def cleanup(self):
        logger.info("Local storage cleanup complete")


# ── S3 storage ────────────────────────────────────────────────────────────────


class S3StorageService:
    """AWS S3 storage (used when USE_LOCAL_STORAGE=false)."""

    def __init__(self):
        import aioboto3
        import boto3

        self.bucket_name = settings.S3_BUCKET_NAME
        self.region = settings.AWS_REGION
        self.cloudfront_domain = settings.CLOUDFRONT_DOMAIN

        self.s3_client = boto3.client(
            "s3",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=self.region,
        )
        self.session = aioboto3.Session(
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=self.region,
        )

    async def initialize(self):
        from botocore.exceptions import ClientError

        try:
            async with self.session.client("s3") as s3:
                try:
                    await s3.head_bucket(Bucket=self.bucket_name)
                    logger.info(f"S3 bucket {self.bucket_name} exists")
                except ClientError:
                    logger.info(f"Creating S3 bucket {self.bucket_name}")
                    await s3.create_bucket(
                        Bucket=self.bucket_name,
                        CreateBucketConfiguration={"LocationConstraint": self.region},
                    )
        except Exception as e:
            logger.error(f"Failed to initialise S3: {e}")
            raise

    async def upload_file(
        self,
        file_data: bytes,
        key: str,
        content_type: str = "application/octet-stream",
        metadata: Optional[dict] = None,
    ) -> str:
        async with self.session.client("s3") as s3:
            # Private by default — keeps avatar images, voice references, and
            # generated session videos from being world-readable via guessable
            # URLs. Callers that need a public-facing link should call
            # `presigned_url(key, ttl_seconds=...)` and pass the result to
            # the client. CloudFront with origin-access can serve these too.
            extra: dict = {"ContentType": content_type, "ACL": "private"}
            if metadata:
                extra["Metadata"] = metadata
            await s3.put_object(Bucket=self.bucket_name, Key=key, Body=file_data, **extra)
        return self.get_url(key)

    async def presigned_url(self, key: str, ttl_seconds: int = 3600) -> str:
        """
        Generate a time-limited signed URL for an S3 object. Used by the
        WebSocket pipeline when handing video chunk URLs to the client —
        clients get a short-lived token instead of a permanent reference.
        """
        # boto3's generate_presigned_url is sync but cheap (no network call).
        return self.s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket_name, "Key": key},
            ExpiresIn=ttl_seconds,
        )

    async def download_file(self, key: str) -> bytes:
        async with self.session.client("s3") as s3:
            resp = await s3.get_object(Bucket=self.bucket_name, Key=key)
            async with resp["Body"] as stream:
                return await stream.read()

    def get_local_path(self, key: str) -> str:
        raise NotImplementedError("S3 has no local path; use download_file()")

    async def delete_file(self, key: str):
        async with self.session.client("s3") as s3:
            await s3.delete_object(Bucket=self.bucket_name, Key=key)

    def get_url(self, key: str) -> str:
        if self.cloudfront_domain:
            return f"https://{self.cloudfront_domain}/{key}"
        return f"https://{self.bucket_name}.s3.{self.region}.amazonaws.com/{key}"

    async def serving_url(self, key: str, ttl_seconds: int = 3600) -> str:
        """
        URL the browser can actually fetch. Objects are uploaded with
        ACL=private, so the raw bucket URL 403s — return CloudFront (which
        has origin access) when configured, else a time-limited presigned URL.
        """
        if self.cloudfront_domain:
            return f"https://{self.cloudfront_domain}/{key}"
        return await self.presigned_url(key, ttl_seconds)

    async def cleanup(self):
        logger.info("S3 storage cleanup complete")


# ── R2 storage ────────────────────────────────────────────────────────────────


class R2StorageService:
    """
    Cloudflare R2 storage (S3-compatible API) — this project's object store
    for raw interview video/audio. Used when USE_LOCAL_STORAGE=false and
    STORAGE_PROVIDER=r2 (the default). See S3StorageService above for the
    base project's original AWS path, kept only for its optional
    Terraform/EC2 deploy route.

    R2 has no AWS-style regions and (unlike S3) no egress fees, which is
    why raw video — much larger than the base project's avatar images —
    targets it instead.
    """

    def __init__(self):
        import aioboto3
        import boto3

        self.bucket_name = settings.R2_BUCKET_NAME
        self.endpoint_url = settings.R2_ENDPOINT
        self.public_url = settings.R2_PUBLIC_URL

        self._client_kwargs = dict(
            endpoint_url=self.endpoint_url,
            aws_access_key_id=settings.R2_ACCESS_KEY_ID,
            aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
            # R2 only has one (virtual) region; boto3 still requires some
            # value to be set for SigV4 signing.
            region_name="auto",
        )
        self.s3_client = boto3.client("s3", **self._client_kwargs)
        self.session = aioboto3.Session()

    async def initialize(self):
        from botocore.exceptions import ClientError

        try:
            async with self.session.client("s3", **self._client_kwargs) as s3:
                try:
                    await s3.head_bucket(Bucket=self.bucket_name)
                    logger.info(f"R2 bucket {self.bucket_name} exists")
                except ClientError:
                    logger.info(f"Creating R2 bucket {self.bucket_name}")
                    # R2 rejects AWS's CreateBucketConfiguration/
                    # LocationConstraint param — omit it entirely.
                    await s3.create_bucket(Bucket=self.bucket_name)
        except Exception as e:
            logger.error(f"Failed to initialise R2: {e}")
            raise

    async def upload_file(
        self,
        file_data: bytes,
        key: str,
        content_type: str = "application/octet-stream",
        metadata: Optional[dict] = None,
    ) -> str:
        async with self.session.client("s3", **self._client_kwargs) as s3:
            extra: dict = {"ContentType": content_type}
            if metadata:
                extra["Metadata"] = metadata
            await s3.put_object(Bucket=self.bucket_name, Key=key, Body=file_data, **extra)
        return self.get_url(key)

    async def presigned_upload_url(
        self,
        key: str,
        content_type: str = "application/octet-stream",
        ttl_seconds: int = 3600,
    ) -> str:
        """
        Presigned PUT URL so the browser can upload raw interview video
        straight to R2 without it passing through the FastAPI backend
        (Prompt 4's `/record` upload flow). The caller must PUT with the
        exact same Content-Type used here — R2 (like S3) includes it in
        the signature.
        """
        return self.s3_client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket_name, "Key": key, "ContentType": content_type},
            ExpiresIn=ttl_seconds,
        )

    async def presigned_url(self, key: str, ttl_seconds: int = 3600) -> str:
        """Presigned GET URL — mirrors S3StorageService.presigned_url."""
        return self.s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket_name, "Key": key},
            ExpiresIn=ttl_seconds,
        )

    async def download_file(self, key: str) -> bytes:
        async with self.session.client("s3", **self._client_kwargs) as s3:
            resp = await s3.get_object(Bucket=self.bucket_name, Key=key)
            async with resp["Body"] as stream:
                return await stream.read()

    def get_local_path(self, key: str) -> str:
        raise NotImplementedError("R2 has no local path; use download_file()")

    async def delete_file(self, key: str):
        async with self.session.client("s3", **self._client_kwargs) as s3:
            await s3.delete_object(Bucket=self.bucket_name, Key=key)

    def get_url(self, key: str) -> str:
        if self.public_url:
            return f"{self.public_url.rstrip('/')}/{key}"
        # No custom domain configured — the raw R2 endpoint isn't
        # browser-readable without a presigned URL, so this is only a
        # reference key, not something to hand to a client directly.
        return f"{self.endpoint_url}/{self.bucket_name}/{key}"

    async def serving_url(self, key: str, ttl_seconds: int = 3600) -> str:
        """URL the browser can actually fetch."""
        if self.public_url:
            return f"{self.public_url.rstrip('/')}/{key}"
        return await self.presigned_url(key, ttl_seconds)

    async def cleanup(self):
        logger.info("R2 storage cleanup complete")


# ── Factory ───────────────────────────────────────────────────────────────────


def _build_storage_service():
    if getattr(settings, "USE_LOCAL_STORAGE", True):
        logger.info("Using LOCAL filesystem storage")
        return LocalStorageService()
    provider = getattr(settings, "STORAGE_PROVIDER", "r2")
    if provider == "r2":
        logger.info("Using Cloudflare R2 storage")
        return R2StorageService()
    logger.info("Using AWS S3 storage")
    return S3StorageService()


storage_service = _build_storage_service()
