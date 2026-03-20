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
    ApiKey,
    ApiKeyCreateResponse,
    ApiKeyListResponse,
    BalanceResponse,
    BotListResponse,
    BotResponse,
    BotStats,
    BotSummary,
    CheckoutResponse,
    ExportJsonResponse,
    LoginResponse,
    NotificationPrefs,
    PlanInfo,
    Transaction,
    WebhookDelivery,
    WebhookDeliveryListResponse,
    WebhookListResponse,
    WebhookResponse,
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
    # Models
    "BotResponse",
    "BotSummary",
    "BotListResponse",
    "BotStats",
    "WebhookResponse",
    "WebhookListResponse",
    "WebhookDelivery",
    "WebhookDeliveryListResponse",
    "ApiKey",
    "ApiKeyListResponse",
    "ApiKeyCreateResponse",
    "PlanInfo",
    "NotificationPrefs",
    "LoginResponse",
    "BalanceResponse",
    "Transaction",
    "CheckoutResponse",
    "ExportJsonResponse",
]
