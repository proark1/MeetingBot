"""Tests for GDPR account erasure.

Verifies the reflective purge deletes rows across *all* per-account tables —
including meeting_summaries and retention_policies, which the previous
hand-maintained list silently missed — while retaining audit_logs.
"""
import uuid

import pytest

from app.db import AsyncSessionLocal
from app.models.account import (
    Account, BotSnapshot, Webhook, ActionItem, MeetingSummary,
    RetentionPolicy, AuditLog,
)
from app.services.gdpr_service import purge_account_owned_rows, account_owned_models
from sqlalchemy import select, func


async def _seed_account_with_data() -> str:
    async with AsyncSessionLocal() as db:
        acct = Account(email=f"gdpr-{uuid.uuid4().hex[:8]}@test.local", hashed_password="x")
        db.add(acct)
        await db.flush()
        aid = acct.id

        db.add(BotSnapshot(id=f"bot-{uuid.uuid4().hex[:8]}", account_id=aid,
                           status="done", meeting_url="https://m/x", data="{}"))
        db.add(Webhook(id=str(uuid.uuid4()), account_id=aid,
                       url="https://hook/x", events='["*"]'))
        db.add(ActionItem(account_id=aid, bot_id="bot-x",
                          content_hash="h" * 8, task="do the thing"))
        # The two tables the old purge list missed:
        db.add(MeetingSummary(account_id=aid, bot_id="bot-x"))
        db.add(RetentionPolicy(account_id=aid))
        # Retained for traceability:
        db.add(AuditLog(account_id=aid, action="account.created"))
        await db.commit()
        return aid


async def _count(model, account_id) -> int:
    async with AsyncSessionLocal() as db:
        row = await db.execute(
            select(func.count()).select_from(model).where(model.account_id == account_id)
        )
        return row.scalar_one()


@pytest.mark.asyncio
async def test_purge_covers_previously_missed_tables(app):
    account_id = await _seed_account_with_data()

    # Sanity: rows exist before purge.
    assert await _count(MeetingSummary, account_id) == 1
    assert await _count(RetentionPolicy, account_id) == 1

    async with AsyncSessionLocal() as db:
        counts = await purge_account_owned_rows(account_id, db)
        await db.commit()

    # The two formerly-orphaned tables are now purged.
    assert await _count(MeetingSummary, account_id) == 0
    assert await _count(RetentionPolicy, account_id) == 0
    # And the always-covered ones.
    assert await _count(BotSnapshot, account_id) == 0
    assert await _count(Webhook, account_id) == 0
    assert await _count(ActionItem, account_id) == 0
    # meeting_summaries / retention_policies appear in the returned counts.
    assert counts.get("meeting_summaries", 0) >= 1
    assert counts.get("retention_policies", 0) >= 1


@pytest.mark.asyncio
async def test_purge_retains_audit_logs(app):
    account_id = await _seed_account_with_data()
    async with AsyncSessionLocal() as db:
        await purge_account_owned_rows(account_id, db)
        await db.commit()
    # Audit trail survives erasure.
    assert await _count(AuditLog, account_id) == 1


def test_account_owned_models_excludes_retained():
    names = {m.__tablename__ for m in account_owned_models()}
    assert "audit_logs" not in names
    assert "accounts" not in names
    # Spot-check coverage of the formerly-missed tables.
    assert "meeting_summaries" in names
    assert "retention_policies" in names
