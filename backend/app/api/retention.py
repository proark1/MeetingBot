"""Data retention policy API.

Endpoints for managing per-account data retention settings.

Per-account policies override the global defaults set by the admin.
The background cleanup task honours these policies when deleting expired data.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from app.deps import SUPERADMIN_ACCOUNT_ID

router = APIRouter(prefix="/auth/retention", tags=["Retention"])


def _account_id(request: Request) -> str:
    """Extract required account_id from request state."""
    from fastapi import HTTPException
    account_id = getattr(request.state, "account_id", None)
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=401, detail="Authentication required")
    return account_id


class RetentionPolicyResponse(BaseModel):
    account_id: Optional[str] = None
    bot_retention_days: int
    recording_retention_days: int
    transcript_retention_days: int
    anonymize_speakers: bool
    is_global: bool = False


class RetentionPolicyUpdate(BaseModel):
    bot_retention_days: Optional[int] = None
    recording_retention_days: Optional[int] = None
    transcript_retention_days: Optional[int] = None
    anonymize_speakers: Optional[bool] = None


def _to_response(policy, account_id: Optional[str] = None, is_global: bool = False) -> dict:
    return {
        "account_id": account_id,
        "bot_retention_days": policy.bot_retention_days,
        "recording_retention_days": policy.recording_retention_days,
        "transcript_retention_days": policy.transcript_retention_days,
        "anonymize_speakers": policy.anonymize_speakers,
        "is_global": is_global,
    }


async def _get_or_create_policy(account_id: Optional[str], db):
    from app.models.account import RetentionPolicy
    from app.config import settings
    from sqlalchemy import select

    result = await db.execute(
        select(RetentionPolicy).where(RetentionPolicy.account_id == account_id)
    )
    policy = result.scalar_one_or_none()
    if policy is None:
        policy = RetentionPolicy(
            account_id=account_id,
            bot_retention_days=settings.DEFAULT_BOT_RETENTION_DAYS,
            recording_retention_days=settings.DEFAULT_RECORDING_RETENTION_DAYS,
            transcript_retention_days=settings.DEFAULT_BOT_RETENTION_DAYS,
        )
        db.add(policy)
        await db.commit()
        await db.refresh(policy)
    return policy


@router.get("", response_model=RetentionPolicyResponse)
async def get_retention_policy(request: Request):
    """Get the data retention policy for your account.

    Returns the per-account policy if one exists, otherwise the global default.
    """
    account_id = _account_id(request)

    from app.db import AsyncSessionLocal
    from app.models.account import RetentionPolicy
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        # Try per-account policy first
        result = await db.execute(
            select(RetentionPolicy).where(RetentionPolicy.account_id == account_id)
        )
        policy = result.scalar_one_or_none()

        if policy is None:
            # Fall back to global policy
            g_result = await db.execute(
                select(RetentionPolicy).where(RetentionPolicy.account_id.is_(None))
            )
            policy = g_result.scalar_one_or_none()

        if policy is None:
            from app.config import settings
            return RetentionPolicyResponse(
                account_id=account_id,
                bot_retention_days=settings.DEFAULT_BOT_RETENTION_DAYS,
                recording_retention_days=settings.DEFAULT_RECORDING_RETENTION_DAYS,
                transcript_retention_days=settings.DEFAULT_BOT_RETENTION_DAYS,
                anonymize_speakers=False,
                is_global=True,
            )

        return RetentionPolicyResponse(
            account_id=account_id,
            bot_retention_days=policy.bot_retention_days,
            recording_retention_days=policy.recording_retention_days,
            transcript_retention_days=policy.transcript_retention_days,
            anonymize_speakers=policy.anonymize_speakers,
            is_global=(policy.account_id is None),
        )


@router.put("", response_model=RetentionPolicyResponse)
async def update_retention_policy(payload: RetentionPolicyUpdate, request: Request):
    """Update your account's data retention policy.

    Only the fields you provide will be changed. All values are in days.
    Set -1 for 'keep forever'. Minimum is 1 day for most fields.
    """
    account_id = _account_id(request)

    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        policy = await _get_or_create_policy(account_id, db)

        if payload.bot_retention_days is not None:
            if payload.bot_retention_days != -1 and payload.bot_retention_days < 1:
                raise HTTPException(status_code=422, detail="bot_retention_days must be >= 1 or -1 (forever)")
            policy.bot_retention_days = payload.bot_retention_days

        if payload.recording_retention_days is not None:
            if payload.recording_retention_days != -1 and payload.recording_retention_days < 1:
                raise HTTPException(status_code=422, detail="recording_retention_days must be >= 1 or -1 (forever)")
            policy.recording_retention_days = payload.recording_retention_days

        if payload.transcript_retention_days is not None:
            if payload.transcript_retention_days != -1 and payload.transcript_retention_days < 1:
                raise HTTPException(status_code=422, detail="transcript_retention_days must be >= 1 or -1 (forever)")
            policy.transcript_retention_days = payload.transcript_retention_days

        if payload.anonymize_speakers is not None:
            policy.anonymize_speakers = payload.anonymize_speakers

        await db.commit()
        await db.refresh(policy)

    return RetentionPolicyResponse(
        account_id=account_id,
        bot_retention_days=policy.bot_retention_days,
        recording_retention_days=policy.recording_retention_days,
        transcript_retention_days=policy.transcript_retention_days,
        anonymize_speakers=policy.anonymize_speakers,
        is_global=False,
    )


@router.delete("", status_code=204)
async def delete_retention_policy(request: Request):
    """Delete your per-account retention policy, reverting to global defaults."""
    account_id = _account_id(request)

    from app.db import AsyncSessionLocal
    from app.models.account import RetentionPolicy
    from sqlalchemy import select, delete

    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(RetentionPolicy).where(RetentionPolicy.account_id == account_id)
        )
        await db.commit()
