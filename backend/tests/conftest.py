"""Shared test fixtures for the MeetingBot API test suite."""

import asyncio
import os
import uuid
from typing import AsyncGenerator

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

# Force test settings before importing any app modules
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["JWT_SECRET"] = "test-secret-key-do-not-use-in-production"
os.environ["API_KEY"] = ""  # Disable superadmin bypass
os.environ["CORS_ORIGINS"] = "*"
os.environ["ADMIN_EMAILS"] = "admin@test.com"


@pytest.fixture(scope="session")
def event_loop():
    """Create a session-scoped event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def app():
    """Create a fresh FastAPI app instance with in-memory DB."""
    # Re-import to pick up test env vars
    from app.db import create_all_tables, engine, Base

    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from app.main import app as _app

    yield _app

    # Cleanup: drop all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client(app) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Unauthenticated async HTTP client."""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as c:
        yield c


@pytest_asyncio.fixture
async def auth_client(client: httpx.AsyncClient) -> httpx.AsyncClient:
    """Authenticated client — registers a test user and attaches the API key."""
    email = f"test-{uuid.uuid4().hex[:8]}@test.com"
    password = "TestPassword123!"
    key_name = "test-key"

    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "key_name": key_name},
    )
    assert resp.status_code in (200, 201), f"Register failed: {resp.text}"
    data = resp.json()

    # Extract the API key from the response
    api_key = data.get("api_key") or data.get("key") or data.get("access_token", "")

    # Set auth header on the client
    client.headers["Authorization"] = f"Bearer {api_key}"
    return client
