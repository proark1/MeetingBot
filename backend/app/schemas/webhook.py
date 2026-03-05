from datetime import datetime

from pydantic import BaseModel, field_validator

VALID_EVENTS = {
    "bot.joining",
    "bot.in_call",
    "bot.call_ended",
    "bot.done",
    "bot.error",
    "bot.transcript_ready",
    "bot.analysis_ready",
}


class WebhookCreate(BaseModel):
    url: str
    events: list[str] = ["*"]
    secret: str | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return v

    @field_validator("events")
    @classmethod
    def validate_events(cls, v: list[str]) -> list[str]:
        if "*" in v:
            return ["*"]
        invalid = set(v) - VALID_EVENTS
        if invalid:
            raise ValueError(f"Unknown events: {invalid}. Valid: {VALID_EVENTS}")
        return v


class WebhookResponse(BaseModel):
    id: str
    url: str
    events: list[str]
    is_active: bool
    created_at: datetime
    delivery_attempts: int
    last_delivery_at: datetime | None
    last_delivery_status: int | None

    model_config = {"from_attributes": True}
