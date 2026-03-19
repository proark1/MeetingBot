"""Email notification service.

Sends transactional emails (meeting done, error, etc.) via SMTP or
SendGrid when configured.

Configure via environment variables:
  EMAIL_BACKEND      = "smtp" | "sendgrid" | "none" (default: "none")
  SMTP_HOST          = e.g. smtp.gmail.com
  SMTP_PORT          = e.g. 587
  SMTP_USERNAME      = SMTP login
  SMTP_PASSWORD      = SMTP password / app password
  SMTP_FROM_ADDRESS  = From address, e.g. "MeetingBot <bot@example.com>"
  SMTP_USE_TLS       = "true" / "false" (default: true)
  SENDGRID_API_KEY   = SendGrid API key (if EMAIL_BACKEND=sendgrid)
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _get_settings():
    from app.config import settings
    return settings


def is_email_enabled() -> bool:
    s = _get_settings()
    backend = getattr(s, "EMAIL_BACKEND", "none")
    if backend == "smtp":
        return bool(getattr(s, "SMTP_HOST", ""))
    if backend == "sendgrid":
        return bool(getattr(s, "SENDGRID_API_KEY", ""))
    return False


def _format_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "unknown"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


def _render_done_email(bot_data: dict) -> tuple[str, str]:
    """Return (subject, html_body) for a meeting-done notification."""
    bot_id = bot_data.get("bot_id", "?")
    url = bot_data.get("meeting_url", "")
    platform = (bot_data.get("meeting_platform") or "meeting").replace("_", " ").title()
    status = bot_data.get("status", "done")
    participants = bot_data.get("participants") or []
    duration = _format_duration(bot_data.get("duration_seconds"))
    analysis = bot_data.get("analysis") or {}
    summary = analysis.get("summary", "No summary available.")
    action_items = analysis.get("action_items") or []
    decisions = analysis.get("decisions") or []

    status_emoji = "✅" if status == "done" else "⚠️"
    subject = f"{status_emoji} Meeting recording complete — {platform}"

    ai_list = ""
    if action_items:
        items_html = "".join(
            f"<li>{ai.get('task', ai) if isinstance(ai, dict) else ai}</li>"
            for ai in action_items[:10]
        )
        ai_list = f"<h3>Action Items</h3><ul>{items_html}</ul>"

    dec_list = ""
    if decisions:
        dec_html = "".join(f"<li>{d}</li>" for d in decisions[:10])
        dec_list = f"<h3>Decisions</h3><ul>{dec_html}</ul>"

    participants_str = ", ".join(participants[:8]) if participants else "N/A"
    if len(participants) > 8:
        participants_str += f" and {len(participants) - 8} more"

    html_body = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            color: #1a1a1a; background: #f5f5f5; margin: 0; padding: 20px; }}
    .card {{ background: white; border-radius: 8px; padding: 24px 32px;
             max-width: 600px; margin: 0 auto; box-shadow: 0 1px 3px rgba(0,0,0,.1); }}
    h2 {{ margin-top: 0; color: #111; }}
    .meta {{ background: #f9f9f9; border-radius: 6px; padding: 12px 16px;
             margin: 16px 0; font-size: 14px; }}
    .meta span {{ display: inline-block; margin-right: 20px; color: #555; }}
    .summary {{ border-left: 3px solid #4f6ef7; padding-left: 12px;
                margin: 16px 0; color: #333; font-size: 15px; }}
    h3 {{ color: #222; font-size: 15px; margin-bottom: 6px; }}
    ul {{ margin: 0 0 16px; padding-left: 20px; }}
    li {{ margin-bottom: 4px; font-size: 14px; }}
    .footer {{ font-size: 12px; color: #888; margin-top: 24px; padding-top: 16px;
               border-top: 1px solid #eee; }}
    .btn {{ display: inline-block; background: #4f6ef7; color: white;
            padding: 10px 20px; border-radius: 6px; text-decoration: none;
            font-size: 14px; margin-top: 16px; }}
  </style>
</head>
<body>
<div class="card">
  <h2>{status_emoji} Your meeting recording is ready</h2>
  <div class="meta">
    <span>📅 {platform}</span>
    <span>⏱ {duration}</span>
    <span>👥 {len(participants)} participant(s)</span>
  </div>
  <p><strong>Participants:</strong> {participants_str}</p>
  <h3>Summary</h3>
  <div class="summary">{summary}</div>
  {ai_list}
  {dec_list}
  <a class="btn" href="#">View Full Report →</a>
  <div class="footer">
    Bot ID: {bot_id}<br>
    Meeting: {url or "N/A"}
  </div>
</div>
</body>
</html>
"""
    return subject, html_body


async def _send_smtp(to_address: str, subject: str, html_body: str) -> None:
    """Send email via SMTP."""
    import smtplib
    import ssl
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    s = _get_settings()
    host = getattr(s, "SMTP_HOST", "")
    port = int(getattr(s, "SMTP_PORT", 587))
    username = getattr(s, "SMTP_USERNAME", "")
    password = getattr(s, "SMTP_PASSWORD", "")
    from_addr = getattr(s, "SMTP_FROM_ADDRESS", username)
    use_tls = str(getattr(s, "SMTP_USE_TLS", "true")).lower() != "false"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_address
    msg.attach(MIMEText(html_body, "html"))

    def _send():
        if use_tls:
            context = ssl.create_default_context()
            with smtplib.SMTP(host, port) as server:
                server.starttls(context=context)
                if username:
                    server.login(username, password)
                server.sendmail(from_addr, [to_address], msg.as_string())
        else:
            with smtplib.SMTP(host, port) as server:
                if username:
                    server.login(username, password)
                server.sendmail(from_addr, [to_address], msg.as_string())

    await asyncio.to_thread(_send)


async def _send_sendgrid(to_address: str, subject: str, html_body: str) -> None:
    """Send email via SendGrid."""
    try:
        import sendgrid
        from sendgrid.helpers.mail import Mail
    except ImportError:
        raise RuntimeError("sendgrid package not installed — run: pip install sendgrid")

    s = _get_settings()
    api_key = getattr(s, "SENDGRID_API_KEY", "")
    from_addr = getattr(s, "SMTP_FROM_ADDRESS", "noreply@meetingbot.io")

    message = Mail(
        from_email=from_addr,
        to_emails=to_address,
        subject=subject,
        html_content=html_body,
    )

    def _send():
        sg = sendgrid.SendGridAPIClient(api_key)
        sg.send(message)

    await asyncio.to_thread(_send)


async def send_email(to_address: str, subject: str, html_body: str) -> bool:
    """Send an email using the configured backend.

    Returns True on success, False on failure (never raises).
    """
    if not is_email_enabled():
        logger.debug("Email disabled — would have sent '%s' to %s", subject, to_address)
        return False

    s = _get_settings()
    backend = getattr(s, "EMAIL_BACKEND", "none")

    try:
        if backend == "smtp":
            await _send_smtp(to_address, subject, html_body)
        elif backend == "sendgrid":
            await _send_sendgrid(to_address, subject, html_body)
        else:
            return False
        logger.info("Email sent to %s: %s", to_address, subject)
        return True
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", to_address, exc)
        return False


async def notify_meeting_done(account_email: str, notify_email: Optional[str], bot_data: dict) -> None:
    """Send a 'meeting done' notification email."""
    recipient = notify_email or account_email
    if not recipient:
        return
    subject, html_body = _render_done_email(bot_data)
    await send_email(recipient, subject, html_body)


async def send_weekly_digest() -> int:
    """Send a weekly digest email to all accounts with notify_on_done=True.

    Queries BotSnapshots from the past 7 days, groups by account, and sends a
    summary email with meeting stats, decisions, and open action items.
    Returns the number of emails sent.
    """
    if not is_email_enabled():
        logger.debug("Email disabled — skipping weekly digest")
        return 0

    from datetime import datetime, timezone, timedelta
    import json as _json
    from app.db import AsyncSessionLocal
    from app.models.account import Account, BotSnapshot, ActionItem
    from sqlalchemy import select

    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    sent = 0

    try:
        async with AsyncSessionLocal() as db:
            acct_result = await db.execute(
                select(Account).where(Account.notify_on_done == True)  # noqa: E712
            )
            accounts = acct_result.scalars().all()

            for account in accounts:
                recipient = account.notify_email or account.email
                if not recipient:
                    continue

                snap_result = await db.execute(
                    select(BotSnapshot).where(
                        BotSnapshot.account_id == account.id,
                        BotSnapshot.created_at >= week_ago,
                        BotSnapshot.status == "done",
                    )
                )
                snapshots = snap_result.scalars().all()
                if not snapshots:
                    continue

                total_meetings = len(snapshots)
                total_duration = 0.0
                all_decisions: list[str] = []
                all_action_items: list[dict] = []
                platform_counts: dict[str, int] = {}

                for snap in snapshots:
                    try:
                        data = _json.loads(snap.data or "{}")
                    except Exception:
                        continue
                    total_duration += data.get("duration_seconds") or 0
                    analysis = data.get("analysis") or {}
                    all_decisions.extend((analysis.get("decisions") or [])[:3])
                    all_action_items.extend((analysis.get("action_items") or [])[:5])
                    plat = data.get("meeting_platform", "unknown")
                    platform_counts[plat] = platform_counts.get(plat, 0) + 1

                ai_result = await db.execute(
                    select(ActionItem).where(
                        ActionItem.account_id == account.id,
                        ActionItem.status == "open",
                    )
                )
                open_ai_count = len(ai_result.scalars().all())

                hours = int(total_duration // 3600)
                minutes = int((total_duration % 3600) // 60)
                duration_str = f"{hours}h {minutes}m" if hours else f"{minutes}m"
                platform_str = ", ".join(
                    f"{p.replace('_', ' ').title()} ({c})" for p, c in platform_counts.items()
                )
                week_range = f"{week_ago.strftime('%b %d')} – {now.strftime('%b %d, %Y')}"

                decisions_html = ""
                if all_decisions:
                    items = "".join(f"<li>{d}</li>" for d in all_decisions[:6])
                    decisions_html = (
                        "<h3 style='color:#222;font-size:15px;margin:16px 0 6px'>Key Decisions</h3>"
                        f"<ul style='margin:0 0 16px;padding-left:20px'>{items}</ul>"
                    )

                ai_html = ""
                if all_action_items:
                    items = "".join(
                        f"<li>{ai.get('task', ai) if isinstance(ai, dict) else ai}"
                        f"{(' — <em>' + ai['assignee'] + '</em>') if isinstance(ai, dict) and ai.get('assignee') else ''}</li>"
                        for ai in all_action_items[:6]
                    )
                    ai_html = (
                        "<h3 style='color:#222;font-size:15px;margin:16px 0 6px'>Action Items (sample)</h3>"
                        f"<ul style='margin:0 0 16px;padding-left:20px'>{items}</ul>"
                    )

                html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1a1a1a;background:#f5f5f5;margin:0;padding:20px;">
<div style="background:white;border-radius:8px;padding:24px 32px;max-width:600px;margin:0 auto;box-shadow:0 1px 3px rgba(0,0,0,.1)">
  <h2 style="margin-top:0;color:#111">Your Weekly Meeting Digest</h2>
  <p style="color:#555;font-size:14px;margin-top:-8px">{week_range}</p>
  <div style="background:#f9f9f9;border-radius:6px;padding:12px 16px;margin:16px 0;font-size:14px">
    <span style="display:inline-block;margin-right:20px;color:#555">📅 <strong>{total_meetings}</strong> meeting{'s' if total_meetings != 1 else ''}</span>
    <span style="display:inline-block;margin-right:20px;color:#555">⏱ <strong>{duration_str}</strong> total</span>
    <span style="display:inline-block;color:#555">✅ <strong>{open_ai_count}</strong> open action item{'s' if open_ai_count != 1 else ''}</span>
  </div>
  {f'<p style="font-size:14px;color:#555"><strong>Platforms:</strong> {platform_str}</p>' if platform_str else ''}
  {decisions_html}
  {ai_html}
  <div style="font-size:12px;color:#888;margin-top:24px;padding-top:16px;border-top:1px solid #eee">
    JustHereToListen.io &mdash; You receive this because you enabled meeting notifications.
  </div>
</div>
</body></html>"""

                subject = f"📋 Weekly digest — {total_meetings} meeting{'s' if total_meetings != 1 else ''} ({week_range})"
                if await send_email(recipient, subject, html_body):
                    sent += 1

    except Exception as exc:
        logger.error("Weekly digest failed: %s", exc)

    logger.info("Weekly digest: sent %d email(s)", sent)
    return sent


async def notify_meeting_error(account_email: str, notify_email: Optional[str], bot_id: str, error: str) -> None:
    """Send a 'meeting error' notification email."""
    recipient = notify_email or account_email
    if not recipient:
        return
    subject = "⚠️ Meeting bot encountered an error"
    html_body = f"""
<!DOCTYPE html>
<html>
<body style="font-family:sans-serif;color:#1a1a1a;padding:20px;">
<div style="max-width:600px;margin:0 auto;background:white;border-radius:8px;padding:24px;">
  <h2>⚠️ Meeting bot error</h2>
  <p>Your meeting bot (<code>{bot_id}</code>) encountered an error:</p>
  <pre style="background:#f5f5f5;padding:12px;border-radius:4px;font-size:13px;white-space:pre-wrap;">{error[:500]}</pre>
  <p>Any recorded audio has been salvaged if possible. Please check your bot status.</p>
</div>
</body>
</html>
"""
    await send_email(recipient, subject, html_body)
