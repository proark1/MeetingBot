import ipaddress
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, AnyHttpUrl


class KeywordAlertConfig(BaseModel):
    """Per-bot keyword alert specification."""
    keyword: str = Field(max_length=100, description="The trigger keyword or phrase (case-insensitive).")
    webhook_url: Optional[str] = Field(default=None, max_length=2048, description="Optional webhook URL to notify in addition to global webhooks.")


class AgenticInstruction(BaseModel):
    """A single delegated instruction the bot should attempt to act on during a meeting."""
    instruction: str = Field(
        max_length=500,
        description=(
            "Natural-language directive, e.g. 'Ask about Q2 timeline', "
            "'Push back if scope creeps', 'Summarise every 10 minutes'."
        ),
    )
    trigger: Literal["on_topic", "on_silence", "on_interval", "manual"] = Field(
        default="on_topic",
        description=(
            "When the bot should evaluate this instruction. "
            "`on_topic` — when the relevant topic comes up. "
            "`on_silence` — after N seconds of silence. "
            "`on_interval` — every N seconds. "
            "`manual` — only when triggered via the API."
        ),
    )
    interval_seconds: Optional[int] = Field(
        default=None,
        ge=15,
        le=3600,
        description="For trigger=on_interval / on_silence — seconds between evaluations.",
    )
    speak: bool = Field(
        default=False,
        description="When true, the bot uses TTS to speak the response. When false, it posts in chat only.",
    )
    max_invocations: Optional[int] = Field(
        default=3,
        ge=1,
        le=50,
        description="Cap on how many times this single instruction can fire during the meeting.",
    )


class CoachingConfig(BaseModel):
    """Per-bot host-coaching configuration."""
    metrics: list[Literal[
        "talk_time", "interruptions", "filler_words", "silence", "sentiment", "monologue", "pace"
    ]] = Field(
        default=["talk_time", "filler_words", "monologue"],
        description="Which signals the coaching engine should track and emit tips for.",
    )
    nudge_interval_seconds: int = Field(
        default=120, ge=30, le=600,
        description="Minimum seconds between coaching tips per metric.",
    )
    host_speaker_name: Optional[str] = Field(
        default=None, max_length=255,
        description=(
            "Display name of the participant being coached. "
            "When omitted, the first non-bot participant is treated as the host."
        ),
    )
    deliver_via: Literal["sse", "webhook", "both"] = Field(
        default="sse",
        description="Where to push coaching tips. `sse` is private to the host UI; `webhook` fans out to subscribers.",
    )


class SpeakerAnalyticsConfig(BaseModel):
    """Per-bot live speaker analytics configuration."""
    interval_seconds: int = Field(
        default=30, ge=5, le=300,
        description="Seconds between aggregated analytics snapshots.",
    )
    include_sentiment: bool = Field(
        default=False,
        description="When true, run a lightweight sentiment pass on each window (extra AI cost).",
    )
    include_interruptions: bool = Field(
        default=True,
        description="Detect interruption events (speaker A starts within 1.5s of speaker B finishing).",
    )


class CrossMeetingMemoryConfig(BaseModel):
    """Per-bot cross-meeting memory retrieval configuration."""
    lookback_days: int = Field(
        default=30, ge=1, le=365,
        description="How far back to search for related past meetings.",
    )
    max_meetings: int = Field(
        default=5, ge=1, le=20,
        description="Maximum number of past meetings to surface as context.",
    )
    workspace_scope: Literal["account", "workspace", "sub_user"] = Field(
        default="account",
        description="Scope of the memory pool. `workspace` requires `workspace_id` to be set on the bot.",
    )
    inject_into_analysis: bool = Field(
        default=True,
        description="When true, related-meeting summaries are injected into the post-meeting analysis prompt.",
    )


class ChatQaConfig(BaseModel):
    """Per-bot in-meeting @bot chat Q&A configuration."""
    trigger: str = Field(
        default="@bot",
        max_length=64,
        description="Case-insensitive prefix that activates a Q&A reply (e.g. '@bot', '/ask').",
    )
    reply_via: Literal["chat", "voice", "both"] = Field(
        default="chat",
        description="How the bot should deliver answers.",
    )
    rate_limit_seconds: int = Field(
        default=10, ge=0, le=300,
        description="Minimum seconds between Q&A replies (0 disables the throttle).",
    )


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

    meeting_url: AnyHttpUrl = Field(description="Full URL of the Zoom, Google Meet, Teams, or onepizza.io meeting.")
    bot_name: str = Field(default="JustHereToListen.io", max_length=100, description="Display name shown in the meeting.")

    # Where to deliver results when the bot finishes
    webhook_url: Optional[str] = Field(
        default=None,
        max_length=2048,
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
    template: Optional[Literal[
        "default", "sales", "standup", "1on1", "retro",
        "kickoff", "allhands", "postmortem", "interview", "design-review",
    ]] = Field(
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
        max_length=100,
        description="Domain-specific terms to improve transcription accuracy (product names, jargon, etc.). Max 100 terms.",
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
        max_length=50,
        description=(
            "List of keyword/phrase triggers (max 50). When any keyword is detected in the transcript, "
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

    # ── Real-time translation ───────────────────────────────────────────────────
    translation_language: Optional[str] = Field(
        default=None,
        max_length=10,
        description=(
            "BCP-47 language code for real-time translation of live transcript entries "
            "(e.g. `es` for Spanish, `fr` for French). When set, each live entry is also "
            "broadcast as a `bot.live_transcript_translated` WebSocket event."
        ),
    )

    # ── PII detection & redaction ───────────────────────────────────────────────
    pii_redaction: bool = Field(
        default=False,
        description=(
            "When true, detect and redact PII (emails, phone numbers, SSNs, credit card numbers) "
            "from the transcript before analysis. Redacted text is replaced with `[REDACTED]`."
        ),
    )

    # ── Meeting cost estimator ──────────────────────────────────────────────────
    avg_hourly_rate_usd: Optional[float] = Field(
        default=None,
        ge=0,
        description=(
            "Average attendee hourly rate in USD. When provided, `meeting_cost_usd` is calculated "
            "as `attendee_count × rate × duration_hours` and included in the bot response."
        ),
    )

    # ── Opt-in advanced features (all default OFF) ─────────────────────────────
    # Each block below activates a distinct capability. Leave them off to get
    # the lightweight bot behaviour; turn them on selectively per bot.

    # #5 — In-meeting @bot chat Q&A
    enable_chat_qa: bool = Field(
        default=False,
        description=(
            "When true, the bot watches the in-meeting chat for messages "
            "starting with the configured trigger (default `@bot`) and replies "
            "inline using the live transcript as context. Off by default."
        ),
    )
    chat_qa: Optional[ChatQaConfig] = Field(
        default=None,
        description="Fine-tunes how chat-Q&A is triggered and answered. Ignored when `enable_chat_qa` is false.",
    )

    # #7 — Live speaker analytics
    enable_speaker_analytics: bool = Field(
        default=False,
        description=(
            "When true, periodically compute and emit per-speaker talk-time, "
            "interruption count, and (optionally) sentiment via SSE/WS. Off by default."
        ),
    )
    speaker_analytics: Optional[SpeakerAnalyticsConfig] = Field(
        default=None,
        description="Snapshot interval and which signals to compute. Ignored when `enable_speaker_analytics` is false.",
    )

    # #8 — Smart decision/action detection
    enable_decision_detection: bool = Field(
        default=False,
        description=(
            "When true, detect decision and action moments in real time and "
            "fire `bot.decision_detected` webhook events with timestamp + speaker. "
            "Off by default."
        ),
    )

    # #11 — Cross-meeting memory
    enable_cross_meeting_memory: bool = Field(
        default=False,
        description=(
            "When true, retrieve summaries of semantically related past meetings "
            "and (optionally) inject them into this meeting's analysis prompt. "
            "Off by default."
        ),
    )
    cross_meeting_memory: Optional[CrossMeetingMemoryConfig] = Field(
        default=None,
        description="Lookback window, scope, and injection toggle. Ignored when `enable_cross_meeting_memory` is false.",
    )

    # #13 — Host coaching mode
    enable_coaching: bool = Field(
        default=False,
        description=(
            "When true, run a private coaching engine that emits tips to the host "
            "(talk-time dominance, filler words, monologue length, etc.). "
            "Tips are streamed over a private SSE channel by default. Off by default."
        ),
    )
    coaching: Optional[CoachingConfig] = Field(
        default=None,
        description="Which signals to track and how to deliver tips. Ignored when `enable_coaching` is false.",
    )

    # #15 — Agentic delegation (bot-to-bot meetings)
    agentic_instructions: list[AgenticInstruction] = Field(
        default=[],
        max_length=20,
        description=(
            "Natural-language directives for the bot to act on during the meeting. "
            "Empty list = standard listener-only behaviour. Max 20 instructions."
        ),
    )
    agentic_autonomy: Literal["off", "low", "medium", "high"] = Field(
        default="off",
        description=(
            "Master autonomy switch. `off` ignores `agentic_instructions`. "
            "`low` only acts on `manual` triggers, `medium` adds `on_topic`, "
            "`high` allows `on_silence` and `on_interval` triggers too."
        ),
    )

    # Pass-through metadata — returned as-is in bot responses and webhook payloads
    metadata: dict[str, Any] = Field(
        default={},
        description="Arbitrary key-value pairs stored with the bot and echoed in all responses. Max 20 keys, 64 char keys, 256 char string values.",
    )

    @field_validator("meeting_url", mode="before")
    @classmethod
    def validate_meeting_url(cls, v: Any) -> Any:
        return _reject_private_url(v)

    @field_validator("metadata", mode="after")
    @classmethod
    def validate_metadata(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(v) > 20:
            raise ValueError("metadata may contain at most 20 keys")
        for key, val in v.items():
            if len(key) > 64:
                raise ValueError(f"metadata key {key!r} exceeds 64 characters")
            if isinstance(val, str) and len(val) > 256:
                raise ValueError(f"metadata value for {key!r} exceeds 256 characters")
        return v

    @field_validator("vocabulary", mode="after")
    @classmethod
    def validate_vocabulary_items(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is not None:
            for term in v:
                if len(term) > 200:
                    raise ValueError(f"vocabulary term exceeds 200 characters: {term[:30]!r}...")
        return v


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
    topics: list[dict[str, Any]] = []
    # Enriched fields (Round 3)
    risks_blockers: list[str] = []
    next_meeting: Optional[str] = None
    unresolved_items: list[str] = []

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
        description=(
            "Array of `{speaker, text, timestamp, source, message_id?}` entries. "
            "`source` is `voice` for spoken utterances (default) or `chat` for "
            "messages captured from the meeting chat panel. `message_id` is a short "
            "stable hash used internally for chat dedup. Available once status is `done`."
        ),
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
    translation_language: Optional[str] = Field(default=None, description="BCP-47 language the stored transcript was translated to (if post-meeting translation was applied).")
    metadata: dict[str, Any] = {}

    # ── Meeting intelligence ────────────────────────────────────────────────────
    health_score: Optional[int] = Field(
        default=None,
        description=(
            "Meeting quality score from 0–100, computed from participation balance, "
            "decision count, action item count, and meeting length. Available once status is `done`."
        ),
    )
    meeting_cost_usd: Optional[float] = Field(
        default=None,
        description=(
            "Estimated meeting cost in USD (attendee_count × avg_hourly_rate_usd × duration_hours). "
            "Only populated when `avg_hourly_rate_usd` was provided at bot creation."
        ),
    )
    pii_detected: bool = Field(
        default=False,
        description="True if PII was detected in the transcript (only relevant when `pii_redaction=true`).",
    )

    # ── Opt-in advanced features (echoed when enabled) ─────────────────────────
    enable_chat_qa: bool = False
    enable_speaker_analytics: bool = False
    enable_decision_detection: bool = False
    enable_cross_meeting_memory: bool = False
    enable_coaching: bool = False
    agentic_autonomy: str = "off"
    detected_decisions: list[dict[str, Any]] = Field(
        default=[],
        description=(
            "Decision and action moments detected during the meeting "
            "(only populated when `enable_decision_detection=true`). "
            "Each item: {kind: 'decision'|'action', text, speaker, timestamp}."
        ),
    )
    related_meetings: list[dict[str, Any]] = Field(
        default=[],
        description=(
            "Semantically related past meetings retrieved for this bot "
            "(only populated when `enable_cross_meeting_memory=true`)."
        ),
    )

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


class PaginatedResponse(BaseModel):
    """Generic paginated response envelope."""
    results: list = []
    total: int = 0
    limit: int = 50
    offset: int = 0
    has_more: bool = False


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


# ── Live interaction (POST /say and /chat) ────────────────────────────────────

class SayRequest(BaseModel):
    """Speak arbitrary text in the live meeting via TTS + virtual microphone."""
    text: str = Field(min_length=1, max_length=2000, description="Text to speak aloud in the meeting.")
    voice: Literal["gemini", "edge"] = Field(
        default="gemini",
        description=(
            "TTS provider. `gemini` — Google Gemini TTS (natural, ~1–2 s). "
            "`edge` — Microsoft Edge TTS (faster, ~300–500 ms, slightly robotic)."
        ),
    )
    interrupt: bool = Field(
        default=False,
        description=(
            "If true and the bot is already speaking, cancel the in-flight speech "
            "and jump ahead. If false (default) this call queues behind the current speech."
        ),
    )


class SayResponse(BaseModel):
    """Acknowledgement returned by POST /say."""
    bot_id: str
    task_id: str = Field(description="Opaque id for the queued speak task. Useful for logging.")
    queued: bool = True
    interrupted_previous: bool = Field(
        default=False,
        description="True when interrupt=true cancelled an already-running speak task.",
    )


class ChatRequest(BaseModel):
    """Post a message into the live meeting's chat panel."""
    text: str = Field(min_length=1, max_length=2000, description="Text to post to the meeting chat.")


class ChatResponse(BaseModel):
    """Acknowledgement returned by POST /chat."""
    bot_id: str
    task_id: str = Field(description="Opaque id for the queued chat-post task.")
    queued: bool = True
