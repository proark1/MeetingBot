"""Integrations API — manage third-party integrations per account."""

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

_ALLOWED_TYPES = {"slack", "notion", "linear", "jira", "google_drive", "hubspot", "salesforce"}


# ── Schemas ───────────────────────────────────────────────────────────────────

class IntegrationCreate(BaseModel):
    type: str = Field(description="Integration type: `slack`, `notion`, `linear`, `jira`, `google_drive`, `hubspot`, or `salesforce`.")
    name: str = Field(default="", max_length=100, description="Human-readable label.")
    config: dict = Field(
        description=(
            "Integration config dict.  "
            "**Slack:** `{webhook_url: str}`.  "
            "**Notion:** `{api_token: str, database_id: str}`.  "
            "**Linear:** `{api_key: str, team_id: str, approval_required?: bool}`.  "
            "**Jira:** `{base_url: str, email: str, token: str, project_key: str, approval_required?: bool}`.  "
            "**HubSpot:** `{access_token: str, approval_required?: bool}`.  "
            "**Salesforce:** `{instance_url: str, access_token: str, approval_required?: bool}`."
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
                {
                    "type": "linear",
                    "name": "Product tasks",
                    "config": {"api_key": "lin_api_...", "team_id": "team_123", "approval_required": True},
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

    model_config = {"json_schema_extra": {"example": {
        "id": "int_4cb812aa",
        "type": "slack",
        "name": "Team channel",
        "is_active": True,
        "config_preview": {"webhook_url": "https://hooks.slack.com/services/T0..."},
        "created_at": "2026-04-22T11:00:00Z",
    }}}


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
    elif integration_type in {"linear", "google_drive", "hubspot"}:
        for key in ("api_key", "access_token"):
            if key in redacted:
                redacted[key] = "***"
    elif integration_type == "jira":
        if "token" in redacted:
            redacted["token"] = "***"
    elif integration_type == "salesforce":
        for key in ("access_token", "client_secret", "password", "security_token"):
            if key in redacted:
                redacted[key] = "***"
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

@router.get(
    "",
    responses={200: {"content": {"application/json": {"example": {
        "results": [{
            "id": "int_4cb812aa",
            "type": "slack",
            "name": "Team channel",
            "is_active": True,
            "config_preview": {"webhook_url": "https://hooks.slack.com/services/T0..."},
            "created_at": "2026-04-22T11:00:00Z",
        }],
        "total": 1,
        "limit": 50,
        "offset": 0,
        "has_more": False,
    }}}}},
)
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
    if type_ == "linear":
        if not config.get("api_key"):
            raise HTTPException(status_code=422, detail="Linear integration requires config.api_key")
        if not config.get("team_id"):
            raise HTTPException(status_code=422, detail="Linear integration requires config.team_id")
    if type_ == "jira":
        for key in ("base_url", "email", "token", "project_key"):
            if not config.get(key):
                raise HTTPException(status_code=422, detail=f"Jira integration requires config.{key}")
    if type_ == "google_drive" and not config.get("access_token"):
        raise HTTPException(status_code=422, detail="Google Drive integration requires config.access_token")
    if type_ == "hubspot" and not config.get("access_token"):
        raise HTTPException(status_code=422, detail="HubSpot integration requires config.access_token")
    if type_ == "salesforce":
        has_token = config.get("instance_url") and config.get("access_token")
        has_password_flow = all(config.get(k) for k in ("client_id", "client_secret", "username", "password", "security_token"))
        if not (has_token or has_password_flow):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Salesforce integration requires either config.instance_url + config.access_token "
                    "or username-password OAuth fields"
                ),
            )


async def _block_integration_ssrf(type_: str, config: dict) -> None:
    """Reject internal/private targets for URL-bearing integrations at
    registration time — defense-in-depth matching the webhook/calendar guards
    (delivery-time still re-validates). Currently the Slack webhook URL."""
    if type_ == "slack":
        url = config.get("webhook_url")
        if url:
            from app.api.webhooks import _block_ssrf
            await _block_ssrf(url)
    if type_ == "jira":
        url = config.get("base_url")
        if url:
            from app.api.webhooks import _block_ssrf
            await _block_ssrf(f"{str(url).rstrip('/')}/rest/api/3/issue")
    if type_ == "salesforce":
        from app.api.webhooks import _block_ssrf
        if config.get("instance_url"):
            await _block_ssrf(f"{str(config['instance_url']).rstrip('/')}/services/data/v59.0/sobjects/Task/")
        if config.get("login_url"):
            await _block_ssrf(f"{str(config['login_url']).rstrip('/')}/services/oauth2/token")


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
    await _block_integration_ssrf(payload.type, payload.config)

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
    await _block_integration_ssrf(payload.type, payload.config)

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
