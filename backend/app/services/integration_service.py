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
# follow_redirects=False so a 302 from a user-supplied integration URL can't
# silently land us on an internal address (round-3 fix #2). The handful of
# trusted endpoints (Notion/Drive/Linear) don't redirect under normal conditions.
_http_client = _httpx.AsyncClient(timeout=15, follow_redirects=False)


async def _ssrf_blocked(url: str) -> "str | None":
    """Reject obviously-internal URLs before posting to a user-supplied target.
    Returns ``None`` if safe, or an error string to log + abort with."""
    try:
        from app.services.webhook_service import check_url_ssrf
    except Exception:  # pragma: no cover — defensive: never block on import error
        return None
    return await check_url_ssrf(url)


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

    blocked = await _ssrf_blocked(webhook_url)
    if blocked is not None:
        logger.warning("Slack integration blocked by SSRF guard  url=%s  reason=%s", webhook_url, blocked)
        return False

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


async def _post_to_google_drive(access_token: str, folder_id: Optional[str], bot_data: dict) -> bool:
    """Upload a Markdown meeting report to Google Drive.

    Uses Drive Files API v3 multipart upload. Returns True on success.
    """
    import httpx

    bot_id = bot_data.get("bot_id", "unknown")
    # Build markdown content
    try:
        from app.api.exports import _build_markdown
        # Build a minimal BotSession-like object from bot_data
        from app.store import BotSession, _now as _store_now
        from dataclasses import fields as _dc_fields
        # Use only fields that exist in both bot_data and BotSession
        session_kwargs: dict = {}
        for f in _dc_fields(BotSession):
            if f.name in bot_data:
                session_kwargs[f.name] = bot_data[f.name]
        # Required fields
        session_kwargs.setdefault("id", bot_id)
        session_kwargs.setdefault("meeting_url", bot_data.get("meeting_url", ""))
        session_kwargs.setdefault("meeting_platform", bot_data.get("meeting_platform", "unknown"))
        session_kwargs.setdefault("bot_name", "JustHereToListen.io")
        from datetime import datetime, timezone
        session_kwargs.setdefault("created_at", datetime.now(timezone.utc))
        session_kwargs.setdefault("updated_at", datetime.now(timezone.utc))
        fake_bot = BotSession(**session_kwargs)
        md_content = _build_markdown(fake_bot)
    except Exception as exc:
        logger.warning("Google Drive: markdown build failed: %s", exc)
        # Fallback minimal content
        analysis = bot_data.get("analysis") or {}
        md_content = f"# Meeting {bot_id}\n\n## Summary\n{analysis.get('summary', 'N/A')}\n"

    filename = f"Meeting-{bot_id[:8]}.md"
    metadata: dict = {"name": filename, "mimeType": "text/markdown"}
    if folder_id:
        metadata["parents"] = [folder_id]

    import json as _json
    boundary = "meeting_bot_boundary"
    body = (
        f"--{boundary}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{_json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: text/markdown\r\n\r\n"
        f"{md_content}\r\n"
        f"--{boundary}--"
    ).encode()

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": f"multipart/related; boundary={boundary}",
    }
    try:
        async with _httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
                headers=headers,
                content=body,
            )
            if resp.status_code in (200, 201):
                logger.info("Google Drive: uploaded %s", filename)
                return True
            else:
                logger.warning("Google Drive upload failed: %s %s", resp.status_code, resp.text[:200])
                return False
    except Exception as exc:
        logger.warning("Google Drive upload error: %s", exc)
        return False


async def _post_to_linear(api_key: str, team_id: str, bot_data: dict) -> bool:
    """Create Linear issues for each action item extracted from the meeting.

    Uses Linear's GraphQL API. Returns True on success, False on failure.
    """
    import httpx

    action_items = (bot_data.get("analysis") or {}).get("action_items", [])
    if not action_items:
        return True  # nothing to create

    summary = (bot_data.get("analysis") or {}).get("summary", "")
    bot_id = bot_data.get("bot_id", "")
    headers = {"Authorization": api_key, "Content-Type": "application/json"}
    url = "https://api.linear.app/graphql"
    successes = 0

    for item in action_items:
        task = item.get("task") or item if isinstance(item, str) else ""
        if not task:
            continue
        assignee_name = item.get("assignee", "") if isinstance(item, dict) else ""
        due = item.get("due_date", "") if isinstance(item, dict) else ""
        description = f"**From meeting:** {bot_id}\n\n{summary[:300]}"
        if assignee_name:
            description += f"\n\n**Assignee:** {assignee_name}"
        if due:
            description += f"\n**Due:** {due}"

        mutation = """
mutation CreateIssue($teamId: String!, $title: String!, $description: String) {
  issueCreate(input: {teamId: $teamId, title: $title, description: $description}) {
    success
    issue { id identifier title }
  }
}"""
        try:
            resp = await _http_client.post(url, headers=headers, json={
                "query": mutation,
                "variables": {"teamId": team_id, "title": task[:255], "description": description},
            })
            if resp.status_code == 200:
                successes += 1
        except Exception as exc:
            logger.warning("Linear issue creation failed: %s", exc)

    return successes > 0


async def _post_to_jira(base_url: str, token: str, email: str, project_key: str, bot_data: dict) -> bool:
    """Create Jira issues for each action item extracted from the meeting.

    Uses Jira REST API v3 with Basic Auth. Returns True on success, False on failure.
    """
    import httpx
    import base64

    action_items = (bot_data.get("analysis") or {}).get("action_items", [])
    if not action_items:
        return True

    summary = (bot_data.get("analysis") or {}).get("summary", "")
    bot_id = bot_data.get("bot_id", "")
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    api_url = base_url.rstrip("/") + "/rest/api/3/issue"
    blocked = await _ssrf_blocked(api_url)
    if blocked is not None:
        logger.warning("Jira integration blocked by SSRF guard  url=%s  reason=%s", api_url, blocked)
        return False
    successes = 0

    for item in action_items:
        task = item.get("task") or item if isinstance(item, str) else ""
        if not task:
            continue
        description_text = f"From meeting {bot_id}. {summary[:300]}"
        payload = {
            "fields": {
                "project": {"key": project_key},
                "summary": str(task)[:255],
                "description": {
                    "type": "doc", "version": 1,
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": description_text}]}],
                },
                "issuetype": {"name": "Task"},
            }
        }
        try:
            resp = await _http_client.post(api_url, headers=headers, json=payload)
            if resp.status_code in (200, 201):
                successes += 1
        except Exception as exc:
            logger.warning("Jira issue creation failed: %s", exc)

    return successes > 0


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

        from app.services.secrets_at_rest import decrypt_json
        tasks = []
        for integration in integrations:
            config = decrypt_json(integration.config)

            if integration.type == "slack":
                webhook_url = config.get("webhook_url", "")
                if webhook_url:
                    tasks.append(_post_to_slack(webhook_url, bot_data))

            elif integration.type == "notion":
                api_token = config.get("api_token", "")
                database_id = config.get("database_id", "")
                if api_token and database_id:
                    tasks.append(_post_to_notion(api_token, database_id, bot_data))

            elif integration.type == "linear":
                api_key = config.get("api_key", "")
                team_id = config.get("team_id", "")
                if api_key and team_id:
                    tasks.append(_post_to_linear(api_key, team_id, bot_data))

            elif integration.type == "jira":
                jira_url = config.get("base_url", "")
                jira_token = config.get("token", "")
                jira_email = config.get("email", "")
                project_key = config.get("project_key", "")
                if jira_url and jira_token and jira_email and project_key:
                    tasks.append(_post_to_jira(jira_url, jira_token, jira_email, project_key, bot_data))

            elif integration.type == "google_drive":
                access_token = config.get("access_token", "")
                folder_id = config.get("folder_id")
                if access_token:
                    tasks.append(_post_to_google_drive(access_token, folder_id, bot_data))

            # CRM types are handled by crm_service.dispatch_crm_integrations
            # (called separately from bot_service._post_completion_notifications)

        if tasks:
            _results = await asyncio.gather(*tasks, return_exceptions=True)
            for _r in _results:
                if isinstance(_r, Exception):
                    logger.warning("Integration task failed: %s", _r)

    except Exception as exc:
        logger.error("dispatch_integrations failed for account %s: %s", account_id, exc)
