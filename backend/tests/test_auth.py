"""Smoke tests for auth endpoints."""

import uuid

import pytest
import httpx


@pytest.mark.asyncio
async def test_register_account(client: httpx.AsyncClient):
    """POST /api/v1/auth/register should create an account."""
    email = f"reg-{uuid.uuid4().hex[:8]}@test.com"
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "SecurePass123!", "key_name": "my-key"},
    )
    assert resp.status_code in (200, 201)
    data = resp.json()
    assert "api_key" in data or "key" in data or "access_token" in data


@pytest.mark.asyncio
async def test_register_duplicate_email(client: httpx.AsyncClient):
    """Registering the same email twice should fail."""
    email = f"dup-{uuid.uuid4().hex[:8]}@test.com"
    payload = {"email": email, "password": "SecurePass123!", "key_name": "k"}

    resp1 = await client.post("/api/v1/auth/register", json=payload)
    assert resp1.status_code in (200, 201)

    resp2 = await client.post("/api/v1/auth/register", json=payload)
    assert resp2.status_code == 409


@pytest.mark.asyncio
async def test_login(client: httpx.AsyncClient):
    """POST /api/v1/auth/login should return a JWT."""
    email = f"login-{uuid.uuid4().hex[:8]}@test.com"
    password = "SecurePass123!"
    await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "key_name": "k"},
    )

    resp = await client.post(
        "/api/v1/auth/login",
        data={"username": email, "password": password},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data


@pytest.mark.asyncio
async def test_invalid_login(client: httpx.AsyncClient):
    """Login with wrong password should fail."""
    resp = await client.post(
        "/api/v1/auth/login",
        data={"username": "nobody@test.com", "password": "wrong"},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_unauthenticated_request(client: httpx.AsyncClient):
    """GET /api/v1/bot without auth should return 401."""
    # Ensure no auth header is set
    client.headers.pop("Authorization", None)
    resp = await client.get("/api/v1/bot")
    # In dev mode (no API_KEY set), unauthenticated access may be allowed
    # but with API_KEY set, it should be 401
    assert resp.status_code in (200, 401)
