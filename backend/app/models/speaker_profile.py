"""Cross-meeting speaker identity — aggregated stats across all meetings."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Float, Index, Integer, String, Text, DateTime, JSON, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SpeakerProfile(Base):
    """Persistent identity for a recurring meeting participant.

    The canonical_name is the normalised display name used as the primary key
    for matching.  Aliases capture spelling variations seen across meetings.
    """

    __tablename__ = "speaker_profiles"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    canonical_name: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    avatar_initials: Mapped[str | None] = mapped_column(String(4), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Aggregated stats — updated after each meeting
    meeting_count: Mapped[int] = mapped_column(Integer, default=0)
    total_talk_time_s: Mapped[float] = mapped_column(Float, default=0.0)
    avg_talk_pct: Mapped[float] = mapped_column(Float, default=0.0)
    total_questions: Mapped[int] = mapped_column(Integer, default=0)
    total_filler_words: Mapped[int] = mapped_column(Integer, default=0)

    # Timestamps
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        Index("ix_speaker_profile_canonical_name", "canonical_name"),
    )
