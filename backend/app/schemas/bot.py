import ipaddress
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, AnyHttpUrl


class KeywordAlertConfig(BaseModel):
    """Per-bot keyword alert specification."""
    keyword: str = Field(description="The trigger keyword or phrase (case-insensitive).")
    webhook_url: Optional[str] = Field(default=None, description="Optional webhook URL to notify in addition to global webhooks.")


def _reject_private_url(v: Any) -> str:
    """Reject meeting URLs that target localhost or private IP addresses."""
    from urllib.parse import urlparse

    url_str = str(v)
    try:
        parsed = urlparse(url_str)
        hostname = parsed.hostname or ""

        if hostname.lower() in ("localhost", "localhost."):
            raise ValueError("URL must not target localhost")

        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise ValueError(f"URL targets a private/internal address: {hostname}")
        except ValueError as ip_exc:
            if "private" in str(ip_exc) or "loopback" in str(ip_exc) or "internal" in str(ip_exc):
                raise
    except ValueError:
        raise
    except Exception:
        pass
    return url_str


class BotCreate(BaseModel):
    """Request body for creating a new meeting bot."""

    meeting_url: AnyHttpUrl = Field(description="Full URL of the Zoom, Google Meet, or Teams meeting.")
    bot_name: str = Field(default="MeetingBot", max_length=100, description="Display name shown in the meeting.")

    # Where to deliver results when the bot finishes
    webhook_url: Optional[str] = Field(
        default=None,
        description=(
            "HTTPS URL to POST the full meeting results to once the bot finishes. "
            "The payload includes status, transcript, analysis, participants, and AI usage. "
            "Leave empty if you prefer to poll GET /api/v1/bot/{id} instead."
        ),
    )

    # Scheduling
    join_at: Optional[datetime] = Field(
        default=None,
        description="ISO-8601 datetime to schedule the bot join. Omit to join immediately.",
    )

    # Analysis options
    analysis_mode: Literal["full", "transcript_only"] = Field(
        default="full",
        description=(
            "`full` — AI summary, key points, action items, decisions, sentiment, topics, chapters. "
            "`transcript_only` — skip all AI analysis, return only the raw transcript."
        ),
    )
    template: Optional[str] = Field(
        default=None,
        description=(
            "Built-in analysis template. One of: default, sales, standup, 1on1, retro, "
            "kickoff, allhands, postmortem, interview, design-review. "
            "Leave empty for the default general-purpose template."
        ),
    )
    prompt_override: Optional[str] = Field(
        default=None,
        max_length=8000,
        description="Custom analysis prompt. Overrides `template` when both are provided.",
    )
    vocabulary: Optional[list[str]] = Field(
        default=None,
        description="Domain-specific terms to improve transcription accuracy (product names, jargon, etc.).",
    )

    # In-call bot behaviour
    respond_on_mention: bool = Field(
        default=True,
        description="When true, the bot monitors live captions and replies when its name is mentioned.",
    )
    mention_response_mode: Literal["text", "voice", "both"] = Field(
        default="text",
        description="`text` — chat message. `voice` — TTS. `both` — chat + TTS.",
    )
    tts_provider: Literal["edge", "gemini"] = Field(
        default="edge",
        description="`edge` — Microsoft Edge TTS (fast, free). `gemini` — Google Gemini TTS (more natural).",
    )
    start_muted: bool = Field(default=False, description="Join with microphone muted.")
    live_transcription: bool = Field(
        default=False,
        description="Transcribe audio in real-time during the call (15-second chunks).",
    )

    # Business account sub-user isolation — also settable via X-Sub-User header
    sub_user_id: Optional[str] = Field(
        default=None,
        max_length=255,
        description=(
            "For business accounts: an opaque identifier for the end-user this bot belongs to. "
            "When set, only requests with the same sub_user_id (via this field or X-Sub-User header) "
            "can see this bot's data. Can also be set via the X-Sub-User request header."
        ),
    )

    # Bot persona (white-label / branding)
    bot_avatar_url: Optional[str] = Field(
        default=None,
        max_length=2048,
        description="URL of the avatar image shown as the bot's profile picture in the meeting.",
    )

    # Video recording
    record_video: bool = Field(
        default=False,
        description=(
            "Capture a video recording of the meeting screen (MP4) in addition to audio. "
            "Download via GET /api/v1/bot/{id}/video once status is `done`."
        ),
    )

    # ── Consent / recording announcement ──────────────────────────────────────
    consent_enabled: bool = Field(
        default=False,
        description=(
            "When true, the bot announces the recording at the start of the meeting "
            "and monitors for opt-out requests. Participants who say or type the opt-out "
            "phrase are removed from the transcript."
        ),
    )
    consent_message: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Custom consent announcement text. Overrides the platform default when set.",
    )

    # ── Keyword alerts ─────────────────────────────────────────────────────────
    keyword_alerts: list[KeywordAlertConfig] = Field(
        default=[],
        description=(
            "List of keyword/phrase triggers. When any keyword is detected in the transcript, "
            "a `bot.keyword_alert` webhook event is fired. Account-level KeywordAlert rules "
            "defined at /api/v1/keyword-alerts are also applied automatically."
        ),
    )

    # ── Follow-up email ────────────────────────────────────────────────────────
    auto_followup_email: bool = Field(
        default=False,
        description=(
            "When true, automatically generate and send a follow-up email draft to the account's "
            "notification email after the meeting analysis is complete."
        ),
    )

    # ── Workspace ──────────────────────────────────────────────────────────────
    workspace_id: Optional[str] = Field(
        default=None,
        max_length=36,
        description="Associate this bot with a team workspace (see /api/v1/workspaces).",
    )

    # ── Transcription provider ─────────────────────────────────────────────────
    transcription_provider: Literal["gemini", "whisper"] = Field(
        default="gemini",
        description=(
            "`gemini` — Gemini Files API (default, requires GEMINI_API_KEY). "
            "`whisper` — local OpenAI Whisper model (privacy-preserving, requires WHISPER_ENABLED=true)."
        ),
    )

    # Pass-through metadata — returned as-is in bot responses and webhook payloads
    metadata: dict[str, Any] = Field(
        default={},
        description="Arbitrary key-value pairs stored with the bot and echoed in all responses.",
    )

    @field_validator("meeting_url", mode="before")
    @classmethod
    def validate_meeting_url(cls, v: Any) -> Any:
        return _reject_private_url(v)


# ── Response schemas ───────────────────────────────────────────────────────────

class AIUsageEntry(BaseModel):
    operation: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0

    model_config = {"extra": "allow"}


class AIUsageSummary(BaseModel):
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    primary_model: Optional[str] = None
    operations: list[AIUsageEntry] = []

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


class BotResponse(BaseModel):
    id: str
    meeting_url: str
    meeting_platform: str
    bot_name: str
    status: str = Field(
        description=(
            "Current lifecycle status:\n"
            "- `ready` — created, about to join immediately\n"
            "- `scheduled` — waiting for the scheduled `join_at` time\n"
            "- `queued` — waiting for a free bot slot (`MAX_CONCURRENT_BOTS` reached)\n"
            "- `joining` — Chromium browser launching and joining the meeting\n"
            "- `in_call` — recording in progress\n"
            "- `call_ended` — meeting ended, audio saved\n"
            "- `transcribing` — audio being transcribed by AI\n"
            "- `done` — transcript and analysis complete ✓\n"
            "- `error` — unrecoverable error (see `error_message`)\n"
            "- `cancelled` — stopped via DELETE /api/v1/bot/{id}\n\n"
            "Poll this endpoint until `done` or `error`, or use `webhook_url` for push delivery."
        )
    )
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_seconds: Optional[float] = Field(default=None, description="Meeting duration in seconds.")

    participants: list[str] = []
    transcript: list[dict[str, Any]] = Field(
        default=[],
        description="Array of {speaker, text, timestamp} entries. Available once status is `done`.",
    )
    analysis: Optional[MeetingAnalysis] = Field(
        default=None,
        description="AI-generated analysis. Available once status is `done` (analysis_mode=full).",
    )
    chapters: list[dict] = []
    speaker_stats: list[dict] = []

    recording_available: bool = Field(
        default=False,
        description="True when a WAV recording can be downloaded via GET /api/v1/bot/{id}/recording.",
    )
    video_available: bool = Field(
        default=False,
        description="True when an MP4 video recording can be downloaded via GET /api/v1/bot/{id}/video.",
    )
    bot_avatar_url: Optional[str] = Field(default=None, description="Bot avatar URL used in the meeting.")

    analysis_mode: str = "full"
    is_demo_transcript: bool = False
    sub_user_id: Optional[str] = Field(default=None, description="Business account sub-user identifier (if set).")
    metadata: dict[str, Any] = {}

    ai_usage: Optional[AIUsageSummary] = None


class BotSummary(BaseModel):
    """Lightweight representation returned by the list endpoint (no transcript/analysis)."""
    id: str
    meeting_url: str
    meeting_platform: str
    bot_name: str
    status: str = Field(
        description=(
            "Current lifecycle status. One of: `ready`, `scheduled`, `queued`, `joining`, "
            "`in_call`, `call_ended`, `transcribing`, `done`, `error`, `cancelled`."
        )
    )
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    participants: list[str] = []
    recording_available: bool = False
    analysis_mode: str = "full"
    is_demo_transcript: bool = False
    sub_user_id: Optional[str] = Field(default=None, description="Business account sub-user identifier (if set).")
    metadata: dict[str, Any] = {}
    ai_total_tokens: int = 0
    ai_total_cost_usd: float = 0.0
    ai_primary_model: Optional[str] = None


class BotListResponse(BaseModel):
    results: list[BotSummary]
    total: int
    limit: int
    offset: int


class Highlight(BaseModel):
    type: str
    text: str
    detail: dict[str, Any] = {}


class HighlightResponse(BaseModel):
    bot_id: str
    highlights: list[Highlight]
