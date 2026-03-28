"""Google / Microsoft SSO via OAuth2 Authorization Code flow.

Endpoints:
  GET  /api/v1/auth/oauth/{provider}/authorize   → redirect to provider login
  GET  /api/v1/auth/oauth/{provider}/callback    → exchange code, return API key + JWT

``provider`` is one of: ``google``, ``microsoft``.

After successful login the endpoint returns JSON with:
  {
    "account_id":  "...",
    "email":       "...",
    "api_key":     "sk_live_...",   ← only non-empty on first login
    "access_token": "...",          ← short-lived JWT for the web UI
    "token_type":  "bearer"
  }

The web UI can also redirect to ``/`` with the JWT in a cookie set by the
callback endpoint (set ``?redirect=1`` to enable the cookie flow).
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import settings
from app.services.oauth_service import (
    get_authorization_url,
    exchange_code,
    get_userinfo,
    upsert_oauth_account,
    verify_state,
    _PROVIDERS,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/oauth", tags=["Auth — SSO"])


def _provider_or_404(provider: str) -> None:
    if provider not in _PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown OAuth provider: {provider!r}")

    client_id = _PROVIDERS[provider]["client_id"]()
    if not client_id:
        raise HTTPException(
            status_code=503,
            detail=f"SSO for {provider!r} is not configured on this server. "
                   f"Set {provider.upper()}_CLIENT_ID and {provider.upper()}_CLIENT_SECRET.",
        )


# ── GET /auth/oauth/{provider}/authorize ──────────────────────────────────────

@router.get("/{provider}/authorize", include_in_schema=True)
async def authorize(
    provider: str,
    redirect: Optional[str] = Query(
        default=None,
        description="Set to '1' to redirect back to the web UI with a session cookie after login.",
    ),
):
    """Redirect the user to the OAuth2 provider login page.

    After the user grants consent, the provider redirects to
    `GET /api/v1/auth/oauth/{provider}/callback`.

    Pass `?redirect=1` if you want the callback to set a session cookie and
    redirect to the web UI instead of returning JSON.
    """
    _provider_or_404(provider)
    url = get_authorization_url(provider, extra_state=redirect or "")
    return RedirectResponse(url=url, status_code=302)


# ── GET /auth/oauth/{provider}/callback ───────────────────────────────────────

@router.get("/{provider}/callback", include_in_schema=True)
async def callback(
    provider: str,
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
    error_description: Optional[str] = Query(default=None),
):
    """Handle the OAuth2 callback from the provider.

    On success returns:
    ```json
    {
      "account_id": "...",
      "email": "...",
      "api_key": "sk_live_...",
      "access_token": "<JWT for web UI>",
      "token_type": "bearer",
      "is_new_account": true
    }
    ```

    `api_key` is the `sk_live_...` key for API calls.
    `access_token` is a short-lived JWT for the web UI (same as `POST /auth/login`).
    On subsequent logins `api_key` will be empty — use your existing key.
    """
    _provider_or_404(provider)

    # Provider-reported error
    if error:
        raise HTTPException(
            status_code=400,
            detail=f"OAuth error from {provider}: {error}. {error_description or ''}",
        )

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    # Verify CSRF state — always required (generated on authorize redirect)
    if not state:
        raise HTTPException(status_code=400, detail="Missing OAuth state parameter")
    extra = verify_state(state)
    if extra is None:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state token")

    # Exchange code for tokens
    try:
        token_data = await exchange_code(provider, code)
    except Exception as exc:
        logger.error("OAuth token exchange failed for %s: %s", provider, exc)
        raise HTTPException(status_code=502, detail=f"Token exchange failed: {exc}")

    access_token_provider = token_data.get("access_token", "")

    # Fetch user info
    try:
        userinfo = await get_userinfo(provider, access_token_provider)
    except Exception as exc:
        logger.error("OAuth userinfo fetch failed for %s: %s", provider, exc)
        raise HTTPException(status_code=502, detail=f"User info fetch failed: {exc}")

    # Upsert account
    try:
        account_id, api_key = await upsert_oauth_account(provider, token_data, userinfo)
    except Exception as exc:
        logger.error("OAuth account upsert failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Account creation failed: {exc}")

    email = (
        userinfo.get("email")
        or userinfo.get("mail")
        or userinfo.get("userPrincipalName")
        or ""
    )
    is_new = bool(api_key)  # non-empty only on first login

    # Issue a web-UI JWT
    from app.api.auth import _create_jwt
    jwt_token = _create_jwt(account_id)

    response_body = {
        "account_id":    account_id,
        "email":         email,
        "provider":      provider,
        "api_key":       api_key,
        "access_token":  jwt_token,
        "token_type":    "bearer",
        "is_new_account": is_new,
    }

    # Cookie + redirect mode (web UI)
    if extra == "1":
        resp = RedirectResponse(url="/", status_code=302)
        resp.set_cookie(
            key="mb_token",
            value=jwt_token,
            httponly=True,
            samesite="lax",
            max_age=settings.JWT_EXPIRE_HOURS * 3600,
        )
        return resp

    return JSONResponse(content=response_body)
