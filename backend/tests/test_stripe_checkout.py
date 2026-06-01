"""Regression tests for the Stripe checkout flow.

Guards against a bug where the UI top-up handler called the async
``create_checkout_session`` without ``await``, unpacking a coroutine object and
500-ing the entire UI-based top-up flow.
"""

import inspect
from types import SimpleNamespace

import pytest
from starlette.responses import RedirectResponse

from app.api import ui as ui_module
from app.services import stripe_service


def test_create_checkout_session_is_coroutine_function():
    """The service is async — every call site MUST await it. A sync call would
    unpack a coroutine and raise at runtime."""
    assert inspect.iscoroutinefunction(stripe_service.create_checkout_session)


async def test_topup_stripe_submit_awaits_and_redirects(monkeypatch):
    """The UI handler must await create_checkout_session and redirect to the
    returned checkout URL (not 500 on a coroutine unpack)."""
    fake_account = SimpleNamespace(id="acct-123")

    async def _fake_get_account(request, db):
        return fake_account

    async def _fake_create(account_id, amount_usd, success_url, cancel_url):
        assert account_id == "acct-123"
        return "sess_1", "https://checkout.stripe.test/sess_1"

    monkeypatch.setattr(ui_module, "_get_account_from_request", _fake_get_account)
    monkeypatch.setattr(stripe_service, "create_checkout_session", _fake_create)

    req = SimpleNamespace(base_url="http://test/")
    resp = await ui_module.topup_stripe_submit(request=req, amount_usd=10, db=None)

    assert isinstance(resp, RedirectResponse)
    assert resp.status_code == 303
    assert resp.headers["location"] == "https://checkout.stripe.test/sess_1"


async def test_topup_stripe_submit_handles_provider_failure(monkeypatch):
    """A Stripe failure should redirect with an error flag, never 500."""
    fake_account = SimpleNamespace(id="acct-123")

    async def _fake_get_account(request, db):
        return fake_account

    async def _fake_create(account_id, amount_usd, success_url, cancel_url):
        raise RuntimeError("Stripe API down")

    monkeypatch.setattr(ui_module, "_get_account_from_request", _fake_get_account)
    monkeypatch.setattr(stripe_service, "create_checkout_session", _fake_create)

    req = SimpleNamespace(base_url="http://test/")
    resp = await ui_module.topup_stripe_submit(request=req, amount_usd=10, db=None)

    assert isinstance(resp, RedirectResponse)
    assert resp.status_code == 303
    assert "error=payment_unavailable" in resp.headers["location"]
