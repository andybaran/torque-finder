"""Cloudflare R2 (S3-compatible) async client.

Cross-cutting asset layer: original PDFs and rendered page PNGs live in R2.
Both ingestion (writes) and the API (reads / presigned URLs) go through this
class so the S3 endpoint, credentials, and bucket name are configured in
exactly one place.
"""

from __future__ import annotations

import aioboto3
from botocore.config import Config

from parts_lookup.config import Settings
from parts_lookup.domain.errors import IngestionError

_R2_REGION = "auto"


class R2Client:
    """Thin async wrapper around the aioboto3 S3 client targeting Cloudflare R2.

    Each method opens a fresh ``async with`` client context — aioboto3's
    clients are not safe to hold across await points without their own
    context manager, and R2 calls are infrequent enough that the per-call
    setup cost is negligible.
    """

    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.r2_bucket
        self._endpoint_url = settings.r2_endpoint_url
        self._access_key_id = settings.r2_access_key_id.get_secret_value()
        self._secret_access_key = settings.r2_secret_access_key.get_secret_value()
        self._public_base_url = settings.r2_public_base_url
        self._session = aioboto3.Session()
        # Force SigV4 — required by R2's S3-compatible surface.
        self._botocore_config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        )

    @property
    def bucket(self) -> str:
        return self._bucket

    def _client_ctx(self) -> object:
        return self._session.client(
            "s3",
            endpoint_url=self._endpoint_url,
            region_name=_R2_REGION,
            aws_access_key_id=self._access_key_id,
            aws_secret_access_key=self._secret_access_key,
            config=self._botocore_config,
        )

    async def upload_bytes(self, key: str, data: bytes, content_type: str) -> None:
        """Upload ``data`` to ``key`` in the configured bucket."""
        try:
            async with self._client_ctx() as client:  # type: ignore[attr-defined]
                await client.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=data,
                    ContentType=content_type,
                )
        except Exception as exc:
            raise IngestionError(
                f"R2 upload failed for key {key!r} ({len(data)} bytes)"
            ) from exc

    async def generate_presigned_url(self, key: str, expires_in: int = 3600) -> str:
        """Return a time-limited GET URL for ``key``."""
        try:
            async with self._client_ctx() as client:  # type: ignore[attr-defined]
                url: str = await client.generate_presigned_url(
                    ClientMethod="get_object",
                    Params={"Bucket": self._bucket, "Key": key},
                    ExpiresIn=expires_in,
                )
                return url
        except Exception as exc:
            raise IngestionError(
                f"R2 presigned URL generation failed for key {key!r}"
            ) from exc

    def public_url(self, key: str) -> str:
        """Build a stable public URL for ``key`` if a public base URL is configured.

        Synchronous URL builder — safe to call from any context. If
        ``Settings.r2_public_base_url`` is unset, raises ``IngestionError``;
        callers without a public base URL must ``await generate_presigned_url``
        instead.
        """
        if not self._public_base_url:
            raise IngestionError(
                "R2_PUBLIC_BASE_URL is not configured; "
                "use `await generate_presigned_url(key)` for a signed URL instead."
            )
        base = self._public_base_url.rstrip("/")
        return f"{base}/{key.lstrip('/')}"
