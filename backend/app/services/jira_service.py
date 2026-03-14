"""Jira integration — create issues for meeting action items."""

import base64
import logging

import httpx

logger = logging.getLogger(__name__)


async def push_action_items(bot) -> None:
    """Create a Jira task for each action item extracted from the meeting."""
    from app.config import settings

    if not settings.JIRA_BASE_URL or not settings.JIRA_API_TOKEN or not settings.JIRA_PROJECT_KEY:
        return

    analysis = bot.analysis or {}
    action_items = analysis.get("action_items", [])
    if not action_items:
        logger.info("No action items for bot %s — skipping Jira", bot.id)
        return

    base_url = settings.JIRA_BASE_URL.rstrip("/")
    credentials = base64.b64encode(
        f"{settings.JIRA_EMAIL}:{settings.JIRA_API_TOKEN}".encode()
    ).decode()
    headers = {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    share_url = ""
    if bot.share_token and settings.BASE_URL:
        share_url = f"{settings.BASE_URL.rstrip('/')}/share/{bot.share_token}"

    meeting_summary = (analysis.get("summary") or "")[:400]

    async with httpx.AsyncClient(timeout=20) as client:
        for item in action_items:
            task = (item.get("task") or "").strip()
            if not task:
                continue

            assignee_name = (item.get("assignee") or "").strip()
            due_raw = (item.get("due_date") or "").strip()

            # Build Atlassian Document Format description
            doc_content: list[dict] = [
                _adf_para(f"From meeting: {bot.meeting_url}"),
            ]
            if assignee_name:
                doc_content.append(_adf_para(f"Assignee: {assignee_name}"))
            if due_raw:
                doc_content.append(_adf_para(f"Due: {due_raw}"))
            if meeting_summary:
                doc_content.append(_adf_para(f"Meeting summary: {meeting_summary}"))
            if share_url:
                doc_content.append(_adf_para(f"Full report: {share_url}"))

            payload: dict = {
                "fields": {
                    "project": {"key": settings.JIRA_PROJECT_KEY},
                    "summary": task[:255],
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": doc_content,
                    },
                    "issuetype": {"name": "Task"},
                }
            }

            due_iso = _parse_date(due_raw)
            if due_iso:
                payload["fields"]["duedate"] = due_iso

            resp = await client.post(
                f"{base_url}/rest/api/3/issue",
                headers=headers,
                json=payload,
            )
            if resp.status_code == 400:
                # Some Jira instances don't support duedate — retry without it
                payload["fields"].pop("duedate", None)
                resp = await client.post(
                    f"{base_url}/rest/api/3/issue",
                    headers=headers,
                    json=payload,
                )
            resp.raise_for_status()
            data = resp.json()
            issue_key = data.get("key", "?")
            logger.info(
                "Jira issue created: %s — %s/browse/%s (bot %s)",
                issue_key, base_url, issue_key, bot.id,
            )


def _adf_para(text: str) -> dict:
    """Single paragraph node for Atlassian Document Format."""
    return {
        "type": "paragraph",
        "content": [{"type": "text", "text": str(text)[:2000]}],
    }


def _parse_date(raw: str) -> str | None:
    if not raw:
        return None
    from datetime import datetime
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y",
                "%B %d", "%b %d"):
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            if dt.year == 1900:
                dt = dt.replace(year=datetime.now().year)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None
