from datetime import datetime
from uuid import uuid4

from sqlalchemy import String, DateTime, JSON, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Webhook(Base):
    """Registered webhook endpoint."""

    __tablename__ = "webhooks"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    # Comma-separated event list, or "*" for all
    events: Mapped[str] = mapped_column(Text, default="*")
    secret: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )

    # Delivery stats
    delivery_attempts: Mapped[int] = mapped_column(default=0)
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    last_delivery_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    last_delivery_status: Mapped[int | None] = mapped_column(nullable=True)
