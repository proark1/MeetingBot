"""Linear integration — create issues for meeting action items."""

import logging

import httpx

logger = logging.getLogger(__name__)

_LINEAR_API = "https://api.linear.app/graphql"

_CREATE_ISSUE = """
mutation CreateIssue($title: String!, $teamId: String!, $description: String, $dueDate: TimelessDate) {
  issueCreate(input: {
    title: $title
    teamId: $teamId
    description: $description
    dueDate: $dueDate
  }) {
    success
    issue { id identifier url title }
  }
}
"""


async def push_action_items(bot) -> None:
    """Create a Linear issue for each action item extracted from the meeting."""
    from app.config import settings

    if not settings.LINEAR_API_KEY or not settings.LINEAR_TEAM_ID:
        return

    analysis = bot.analysis or {}
    action_items = analysis.get("action_items", [])
    if not action_items:
        logger.info("No action items for bot %s — skipping Linear", bot.id)
        return

    share_url = ""
    if bot.share_token and settings.BASE_URL:
        share_url = f"{settings.BASE_URL.rstrip('/')}/share/{bot.share_token}"

    meeting_summary = (analysis.get("summary") or "")[:500]

    headers = {
        "Authorization": settings.LINEAR_API_KEY,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20) as client:
        for item in action_items:
            task = (item.get("task") or "").strip()
            if not task:
                continue

            assignee_name = item.get("assignee", "")
            due_raw = (item.get("due_date") or "").strip()

            description_parts = [
                f"**From meeting:** {bot.meeting_url}",
            ]
            if assignee_name:
                description_parts.append(f"**Assignee:** {assignee_name}")
            if due_raw:
                description_parts.append(f"**Due:** {due_raw}")
            if meeting_summary:
                description_parts.append(f"\n**Meeting summary:** {meeting_summary}")
            if share_url:
                description_parts.append(f"\n[View full meeting report]({share_url})")

            # Attempt to normalise due date to YYYY-MM-DD (Linear's TimelessDate)
            due_date_iso: str | None = _parse_date(due_raw)

            variables = {
                "title": task[:256],
                "teamId": settings.LINEAR_TEAM_ID,
                "description": "\n".join(description_parts)[:4000],
            }
            if due_date_iso:
                variables["dueDate"] = due_date_iso

            resp = await client.post(
                _LINEAR_API,
                headers=headers,
                json={"query": _CREATE_ISSUE, "variables": variables},
            )
            resp.raise_for_status()
            result = resp.json()
            errors = result.get("errors")
            if errors:
                logger.warning("Linear error for bot %s: %s", bot.id, errors)
                continue
            issue = (result.get("data") or {}).get("issueCreate", {}).get("issue") or {}
            if issue:
                logger.info(
                    "Linear issue created: %s — %s (bot %s)",
                    issue.get("identifier"), issue.get("url"), bot.id,
                )


def _parse_date(raw: str) -> str | None:
    """Try to parse a free-form date string into YYYY-MM-DD."""
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
