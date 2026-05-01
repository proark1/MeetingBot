"""Cloud storage service for meeting recordings.

Supports S3-compatible storage (AWS S3, Cloudflare R2, MinIO, etc.).
Falls back to local filesystem when no cloud storage is configured.

Configure via environment variables:
  STORAGE_BACKEND   = "s3" | "local" (default: "local")
  S3_BUCKET         = bucket name
  S3_ENDPOINT_URL   = optional custom endpoint (Cloudflare R2, MinIO, etc.)
  S3_ACCESS_KEY_ID  = AWS access key / R2 account ID token
  S3_SECRET_ACCESS_KEY = AWS secret key / R2 secret token
  S3_REGION         = AWS region (default: us-east-1)
  S3_PUBLIC_URL     = optional CDN/public base URL for recordings
"""

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _get_settings():
    from app.config import settings
    return settings


def is_cloud_storage_enabled() -> bool:
    s = _get_settings()
    return getattr(s, "STORAGE_BACKEND", "local") == "s3" and bool(getattr(s, "S3_BUCKET", ""))


def _get_s3_client():
    """Return a boto3 S3 client configured from settings."""
    try:
        import boto3
    except ImportError:
        raise RuntimeError(
            "boto3 is not installed — run: pip install boto3"
        )
    s = _get_settings()
    kwargs: dict = {
        "aws_access_key_id":     getattr(s, "S3_ACCESS_KEY_ID", "") or None,
        "aws_secret_access_key": getattr(s, "S3_SECRET_ACCESS_KEY", "") or None,
        "region_name":           getattr(s, "S3_REGION", "us-east-1"),
    }
    endpoint = getattr(s, "S3_ENDPOINT_URL", "") or None
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("s3", **kwargs)


async def upload_recording(local_path: str, bot_id: str, account_id: Optional[str] = None) -> Optional[str]:
    """Upload a recording to cloud storage.

    Returns the storage key/URL if successful, None if cloud storage is
    disabled or if upload fails (local file is kept in both cases).

    The key layout is ``recordings/{account_id}/{bot_id}.wav`` so GDPR
    erasure (``delete_all_recordings_for_account``) can list by prefix.
    Bots without an owning account fall back to ``recordings/_legacy/...``.
    """
    if not is_cloud_storage_enabled():
        return None

    if not os.path.exists(local_path):
        logger.warning("upload_recording: file not found: %s", local_path)
        return None

    import asyncio
    s = _get_settings()
    bucket = getattr(s, "S3_BUCKET", "")
    # account_id is required for GDPR erasure to work; bots without an owner
    # are filed under a sentinel prefix.
    _acct_segment = account_id if account_id else "_legacy"
    key = f"recordings/{_acct_segment}/{bot_id}.wav"

    def _upload():
        client = _get_s3_client()
        client.upload_file(
            local_path,
            bucket,
            key,
            ExtraArgs={"ContentType": "audio/wav"},
        )
        return key

    try:
        storage_key = await asyncio.to_thread(_upload)
        logger.info("Uploaded recording for bot %s → s3://%s/%s", bot_id, bucket, storage_key)

        # Delete the local file after successful upload to save disk space
        try:
            os.remove(local_path)
            logger.info("Deleted local recording after cloud upload: %s", local_path)
        except OSError as e:
            logger.warning("Could not delete local recording after upload: %s", e)

        return storage_key
    except Exception as exc:
        logger.error("Cloud upload failed for bot %s: %s (keeping local file)", bot_id, exc)
        return None


async def get_recording_url(storage_key: str, expires_in: int = 3600) -> Optional[str]:
    """Return a pre-signed URL for a recording stored in S3.

    Falls back to the S3_PUBLIC_URL base if configured (CDN/public bucket).
    Returns None if cloud storage is not configured.
    """
    if not is_cloud_storage_enabled():
        return None

    import asyncio
    s = _get_settings()
    public_base = getattr(s, "S3_PUBLIC_URL", "") or ""

    if public_base:
        return f"{public_base.rstrip('/')}/{storage_key}"

    bucket = getattr(s, "S3_BUCKET", "")

    def _presign():
        client = _get_s3_client()
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": storage_key},
            ExpiresIn=expires_in,
        )

    try:
        return await asyncio.to_thread(_presign)
    except Exception as exc:
        logger.error("Failed to generate presigned URL for %s: %s", storage_key, exc)
        return None


async def delete_recording(storage_key: str) -> bool:
    """Delete a recording from cloud storage. Returns True on success."""
    if not is_cloud_storage_enabled():
        return False

    import asyncio
    s = _get_settings()
    bucket = getattr(s, "S3_BUCKET", "")

    def _delete():
        client = _get_s3_client()
        client.delete_object(Bucket=bucket, Key=storage_key)

    try:
        await asyncio.to_thread(_delete)
        logger.info("Deleted cloud recording: %s", storage_key)
        return True
    except Exception as exc:
        logger.error("Failed to delete cloud recording %s: %s", storage_key, exc)
        return False


async def delete_all_recordings_for_account(account_id: str) -> int:
    """Delete all recordings belonging to an account (GDPR erasure).

    Returns the number of objects deleted. Sweeps both the canonical
    ``recordings/{account_id}/`` prefix and any legacy
    ``recordings/{bot_id}.wav`` keys whose ``BotSnapshot.account_id``
    matches — older recordings (from before round-2 fix #1) were filed
    flat without the account segment.
    """
    if not is_cloud_storage_enabled():
        return 0

    import asyncio
    s = _get_settings()
    bucket = getattr(s, "S3_BUCKET", "")
    prefix = f"recordings/{account_id}/"

    # Collect legacy keys (flat layout) by joining the bot_snapshots table.
    legacy_keys: list[str] = []
    try:
        from app.db import AsyncSessionLocal
        from app.models.account import BotSnapshot
        from sqlalchemy import select
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(BotSnapshot.id).where(BotSnapshot.account_id == account_id)
            )
            for (bot_id,) in result.all():
                legacy_keys.append(f"recordings/{bot_id}.wav")
    except Exception as exc:
        logger.warning("GDPR: legacy-key enumeration failed for %s: %s", account_id, exc)

    def _list_and_delete():
        client = _get_s3_client()
        paginator = client.get_paginator("list_objects_v2")
        keys: list[dict] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append({"Key": obj["Key"]})
        # Add legacy flat-layout keys directly to the batch — S3's
        # DeleteObjects silently ignores keys that don't exist, so a HEAD
        # probe per key would only add round-trips with no benefit.
        for legacy_key in legacy_keys:
            keys.append({"Key": legacy_key})
        if not keys:
            return 0
        # S3 batch delete (max 1000 per call)
        deleted = 0
        for i in range(0, len(keys), 1000):
            batch = keys[i:i + 1000]
            resp = client.delete_objects(Bucket=bucket, Delete={"Objects": batch})
            deleted += len(resp.get("Deleted", []))
        return deleted

    try:
        count = await asyncio.to_thread(_list_and_delete)
        logger.info("GDPR: deleted %d cloud recordings for account %s", count, account_id)
        return count
    except Exception as exc:
        logger.error("GDPR cloud cleanup failed for account %s: %s", account_id, exc)
        return 0
