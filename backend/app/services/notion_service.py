"""Notion integration — push meeting summaries to a Notion database."""

import logging

import httpx

logger = logging.getLogger(__name__)

_NOTION_API = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"


async def push_meeting(bot) -> None:
    """Create a Notion page in the configured database with the meeting summary."""
    from app.config import settings

    if not settings.NOTION_API_KEY or not settings.NOTION_DATABASE_ID:
        return

    analysis = bot.analysis or {}
    summary = analysis.get("summary", "No summary available")
    action_items = analysis.get("action_items", [])
    decisions = analysis.get("decisions", [])
    next_steps = analysis.get("next_steps", [])
    sentiment = analysis.get("sentiment", "neutral")
    participants = ", ".join(bot.participants or [])

    duration_min = 0
    if bot.started_at and bot.ended_at:
        duration_min = int((bot.ended_at - bot.started_at).total_seconds() / 60)

    share_url = ""
    if bot.share_token and settings.BASE_URL:
        share_url = f"{settings.BASE_URL.rstrip('/')}/share/{bot.share_token}"

    meeting_title = f"Meeting — {(bot.meeting_url or '')[:60]}"
    if bot.bot_name and bot.bot_name != "MeetingBot":
        meeting_title = f"Meeting — {bot.bot_name}"

    # Page content blocks
    children: list[dict] = [_heading2("Summary"), _paragraph(summary)]

    if decisions:
        children.append(_heading2("Decisions"))
        children += [_bullet(d) for d in decisions]

    if action_items:
        children.append(_heading2("Action Items"))
        for item in action_items:
            task = item.get("task", "")
            assignee = item.get("assignee", "")
            due = item.get("due_date", "")
            parts = [task]
            if assignee:
                parts.append(f"→ {assignee}")
            if due:
                parts.append(f"[due: {due}]")
            children.append(_bullet(" ".join(parts)))

    if next_steps:
        children.append(_heading2("Next Steps"))
        children += [_bullet(s) for s in next_steps]

    if share_url:
        children.append(_paragraph(f"Full report: {share_url}"))

    # Properties depend on what columns exist in the database —
    # only send the ones the Notion template guarantees.
    properties: dict = {
        "Name": {"title": [{"text": {"content": meeting_title[:250]}}]},
    }

    # Optional columns — users can add these to their Notion DB
    try:
        properties["Status"] = {"select": {"name": "Done"}}
        properties["Sentiment"] = {"select": {"name": sentiment.capitalize()}}
        properties["Platform"] = {
            "select": {"name": (bot.meeting_platform or "unknown").replace("_", " ").title()}
        }
        properties["Duration (min)"] = {"number": duration_min}
        if participants:
            properties["Participants"] = {
                "rich_text": [{"text": {"content": participants[:2000]}}]
            }
        date_val = (bot.started_at or bot.created_at)
        if date_val:
            properties["Date"] = {"date": {"start": date_val.isoformat()}}
    except Exception:
        pass  # extra properties are best-effort

    payload = {
        "parent": {"database_id": settings.NOTION_DATABASE_ID},
        "properties": properties,
        "children": children,
    }

    headers = {
        "Authorization": f"Bearer {settings.NOTION_API_KEY}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(f"{_NOTION_API}/pages", headers=headers, json=payload)
        resp.raise_for_status()
        page = resp.json()
        logger.info("Notion page created: %s (bot %s)", page.get("url", "?"), bot.id)


# ── Block helpers ──────────────────────────────────────────────────────────────

def _rich_text(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": str(text)[:2000]}}]


def _heading2(text: str) -> dict:
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _rich_text(text)}}


def _paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _rich_text(text)}}


def _bullet(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": _rich_text(text)}}
