"""SQLAlchemy models for accounts, API keys, credit transactions, and billing."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    credits_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    transactions: Mapped[list["CreditTransaction"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    stripe_topups: Mapped[list["StripeTopUp"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    usdc_deposit: Mapped[Optional["UsdcDeposit"]] = relationship(back_populates="account", uselist=False, cascade="all, delete-orphan")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), default="Default")
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    account: Mapped["Account"] = relationship(back_populates="api_keys")


class CreditTransaction(Base):
    __tablename__ = "credit_transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"), nullable=False, index=True)
    # positive = credit added, negative = credit deducted
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)  # stripe_topup | usdc_topup | bot_usage
    description: Mapped[str] = mapped_column(Text, default="")
    reference_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # Stripe session / tx hash / bot id
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    account: Mapped["Account"] = relationship(back_populates="transactions")


class StripeTopUp(Base):
    __tablename__ = "stripe_topups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"), nullable=False, index=True)
    stripe_session_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | completed | expired
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    account: Mapped["Account"] = relationship(back_populates="stripe_topups")


class UsdcDeposit(Base):
    __tablename__ = "usdc_deposits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"), nullable=False, unique=True, index=True)
    deposit_address: Mapped[str] = mapped_column(String(42), unique=True, nullable=False, index=True)
    hd_index: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    account: Mapped["Account"] = relationship(back_populates="usdc_deposit")


class MonitorState(Base):
    """Persists state for background tasks (e.g. last processed Ethereum block)."""

    __tablename__ = "monitor_state"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class PlatformConfig(Base):
    """Platform-level configuration managed by admins (e.g. USDC collection wallet)."""

    __tablename__ = "platform_config"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
