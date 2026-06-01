"""Unit tests for the billing/credit money paths.

Previously untested — the most dangerous gap, since these move money. Covers
idempotent crediting/deduction (the partial-unique (type, reference_id) index)
and monthly-quota reservation/refund.

All tests depend on the ``app`` fixture so the in-memory tables exist, then
open their own ``AsyncSessionLocal`` sessions.
"""
from decimal import Decimal

import pytest

from app.db import AsyncSessionLocal
from app.models.account import Account, CreditTransaction
from app.services import credit_service
from sqlalchemy import select, func


async def _make_account(*, plan="free", credits="0", used=0) -> str:
    import uuid
    async with AsyncSessionLocal() as db:
        acct = Account(
            email=f"credit-{uuid.uuid4().hex[:8]}@test.local",
            hashed_password="x",
            plan=plan,
            credits_usd=Decimal(credits),
            monthly_bots_used=used,
        )
        db.add(acct)
        await db.commit()
        return acct.id


async def _balance(account_id: str) -> Decimal:
    async with AsyncSessionLocal() as db:
        row = await db.execute(select(Account.credits_usd).where(Account.id == account_id))
        return row.scalar_one()


async def _used(account_id: str) -> int:
    async with AsyncSessionLocal() as db:
        row = await db.execute(select(Account.monthly_bots_used).where(Account.id == account_id))
        return row.scalar_one()


async def _tx_count(account_id: str) -> int:
    async with AsyncSessionLocal() as db:
        row = await db.execute(
            select(func.count()).select_from(CreditTransaction).where(
                CreditTransaction.account_id == account_id
            )
        )
        return row.scalar_one()


@pytest.mark.asyncio
async def test_add_credits_idempotent_on_reference(app):
    """add_credits with the same (type, reference_id) credits only once."""
    account_id = await _make_account()

    async with AsyncSessionLocal() as db:
        b1 = await credit_service.add_credits(
            account_id, Decimal("10"), "stripe_topup", "first", "sess_123", db
        )
    assert b1 == Decimal("10")

    # Replay the same reference — no-op, balance unchanged.
    async with AsyncSessionLocal() as db:
        b2 = await credit_service.add_credits(
            account_id, Decimal("10"), "stripe_topup", "dup", "sess_123", db
        )
    assert b2 == Decimal("10")
    assert await _tx_count(account_id) == 1


@pytest.mark.asyncio
async def test_deduct_credits_for_bot_flat_fee(app):
    """Default config charges the flat BOT_FLAT_FEE_USD per bot run."""
    account_id = await _make_account(credits="5")
    await credit_service.deduct_credits_for_bot(account_id, "bot_aaaaaaaa", cost_usd=0.0)
    bal = await _balance(account_id)
    assert bal == Decimal("5") - Decimal(str(credit_service.settings.BOT_FLAT_FEE_USD))


@pytest.mark.asyncio
async def test_deduct_credits_for_bot_idempotent(app):
    """Deducting twice for the same bot_id charges only once."""
    account_id = await _make_account(credits="5")

    await credit_service.deduct_credits_for_bot(account_id, "bot_dupdup1", cost_usd=0.0)
    bal1 = await _balance(account_id)
    await credit_service.deduct_credits_for_bot(account_id, "bot_dupdup1", cost_usd=0.0)
    bal2 = await _balance(account_id)

    assert bal1 == bal2
    assert await _tx_count(account_id) == 1


@pytest.mark.asyncio
async def test_deduct_skips_superadmin_and_anon(app):
    """Superadmin / unauthenticated deductions are no-ops (no raise)."""
    from app.deps import SUPERADMIN_ACCOUNT_ID
    await credit_service.deduct_credits_for_bot(SUPERADMIN_ACCOUNT_ID, "bot_x", 1.0)
    await credit_service.deduct_credits_for_bot(None, "bot_x", 1.0)


@pytest.mark.asyncio
async def test_increment_monthly_usage_enforces_limit(app):
    """Free plan allows PLAN_FREE_BOTS_PER_MONTH increments, then raises 402."""
    from fastapi import HTTPException
    limit = credit_service.settings.PLAN_FREE_BOTS_PER_MONTH
    account_id = await _make_account(plan="free")

    for _ in range(limit):
        await credit_service.increment_monthly_usage(account_id)

    with pytest.raises(HTTPException) as exc:
        await credit_service.increment_monthly_usage(account_id)
    assert exc.value.status_code == 402
    assert await _used(account_id) == limit


@pytest.mark.asyncio
async def test_decrement_monthly_usage_clamps_at_zero(app):
    """decrement never drives the counter below zero, and refunds a used slot."""
    account_id = await _make_account(plan="free", used=0)

    await credit_service.decrement_monthly_usage(account_id)
    assert await _used(account_id) == 0

    await credit_service.increment_monthly_usage(account_id)
    await credit_service.decrement_monthly_usage(account_id)
    assert await _used(account_id) == 0
