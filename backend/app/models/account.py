"""SQLAlchemy models for accounts, API keys, credit transactions, and billing."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text
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
    # "personal" (default) or "business" — business accounts can scope data per sub-user
    account_type: Mapped[str] = mapped_column(String(20), default="personal", nullable=False)
    wallet_address: Mapped[Optional[str]] = mapped_column(String(42), unique=True, nullable=True, index=True)
    # Subscription plan: "free" | "starter" | "pro" | "business"
    plan: Mapped[str] = mapped_column(String(20), default="free", nullable=False)
    # Monthly usage counters (reset on billing cycle)
    monthly_bots_used: Mapped[int] = mapped_column(Integer, default=0)
    monthly_reset_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Stripe subscription fields
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Email notification preferences
    notify_on_done: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Brute-force lockout tracking
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_failed_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    transactions: Mapped[list["CreditTransaction"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    stripe_topups: Mapped[list["StripeTopUp"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    usdc_deposit: Mapped[Optional["UsdcDeposit"]] = relationship(back_populates="account", uselist=False, cascade="all, delete-orphan")
    integrations: Mapped[list["Integration"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    calendar_feeds: Mapped[list["CalendarFeed"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    oauth_accounts: Mapped[list["OAuthAccount"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    keyword_alerts: Mapped[list["KeywordAlert"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    support_keys: Mapped[list["SupportKey"]] = relationship(back_populates="account", cascade="all, delete-orphan")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), default="Default")
    # "live" (default) or "test" — test keys return demo data without deducting credits
    mode: Mapped[str] = mapped_column(String(10), default="live", nullable=False)
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


class BotSnapshot(Base):
    """Persists completed/error/cancelled bot sessions across server restarts."""

    __tablename__ = "bot_snapshots"
    __table_args__ = (
        # Composite index for the most common query: filter by account + sort by date
        Index("ix_bot_snapshots_account_created", "account_id", "created_at"),
        # Composite index used by sub-user isolation queries
        Index("ix_bot_snapshots_account_sub_user", "account_id", "sub_user_id"),
        # Composite index for weekly-digest and analytics queries that filter by account + status
        Index("ix_bot_snapshots_account_status", "account_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    # For business accounts: isolates data per end-user within the account
    sub_user_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    meeting_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    data: Mapped[str] = mapped_column(Text, nullable=False)  # JSON-serialized BotSession fields


class Webhook(Base):
    """Persists per-account webhook registrations across server restarts."""

    __tablename__ = "webhooks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Owning account — NULL means a legacy/superadmin global webhook.
    # Always filter by account_id in tenant-facing queries to prevent cross-tenant leaks.
    account_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    events: Mapped[str] = mapped_column(Text, nullable=False)  # JSON-serialized list of event names
    # Stored in plaintext — required to compute HMAC-SHA256 signatures on outgoing deliveries
    secret: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_delivery_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_delivery_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)


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


class UnmatchedUsdcTransfer(Base):
    """
    Records USDC transfers sent to the platform wallet that could not be attributed
    to any registered user account (e.g. the sender's wallet was not registered).

    Admins can inspect these via GET /admin/usdc/unmatched and manually credit
    the correct account using POST /admin/credit once the user is identified.
    """

    __tablename__ = "unmatched_usdc_transfers"

    tx_hash: Mapped[str] = mapped_column(String(66), primary_key=True)
    from_address: Mapped[str] = mapped_column(String(42), nullable=False, index=True)
    to_address: Mapped[str] = mapped_column(String(42), nullable=False)
    amount_usdc: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    block_number: Mapped[int] = mapped_column(Integer, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolution_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class AuditLog(Base):
    """GDPR-compliant audit trail of account actions."""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    resource_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    resource_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON extra context
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class Integration(Base):
    """Third-party integration config per account (Slack, Notion, etc.)."""

    __tablename__ = "integrations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(50), nullable=False)  # "slack" | "notion"
    name: Mapped[str] = mapped_column(String(100), default="")
    config: Mapped[str] = mapped_column(Text, default="{}")  # JSON: webhook_url, token, channel, etc.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    account: Mapped["Account"] = relationship(back_populates="integrations")


class CalendarFeed(Base):
    """iCal feed for calendar auto-join."""

    __tablename__ = "calendar_feeds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), default="My Calendar")
    ical_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    bot_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    auto_record: Mapped[bool] = mapped_column(Boolean, default=True)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    account: Mapped["Account"] = relationship(back_populates="calendar_feeds")


class OAuthAccount(Base):
    """Links an OAuth identity (Google / Microsoft) to an Account."""

    __tablename__ = "oauth_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)   # "google" | "microsoft"
    provider_user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    access_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    account: Mapped["Account"] = relationship(back_populates="oauth_accounts")


class WebhookDelivery(Base):
    """Persistent delivery log + retry queue for global webhook deliveries."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        # Used by the retry loop: SELECT WHERE status IN ('pending','retrying') AND next_retry_at <= now()
        Index("ix_webhook_deliveries_retry", "status", "next_retry_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    webhook_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    bot_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    event: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    response_status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    response_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    request_body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class IdempotencyKey(Base):
    """Maps an (account_id, idempotency_key) pair to the bot_id created by that request."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        # The primary lookup: account_id + key must be unique; also speeds up the dedup check
        Index("ix_idempotency_account_key", "account_id", "key", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    bot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class RetentionPolicy(Base):
    """Per-account or global data retention configuration."""

    __tablename__ = "retention_policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # NULL = global policy; non-null = per-account override
    account_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, unique=True, index=True)
    # Days to retain bot data (transcripts, analysis, recordings). -1 = keep forever.
    bot_retention_days: Mapped[int] = mapped_column(Integer, default=90)
    # Days to retain audio/video recordings specifically (may be shorter than bot data).
    recording_retention_days: Mapped[int] = mapped_column(Integer, default=30)
    # Auto-delete transcripts after N days (0 = never auto-delete transcript separately).
    transcript_retention_days: Mapped[int] = mapped_column(Integer, default=90)
    # Whether to delete PII from transcripts (replace speaker names with anonymous IDs).
    anonymize_speakers: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class KeywordAlert(Base):
    """Keyword-triggered webhook alert configuration per account."""

    __tablename__ = "keyword_alerts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    # JSON list of keyword strings to watch for (case-insensitive).
    keywords: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # Optional additional webhook URL to POST the alert to (beyond global webhooks).
    webhook_url: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Track how many times each alert has fired.
    trigger_count: Mapped[int] = mapped_column(Integer, default=0)
    last_triggered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    account: Mapped["Account"] = relationship(back_populates="keyword_alerts")


class SupportKey(Base):
    """User-generated support token shared with admin during support sessions.

    The plaintext key is NEVER stored — only its SHA-256 hex digest.
    Admin cannot derive the original key from the hash, preserving user privacy.
    The user must explicitly share the key to authorise a support session.
    """

    __tablename__ = "support_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(100), default="Support Key")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    account: Mapped["Account"] = relationship(back_populates="support_keys")


class Workspace(Base):
    """Team workspace — shared context for multiple accounts/members."""

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    owner_account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"), nullable=False, index=True)
    # JSON settings: default_bot_name, require_consent, etc.
    settings: Mapped[str] = mapped_column(Text, default="{}")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    members: Mapped[list["WorkspaceMember"]] = relationship(back_populates="workspace", cascade="all, delete-orphan")


class WorkspaceMember(Base):
    """Membership record linking an account to a workspace with a role."""

    __tablename__ = "workspace_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspaces.id"), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"), nullable=False, index=True)
    # "admin" | "member" | "viewer"
    role: Mapped[str] = mapped_column(String(20), default="member", nullable=False)
    invited_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    workspace: Mapped["Workspace"] = relationship(back_populates="members")


class SamlConfig(Base):
    """SAML 2.0 SSO configuration for enterprise workspaces."""

    __tablename__ = "saml_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Organisation slug (used in the SSO URL: /auth/saml/{org_slug}/authorize)
    org_slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    # Display name shown on login page
    org_name: Mapped[str] = mapped_column(String(100), default="")
    # SAML IdP metadata URL (for auto-discovery) OR raw XML (stored in metadata_xml)
    idp_metadata_url: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    idp_metadata_xml: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # SP entity ID and ACS URL are derived from org_slug at runtime
    # Attribute mapping — JSON: {"email": "...", "first_name": "...", "last_name": "..."}
    attribute_mapping: Mapped[str] = mapped_column(Text, default='{"email": "email"}')
    # Workspace to add new SSO users to automatically
    workspace_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    # Default role for new SSO-provisioned users
    default_role: Mapped[str] = mapped_column(String(20), default="member")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ActionItem(Base):
    """First-class action item extracted from a meeting, with lifecycle tracking."""
    __tablename__ = "action_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    account_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    sub_user_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    bot_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # Stable content hash so upsert is idempotent: sha256(bot_id + task.lower().strip())
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    task: Mapped[str] = mapped_column(Text, nullable=False)
    assignee: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    due_date: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Numeric(3, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)  # open | done
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class MeetingSummary(Base):
    """Lightweight permanent record of each meeting — survives beyond BotSnapshot TTL.
    Used for longitudinal analytics: trends, topic frequency, sentiment over time.
    """

    __tablename__ = "meeting_summaries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    bot_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    meeting_url: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    platform: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    participant_count: Mapped[int] = mapped_column(Integer, default=0)
    sentiment: Mapped[Optional[float]] = mapped_column(Numeric(3, 2), nullable=True)
    health_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    topics: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON list of topic strings
    template: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    ai_cost_usd: Mapped[Optional[float]] = mapped_column(Numeric(10, 6), nullable=True)
    transcript_word_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
