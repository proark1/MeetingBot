"""Credit balance management — check, add, and deduct credits."""

import logging
import uuid
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.account import Account, CreditTransaction

logger = logging.getLogger(__name__)


async def check_credits(account_id: str, db: AsyncSession) -> None:
    """Raise HTTP 402 if account has insufficient credits to start a bot."""
    min_usd = Decimal(str(settings.MIN_CREDITS_USD))
    result = await db.execute(select(Account.credits_usd).where(Account.id == account_id))
    balance = result.scalar_one_or_none()
    if balance is None or balance < min_usd:
        bal_str = f"${float(balance):.4f}" if balance is not None else "$0.0000"
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Insufficient credits. Balance: {bal_str}. "
                f"Minimum required: ${min_usd:.4f}. "
                "Top up at /topup or via POST /api/v1/billing/stripe/checkout"
            ),
        )


async def add_credits(
    account_id: str,
    amount_usd: Decimal,
    type: str,
    description: str,
    reference_id: Optional[str],
    db: AsyncSession,
) -> Decimal:
    """Add credits to an account and record the transaction. Returns new balance."""
    account = await db.get(Account, account_id)
    if account is None:
        raise ValueError(f"Account {account_id} not found")

    account.credits_usd = (account.credits_usd or Decimal("0")) + amount_usd

    tx = CreditTransaction(
        id=str(uuid.uuid4()),
        account_id=account_id,
        amount_usd=amount_usd,
        type=type,
        description=description,
        reference_id=reference_id,
    )
    db.add(tx)
    await db.commit()

    logger.info(
        "Credits added: +$%.4f to account %s (type=%s, ref=%s). New balance: $%.4f",
        amount_usd, account_id, type, reference_id, account.credits_usd,
    )
    return account.credits_usd


async def deduct_credits_for_bot(
    account_id: Optional[str],
    bot_id: str,
    cost_usd: float,
) -> None:
    """
    Deduct credits for a completed bot run (called from bot_service, creates its own DB session).
    No-op for superadmin or unauthenticated mode.
    """
    from app.deps import SUPERADMIN_ACCOUNT_ID
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        return

    markup = Decimal(str(settings.CREDIT_MARKUP))
    amount = Decimal(str(cost_usd)) * markup
    if amount <= Decimal("0"):
        amount = Decimal("0.01")  # Minimum deduction per bot run

    from app.db import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        account = await db.get(Account, account_id)
        if account is None:
            logger.warning("deduct_credits_for_bot: account %s not found", account_id)
            return

        account.credits_usd = (account.credits_usd or Decimal("0")) - amount

        tx = CreditTransaction(
            id=str(uuid.uuid4()),
            account_id=account_id,
            amount_usd=-amount,
            type="bot_usage",
            description=(
                f"Bot run {bot_id[:8]} "
                f"(AI cost ${cost_usd:.6f} × {settings.CREDIT_MARKUP}x markup)"
            ),
            reference_id=bot_id,
        )
        db.add(tx)
        await db.commit()

    logger.info(
        "Credits deducted: -$%.4f from account %s for bot %s (cost=%.6f markup=%.1f)",
        amount, account_id, bot_id, cost_usd, settings.CREDIT_MARKUP,
    )
