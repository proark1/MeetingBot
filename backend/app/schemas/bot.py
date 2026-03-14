import ipaddress
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, AnyHttpUrl


def _reject_private_url(v: Any) -> str:
    """Reject meeting URLs that target localhost or private IP addresses.

    Only checks the literal hostname — no DNS lookup is performed, because:
    1. Meeting URLs are navigated by a browser, not fetched by the server,
       so full SSRF prevention (DNS rebinding etc.) is unnecessary here.
    2. A synchronous DNS lookup blocks the async event loop and fails when
       the network is unreachable, breaking bot creation entirely.
    """
    from urllib.parse import urlparse

    url_str = str(v)
    try:
        parsed = urlparse(url_str)
        hostname = parsed.hostname or ""

        # Reject "localhost" before anything else
        if hostname.lower() in ("localhost", "localhost."):
            raise ValueError("URL must not target localhost")

        # If the hostname is a bare IP address, check it immediately
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise ValueError(f"URL targets a private/internal address: {hostname}")
        except ValueError as ip_exc:
            if "private" in str(ip_exc) or "loopback" in str(ip_exc) or "internal" in str(ip_exc):
                raise  # re-raise our own rejection
            # hostname is not an IP literal — that's fine, it's a normal domain
    except ValueError:
        raise
    except Exception:
        pass
    return url_str


class BotCreate(BaseModel):
    meeting_url: AnyHttpUrl
    bot_name: str = Field(default="MeetingBot", max_length=100)
    join_at: datetime | None = None
    notify_email: str | None = None
    template_id: str | None = None
    prompt_override: str | None = Field(
        None,
        max_length=8000,
        description=(
            "Custom analysis prompt used when template_id is 'seed-customized'. "
            "Required when selecting the Customized template; ignored for all other templates. "
            "Maximum 8000 characters."
        ),
    )
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
            "When true, the bot monitors live captions during the call and replies "
            "whenever its name is mentioned."
        ),
    )
    mention_response_mode: Literal["text", "voice", "both"] = Field(
        default="text",
        description=(
            "How the bot responds when its name is mentioned. "
            "`text` — sends a message in the meeting chat. "
            "`voice` — speaks the reply aloud via TTS so all participants hear it. "
            "`both` — does both."
        ),
    )
    tts_provider: Literal["edge", "gemini"] = Field(
        default="edge",
        description=(
            "TTS engine used for voice responses. "
            "`edge` (default) — Microsoft Edge TTS: fast (~300 ms), free, no extra key. "
            "`gemini` — Google Gemini TTS: more natural voice, uses your GEMINI_API_KEY."
        ),
    )
    start_muted: bool = Field(
        default=False,
        description=(
            "Whether the bot joins with its microphone muted. "
            "False (default) — bot joins with mic on so TTS plays immediately. "
            "True — bot joins muted and unmutes briefly only while speaking TTS."
        ),
    )
    live_transcription: bool = Field(
        default=False,
        description=(
            "When true, the bot transcribes audio in 15-second live chunks during the call. "
            "This enables real-time meeting context (bot can answer 'what did we just discuss?') "
            "and voice-based bot-name detection without relying on DOM captions. "
            "When false (default), audio is only transcribed after the meeting ends."
        ),
    )
    extra_metadata: dict[str, Any] = {}

    @field_validator("meeting_url", mode="before")
    @classmethod
    def validate_meeting_url(cls, v: Any) -> Any:
        return _reject_private_url(v)


class AIUsageEntry(BaseModel):
    """A single AI API call made during the bot lifecycle."""
    operation: str = Field(description="What the call was for: transcription, analysis, chapters, etc.")
    provider: str = Field(description="AI provider: 'anthropic' or 'google'.")
    model: str = Field(description="Model ID used (e.g. 'claude-opus-4-6', 'gemini-2.5-flash').")
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = Field(default=0.0, description="Estimated cost in USD for this call.")
    duration_s: float = Field(default=0.0, description="Wall-clock time for the API call.")

    model_config = {"extra": "allow"}


class AIUsageSummary(BaseModel):
    """Aggregated AI usage for a meeting session."""
    total_tokens: int = 0
    total_cost_usd: float = Field(default=0.0, description="Total estimated cost in USD.")
    primary_model: str | None = Field(default=None, description="Model used for the majority of tokens.")
    meeting_duration_s: float = Field(default=0.0, description="Meeting duration in seconds.")
    operations: list[AIUsageEntry] = Field(default=[], description="Per-operation breakdown.")

    model_config = {"extra": "allow"}


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
    mention_response_mode: str = "text"
    tts_provider: str = "edge"
    start_muted: bool = False
    live_transcription: bool = False
    extra_metadata: dict[str, Any] = {}
    is_demo_transcript: bool = False
    ai_total_tokens: int = Field(default=0, description="Total AI tokens used across all operations.")
    ai_total_cost_usd: float = Field(default=0.0, description="Total estimated AI cost in USD.")
    ai_primary_model: str | None = Field(default=None, description="Primary AI model used.")
    meeting_duration_s: float = Field(default=0.0, description="Meeting duration in seconds.")

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
    # Internal filesystem path — never exposed to API consumers; the recording
    # is served through GET /api/v1/bot/{id}/recording instead.
    recording_path: str | None = Field(default=None, exclude=True)
    share_token: str | None = None
    chapters: list[dict] | None = None
    speaker_stats: list[dict] | None = None
    analysis_mode: str = Field(
        default="full",
        description="Whether AI analysis was run (`full`) or skipped (`transcript_only`).",
    )
    respond_on_mention: bool = Field(
        default=True,
        description="Whether the bot replies when its name is mentioned.",
    )
    mention_response_mode: str = Field(
        default="text",
        description="How the bot replies: 'text' (chat), 'voice' (TTS), or 'both'.",
    )
    tts_provider: str = Field(
        default="edge",
        description="TTS engine: 'edge' (Microsoft Edge TTS) or 'gemini' (Gemini TTS).",
    )
    start_muted: bool = Field(
        default=False,
        description="Whether the bot joined with its microphone muted.",
    )
    live_transcription: bool = Field(
        default=False,
        description="Whether live 15-second audio transcription was enabled during the call.",
    )
    extra_metadata: dict[str, Any] = {}
    is_demo_transcript: bool = Field(
        default=False,
        description="True when the transcript was AI-generated because no real audio was captured.",
    )
    ai_usage: AIUsageSummary | None = Field(
        default=None,
        description="AI usage breakdown: tokens, cost, model, and per-operation detail.",
    )

    model_config = {"from_attributes": True}


class BotListResponse(BaseModel):
    results: list[BotSummary]
    count: int
