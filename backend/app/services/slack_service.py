"""Post meeting summary to a Slack channel via Incoming Webhook."""

import logging
import httpx

logger = logging.getLogger(__name__)


def _fmt_duration(start, end) -> str:
    if not start or not end:
        return "—"
    secs = max(0, int((end - start).total_seconds()))
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s"


async def send_meeting_summary(bot, webhook_url: str) -> None:
    """Post a Slack message block to the configured webhook. No-op if url is empty."""
    if not webhook_url:
        return

    analysis = bot.analysis or {}
    duration = _fmt_duration(bot.started_at, bot.ended_at)
    participants = ", ".join(bot.participants or []) or "—"
    sentiment = analysis.get("sentiment", "neutral")
    sentiment_emoji = {"positive": "🟢", "neutral": "🔵", "negative": "🔴"}.get(sentiment, "🔵")
    summary = analysis.get("summary", "No summary available.")

    action_items = analysis.get("action_items", [])
    action_text = "\n".join(
        f"• {item.get('task', '')} {f'(@{item[\"assignee\"]})' if item.get('assignee') else ''}"
        for item in action_items[:5]
    ) or "_None identified_"

    share_link = ""
    from app.config import settings
    if bot.share_token and settings.BASE_URL:
        share_link = f"\n<{settings.BASE_URL}/share/{bot.share_token}|View full report →>"

    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"Meeting Summary — {bot.bot_name}"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Platform:* {bot.meeting_platform.replace('_', ' ').title()}"},
                    {"type": "mrkdwn", "text": f"*Duration:* {duration}"},
                    {"type": "mrkdwn", "text": f"*Participants:* {participants}"},
                    {"type": "mrkdwn", "text": f"*Sentiment:* {sentiment_emoji} {sentiment.title()}"},
                ],
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Summary*\n{summary}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Action Items*\n{action_text}"}},
        ]
    }
    if share_link:
        payload["blocks"].append(
            {"type": "section", "text": {"type": "mrkdwn", "text": share_link}}
        )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json=payload)
            resp.raise_for_status()
        logger.info("Slack summary sent for bot %s", bot.id)
    except Exception as exc:
        logger.error("Slack webhook failed for bot %s: %s", bot.id, exc)
        raise
