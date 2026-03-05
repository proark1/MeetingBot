from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_validator


class TranscriptEntry(BaseModel):
    speaker: str
    text: str
    timestamp: float  # seconds from start
    words: list[dict] | None = None


class ActionItem(BaseModel):
    task: str
    assignee: str | None = None
    due_date: str | None = None


class MeetingAnalysis(BaseModel):
    summary: str
    key_points: list[str]
    action_items: list[ActionItem]
    decisions: list[str]
    next_steps: list[str]
    sentiment: str  # positive | neutral | negative
    topics: list[str]
    duration_minutes: float | None = None


class BotCreate(BaseModel):
    meeting_url: str
    bot_name: str = "MeetingBot"
    join_at: datetime | None = None
    extra_metadata: dict[str, Any] = {}

    @field_validator("meeting_url")
    @classmethod
    def validate_meeting_url(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("meeting_url must not be empty")
        return v


class BotResponse(BaseModel):
    id: str
    meeting_url: str
    meeting_platform: str
    bot_name: str
    status: str
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    transcript: list[TranscriptEntry]
    analysis: MeetingAnalysis | None
    recording_url: str | None
    extra_metadata: dict[str, Any]

    model_config = {"from_attributes": True}


class BotListResponse(BaseModel):
    results: list[BotResponse]
    count: int
    next: str | None = None
    previous: str | None = None
