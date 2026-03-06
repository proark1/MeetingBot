from datetime import datetime
from typing import Any

from pydantic import BaseModel


class BotCreate(BaseModel):
    meeting_url: str
    bot_name: str = "MeetingBot"
    join_at: datetime | None = None
    extra_metadata: dict[str, Any] = {}


class MeetingAnalysis(BaseModel):
    summary: str = ""
    key_points: list[str] = []
    action_items: list[dict[str, Any]] = []
    decisions: list[str] = []
    next_steps: list[str] = []
    sentiment: str = "neutral"
    topics: list[str] = []

    model_config = {"extra": "allow"}


class BotResponse(BaseModel):
    id: str
    meeting_url: str
    meeting_platform: str
    bot_name: str
    status: str
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    transcript: list[dict[str, Any]] = []
    analysis: MeetingAnalysis | None = None
    recording_url: str | None = None
    extra_metadata: dict[str, Any] = {}

    model_config = {"from_attributes": True}


class BotListResponse(BaseModel):
    results: list[BotResponse]
    count: int
