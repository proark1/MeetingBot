"""In-memory store for bot sessions and webhooks.

No database — all state lives here during the process lifetime.
Completed bots are kept for BOT_TTL_HOURS then cleaned up automatically.
"""

import asyncio
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

    # Owning account (None = unauthenticated / superadmin mode)
    account_id: Optional[str] = None

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
    ) -> tuple[list[BotSession], int]:
        bots = sorted(self._bots.values(), key=lambda b: b.created_at, reverse=True)
        if status:
            bots = [b for b in bots if b.status == status]
        if account_id:
            bots = [b for b in bots if b.account_id == account_id]
        total = len(bots)
        return bots[offset : offset + limit], total

    async def delete_bot(self, bot_id: str) -> None:
        async with self._lock:
            self._bots.pop(bot_id, None)

    async def mark_terminal(self, bot_id: str, status: str, **kwargs) -> Optional[BotSession]:
        """Set terminal status and schedule TTL expiry."""
        kwargs["expires_at"] = _now() + timedelta(hours=BOT_TTL_HOURS)
        return await self.update_bot(bot_id, status=status, **kwargs)

    # ── Webhooks ──────────────────────────────────────────────────────────────

    def new_webhook(self, url: str, events: list[str], secret: Optional[str] = None) -> WebhookEntry:
        wh = WebhookEntry(id=str(uuid.uuid4()), url=url, events=events, secret=secret)
        self._webhooks[wh.id] = wh
        return wh

    def get_webhook(self, webhook_id: str) -> Optional[WebhookEntry]:
        return self._webhooks.get(webhook_id)

    def list_webhooks(self) -> list[WebhookEntry]:
        return sorted(self._webhooks.values(), key=lambda w: w.created_at, reverse=True)

    def delete_webhook(self, webhook_id: str) -> bool:
        return self._webhooks.pop(webhook_id, None) is not None

    def active_webhooks(self) -> list[WebhookEntry]:
        return [w for w in self._webhooks.values() if w.is_active]

    # ── Cleanup ───────────────────────────────────────────────────────────────

    async def cleanup_expired(self) -> int:
        """Remove expired bots (and their recording files) from memory."""
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
        if expired:
            logger.info("Cleaned up %d expired bot(s) from memory", len(expired))
        return len(expired)


# Module-level singleton
store = Store()
