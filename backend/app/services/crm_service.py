"""CRM integration service for HubSpot and Salesforce.

After a meeting completes, this service creates/updates CRM records:
- HubSpot: creates a Note engagement and optionally a Contact for each participant
- Salesforce: creates a Task/Activity record in the connected org

Integration config is stored in the `integrations` table with type "hubspot" or "salesforce".
Config JSON schema:
  HubSpot:     {"access_token": "...", "create_contacts": true}
  Salesforce:  {"instance_url": "...", "access_token": "...", "owner_id": "..."}
"""

import asyncio
import json
import logging
from typing import Any, Optional

import httpx as _httpx

logger = logging.getLogger(__name__)
_http_client = _httpx.AsyncClient(timeout=15, follow_redirects=True)


def _truncate(text: str, max_len: int = 4000) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "…"


def _format_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "unknown"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


# ── HubSpot ───────────────────────────────────────────────────────────────────

def _build_hubspot_note_body(bot_data: dict) -> str:
    """Build a plain-text note body for HubSpot."""
    analysis = bot_data.get("analysis") or {}
    platform = (bot_data.get("meeting_platform") or "meeting").replace("_", " ").title()
    duration = _format_duration(bot_data.get("duration_seconds"))
    participants = bot_data.get("participants") or []
    summary = analysis.get("summary") or "No summary available."
    action_items = analysis.get("action_items") or []
    decisions = analysis.get("decisions") or []

    lines = [
        f"Meeting Recording — {platform}",
        f"Duration: {duration}",
        f"Participants: {', '.join(participants[:20]) or 'N/A'}",
        "",
        "SUMMARY",
        summary,
    ]

    if action_items:
        lines += ["", "ACTION ITEMS"]
        for item in action_items[:20]:
            task = item.get("task", item) if isinstance(item, dict) else str(item)
            assignee = item.get("assignee", "") if isinstance(item, dict) else ""
            lines.append(f"• {task}" + (f" ({assignee})" if assignee else ""))

    if decisions:
        lines += ["", "DECISIONS"]
        for d in decisions[:10]:
            lines.append(f"• {d}")

    lines += ["", f"Bot ID: {bot_data.get('bot_id', '?')}"]
    return "\n".join(lines)


async def _post_to_hubspot(access_token: str, config: dict, bot_data: dict) -> bool:
    """Create a HubSpot Note engagement for the meeting.

    Returns True on success, False on failure (never raises).
    """
    import httpx

    note_body = _build_hubspot_note_body(bot_data)

    # HubSpot Engagements API v1 — create a NOTE
    payload: dict[str, Any] = {
        "engagement": {
            "active": True,
            "type": "NOTE",
        },
        "associations": {},
        "metadata": {
            "body": _truncate(note_body),
        },
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        resp = await _http_client.post(
            "https://api.hubapi.com/engagements/v1/engagements",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        engagement_id = resp.json().get("engagement", {}).get("id")
        logger.info(
            "HubSpot note created (engagement %s) for bot %s",
            engagement_id,
            bot_data.get("bot_id"),
        )
        return True
    except Exception as exc:
        logger.error("HubSpot integration failed for bot %s: %s", bot_data.get("bot_id"), exc)
        return False


# ── Salesforce ────────────────────────────────────────────────────────────────

def _build_salesforce_task(bot_data: dict) -> dict:
    """Build a Salesforce Task object from bot data."""
    analysis = bot_data.get("analysis") or {}
    platform = (bot_data.get("meeting_platform") or "meeting").replace("_", " ").title()
    duration = _format_duration(bot_data.get("duration_seconds"))
    participants = bot_data.get("participants") or []
    summary = analysis.get("summary") or "No summary available."

    subject = f"Meeting Recording — {platform} ({duration})"
    description_lines = [
        f"Platform: {platform}",
        f"Duration: {duration}",
        f"Participants: {', '.join(participants[:20]) or 'N/A'}",
        "",
        "Summary:",
        summary,
    ]

    action_items = analysis.get("action_items") or []
    if action_items:
        description_lines += ["", "Action Items:"]
        for item in action_items[:10]:
            task = item.get("task", item) if isinstance(item, dict) else str(item)
            description_lines.append(f"• {task}")

    return {
        "Subject": subject,
        "Description": _truncate("\n".join(description_lines), 32000),
        "Status": "Completed",
        "Priority": "Normal",
        "ActivityDate": None,  # will be set to today by caller
    }


async def _post_to_salesforce(instance_url: str, access_token: str, config: dict, bot_data: dict) -> bool:
    """Create a Salesforce Task for the meeting.

    Returns True on success, False on failure (never raises).
    """
    import httpx
    from datetime import date

    task = _build_salesforce_task(bot_data)
    task["ActivityDate"] = date.today().isoformat()

    # Optionally assign to a specific owner
    owner_id = config.get("owner_id")
    if owner_id:
        task["OwnerId"] = owner_id

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    url = f"{instance_url.rstrip('/')}/services/data/v59.0/sobjects/Task/"

    try:
        resp = await _http_client.post(url, headers=headers, json=task)
        resp.raise_for_status()
        task_id = resp.json().get("id")
        logger.info("Salesforce Task %s created for bot %s", task_id, bot_data.get("bot_id"))
        return True
    except Exception as exc:
        logger.error("Salesforce integration failed for bot %s: %s", bot_data.get("bot_id"), exc)
        return False


async def _get_salesforce_token(config: dict) -> tuple[str, str]:
    """Obtain a Salesforce access token via username-password OAuth flow.

    Returns (instance_url, access_token).
    """
    from app.config import settings

    # Prefer config-level credentials, fall back to global settings
    client_id = config.get("client_id") or settings.SALESFORCE_CLIENT_ID
    client_secret = config.get("client_secret") or settings.SALESFORCE_CLIENT_SECRET
    username = config.get("username") or settings.SALESFORCE_USERNAME
    password = config.get("password") or settings.SALESFORCE_PASSWORD
    security_token = config.get("security_token") or settings.SALESFORCE_SECURITY_TOKEN
    login_url = config.get("login_url", "https://login.salesforce.com")

    data = {
        "grant_type": "password",
        "client_id": client_id,
        "client_secret": client_secret,
        "username": username,
        "password": password + security_token,
    }

    resp = await _http_client.post(f"{login_url}/services/oauth2/token", data=data)
    resp.raise_for_status()
    result = resp.json()
    return result["instance_url"], result["access_token"]


# ── Public API ────────────────────────────────────────────────────────────────

async def dispatch_crm_integrations(account_id: str, bot_data: dict) -> None:
    """Fire HubSpot and Salesforce integrations for an account on meeting completion.

    Loads integrations from the database and dispatches in parallel.
    Silently absorbs all errors so a failing CRM never breaks bot flow.
    """
    try:
        from app.db import AsyncSessionLocal
        from app.models.account import Integration
        from sqlalchemy import select

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Integration).where(
                    Integration.account_id == account_id,
                    Integration.is_active == True,  # noqa: E712
                    Integration.type.in_(["hubspot", "salesforce"]),
                )
            )
            integrations = result.scalars().all()

        if not integrations:
            return

        tasks = []
        for integration in integrations:
            try:
                config = json.loads(integration.config or "{}")
            except Exception:
                config = {}

            if integration.type == "hubspot":
                access_token = config.get("access_token", "")
                if access_token:
                    tasks.append(_post_to_hubspot(access_token, config, bot_data))
                else:
                    logger.warning("HubSpot integration %s missing access_token", integration.id)

            elif integration.type == "salesforce":
                # Prefer stored access_token; fall back to username-password flow
                access_token = config.get("access_token", "")
                instance_url = config.get("instance_url", "")
                if not access_token:
                    try:
                        instance_url, access_token = await _get_salesforce_token(config)
                    except Exception as exc:
                        logger.error("Salesforce token fetch failed for integration %s: %s", integration.id, exc)
                        continue
                if instance_url and access_token:
                    tasks.append(_post_to_salesforce(instance_url, access_token, config, bot_data))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    except Exception as exc:
        logger.error("dispatch_crm_integrations failed for account %s: %s", account_id, exc)
