"""
Pydantic response models for the MeetingBot API.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Bot models
# ---------------------------------------------------------------------------


class BotResponse(BaseModel):
    """Full bot object returned by create/get bot endpoints."""

    id: str
    meeting_url: str
    bot_name: str = "JustHereToListen.io"
    bot_avatar_url: Optional[str] = None
    webhook_url: Optional[str] = None
    join_at: Optional[datetime] = None
    analysis_mode: Optional[str] = None
    template: Optional[str] = None
    prompt_override: Optional[str] = None
    vocabulary: Optional[List[str]] = None
    respond_on_mention: Optional[bool] = None
    mention_response_mode: Optional[str] = None
    tts_provider: Optional[str] = None
    start_muted: Optional[bool] = None
    live_transcription: Optional[bool] = None
    sub_user_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    record_video: bool = False
    status: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"extra": "allow"}


class BotSummary(BaseModel):
    """Abbreviated bot object returned in list responses."""

    id: str
    meeting_url: str
    bot_name: str = "JustHereToListen.io"
    status: Optional[str] = None
    created_at: Optional[datetime] = None
    sub_user_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    model_config = {"extra": "allow"}


class BotListResponse(BaseModel):
    """Paginated list of bots."""

    results: List[BotSummary]
    total: int
    limit: int
    offset: int


class BotStats(BaseModel):
    """Aggregate bot counts."""

    total: Optional[int] = None
    active: Optional[int] = None
    completed: Optional[int] = None
    failed: Optional[int] = None

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Webhook models
# ---------------------------------------------------------------------------


class WebhookResponse(BaseModel):
    """Webhook object."""

    id: str
    url: str
    events: List[str]
    secret: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"extra": "allow"}


class WebhookListResponse(BaseModel):
    """List of webhooks."""

    results: List[WebhookResponse]
    total: Optional[int] = None

    model_config = {"extra": "allow"}


class WebhookDelivery(BaseModel):
    """A single webhook delivery attempt."""

    id: str
    webhook_id: str
    event: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    response_status: Optional[int] = None
    response_body: Optional[str] = None
    delivered_at: Optional[datetime] = None
    success: Optional[bool] = None

    model_config = {"extra": "allow"}


class WebhookDeliveryListResponse(BaseModel):
    """Paginated list of webhook delivery logs."""

    results: List[WebhookDelivery]
    total: Optional[int] = None
    limit: Optional[int] = None
    offset: Optional[int] = None

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Auth models
# ---------------------------------------------------------------------------


class ApiKey(BaseModel):
    """An API key object."""

    id: str
    name: str
    key_prefix: Optional[str] = None
    created_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None

    model_config = {"extra": "allow"}


class ApiKeyListResponse(BaseModel):
    """List of API keys."""

    results: List[ApiKey]
    total: Optional[int] = None

    model_config = {"extra": "allow"}


class ApiKeyCreateResponse(BaseModel):
    """Response when creating a new API key (includes the full key once)."""

    id: str
    name: str
    key: str
    created_at: Optional[datetime] = None

    model_config = {"extra": "allow"}


class PlanInfo(BaseModel):
    """Account plan information."""

    plan: Optional[str] = None
    status: Optional[str] = None
    limits: Optional[Dict[str, Any]] = None
    usage: Optional[Dict[str, Any]] = None

    model_config = {"extra": "allow"}


class NotificationPrefs(BaseModel):
    """Notification preferences."""

    email_on_completion: Optional[bool] = None
    email_on_failure: Optional[bool] = None
    webhook_on_completion: Optional[bool] = None

    model_config = {"extra": "allow"}


class LoginResponse(BaseModel):
    """JWT login response."""

    access_token: str
    token_type: str = "bearer"

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Billing models
# ---------------------------------------------------------------------------


class Transaction(BaseModel):
    """A billing transaction."""

    id: str
    amount_usd: float
    description: Optional[str] = None
    created_at: Optional[datetime] = None
    type: Optional[str] = None

    model_config = {"extra": "allow"}


class BalanceResponse(BaseModel):
    """Account balance and transaction history."""

    balance_usd: float
    transactions: Optional[List[Transaction]] = None

    model_config = {"extra": "allow"}


class CheckoutResponse(BaseModel):
    """Stripe checkout session response."""

    checkout_url: str
    session_id: Optional[str] = None

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Export models
# ---------------------------------------------------------------------------


class ExportJsonResponse(BaseModel):
    """JSON export of a bot session."""

    id: Optional[str] = None
    transcript: Optional[List[Dict[str, Any]]] = None
    analysis: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None

    model_config = {"extra": "allow"}
