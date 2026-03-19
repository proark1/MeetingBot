"""Integration service for Slack and Notion.

Sends meeting summaries, action items, and transcripts to third-party
platforms when configured per-account.

Supported integration types:
  slack   — webhook URL; posts a rich Block Kit message
  notion  — API token + database ID; creates a page per meeting
"""

import asyncio
import json
import logging
from typing import Any, Optional

import httpx as _httpx

logger = logging.getLogger(__name__)
_http_client = _httpx.AsyncClient(timeout=15, follow_redirects=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _truncate(text: str, max_len: int = 2000) -> str:
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


# ── Slack ─────────────────────────────────────────────────────────────────────

def _build_slack_blocks(bot_data: dict) -> list[dict]:
    """Return Slack Block Kit blocks for a meeting-done notification."""
    platform = (bot_data.get("meeting_platform") or "meeting").replace("_", " ").title()
    duration = _format_duration(bot_data.get("duration_seconds"))
    participants = bot_data.get("participants") or []
    analysis = bot_data.get("analysis") or {}
    summary = analysis.get("summary") or "No summary available."
    action_items = analysis.get("action_items") or []
    decisions = analysis.get("decisions") or []
    status = bot_data.get("status", "done")
    status_emoji = "✅" if status == "done" else "⚠️"

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{status_emoji} Meeting recording complete"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Platform:*\n{platform}"},
                {"type": "mrkdwn", "text": f"*Duration:*\n{duration}"},
                {"type": "mrkdwn", "text": f"*Participants:*\n{len(participants)}"},
                {"type": "mrkdwn", "text": f"*Status:*\n{status}"},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Summary*\n{_truncate(summary, 1000)}"},
        },
    ]

    if action_items:
        items_text = "\n".join(
            f"• {ai.get('task', ai) if isinstance(ai, dict) else ai}"
            for ai in action_items[:10]
        )
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Action Items*\n{items_text}"},
        })

    if decisions:
        dec_text = "\n".join(f"• {d}" for d in decisions[:10])
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Decisions*\n{dec_text}"},
        })

    if participants:
        parts_text = ", ".join(participants[:8])
        if len(participants) > 8:
            parts_text += f" +{len(participants) - 8} more"
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"👥 {parts_text}"}],
        })

    return blocks


async def _post_to_slack(webhook_url: str, bot_data: dict) -> bool:
    """POST a Block Kit message to a Slack Incoming Webhook URL.

    Returns True on success, False on failure (never raises).
    """
    blocks = _build_slack_blocks(bot_data)
    payload = {
        "text": "Meeting recording complete",  # fallback text
        "blocks": blocks,
    }

    try:
        resp = await _http_client.post(webhook_url, json=payload)
        resp.raise_for_status()
        logger.info("Slack notification sent for bot %s", bot_data.get("bot_id"))
        return True
    except Exception as exc:
        logger.error("Slack notification failed for bot %s: %s", bot_data.get("bot_id"), exc)
        return False


# ── Notion ────────────────────────────────────────────────────────────────────

def _rich_text(content: str) -> list[dict]:
    """Build a Notion rich_text array from a plain string, chunked to 2000 chars."""
    chunks = [content[i:i + 2000] for i in range(0, len(content), 2000)] if content else [""]
    return [{"type": "text", "text": {"content": chunk}} for chunk in chunks[:100]]


def _build_notion_page(bot_data: dict, database_id: str) -> dict:
    """Build a Notion create-page request body."""
    platform = (bot_data.get("meeting_platform") or "meeting").replace("_", " ").title()
    analysis = bot_data.get("analysis") or {}
    summary = analysis.get("summary") or "No summary available."
    action_items = analysis.get("action_items") or []
    decisions = analysis.get("decisions") or []
    participants = bot_data.get("participants") or []
    duration = _format_duration(bot_data.get("duration_seconds"))
    transcript = bot_data.get("transcript") or []
    status = bot_data.get("status", "done")

    # Title: "Meeting — {platform} ({date})"
    from datetime import datetime, timezone
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title = f"Meeting — {platform} ({date_str})"

    # Build children blocks
    children: list[dict] = []

    # Summary section
    children.append({
        "object": "block", "type": "heading_2",
        "heading_2": {"rich_text": _rich_text("Summary")},
    })
    children.append({
        "object": "block", "type": "paragraph",
        "paragraph": {"rich_text": _rich_text(summary)},
    })

    # Action items
    if action_items:
        children.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _rich_text("Action Items")},
        })
        for ai in action_items[:50]:
            text = ai.get("task", ai) if isinstance(ai, dict) else str(ai)
            children.append({
                "object": "block", "type": "to_do",
                "to_do": {"rich_text": _rich_text(text), "checked": False},
            })

    # Decisions
    if decisions:
        children.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _rich_text("Decisions")},
        })
        for d in decisions[:50]:
            children.append({
                "object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _rich_text(str(d))},
            })

    # Transcript (first 50 entries)
    if transcript:
        children.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _rich_text("Transcript")},
        })
        for entry in transcript[:50]:
            speaker = entry.get("speaker", "Unknown")
            text = entry.get("text", "")
            children.append({
                "object": "block", "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "text": {"content": f"{speaker}: "}, "annotations": {"bold": True}},
                        {"type": "text", "text": {"content": _truncate(text, 1900)}},
                    ]
                },
            })

    properties: dict[str, Any] = {
        "Name": {"title": _rich_text(title)},
        "Platform": {"rich_text": _rich_text(platform)},
        "Duration": {"rich_text": _rich_text(duration)},
        "Status": {"rich_text": _rich_text(status)},
        "Participants": {"rich_text": _rich_text(", ".join(participants[:20]))},
    }

    return {
        "parent": {"database_id": database_id},
        "properties": properties,
        "children": children[:100],  # Notion max 100 blocks per request
    }


async def _post_to_notion(api_token: str, database_id: str, bot_data: dict) -> bool:
    """Create a Notion page in the given database.

    Returns True on success, False on failure (never raises).
    """
    page_body = _build_notion_page(bot_data, database_id)

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

    try:
        resp = await _http_client.post(
            "https://api.notion.com/v1/pages",
            headers=headers,
            json=page_body,
        )
        resp.raise_for_status()
        logger.info("Notion page created for bot %s", bot_data.get("bot_id"))
        return True
    except Exception as exc:
        logger.error("Notion integration failed for bot %s: %s", bot_data.get("bot_id"), exc)
        return False


# ── Public API ────────────────────────────────────────────────────────────────

async def dispatch_integrations(account_id: str, bot_data: dict) -> None:
    """Fire all active integrations for an account on meeting completion.

    Loads integrations from the database and dispatches in parallel.
    Silently absorbs all errors so a failing integration never breaks bot flow.
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

            if integration.type == "slack":
                webhook_url = config.get("webhook_url", "")
                if webhook_url:
                    tasks.append(_post_to_slack(webhook_url, bot_data))

            elif integration.type == "notion":
                api_token = config.get("api_token", "")
                database_id = config.get("database_id", "")
                if api_token and database_id:
                    tasks.append(_post_to_notion(api_token, database_id, bot_data))

            # CRM types are handled by crm_service.dispatch_crm_integrations
            # (called separately from bot_service._post_completion_notifications)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    except Exception as exc:
        logger.error("dispatch_integrations failed for account %s: %s", account_id, exc)
