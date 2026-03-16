"""Account registration, login, and API key management."""

import logging
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from jose import jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import get_current_account_id, SUPERADMIN_ACCOUNT_ID
from app.models.account import Account, ApiKey

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["Auth"])

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _hash_password(password: str) -> str:
    return _pwd_ctx.hash(password)


def _verify_password(plain: str, hashed: str) -> bool:
    return _pwd_ctx.verify(plain, hashed)


def _create_jwt(account_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.JWT_EXPIRE_HOURS)
    return jwt.encode(
        {"sub": account_id, "exp": expire},
        settings.JWT_SECRET,
        algorithm="HS256",
    )


def generate_api_key() -> str:
    return "sk_live_" + secrets.token_urlsafe(40)


# ── Schemas ───────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr = Field(description="Email address for the new account.")
    password: str = Field(min_length=8, description="Password (minimum 8 characters).")
    key_name: str = Field(
        default="Default",
        max_length=100,
        description="Label for the first API key generated with your account.",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "email": "you@example.com",
                "password": "supersecret",
                "key_name": "Production",
            }
        }
    }


class RegisterResponse(BaseModel):
    account_id: str = Field(description="Unique account UUID.")
    email: str = Field(description="Registered email address.")
    api_key: str = Field(
        description=(
            "Your first API key (`sk_live_...`). "
            "Include it on every API request as: `Authorization: Bearer <api_key>`."
        )
    )
    message: str = Field(description="Human-readable instructions for using the API key.")

    model_config = {
        "json_schema_extra": {
            "example": {
                "account_id": "550e8400-e29b-41d4-a716-446655440000",
                "email": "you@example.com",
                "api_key": "YOUR_API_KEY_RETURNED_HERE",
                "message": "Account created. Use the api_key as your Bearer token: Authorization: Bearer <api_key>",
            }
        }
    }


class LoginResponse(BaseModel):
    """
    JWT access token for authenticating web UI sessions.

    **Note:** This token is intended for the browser-based web UI only.
    For API calls, use your `sk_live_...` API key as a Bearer token instead.
    """

    access_token: str = Field(description="JWT token for web UI session (valid for `JWT_EXPIRE_HOURS` hours).")
    token_type: str = Field(default="bearer", description="Always `bearer`.")
    account_id: str = Field(description="The authenticated account UUID.")


class CreateKeyRequest(BaseModel):
    name: str = Field(
        default="New Key",
        max_length=100,
        description="Human-readable label for this API key (e.g. 'Production', 'CI/CD').",
    )

    model_config = {"json_schema_extra": {"example": {"name": "Production"}}}


class ApiKeyResponse(BaseModel):
    id: str = Field(description="Unique key UUID (used to revoke the key).")
    name: str = Field(description="Human-readable label.")
    key_preview: str = Field(description="First 16 characters of the key followed by `...` — the full key is only shown once at creation.")
    is_active: bool = Field(description="False if the key has been revoked.")
    created_at: datetime = Field(description="When the key was created (UTC).")
    last_used_at: Optional[datetime] = Field(default=None, description="Last time this key was used for an authenticated request (UTC), or null if never used.")


class AccountResponse(BaseModel):
    id: str = Field(description="Unique account UUID.")
    email: str = Field(description="Registered email address.")
    credits_usd: float = Field(description="Current prepaid credit balance in USD.")
    is_active: bool = Field(description="False if the account has been disabled by an admin.")
    created_at: datetime = Field(description="Account creation time (UTC).")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/register", response_model=RegisterResponse, status_code=201)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """Create a new account and return the first API key."""
    existing = await db.execute(select(Account).where(Account.email == payload.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already registered")

    account = Account(
        id=str(uuid.uuid4()),
        email=payload.email,
        hashed_password=_hash_password(payload.password),
        credits_usd=Decimal("0"),
    )
    db.add(account)

    key_value = generate_api_key()
    api_key = ApiKey(
        id=str(uuid.uuid4()),
        account_id=account.id,
        key=key_value,
        name=payload.key_name,
    )
    db.add(api_key)
    await db.commit()

    logger.info("New account registered: %s (%s)", account.email, account.id)
    return RegisterResponse(
        account_id=account.id,
        email=account.email,
        api_key=key_value,
        message=(
            "Account created. Use the api_key as your Bearer token: "
            "Authorization: Bearer <api_key>"
        ),
    )


@router.post("/login", response_model=LoginResponse)
async def login(
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """
    Authenticate and receive a JWT for web UI sessions.

    **Request format:** `application/x-www-form-urlencoded` (OAuth2 password flow).
    Send `username` (your email) and `password` as form fields — **not** JSON.

    The returned JWT is only for the browser-based web UI (`/dashboard`, `/topup`).
    For API calls, use your `sk_live_...` API key with `Authorization: Bearer <key>`.
    """
    result = await db.execute(select(Account).where(Account.email == form.username))
    account = result.scalar_one_or_none()
    if not account or not _verify_password(form.password, account.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not account.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")

    token = _create_jwt(account.id)
    return LoginResponse(access_token=token, account_id=account.id)


@router.get("/me", response_model=AccountResponse)
async def get_me(
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
):
    """Get the current account's info and credit balance."""
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="No per-user account for superadmin/unauthenticated mode")
    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return AccountResponse(
        id=account.id,
        email=account.email,
        credits_usd=float(account.credits_usd or 0),
        is_active=account.is_active,
        created_at=account.created_at,
    )


@router.post("/keys", response_model=ApiKeyResponse, status_code=201)
async def create_api_key(
    payload: CreateKeyRequest,
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
):
    """Generate a new named API key for the current account."""
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Use per-user authentication to manage API keys")

    key_value = generate_api_key()
    api_key = ApiKey(
        id=str(uuid.uuid4()),
        account_id=account_id,
        key=key_value,
        name=payload.name,
    )
    db.add(api_key)
    await db.commit()

    return ApiKeyResponse(
        id=api_key.id,
        name=api_key.name,
        key_preview=key_value[:16] + "...",
        is_active=True,
        created_at=api_key.created_at,
        last_used_at=None,
    )


@router.get("/keys", response_model=list[ApiKeyResponse])
async def list_api_keys(
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
):
    """List all active API keys for the current account."""
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Use per-user authentication to manage API keys")

    result = await db.execute(
        select(ApiKey)
        .where(ApiKey.account_id == account_id, ApiKey.is_active == True)  # noqa: E712
        .order_by(ApiKey.created_at.desc())
    )
    keys = result.scalars().all()
    return [
        ApiKeyResponse(
            id=k.id,
            name=k.name,
            key_preview=k.key[:16] + "...",
            is_active=k.is_active,
            created_at=k.created_at,
            last_used_at=k.last_used_at,
        )
        for k in keys
    ]


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: str,
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
):
    """Revoke (deactivate) an API key."""
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Use per-user authentication to manage API keys")

    result = await db.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.account_id == account_id)
    )
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    key.is_active = False
    await db.commit()
