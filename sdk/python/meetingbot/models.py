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


class SubscribeResponse(BaseModel):
    """Stripe subscription checkout response."""

    session_id: str
    checkout_url: str
    plan: str

    model_config = {"extra": "allow"}


class UsageResponse(BaseModel):
    """Monthly usage breakdown."""

    bots_used: int = 0
    bots_limit: int = 0
    plan: str = "free"
    credits_balance: float = 0.0
    credits_spent_this_month: float = 0.0
    avg_cost_per_bot: float = 0.0
    billing_cycle_reset: Optional[str] = None
    daily_usage: List[Dict[str, Any]] = []

    model_config = {"extra": "allow"}


class TrendsResponse(BaseModel):
    """Longitudinal analytics trends."""

    range_days: int = 30
    total_meetings: int = 0
    total_hours: float = 0.0
    meetings_per_day: List[Dict[str, Any]] = []
    sentiment_trend: List[Dict[str, Any]] = []
    health_trend: List[Dict[str, Any]] = []
    top_topics: List[Dict[str, Any]] = []
    cost_trend: List[Dict[str, Any]] = []

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


# ---------------------------------------------------------------------------
# Transcript / Analysis models
# ---------------------------------------------------------------------------


class TranscriptEntry(BaseModel):
    """A single transcript segment."""

    speaker: Optional[str] = None
    text: Optional[str] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None

    model_config = {"extra": "allow"}


class TranscriptResponse(BaseModel):
    """Raw transcript returned by get_transcript."""

    transcript: Optional[List[TranscriptEntry]] = None

    model_config = {"extra": "allow"}


class AnalysisResponse(BaseModel):
    """AI analysis result."""

    summary: Optional[str] = None
    key_points: Optional[List[str]] = None
    action_items: Optional[List[Dict[str, Any]]] = None
    decisions: Optional[List[str]] = None
    next_steps: Optional[List[str]] = None
    sentiment: Optional[str] = None
    topics: Optional[List[Dict[str, Any]]] = None

    model_config = {"extra": "allow"}


class HighlightsResponse(BaseModel):
    """Curated highlights from a meeting."""

    key_points: Optional[List[str]] = None
    action_items: Optional[List[Dict[str, Any]]] = None
    decisions: Optional[List[str]] = None

    model_config = {"extra": "allow"}


class AskResponse(BaseModel):
    """Response from asking a question about a meeting."""

    answer: Optional[str] = None

    model_config = {"extra": "allow"}


class FollowupEmailResponse(BaseModel):
    """Generated follow-up email."""

    subject: Optional[str] = None
    body: Optional[str] = None

    model_config = {"extra": "allow"}


class ShareResponse(BaseModel):
    """Shareable link for a meeting."""

    share_url: Optional[str] = None
    token: Optional[str] = None

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Template models
# ---------------------------------------------------------------------------


class TemplateInfo(BaseModel):
    """An analysis template."""

    name: Optional[str] = None
    description: Optional[str] = None

    model_config = {"extra": "allow"}


class TemplateListResponse(BaseModel):
    """List of available analysis templates."""

    templates: Optional[List[TemplateInfo]] = None

    model_config = {"extra": "allow"}


class DefaultPromptResponse(BaseModel):
    """Default analysis prompt."""

    prompt: Optional[str] = None

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Analytics models
# ---------------------------------------------------------------------------


class AnalyticsResponse(BaseModel):
    """Account analytics."""

    model_config = {"extra": "allow"}


class RecurringAnalyticsResponse(BaseModel):
    """Recurring meeting insights."""

    model_config = {"extra": "allow"}


class ApiUsageResponse(BaseModel):
    """API usage statistics."""

    model_config = {"extra": "allow"}


class MyAnalyticsResponse(BaseModel):
    """Personal analytics."""

    model_config = {"extra": "allow"}


class SearchResult(BaseModel):
    """A single search result."""

    bot_id: Optional[str] = None
    meeting_url: Optional[str] = None
    bot_name: Optional[str] = None
    snippet: Optional[str] = None
    score: Optional[float] = None

    model_config = {"extra": "allow"}


class SearchResponse(BaseModel):
    """Search results."""

    results: Optional[List[SearchResult]] = None
    total: Optional[int] = None

    model_config = {"extra": "allow"}


class AuditLogEntry(BaseModel):
    """A single audit log entry."""

    id: Optional[str] = None
    action: Optional[str] = None
    account_id: Optional[str] = None
    detail: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = {"extra": "allow"}


class AuditLogResponse(BaseModel):
    """Audit log entries."""

    results: Optional[List[AuditLogEntry]] = None
    total: Optional[int] = None

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Action Item models
# ---------------------------------------------------------------------------


class ActionItemResponse(BaseModel):
    """An action item from a meeting."""

    id: Optional[str] = None
    bot_id: Optional[str] = None
    text: Optional[str] = None
    assignee: Optional[str] = None
    status: Optional[str] = None
    due_date: Optional[str] = None
    confidence: Optional[float] = None
    created_at: Optional[datetime] = None

    model_config = {"extra": "allow"}


class ActionItemListResponse(BaseModel):
    """List of action items."""

    results: Optional[List[ActionItemResponse]] = None
    total: Optional[int] = None

    model_config = {"extra": "allow"}


class ActionItemStatsResponse(BaseModel):
    """Action item statistics."""

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Keyword Alert models
# ---------------------------------------------------------------------------


class KeywordAlertResponse(BaseModel):
    """A keyword alert configuration."""

    id: Optional[str] = None
    keywords: Optional[List[str]] = None
    webhook_url: Optional[str] = None
    events: Optional[List[str]] = None
    is_active: Optional[bool] = None
    created_at: Optional[datetime] = None

    model_config = {"extra": "allow"}


class KeywordAlertListResponse(BaseModel):
    """List of keyword alerts."""

    results: Optional[List[KeywordAlertResponse]] = None
    total: Optional[int] = None

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Calendar Feed models
# ---------------------------------------------------------------------------


class CalendarFeedResponse(BaseModel):
    """A calendar feed configuration."""

    id: Optional[str] = None
    name: Optional[str] = None
    url: Optional[str] = None
    is_active: Optional[bool] = None
    auto_record: Optional[bool] = None
    bot_name: Optional[str] = None
    last_synced_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    model_config = {"extra": "allow"}


class CalendarFeedListResponse(BaseModel):
    """List of calendar feeds."""

    results: Optional[List[CalendarFeedResponse]] = None
    total: Optional[int] = None

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Integration models
# ---------------------------------------------------------------------------


class IntegrationResponse(BaseModel):
    """An integration configuration."""

    id: Optional[str] = None
    type: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None
    created_at: Optional[datetime] = None

    model_config = {"extra": "allow"}


class IntegrationListResponse(BaseModel):
    """List of integrations."""

    results: Optional[List[IntegrationResponse]] = None
    total: Optional[int] = None

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Workspace models
# ---------------------------------------------------------------------------


class WorkspaceMemberResponse(BaseModel):
    """A workspace member."""

    id: Optional[str] = None
    workspace_id: Optional[str] = None
    account_id: Optional[str] = None
    role: Optional[str] = None
    invited_by: Optional[str] = None
    joined_at: Optional[datetime] = None

    model_config = {"extra": "allow"}


class WorkspaceResponse(BaseModel):
    """A workspace."""

    id: Optional[str] = None
    name: Optional[str] = None
    slug: Optional[str] = None
    owner_account_id: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    member_role: Optional[str] = None

    model_config = {"extra": "allow"}


class WorkspaceListResponse(BaseModel):
    """List of workspaces."""

    results: Optional[List[WorkspaceResponse]] = None
    total: Optional[int] = None

    model_config = {"extra": "allow"}


class WorkspaceMemberListResponse(BaseModel):
    """List of workspace members."""

    results: Optional[List[WorkspaceMemberResponse]] = None
    total: Optional[int] = None

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Retention models
# ---------------------------------------------------------------------------


class RetentionPolicyResponse(BaseModel):
    """Retention policy configuration."""

    retention_days: Optional[int] = None
    anonymize_speakers: Optional[bool] = None

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# MCP models
# ---------------------------------------------------------------------------


class McpSchemaResponse(BaseModel):
    """MCP server manifest."""

    model_config = {"extra": "allow"}


class McpCallResponse(BaseModel):
    """MCP tool call result."""

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Account models
# ---------------------------------------------------------------------------


class AccountInfo(BaseModel):
    """Current account information."""

    id: Optional[str] = None
    email: Optional[str] = None
    account_type: Optional[str] = None
    is_admin: Optional[bool] = None
    created_at: Optional[datetime] = None

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Webhook event list
# ---------------------------------------------------------------------------


class WebhookEventsResponse(BaseModel):
    """Supported webhook event types."""

    events: Optional[List[str]] = None

    model_config = {"extra": "allow"}
