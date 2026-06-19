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


class OAuthEmailNotVerifiedError(Exception):
    """Raised when an SSO identity's email can't be trusted to auto-link to an
    existing local account (prevents cross-provider account takeover)."""


def _legacy_plaintext_key(api_key) -> str:
    """Return a legacy plaintext key when one exists; hashed rows are one-shot."""
    return getattr(api_key, "key", None) or ""


async def _create_sso_api_key(session, account_id: str, name: str) -> str:
    from app.api.auth import api_key_storage_fields, generate_api_key
    from app.models.account import ApiKey

    key_value = generate_api_key(mode="live")
    session.add(ApiKey(
        account_id=account_id,
        name=name,
        **api_key_storage_fields(key_value),
    ))
    return key_value


def email_is_verified(provider: str, userinfo: dict) -> bool:
    """Whether the provider asserts this email address is verified.

    Linking an SSO identity to a *pre-existing* local account by email is only
    safe when the provider has verified the address — otherwise an attacker who
    controls an IdP (e.g. their own Azure tenant on the multi-tenant ``/common``
    endpoint) can mint an arbitrary email/UPN and take over the victim's account.

    * Google returns an ``email_verified`` claim on its userinfo endpoint.
    * Microsoft Graph ``/me`` exposes no verification claim, and ``/common``
      lets a tenant admin set any ``userPrincipalName``; treat as unverified.
    """
    if provider == "google":
        v = userinfo.get("email_verified")
        return v is True or str(v).strip().lower() == "true"
    # Microsoft (and any unknown provider): not provably verified.
    return False

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


_STATE_MAX_AGE_S = 600  # 10 minutes


def generate_state(extra: Optional[str] = None) -> str:
    """Return a signed state token.  ``extra`` is embedded in the payload.

    The payload now carries an ``iat`` (issued-at) timestamp so a captured
    state can't be replayed indefinitely. ``verify_state`` rejects tokens
    older than ``_STATE_MAX_AGE_S``.
    """
    import time
    payload = json.dumps({
        "nonce": secrets.token_urlsafe(16),
        "extra": extra or "",
        "iat": int(time.time()),
    })
    sig = hmac.new(_state_secret(), payload.encode(), hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(f"{payload}||{sig}".encode()).decode()
    return token


def verify_state(token: str) -> Optional[str]:
    """Verify and decode a state token.  Returns the ``extra`` field or None on failure.

    Rejects tokens whose ``iat`` is missing, malformed, or older than 10 min
    (defends against replay of captured state values).
    """
    import time
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        payload, sig = decoded.rsplit("||", 1)
        expected = hmac.new(_state_secret(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(payload)
        iat = data.get("iat")
        if not isinstance(iat, int):
            return None  # legacy tokens without iat — reject as well
        if abs(time.time() - iat) > _STATE_MAX_AGE_S:
            return None
        return data.get("extra", "")
    except Exception:
        return None


# ── Authorization URL ──────────────────────────────────────────────────────────

def get_authorization_url(
    provider: str,
    extra_state: Optional[str] = None,
    signed_state: Optional[str] = None,
) -> str:
    """Build the provider's authorization URL for the redirect.

    Pass ``signed_state`` to embed a state token already minted by the caller
    (so the same value can be bound to a browser cookie); otherwise a fresh
    one is generated from ``extra_state``.
    """
    cfg = _PROVIDERS[provider]
    state = signed_state or generate_state(extra_state)
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
            # Update tokens. Encrypt at rest — these grant access to the user's
            # Google/Microsoft account; any future reader must decrypt_text().
            from app.services.secrets_at_rest import encrypt_text as _enc
            oauth_row.access_token     = _enc(access_token)
            oauth_row.refresh_token    = _enc(refresh_token) if refresh_token else oauth_row.refresh_token
            oauth_row.token_expires_at = token_expires_at
            account_id = oauth_row.account_id
            await session.commit()

            # Return any active API key (first one)
            result2 = await session.execute(
                select(ApiKey).where(ApiKey.account_id == account_id, ApiKey.is_active == True)
            )
            key_row = result2.scalars().first()
            api_key_value = _legacy_plaintext_key(key_row)
            if not api_key_value:
                api_key_value = await _create_sso_api_key(session, account_id, f"{provider.title()} SSO")
                await session.commit()
            return account_id, api_key_value

        # 2. No existing OAuth link — check if an account with this email already exists.
        # Email is matched case-insensitively so a user who registered as
        # ``Foo@Bar.com`` is recognised by SSO that returns ``foo@bar.com``.
        normalized_email = (email or "").strip().lower()
        account: Optional[Account] = None
        if normalized_email:
            # Email column is stored lowercase post round-2 fix #8 (legacy
            # rows lowercased by a one-time startup backfill in db.py), so
            # direct equality uses the email index.
            result3 = await session.execute(
                select(Account).where(Account.email == normalized_email)
            )
            account = result3.scalar_one_or_none()

        # 2b. Account-takeover guard: only auto-link this SSO identity to a
        # pre-existing local account when the provider has *verified* the email.
        # Otherwise an attacker who controls an IdP could present an unverified
        # address matching a victim's account and log in as them.
        if account is not None and not email_is_verified(provider, userinfo):
            raise OAuthEmailNotVerifiedError(
                f"{provider} did not verify {normalized_email!r}; refusing to link "
                "to the existing account. Sign in with your password instead."
            )

        # 3. Create account if needed
        new_key_plaintext = ""
        if account is None:
            dummy_pw = bcrypt.hashpw(secrets.token_hex(32).encode(), bcrypt.gensalt()).decode()
            account = Account(email=normalized_email or f"{provider}:{provider_user_id}", hashed_password=dummy_pw)
            session.add(account)
            await session.flush()  # get account.id

            # Issue a new API key (round-3 fix #6: also persist key_prefix + key_hash)
            new_key_plaintext = await _create_sso_api_key(session, account.id, "Default")

        # 4. Create the OAuth link. Encrypt the SSO tokens at rest (a DB leak
        # otherwise exposes live Google/Microsoft credentials); readers must
        # round-trip through secrets_at_rest.decrypt_text().
        from app.services.secrets_at_rest import encrypt_text as _enc
        oauth_link = OAuthAccount(
            account_id=account.id,
            provider=provider,
            provider_user_id=provider_user_id,
            email=email,
            access_token=_enc(access_token),
            refresh_token=_enc(refresh_token),
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
            new_key_plaintext = _legacy_plaintext_key(key_row2)
            if not new_key_plaintext:
                new_key_plaintext = await _create_sso_api_key(session, account.id, f"{provider.title()} SSO")
                await session.commit()

        return account.id, new_key_plaintext
