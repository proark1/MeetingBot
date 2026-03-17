"""Web UI routes — HTML pages for account management and billing."""

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import _admin_emails
from app.models.account import (
    Account, ApiKey, CreditTransaction, PlatformConfig, MonitorState,
    UnmatchedUsdcTransfer, Integration, CalendarFeed, OAuthAccount, Webhook,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["UI"])

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_COOKIE = "mb_token"


def _get_token_from_request(request: Request) -> Optional[str]:
    return request.cookies.get(_COOKIE)


async def _get_account_from_request(request: Request, db: AsyncSession) -> Optional[Account]:
    token = _get_token_from_request(request)
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        account_id = payload.get("sub", "")
        if not account_id:
            return None
    except JWTError:
        return None
    result = await db.execute(select(Account).where(Account.id == account_id))
    return result.scalar_one_or_none()


def _flash(type: str, message: str) -> dict:
    return {"type": type, "message": message}


# ── Root ──────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        account = await _get_account_from_request(request, db)
        if account:
            return RedirectResponse("/dashboard")
    except Exception as exc:
        logger.warning("Root route DB lookup failed (serving landing page): %s", exc)
    return templates.TemplateResponse("landing.html", {"request": request})


# ── Register ──────────────────────────────────────────────────────────────────

@router.get("/register", response_class=HTMLResponse, include_in_schema=False)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {
        "request": request,
        "account": None,
        "google_sso_enabled": bool(settings.GOOGLE_CLIENT_ID),
        "microsoft_sso_enabled": bool(settings.MICROSOFT_CLIENT_ID),
    })


@router.post("/register", response_class=HTMLResponse, include_in_schema=False)
async def register_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    account_type: str = Form(default="personal"),
    db: AsyncSession = Depends(get_db),
):
    from app.api.auth import _hash_password, generate_api_key
    import uuid
    from decimal import Decimal

    # Check password length
    if len(password) < 8:
        return templates.TemplateResponse("register.html", {
            "request": request,
            "account": None,
            "flash": _flash("danger", "Password must be at least 8 characters."),
        })

    # Check email not taken
    existing = await db.execute(select(Account).where(Account.email == email))
    if existing.scalar_one_or_none():
        return templates.TemplateResponse("register.html", {
            "request": request,
            "account": None,
            "flash": _flash("danger", "Email already registered. Try logging in."),
        })

    # Validate account_type
    if account_type not in ("personal", "business"):
        account_type = "personal"

    account = Account(
        id=str(uuid.uuid4()),
        email=email,
        hashed_password=_hash_password(password),
        credits_usd=Decimal("0"),
        account_type=account_type,
    )
    db.add(account)

    api_key_value = generate_api_key()
    api_key = ApiKey(
        id=str(uuid.uuid4()),
        account_id=account.id,
        key=api_key_value,
        name="Default",
    )
    db.add(api_key)
    await db.commit()

    # Log in immediately
    from app.api.auth import _create_jwt
    token = _create_jwt(account.id)
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(_COOKIE, token, httponly=True, samesite="lax", max_age=settings.JWT_EXPIRE_HOURS * 3600)
    return response


# ── Login ─────────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {
        "request": request,
        "account": None,
        "google_sso_enabled": bool(settings.GOOGLE_CLIENT_ID),
        "microsoft_sso_enabled": bool(settings.MICROSOFT_CLIENT_ID),
    })


@router.post("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    from app.api.auth import _verify_password, _create_jwt

    result = await db.execute(select(Account).where(Account.email == email))
    account = result.scalar_one_or_none()
    if not account or not _verify_password(password, account.hashed_password):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "account": None,
            "flash": _flash("danger", "Invalid email or password."),
            "google_sso_enabled": bool(settings.GOOGLE_CLIENT_ID),
            "microsoft_sso_enabled": bool(settings.MICROSOFT_CLIENT_ID),
        })

    token = _create_jwt(account.id)
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(_COOKIE, token, httponly=True, samesite="lax", max_age=settings.JWT_EXPIRE_HOURS * 3600)
    return response


# ── Logout ────────────────────────────────────────────────────────────────────

@router.get("/logout", include_in_schema=False)
async def logout():
    response = RedirectResponse("/login")
    response.delete_cookie(_COOKIE)
    return response


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    account = await _get_account_from_request(request, db)
    if not account:
        return RedirectResponse("/login")

    keys_result = await db.execute(
        select(ApiKey)
        .where(ApiKey.account_id == account.id, ApiKey.is_active == True)  # noqa: E712
        .order_by(ApiKey.created_at.desc())
    )
    api_keys = [
        {
            "id": k.id,
            "name": k.name,
            "key_preview": k.key[:16] + "...",
            "last_used_at": k.last_used_at.strftime("%Y-%m-%d %H:%M") if k.last_used_at else None,
        }
        for k in keys_result.scalars().all()
    ]

    txns_result = await db.execute(
        select(CreditTransaction)
        .where(CreditTransaction.account_id == account.id)
        .order_by(CreditTransaction.created_at.desc())
        .limit(30)
    )
    transactions = [
        {
            "created_at": t.created_at.isoformat(),
            "type": t.type,
            "description": t.description,
            "amount_usd": float(t.amount_usd),
        }
        for t in txns_result.scalars().all()
    ]

    # Subscription plan info
    plan_limits = {
        "free": settings.PLAN_FREE_BOTS_PER_MONTH,
        "starter": settings.PLAN_STARTER_BOTS_PER_MONTH,
        "pro": settings.PLAN_PRO_BOTS_PER_MONTH,
        "business": settings.PLAN_BUSINESS_BOTS_PER_MONTH,
    }
    plan = account.plan or "free"
    plan_limit = plan_limits.get(plan, settings.PLAN_FREE_BOTS_PER_MONTH)

    # Linked SSO accounts
    oauth_result = await db.execute(
        select(OAuthAccount).where(OAuthAccount.account_id == account.id)
    )
    oauth_accounts = [
        {"provider": o.provider, "email": o.email}
        for o in oauth_result.scalars().all()
    ]

    # Active integrations
    integ_result = await db.execute(
        select(Integration).where(
            Integration.account_id == account.id, Integration.is_active == True  # noqa: E712
        )
    )
    active_integrations = [
        {"id": i.id, "type": i.type, "name": i.name}
        for i in integ_result.scalars().all()
    ]

    # Calendar feeds
    cal_result = await db.execute(
        select(CalendarFeed).where(CalendarFeed.account_id == account.id)
    )
    calendar_feeds = [
        {"id": f.id, "name": f.name, "is_active": f.is_active,
         "last_synced_at": f.last_synced_at.strftime("%Y-%m-%d %H:%M") if f.last_synced_at else None}
        for f in cal_result.scalars().all()
    ]

    # All integrations (not just active, for full management UI)
    all_integ_result = await db.execute(
        select(Integration).where(Integration.account_id == account.id)
        .order_by(Integration.created_at.desc())
    )
    all_integrations = [
        {
            "id": i.id,
            "type": i.type,
            "name": i.name,
            "is_active": i.is_active,
            "created_at": i.created_at.strftime("%Y-%m-%d"),
        }
        for i in all_integ_result.scalars().all()
    ]

    # All calendar feeds
    all_cal_result = await db.execute(
        select(CalendarFeed).where(CalendarFeed.account_id == account.id)
        .order_by(CalendarFeed.created_at.desc())
    )
    all_calendar_feeds = [
        {
            "id": f.id,
            "name": f.name,
            "is_active": f.is_active,
            "auto_record": f.auto_record,
            "bot_name": f.bot_name,
            "last_synced_at": f.last_synced_at.strftime("%Y-%m-%d %H:%M") if f.last_synced_at else None,
            "created_at": f.created_at.strftime("%Y-%m-%d"),
        }
        for f in all_cal_result.scalars().all()
    ]

    # Recent bots (from in-memory store)
    recent_bots = []
    try:
        from app.store import store as _store
        bots_list, _total = await _store.list_bots(limit=8, account_id=account.id)
        recent_bots = [
            {
                "id": b.id,
                "meeting_url": b.meeting_url,
                "meeting_platform": b.meeting_platform,
                "status": b.status,
                "created_at": b.created_at.strftime("%Y-%m-%d %H:%M"),
                "bot_name": b.bot_name,
            }
            for b in bots_list
        ]
    except Exception:
        pass

    flash = None
    if request.query_params.get("payment") == "success":
        flash = _flash("success", "Payment successful! Your credits will be added shortly.")
    if request.query_params.get("wallet") == "saved":
        flash = _flash("success", "Wallet address saved successfully.")
    if request.query_params.get("wallet") == "error":
        flash = _flash("danger", "Invalid Ethereum address. Must be 0x followed by 40 hex characters.")
    if request.query_params.get("wallet") == "taken":
        flash = _flash("danger", "This wallet address is already linked to another account.")
    if request.query_params.get("notify") == "saved":
        flash = _flash("success", "Notification preferences updated.")
    if request.query_params.get("integ_added") == "1":
        flash = _flash("success", "Integration added successfully.")
    if request.query_params.get("integ_deleted") == "1":
        flash = _flash("success", "Integration removed.")
    if request.query_params.get("integ_error"):
        err = request.query_params.get("integ_error")
        messages = {
            "invalid_slack_url": "Invalid Slack Webhook URL. Must start with https://hooks.slack.com/",
            "notion_missing_fields": "Please provide both Notion API token and Database ID.",
            "invalid_type": "Invalid integration type.",
        }
        flash = _flash("danger", messages.get(err, "Integration error."))
    if request.query_params.get("cal_added") == "1":
        flash = _flash("success", "Calendar feed added. It will sync within 5 minutes.")
    if request.query_params.get("cal_deleted") == "1":
        flash = _flash("success", "Calendar feed removed.")
    if request.query_params.get("cal_error") == "invalid_url":
        flash = _flash("danger", "Invalid iCal URL. Must start with http:// or https://")

    new_key = request.query_params.get("new_key")
    if new_key and request.query_params.get("created") == "1":
        flash = _flash("success", "API key created successfully.")

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "account": account,
        "is_admin": _is_admin(account),
        "balance": float(account.credits_usd or 0),
        "wallet_address": account.wallet_address,
        "api_keys": api_keys,
        "transactions": transactions,
        "flash": flash,
        "new_key": new_key if request.query_params.get("created") == "1" else None,
        # Plan info
        "plan": plan,
        "plan_limit": plan_limit,
        "monthly_bots_used": account.monthly_bots_used or 0,
        # Notification prefs
        "notify_on_done": account.notify_on_done,
        "notify_email": account.notify_email or "",
        # SSO
        "oauth_accounts": oauth_accounts,
        "google_sso_enabled": bool(settings.GOOGLE_CLIENT_ID),
        "microsoft_sso_enabled": bool(settings.MICROSOFT_CLIENT_ID),
        # Integrations & calendar
        "active_integrations": active_integrations,
        "all_integrations": all_integrations,
        "calendar_feeds": calendar_feeds,
        "all_calendar_feeds": all_calendar_feeds,
        # Recent bots
        "recent_bots": recent_bots,
    })


@router.post("/dashboard/keys", include_in_schema=False)
async def create_key_ui(
    request: Request,
    name: str = Form(default="New Key"),
    db: AsyncSession = Depends(get_db),
):
    account = await _get_account_from_request(request, db)
    if not account:
        return RedirectResponse("/login")

    from app.api.auth import generate_api_key
    import uuid
    key_value = generate_api_key()
    api_key = ApiKey(
        id=str(uuid.uuid4()),
        account_id=account.id,
        key=key_value,
        name=name or "New Key",
    )
    db.add(api_key)
    await db.commit()

    return RedirectResponse(f"/dashboard?created=1&new_key={key_value}", status_code=303)


@router.post("/dashboard/keys/{key_id}/revoke", include_in_schema=False)
async def revoke_key_ui(
    key_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    account = await _get_account_from_request(request, db)
    if not account:
        return RedirectResponse("/login")

    result = await db.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.account_id == account.id)
    )
    key = result.scalar_one_or_none()
    if key:
        key.is_active = False
        await db.commit()

    return RedirectResponse("/dashboard", status_code=303)


@router.post("/dashboard/wallet", include_in_schema=False)
async def save_wallet_ui(
    request: Request,
    wallet_address: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    account = await _get_account_from_request(request, db)
    if not account:
        return RedirectResponse("/login")

    import re
    # Normalize to lowercase — Ethereum addresses are case-insensitive;
    # consistent storage ensures the monitor's case-folded lookup always matches.
    address = wallet_address.strip().lower()
    if not re.match(r"^0x[0-9a-f]{40}$", address):
        return RedirectResponse("/dashboard?wallet=error", status_code=303)

    # Check uniqueness
    from app.models.account import Account as AccountModel
    existing = await db.execute(
        select(AccountModel).where(AccountModel.wallet_address == address, AccountModel.id != account.id)
    )
    if existing.scalar_one_or_none():
        return RedirectResponse("/dashboard?wallet=taken", status_code=303)

    account.wallet_address = address
    await db.commit()

    return RedirectResponse("/dashboard?wallet=saved", status_code=303)


@router.post("/dashboard/notifications", include_in_schema=False)
async def update_notifications_ui(
    request: Request,
    notify_on_done: str = Form(default=""),
    notify_email: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    account = await _get_account_from_request(request, db)
    if not account:
        return RedirectResponse("/login")

    account.notify_on_done = notify_on_done == "on"
    account.notify_email = notify_email.strip() or None
    await db.commit()
    return RedirectResponse("/dashboard?notify=saved", status_code=303)


# ── Top Up ────────────────────────────────────────────────────────────────────

@router.get("/topup", response_class=HTMLResponse, include_in_schema=False)
async def topup_page(request: Request, db: AsyncSession = Depends(get_db)):
    account = await _get_account_from_request(request, db)
    if not account:
        return RedirectResponse("/login")

    usdc_address = None
    # Check for admin-configured platform wallet first
    from app.api.admin import WALLET_KEY
    wallet_result = await db.execute(
        select(PlatformConfig).where(PlatformConfig.key == WALLET_KEY)
    )
    wallet_config = wallet_result.scalar_one_or_none()
    if wallet_config and wallet_config.value:
        usdc_address = wallet_config.value
    elif settings.CRYPTO_HD_SEED:
        try:
            from app.services.crypto_service import get_or_create_deposit_address
            usdc_address = await get_or_create_deposit_address(account.id, db)
        except Exception:
            pass

    flash = None
    if request.query_params.get("payment") == "cancelled":
        flash = _flash("warning", "Payment cancelled.")

    amounts = []
    try:
        amounts = [int(x.strip()) for x in settings.STRIPE_TOP_UP_AMOUNTS.split(",") if x.strip()]
    except ValueError:
        amounts = [10, 25, 50, 100]

    return templates.TemplateResponse("topup.html", {
        "request": request,
        "account": account,
        "is_admin": _is_admin(account),
        "amounts": amounts,
        "stripe_enabled": bool(settings.STRIPE_SECRET_KEY),
        "usdc_enabled": bool(usdc_address),
        "usdc_address": usdc_address,
        "usdc_contract": settings.USDC_CONTRACT,
        "user_wallet": account.wallet_address,
        "flash": flash,
    })


@router.post("/topup/stripe", include_in_schema=False)
async def topup_stripe_submit(
    request: Request,
    amount_usd: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    account = await _get_account_from_request(request, db)
    if not account:
        return RedirectResponse("/login")

    valid_amounts = []
    try:
        valid_amounts = [int(x.strip()) for x in settings.STRIPE_TOP_UP_AMOUNTS.split(",") if x.strip()]
    except ValueError:
        valid_amounts = [10, 25, 50, 100]

    if amount_usd not in valid_amounts:
        return RedirectResponse("/topup?error=invalid_amount", status_code=303)

    base_url = str(request.base_url).rstrip("/")
    from app.services.stripe_service import create_checkout_session
    _, checkout_url = create_checkout_session(
        account_id=account.id,
        amount_usd=amount_usd,
        success_url=f"{base_url}/dashboard?payment=success",
        cancel_url=f"{base_url}/topup?payment=cancelled",
    )
    return RedirectResponse(checkout_url, status_code=303)


# ── Admin ────────────────────────────────────────────────────────────────────

def _is_admin(account: Optional[Account]) -> bool:
    if not account:
        return False
    return account.email.lower() in _admin_emails() or account.is_admin


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_page(request: Request, db: AsyncSession = Depends(get_db)):
    account = await _get_account_from_request(request, db)
    if not _is_admin(account):
        return RedirectResponse("/dashboard")

    from app.api.admin import WALLET_KEY, RPC_URL_KEY
    from sqlalchemy import func, desc, case

    # Platform wallet
    wallet_result = await db.execute(
        select(PlatformConfig).where(PlatformConfig.key == WALLET_KEY)
    )
    wallet_config = wallet_result.scalar_one_or_none()

    # RPC URL — check env first, then DB
    rpc_url_source = "none"
    rpc_url_preview = None
    if settings.CRYPTO_RPC_URL:
        rpc_url_source = "env"
        u = settings.CRYPTO_RPC_URL
        rpc_url_preview = u[:30] + "..." if len(u) > 30 else u
    else:
        rpc_result = await db.execute(
            select(PlatformConfig).where(PlatformConfig.key == RPC_URL_KEY)
        )
        rpc_config = rpc_result.scalar_one_or_none()
        if rpc_config and rpc_config.value:
            rpc_url_source = "db"
            u = rpc_config.value
            rpc_url_preview = u[:30] + "..." if len(u) > 30 else u

    # Monitor state
    monitor_result = await db.execute(
        select(MonitorState).where(MonitorState.key == "usdc_last_block")
    )
    monitor_state = monitor_result.scalar_one_or_none()

    # Stats
    total_accounts = (await db.execute(select(func.count(Account.id)))).scalar_one()
    total_credits = (await db.execute(select(func.sum(Account.credits_usd)))).scalar_one() or 0
    total_usdc_in = (await db.execute(
        select(func.sum(CreditTransaction.amount_usd)).where(CreditTransaction.type == "usdc_topup")
    )).scalar_one() or 0
    total_stripe_in = (await db.execute(
        select(func.sum(CreditTransaction.amount_usd)).where(CreditTransaction.type == "stripe_topup")
    )).scalar_one() or 0
    total_unmatched_pending = (await db.execute(
        select(func.count(UnmatchedUsdcTransfer.tx_hash)).where(UnmatchedUsdcTransfer.resolved == False)  # noqa: E712
    )).scalar_one()

    # Plan breakdown
    plan_counts_result = await db.execute(
        select(Account.plan, func.count(Account.id))
        .group_by(Account.plan)
    )
    plan_counts = {row[0] or "free": row[1] for row in plan_counts_result.all()}

    # Webhook count
    webhook_count_result = await db.execute(
        select(func.count(Webhook.id)).where(Webhook.is_active == True)  # noqa: E712
    )
    active_webhook_count = webhook_count_result.scalar_one()

    # Integration count
    integration_count_result = await db.execute(
        select(func.count(Integration.id)).where(Integration.is_active == True)  # noqa: E712
    )
    active_integration_count = integration_count_result.scalar_one()

    # Calendar feeds count
    calendar_feed_count_result = await db.execute(
        select(func.count(CalendarFeed.id)).where(CalendarFeed.is_active == True)  # noqa: E712
    )
    active_calendar_feed_count = calendar_feed_count_result.scalar_one()

    # OAuth (SSO) linked accounts
    oauth_linked_count_result = await db.execute(
        select(func.count(OAuthAccount.id))
    )
    oauth_linked_count = oauth_linked_count_result.scalar_one()

    # All user accounts
    accounts_result = await db.execute(
        select(Account).order_by(desc(Account.credits_usd))
    )
    all_accounts = [
        {
            "id": a.id,
            "email": a.email,
            "account_type": a.account_type,
            "plan": a.plan or "free",
            "credits_usd": float(a.credits_usd or 0),
            "wallet_address": a.wallet_address,
            "is_active": a.is_active,
            "is_admin": a.is_admin,
            "monthly_bots_used": a.monthly_bots_used or 0,
            "created_at": a.created_at.strftime("%Y-%m-%d %H:%M"),
        }
        for a in accounts_result.scalars().all()
    ]

    # Recent transactions (all users, last 50)
    txns_result = await db.execute(
        select(CreditTransaction, Account.email)
        .join(Account, CreditTransaction.account_id == Account.id)
        .order_by(desc(CreditTransaction.created_at))
        .limit(50)
    )
    recent_txns = [
        {
            "created_at": t.created_at.strftime("%Y-%m-%d %H:%M"),
            "email": email,
            "type": t.type,
            "description": t.description,
            "amount_usd": float(t.amount_usd),
            "reference_id": t.reference_id,
        }
        for t, email in txns_result.all()
    ]

    # Unmatched USDC transfers (most recent first, pending first)
    unmatched_result = await db.execute(
        select(UnmatchedUsdcTransfer).order_by(
            UnmatchedUsdcTransfer.resolved.asc(),
            desc(UnmatchedUsdcTransfer.detected_at),
        )
    )
    unmatched_transfers = [
        {
            "tx_hash": u.tx_hash,
            "from_address": u.from_address,
            "amount_usdc": float(u.amount_usdc),
            "block_number": u.block_number,
            "detected_at": u.detected_at.strftime("%Y-%m-%d %H:%M"),
            "resolved": u.resolved,
            "resolution_note": u.resolution_note,
        }
        for u in unmatched_result.scalars().all()
    ]

    # Bot activity stats (platform-wide from in-memory store)
    try:
        from app.store import store as _store
        all_bots, total_bots_mem = await _store.list_bots(limit=10000)
        bot_status_counts: dict[str, int] = {}
        bot_platform_counts: dict[str, int] = {}
        total_ai_cost = 0.0
        total_ai_tokens = 0
        active_statuses = ("ready", "scheduled", "queued", "joining", "in_call", "call_ended")
        for b in all_bots:
            bot_status_counts[b.status] = bot_status_counts.get(b.status, 0) + 1
            bot_platform_counts[b.meeting_platform] = bot_platform_counts.get(b.meeting_platform, 0) + 1
            total_ai_cost += b.ai_total_cost_usd
            total_ai_tokens += b.ai_total_tokens
        active_bots = sum(bot_status_counts.get(s, 0) for s in active_statuses)
        bot_stats = {
            "total": total_bots_mem,
            "active": active_bots,
            "done": bot_status_counts.get("done", 0),
            "error": bot_status_counts.get("error", 0),
            "by_status": bot_status_counts,
            "by_platform": bot_platform_counts,
            "total_ai_cost_usd": round(total_ai_cost, 4),
            "total_ai_tokens": total_ai_tokens,
        }
    except Exception:
        bot_stats = {"total": 0, "active": 0, "done": 0, "error": 0, "by_status": {}, "by_platform": {}, "total_ai_cost_usd": 0.0, "total_ai_tokens": 0}

    flash = None
    msg = request.query_params.get("msg")
    err = request.query_params.get("error")
    if msg == "wallet_saved":
        flash = _flash("success", "Wallet address saved.")
    elif msg == "credit_ok":
        flash = _flash("success", "Account credited successfully.")
    elif msg == "rescan_ok":
        flash = _flash("success", "USDC monitor rescan scheduled.")
    elif msg == "resolved":
        flash = _flash("success", "Transfer marked as resolved.")
    elif msg == "rpc_saved":
        flash = _flash("success", "RPC URL saved. The USDC monitor will use it on the next cycle (within 60 s).")
    elif msg == "account_updated":
        flash = _flash("success", "Account updated successfully.")
    elif err == "invalid_address":
        flash = _flash("danger", "Invalid Ethereum address.")
    elif err == "invalid_rpc_url":
        flash = _flash("danger", "Invalid RPC URL — must start with http:// or https://")
    elif err == "rpc_unreachable":
        reason = request.query_params.get("reason", "connection failed")
        flash = _flash("danger", f"RPC URL validation failed: {reason}")
    elif err == "account_not_found":
        flash = _flash("danger", "Account not found for that email.")
    elif err == "credit_failed":
        flash = _flash("danger", "Failed to apply credit — check server logs.")
    elif err == "invalid_amount":
        flash = _flash("danger", "Invalid amount.")

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "account": account,
        "is_admin": True,
        "wallet_address": wallet_config.value if wallet_config else None,
        "usdc_contract": settings.USDC_CONTRACT,
        "crypto_rpc_configured": rpc_url_source != "none",
        "crypto_rpc_source": rpc_url_source,
        "crypto_rpc_preview": rpc_url_preview,
        "hd_seed_configured": bool(settings.CRYPTO_HD_SEED),
        "stripe_configured": bool(settings.STRIPE_SECRET_KEY),
        "monitor_last_block": monitor_state.value if monitor_state else None,
        "stats": {
            "total_accounts": total_accounts,
            "total_credits_usd": float(total_credits),
            "total_usdc_received": float(total_usdc_in),
            "total_stripe_received": float(total_stripe_in),
            "unmatched_pending": total_unmatched_pending,
        },
        "plan_counts": plan_counts,
        "active_webhook_count": active_webhook_count,
        "active_integration_count": active_integration_count,
        "active_calendar_feed_count": active_calendar_feed_count,
        "oauth_linked_count": oauth_linked_count,
        # SSO / email / storage config status
        "google_sso_configured": bool(settings.GOOGLE_CLIENT_ID),
        "microsoft_sso_configured": bool(settings.MICROSOFT_CLIENT_ID),
        "email_configured": settings.EMAIL_BACKEND not in ("none", ""),
        "storage_backend": settings.STORAGE_BACKEND,
        "video_recording_enabled": settings.VIDEO_RECORDING_ENABLED,
        "all_accounts": all_accounts,
        "recent_txns": recent_txns,
        "unmatched_transfers": unmatched_transfers,
        "bot_stats": bot_stats,
        "flash": flash,
    })


@router.post("/admin/wallet", include_in_schema=False)
async def admin_wallet_submit(
    request: Request,
    wallet_address: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    account = await _get_account_from_request(request, db)
    if not _is_admin(account):
        return RedirectResponse("/dashboard")

    import re
    address = wallet_address.strip().lower()
    if not re.match(r"^0x[0-9a-f]{40}$", address):
        return RedirectResponse("/admin?error=invalid_address", status_code=303)

    from app.api.admin import WALLET_KEY
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
    logger.info("Admin updated platform wallet to %s", address)
    return RedirectResponse("/admin?msg=wallet_saved", status_code=303)


@router.post("/admin/rpc-url", include_in_schema=False)
async def admin_rpc_url_submit(
    request: Request,
    rpc_url: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    account = await _get_account_from_request(request, db)
    if not _is_admin(account):
        return RedirectResponse("/dashboard")

    url = rpc_url.strip()
    if not url.startswith(("http://", "https://")):
        return RedirectResponse("/admin?error=invalid_rpc_url", status_code=303)

    from app.services.crypto_service import test_rpc_url
    ok, reason = await test_rpc_url(url)
    if not ok:
        import urllib.parse
        return RedirectResponse(
            f"/admin?error=rpc_unreachable&reason={urllib.parse.quote(reason[:200])}",
            status_code=303,
        )

    from app.api.admin import RPC_URL_KEY
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
    logger.info("Admin set CRYPTO_RPC_URL via admin panel")
    return RedirectResponse("/admin?msg=rpc_saved", status_code=303)


@router.post("/admin/credit", include_in_schema=False)
async def admin_credit_submit(
    request: Request,
    email: str = Form(...),
    amount_usd: float = Form(...),
    note: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    account = await _get_account_from_request(request, db)
    if not _is_admin(account):
        return RedirectResponse("/dashboard")

    if amount_usd <= 0:
        return RedirectResponse("/admin?error=invalid_amount", status_code=303)

    from decimal import Decimal
    result = await db.execute(select(Account).where(Account.email == email))
    target = result.scalar_one_or_none()
    if not target:
        return RedirectResponse("/admin?error=account_not_found", status_code=303)

    try:
        from app.services.credit_service import add_credits
        await add_credits(
            account_id=target.id,
            amount_usd=Decimal(str(amount_usd)),
            type="usdc_topup",
            description=f"Admin manual credit: {note or 'Manual credit'}",
            reference_id=None,
            db=db,
        )
        logger.info("Admin credited $%.4f to %s. Note: %s", amount_usd, email, note)
    except Exception as exc:
        logger.error("Admin credit failed: %s", exc)
        return RedirectResponse("/admin?error=credit_failed", status_code=303)

    return RedirectResponse("/admin?msg=credit_ok", status_code=303)


@router.post("/admin/rescan", include_in_schema=False)
async def admin_rescan_submit(
    request: Request,
    from_block: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    account = await _get_account_from_request(request, db)
    if not _is_admin(account):
        return RedirectResponse("/dashboard")

    new_value = str(max(0, from_block - 1))
    result = await db.execute(
        select(MonitorState).where(MonitorState.key == "usdc_last_block")
    )
    state = result.scalar_one_or_none()
    if state is None:
        state = MonitorState(key="usdc_last_block", value=new_value)
        db.add(state)
    else:
        state.value = new_value
    await db.commit()
    logger.info("Admin reset USDC monitor last-block to %s (rescan from %d)", new_value, from_block)
    return RedirectResponse("/admin?msg=rescan_ok", status_code=303)


@router.post("/admin/usdc/unmatched/{tx_hash}/resolve", include_in_schema=False)
async def admin_resolve_unmatched(
    tx_hash: str,
    request: Request,
    note: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    account = await _get_account_from_request(request, db)
    if not _is_admin(account):
        return RedirectResponse("/dashboard")

    result = await db.execute(
        select(UnmatchedUsdcTransfer).where(UnmatchedUsdcTransfer.tx_hash == tx_hash)
    )
    transfer = result.scalar_one_or_none()
    if transfer:
        transfer.resolved = True
        transfer.resolution_note = note or "Resolved by admin"
        await db.commit()
        logger.info("Admin resolved unmatched transfer %s. Note: %s", tx_hash, transfer.resolution_note)
    return RedirectResponse("/admin?msg=resolved", status_code=303)


@router.post("/admin/accounts/{account_id}/toggle-active", include_in_schema=False)
async def admin_toggle_account_active(
    account_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Enable or disable a user account."""
    admin = await _get_account_from_request(request, db)
    if not _is_admin(admin):
        return RedirectResponse("/dashboard")

    result = await db.execute(select(Account).where(Account.id == account_id))
    target = result.scalar_one_or_none()
    if target and target.id != admin.id:  # prevent self-disable
        target.is_active = not target.is_active
        await db.commit()
        state = "enabled" if target.is_active else "disabled"
        logger.info("Admin %s %s account %s", admin.email, state, target.email)
    return RedirectResponse("/admin?msg=account_updated", status_code=303)


@router.post("/admin/accounts/{account_id}/toggle-admin", include_in_schema=False)
async def admin_toggle_account_admin(
    account_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Grant or revoke admin privileges for a user account."""
    admin = await _get_account_from_request(request, db)
    if not _is_admin(admin):
        return RedirectResponse("/dashboard")

    result = await db.execute(select(Account).where(Account.id == account_id))
    target = result.scalar_one_or_none()
    if target and target.id != admin.id:  # prevent self-de-admin
        target.is_admin = not target.is_admin
        await db.commit()
        state = "granted" if target.is_admin else "revoked"
        logger.info("Admin %s %s admin for account %s", admin.email, state, target.email)
    return RedirectResponse("/admin?msg=account_updated", status_code=303)


@router.post("/admin/accounts/{account_id}/set-plan", include_in_schema=False)
async def admin_set_account_plan(
    account_id: str,
    request: Request,
    plan: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Set the subscription plan for a user account."""
    admin = await _get_account_from_request(request, db)
    if not _is_admin(admin):
        return RedirectResponse("/dashboard")

    valid_plans = ("free", "starter", "pro", "business")
    if plan not in valid_plans:
        return RedirectResponse("/admin?error=invalid_amount", status_code=303)

    result = await db.execute(select(Account).where(Account.id == account_id))
    target = result.scalar_one_or_none()
    if target:
        target.plan = plan
        await db.commit()
        logger.info("Admin %s set plan=%s for account %s", admin.email, plan, target.email)
    return RedirectResponse("/admin?msg=account_updated", status_code=303)


# ── Integrations UI ───────────────────────────────────────────────────────────

@router.post("/dashboard/integrations/add", include_in_schema=False)
async def add_integration_ui(
    request: Request,
    integ_type: str = Form(...),
    name: str = Form(default=""),
    slack_webhook_url: str = Form(default=""),
    notion_api_token: str = Form(default=""),
    notion_database_id: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    """Add a Slack or Notion integration from the dashboard UI."""
    import json, uuid
    account = await _get_account_from_request(request, db)
    if not account:
        return RedirectResponse("/login")

    if integ_type == "slack":
        url = slack_webhook_url.strip()
        if not url.startswith("https://hooks.slack.com/"):
            return RedirectResponse("/dashboard?integ_error=invalid_slack_url#integrations", status_code=303)
        config = json.dumps({"webhook_url": url})
        display_name = name.strip() or "Slack"
    elif integ_type == "notion":
        token = notion_api_token.strip()
        db_id = notion_database_id.strip()
        if not token or not db_id:
            return RedirectResponse("/dashboard?integ_error=notion_missing_fields#integrations", status_code=303)
        config = json.dumps({"api_token": token, "database_id": db_id})
        display_name = name.strip() or "Notion"
    else:
        return RedirectResponse("/dashboard?integ_error=invalid_type#integrations", status_code=303)

    from app.models.account import Integration
    integ = Integration(
        id=str(uuid.uuid4()),
        account_id=account.id,
        type=integ_type,
        name=display_name,
        config=config,
        is_active=True,
    )
    db.add(integ)
    await db.commit()
    logger.info("Added %s integration for account %s", integ_type, account.email)
    return RedirectResponse("/dashboard?integ_added=1#integrations", status_code=303)


@router.post("/dashboard/integrations/{integ_id}/delete", include_in_schema=False)
async def delete_integration_ui(
    integ_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    account = await _get_account_from_request(request, db)
    if not account:
        return RedirectResponse("/login")

    from app.models.account import Integration
    result = await db.execute(
        select(Integration).where(Integration.id == integ_id, Integration.account_id == account.id)
    )
    integ = result.scalar_one_or_none()
    if integ:
        await db.delete(integ)
        await db.commit()
    return RedirectResponse("/dashboard?integ_deleted=1#integrations", status_code=303)


@router.post("/dashboard/integrations/{integ_id}/toggle", include_in_schema=False)
async def toggle_integration_ui(
    integ_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    account = await _get_account_from_request(request, db)
    if not account:
        return RedirectResponse("/login")

    from app.models.account import Integration
    result = await db.execute(
        select(Integration).where(Integration.id == integ_id, Integration.account_id == account.id)
    )
    integ = result.scalar_one_or_none()
    if integ:
        integ.is_active = not integ.is_active
        await db.commit()
    return RedirectResponse("/dashboard#integrations", status_code=303)


# ── Calendar feeds UI ─────────────────────────────────────────────────────────

@router.post("/dashboard/calendar/add", include_in_schema=False)
async def add_calendar_ui(
    request: Request,
    name: str = Form(default="My Calendar"),
    ical_url: str = Form(...),
    bot_name: str = Form(default=""),
    auto_record: str = Form(default="on"),
    db: AsyncSession = Depends(get_db),
):
    """Add an iCal calendar feed from the dashboard UI."""
    import uuid
    account = await _get_account_from_request(request, db)
    if not account:
        return RedirectResponse("/login")

    url = ical_url.strip()
    if not url.startswith(("http://", "https://")):
        return RedirectResponse("/dashboard?cal_error=invalid_url#calendar", status_code=303)

    from app.models.account import CalendarFeed
    feed = CalendarFeed(
        id=str(uuid.uuid4()),
        account_id=account.id,
        name=(name.strip() or "My Calendar")[:100],
        ical_url=url,
        bot_name=(bot_name.strip() or None),
        auto_record=(auto_record == "on"),
        is_active=True,
    )
    db.add(feed)
    await db.commit()
    logger.info("Added calendar feed '%s' for account %s", feed.name, account.email)
    return RedirectResponse("/dashboard?cal_added=1#calendar", status_code=303)


@router.post("/dashboard/calendar/{feed_id}/delete", include_in_schema=False)
async def delete_calendar_ui(
    feed_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    account = await _get_account_from_request(request, db)
    if not account:
        return RedirectResponse("/login")

    from app.models.account import CalendarFeed
    result = await db.execute(
        select(CalendarFeed).where(CalendarFeed.id == feed_id, CalendarFeed.account_id == account.id)
    )
    feed = result.scalar_one_or_none()
    if feed:
        await db.delete(feed)
        await db.commit()
    return RedirectResponse("/dashboard?cal_deleted=1#calendar", status_code=303)


@router.post("/dashboard/calendar/{feed_id}/toggle", include_in_schema=False)
async def toggle_calendar_ui(
    feed_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    account = await _get_account_from_request(request, db)
    if not account:
        return RedirectResponse("/login")

    from app.models.account import CalendarFeed
    result = await db.execute(
        select(CalendarFeed).where(CalendarFeed.id == feed_id, CalendarFeed.account_id == account.id)
    )
    feed = result.scalar_one_or_none()
    if feed:
        feed.is_active = not feed.is_active
        await db.commit()
    return RedirectResponse("/dashboard#calendar", status_code=303)
