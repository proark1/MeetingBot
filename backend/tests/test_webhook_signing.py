"""Unit tests for webhook signing, status classification, and SSRF/DNS cache."""

import hashlib
import hmac

import pytest

from app.services import webhook_service as W


def test_sign_is_deterministic_and_uses_timestamped_payload():
    body = '{"event":"bot.done"}'
    sig, ts = W._sign(body, "shhh", ts_unix="1700000000")
    expected = "sha256=" + hmac.new(
        b"shhh", f"1700000000.{body}".encode(), hashlib.sha256
    ).hexdigest()
    assert sig == expected
    assert ts == "1700000000"
    # same inputs -> same signature
    assert W._sign(body, "shhh", ts_unix="1700000000")[0] == sig
    # different secret -> different signature
    assert W._sign(body, "other", ts_unix="1700000000")[0] != sig


def test_classify_status():
    assert W._classify_status(200) == "success"
    assert W._classify_status(204) == "success"
    assert W._classify_status(500) == "retry"
    assert W._classify_status(429) == "retry"
    assert W._classify_status(408) == "retry"
    assert W._classify_status(None) == "retry"   # connection/timeout
    assert W._classify_status(404) == "fail"
    assert W._classify_status(401) == "fail"


async def test_ssrf_rejects_localhost_and_private_literals():
    assert await W.check_url_ssrf("http://localhost/x") is not None
    assert await W.check_url_ssrf("http://127.0.0.1/x") is not None
    assert await W.check_url_ssrf("http://169.254.169.254/latest/meta-data") is not None
    assert await W.check_url_ssrf("http://10.0.0.5/x") is not None
    assert await W.check_url_ssrf("ftp://example.com/x") is not None  # scheme not allowed


async def test_ssrf_allows_public_host_and_caches_resolution(monkeypatch):
    W._dns_cache.clear()
    calls = {"n": 0}

    def _fake_getaddrinfo(host, *a, **k):
        calls["n"] += 1
        return [(2, 1, 6, "", ("93.184.216.34", 0))]  # a public IP

    monkeypatch.setattr(W.socket, "getaddrinfo", _fake_getaddrinfo)

    assert await W.check_url_ssrf("https://safe.example.test/hook") is None
    assert await W.check_url_ssrf("https://safe.example.test/hook") is None
    # second call served from the DNS cache — only one real resolution
    assert calls["n"] == 1


async def test_ssrf_does_not_cache_blocked_resolution(monkeypatch):
    W._dns_cache.clear()

    def _fake_getaddrinfo(host, *a, **k):
        return [(2, 1, 6, "", ("10.1.2.3", 0))]  # private

    monkeypatch.setattr(W.socket, "getaddrinfo", _fake_getaddrinfo)
    assert await W.check_url_ssrf("https://evil.example.test/hook") is not None
    assert "evil.example.test" not in W._dns_cache
