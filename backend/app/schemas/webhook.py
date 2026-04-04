from datetime import datetime
from typing import Optional

from pydantic import BaseModel, field_validator


class WebhookCreate(BaseModel):
    url: str
    events: list[str] = ["*"]  # ["*"] = all events; or specific like ["bot.done", "bot.error"]
    secret: Optional[str] = None  # Optional HMAC signing secret

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
