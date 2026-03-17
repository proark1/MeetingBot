"""Export endpoints — download meeting reports as PDF, Markdown, JSON, or SRT."""

import io
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, PlainTextResponse
from pydantic import BaseModel
from typing import Any, Dict, List, Optional

from app.deps import SUPERADMIN_ACCOUNT_ID
from app.store import store, BotSession

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


async def _get_or_404(bot_id: str, account_id=None) -> BotSession:
    bot = await store.get_bot(bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail=f"Bot {bot_id!r} not found")
    if (
        account_id
        and account_id != SUPERADMIN_ACCOUNT_ID
        and bot.account_id is not None
        and bot.account_id != account_id
    ):
        raise HTTPException(status_code=404, detail=f"Bot {bot_id!r} not found")
    return bot


# ── Markdown export ───────────────────────────────────────────────────────────

@router.get("/{bot_id}/export/markdown", response_class=PlainTextResponse)
async def export_markdown(bot_id: str, request: Request):
    """Export the meeting report as a Markdown document."""
    bot = await _get_or_404(bot_id, getattr(request.state, "account_id", None))
    md = _build_markdown(bot)
    filename = f"meeting-{bot_id[:8]}.md"
    return PlainTextResponse(
        content=md,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── PDF export ────────────────────────────────────────────────────────────────

@router.get("/{bot_id}/export/pdf")
async def export_pdf(bot_id: str, request: Request):
    """Export the meeting report as a PDF document."""
    try:
        from reportlab.lib.pagesizes import A4  # noqa: F401 (import check)
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="PDF export requires reportlab — run: pip install reportlab",
        )

    bot = await _get_or_404(bot_id, getattr(request.state, "account_id", None))
    pdf_bytes = _build_pdf(bot)
    filename = f"meeting-{bot_id[:8]}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Builders ──────────────────────────────────────────────────────────────────

def _build_markdown(bot: BotSession) -> str:
    analysis = bot.analysis or {}
    lines: list[str] = []

    lines.append("# Meeting Report")
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


def _build_pdf(bot: BotSession) -> bytes:
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

    story = []
    analysis = bot.analysis or {}

    story.append(Paragraph("Meeting Report", h1))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.lightgrey))
    story.append(Spacer(1, 0.3 * cm))

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

    def _section(title: str, items: list[str]) -> None:
        if not items:
            return
        story.append(Paragraph(title, h2))
        list_items = [ListItem(Paragraph(str(i), body), leftIndent=15) for i in items]
        story.append(ListFlowable(list_items, bulletType="bullet"))

    summary = analysis.get("summary", "")
    if summary:
        story.append(Paragraph("Summary", h2))
        story.append(Paragraph(summary, body))

    _section("Key Points", analysis.get("key_points", []))
    _section("Decisions", analysis.get("decisions", []))

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

    speaker_stats = bot.speaker_stats or []
    if speaker_stats:
        story.append(Paragraph("Speaker Stats", h2))
        header = ["Speaker", "Talk Time", "%", "Turns", "Questions", "Filler Words"]
        rows = [header]
        for sp in speaker_stats:
            tt = sp.get("talk_time_s", 0)
            m, s = divmod(int(tt), 60)
            rows.append([
                sp.get("name", "?"), f"{m}m {s}s",
                f"{sp.get('talk_pct', 0):.1f}%",
                str(sp.get("turns", 0)), str(sp.get("questions", 0)),
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

    chapters = bot.chapters or []
    if chapters:
        story.append(Paragraph("Chapters", h2))
        for ch in chapters:
            ts = _fmt_ts(ch.get("start_time", 0))
            story.append(Paragraph(f"{ts} — {ch.get('title', '')}", h3))
            if ch.get("summary"):
                story.append(Paragraph(ch["summary"], body))

    transcript = bot.transcript or []
    if transcript:
        story.append(Paragraph("Transcript", h2))
        for entry in transcript:
            ts = _fmt_ts(entry.get("timestamp", 0))
            speaker = entry.get("speaker", "?")
            text = entry.get("text", "")
            story.append(Paragraph(f"<b>[{ts}] {speaker}:</b> {text}", body))
            story.append(Spacer(1, 0.1 * cm))

    doc.build(story)
    return buf.getvalue()


# ── JSON export ───────────────────────────────────────────────────────────────

class _ExportJsonResponse(BaseModel):
    id: str
    meeting_url: str
    meeting_platform: Optional[str] = None
    bot_name: str
    status: str
    transcript: List[Dict[str, Any]]
    analysis: Optional[Dict[str, Any]] = None
    chapters: List[Dict[str, Any]]
    speaker_stats: List[Dict[str, Any]]
    participants: List[str]
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    metadata: Optional[Dict[str, Any]] = None
    sub_user_id: Optional[str] = None


@router.get("/{bot_id}/export/json", response_model=_ExportJsonResponse)
async def export_json(bot_id: str, request: Request):
    """Export the full bot session as structured JSON."""
    bot = await _get_or_404(bot_id, getattr(request.state, "account_id", None))
    return _ExportJsonResponse(
        id=bot.id,
        meeting_url=bot.meeting_url,
        meeting_platform=bot.meeting_platform,
        bot_name=bot.bot_name,
        status=bot.status,
        transcript=bot.transcript or [],
        analysis=bot.analysis,
        chapters=bot.chapters or [],
        speaker_stats=bot.speaker_stats or [],
        participants=bot.participants or [],
        started_at=bot.started_at.isoformat() if bot.started_at else None,
        ended_at=bot.ended_at.isoformat() if bot.ended_at else None,
        duration_seconds=bot.duration_seconds,
        metadata=bot.metadata or {},
        sub_user_id=bot.sub_user_id,
    )


# ── SRT export ────────────────────────────────────────────────────────────────

def _srt_timestamp(seconds: float) -> str:
    """Convert float seconds to SRT timestamp format HH:MM:SS,mmm."""
    seconds = max(0.0, seconds)
    ms = int((seconds % 1) * 1000)
    total_secs = int(seconds)
    s = total_secs % 60
    total_mins = total_secs // 60
    m = total_mins % 60
    h = total_mins // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _build_srt(bot: BotSession) -> str:
    transcript = bot.transcript or []
    if not transcript:
        return ""

    lines: list[str] = []
    for i, entry in enumerate(transcript):
        start = float(entry.get("timestamp", 0))
        # Use next entry's timestamp as end, or add 5 s for the last entry
        if i + 1 < len(transcript):
            end = float(transcript[i + 1].get("timestamp", start + 5))
        else:
            end = start + 5.0
        # Ensure end > start
        if end <= start:
            end = start + 2.0

        speaker = entry.get("speaker", "")
        text = entry.get("text", "").strip()
        caption = f"{speaker}: {text}" if speaker else text

        lines.append(str(i + 1))
        lines.append(f"{_srt_timestamp(start)} --> {_srt_timestamp(end)}")
        lines.append(caption)
        lines.append("")

    return "\n".join(lines)


@router.get("/{bot_id}/export/srt")
async def export_srt(bot_id: str, request: Request):
    """Export the meeting transcript as an SRT subtitle file."""
    bot = await _get_or_404(bot_id, getattr(request.state, "account_id", None))
    srt_content = _build_srt(bot)
    filename = f"meeting-{bot_id[:8]}.srt"
    return PlainTextResponse(
        content=srt_content,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
