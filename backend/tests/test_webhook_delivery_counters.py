"""Webhook delivery-counter atomicity + DNS-cache bounding (audit follow-up).

- record_webhook_delivery does the counter read-modify-write under the store
  lock, so concurrent deliveries can't lose increments (the residual half of
  the cross-path race).
- check_url_ssrf's positive DNS cache is size-capped.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from app.store import store
import app.services.webhook_service as W


def _now():
    return datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_delivery_counters_and_auto_disable(app):
    wh = await store.new_webhook("https://example.test/hook", ["*"])

    # Four failures: increments, still active.
    for i in range(1, 5):
        cf, active, auto = await store.record_webhook_delivery(
            wh.id, success=False, status_code=500, now=_now())
        assert (cf, active, auto) == (i, True, False)

    # Fifth failure trips the auto-disable threshold exactly once.
    cf, active, auto = await store.record_webhook_delivery(
        wh.id, success=False, status_code=500, now=_now())
    assert (cf, active, auto) == (5, False, True)

    # Sixth: still counting, already disabled, no re-fire.
    cf, active, auto = await store.record_webhook_delivery(
        wh.id, success=False, status_code=500, now=_now())
    assert (cf, active, auto) == (6, False, False)

    # Success resets failures but does not re-enable a disabled hook.
    cf, active, auto = await store.record_webhook_delivery(
        wh.id, success=True, status_code=200, now=_now())
    assert (cf, active, auto) == (0, False, False)

    live = await store.get_webhook(wh.id)
    assert live.delivery_attempts == 7
    assert live.consecutive_failures == 0


@pytest.mark.asyncio
async def test_concurrent_deliveries_do_not_lose_increments(app):
    wh = await store.new_webhook("https://example.test/hook2", ["*"])
    n = 30
    await asyncio.gather(*(
        store.record_webhook_delivery(wh.id, success=False, status_code=503, now=_now())
        for _ in range(n)
    ))
    live = await store.get_webhook(wh.id)
    assert live.delivery_attempts == n           # no lost increments
    assert live.consecutive_failures == n
    assert live.is_active is False               # disabled past the threshold


@pytest.mark.asyncio
async def test_dns_cache_is_size_capped(monkeypatch):
    W._dns_cache.clear()
    monkeypatch.setattr(W, "_DNS_CACHE_MAX", 50)
    # Long TTL so entries don't expire mid-test.
    monkeypatch.setattr(W, "_DNS_CACHE_TTL_S", 600.0)
    monkeypatch.setattr(W.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])

    for i in range(200):
        assert await W.check_url_ssrf(f"https://host-{i}.example.test/x") is None

    assert len(W._dns_cache) <= 50
    W._dns_cache.clear()
