"""Post-meeting email summary via SMTP."""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def _fmt_duration(start, end) -> str:
    if not start or not end:
        return "—"
    secs = max(0, int((end - start).total_seconds()))
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s"


async def send_meeting_summary(bot) -> None:
    """Send a post-meeting summary email. No-op if SMTP is not configured."""
    from app.config import settings

    if not settings.SMTP_HOST or not bot.notify_email:
        return

    analysis = bot.analysis or {}
    duration = _fmt_duration(bot.started_at, bot.ended_at)
    participants = ", ".join(bot.participants or []) or "—"
    summary = analysis.get("summary", "No summary available.")

    action_items_html = ""
    for item in analysis.get("action_items", []):
        assignee = f" <em>(@{item.get('assignee', '')})</em>" if item.get("assignee") else ""
        action_items_html += f"<li>{item.get('task', '')}{assignee}</li>"

    decisions_html = "".join(f"<li>{d}</li>" for d in analysis.get("decisions", []))
    next_steps_html = "".join(f"<li>{s}</li>" for s in analysis.get("next_steps", []))

    share_link = ""
    if bot.share_token and settings.BASE_URL:
        share_link = f'<p><a href="{settings.BASE_URL}/share/{bot.share_token}">View full report →</a></p>'

    html = f"""
    <html><body style="font-family:sans-serif;max-width:600px;margin:0 auto;color:#1a1a2e">
    <h2 style="color:#4361ee">Meeting Summary — {bot.bot_name}</h2>
    <table style="width:100%;border-collapse:collapse;margin-bottom:1.5rem">
      <tr><td style="padding:4px 0;color:#666">Platform</td><td>{bot.meeting_platform.replace("_"," ").title()}</td></tr>
      <tr><td style="padding:4px 0;color:#666">Duration</td><td>{duration}</td></tr>
      <tr><td style="padding:4px 0;color:#666">Participants</td><td>{participants}</td></tr>
      <tr><td style="padding:4px 0;color:#666">Sentiment</td><td>{analysis.get("sentiment","neutral").title()}</td></tr>
    </table>
    <h3>Summary</h3><p>{summary}</p>
    {f'<h3>Action Items</h3><ul>{action_items_html}</ul>' if action_items_html else ''}
    {f'<h3>Decisions</h3><ul>{decisions_html}</ul>' if decisions_html else ''}
    {f'<h3>Next Steps</h3><ul>{next_steps_html}</ul>' if next_steps_html else ''}
    {share_link}
    <hr/><p style="font-size:0.8rem;color:#999">Sent by MeetingBot</p>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Meeting Summary: {bot.bot_name}"
    msg["From"] = settings.SMTP_FROM
    msg["To"] = bot.notify_email
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as smtp:
            smtp.ehlo()
            if settings.SMTP_PORT != 25:
                smtp.starttls()
            if settings.SMTP_USER and settings.SMTP_PASS:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASS)
            smtp.sendmail(settings.SMTP_FROM, [bot.notify_email], msg.as_string())
        logger.info("Meeting summary sent to %s for bot %s", bot.notify_email, bot.id)
    except Exception as exc:
        logger.error("Failed to send meeting summary email: %s", exc)
        raise
