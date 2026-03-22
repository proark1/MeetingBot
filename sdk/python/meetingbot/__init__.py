"""
MeetingBot Python SDK
=====================

Provides both synchronous and asynchronous clients for the MeetingBot API.

Quickstart (sync)::

    from meetingbot import MeetingBotClient

    client = MeetingBotClient(api_key="sk_live_...")
    bot = client.create_bot(
        meeting_url="https://zoom.us/j/123456789",
        bot_name="My Recorder",
        webhook_url="https://myapp.com/webhook",
    )
    print(f"Bot created: {bot.id}, status: {bot.status}")

Quickstart (async)::

    import asyncio
    from meetingbot import AsyncMeetingBotClient

    async def main():
        async with AsyncMeetingBotClient(api_key="sk_live_...") as client:
            bot = await client.create_bot(
                meeting_url="https://zoom.us/j/123456789",
                bot_name="My Recorder",
            )
            print(f"Bot created: {bot.id}")

    asyncio.run(main())
"""

from .async_client import AsyncMeetingBotClient
from .client import MeetingBotClient
from .exceptions import (
    AuthError,
    MeetingBotError,
    NotFoundError,
    RateLimitError,
    ServerError,
    ValidationError,
)
from .models import (
    AccountInfo,
    ActionItemListResponse,
    ActionItemResponse,
    ActionItemStatsResponse,
    AnalysisResponse,
    AnalyticsResponse,
    ApiKey,
    ApiKeyCreateResponse,
    ApiKeyListResponse,
    ApiUsageResponse,
    AskResponse,
    AuditLogEntry,
    AuditLogResponse,
    BalanceResponse,
    BotListResponse,
    BotResponse,
    BotStats,
    BotSummary,
    CalendarFeedListResponse,
    CalendarFeedResponse,
    CheckoutResponse,
    DefaultPromptResponse,
    ExportJsonResponse,
    FollowupEmailResponse,
    HighlightsResponse,
    IntegrationListResponse,
    IntegrationResponse,
    KeywordAlertListResponse,
    KeywordAlertResponse,
    LoginResponse,
    McpCallResponse,
    McpSchemaResponse,
    MyAnalyticsResponse,
    NotificationPrefs,
    PlanInfo,
    RecurringAnalyticsResponse,
    RetentionPolicyResponse,
    SearchResponse,
    SearchResult,
    ShareResponse,
    TemplateInfo,
    TemplateListResponse,
    Transaction,
    TranscriptEntry,
    TranscriptResponse,
    WebhookDelivery,
    WebhookDeliveryListResponse,
    WebhookEventsResponse,
    WebhookListResponse,
    WebhookResponse,
    WorkspaceListResponse,
    WorkspaceMemberListResponse,
    WorkspaceMemberResponse,
    WorkspaceResponse,
)

__version__ = "1.0.0"

__all__ = [
    # Clients
    "MeetingBotClient",
    "AsyncMeetingBotClient",
    # Exceptions
    "MeetingBotError",
    "AuthError",
    "NotFoundError",
    "RateLimitError",
    "ServerError",
    "ValidationError",
    # Bot models
    "BotResponse",
    "BotSummary",
    "BotListResponse",
    "BotStats",
    "TranscriptEntry",
    "TranscriptResponse",
    "AnalysisResponse",
    "HighlightsResponse",
    "AskResponse",
    "FollowupEmailResponse",
    "ShareResponse",
    # Webhook models
    "WebhookResponse",
    "WebhookListResponse",
    "WebhookDelivery",
    "WebhookDeliveryListResponse",
    "WebhookEventsResponse",
    # Auth models
    "ApiKey",
    "ApiKeyListResponse",
    "ApiKeyCreateResponse",
    "PlanInfo",
    "NotificationPrefs",
    "LoginResponse",
    "AccountInfo",
    # Billing models
    "BalanceResponse",
    "Transaction",
    "CheckoutResponse",
    # Export models
    "ExportJsonResponse",
    # Template models
    "TemplateInfo",
    "TemplateListResponse",
    "DefaultPromptResponse",
    # Analytics models
    "AnalyticsResponse",
    "RecurringAnalyticsResponse",
    "ApiUsageResponse",
    "MyAnalyticsResponse",
    "SearchResult",
    "SearchResponse",
    "AuditLogEntry",
    "AuditLogResponse",
    # Action Item models
    "ActionItemResponse",
    "ActionItemListResponse",
    "ActionItemStatsResponse",
    # Keyword Alert models
    "KeywordAlertResponse",
    "KeywordAlertListResponse",
    # Calendar Feed models
    "CalendarFeedResponse",
    "CalendarFeedListResponse",
    # Integration models
    "IntegrationResponse",
    "IntegrationListResponse",
    # Workspace models
    "WorkspaceResponse",
    "WorkspaceListResponse",
    "WorkspaceMemberResponse",
    "WorkspaceMemberListResponse",
    # Retention models
    "RetentionPolicyResponse",
    # MCP models
    "McpSchemaResponse",
    "McpCallResponse",
]
