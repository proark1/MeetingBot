from datetime import datetime
from uuid import uuid4

from sqlalchemy import String, DateTime, JSON, Text, Index, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Bot(Base):
    """Represents a meeting bot instance."""

    __tablename__ = "bots"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    meeting_url: Mapped[str] = mapped_column(Text, nullable=False)
    meeting_platform: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unknown"
    )

    # Bot config
    bot_name: Mapped[str] = mapped_column(String(128), default="MeetingBot")
    join_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Lifecycle state
    # ready → joining → in_call → call_ended → done | error
    status: Mapped[str] = mapped_column(String(32), default="ready")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Output data
    transcript: Mapped[list] = mapped_column(JSON, default=list)
    participants: Mapped[list] = mapped_column(JSON, default=list)
    analysis: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    chapters: Mapped[list | None] = mapped_column(JSON, nullable=True)
    speaker_stats: Mapped[list | None] = mapped_column(JSON, nullable=True)
    recording_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    recording_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    share_token: Mapped[str | None] = mapped_column(String(24), nullable=True, unique=True)
    notify_email: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Template / vocabulary
    template_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    prompt_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    vocabulary: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Analysis mode: "full" (AI summary + chapters) | "transcript_only" (raw text only)
    analysis_mode: Mapped[str] = mapped_column(String(32), default="full")

    # Live in-call: reply when the bot's name is mentioned
    # respond_on_mention: master on/off toggle
    # mention_response_mode: "text" | "voice" | "both"
    respond_on_mention: Mapped[bool] = mapped_column(Boolean, default=True)
    mention_response_mode: Mapped[str] = mapped_column(String(16), default="text")
    tts_provider: Mapped[str] = mapped_column(String(16), default="edge")

    # Whether the bot joins with its microphone muted.
    # False (default) — mic is on so voice responses play immediately.
    # True — bot joins muted and toggles mic only while speaking TTS.
    start_muted: Mapped[bool] = mapped_column(Boolean, default=False)

    # Whether to run live audio transcription every 15 s during the call.
    # Enables real-time meeting context and voice bot-name detection.
    # If False, audio is only transcribed after the meeting ends (default).
    live_transcription: Mapped[bool] = mapped_column(Boolean, default=False)

    # Arbitrary caller metadata
    extra_metadata: Mapped[dict] = mapped_column(JSON, default=dict)

    __table_args__ = (
        Index("ix_bot_status",      "status"),
        Index("ix_bot_created_at",  "created_at"),
        Index("ix_bot_meeting_url", "meeting_url"),
    )
