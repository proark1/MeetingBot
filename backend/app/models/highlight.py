from datetime import datetime
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, Float, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Highlight(Base):
    """A bookmarked transcript moment with an optional comment."""

    __tablename__ = "highlights"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    bot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[float] = mapped_column(Float, nullable=False)
    text_snippet: Mapped[str] = mapped_column(Text, nullable=False, default="")
    speaker: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
