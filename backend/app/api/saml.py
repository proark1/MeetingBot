"""SAML 2.0 SSO API.

Provides enterprise Single Sign-On via SAML 2.0.
Organisations register their IdP metadata via the admin panel.
Users then authenticate at /api/v1/auth/saml/{org_slug}/authorize.

Flow:
1. GET  /api/v1/auth/saml/{org_slug}/authorize   → redirects to IdP login
2. POST /api/v1/auth/saml/{org_slug}/callback     → IdP posts SAML response here
3. Validates assertion, provisions/finds account, returns JWT cookie + API key

SP metadata: GET /api/v1/auth/saml/{org_slug}/metadata

Requires SAML_ENABLED=true and the python3-saml or pysaml2 package.
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/saml", tags=["SSO — SAML"])


def _saml_enabled() -> bool:
    return settings.SAML_ENABLED


class SamlConfigCreate(BaseModel):
    org_name: str
    idp_metadata_url: Optional[str] = None
    idp_metadata_xml: Optional[str] = None
    attribute_mapping: dict = {"email": "email", "first_name": "givenName", "last_name": "sn"}
    workspace_id: Optional[str] = None
    default_role: str = "member"


async def _load_saml_config(org_slug: str, db):
    """Load SAML config for the given org slug."""
    from app.models.account import SamlConfig
    from sqlalchemy import select

    result = await db.execute(
        select(SamlConfig).where(
            SamlConfig.org_slug == org_slug,
            SamlConfig.is_active == True,  # noqa
        )
    )
    cfg = result.scalar_one_or_none()
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"SAML organisation {org_slug!r} not found or inactive")
    return cfg


def _build_sp_settings(org_slug: str, cfg) -> dict:
    """Build the python3-saml settings dict for the Service Provider."""
    base_url = settings.SAML_SP_BASE_URL.rstrip("/")
    acs_url = f"{base_url}/api/v1/auth/saml/{org_slug}/callback"
    entity_id = f"{base_url}/api/v1/auth/saml/{org_slug}/metadata"

    sp_settings: dict = {
        "strict": True,
        "debug": False,
        "sp": {
            "entityId": entity_id,
            "assertionConsumerService": {
                "url": acs_url,
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
        },
        "idp": {
            "entityId": "",
            "singleSignOnService": {
                "url": "",
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "x509cert": "",
        },
    }
    return sp_settings


async def _fetch_idp_metadata(url: str) -> str:
    """Fetch IdP metadata XML from a URL."""
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


def _extract_email_from_assertion(auth, attribute_mapping: dict) -> Optional[str]:
    """Extract email from SAML attributes using the configured mapping."""
    attrs = auth.get_attributes()
    email_attr = attribute_mapping.get("email", "email")
    # Try direct attribute name
    if email_attr in attrs:
        values = attrs[email_attr]
        return values[0] if values else None
    # Try NameID as fallback
    nameid = auth.get_nameid()
    if nameid and "@" in nameid:
        return nameid
    return None


@router.get("/{org_slug}/metadata", summary="SP metadata")
async def saml_sp_metadata(org_slug: str):
    """Return the SAML Service Provider metadata XML for this org.

    Share this URL with your Identity Provider when configuring the SP.
    """
    if not _saml_enabled():
        raise HTTPException(status_code=501, detail="SAML SSO is not enabled (set SAML_ENABLED=true)")

    try:
        from onelogin.saml2.settings import OneLogin_Saml2_Settings
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="python3-saml not installed — run: pip install python3-saml",
        )

    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        cfg = await _load_saml_config(org_slug, db)

    sp_settings = _build_sp_settings(org_slug, cfg)
    saml_settings = OneLogin_Saml2_Settings(settings=sp_settings, sp_validation_only=True)
    metadata = saml_settings.get_sp_metadata()

    return Response(content=metadata, media_type="application/xml")


@router.get("/{org_slug}/authorize", summary="Initiate SAML login")
async def saml_authorize(org_slug: str, request: Request, redirect: bool = False):
    """Redirect the user to the IdP for SAML authentication.

    Pass `?redirect=1` to use cookie-based flow for the web UI.
    """
    if not _saml_enabled():
        raise HTTPException(status_code=501, detail="SAML SSO is not enabled")

    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
        from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="python3-saml not installed — run: pip install python3-saml",
        )

    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        cfg = await _load_saml_config(org_slug, db)

    sp_settings = _build_sp_settings(org_slug, cfg)

    # Merge IdP settings from metadata
    try:
        if cfg.idp_metadata_url:
            idp_data = OneLogin_Saml2_IdPMetadataParser.parse_remote(cfg.idp_metadata_url)
        elif cfg.idp_metadata_xml:
            idp_data = OneLogin_Saml2_IdPMetadataParser.parse(cfg.idp_metadata_xml)
        else:
            raise HTTPException(status_code=500, detail="SAML config has no IdP metadata")
        sp_settings = OneLogin_Saml2_IdPMetadataParser.merge_settings(sp_settings, idp_data)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("SAML metadata error for %s: %s", org_slug, exc)
        raise HTTPException(status_code=500, detail=f"Failed to load IdP metadata: {exc}")

    # Build request object expected by python3-saml
    req = {
        "https": "on" if request.url.scheme == "https" else "off",
        "http_host": request.url.hostname,
        "script_name": request.url.path,
        "get_data": dict(request.query_params),
        "post_data": {},
    }

    auth = OneLogin_Saml2_Auth(req, sp_settings)
    sso_url = auth.login()
    return RedirectResponse(url=sso_url)


@router.post("/{org_slug}/callback", summary="SAML callback")
async def saml_callback(org_slug: str, request: Request):
    """Handle SAML assertion from the IdP.

    Creates or retrieves the user account and returns a JWT + API key.
    """
    if not _saml_enabled():
        raise HTTPException(status_code=501, detail="SAML SSO is not enabled")

    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
        from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="python3-saml not installed — run: pip install python3-saml",
        )

    from app.db import AsyncSessionLocal

    form_data = await request.form()
    post_data = {k: v for k, v in form_data.items()}

    async with AsyncSessionLocal() as db:
        cfg = await _load_saml_config(org_slug, db)

        sp_settings = _build_sp_settings(org_slug, cfg)
        try:
            if cfg.idp_metadata_url:
                idp_data = OneLogin_Saml2_IdPMetadataParser.parse_remote(cfg.idp_metadata_url)
            elif cfg.idp_metadata_xml:
                idp_data = OneLogin_Saml2_IdPMetadataParser.parse(cfg.idp_metadata_xml)
            else:
                raise HTTPException(status_code=500, detail="SAML config has no IdP metadata")
            sp_settings = OneLogin_Saml2_IdPMetadataParser.merge_settings(sp_settings, idp_data)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to load IdP metadata: {exc}")

        req = {
            "https": "on" if request.url.scheme == "https" else "off",
            "http_host": request.url.hostname,
            "script_name": request.url.path,
            "get_data": dict(request.query_params),
            "post_data": post_data,
        }

        auth = OneLogin_Saml2_Auth(req, sp_settings)
        auth.process_response()

        errors = auth.get_errors()
        if errors:
            logger.error("SAML assertion errors for %s: %s", org_slug, errors)
            raise HTTPException(status_code=401, detail=f"SAML validation failed: {errors}")

        if not auth.is_authenticated():
            raise HTTPException(status_code=401, detail="SAML authentication failed")

        try:
            attribute_mapping = json.loads(cfg.attribute_mapping or '{"email": "email"}')
        except Exception:
            attribute_mapping = {"email": "email"}

        email = _extract_email_from_assertion(auth, attribute_mapping)
        if not email:
            raise HTTPException(status_code=400, detail="Could not extract email from SAML assertion")

        # Provision or find the account
        from app.models.account import Account, ApiKey
        from sqlalchemy import select
        import secrets as _sec
        import bcrypt

        result = await db.execute(select(Account).where(Account.email == email))
        account = result.scalar_one_or_none()
        is_new = False

        if account is None:
            is_new = True
            hashed_pw = bcrypt.hashpw(_sec.token_hex(32).encode(), bcrypt.gensalt()).decode()
            account = Account(email=email, hashed_password=hashed_pw)
            db.add(account)
            await db.flush()

        # Generate API key for new accounts (round-3 fix #6: also persist key_prefix + key_hash)
        api_key_val = ""
        if is_new:
            api_key_val = f"sk_live_{_sec.token_hex(32)}"
            from app.api.auth import api_key_storage_fields as _api_key_fields
            api_key = ApiKey(
                account_id=account.id,
                name="SAML SSO",
                **_api_key_fields(api_key_val),
            )
            db.add(api_key)

        await db.commit()

        # Issue JWT
        from app.api.auth import _create_jwt
        access_token = _create_jwt(account.id)

    return {
        "account_id": account.id,
        "email": email,
        "api_key": api_key_val,
        "access_token": access_token,
        "is_new_account": is_new,
        "org_slug": org_slug,
    }


# ── Admin endpoints for SAML config management ────────────────────────────────

class SamlConfigResponse(BaseModel):
    id: str
    org_slug: str
    org_name: str
    idp_metadata_url: Optional[str] = None
    attribute_mapping: dict
    workspace_id: Optional[str] = None
    default_role: str
    is_active: bool
    created_at: str


@router.get("/configs", summary="List SAML configs (admin)")
async def list_saml_configs(request: Request):
    """List all SAML organisation configurations. Admin only."""
    # Inline admin check (same logic as require_admin dependency)
    account_id = getattr(request.state, "account_id", None)
    if not account_id:
        raise HTTPException(status_code=403, detail="Admin access required")
    from app.db import AsyncSessionLocal
    from app.models.account import Account
    from sqlalchemy import select as _sel
    from app.deps import SUPERADMIN_ACCOUNT_ID, _admin_emails
    if account_id != SUPERADMIN_ACCOUNT_ID:
        async with AsyncSessionLocal() as _db:
            _res = await _db.execute(_sel(Account).where(Account.id == account_id))
            _acc = _res.scalar_one_or_none()
        if not _acc or (not _acc.is_admin and _acc.email.lower() not in _admin_emails()):
            raise HTTPException(status_code=403, detail="Admin access required")

    from app.db import AsyncSessionLocal
    from app.models.account import SamlConfig
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(SamlConfig).order_by(SamlConfig.created_at.desc()))
        rows = result.scalars().all()

    return [
        {
            "id": r.id,
            "org_slug": r.org_slug,
            "org_name": r.org_name,
            "idp_metadata_url": r.idp_metadata_url,
            "attribute_mapping": json.loads(r.attribute_mapping or "{}"),
            "workspace_id": r.workspace_id,
            "default_role": r.default_role,
            "is_active": r.is_active,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.post("/configs", status_code=201, summary="Create SAML config (admin)")
async def create_saml_config(payload: SamlConfigCreate, request: Request):
    """Register a new SAML organisation. Admin only."""
    # Inline admin check (same logic as require_admin dependency)
    account_id = getattr(request.state, "account_id", None)
    if not account_id:
        raise HTTPException(status_code=403, detail="Admin access required")
    from app.db import AsyncSessionLocal
    from app.models.account import Account
    from sqlalchemy import select as _sel
    from app.deps import SUPERADMIN_ACCOUNT_ID, _admin_emails
    if account_id != SUPERADMIN_ACCOUNT_ID:
        async with AsyncSessionLocal() as _db:
            _res = await _db.execute(_sel(Account).where(Account.id == account_id))
            _acc = _res.scalar_one_or_none()
        if not _acc or (not _acc.is_admin and _acc.email.lower() not in _admin_emails()):
            raise HTTPException(status_code=403, detail="Admin access required")

    if not _saml_enabled():
        raise HTTPException(status_code=501, detail="SAML SSO is not enabled (set SAML_ENABLED=true)")

    import re

    org_slug = re.sub(r"[^\w-]", "-", payload.org_name.lower()).strip("-") or "org"

    from app.db import AsyncSessionLocal
    from app.models.account import SamlConfig
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        existing = await db.execute(select(SamlConfig).where(SamlConfig.org_slug == org_slug))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail=f"SAML config for slug '{org_slug}' already exists")

        cfg = SamlConfig(
            org_slug=org_slug,
            org_name=payload.org_name,
            idp_metadata_url=payload.idp_metadata_url,
            idp_metadata_xml=payload.idp_metadata_xml,
            attribute_mapping=json.dumps(payload.attribute_mapping),
            workspace_id=payload.workspace_id,
            default_role=payload.default_role,
        )
        db.add(cfg)
        await db.commit()
        await db.refresh(cfg)

    return {
        "id": cfg.id,
        "org_slug": cfg.org_slug,
        "org_name": cfg.org_name,
        "is_active": cfg.is_active,
        "created_at": cfg.created_at.isoformat(),
    }
