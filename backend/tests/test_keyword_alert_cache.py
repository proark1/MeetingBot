"""Tests for keyword matching and the per-meeting account-rule cache."""

import json
import uuid

from app.services import keyword_alert_service as K


def test_matches_keyword_case_insensitive():
    assert K._matches_keyword("We should CANCEL the contract", "cancel")
    assert K._matches_keyword("competitor acme corp came up", "Acme Corp")
    assert not K._matches_keyword("nothing relevant here", "refund")


async def _insert_alert(account_id, keywords, name="a"):
    from app.db import AsyncSessionLocal
    from app.models.account import KeywordAlert

    async with AsyncSessionLocal() as s:
        s.add(KeywordAlert(
            account_id=account_id, name=name,
            keywords=json.dumps(keywords), webhook_url=None, is_active=True,
        ))
        await s.commit()


async def test_account_rules_are_cached_and_invalidated(app):
    acct = f"acct-{uuid.uuid4().hex[:8]}"
    K.invalidate_account_alerts(acct)

    await _insert_alert(acct, ["budget"])
    first = await K._load_account_keyword_alerts(acct)
    assert len(first) == 1

    # A new row added after the first load is NOT seen while cached.
    await _insert_alert(acct, ["timeline"], name="b")
    cached = await K._load_account_keyword_alerts(acct)
    assert len(cached) == 1

    # After invalidation the fresh query sees both rules.
    K.invalidate_account_alerts(acct)
    refreshed = await K._load_account_keyword_alerts(acct)
    assert len(refreshed) == 2
