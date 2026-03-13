"""Weekly meeting digest email.

Sends a summary of the past 7 days' meetings to all addresses in DIGEST_EMAIL.
Triggered every Monday at 09:00 UTC by APScheduler (registered in main.py).
"""

import asyncio
import html
import logging
import smtplib
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from sqlalchemy import select
from sqlalchemy.orm import defer

from app.models.action_item import ActionItem
from app.models.bot import Bot

logger = logging.getLogger(__name__)


def _fmt_duration_s(secs: int) -> str:
    if secs <= 0:
        return "—"
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s"


async def send_weekly_digest(db_factory) -> None:
    """Aggregate last 7 days of meetings and email a digest to DIGEST_EMAIL recipients."""
    from app.config import settings

    if not settings.DIGEST_EMAIL:
        return
    if not settings.SMTP_HOST:
        logger.warning("DIGEST_EMAIL is set but SMTP_HOST is not configured — skipping digest")
        return

    recipients = [r.strip() for r in settings.DIGEST_EMAIL.split(",") if r.strip()]
    if not recipients:
        return

    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    async with db_factory() as db:
        # ── Query last 7 days of completed meetings (no heavy columns) ─────────
        rows = (
            await db.execute(
                select(Bot)
                .options(
                    defer(Bot.transcript),
                    defer(Bot.chapters),
                    defer(Bot.speaker_stats),
                    defer(Bot.vocabulary),
                )
                .where(Bot.status == "done")
                .where(Bot.created_at >= week_ago)
                .order_by(Bot.created_at.desc())
            )
        ).scalars().all()

        if not rows:
            logger.info("Weekly digest: no completed meetings in the last 7 days — skipping")
            return

        # ── Query all overdue action items (due_date < today, not done) ────────
        today_iso = now.date().isoformat()  # e.g. "2025-01-20"
        overdue_rows = (
            await db.execute(
                select(ActionItem)
                .where(ActionItem.done.is_(False))
                .where(ActionItem.due_date.isnot(None))
                .where(ActionItem.due_date < today_iso)
                .order_by(ActionItem.due_date)
                .limit(20)
            )
        ).scalars().all()

    # ── Aggregate ──────────────────────────────────────────────────────────────
    total_meetings = len(rows)

    total_secs = sum(
        max(0, int((b.ended_at - b.started_at).total_seconds()))
        for b in rows
        if b.started_at and b.ended_at
    )

    sentiment_counts: dict[str, int] = {"positive": 0, "neutral": 0, "negative": 0}
    topic_counter: Counter = Counter()
    participant_counter: Counter = Counter()

    for b in rows:
        analysis = b.analysis or {}
        s = analysis.get("sentiment", "neutral")
        if s in sentiment_counts:
            sentiment_counts[s] += 1
        for t in analysis.get("topics", []):
            topic_counter[t.lower().strip()] += 1
        for p in (b.participants or []):
            participant_counter[p.strip()] += 1

    top_topics = topic_counter.most_common(5)
    top_participants = participant_counter.most_common(5)

    # ── Build HTML ─────────────────────────────────────────────────────────────
    week_label = week_ago.strftime("%b %-d") + " – " + now.strftime("%b %-d, %Y")

    sentiment_bar = (
        f'<span style="color:#2ecc71">▲ {sentiment_counts["positive"]} positive</span> &nbsp;'
        f'<span style="color:#999">● {sentiment_counts["neutral"]} neutral</span> &nbsp;'
        f'<span style="color:#e74c3c">▼ {sentiment_counts["negative"]} negative</span>'
    )

    topics_html = ""
    if top_topics:
        topics_html = "<ul>" + "".join(
            f"<li>{html.escape(t)} <em>({c}×)</em></li>" for t, c in top_topics
        ) + "</ul>"

    participants_html = ""
    if top_participants:
        participants_html = "<ul>" + "".join(
            f"<li>{html.escape(p)} <em>({c} meetings)</em></li>" for p, c in top_participants
        ) + "</ul>"

    overdue_html = ""
    if overdue_rows:
        items = "".join(
            f"<li>{html.escape(ai.task)}"
            f"{' <em>(@' + html.escape(ai.assignee) + ')</em>' if ai.assignee else ''}"
            f" <span style='color:#e74c3c'>due {html.escape(ai.due_date or '')}</span></li>"
            for ai in overdue_rows
        )
        overdue_html = f"<h3 style='color:#e74c3c'>⚠ Overdue Action Items</h3><ul>{items}</ul>"

    recent_meetings_html = "<ul>" + "".join(
        f"<li><strong>{html.escape(b.meeting_platform.replace('_', ' ').title())}</strong>"
        f" — {html.escape((b.analysis or {}).get('summary', '')[:100])}…</li>"
        for b in rows[:5]
    ) + "</ul>"

    dashboard_link = ""
    if settings.BASE_URL:
        dashboard_link = f'<p><a href="{html.escape(settings.BASE_URL)}">Open MeetingBot Dashboard →</a></p>'

    body_html = f"""
    <html><body style="font-family:sans-serif;max-width:640px;margin:0 auto;color:#1a1a2e">
    <h2 style="color:#4361ee">📅 Weekly Meeting Digest</h2>
    <p style="color:#666">{html.escape(week_label)}</p>

    <table style="width:100%;border-collapse:collapse;margin-bottom:1.5rem;background:#f8f9ff;border-radius:8px">
      <tr>
        <td style="padding:12px 16px;text-align:center">
          <div style="font-size:2rem;font-weight:700;color:#4361ee">{total_meetings}</div>
          <div style="color:#666;font-size:0.85rem">Meetings</div>
        </td>
        <td style="padding:12px 16px;text-align:center">
          <div style="font-size:2rem;font-weight:700;color:#4361ee">{html.escape(_fmt_duration_s(total_secs))}</div>
          <div style="color:#666;font-size:0.85rem">Total Time</div>
        </td>
        <td style="padding:12px 16px;text-align:center">
          <div style="font-size:2rem;font-weight:700;color:#e74c3c">{len(overdue_rows)}</div>
          <div style="color:#666;font-size:0.85rem">Overdue Items</div>
        </td>
      </tr>
    </table>

    <p>{sentiment_bar}</p>

    {f'<h3>🔥 Top Topics</h3>{topics_html}' if topics_html else ''}
    {f'<h3>👥 Most Active Participants</h3>{participants_html}' if participants_html else ''}
    {overdue_html}
    <h3>Recent Meetings</h3>{recent_meetings_html}
    {dashboard_link}
    <hr/><p style="font-size:0.8rem;color:#999">Weekly digest from MeetingBot — every Monday at 09:00 UTC</p>
    </body></html>
    """

    # ── Send to all recipients ─────────────────────────────────────────────────
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"MeetingBot Weekly Digest — {total_meetings} meetings, {_fmt_duration_s(total_secs)}"
    msg["From"] = settings.SMTP_FROM
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(body_html, "html"))

    def _send() -> None:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as smtp:
            smtp.ehlo()
            if settings.SMTP_PORT != 25:
                smtp.starttls()
            if settings.SMTP_USER and settings.SMTP_PASS:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASS)
            smtp.sendmail(settings.SMTP_FROM, recipients, msg.as_string())

    try:
        await asyncio.to_thread(_send)
        logger.info(
            "Weekly digest sent to %d recipient(s): %d meetings, %d overdue items",
            len(recipients), total_meetings, len(overdue_rows),
        )
    except Exception as exc:
        logger.error("Failed to send weekly digest email: %s", exc)
