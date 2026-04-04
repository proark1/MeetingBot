"""Shared FastAPI dependencies — authentication and account resolution."""

import hmac
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
    if settings.API_KEY and hmac.compare_digest(token, settings.API_KEY):
        request.state.account_id = SUPERADMIN_ACCOUNT_ID
        request.state.sandbox = False
        return SUPERADMIN_ACCOUNT_ID

    # JWT (web UI sessions)
    if token.startswith("eyJ"):
        try:
            payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
            account_id: str = payload.get("sub", "")
            if not account_id:
                raise ValueError("No sub claim in JWT")
            request.state.account_id = account_id
            request.state.sandbox = False
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
    request.state.sandbox = token.startswith("sk_test_")
    return api_key.account_id


import re as _re

_SUB_USER_RE = _re.compile(r"^[a-zA-Z0-9_\-\.@]{1,255}$")


async def get_sub_user_id(request: Request) -> Optional[str]:
    """Extract X-Sub-User header for business account data isolation.

    Business accounts pass this header to scope bot data per end-user.
    Returns None for personal accounts or when the header is absent.
    Validates format: alphanumeric, underscore, dash, dot, or @ (max 255 chars).
    """
    sub_user = request.headers.get("X-Sub-User")
    if sub_user:
        sub_user = sub_user.strip()
        if not _SUB_USER_RE.match(sub_user):
            raise HTTPException(
                status_code=400,
                detail="X-Sub-User must be 1–255 characters: alphanumeric, underscore, dash, dot, or @",
            )
    request.state.sub_user_id = sub_user or None
    return sub_user or None


async def require_auth(
    account_id: Optional[str] = Depends(get_current_account_id),
) -> Optional[str]:
    """Router-level dependency: authenticate request and return account_id."""
    return account_id


_admin_emails_cache: set[str] | None = None
_admin_emails_raw: str | None = None


def _admin_emails() -> set[str]:
    """Return the set of admin emails from config (cached until config changes)."""
    global _admin_emails_cache, _admin_emails_raw
    raw = settings.ADMIN_EMAILS
    if _admin_emails_cache is None or raw != _admin_emails_raw:
        _admin_emails_raw = raw
        _admin_emails_cache = {e.strip().lower() for e in raw.split(",") if e.strip()}
    return _admin_emails_cache


# ── Feature gating by plan ────────────────────────────────────────────────────
# Feature name → minimum plan required (ascending: free < starter < pro < business)
PLAN_FEATURES: dict[str, str] = {
    # Starter+
    "calendar_auto_join": "starter",
    "integrations": "starter",
    "translation": "starter",
    # Pro+
    "pii_redaction": "pro",
    "workspaces": "pro",
    "keyword_alerts": "pro",
    "custom_templates": "pro",
    # Business only
    "saml_sso": "business",
    "org_analytics": "business",
    "usdc_payments": "business",
    "custom_retention": "business",
}

_PLAN_ORDER = {"free": 0, "starter": 1, "pro": 2, "business": 3}


async def check_feature(feature_name: str, account_id: Optional[str], db: AsyncSession) -> None:
    """Raise 403 if the account's plan doesn't include the named feature."""
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        return  # superadmin / dev mode — no gating

    min_plan = PLAN_FEATURES.get(feature_name)
    if min_plan is None:
        return  # feature not gated

    from app.models.account import Account
    result = await db.execute(select(Account.plan).where(Account.id == account_id))
    plan = result.scalar_one_or_none() or "free"

    if _PLAN_ORDER.get(plan, 0) < _PLAN_ORDER.get(min_plan, 0):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"The '{feature_name}' feature requires the {min_plan.capitalize()} plan or higher. "
                f"Your current plan: {plan.capitalize()}. "
                f"Upgrade at /dashboard or POST /api/v1/billing/subscribe"
            ),
        )


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
