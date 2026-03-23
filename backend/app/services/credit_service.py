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
    flat_fee = Decimal(str(settings.BOT_FLAT_FEE_USD))
    min_usd = flat_fee if flat_fee > 0 else Decimal(str(settings.MIN_CREDITS_USD))
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


async def check_plan_limit(account_id: str) -> None:
    """Raise HTTP 402 if account has reached its monthly bot limit.

    Uses SELECT ... FOR UPDATE to prevent race conditions when two
    concurrent create_bot calls check the same account simultaneously.
    """
    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Account).where(Account.id == account_id).with_for_update()
        )
        account = result.scalar_one_or_none()
        if account is None:
            return  # unknown account — let downstream handle it

        limit = settings.plan_limits.get(account.plan or "free", settings.PLAN_FREE_BOTS_PER_MONTH)
        if limit == -1:
            return  # unlimited

        used = account.monthly_bots_used or 0
        if used < limit:
            return  # within limit

        plan_name = (account.plan or "free").capitalize()
        plan_order = ["free", "starter", "pro", "business"]
        try:
            idx = plan_order.index(account.plan or "free")
        except ValueError:
            idx = 0
        if idx < len(plan_order) - 1:
            next_plan = plan_order[idx + 1]
            next_limit = settings.plan_limits.get(next_plan, -1)
            next_desc = "unlimited" if next_limit == -1 else str(next_limit)
            upgrade_msg = f" Upgrade to {next_plan.capitalize()} for {next_desc} bots/month."
        else:
            upgrade_msg = ""

        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Monthly bot limit reached ({used}/{limit}). "
                f"Current plan: {plan_name}.{upgrade_msg}"
            ),
        )


async def increment_monthly_usage(account_id: str) -> None:
    """Atomically increment the monthly bot usage counter for an account."""
    from app.db import AsyncSessionLocal
    from sqlalchemy import update

    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(monthly_bots_used=Account.monthly_bots_used + 1)
        )
        await db.commit()


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

    flat_fee = Decimal(str(settings.BOT_FLAT_FEE_USD))
    if flat_fee > 0:
        amount = flat_fee
        description = f"Bot run {bot_id[:8]} (flat fee)"
    else:
        markup = Decimal(str(settings.CREDIT_MARKUP))
        amount = Decimal(str(cost_usd)) * markup
        if amount <= Decimal("0"):
            amount = Decimal("0.01")  # Minimum deduction per bot run
        description = f"Bot run {bot_id[:8]} (AI cost ${cost_usd:.6f} × {settings.CREDIT_MARKUP}x markup)"

    from app.db import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        # Use SELECT ... FOR UPDATE to prevent concurrent deduction race conditions
        result = await db.execute(
            select(Account).where(Account.id == account_id).with_for_update()
        )
        account = result.scalar_one_or_none()
        if account is None:
            logger.warning("deduct_credits_for_bot: account %s not found", account_id)
            return

        account.credits_usd = (account.credits_usd or Decimal("0")) - amount

        tx = CreditTransaction(
            id=str(uuid.uuid4()),
            account_id=account_id,
            amount_usd=-amount,
            type="bot_usage",
            description=description,
            reference_id=bot_id,
        )
        db.add(tx)
        await db.commit()

    logger.info(
        "Credits deducted: -$%.4f from account %s for bot %s",
        amount, account_id, bot_id,
    )
