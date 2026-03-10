import ipaddress
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, AnyHttpUrl


def _reject_private_url(v: Any) -> str:
    """Raise if the URL resolves to a private/loopback address (SSRF prevention)."""
    import socket
    url_str = str(v)
    try:
        from urllib.parse import urlparse
        hostname = urlparse(url_str).hostname or ""
        # Resolve to IP (getaddrinfo handles both IPv4 and IPv6)
        results = socket.getaddrinfo(hostname, None)
        for _, _, _, _, sockaddr in results:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise ValueError(f"URL resolves to a private/internal address: {sockaddr[0]}")
    except ValueError:
        raise
    except Exception:
        pass  # DNS failure etc. — let the delivery attempt fail naturally
    return url_str


class BotCreate(BaseModel):
    meeting_url: AnyHttpUrl
    bot_name: str = Field(default="MeetingBot", max_length=100)
    join_at: datetime | None = None
    notify_email: str | None = None
    template_id: str | None = None
    vocabulary: list[str] | None = None
    analysis_mode: Literal["full", "transcript_only"] = Field(
        default="full",
        description=(
            "Controls post-meeting processing. "
            "`full` (default) runs AI analysis, smart chapters, and action-item extraction. "
            "`transcript_only` skips all AI processing and returns only the raw transcript."
        ),
    )
    respond_on_mention: bool = Field(
        default=True,
        description=(
            "When true, the bot monitors live captions during the call and sends a "
            "short Gemini-generated reply to the meeting chat whenever its name is mentioned."
        ),
    )
    extra_metadata: dict[str, Any] = {}

    @field_validator("meeting_url", mode="before")
    @classmethod
    def validate_meeting_url(cls, v: Any) -> Any:
        # AnyHttpUrl handles basic URL validation; we just return the value here
        # so Pydantic can coerce it. The AnyHttpUrl type enforces http/https.
        return v


class MeetingAnalysis(BaseModel):
    summary: str = ""
    key_points: list[str] = []
    action_items: list[dict[str, Any]] = []
    decisions: list[str] = []
    next_steps: list[str] = []
    sentiment: str = "neutral"
    topics: list[str] = []

    model_config = {"extra": "allow"}


class BotSummary(BaseModel):
    """Lightweight bot representation returned by the list endpoint.
    Omits transcript and analysis to keep list responses small."""
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
    participants: list[str] = []
    recording_url: str | None = None
    share_token: str | None = None
    analysis_mode: str = "full"
    respond_on_mention: bool = True
    extra_metadata: dict[str, Any] = {}
    is_demo_transcript: bool = False

    model_config = {"from_attributes": True}


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
    participants: list[str] = Field(
        default=[],
        description="List of participant display names detected during the meeting.",
    )
    transcript: list[dict[str, Any]] = Field(
        default=[],
        description="Array of {speaker, text, timestamp} entries. Populated once status is `done`.",
    )
    analysis: MeetingAnalysis | None = Field(
        default=None,
        description="AI-generated meeting analysis. Populated once status is `done`.",
    )
    recording_url: str | None = None
    recording_path: str | None = None
    share_token: str | None = None
    chapters: list[dict] | None = None
    speaker_stats: list[dict] | None = None
    analysis_mode: str = Field(
        default="full",
        description="Whether AI analysis was run (`full`) or skipped (`transcript_only`).",
    )
    respond_on_mention: bool = Field(
        default=True,
        description="Whether the bot is configured to reply in chat when its name is mentioned.",
    )
    extra_metadata: dict[str, Any] = {}
    is_demo_transcript: bool = Field(
        default=False,
        description="True when the transcript was AI-generated because no real audio was captured.",
    )

    model_config = {"from_attributes": True}


class BotListResponse(BaseModel):
    results: list[BotSummary]
    count: int
