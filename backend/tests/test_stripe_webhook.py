"""Tests for the Stripe credit-granting path (handle_checkout_completed).

This is the money path: a completed checkout must credit the account exactly
once, even if Stripe delivers the event more than once (retries). Previously the
whole webhook path had zero test coverage.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models.account import Account


async def _make_account(credits: str = "0") -> str:
    async with AsyncSessionLocal() as s:
        acct = Account(
            email=f"pay-{uuid.uuid4().hex[:8]}@test.com",
            hashed_password="x",
            credits_usd=Decimal(credits),
        )
        s.add(acct)
        await s.flush()
        account_id = acct.id
        await s.commit()
    return account_id


async def _balance(account_id: str) -> Decimal:
    async with AsyncSessionLocal() as s:
        bal = (
            await s.execute(select(Account.credits_usd).where(Account.id == account_id))
        ).scalar_one()
    return Decimal(str(bal))


@pytest.mark.asyncio
async def test_checkout_credits_account_once(app):
    from app.services.stripe_service import (
        handle_checkout_completed,
        record_stripe_session,
    )

    account_id = await _make_account("0")
    session_id = f"cs_test_{uuid.uuid4().hex}"
    await record_stripe_session(session_id, account_id, 25)

    # Stripe sends amount_total in cents; the handler uses it as authoritative.
    event_session = {
        "id": session_id,
        "metadata": {"account_id": account_id},
        "amount_total": 2500,
    }

    credited = await handle_checkout_completed(event_session)
    assert credited == Decimal("25")
    assert await _balance(account_id) == Decimal("25")

    # Idempotency: a duplicate delivery must NOT double-credit.
    again = await handle_checkout_completed(event_session)
    assert again is None
    assert await _balance(account_id) == Decimal("25")


@pytest.mark.asyncio
async def test_checkout_missing_account_is_ignored(app):
    from app.services.stripe_service import handle_checkout_completed

    # No account_id in metadata → nothing credited, no crash.
    result = await handle_checkout_completed(
        {"id": f"cs_test_{uuid.uuid4().hex}", "metadata": {}, "amount_total": 1000}
    )
    assert result is None


@pytest.mark.asyncio
async def test_webhook_verify_requires_secret(app):
    # With STRIPE_WEBHOOK_SECRET unset (test default), signature verification
    # must refuse rather than trust an unsigned payload.
    from app.services.stripe_service import verify_webhook

    with pytest.raises(Exception):
        await verify_webhook(b"{}", "t=1,v1=deadbeef")
