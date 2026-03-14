"""Export endpoints — download meeting reports as PDF or Markdown."""

import io
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.bot import Bot

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bot", tags=["Exports"])


def _fmt_duration(start, end) -> str:
    if not start or not end:
        return "—"
    secs = max(0, int((end - start).total_seconds()))
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s"


def _fmt_ts(secs: float) -> str:
    m = int(secs // 60)
    s = int(secs % 60)
    return f"{m:02d}:{s:02d}"


# ── Markdown export ───────────────────────────────────────────────────────────

@router.get("/{bot_id}/export/markdown", response_class=PlainTextResponse)
async def export_markdown(
    bot_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Export the meeting report as a Markdown document."""
    bot = await _get_or_404(db, bot_id)
    md = _build_markdown(bot)
    filename = f"meeting-{bot_id[:8]}.md"
    return PlainTextResponse(
        content=md,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── PDF export ────────────────────────────────────────────────────────────────

@router.get("/{bot_id}/export/pdf")
async def export_pdf(
    bot_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Export the meeting report as a PDF document."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
            HRFlowable, ListFlowable, ListItem,
        )
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="PDF export requires reportlab — run: pip install reportlab",
        )

    bot = await _get_or_404(db, bot_id)
    pdf_bytes = _build_pdf(bot)

    filename = f"meeting-{bot_id[:8]}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Builders ──────────────────────────────────────────────────────────────────

def _build_markdown(bot: Bot) -> str:
    analysis = bot.analysis or {}
    lines: list[str] = []

    lines.append(f"# Meeting Report")
    lines.append("")
    lines.append(f"**URL:** {bot.meeting_url}")
    lines.append(f"**Platform:** {(bot.meeting_platform or 'unknown').replace('_', ' ').title()}")
    lines.append(f"**Status:** {bot.status}")
    if bot.started_at:
        lines.append(f"**Started:** {bot.started_at.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"**Duration:** {_fmt_duration(bot.started_at, bot.ended_at)}")
    if bot.participants:
        lines.append(f"**Participants:** {', '.join(bot.participants)}")
    lines.append("")

    summary = analysis.get("summary", "")
    if summary:
        lines += ["## Summary", "", summary, ""]

    sentiment = analysis.get("sentiment", "")
    if sentiment:
        lines += [f"**Sentiment:** {sentiment.title()}", ""]

    key_points = analysis.get("key_points", [])
    if key_points:
        lines += ["## Key Points", ""]
        lines += [f"- {p}" for p in key_points]
        lines.append("")

    decisions = analysis.get("decisions", [])
    if decisions:
        lines += ["## Decisions", ""]
        lines += [f"- {d}" for d in decisions]
        lines.append("")

    action_items = analysis.get("action_items", [])
    if action_items:
        lines += ["## Action Items", ""]
        for item in action_items:
            task = item.get("task", "")
            assignee = item.get("assignee", "")
            due = item.get("due_date", "")
            line = f"- [ ] {task}"
            if assignee:
                line += f" *(→ {assignee})*"
            if due:
                line += f" — due {due}"
            lines.append(line)
        lines.append("")

    next_steps = analysis.get("next_steps", [])
    if next_steps:
        lines += ["## Next Steps", ""]
        lines += [f"- {s}" for s in next_steps]
        lines.append("")

    topics = analysis.get("topics", [])
    if topics:
        lines += [f"**Topics:** {', '.join(topics)}", ""]

    chapters = bot.chapters or []
    if chapters:
        lines += ["## Chapters", ""]
        for ch in chapters:
            ts = _fmt_ts(ch.get("start_time", 0))
            lines.append(f"### {ts} — {ch.get('title', '')}")
            if ch.get("summary"):
                lines.append(ch["summary"])
            lines.append("")

    speaker_stats = bot.speaker_stats or []
    if speaker_stats:
        lines += ["## Speaker Stats", ""]
        lines.append("| Speaker | Talk time | % | Turns | Questions |")
        lines.append("|---------|-----------|---|-------|-----------|")
        for sp in speaker_stats:
            tt = sp.get("talk_time_s", 0)
            m, s = divmod(int(tt), 60)
            lines.append(
                f"| {sp.get('name', '?')} | {m}m {s}s | {sp.get('talk_pct', 0):.1f}% "
                f"| {sp.get('turns', 0)} | {sp.get('questions', 0)} |"
            )
        lines.append("")

    transcript = bot.transcript or []
    if transcript:
        lines += ["## Transcript", ""]
        for entry in transcript:
            ts = _fmt_ts(entry.get("timestamp", 0))
            speaker = entry.get("speaker", "?")
            text = entry.get("text", "")
            lines.append(f"**[{ts}] {speaker}:** {text}")
            lines.append("")

    return "\n".join(lines)


def _build_pdf(bot: Bot) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        HRFlowable, ListFlowable, ListItem,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
    )

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=18, spaceAfter=6)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=13, spaceBefore=12, spaceAfter=4)
    h3 = ParagraphStyle("H3", parent=styles["Heading3"], fontSize=11, spaceBefore=8, spaceAfter=2)
    body = styles["Normal"]
    meta = ParagraphStyle("Meta", parent=body, fontSize=9, textColor=colors.grey)
    bold = ParagraphStyle("Bold", parent=body, fontName="Helvetica-Bold")
    mono = ParagraphStyle("Mono", parent=body, fontName="Courier", fontSize=8.5)

    story = []
    analysis = bot.analysis or {}

    # Title
    story.append(Paragraph("Meeting Report", h1))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.lightgrey))
    story.append(Spacer(1, 0.3 * cm))

    # Metadata table
    platform = (bot.meeting_platform or "unknown").replace("_", " ").title()
    duration = _fmt_duration(bot.started_at, bot.ended_at)
    started = bot.started_at.strftime("%Y-%m-%d %H:%M UTC") if bot.started_at else "—"
    participants = ", ".join(bot.participants or []) or "—"
    sentiment = (analysis.get("sentiment") or "neutral").title()

    meta_data = [
        ["URL", bot.meeting_url[:80]],
        ["Platform", platform],
        ["Date", started],
        ["Duration", duration],
        ["Participants", participants[:120]],
        ["Sentiment", sentiment],
    ]
    t = Table(meta_data, colWidths=[3.5 * cm, None])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story += [t, Spacer(1, 0.5 * cm)]

    def _section(title: str, items: list[str], ordered: bool = False) -> None:
        if not items:
            return
        story.append(Paragraph(title, h2))
        list_items = [ListItem(Paragraph(str(i), body), leftIndent=15) for i in items]
        story.append(ListFlowable(list_items, bulletType="bullet" if not ordered else "1"))

    # Summary
    summary = analysis.get("summary", "")
    if summary:
        story.append(Paragraph("Summary", h2))
        story.append(Paragraph(summary, body))

    _section("Key Points", analysis.get("key_points", []))
    _section("Decisions", analysis.get("decisions", []))

    # Action items
    action_items = analysis.get("action_items", [])
    if action_items:
        story.append(Paragraph("Action Items", h2))
        for item in action_items:
            task = item.get("task", "")
            assignee = item.get("assignee", "")
            due = item.get("due_date", "")
            suffix = ""
            if assignee:
                suffix += f" → {assignee}"
            if due:
                suffix += f" (due {due})"
            story.append(Paragraph(f"☐  {task}{suffix}", body))

    _section("Next Steps", analysis.get("next_steps", []))

    # Speaker stats
    speaker_stats = bot.speaker_stats or []
    if speaker_stats:
        story.append(Paragraph("Speaker Stats", h2))
        header = ["Speaker", "Talk Time", "%", "Turns", "Questions", "Filler Words"]
        rows = [header]
        for sp in speaker_stats:
            tt = sp.get("talk_time_s", 0)
            m, s = divmod(int(tt), 60)
            rows.append([
                sp.get("name", "?"),
                f"{m}m {s}s",
                f"{sp.get('talk_pct', 0):.1f}%",
                str(sp.get("turns", 0)),
                str(sp.get("questions", 0)),
                str(sp.get("filler_words", 0)),
            ])
        t2 = Table(rows, repeatRows=1)
        t2.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4361ee")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f7ff")]),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.lightgrey),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story += [t2, Spacer(1, 0.4 * cm)]

    # Chapters
    chapters = bot.chapters or []
    if chapters:
        story.append(Paragraph("Chapters", h2))
        for ch in chapters:
            ts = _fmt_ts(ch.get("start_time", 0))
            story.append(Paragraph(f"{ts} — {ch.get('title', '')}", h3))
            if ch.get("summary"):
                story.append(Paragraph(ch["summary"], body))

    # Transcript
    transcript = bot.transcript or []
    if transcript:
        story.append(Paragraph("Transcript", h2))
        for entry in transcript:
            ts = _fmt_ts(entry.get("timestamp", 0))
            speaker = entry.get("speaker", "?")
            text = entry.get("text", "")
            story.append(
                Paragraph(f"<b>[{ts}] {speaker}:</b> {text}", body)
            )
            story.append(Spacer(1, 0.1 * cm))

    doc.build(story)
    return buf.getvalue()


# ── Helper ────────────────────────────────────────────────────────────────────

async def _get_or_404(db: AsyncSession, bot_id: str) -> Bot:
    result = await db.execute(select(Bot).where(Bot.id == bot_id))
    bot = result.scalar_one_or_none()
    if bot is None:
        raise HTTPException(status_code=404, detail=f"Bot {bot_id!r} not found")
    return bot
