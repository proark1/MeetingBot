"""Integrations API — manage Slack and Notion integrations per account."""

import json
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_account_id, SUPERADMIN_ACCOUNT_ID
from app.models.account import Integration

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/integrations", tags=["Integrations"])

_ALLOWED_TYPES = {"slack", "notion"}


# ── Schemas ───────────────────────────────────────────────────────────────────

class IntegrationCreate(BaseModel):
    type: str = Field(description="Integration type: `slack` or `notion`.")
    name: str = Field(default="", max_length=100, description="Human-readable label.")
    config: dict = Field(
        description=(
            "Integration config dict.  "
            "**Slack:** `{webhook_url: str}`.  "
            "**Notion:** `{api_token: str, database_id: str}`."
        )
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "type": "slack",
                    "name": "Team channel",
                    "config": {"webhook_url": "https://hooks.slack.com/services/..."},
                },
                {
                    "type": "notion",
                    "name": "Meeting notes",
                    "config": {"api_token": "secret_...", "database_id": "abc123"},
                },
            ]
        }
    }


class IntegrationResponse(BaseModel):
    id: str
    type: str
    name: str
    is_active: bool
    config_preview: dict = Field(description="Config with secrets redacted.")
    created_at: str


def _redact_config(integration_type: str, config: dict) -> dict:
    """Return config with secrets replaced by '***...' for API responses."""
    redacted = dict(config)
    if integration_type == "slack":
        if "webhook_url" in redacted:
            url = redacted["webhook_url"]
            redacted["webhook_url"] = url[:35] + "..." if len(url) > 35 else url
    elif integration_type == "notion":
        if "api_token" in redacted:
            redacted["api_token"] = "secret_***"
    return redacted


def _to_response(integration: Integration) -> IntegrationResponse:
    from app.services.secrets_at_rest import decrypt_json
    config = decrypt_json(integration.config)
    return IntegrationResponse(
        id=integration.id,
        type=integration.type,
        name=integration.name,
        is_active=integration.is_active,
        config_preview=_redact_config(integration.type, config),
        created_at=integration.created_at.isoformat(),
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
async def list_integrations(
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
):
    """List all integrations for the current account.

    Returns a paginated envelope with `results`, `total`, `limit`, `offset`, and `has_more`.
    """
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Use per-user authentication")

    from sqlalchemy import func
    total_result = await db.execute(
        select(func.count(Integration.id))
        .where(Integration.account_id == account_id)
    )
    total = total_result.scalar() or 0

    result = await db.execute(
        select(Integration)
        .where(Integration.account_id == account_id)
        .order_by(Integration.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    items = [_to_response(i) for i in result.scalars().all()]
    return {
        "results": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    }


def _validate_integration_config(type_: str, config: dict) -> None:
    """Validate config requirements for a given integration type."""
    if type_ == "slack" and not config.get("webhook_url"):
        raise HTTPException(status_code=422, detail="Slack integration requires config.webhook_url")
    if type_ == "notion":
        if not config.get("api_token"):
            raise HTTPException(status_code=422, detail="Notion integration requires config.api_token")
        if not config.get("database_id"):
            raise HTTPException(status_code=422, detail="Notion integration requires config.database_id")


@router.post("", response_model=IntegrationResponse, status_code=201)
async def create_integration(
    payload: IntegrationCreate,
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
):
    """Create a new integration for the current account."""
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Use per-user authentication")

    if payload.type not in _ALLOWED_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported integration type '{payload.type}'. Allowed: {sorted(_ALLOWED_TYPES)}",
        )

    _validate_integration_config(payload.type, payload.config)

    from app.services.secrets_at_rest import encrypt_json
    integration = Integration(
        id=str(uuid.uuid4()),
        account_id=account_id,
        type=payload.type,
        name=payload.name or payload.type.title(),
        config=encrypt_json(payload.config),
        is_active=True,
    )
    db.add(integration)
    await db.commit()
    await db.refresh(integration)

    logger.info("Account %s created %s integration %s", account_id, payload.type, integration.id)
    return _to_response(integration)


@router.patch("/{integration_id}", response_model=IntegrationResponse)
async def update_integration(
    integration_id: str,
    payload: IntegrationCreate,
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
):
    """Update an existing integration's config."""
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Use per-user authentication")

    result = await db.execute(
        select(Integration).where(
            Integration.id == integration_id,
            Integration.account_id == account_id,
        )
    )
    integration = result.scalar_one_or_none()
    if not integration:
        raise HTTPException(status_code=404, detail="Integration not found")

    if payload.type not in _ALLOWED_TYPES:
        raise HTTPException(status_code=422, detail=f"Unsupported integration type '{payload.type}'")

    _validate_integration_config(payload.type, payload.config)

    integration.type = payload.type
    from app.services.secrets_at_rest import encrypt_json
    integration.name = payload.name or integration.name
    integration.config = encrypt_json(payload.config)
    await db.commit()
    await db.refresh(integration)
    return _to_response(integration)


@router.delete("/{integration_id}", status_code=204)
async def delete_integration(
    integration_id: str,
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
):
    """Delete an integration."""
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Use per-user authentication")

    result = await db.execute(
        select(Integration).where(
            Integration.id == integration_id,
            Integration.account_id == account_id,
        )
    )
    integration = result.scalar_one_or_none()
    if not integration:
        raise HTTPException(status_code=404, detail="Integration not found")

    await db.delete(integration)
    await db.commit()
