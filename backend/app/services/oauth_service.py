"""Google and Microsoft OAuth2 SSO helpers.

Flow (Authorization Code):
1. Client redirects user to GET /api/v1/auth/oauth/{provider}/authorize
2. Provider calls back to GET /api/v1/auth/oauth/{provider}/callback?code=...&state=...
3. Server exchanges code → tokens, fetches user info, upserts OAuthAccount + Account,
   issues an API key (sk_live_...) or a web-UI JWT — returns both.

State parameter is an opaque HMAC-signed token (base64url) to prevent CSRF.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)
_http_client = httpx.AsyncClient(timeout=15, follow_redirects=True)

# ── Provider configurations ────────────────────────────────────────────────────

_PROVIDERS: dict[str, dict] = {
    "google": {
        "auth_url":    "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url":   "https://oauth2.googleapis.com/token",
        "userinfo_url":"https://www.googleapis.com/oauth2/v3/userinfo",
        "scopes":      "openid email profile",
        "client_id":   lambda: settings.GOOGLE_CLIENT_ID,
        "client_secret": lambda: settings.GOOGLE_CLIENT_SECRET,
    },
    "microsoft": {
        "auth_url":    "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url":   "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "userinfo_url":"https://graph.microsoft.com/v1.0/me",
        "scopes":      "openid email profile User.Read",
        "client_id":   lambda: settings.MICROSOFT_CLIENT_ID,
        "client_secret": lambda: settings.MICROSOFT_CLIENT_SECRET,
    },
}


def _redirect_uri(provider: str) -> str:
    base = settings.OAUTH_REDIRECT_BASE_URL.rstrip("/")
    return f"{base}/api/v1/auth/oauth/{provider}/callback"


# ── CSRF state token ───────────────────────────────────────────────────────────

def _state_secret() -> bytes:
    return settings.JWT_SECRET.encode()[:32].ljust(32, b"0")


def generate_state(extra: Optional[str] = None) -> str:
    """Return a signed state token.  ``extra`` is embedded in the payload."""
    payload = json.dumps({"nonce": secrets.token_urlsafe(16), "extra": extra or ""})
    sig = hmac.new(_state_secret(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    token = base64.urlsafe_b64encode(f"{payload}||{sig}".encode()).decode()
    return token


def verify_state(token: str) -> Optional[str]:
    """Verify and decode a state token.  Returns the ``extra`` field or None on failure."""
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        payload, sig = decoded.rsplit("||", 1)
        expected = hmac.new(_state_secret(), payload.encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(sig, expected):
            return None
        return json.loads(payload).get("extra", "")
    except Exception:
        return None


# ── Authorization URL ──────────────────────────────────────────────────────────

def get_authorization_url(provider: str, extra_state: Optional[str] = None) -> str:
    """Build the provider's authorization URL for the redirect."""
    cfg = _PROVIDERS[provider]
    state = generate_state(extra_state)
    params = {
        "client_id":     cfg["client_id"](),
        "redirect_uri":  _redirect_uri(provider),
        "response_type": "code",
        "scope":         cfg["scopes"],
        "state":         state,
        "access_type":   "offline",   # Google: request refresh token
        "prompt":        "select_account",
    }
    if provider == "microsoft":
        params.pop("access_type", None)
        params.pop("prompt", None)
        params["response_mode"] = "query"
    from urllib.parse import urlencode
    return f"{cfg['auth_url']}?{urlencode(params)}"


# ── Token exchange ─────────────────────────────────────────────────────────────

async def exchange_code(provider: str, code: str) -> dict:
    """Exchange an authorization code for access/refresh tokens."""
    cfg = _PROVIDERS[provider]
    resp = await _http_client.post(cfg["token_url"], data={
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  _redirect_uri(provider),
        "client_id":     cfg["client_id"](),
        "client_secret": cfg["client_secret"](),
    })
    resp.raise_for_status()
    return resp.json()


async def get_userinfo(provider: str, access_token: str) -> dict:
    """Fetch user profile from the provider's userinfo endpoint."""
    cfg = _PROVIDERS[provider]
    resp = await _http_client.get(
        cfg["userinfo_url"],
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    return resp.json()


# ── Account upsert ─────────────────────────────────────────────────────────────

async def upsert_oauth_account(
    provider: str,
    token_data: dict,
    userinfo: dict,
) -> tuple[str, str]:
    """Find-or-create an Account for this OAuth identity.

    Returns ``(account_id, api_key_plaintext)``.
    The API key is only returned on *first* login (when creating a new account);
    on subsequent logins the existing key is re-used.
    """
    from app.db import AsyncSessionLocal
    from app.models.account import Account, ApiKey, OAuthAccount
    from sqlalchemy import select
    import bcrypt

    provider_user_id = str(
        userinfo.get("sub") or userinfo.get("id") or userinfo.get("oid") or ""
    )
    email = (
        userinfo.get("email")
        or userinfo.get("mail")
        or userinfo.get("userPrincipalName")
        or ""
    ).lower()

    if not provider_user_id:
        raise ValueError(f"Could not determine user ID from {provider} userinfo")

    access_token  = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    expires_in    = token_data.get("expires_in", 3600)
    token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

    async with AsyncSessionLocal() as session:
        # 1. Find existing OAuth link by provider + provider_user_id
        result = await session.execute(
            select(OAuthAccount).where(
                OAuthAccount.provider == provider,
                OAuthAccount.provider_user_id == provider_user_id,
            )
        )
        oauth_row: Optional[OAuthAccount] = result.scalar_one_or_none()

        if oauth_row:
            # Update tokens
            oauth_row.access_token     = access_token
            oauth_row.refresh_token    = refresh_token or oauth_row.refresh_token
            oauth_row.token_expires_at = token_expires_at
            account_id = oauth_row.account_id
            await session.commit()

            # Return any active API key (first one)
            result2 = await session.execute(
                select(ApiKey).where(ApiKey.account_id == account_id, ApiKey.is_active == True)
            )
            key_row = result2.scalars().first()
            return account_id, key_row.key if key_row else ""

        # 2. No existing OAuth link — check if an account with this email already exists
        account: Optional[Account] = None
        if email:
            result3 = await session.execute(select(Account).where(Account.email == email))
            account = result3.scalar_one_or_none()

        # 3. Create account if needed
        new_key_plaintext = ""
        if account is None:
            dummy_pw = bcrypt.hashpw(secrets.token_hex(32).encode(), bcrypt.gensalt()).decode()
            account = Account(email=email or f"{provider}:{provider_user_id}", hashed_password=dummy_pw)
            session.add(account)
            await session.flush()  # get account.id

            # Issue a new API key (round-3 fix #6: also persist key_prefix + key_hash)
            new_key_plaintext = f"sk_live_{''.join(secrets.token_urlsafe(30)[:40])}"
            from app.api.auth import api_key_storage_fields as _api_key_fields
            api_key = ApiKey(
                account_id=account.id,
                name="Default",
                **_api_key_fields(new_key_plaintext),
            )
            session.add(api_key)

        # 4. Create the OAuth link
        oauth_link = OAuthAccount(
            account_id=account.id,
            provider=provider,
            provider_user_id=provider_user_id,
            email=email,
            access_token=access_token,
            refresh_token=refresh_token,
            token_expires_at=token_expires_at,
        )
        session.add(oauth_link)
        await session.commit()

        if not new_key_plaintext:
            # Existing account — return their first active key
            result4 = await session.execute(
                select(ApiKey).where(ApiKey.account_id == account.id, ApiKey.is_active == True)
            )
            key_row2 = result4.scalars().first()
            new_key_plaintext = key_row2.key if key_row2 else ""

        return account.id, new_key_plaintext
