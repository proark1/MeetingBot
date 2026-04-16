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


# ── Disable every slowapi Limiter instance for tests ────────────────────────
# The app instantiates `Limiter(...)` in several routers (app/_limiter.py,
# app/api/auth.py, app/api/bots.py, app/api/webhooks.py, app/api/exports.py).
# Rather than hunting them down and disabling each one, monkey-patch the class
# __init__ so every instance is born with .enabled = False. Must run before
# any app module that creates a Limiter is imported.
from slowapi import Limiter as _Limiter  # noqa: E402

_orig_limiter_init = _Limiter.__init__


def _limiter_init_disabled(self, *args, **kwargs):
    _orig_limiter_init(self, *args, **kwargs)
    self.enabled = False


_Limiter.__init__ = _limiter_init_disabled


# ── Swap app.db.engine for a shared-connection in-memory SQLite ─────────────
# Default `create_async_engine("sqlite+aiosqlite:///:memory:")` gives every
# connection its own fresh DB, so tables created in the test fixture are
# invisible to the request handler. StaticPool forces all async sessions to
# share a single connection (and therefore a single in-memory DB).
def _install_shared_inmemory_engine() -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    import app.db as _db

    _db.engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    _db.AsyncSessionLocal = async_sessionmaker(
        _db.engine, expire_on_commit=False, class_=AsyncSession
    )


_install_shared_inmemory_engine()


@pytest.fixture(scope="session")
def event_loop():
    """Create a session-scoped event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def app():
    """Create a fresh FastAPI app instance with in-memory DB."""
    from app.db import engine, Base

    # Register ORM models on Base.metadata before calling create_all — otherwise
    # on the very first test (before app.main has been imported transitively)
    # Base.metadata is empty and no tables are created.
    from app.models import account  # noqa: F401

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

    # Freshly registered accounts start at $0 credits; top them up so tests
    # that exercise credit-gated endpoints (e.g. POST /api/v1/bot) don't 402.
    from decimal import Decimal
    from sqlalchemy import update

    from app.db import AsyncSessionLocal
    from app.models.account import Account

    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Account).where(Account.email == email).values(credits_usd=Decimal("100"))
        )
        await session.commit()

    # Set auth header on the client
    client.headers["Authorization"] = f"Bearer {api_key}"
    return client
