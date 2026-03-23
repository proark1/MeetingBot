"""Admin API — platform configuration management (wallet address, etc.)."""

import hashlib
import json
import logging
import re
import time as _time
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi as _fastapi_get_openapi
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import require_admin
from app._limiter import limiter as _limiter
from app.models.account import Account, MonitorState, PlatformConfig, UnmatchedUsdcTransfer

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["Admin"], dependencies=[Depends(require_admin)])

_ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Platform config keys
WALLET_KEY = "usdc_collection_wallet"
RPC_URL_KEY = "crypto_rpc_url"


# ── Schemas ───────────────────────────────────────────────────────────────────

class WalletResponse(BaseModel):
    wallet_address: Optional[str] = Field(
        description="The platform USDC collection wallet address (ERC-20). Null if not set."
    )


class WalletUpdateRequest(BaseModel):
    wallet_address: str = Field(
        description="Ethereum address (0x..., 42 characters) where users send USDC.",
        examples=["0xAbCdEf0123456789AbCdEf0123456789AbCdEf01"],
    )


class PlatformConfigItem(BaseModel):
    key: str
    value: str
    updated_at: Optional[str] = None


class PlatformConfigResponse(BaseModel):
    configs: list[PlatformConfigItem]


class ManualCreditRequest(BaseModel):
    email: str = Field(description="Email of the account to credit.")
    amount_usd: float = Field(gt=0, description="Amount in USD to credit (must be positive).")
    note: Optional[str] = Field(None, description="Optional admin note recorded in the transaction description.")


class ManualCreditResponse(BaseModel):
    account_id: str
    email: str
    credited_usd: float
    new_balance_usd: float


class RescanRequest(BaseModel):
    from_block: int = Field(ge=0, description="Block number to start rescanning from.")


class RescanResponse(BaseModel):
    from_block: int
    message: str


class UnmatchedTransferItem(BaseModel):
    tx_hash: str = Field(description="Ethereum transaction hash.")
    from_address: str = Field(description="Sender's Ethereum address (unrecognized — not registered on any account).")
    to_address: str = Field(description="Platform wallet address that received the USDC.")
    amount_usdc: float = Field(description="Amount of USDC sent.")
    block_number: int = Field(description="Ethereum block number of the transfer.")
    detected_at: str = Field(description="ISO-8601 UTC timestamp when the monitor detected the transfer.")
    resolved: bool = Field(description="True if an admin has manually credited the account.")
    resolution_note: Optional[str] = Field(default=None, description="Admin note set when resolving.")


class UnmatchedTransferListResponse(BaseModel):
    transfers: list[UnmatchedTransferItem]
    total: int


class ResolveUnmatchedRequest(BaseModel):
    note: Optional[str] = Field(None, description="Optional note describing the resolution action taken.")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/wallet", response_model=WalletResponse)
async def get_platform_wallet(
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """Get the current platform USDC collection wallet address."""
    result = await db.execute(
        select(PlatformConfig).where(PlatformConfig.key == WALLET_KEY)
    )
    config = result.scalar_one_or_none()
    return WalletResponse(wallet_address=config.value if config else None)


@router.put("/wallet", response_model=WalletResponse)
@_limiter.limit("10/minute")
async def set_platform_wallet(
    request: Request,
    payload: WalletUpdateRequest,
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """Set or update the platform USDC collection wallet address."""
    address = payload.wallet_address.strip()

    if not _ETH_ADDRESS_RE.match(address):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid Ethereum address. Must be 0x followed by 40 hex characters.",
        )

    result = await db.execute(
        select(PlatformConfig).where(PlatformConfig.key == WALLET_KEY)
    )
    config = result.scalar_one_or_none()

    if config:
        config.value = address
    else:
        config = PlatformConfig(key=WALLET_KEY, value=address)
        db.add(config)

    await db.commit()
    logger.info("Platform USDC wallet updated to %s", address)
    return WalletResponse(wallet_address=address)


@router.get("/config", response_model=PlatformConfigResponse)
async def get_all_platform_config(
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """Get all platform configuration values."""
    result = await db.execute(select(PlatformConfig))
    configs = result.scalars().all()
    return PlatformConfigResponse(
        configs=[
            PlatformConfigItem(
                key=c.key,
                value=c.value,
                updated_at=c.updated_at.isoformat() if c.updated_at else None,
            )
            for c in configs
        ]
    )


@router.post("/credit", response_model=ManualCreditResponse)
@_limiter.limit("10/minute")
async def manual_credit(
    request: Request,
    payload: ManualCreditRequest,
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """
    Manually credit a user's account.

    Use this to correct missed USDC deposits that the monitor failed to detect
    (e.g. the user sent from an unregistered wallet, or the monitor was not
    running when the on-chain transfer occurred).
    """
    from app.services.credit_service import add_credits

    result = await db.execute(select(Account).where(Account.email == payload.email))
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found.")

    note = payload.note or "Manual admin credit"
    new_balance = await add_credits(
        account_id=account.id,
        amount_usd=Decimal(str(payload.amount_usd)),
        type="usdc_topup",
        description=f"Admin manual credit: {note}",
        reference_id=None,
        db=db,
    )
    logger.info(
        "Admin manually credited $%.4f to account %s (%s). Note: %s",
        payload.amount_usd, account.id, account.email, note,
    )
    return ManualCreditResponse(
        account_id=account.id,
        email=account.email,
        credited_usd=float(payload.amount_usd),
        new_balance_usd=float(new_balance),
    )


class RpcUrlRequest(BaseModel):
    rpc_url: str = Field(
        description="Ethereum JSON-RPC endpoint URL (Infura, Alchemy, QuickNode, etc.).",
        examples=["https://mainnet.infura.io/v3/YOUR_KEY"],
    )


class RpcUrlResponse(BaseModel):
    rpc_url_set: bool = Field(description="True if an RPC URL is now configured.")
    source: str = Field(description="'env' if from environment variable, 'db' if set via admin panel.")


@router.get("/rpc-url", response_model=RpcUrlResponse)
async def get_rpc_url_status(
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """Check whether a CRYPTO_RPC_URL is configured (env var or admin-set)."""
    from app.config import settings
    if settings.CRYPTO_RPC_URL:
        return RpcUrlResponse(rpc_url_set=True, source="env")
    result = await db.execute(
        select(PlatformConfig).where(PlatformConfig.key == RPC_URL_KEY)
    )
    config = result.scalar_one_or_none()
    return RpcUrlResponse(
        rpc_url_set=bool(config and config.value),
        source="db" if config and config.value else "none",
    )


@router.put("/rpc-url", response_model=RpcUrlResponse)
@_limiter.limit("10/minute")
async def set_rpc_url(
    request: Request,
    payload: RpcUrlRequest,
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """
    Set the Ethereum RPC URL used by the USDC monitor.

    This stores the URL in the database so the monitor can use it without
    requiring a server restart or environment variable change.
    The environment variable `CRYPTO_RPC_URL` always takes precedence if set.
    """
    url = payload.rpc_url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="RPC URL must start with http:// or https://",
        )

    from app.services.crypto_service import test_rpc_url
    ok, reason = await test_rpc_url(url)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"RPC URL validation failed: {reason}",
        )

    result = await db.execute(
        select(PlatformConfig).where(PlatformConfig.key == RPC_URL_KEY)
    )
    config = result.scalar_one_or_none()
    if config:
        config.value = url
    else:
        config = PlatformConfig(key=RPC_URL_KEY, value=url)
        db.add(config)
    await db.commit()
    logger.info("Admin set CRYPTO_RPC_URL via admin panel (stored in DB)")
    return RpcUrlResponse(rpc_url_set=True, source="db")


@router.get("/usdc/unmatched", response_model=UnmatchedTransferListResponse)
async def list_unmatched_transfers(
    resolved: Optional[bool] = None,
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """
    List USDC transfers that arrived at the platform wallet but could not be
    attributed to any user account because the sender's wallet was not registered.

    These represent funds that were received on-chain but have not yet been credited
    to any user balance. To resolve:

    1. Identify the user by their `from_address` (ask them which wallet they used).
    2. Credit their account via `POST /admin/credit`.
    3. Mark the transfer resolved via `POST /admin/usdc/unmatched/{tx_hash}/resolve`.

    Use `?resolved=false` (default) to see only pending items, `?resolved=true` for
    resolved ones, or omit the parameter to see all.
    """
    from sqlalchemy import desc

    query = select(UnmatchedUsdcTransfer)
    if resolved is not None:
        query = query.where(UnmatchedUsdcTransfer.resolved == resolved)
    query = query.order_by(desc(UnmatchedUsdcTransfer.detected_at))

    result = await db.execute(query)
    transfers = result.scalars().all()

    return UnmatchedTransferListResponse(
        transfers=[
            UnmatchedTransferItem(
                tx_hash=t.tx_hash,
                from_address=t.from_address,
                to_address=t.to_address,
                amount_usdc=float(t.amount_usdc),
                block_number=t.block_number,
                detected_at=t.detected_at.isoformat(),
                resolved=t.resolved,
                resolution_note=t.resolution_note,
            )
            for t in transfers
        ],
        total=len(transfers),
    )


@router.post("/usdc/unmatched/{tx_hash}/resolve", response_model=UnmatchedTransferItem)
async def resolve_unmatched_transfer(
    tx_hash: str,
    payload: ResolveUnmatchedRequest,
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """
    Mark an unmatched USDC transfer as resolved after the account has been manually credited.

    Call this after using `POST /admin/credit` to apply the funds to the correct account.
    """
    result = await db.execute(
        select(UnmatchedUsdcTransfer).where(UnmatchedUsdcTransfer.tx_hash == tx_hash)
    )
    transfer = result.scalar_one_or_none()
    if transfer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found.")

    transfer.resolved = True
    transfer.resolution_note = payload.note or "Resolved by admin"
    await db.commit()
    logger.info("Admin marked unmatched USDC transfer %s as resolved. Note: %s", tx_hash, transfer.resolution_note)

    return UnmatchedTransferItem(
        tx_hash=transfer.tx_hash,
        from_address=transfer.from_address,
        to_address=transfer.to_address,
        amount_usdc=float(transfer.amount_usdc),
        block_number=transfer.block_number,
        detected_at=transfer.detected_at.isoformat(),
        resolved=transfer.resolved,
        resolution_note=transfer.resolution_note,
    )


@router.post("/usdc/rescan", response_model=RescanResponse)
@_limiter.limit("5/minute")
async def rescan_usdc_from_block(
    request: Request,
    payload: RescanRequest,
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """
    Reset the USDC monitor's last-processed block so it will rescan from a
    given block on its next cycle.

    Use this when a USDC deposit was missed because the monitor had already
    advanced past that block (e.g. the user sent from an unregistered wallet
    and later registered it).  After calling this endpoint the monitor will
    re-examine all Transfer events starting at `from_block` on the next 60-second
    tick; duplicate transactions are skipped automatically via idempotency checks.
    """
    result = await db.execute(
        select(MonitorState).where(MonitorState.key == "usdc_last_block")
    )
    state = result.scalar_one_or_none()

    # Set last processed block to one before the desired start so the monitor
    # picks up from_block inclusive on its next run.
    new_value = str(max(0, payload.from_block - 1))
    if state is None:
        state = MonitorState(key="usdc_last_block", value=new_value)
        db.add(state)
    else:
        state.value = new_value

    await db.commit()
    logger.info("Admin reset USDC monitor last-block to %s (will rescan from block %d)", new_value, payload.from_block)
    return RescanResponse(
        from_block=payload.from_block,
        message=f"Monitor will rescan USDC transfers starting from block {payload.from_block} on the next cycle (≤60 s).",
    )


# ── Account type management ───────────────────────────────────────────────────

class SetAccountTypeRequest(BaseModel):
    account_type: str = Field(
        description="Account type to set: `personal` or `business`.",
        examples=["business"],
    )


class SetAccountTypeResponse(BaseModel):
    account_id: str
    email: str
    account_type: str = Field(description="New account type.")
    message: str


@router.post("/accounts/{account_id}/set-account-type", response_model=SetAccountTypeResponse)
async def set_account_type(
    account_id: str,
    payload: SetAccountTypeRequest,
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """
    Set the account type (`personal` or `business`) for any user account.

    - **personal** — standard single-user account.
    - **business** — multi-tenant mode; the account can use `X-Sub-User` to
      isolate data per end-user.
    """
    if payload.account_type not in ("personal", "business"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="account_type must be 'personal' or 'business'.",
        )

    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found.")

    account.account_type = payload.account_type
    await db.commit()
    logger.info("Admin set account_type=%s for account %s (%s)", payload.account_type, account.id, account.email)
    return SetAccountTypeResponse(
        account_id=account.id,
        email=account.email,
        account_type=account.account_type,
        message=f"Account type updated to '{payload.account_type}'.",
    )


# ── Platform analytics ────────────────────────────────────────────────────────

_analytics_cache: dict = {}
_ANALYTICS_TTL = 300  # 5-minute cache


def _detect_platform(url: str) -> str:
    u = (url or "").lower()
    if "zoom.us" in u or "zoom.com" in u:
        return "Zoom"
    if "meet.google.com" in u:
        return "Google Meet"
    if "teams.microsoft.com" in u or "teams.live.com" in u:
        return "Microsoft Teams"
    if "webex.com" in u:
        return "Webex"
    if "whereby.com" in u:
        return "Whereby"
    return "Other"


@router.get("/platform-analytics", tags=["Admin"])
async def platform_analytics(
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """Aggregated platform analytics — no private user data.

    Returns counts, trends, feature-adoption rates, and AI token/cost breakdowns.
    Transcript content, meeting URLs, and analysis text are never included.
    Results are cached for 5 minutes.
    """
    cached = _analytics_cache.get("platform")
    if cached and (_time.monotonic() - cached[0]) < _ANALYTICS_TTL:
        return cached[1]

    from app.models.account import BotSnapshot
    from app.store import store as _store

    now = datetime.now(timezone.utc)
    d30 = now - timedelta(days=30)
    d7 = now - timedelta(days=7)

    # ── Account stats ──────────────────────────────────────────────────────────
    try:
        total_accounts = (await db.execute(select(func.count(Account.id)))).scalar_one()
        new_30d_accts = (await db.execute(
            select(func.count(Account.id)).where(Account.created_at >= d30)
        )).scalar_one()
        plan_counts_q = await db.execute(
            select(Account.plan, func.count(Account.id)).group_by(Account.plan)
        )
        plan_counts = {(r[0] or "free"): r[1] for r in plan_counts_q.all()}
    except Exception:
        logger.warning("Account stats query failed — using defaults", exc_info=True)
        await db.rollback()
        total_accounts, new_30d_accts, plan_counts = 0, 0, {}

    # ── Load bot snapshots (terminal bots, capped at 50k for memory safety) ───
    snaps_q = await db.execute(
        select(
            BotSnapshot.status, BotSnapshot.meeting_url,
            BotSnapshot.created_at, BotSnapshot.account_id, BotSnapshot.data
        ).order_by(BotSnapshot.created_at.desc()).limit(50000)
    )
    snaps = snaps_q.all()

    # Active / live bots from in-memory store
    active_bots, _ = await _store.list_bots(limit=10000)

    # ── Aggregate ──────────────────────────────────────────────────────────────
    status_counts: dict[str, int] = {}
    platform_counts: dict[str, int] = {}
    daily_bots: dict[str, int] = {}
    daily_errors: dict[str, int] = {}
    features: dict[str, int] = {
        "analysis_full": 0, "analysis_transcript_only": 0,
        "consent_enabled": 0, "live_transcription": 0,
        "record_video": 0, "pii_redaction": 0, "translation": 0,
        "keyword_alerts": 0, "workspace": 0, "scheduled": 0,
        "custom_template": 0, "custom_prompt": 0, "auto_followup": 0,
    }
    template_counts: dict[str, int] = {}
    transcription_counts: dict[str, int] = {}
    duration_samples: list[float] = []
    error_messages: dict[str, int] = {}
    error_by_platform: dict[str, int] = {}
    ai_by_model: dict[str, dict] = {}
    ai_by_operation: dict[str, dict] = {}
    ai_daily: dict[str, dict] = {}
    total_ai_tokens = 0
    total_ai_cost = 0.0
    user_stats: dict[str, dict] = {}

    def _inc_ai(bucket: dict, key: str, tokens: int, cost: float) -> None:
        if key not in bucket:
            bucket[key] = {"tokens": 0, "cost": 0.0, "calls": 0}
        bucket[key]["tokens"] += tokens
        bucket[key]["cost"] += cost
        bucket[key]["calls"] += 1

    for s in snaps:
        # Status
        sc = s.status or "unknown"
        status_counts[sc] = status_counts.get(sc, 0) + 1

        # Platform — derived from URL only, never expose full URL
        p = _detect_platform(s.meeting_url or "")
        platform_counts[p] = platform_counts.get(p, 0) + 1

        # Daily trend
        if s.created_at and s.created_at >= d30:
            day = s.created_at.strftime("%Y-%m-%d")
            daily_bots[day] = daily_bots.get(day, 0) + 1
            if sc == "error":
                daily_errors[day] = daily_errors.get(day, 0) + 1

        # Parse JSON blob for feature flags & AI usage
        try:
            data = json.loads(s.data) if s.data else {}
        except Exception:
            data = {}

        # Error analysis
        if sc == "error":
            short_err = (data.get("error_message") or "Unknown error")[:80]
            error_messages[short_err] = error_messages.get(short_err, 0) + 1
            error_by_platform[p] = error_by_platform.get(p, 0) + 1

        am = data.get("analysis_mode", "full")
        features["analysis_full" if am == "full" else "analysis_transcript_only"] += 1
        if data.get("consent_enabled"):       features["consent_enabled"] += 1
        if data.get("live_transcription"):    features["live_transcription"] += 1
        if data.get("record_video"):          features["record_video"] += 1
        if data.get("pii_redaction"):         features["pii_redaction"] += 1
        if data.get("translation_language"):  features["translation"] += 1
        if data.get("keyword_alerts"):        features["keyword_alerts"] += 1
        if data.get("workspace_id"):          features["workspace"] += 1
        if data.get("join_at"):               features["scheduled"] += 1
        if data.get("auto_followup_email"):   features["auto_followup"] += 1
        if data.get("template"):
            features["custom_template"] += 1
            t = data["template"]
            template_counts[t] = template_counts.get(t, 0) + 1
        if data.get("prompt_override"):       features["custom_prompt"] += 1

        tp = data.get("transcription_provider", "gemini")
        transcription_counts[tp] = transcription_counts.get(tp, 0) + 1

        dur = data.get("duration_seconds")
        if dur:
            duration_samples.append(float(dur))

        # AI usage — tokens and cost, never transcript text
        day_key = s.created_at.strftime("%Y-%m-%d") if s.created_at else None
        for u in data.get("ai_usage", []):
            model = u.get("model", "unknown")
            op    = u.get("operation", "unknown")
            toks  = u.get("total_tokens", 0)
            cost  = u.get("cost_usd", 0.0)
            total_ai_tokens += toks
            total_ai_cost   += cost
            _inc_ai(ai_by_model, model, toks, cost)
            _inc_ai(ai_by_operation, op, toks, cost)
            if day_key and s.created_at and s.created_at >= d30:
                if day_key not in ai_daily:
                    ai_daily[day_key] = {"tokens": 0, "cost": 0.0}
                ai_daily[day_key]["tokens"] += toks
                ai_daily[day_key]["cost"]   += cost

        # Per-user aggregate (email-keyed after join, no private content)
        acct_id = s.account_id
        if acct_id:
            if acct_id not in user_stats:
                user_stats[acct_id] = {
                    "total_bots": 0, "bots_30d": 0, "bots_7d": 0,
                    "last_active": None, "platform_pref": {},
                    "ai_tokens": 0, "ai_cost": 0.0, "features_used": set(),
                }
            us = user_stats[acct_id]
            us["total_bots"] += 1
            if s.created_at and s.created_at >= d30:
                us["bots_30d"] += 1
            if s.created_at and s.created_at >= d7:
                us["bots_7d"] += 1
            if not us["last_active"] or (s.created_at and s.created_at > us["last_active"]):
                us["last_active"] = s.created_at
            us["platform_pref"][p] = us["platform_pref"].get(p, 0) + 1
            for u in data.get("ai_usage", []):
                us["ai_tokens"] += u.get("total_tokens", 0)
                us["ai_cost"]   += u.get("cost_usd", 0.0)
            for flag, key in [
                ("consent_enabled", "consent"), ("live_transcription", "live_transcript"),
                ("record_video", "video"), ("pii_redaction", "pii"),
                ("translation_language", "translation"), ("workspace_id", "workspace"),
            ]:
                if data.get(flag):
                    us["features_used"].add(key)

    # Include live bots in status/platform counts
    active_statuses = {"ready", "scheduled", "queued", "joining", "in_call", "call_ended"}
    active_now = 0
    for b in active_bots:
        sc = b.status
        status_counts[sc] = status_counts.get(sc, 0) + 1
        p = _detect_platform(b.meeting_url or "")
        platform_counts[p] = platform_counts.get(p, 0) + 1
        if sc in active_statuses:
            active_now += 1

    total_bots = len(snaps) + len(active_bots)
    bots_30d = sum(1 for s in snaps if s.created_at and s.created_at >= d30) + \
               sum(1 for b in active_bots if b.created_at >= d30)
    bots_7d  = sum(1 for s in snaps if s.created_at and s.created_at >= d7) + \
               sum(1 for b in active_bots if b.created_at >= d7)

    # Success rate
    done  = status_counts.get("done", 0)
    error = status_counts.get("error", 0)
    total_terminal = done + error + status_counts.get("cancelled", 0)
    success_rate = round(100 * done / total_terminal, 1) if total_terminal > 0 else 0.0
    avg_duration = round(sum(duration_samples) / len(duration_samples), 0) if duration_samples else None

    # 30-day daily trend arrays (fill zeros)
    trend_days = []
    ai_trend   = []
    for i in range(30):
        day = (now - timedelta(days=29 - i)).strftime("%Y-%m-%d")
        trend_days.append({"date": day, "bots": daily_bots.get(day, 0), "errors": daily_errors.get(day, 0)})
        ai_day = ai_daily.get(day, {"tokens": 0, "cost": 0.0})
        ai_trend.append({"date": day, "tokens": ai_day["tokens"], "cost": round(ai_day["cost"], 4)})

    # Per-user table — fetch emails, sort by total bots
    per_user = []
    if user_stats:
        acct_rows = await db.execute(
            select(Account.id, Account.email, Account.plan)
            .where(Account.id.in_(list(user_stats.keys())))
        )
        acct_map = {r.id: {"email": r.email, "plan": r.plan or "free"} for r in acct_rows.all()}
        for acct_id, us in sorted(user_stats.items(), key=lambda x: x[1]["total_bots"], reverse=True)[:100]:
            info = acct_map.get(acct_id, {"email": f"[deleted:{acct_id[:8]}]", "plan": "unknown"})
            pref = max(us["platform_pref"], key=us["platform_pref"].get) if us["platform_pref"] else "—"
            per_user.append({
                "email": info["email"],
                "plan": info["plan"],
                "total_bots": us["total_bots"],
                "bots_30d": us["bots_30d"],
                "bots_7d": us["bots_7d"],
                "last_active": us["last_active"].strftime("%Y-%m-%d") if us["last_active"] else None,
                "platform_pref": pref,
                "ai_tokens": us["ai_tokens"],
                "ai_cost_usd": round(us["ai_cost"], 4),
                "features_used": sorted(us["features_used"]),
            })

    # ── Billing & Revenue ─────────────────────────────────────────────────────
    from app.models.account import CreditTransaction, WebhookDelivery, ActionItem

    credits_added_val = 0.0
    credits_consumed_val = 0.0
    credits_by_type: dict = {}
    daily_revenue: list = []
    try:
        credits_added_r = await db.execute(
            select(func.coalesce(func.sum(CreditTransaction.amount_usd), 0))
            .where(CreditTransaction.amount_usd > 0)
        )
        credits_added_val = float(credits_added_r.scalar_one() or 0)
        credits_consumed_r = await db.execute(
            select(func.coalesce(func.sum(CreditTransaction.amount_usd), 0))
            .where(CreditTransaction.amount_usd < 0)
        )
        credits_consumed_val = float(credits_consumed_r.scalar_one() or 0)

        credits_by_type_r = await db.execute(
            select(CreditTransaction.type, func.sum(CreditTransaction.amount_usd), func.count())
            .group_by(CreditTransaction.type)
        )
        credits_by_type = {
            r[0]: {"amount": round(float(r[1] or 0), 4), "count": r[2]}
            for r in credits_by_type_r.all()
        }

        # Use cast(... as Date) for cross-DB compatibility (works on both SQLite and PostgreSQL)
        from sqlalchemy import Date, cast
        _date_expr = cast(CreditTransaction.created_at, Date)
        daily_rev_r = await db.execute(
            select(_date_expr, func.sum(CreditTransaction.amount_usd))
            .where(CreditTransaction.created_at >= d30, CreditTransaction.amount_usd > 0)
            .group_by(_date_expr)
        )
        daily_rev_map = {str(r[0]): round(float(r[1] or 0), 4) for r in daily_rev_r.all()}
        for i in range(30):
            day = (now - timedelta(days=29 - i)).strftime("%Y-%m-%d")
            daily_revenue.append({"date": day, "amount": daily_rev_map.get(day, 0)})
    except Exception:
        logger.warning("Billing analytics query failed — using defaults", exc_info=True)
        await db.rollback()  # reset aborted transaction so subsequent queries work
        daily_revenue = [{"date": (now - timedelta(days=29 - i)).strftime("%Y-%m-%d"), "amount": 0} for i in range(30)]

    # ── Webhook Health ─────────────────────────────────────────────────────────
    delivery_by_status: dict = {}
    total_deliveries = 0
    wh_success_rate = 0.0
    recent_failures: list = []
    try:
        delivery_stats_r = await db.execute(
            select(WebhookDelivery.status, func.count()).group_by(WebhookDelivery.status)
        )
        delivery_by_status = {r[0]: r[1] for r in delivery_stats_r.all()}
        total_deliveries = sum(delivery_by_status.values())
        delivered_count = delivery_by_status.get("success", 0) + delivery_by_status.get("delivered", 0)
        wh_success_rate = round(100 * delivered_count / total_deliveries, 1) if total_deliveries > 0 else 0.0

        recent_fail_r = await db.execute(
            select(
                WebhookDelivery.event, WebhookDelivery.error_message,
                WebhookDelivery.response_status_code, WebhookDelivery.created_at,
            )
            .where(WebhookDelivery.status.in_(["failed", "retrying"]))
            .order_by(WebhookDelivery.created_at.desc())
            .limit(10)
        )
        recent_failures = [
            {
                "event": r.event,
                "error": (r.error_message or "")[:100],
                "status_code": r.response_status_code,
                "at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in recent_fail_r.all()
        ]
    except Exception:
        logger.warning("Webhook analytics query failed — using defaults", exc_info=True)
        await db.rollback()

    # ── Action Items Stats ─────────────────────────────────────────────────────
    ai_total_count = 0
    ai_open_count = 0
    ai_done_count = 0
    ai_completion = 0.0
    try:
        ai_total_count = (await db.execute(select(func.count(ActionItem.id)))).scalar_one()
        ai_open_count = (await db.execute(
            select(func.count(ActionItem.id)).where(ActionItem.status == "open")
        )).scalar_one()
        ai_done_count = (await db.execute(
            select(func.count(ActionItem.id)).where(ActionItem.status == "done")
        )).scalar_one()
        ai_completion = round(100 * ai_done_count / ai_total_count, 1) if ai_total_count > 0 else 0.0
    except Exception:
        logger.warning("Action items analytics query failed — using defaults", exc_info=True)
        await db.rollback()

    # ── System Status ──────────────────────────────────────────────────────────
    from app.api.bots import _running_tasks, _bot_queue

    result = {
        "overview": {
            "total_bots": total_bots,
            "bots_30d": bots_30d,
            "bots_7d": bots_7d,
            "active_now": active_now,
            "success_rate_pct": success_rate,
            "avg_duration_seconds": avg_duration,
            "total_accounts": total_accounts,
            "new_accounts_30d": new_30d_accts,
            "total_ai_tokens": total_ai_tokens,
            "total_ai_cost_usd": round(total_ai_cost, 4),
        },
        "trends": trend_days,
        "platforms": platform_counts,
        "status_breakdown": status_counts,
        "plan_counts": plan_counts,
        "features": features,
        "templates": template_counts,
        "transcription_providers": transcription_counts,
        "ai_usage": {
            "total_tokens": total_ai_tokens,
            "total_cost_usd": round(total_ai_cost, 4),
            "by_model": {
                k: {"tokens": v["tokens"], "cost": round(v["cost"], 4), "calls": v["calls"]}
                for k, v in sorted(ai_by_model.items(), key=lambda x: x[1]["tokens"], reverse=True)
            },
            "by_operation": {
                k: {"tokens": v["tokens"], "cost": round(v["cost"], 4), "calls": v["calls"]}
                for k, v in sorted(ai_by_operation.items(), key=lambda x: x[1]["tokens"], reverse=True)
            },
            "daily": ai_trend,
        },
        "billing": {
            "total_credits_added_usd": round(credits_added_val, 4),
            "total_credits_consumed_usd": round(credits_consumed_val, 4),
            "net_balance_usd": round(credits_added_val + credits_consumed_val, 4),
            "by_type": credits_by_type,
            "daily_revenue": daily_revenue,
        },
        "webhooks": {
            "total_deliveries": total_deliveries,
            "by_status": delivery_by_status,
            "success_rate_pct": wh_success_rate,
            "active_webhooks": len(_store.list_webhooks()),
            "recent_failures": recent_failures,
        },
        "action_items": {
            "total": ai_total_count,
            "open": ai_open_count,
            "done": ai_done_count,
            "completion_rate_pct": ai_completion,
        },
        "errors": {
            "total": status_counts.get("error", 0),
            "by_platform": error_by_platform,
            "top_messages": sorted(error_messages.items(), key=lambda x: x[1], reverse=True)[:10],
        },
        "system": {
            "running_tasks": sum(1 for t in _running_tasks.values() if not t.done()),
            "queue_depth": len(_bot_queue),
            "max_concurrent": settings.MAX_CONCURRENT_BOTS,
            "in_memory_bots": len(active_bots),
        },
        "per_user": per_user,
        "generated_at": now.isoformat(),
    }

    _analytics_cache["platform"] = (_time.monotonic(), result)
    return result


# ── Support key lookup ─────────────────────────────────────────────────────────

@router.get("/support-lookup", tags=["Admin"])
async def support_lookup(
    key: str,
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """Look up a user via their support key.

    The user generates the key in their dashboard Settings → Support Access.
    Admin enters the plaintext key here; the system verifies against the stored
    hash and returns limited account + bot activity data for that user.
    No transcript content is returned — only metadata.
    """
    from app.models.account import SupportKey

    key_hash = hashlib.sha256(key.strip().encode()).hexdigest()
    sk_result = await db.execute(
        select(SupportKey).where(
            SupportKey.key_hash == key_hash,
            SupportKey.is_active == True,  # noqa: E712
        )
    )
    sk = sk_result.scalar_one_or_none()
    if sk is None:
        raise HTTPException(status_code=404, detail="Support key not found, expired, or already revoked")

    # Check expiry
    if sk.expires_at and sk.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Support key has expired")

    # Record usage timestamp
    sk.last_used_at = datetime.now(timezone.utc)
    await db.commit()

    account = await db.get(Account, sk.account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    # Recent bots — metadata only, no transcript/analysis text
    from app.store import store as _store
    bots, _ = await _store.list_bots(limit=30, account_id=sk.account_id)

    return {
        "account_id": account.id,
        "email": account.email,
        "plan": account.plan or "free",
        "account_type": account.account_type,
        "created_at": account.created_at.isoformat(),
        "monthly_bots_used": account.monthly_bots_used or 0,
        "key_label": sk.label,
        "key_created_at": sk.created_at.isoformat(),
        "key_expires_at": sk.expires_at.isoformat() if sk.expires_at else None,
        "recent_bots": [
            {
                "id": b.id,
                "status": b.status,
                "meeting_platform": b.meeting_platform,
                "meeting_url": b.meeting_url,   # user consented by sharing key
                "bot_name": b.bot_name,
                "created_at": b.created_at.isoformat(),
                "duration_seconds": b.duration_seconds,
                "transcript_entries": len(b.transcript) if b.transcript else 0,
                "has_analysis": bool(b.analysis),
                "error_message": b.error_message,
                "ai_tokens": b.ai_total_tokens,
                "ai_cost_usd": b.ai_total_cost_usd,
            }
            for b in bots
        ],
    }


# Admin API docs are registered directly on the app in main.py (outside the
# require_admin router) so the Swagger UI can load the OpenAPI schema without
# needing the browser to send an Authorization header.
