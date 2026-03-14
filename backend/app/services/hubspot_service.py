"""HubSpot CRM integration — log meeting notes as engagements."""

import logging

import httpx

logger = logging.getLogger(__name__)

_HUBSPOT_API = "https://api.hubapi.com"


async def push_meeting_note(bot) -> None:
    """Create a HubSpot Note engagement with the meeting summary."""
    from app.config import settings

    if not settings.HUBSPOT_API_KEY:
        return

    analysis = bot.analysis or {}
    summary = analysis.get("summary", "No summary available")
    action_items = analysis.get("action_items", [])
    decisions = analysis.get("decisions", [])
    sentiment = analysis.get("sentiment", "neutral")
    participants = ", ".join(bot.participants or []) or "—"

    duration_min = 0
    if bot.started_at and bot.ended_at:
        duration_min = int((bot.ended_at - bot.started_at).total_seconds() / 60)

    share_url = ""
    if bot.share_token and settings.BASE_URL:
        share_url = f"{settings.BASE_URL.rstrip('/')}/share/{bot.share_token}"

    lines = [
        f"Meeting URL: {bot.meeting_url}",
        f"Platform: {(bot.meeting_platform or 'unknown').replace('_', ' ').title()}",
        f"Duration: {duration_min} min",
        f"Participants: {participants}",
        f"Sentiment: {sentiment.title()}",
        "",
        "Summary:",
        summary,
    ]
    if decisions:
        lines += ["", "Decisions:"] + [f"• {d}" for d in decisions]

    if action_items:
        lines += ["", "Action Items:"]
        for item in action_items:
            task = item.get("task", "")
            assignee = item.get("assignee", "")
            due = item.get("due_date", "")
            line = f"• {task}"
            if assignee:
                line += f" → {assignee}"
            if due:
                line += f" [due: {due}]"
            lines.append(line)

    if share_url:
        lines += ["", f"Full report: {share_url}"]

    note_body = "\n".join(lines)

    timestamp_ms = int(
        (bot.started_at or bot.created_at).timestamp() * 1000
    )

    # HubSpot Engagements API v1
    payload = {
        "engagement": {
            "active": True,
            "type": "NOTE",
            "timestamp": timestamp_ms,
        },
        "associations": {
            "contactIds": [],
            "companyIds": [],
            "dealIds": [],
            "ownerIds": [],
        },
        "metadata": {"body": note_body},
    }

    headers = {
        "Authorization": f"Bearer {settings.HUBSPOT_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{_HUBSPOT_API}/engagements/v1/engagements",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        eng_id = data.get("engagement", {}).get("id", "?")
        logger.info("HubSpot note created: engagement %s (bot %s)", eng_id, bot.id)
