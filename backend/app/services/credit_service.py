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


def _quota_diag(plan: str, limit: int) -> str:
    plan_name = (plan or "free").capitalize()
    plan_order = ["free", "starter", "pro", "business"]
    try:
        idx = plan_order.index(plan or "free")
    except ValueError:
        idx = 0
    if idx < len(plan_order) - 1:
        next_plan = plan_order[idx + 1]
        next_limit = settings.plan_limits.get(next_plan, -1)
        next_desc = "unlimited" if next_limit == -1 else str(next_limit)
        upgrade = f" Upgrade to {next_plan.capitalize()} for {next_desc} bots/month."
    else:
        upgrade = ""
    return f"Monthly bot limit reached ({limit}/{limit}). Current plan: {plan_name}.{upgrade}"


async def check_plan_limit(account_id: str) -> None:
    """Best-effort check: raise 402 if account already at its monthly cap.

    This is a fast-fail signal only — the actual race-safe enforcement happens
    in ``increment_monthly_usage`` via an atomic conditional UPDATE. We keep
    the check here so the caller gets an immediate, well-formed 402 instead
    of doing the work and rolling back.
    """
    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Account.plan, Account.monthly_bots_used).where(Account.id == account_id)
        )
        row = result.first()
        if row is None:
            return  # unknown account — let downstream handle it
        plan = row[0] or "free"
        used = row[1] or 0
        limit = settings.plan_limits.get(plan, settings.PLAN_FREE_BOTS_PER_MONTH)
        if limit == -1 or used < limit:
            return
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=_quota_diag(plan, limit),
        )


async def increment_monthly_usage(account_id: str) -> None:
    """Atomically reserve a quota slot. Raises 402 if no slot was available.

    The previous implementation ran ``SELECT ... FOR UPDATE`` in
    ``check_plan_limit`` then released the row lock and let an unconditional
    UPDATE bump the counter — two concurrent POST /bot callers could both
    pass the SELECT-side check and both increment, blowing past the cap.
    Now the UPDATE itself carries the cap predicate, so at most ``limit``
    increments succeed regardless of contention.
    """
    from app.db import AsyncSessionLocal
    from sqlalchemy import update

    async with AsyncSessionLocal() as db:
        plan_result = await db.execute(select(Account.plan).where(Account.id == account_id))
        plan_row = plan_result.first()
        if plan_row is None:
            return  # unknown account — silently skip
        plan = plan_row[0] or "free"
        limit = settings.plan_limits.get(plan, settings.PLAN_FREE_BOTS_PER_MONTH)

        if limit == -1:
            await db.execute(
                update(Account)
                .where(Account.id == account_id)
                .values(monthly_bots_used=Account.monthly_bots_used + 1)
            )
            await db.commit()
            return

        upd = await db.execute(
            update(Account)
            .where(Account.id == account_id, Account.monthly_bots_used < limit)
            .values(monthly_bots_used=Account.monthly_bots_used + 1)
        )
        await db.commit()
        if upd.rowcount and upd.rowcount > 0:
            return

    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail=_quota_diag(plan, limit),
    )


async def add_credits(
    account_id: str,
    amount_usd: Decimal,
    type: str,
    description: str,
    reference_id: Optional[str],
    db: AsyncSession,
) -> Decimal:
    """Add credits to an account and record the transaction. Returns new balance.

    Idempotent on ``(type, reference_id)``: if the partial-unique index
    ``ix_credit_tx_unique_ref`` rejects the insert as a duplicate (e.g. the
    USDC monitor and an admin rescan race on the same on-chain tx), the call
    rolls back and returns the existing balance untouched (round-3 fix #4).
    """
    from sqlalchemy.exc import IntegrityError

    # Use SELECT ... FOR UPDATE to prevent lost updates from concurrent additions
    result = await db.execute(
        select(Account).where(Account.id == account_id).with_for_update()
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise ValueError(f"Account {account_id} not found")

    prior_balance = account.credits_usd or Decimal("0")
    account.credits_usd = prior_balance + amount_usd

    tx = CreditTransaction(
        id=str(uuid.uuid4()),
        account_id=account_id,
        amount_usd=amount_usd,
        type=type,
        description=description,
        reference_id=reference_id,
    )
    db.add(tx)
    try:
        await db.commit()
    except IntegrityError as exc:
        # Partial-unique violation on (type, reference_id) — already credited.
        await db.rollback()
        logger.info(
            "add_credits no-op: duplicate (type=%s, ref=%s) — already credited (%s)",
            type, reference_id, exc.orig if hasattr(exc, "orig") else exc,
        )
        # Re-fetch the canonical balance after rollback.
        result = await db.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
        return (account.credits_usd if account else prior_balance)

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
