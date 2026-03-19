"""In-memory store for bot sessions and webhooks.

Terminal bots (done/error/cancelled) are also persisted to SQLite so they
survive server restarts.  Active bots are still RAM-only.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

BOT_TTL_HOURS = 24  # how long to keep completed bots in memory


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Bot session ────────────────────────────────────────────────────────────────

@dataclass
class BotSession:
    id: str
    meeting_url: str
    meeting_platform: str
    bot_name: str
    status: str  # ready | scheduled | queued | joining | in_call | call_ended | transcribing | done | error | cancelled

    # Per-bot webhook — called once when bot reaches a terminal state
    webhook_url: Optional[str] = None

    # Results (populated during/after the meeting)
    transcript: list = field(default_factory=list)
    analysis: Optional[dict] = None
    chapters: list = field(default_factory=list)
    speaker_stats: list = field(default_factory=list)
    participants: list = field(default_factory=list)
    recording_path: Optional[str] = None
    video_path: Optional[str] = None
    error_message: Optional[str] = None

    # Bot configuration
    analysis_mode: str = "full"
    template: Optional[str] = None
    prompt_override: Optional[str] = None
    vocabulary: list = field(default_factory=list)
    respond_on_mention: bool = True
    mention_response_mode: str = "text"
    tts_provider: str = "edge"
    start_muted: bool = False
    live_transcription: bool = False
    join_at: Optional[datetime] = None
    metadata: dict = field(default_factory=dict)
    is_demo_transcript: bool = False

    # Bot persona / white-label
    bot_avatar_url: Optional[str] = None

    # Video recording
    record_video: bool = False

    # Owning account (None = unauthenticated / superadmin mode)
    account_id: Optional[str] = None

    # For business accounts: isolates data per end-user within the account
    sub_user_id: Optional[str] = None

    # ── Consent / opt-out ──────────────────────────────────────────────────────
    consent_enabled: bool = False       # announce recording and honour opt-out requests
    consent_message: Optional[str] = None   # custom consent announcement text
    opted_out_participants: list = field(default_factory=list)  # names of participants who opted out

    # ── Keyword alerts ─────────────────────────────────────────────────────────
    # Per-bot keyword list — supplements account-level KeywordAlert rules.
    keyword_alerts: list = field(default_factory=list)  # [{"keyword": str, "webhook_url": str|None}]

    # ── Follow-up email ────────────────────────────────────────────────────────
    auto_followup_email: bool = False   # auto-generate & send follow-up email on completion
    followup_email: Optional[dict] = None  # {"subject": ..., "body": ...} cached draft

    # ── Workspace ──────────────────────────────────────────────────────────────
    workspace_id: Optional[str] = None

    # ── Transcription provider ─────────────────────────────────────────────────
    transcription_provider: str = "gemini"  # "gemini" | "whisper"

    # ── Real-time intelligence ─────────────────────────────────────────────────
    translation_language: Optional[str] = None  # BCP-47 (e.g. "es", "fr") for live translation

    # ── PII detection & redaction ──────────────────────────────────────────────
    pii_redaction: bool = False   # redact PII from transcript before analysis
    pii_detected: bool = False    # True if PII was found (set after transcription)

    # ── Meeting intelligence (set at completion) ───────────────────────────────
    health_score: Optional[int] = None         # 0-100 meeting quality score
    meeting_cost_usd: Optional[float] = None   # estimated attendee cost in USD
    avg_hourly_rate_usd: Optional[float] = None  # used to compute meeting cost

    # AI usage tracking
    ai_usage: list = field(default_factory=list)  # list of usage entry dicts

    # Timestamps
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None  # set when terminal state is reached

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.started_at and self.ended_at:
            s = self.started_at if self.started_at.tzinfo else self.started_at.replace(tzinfo=timezone.utc)
            e = self.ended_at if self.ended_at.tzinfo else self.ended_at.replace(tzinfo=timezone.utc)
            return round((e - s).total_seconds(), 1)
        return None

    @property
    def ai_total_tokens(self) -> int:
        return sum(r.get("total_tokens", 0) for r in self.ai_usage)

    @property
    def ai_total_cost_usd(self) -> float:
        return round(sum(r.get("cost_usd", 0.0) for r in self.ai_usage), 6)

    @property
    def ai_primary_model(self) -> Optional[str]:
        if not self.ai_usage:
            return None
        model_tokens: dict[str, int] = {}
        for r in self.ai_usage:
            m = r.get("model", "")
            model_tokens[m] = model_tokens.get(m, 0) + r.get("total_tokens", 0)
        return max(model_tokens, key=model_tokens.get) if model_tokens else None

    def recording_available(self) -> bool:
        import os
        return bool(self.recording_path and os.path.exists(self.recording_path))


# ── Webhook entry ──────────────────────────────────────────────────────────────

@dataclass
class WebhookEntry:
    id: str
    url: str
    events: list = field(default_factory=lambda: ["*"])  # ["*"] = all events
    secret: Optional[str] = None
    is_active: bool = True
    created_at: datetime = field(default_factory=_now)
    delivery_attempts: int = 0
    last_delivery_at: Optional[datetime] = None
    last_delivery_status: Optional[int] = None
    consecutive_failures: int = 0


# ── Store ─────────────────────────────────────────────────────────────────────

class Store:
    """Thread-safe in-memory store for bots and webhooks."""

    def __init__(self) -> None:
        self._bots: dict[str, BotSession] = {}
        self._webhooks: dict[str, WebhookEntry] = {}
        self._lock = asyncio.Lock()

    # ── Bots ──────────────────────────────────────────────────────────────────

    async def create_bot(self, session: BotSession) -> None:
        async with self._lock:
            self._bots[session.id] = session

    async def get_bot(self, bot_id: str) -> Optional[BotSession]:
        async with self._lock:
            return self._bots.get(bot_id)

    async def update_bot(self, bot_id: str, **kwargs) -> Optional[BotSession]:
        async with self._lock:
            bot = self._bots.get(bot_id)
            if bot is None:
                return None
            for k, v in kwargs.items():
                setattr(bot, k, v)
            bot.updated_at = _now()
            return bot

    async def list_bots(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        account_id: Optional[str] = None,
        sub_user_id: Optional[str] = None,
    ) -> tuple[list[BotSession], int]:
        async with self._lock:
            snapshot = list(self._bots.values())
        # Filter first (O(n)), then sort only the smaller result set
        filtered = snapshot
        if status:
            filtered = [b for b in filtered if b.status == status]
        if account_id:
            filtered = [b for b in filtered if b.account_id == account_id]
        if sub_user_id is not None:
            filtered = [b for b in filtered if b.sub_user_id == sub_user_id]
        bots = sorted(filtered, key=lambda b: b.created_at, reverse=True)
        total = len(bots)
        return bots[offset : offset + limit], total

    async def delete_bot(self, bot_id: str) -> None:
        async with self._lock:
            self._bots.pop(bot_id, None)

    async def mark_terminal(self, bot_id: str, status: str, **kwargs) -> Optional[BotSession]:
        """Set terminal status, schedule TTL expiry, and persist to SQLite."""
        kwargs["expires_at"] = _now() + timedelta(hours=BOT_TTL_HOURS)
        bot = await self.update_bot(bot_id, status=status, **kwargs)
        if bot is not None:
            await self._persist_bot(bot)
        return bot

    async def _persist_bot(self, bot: "BotSession") -> None:
        """Upsert a bot snapshot into the database (best-effort)."""
        try:
            from app.db import AsyncSessionLocal
            from app.models.account import BotSnapshot
            from sqlalchemy import select as _select

            def _dt(v):
                if v is None:
                    return None
                if isinstance(v, datetime):
                    return v.isoformat()
                return str(v)

            # Snapshot all fields while holding the lock to prevent concurrent mutation
            async with self._lock:
                bot_id = bot.id
                bot_account_id = bot.account_id
                bot_sub_user_id = bot.sub_user_id
                bot_status = bot.status
                bot_meeting_url = bot.meeting_url
                bot_created_at = bot.created_at
                bot_expires_at = bot.expires_at
                data = json.dumps({
                    "id": bot.id,
                    "meeting_url": bot.meeting_url,
                    "meeting_platform": bot.meeting_platform,
                    "bot_name": bot.bot_name,
                    "status": bot.status,
                    "webhook_url": bot.webhook_url,
                    "transcript": bot.transcript,
                    "analysis": bot.analysis,
                    "chapters": bot.chapters,
                    "speaker_stats": bot.speaker_stats,
                    "participants": bot.participants,
                    "recording_path": bot.recording_path,
                    "video_path": bot.video_path,
                    "error_message": bot.error_message,
                    "analysis_mode": bot.analysis_mode,
                    "template": bot.template,
                    "prompt_override": bot.prompt_override,
                    "vocabulary": bot.vocabulary,
                    "respond_on_mention": bot.respond_on_mention,
                    "mention_response_mode": bot.mention_response_mode,
                    "tts_provider": bot.tts_provider,
                    "start_muted": bot.start_muted,
                    "live_transcription": bot.live_transcription,
                    "join_at": _dt(bot.join_at),
                    "metadata": bot.metadata,
                    "is_demo_transcript": bot.is_demo_transcript,
                    "bot_avatar_url": bot.bot_avatar_url,
                    "record_video": bot.record_video,
                    "account_id": bot.account_id,
                    "sub_user_id": bot.sub_user_id,
                    "consent_enabled": bot.consent_enabled,
                    "consent_message": bot.consent_message,
                    "opted_out_participants": bot.opted_out_participants,
                    "keyword_alerts": bot.keyword_alerts,
                    "auto_followup_email": bot.auto_followup_email,
                    "followup_email": bot.followup_email,
                    "workspace_id": bot.workspace_id,
                    "transcription_provider": bot.transcription_provider,
                    "translation_language": bot.translation_language,
                    "pii_redaction": bot.pii_redaction,
                    "pii_detected": bot.pii_detected,
                    "health_score": bot.health_score,
                    "meeting_cost_usd": bot.meeting_cost_usd,
                    "avg_hourly_rate_usd": bot.avg_hourly_rate_usd,
                    "ai_usage": bot.ai_usage,
                    "created_at": _dt(bot.created_at),
                    "updated_at": _dt(bot.updated_at),
                    "started_at": _dt(bot.started_at),
                    "ended_at": _dt(bot.ended_at),
                    "expires_at": _dt(bot.expires_at),
                })

            # DB I/O happens outside the lock
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    _select(BotSnapshot).where(BotSnapshot.id == bot_id)
                )
                snap = result.scalar_one_or_none()
                if snap is None:
                    snap = BotSnapshot(
                        id=bot_id,
                        account_id=bot_account_id,
                        sub_user_id=bot_sub_user_id,
                        status=bot_status,
                        meeting_url=bot_meeting_url,
                        created_at=bot_created_at,
                        expires_at=bot_expires_at,
                        data=data,
                    )
                    db.add(snap)
                else:
                    snap.status = bot_status
                    snap.expires_at = bot_expires_at
                    snap.data = data
                await db.commit()
        except Exception:
            logger.exception("Failed to persist bot %s to database", bot.id)

    # ── Webhooks ──────────────────────────────────────────────────────────────

    async def new_webhook(self, url: str, events: list[str], secret: Optional[str] = None) -> WebhookEntry:
        wh = WebhookEntry(id=str(uuid.uuid4()), url=url, events=events, secret=secret)
        async with self._lock:
            self._webhooks[wh.id] = wh
        await self._persist_webhook(wh)
        return wh

    def get_webhook(self, webhook_id: str) -> Optional[WebhookEntry]:
        return self._webhooks.get(webhook_id)

    def list_webhooks(self) -> list[WebhookEntry]:
        return sorted(self._webhooks.values(), key=lambda w: w.created_at, reverse=True)

    async def delete_webhook(self, webhook_id: str) -> bool:
        async with self._lock:
            removed = self._webhooks.pop(webhook_id, None) is not None
        if removed:
            await self._delete_webhook_from_db(webhook_id)
        return removed

    def active_webhooks(self) -> list[WebhookEntry]:
        return [w for w in self._webhooks.values() if w.is_active]

    async def _persist_webhook(self, wh: "WebhookEntry") -> None:
        """Upsert a webhook into the database (best-effort)."""
        try:
            from app.db import AsyncSessionLocal
            from app.models.account import Webhook as WebhookModel
            from sqlalchemy import select as _select

            async with AsyncSessionLocal() as db:
                result = await db.execute(_select(WebhookModel).where(WebhookModel.id == wh.id))
                row = result.scalar_one_or_none()
                if row is None:
                    row = WebhookModel(
                        id=wh.id,
                        url=wh.url,
                        events=json.dumps(wh.events),
                        secret=wh.secret,
                        is_active=wh.is_active,
                        created_at=wh.created_at,
                        delivery_attempts=wh.delivery_attempts,
                        last_delivery_at=wh.last_delivery_at,
                        last_delivery_status=wh.last_delivery_status,
                        consecutive_failures=wh.consecutive_failures,
                    )
                    db.add(row)
                else:
                    row.url = wh.url
                    row.events = json.dumps(wh.events)
                    row.secret = wh.secret
                    row.is_active = wh.is_active
                    row.delivery_attempts = wh.delivery_attempts
                    row.last_delivery_at = wh.last_delivery_at
                    row.last_delivery_status = wh.last_delivery_status
                    row.consecutive_failures = wh.consecutive_failures
                await db.commit()
        except Exception:
            logger.exception("Failed to persist webhook %s to database", wh.id)

    async def _delete_webhook_from_db(self, webhook_id: str) -> None:
        """Delete a webhook from the database (best-effort)."""
        try:
            from app.db import AsyncSessionLocal
            from app.models.account import Webhook as WebhookModel
            from sqlalchemy import delete as _delete

            async with AsyncSessionLocal() as db:
                await db.execute(_delete(WebhookModel).where(WebhookModel.id == webhook_id))
                await db.commit()
        except Exception:
            logger.exception("Failed to delete webhook %s from database", webhook_id)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    async def cleanup_expired(self) -> int:
        """Remove expired bots (and their recording files) from memory and DB."""
        import os
        now = _now()
        async with self._lock:
            expired = [
                bot_id
                for bot_id, bot in self._bots.items()
                if bot.expires_at and bot.expires_at < now
            ]
            for bot_id in expired:
                bot = self._bots.pop(bot_id)
                if bot.recording_path and os.path.exists(bot.recording_path):
                    try:
                        os.remove(bot.recording_path)
                        logger.debug("Deleted recording for expired bot %s", bot_id)
                    except Exception as exc:
                        logger.warning("Could not delete recording %s: %s", bot.recording_path, exc)
                if bot.video_path and os.path.exists(bot.video_path):
                    try:
                        os.remove(bot.video_path)
                        logger.debug("Deleted video for expired bot %s", bot_id)
                    except Exception as exc:
                        logger.warning("Could not delete video %s: %s", bot.video_path, exc)

        if expired:
            # Purge expired snapshots from DB too
            try:
                from app.db import AsyncSessionLocal
                from app.models.account import BotSnapshot
                from sqlalchemy import delete as _delete
                async with AsyncSessionLocal() as db:
                    await db.execute(
                        _delete(BotSnapshot).where(BotSnapshot.expires_at < now)
                    )
                    await db.commit()
            except Exception:
                logger.exception("Failed to purge expired bot snapshots from database")

            logger.info("Cleaned up %d expired bot(s) from memory", len(expired))
        return len(expired)


async def load_persisted_bots() -> int:
    """Load non-expired bot snapshots from SQLite into the in-memory store.

    Called once at startup so terminal bots survive server restarts.
    Returns the number of bots loaded.
    """
    try:
        from app.db import AsyncSessionLocal
        from app.models.account import BotSnapshot
        from sqlalchemy import select as _select

        def _parse_dt(v):
            if v is None:
                return None
            if isinstance(v, datetime):
                return v
            try:
                dt = datetime.fromisoformat(v)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                return None

        now = _now()
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                _select(BotSnapshot).where(
                    (BotSnapshot.expires_at > now) | (BotSnapshot.expires_at.is_(None))
                )
            )
            snapshots = result.scalars().all()

        count = 0
        for snap in snapshots:
            try:
                d = json.loads(snap.data)
                bot = BotSession(
                    id=d["id"],
                    meeting_url=d["meeting_url"],
                    meeting_platform=d.get("meeting_platform", "unknown"),
                    bot_name=d.get("bot_name", "JustHereToListen.io"),
                    status=d.get("status", "done"),
                    webhook_url=d.get("webhook_url"),
                    transcript=d.get("transcript", []),
                    analysis=d.get("analysis"),
                    chapters=d.get("chapters", []),
                    speaker_stats=d.get("speaker_stats", []),
                    participants=d.get("participants", []),
                    recording_path=d.get("recording_path"),
                    video_path=d.get("video_path"),
                    error_message=d.get("error_message"),
                    analysis_mode=d.get("analysis_mode", "full"),
                    template=d.get("template"),
                    prompt_override=d.get("prompt_override"),
                    vocabulary=d.get("vocabulary", []),
                    respond_on_mention=d.get("respond_on_mention", True),
                    mention_response_mode=d.get("mention_response_mode", "text"),
                    tts_provider=d.get("tts_provider", "edge"),
                    start_muted=d.get("start_muted", False),
                    live_transcription=d.get("live_transcription", False),
                    join_at=_parse_dt(d.get("join_at")),
                    metadata=d.get("metadata", {}),
                    is_demo_transcript=d.get("is_demo_transcript", False),
                    bot_avatar_url=d.get("bot_avatar_url"),
                    record_video=d.get("record_video", False),
                    account_id=d.get("account_id"),
                    sub_user_id=d.get("sub_user_id"),
                    consent_enabled=d.get("consent_enabled", False),
                    consent_message=d.get("consent_message"),
                    opted_out_participants=d.get("opted_out_participants", []),
                    keyword_alerts=d.get("keyword_alerts", []),
                    auto_followup_email=d.get("auto_followup_email", False),
                    followup_email=d.get("followup_email"),
                    workspace_id=d.get("workspace_id"),
                    transcription_provider=d.get("transcription_provider", "gemini"),
                    translation_language=d.get("translation_language"),
                    pii_redaction=d.get("pii_redaction", False),
                    pii_detected=d.get("pii_detected", False),
                    health_score=d.get("health_score"),
                    meeting_cost_usd=d.get("meeting_cost_usd"),
                    avg_hourly_rate_usd=d.get("avg_hourly_rate_usd"),
                    ai_usage=d.get("ai_usage", []),
                    created_at=_parse_dt(d.get("created_at")) or now,
                    updated_at=_parse_dt(d.get("updated_at")) or now,
                    started_at=_parse_dt(d.get("started_at")),
                    ended_at=_parse_dt(d.get("ended_at")),
                    expires_at=_parse_dt(d.get("expires_at")),
                )
                await store.create_bot(bot)
                count += 1
            except Exception:
                logger.exception("Failed to restore bot snapshot %s", snap.id)

        return count
    except Exception:
        logger.exception("Failed to load persisted bots from database")
        return 0


async def load_persisted_webhooks() -> int:
    """Load webhook registrations from the database into the in-memory store.

    Called once at startup so webhook configs survive server restarts.
    Returns the number of webhooks loaded.
    """
    try:
        from app.db import AsyncSessionLocal
        from app.models.account import Webhook as WebhookModel
        from sqlalchemy import select as _select

        async with AsyncSessionLocal() as db:
            result = await db.execute(_select(WebhookModel))
            rows = result.scalars().all()

        count = 0
        for row in rows:
            try:
                events = json.loads(row.events) if row.events else ["*"]
                wh = WebhookEntry(
                    id=row.id,
                    url=row.url,
                    events=events,
                    secret=row.secret,
                    is_active=row.is_active,
                    created_at=row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=timezone.utc),
                    delivery_attempts=row.delivery_attempts or 0,
                    last_delivery_at=row.last_delivery_at,
                    last_delivery_status=row.last_delivery_status,
                    consecutive_failures=row.consecutive_failures or 0,
                )
                store._webhooks[wh.id] = wh
                count += 1
            except Exception:
                logger.exception("Failed to restore webhook %s", row.id)

        return count
    except Exception:
        logger.exception("Failed to load persisted webhooks from database")
        return 0


# Module-level singleton
store = Store()
