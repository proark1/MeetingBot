"""Privacy controls: consent policy and participant deletion requests."""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import SUPERADMIN_ACCOUNT_ID, get_current_account_id
from app.models.account import BotSnapshot, ConsentPolicy, DataDeletionRequest
from app.store import store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/privacy", tags=["Privacy"])


class ConsentPolicyResponse(BaseModel):
    account_id: str
    require_consent: bool
    consent_message: Optional[str] = None
    opt_out_phrase: Optional[str] = None
    auto_redact_opt_outs: bool
    updated_at: Optional[datetime] = None


class ConsentPolicyUpdate(BaseModel):
    require_consent: Optional[bool] = None
    consent_message: Optional[str] = Field(default=None, max_length=500)
    opt_out_phrase: Optional[str] = Field(default=None, max_length=100)
    auto_redact_opt_outs: Optional[bool] = None

    model_config = {"json_schema_extra": {"example": {
        "require_consent": True,
        "consent_message": "This call is being recorded and transcribed by Acme Notes. Say 'opt out' to be redacted.",
        "opt_out_phrase": "opt out",
        "auto_redact_opt_outs": True,
    }}}


class DeletionRequestCreate(BaseModel):
    bot_id: Optional[str] = Field(default=None, max_length=36)
    requester_email: EmailStr
    participant_name: Optional[str] = Field(default=None, max_length=255)
    reason: Optional[str] = Field(default=None, max_length=2000)

    model_config = {"json_schema_extra": {"example": {
        "bot_id": "bot_8a72c5e1",
        "requester_email": "participant@example.com",
        "participant_name": "Alex",
        "reason": "Please remove my contribution from the transcript.",
    }}}


class DeletionRequestResponse(BaseModel):
    id: str
    account_id: Optional[str] = None
    bot_id: Optional[str] = None
    requester_email: str
    participant_name: Optional[str] = None
    reason: Optional[str] = None
    status: str
    resolution_note: Optional[str] = None
    created_at: datetime
    resolved_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class PublicDeletionRequestResponse(BaseModel):
    id: str
    status: str
    created_at: datetime


class DeletionRequestUpdate(BaseModel):
    status: str = Field(description="pending | approved | rejected | completed")
    resolution_note: Optional[str] = Field(default=None, max_length=2000)
    erase_meeting_data: bool = Field(
        default=False,
        description="When true, wipe transcript, analysis, chapters, speaker stats, and recording links for the bot.",
    )


def _auth_account_id(account_id: Optional[str]) -> str:
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Use per-user authentication")
    return account_id


def _policy_response(account_id: str, policy: ConsentPolicy) -> ConsentPolicyResponse:
    return ConsentPolicyResponse(
        account_id=account_id,
        require_consent=policy.require_consent,
        consent_message=policy.consent_message,
        opt_out_phrase=policy.opt_out_phrase,
        auto_redact_opt_outs=policy.auto_redact_opt_outs,
        updated_at=policy.updated_at,
    )


async def _get_or_create_policy(account_id: str, db: AsyncSession) -> ConsentPolicy:
    result = await db.execute(select(ConsentPolicy).where(ConsentPolicy.account_id == account_id))
    policy = result.scalar_one_or_none()
    if policy is not None:
        return policy
    from app.config import settings
    policy = ConsentPolicy(
        account_id=account_id,
        require_consent=bool(settings.CONSENT_ANNOUNCEMENT_ENABLED),
        consent_message=settings.CONSENT_MESSAGE or None,
        opt_out_phrase=settings.CONSENT_OPT_OUT_PHRASE or None,
        auto_redact_opt_outs=True,
    )
    db.add(policy)
    await db.commit()
    await db.refresh(policy)
    return policy


async def _resolve_bot_account(bot_id: Optional[str], db: AsyncSession) -> Optional[str]:
    if not bot_id:
        return None
    try:
        bot = await store.get_bot(bot_id)
        if bot is not None:
            return bot.account_id
    except Exception:
        logger.debug("Live bot lookup failed while resolving deletion request", exc_info=True)

    result = await db.execute(select(BotSnapshot.account_id).where(BotSnapshot.id == bot_id))
    return result.scalar_one_or_none()


async def _delete_artifact(path_or_key: Optional[str]) -> None:
    if not path_or_key:
        return
    if os.path.exists(path_or_key):
        try:
            os.remove(path_or_key)
            return
        except OSError as exc:
            logger.warning("Could not delete local recording artifact %s: %s", path_or_key, exc)
    try:
        from app.services.storage_service import delete_recording
        await delete_recording(path_or_key)
    except Exception as exc:
        logger.debug("Cloud recording delete skipped for %s: %s", path_or_key, exc)


async def _erase_bot_data(bot_id: str, account_id: str, db: AsyncSession) -> bool:
    """Wipe sensitive data for a bot owned by account_id. Returns True if found."""
    erased = False

    bot = await store.get_bot(bot_id)
    if bot is not None and bot.account_id == account_id:
        await _delete_artifact(bot.recording_path)
        await _delete_artifact(bot.video_path)
        await store.update_bot(
            bot_id,
            transcript=[],
            analysis=None,
            chapters=[],
            speaker_stats=[],
            recording_path=None,
            video_path=None,
            opted_out_participants=[],
            metadata={**(bot.metadata or {}), "erased_at": datetime.now(timezone.utc).isoformat()},
        )
        erased = True

    result = await db.execute(
        select(BotSnapshot).where(BotSnapshot.id == bot_id, BotSnapshot.account_id == account_id)
    )
    snap = result.scalar_one_or_none()
    if snap is not None:
        try:
            from app.services.secrets_at_rest import decrypt_text, encrypt_text
            data = json.loads(decrypt_text(snap.data) or "{}")
        except Exception:
            data = {}
        await _delete_artifact(data.get("recording_path"))
        await _delete_artifact(data.get("video_path"))
        data.update({
            "transcript": [],
            "analysis": None,
            "chapters": [],
            "speaker_stats": [],
            "recording_path": None,
            "video_path": None,
            "opted_out_participants": [],
            "erased_at": datetime.now(timezone.utc).isoformat(),
        })
        from app.services.secrets_at_rest import encrypt_text
        snap.data = encrypt_text(json.dumps(data))
        erased = True

    return erased


def _to_deletion_response(row: DataDeletionRequest) -> DeletionRequestResponse:
    return DeletionRequestResponse(
        id=row.id,
        account_id=row.account_id,
        bot_id=row.bot_id,
        requester_email=row.requester_email,
        participant_name=row.participant_name,
        reason=row.reason,
        status=row.status,
        resolution_note=row.resolution_note,
        created_at=row.created_at,
        resolved_at=row.resolved_at,
    )


@router.get("/consent-policy", response_model=ConsentPolicyResponse)
async def get_consent_policy(
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
):
    """Get account-level recording consent defaults."""
    account_id = _auth_account_id(account_id)
    policy = await _get_or_create_policy(account_id, db)
    return _policy_response(account_id, policy)


@router.put("/consent-policy", response_model=ConsentPolicyResponse)
async def update_consent_policy(
    payload: ConsentPolicyUpdate,
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
):
    """Update account-level recording consent defaults."""
    account_id = _auth_account_id(account_id)
    policy = await _get_or_create_policy(account_id, db)
    if payload.require_consent is not None:
        policy.require_consent = payload.require_consent
    if payload.consent_message is not None:
        policy.consent_message = payload.consent_message.strip() or None
    if payload.opt_out_phrase is not None:
        policy.opt_out_phrase = payload.opt_out_phrase.strip() or None
    if payload.auto_redact_opt_outs is not None:
        policy.auto_redact_opt_outs = payload.auto_redact_opt_outs
    await db.commit()
    await db.refresh(policy)
    return _policy_response(account_id, policy)


@router.post("/deletion-requests", response_model=PublicDeletionRequestResponse, status_code=202)
async def create_deletion_request(payload: DeletionRequestCreate, db: AsyncSession = Depends(get_db)):
    """Submit a participant data deletion request.

    This endpoint intentionally does not reveal whether the supplied bot_id
    exists or belongs to a customer account.
    """
    account_id = await _resolve_bot_account(payload.bot_id, db)
    row = DataDeletionRequest(
        id=str(uuid.uuid4()),
        account_id=account_id,
        bot_id=payload.bot_id,
        requester_email=str(payload.requester_email),
        participant_name=(payload.participant_name or "").strip() or None,
        reason=(payload.reason or "").strip() or None,
        status="pending",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return PublicDeletionRequestResponse(id=row.id, status=row.status, created_at=row.created_at)


@router.get("/deletion-requests", response_model=list[DeletionRequestResponse])
async def list_deletion_requests(
    status: Optional[str] = None,
    limit: int = 100,
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
):
    """List deletion requests for the authenticated account."""
    account_id = _auth_account_id(account_id)
    limit = max(1, min(limit, 200))
    q = select(DataDeletionRequest).where(DataDeletionRequest.account_id == account_id)
    if status:
        q = q.where(DataDeletionRequest.status == status)
    q = q.order_by(DataDeletionRequest.created_at.desc()).limit(limit)
    result = await db.execute(q)
    return [_to_deletion_response(row) for row in result.scalars().all()]


@router.get("/deletion-requests/{request_id}", response_model=PublicDeletionRequestResponse)
async def get_deletion_request_status(request_id: str, db: AsyncSession = Depends(get_db)):
    """Check public status for a deletion request without exposing meeting data."""
    result = await db.execute(select(DataDeletionRequest).where(DataDeletionRequest.id == request_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Deletion request not found")
    return PublicDeletionRequestResponse(id=row.id, status=row.status, created_at=row.created_at)


@router.patch("/deletion-requests/{request_id}", response_model=DeletionRequestResponse)
async def update_deletion_request(
    request_id: str,
    payload: DeletionRequestUpdate,
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
):
    """Resolve a deletion request and optionally erase the associated meeting data."""
    account_id = _auth_account_id(account_id)
    if payload.status not in {"pending", "approved", "rejected", "completed"}:
        raise HTTPException(status_code=422, detail="status must be pending, approved, rejected, or completed")

    result = await db.execute(
        select(DataDeletionRequest).where(
            DataDeletionRequest.id == request_id,
            DataDeletionRequest.account_id == account_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Deletion request not found")

    if payload.erase_meeting_data:
        if not row.bot_id:
            raise HTTPException(status_code=409, detail="Deletion request has no bot_id to erase")
        found = await _erase_bot_data(row.bot_id, account_id, db)
        if not found:
            raise HTTPException(status_code=404, detail="Associated meeting data not found")
        row.status = "completed"
    else:
        row.status = payload.status

    row.resolution_note = payload.resolution_note
    row.resolved_by = account_id
    if row.status in {"approved", "rejected", "completed"}:
        row.resolved_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(row)
    return _to_deletion_response(row)
