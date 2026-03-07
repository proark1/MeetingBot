from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


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
    status: str = Field(
        description=(
            "Current lifecycle status. One of: "
            "`joining` (navigating to meeting), "
            "`in_call` (admitted, recording), "
            "`call_ended` (meeting over, transcribing), "
            "`done` (transcript + analysis ready), "
            "`error` (see error_message). "
            "Auto-leave (empty room or everyone left for 5 min) transitions "
            "the bot from `in_call` → `call_ended` automatically."
        )
    )
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = Field(default=None, description="When the bot was admitted into the call.")
    ended_at: datetime | None = Field(default=None, description="When the call ended or the bot left.")
    transcript: list[dict[str, Any]] = Field(
        default=[],
        description="Array of {speaker, text, timestamp} entries. Populated once status is `done`.",
    )
    analysis: MeetingAnalysis | None = Field(
        default=None,
        description="AI-generated meeting analysis. Populated once status is `done`.",
    )
    recording_url: str | None = None
    extra_metadata: dict[str, Any] = {}

    model_config = {"from_attributes": True}


class BotListResponse(BaseModel):
    results: list[BotResponse]
    count: int
