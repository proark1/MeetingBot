"""Billing API — Stripe top-up, USDC deposits, and balance queries."""

import logging
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import get_current_account_id, SUPERADMIN_ACCOUNT_ID
from app.models.account import Account, CreditTransaction

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["Billing"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class CheckoutRequest(BaseModel):
    amount_usd: int
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None


class CheckoutResponse(BaseModel):
    session_id: str
    checkout_url: str
    amount_usd: int


class UsdcAddressResponse(BaseModel):
    deposit_address: str
    contract: str
    network: str
    note: str


class TransactionItem(BaseModel):
    id: str
    amount_usd: float
    type: str
    description: str
    reference_id: Optional[str]
    created_at: str


class BalanceResponse(BaseModel):
    credits_usd: float
    transactions: list[TransactionItem]


# ── Helper ────────────────────────────────────────────────────────────────────

def _require_account(account_id: Optional[str]) -> str:
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(
            status_code=403,
            detail="Billing requires a per-user account. Register at POST /api/v1/auth/register",
        )
    return account_id


def _get_valid_amounts() -> list[int]:
    try:
        return [int(x.strip()) for x in settings.STRIPE_TOP_UP_AMOUNTS.split(",") if x.strip()]
    except ValueError:
        return [10, 25, 50, 100]


# ── Stripe ────────────────────────────────────────────────────────────────────

@router.post("/stripe/checkout", response_model=CheckoutResponse)
async def create_stripe_checkout(
    payload: CheckoutRequest,
    request: Request,
    account_id: Optional[str] = Depends(get_current_account_id),
):
    """
    Create a Stripe Checkout session to top up your credit balance.

    After payment, credits are added automatically via the Stripe webhook.
    Set `success_url` and `cancel_url` to redirect after payment (optional).
    """
    _require_account(account_id)

    valid_amounts = _get_valid_amounts()
    if payload.amount_usd not in valid_amounts:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid amount. Choose from: {valid_amounts}",
        )

    if not settings.STRIPE_SECRET_KEY:
        raise HTTPException(
            status_code=503,
            detail="Stripe payments are not configured on this server",
        )

    base_url = str(request.base_url).rstrip("/")
    success_url = payload.success_url or f"{base_url}/dashboard?payment=success"
    cancel_url = payload.cancel_url or f"{base_url}/topup?payment=cancelled"

    from app.services.stripe_service import create_checkout_session
    session_id, checkout_url = create_checkout_session(
        account_id=account_id,
        amount_usd=payload.amount_usd,
        success_url=success_url,
        cancel_url=cancel_url,
    )

    return CheckoutResponse(
        session_id=session_id,
        checkout_url=checkout_url,
        amount_usd=payload.amount_usd,
    )


@router.post("/stripe/webhook", include_in_schema=False)
async def stripe_webhook(request: Request):
    """
    Stripe webhook receiver — verifies HMAC signature and credits accounts on payment.
    Register this URL in your Stripe dashboard as a webhook endpoint.
    Event type: checkout.session.completed
    """
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Stripe webhook not configured")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        from app.services.stripe_service import verify_webhook, handle_checkout_completed
        event = verify_webhook(payload, sig_header)
    except Exception as exc:
        logger.warning("Stripe webhook signature verification failed: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        await handle_checkout_completed(session)

    return {"received": True}


# ── USDC ──────────────────────────────────────────────────────────────────────

@router.get("/usdc/address", response_model=UsdcAddressResponse)
async def get_usdc_address(
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Get your unique USDC deposit address on Ethereum mainnet.

    Send USDC (ERC-20) to this address and your credit balance will be
    updated automatically within ~1 minute.

    1 USDC = $1.00 credit
    """
    _require_account(account_id)

    if not settings.CRYPTO_HD_SEED:
        raise HTTPException(
            status_code=503,
            detail="Crypto payments are not configured on this server",
        )

    from app.services.crypto_service import get_or_create_deposit_address
    address = await get_or_create_deposit_address(account_id, db)

    return UsdcAddressResponse(
        deposit_address=address,
        contract=settings.USDC_CONTRACT,
        network="Ethereum Mainnet (ERC-20)",
        note=(
            "Send USDC only. Other tokens will not be credited. "
            "Credits are added automatically within ~1 minute after confirmation."
        ),
    )


# ── Balance ───────────────────────────────────────────────────────────────────

@router.get("/balance", response_model=BalanceResponse)
async def get_balance(
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
):
    """Get current credit balance and the last 50 transactions."""
    _require_account(account_id)

    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    txns_result = await db.execute(
        select(CreditTransaction)
        .where(CreditTransaction.account_id == account_id)
        .order_by(CreditTransaction.created_at.desc())
        .limit(50)
    )
    txns = txns_result.scalars().all()

    return BalanceResponse(
        credits_usd=float(account.credits_usd or 0),
        transactions=[
            TransactionItem(
                id=t.id,
                amount_usd=float(t.amount_usd),
                type=t.type,
                description=t.description,
                reference_id=t.reference_id,
                created_at=t.created_at.isoformat(),
            )
            for t in txns
        ],
    )
