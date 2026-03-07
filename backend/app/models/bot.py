from datetime import datetime
from uuid import uuid4

from sqlalchemy import String, DateTime, JSON, Text
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
    recording_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Arbitrary caller metadata
    extra_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
