from datetime import datetime
from typing import Optional

from pydantic import BaseModel, field_validator


class WebhookCreate(BaseModel):
    url: str
    events: list[str] = ["*"]  # ["*"] = all events; or specific like ["bot.done", "bot.error"]
    secret: Optional[str] = None  # Optional HMAC signing secret

    model_config = {"json_schema_extra": {
        "examples": [
            {
                "url": "https://api.acme.com/justheretolisten/webhook",
                "events": ["*"],
                "secret": "whsec_super_secret_for_hmac",
            },
            {
                "url": "https://api.acme.com/justheretolisten/webhook",
                "events": ["bot.done", "bot.error", "bot.keyword_alert"],
            },
        ],
    }}

    @field_validator("events")
    @classmethod
    def validate_events(cls, v: list[str]) -> list[str]:
        if v == ["*"]:
            return v
        from app.api.webhooks import WEBHOOK_EVENTS
        unknown = [e for e in v if e not in WEBHOOK_EVENTS]
        if unknown:
            raise ValueError(
                f"Unknown event(s): {unknown!r}. Valid events: {WEBHOOK_EVENTS}"
            )
        return v


class WebhookResponse(BaseModel):
    id: str
    url: str
    events: list[str]
    is_active: bool
    created_at: datetime
    delivery_attempts: int = 0
    last_delivery_at: Optional[datetime] = None
    last_delivery_status: Optional[int] = None
    consecutive_failures: int = 0
    account_id: Optional[str] = None

    model_config = {"json_schema_extra": {"example": {
        "id": "wh_5fa921b7",
        "url": "https://api.acme.com/justheretolisten/webhook",
        "events": ["bot.done", "bot.error", "bot.keyword_alert"],
        "is_active": True,
        "created_at": "2026-05-04T12:00:00Z",
        "delivery_attempts": 124,
        "last_delivery_at": "2026-05-04T15:34:18Z",
        "last_delivery_status": 200,
        "consecutive_failures": 0,
        "account_id": "550e8400-e29b-41d4-a716-446655440000",
    }}}


# ── Webhook event payload schemas (for OpenAPI documentation) ─────────────────

class WebhookEventPayload(BaseModel):
    """Base payload delivered to webhook endpoints.

    All webhook events share this structure. The `data` field varies by event type.
    Payloads are signed with HMAC-SHA256 when a webhook secret is configured.
    Signature is in the `X-MeetingBot-Signature` header; timestamp in `X-MeetingBot-Timestamp`.
    """
    event: str
    bot_id: Optional[str] = None
    timestamp: str
    data: dict = {}

    model_config = {"json_schema_extra": {
        "examples": [{
            "event": "bot.done",
            "bot_id": "abc123",
            "timestamp": "2026-04-04T12:00:00Z",
            "data": {
                "status": "done",
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "transcript": [{"speaker": "Alice", "text": "Hello", "timestamp": 0.0}],
                "summary": "Team discussed Q2 roadmap...",
            },
        }],
    }}


class WebhookEventList(BaseModel):
    """Supported webhook event names.

    Events:
    - `bot.joining` — Bot is connecting to the meeting
    - `bot.in_call` — Bot has joined and is recording
    - `bot.call_ended` — Meeting ended, transcription starting
    - `bot.transcript_ready` — Raw transcript is available
    - `bot.analysis_ready` — AI analysis is complete
    - `bot.done` — All processing finished (full payload with transcript + analysis)
    - `bot.error` — Bot encountered an error
    - `bot.cancelled` — Bot was cancelled by user
    - `bot.keyword_alert` — A monitored keyword was detected in the transcript
    - `bot.live_transcript` — Real-time transcript entry (during call)
    - `bot.live_transcript_translated` — Translated live transcript entry
    - `bot.recurring_intel_ready` — Recurring meeting intelligence report ready
    - `bot.test` — Test event sent from webhook playground
    """
    events: list[str]

    model_config = {"json_schema_extra": {"example": {
        "events": [
            "bot.joining", "bot.in_call", "bot.call_ended",
            "bot.transcript_ready", "bot.analysis_ready",
            "bot.done", "bot.error", "bot.cancelled",
            "bot.keyword_alert",
            "bot.live_transcript", "bot.live_transcript_translated", "bot.live_chat_message",
            "bot.recurring_intel_ready", "bot.test",
        ],
    }}}
