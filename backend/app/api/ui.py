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
from app.models.account import Account, ApiKey, CreditTransaction, PlatformConfig, MonitorState, UnmatchedUsdcTransfer

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

@router.get("/", include_in_schema=False)
async def root(request: Request, db: AsyncSession = Depends(get_db)):
    account = await _get_account_from_request(request, db)
    if account:
        return RedirectResponse("/dashboard")
    return RedirectResponse("/login")


# ── Register ──────────────────────────────────────────────────────────────────

@router.get("/register", response_class=HTMLResponse, include_in_schema=False)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "account": None})


@router.post("/register", response_class=HTMLResponse, include_in_schema=False)
async def register_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
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

    account = Account(
        id=str(uuid.uuid4()),
        email=email,
        hashed_password=_hash_password(password),
        credits_usd=Decimal("0"),
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
    return templates.TemplateResponse("login.html", {"request": request, "account": None})


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

    flash = None
    if request.query_params.get("payment") == "success":
        flash = _flash("success", "Payment successful! Your credits will be added shortly.")
    if request.query_params.get("wallet") == "saved":
        flash = _flash("success", "Wallet address saved successfully.")
    if request.query_params.get("wallet") == "error":
        flash = _flash("danger", "Invalid Ethereum address. Must be 0x followed by 40 hex characters.")
    if request.query_params.get("wallet") == "taken":
        flash = _flash("danger", "This wallet address is already linked to another account.")

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "account": account,
        "is_admin": _is_admin(account),
        "balance": float(account.credits_usd or 0),
        "wallet_address": account.wallet_address,
        "api_keys": api_keys,
        "transactions": transactions,
        "flash": flash,
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

    return RedirectResponse("/dashboard?created=1", status_code=303)


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
    from sqlalchemy import func, desc

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

    # All user accounts
    accounts_result = await db.execute(
        select(Account).order_by(desc(Account.credits_usd))
    )
    all_accounts = [
        {
            "id": a.id,
            "email": a.email,
            "credits_usd": float(a.credits_usd or 0),
            "wallet_address": a.wallet_address,
            "is_active": a.is_active,
            "is_admin": a.is_admin,
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
    elif err == "invalid_address":
        flash = _flash("danger", "Invalid Ethereum address.")
    elif err == "invalid_rpc_url":
        flash = _flash("danger", "Invalid RPC URL — must start with http:// or https://")
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
        "all_accounts": all_accounts,
        "recent_txns": recent_txns,
        "unmatched_transfers": unmatched_transfers,
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
