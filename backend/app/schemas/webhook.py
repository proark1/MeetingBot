from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class WebhookCreate(BaseModel):
    url: str
    events: list[str] = ["*"]  # ["*"] = all events; or specific like ["bot.done", "bot.error"]
    secret: Optional[str] = None  # Optional HMAC signing secret


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
