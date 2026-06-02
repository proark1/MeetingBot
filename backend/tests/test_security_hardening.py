"""Security-hardening regression tests (audit remediation).

Covers:
- S1: USDC wallet linking requires/verifies an ownership signature so an
  attacker can't front-run registration of a victim's address.
- S2: API keys are never persisted in plaintext (only the peppered HMAC).
"""

import importlib.util

import httpx
import pytest
from sqlalchemy import select

# eth_account is an optional dependency (installed only in the crypto Docker
# layer, not in requirements.txt / CI). The wallet-ownership tests need it to
# produce signatures, so skip them cleanly when it isn't importable. The S2 key
# test below does NOT need it and always runs.
_requires_eth = pytest.mark.skipif(
    importlib.util.find_spec("eth_account") is None,
    reason="eth_account not installed (optional crypto dependency)",
)


# ── S1: wallet ownership proof ──────────────────────────────────────────────

def _new_eth_account():
    from eth_account import Account as EthAccount
    acct = EthAccount.create()
    return acct.address.lower(), acct.key  # (address, private key bytes)


def _sign(message: str, private_key) -> str:
    from eth_account import Account as EthAccount
    from eth_account.messages import encode_defunct
    signed = EthAccount.sign_message(encode_defunct(text=message), private_key=private_key)
    return signed.signature.hex()


@_requires_eth
@pytest.mark.asyncio
async def test_wallet_link_with_valid_signature(auth_client: httpx.AsyncClient):
    address, pk = _new_eth_account()

    chal = await auth_client.get("/api/v1/auth/wallet/challenge",
                                 params={"wallet_address": address})
    assert chal.status_code == 200, chal.text
    message = chal.json()["message"]

    resp = await auth_client.put(
        "/api/v1/auth/wallet",
        json={"wallet_address": address, "signature": _sign(message, pk)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["wallet_address"] == address


@_requires_eth
@pytest.mark.asyncio
async def test_wallet_link_rejects_bad_signature(auth_client: httpx.AsyncClient):
    address, _pk = _new_eth_account()
    _other_addr, other_pk = _new_eth_account()

    chal = await auth_client.get("/api/v1/auth/wallet/challenge",
                                 params={"wallet_address": address})
    message = chal.json()["message"]
    # Sign with a DIFFERENT key — must not prove control of `address`.
    resp = await auth_client.put(
        "/api/v1/auth/wallet",
        json={"wallet_address": address, "signature": _sign(message, other_pk)},
    )
    assert resp.status_code == 400


@_requires_eth
@pytest.mark.asyncio
async def test_wallet_link_requires_signature_when_enforced(auth_client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "REQUIRE_WALLET_SIGNATURE", True)
    address, _pk = _new_eth_account()
    resp = await auth_client.put("/api/v1/auth/wallet", json={"wallet_address": address})
    assert resp.status_code == 400
    assert "signature" in resp.json()["detail"].lower()


# ── S2: API keys never persisted in plaintext ───────────────────────────────

@pytest.mark.asyncio
async def test_api_keys_not_stored_in_plaintext(auth_client: httpx.AsyncClient):
    # auth_client already registered an account + key and authenticates with it,
    # which itself proves the peppered-HMAC auth path works end to end.
    from app.db import AsyncSessionLocal
    from app.models.account import ApiKey

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(ApiKey))).scalars().all()

    assert rows, "expected at least one API key row"
    for row in rows:
        assert row.key is None, "plaintext key must not be persisted"
        assert row.key_hash and row.key_hash.startswith("h2:")
        assert row.key_prefix
