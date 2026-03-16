"""Shared FastAPI dependencies — authentication and account resolution."""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)

# Sentinel value for the legacy superadmin API_KEY bypass
SUPERADMIN_ACCOUNT_ID = "__superadmin__"


async def get_current_account_id(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
    db: AsyncSession = Depends(get_db),
) -> Optional[str]:
    """
    Resolve account_id from the Authorization header.

    Priority:
    1. Legacy API_KEY env var → SUPERADMIN_ACCOUNT_ID (no per-user account)
    2. JWT (eyJ...) → decode and return account_id
    3. Per-user API key (sk_live_...) → DB lookup, return account_id

    If no credentials and API_KEY is unset → allow (unauthenticated dev mode).
    Sets request.state.account_id for downstream use.
    """
    if credentials is None:
        if settings.API_KEY:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing Authorization header. Use: Authorization: Bearer <key>",
            )
        # Unauthenticated dev mode
        request.state.account_id = None
        return None

    token = credentials.credentials

    # Legacy superadmin bypass
    if settings.API_KEY and token == settings.API_KEY:
        request.state.account_id = SUPERADMIN_ACCOUNT_ID
        return SUPERADMIN_ACCOUNT_ID

    # JWT (web UI sessions)
    if token.startswith("eyJ"):
        try:
            payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
            account_id: str = payload.get("sub", "")
            if not account_id:
                raise ValueError("No sub claim in JWT")
            request.state.account_id = account_id
            return account_id
        except (JWTError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
            )

    # Per-user API key (sk_live_...)
    from app.models.account import ApiKey
    result = await db.execute(
        select(ApiKey).where(ApiKey.key == token, ApiKey.is_active == True)  # noqa: E712
    )
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )

    # Update last_used_at (best-effort, don't fail auth if this fails)
    try:
        await db.execute(
            update(ApiKey)
            .where(ApiKey.id == api_key.id)
            .values(last_used_at=datetime.now(timezone.utc))
        )
        await db.commit()
    except Exception:
        pass

    request.state.account_id = api_key.account_id
    return api_key.account_id


async def require_auth(
    account_id: Optional[str] = Depends(get_current_account_id),
) -> Optional[str]:
    """Router-level dependency: authenticate request and return account_id."""
    return account_id


def _admin_emails() -> set[str]:
    """Return the set of admin emails from config (evaluated at call time, not import time)."""
    return {e.strip().lower() for e in settings.ADMIN_EMAILS.split(",") if e.strip()}


async def require_admin(
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
) -> str:
    """Require that the current user is an admin. Returns the account_id."""
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access requires a per-user account.",
        )

    from app.models.account import Account
    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    if account.email.lower() not in _admin_emails() and not account.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access denied.",
        )

    return account_id
