from datetime import datetime

from pydantic import BaseModel


class WebhookCreate(BaseModel):
    url: str
    events: list[str] = ["*"]
    secret: str | None = None


class WebhookResponse(BaseModel):
    id: str
    url: str
    events: list[str]
    is_active: bool
    created_at: datetime
    delivery_attempts: int = 0
    last_delivery_at: datetime | None = None
    last_delivery_status: int | None = None

    model_config = {"from_attributes": True}
