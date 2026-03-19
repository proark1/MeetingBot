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
