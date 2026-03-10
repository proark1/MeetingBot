"""Bot management API — mirrors Recall.ai's /api/v1/bot endpoints."""

import asyncio
import logging
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, AsyncSessionLocal
from app.models.bot import Bot
from app.schemas.bot import BotCreate, BotListResponse, BotResponse, BotSummary, MeetingAnalysis
from app.services import bot_service, intelligence_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bot", tags=["Bots"])

# Track running lifecycle tasks so we can cancel them (single-process only)
_running_tasks: dict[str, asyncio.Task] = {}

# Statuses treated as "active" for stats
_ACTIVE_STATUSES = ("ready", "scheduled", "joining", "in_call", "call_ended")


def _is_demo(bot: Bot) -> bool:
    return bool((bot.extra_metadata or {}).get("is_demo_transcript"))


def _bot_to_summary(bot: Bot) -> BotSummary:
    return BotSummary(
        id=bot.id,
        meeting_url=bot.meeting_url,
        meeting_platform=bot.meeting_platform,
        bot_name=bot.bot_name,
        status=bot.status,
        error_message=bot.error_message,
        created_at=bot.created_at,
        updated_at=bot.updated_at,
        started_at=bot.started_at,
        ended_at=bot.ended_at,
        participants=bot.participants or [],
        recording_url=bot.recording_url,
        share_token=bot.share_token,
        extra_metadata=bot.extra_metadata or {},
        is_demo_transcript=_is_demo(bot),
    )


def _bot_to_response(bot: Bot) -> BotResponse:
    return BotResponse(
        id=bot.id,
        meeting_url=bot.meeting_url,
        meeting_platform=bot.meeting_platform,
        bot_name=bot.bot_name,
        status=bot.status,
        error_message=bot.error_message,
        created_at=bot.created_at,
        updated_at=bot.updated_at,
        started_at=bot.started_at,
        ended_at=bot.ended_at,
        participants=bot.participants or [],
        transcript=bot.transcript or [],
        analysis=MeetingAnalysis(**bot.analysis) if bot.analysis else None,
        chapters=bot.chapters or [],
        speaker_stats=bot.speaker_stats or [],
        recording_url=bot.recording_url,
        recording_path=bot.recording_path,
        share_token=bot.share_token,
        extra_metadata=bot.extra_metadata or {},
        is_demo_transcript=_is_demo(bot),
    )


# ── GET /api/v1/bot/stats ────────────────────────────────────────────────────
# Must be defined before /{bot_id} to avoid path conflict

@router.get("/stats", tags=["Bots"])
async def get_stats(db: Annotated[AsyncSession, Depends(get_db)]):
    """Aggregate counts by status for dashboard widgets."""
    rows = (
        await db.execute(
            select(Bot.status, func.count(Bot.id).label("n")).group_by(Bot.status)
        )
    ).all()

    counts: dict[str, int] = {row.status: row.n for row in rows}
    total = sum(counts.values())
    active = sum(counts.get(s, 0) for s in _ACTIVE_STATUSES)

    return {
        "total": total,
        "active": active,
        "done": counts.get("done", 0),
        "error": counts.get("error", 0),
        "by_status": counts,
    }


# ── POST /api/v1/bot ────────────────────────────────────────────────────────

@router.post("", response_model=BotResponse, status_code=201)
async def create_bot(
    payload: BotCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a new meeting bot and immediately start its lifecycle.

    The bot navigates to the meeting URL, waits to be admitted, records audio,
    transcribes with Gemini, and analyses the transcript.

    **Auto-leave:** if the bot is the only participant for `BOT_ALONE_TIMEOUT`
    seconds (default 5 min) — either because the room was empty on join, or
    because everyone else left — it will leave automatically.
    """
    from app.config import settings

    # Rate limit: cap concurrent browser bots to avoid OOM crashes
    active = sum(1 for t in _running_tasks.values() if not t.done())
    if active >= settings.MAX_CONCURRENT_BOTS:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Too many bots running ({active}/{settings.MAX_CONCURRENT_BOTS}). "
                "Wait for a bot to finish before creating another."
            ),
        )

    from datetime import timezone as _tz
    from datetime import datetime as _dt
    is_scheduled = (
        payload.join_at is not None
        and payload.join_at.replace(tzinfo=_tz.utc) > _dt.now(_tz.utc)
    )
    bot = Bot(
        meeting_url=str(payload.meeting_url),
        meeting_platform=bot_service.detect_platform(str(payload.meeting_url)),
        bot_name=payload.bot_name,
        join_at=payload.join_at,
        notify_email=payload.notify_email,
        template_id=payload.template_id,
        vocabulary=payload.vocabulary,
        analysis_mode=payload.analysis_mode,
        respond_on_mention=payload.respond_on_mention,
        mention_response_mode=payload.mention_response_mode,
        tts_provider=payload.tts_provider,
        status="scheduled" if is_scheduled else "ready",
        extra_metadata=payload.extra_metadata,
        share_token=secrets.token_urlsafe(12),
    )
    db.add(bot)
    await db.commit()
    await db.refresh(bot)

    task = asyncio.create_task(
        bot_service.run_bot_lifecycle(bot.id, AsyncSessionLocal)
    )
    _running_tasks[bot.id] = task
    task.add_done_callback(lambda _: _running_tasks.pop(bot.id, None))

    logger.info("Created bot %s for %s", bot.id, bot.meeting_url)
    return _bot_to_response(bot)


# ── GET /api/v1/bot ─────────────────────────────────────────────────────────

@router.get("", response_model=BotListResponse)
async def list_bots(
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None),
    search: str | None = Query(default=None, description="Filter by meeting URL (partial match)"),
):
    """List all bots. Returns lightweight summaries (no transcript/analysis)."""
    query = select(Bot).order_by(Bot.created_at.desc())
    count_query = select(func.count()).select_from(Bot)

    if status:
        query = query.where(Bot.status == status)
        count_query = count_query.where(Bot.status == status)

    if search:
        pattern = f"%{search}%"
        query = query.where(Bot.meeting_url.ilike(pattern))
        count_query = count_query.where(Bot.meeting_url.ilike(pattern))

    total = (await db.execute(count_query)).scalar_one()
    bots = (await db.execute(query.limit(limit).offset(offset))).scalars().all()

    return BotListResponse(
        results=[_bot_to_summary(b) for b in bots],
        count=total,
    )


# ── GET /api/v1/bot/{id} ────────────────────────────────────────────────────

@router.get("/{bot_id}", response_model=BotResponse)
async def get_bot(
    bot_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get a single bot by ID including full transcript and analysis.

    Poll this endpoint until `status` is `done` (or `error`).

    **Statuses:**
    - `joining` — browser opening, navigating to meeting URL
    - `in_call` — host admitted the bot; recording in progress
    - `call_ended` — meeting ended (or bot left); transcription running
    - `done` — transcript and analysis ready
    - `error` — something failed; see `error_message`

    **Auto-leave** triggers `call_ended` when the bot has been alone for
    `BOT_ALONE_TIMEOUT` seconds (default 5 min).
    """
    bot = await _get_or_404(db, bot_id)
    return _bot_to_response(bot)


# ── DELETE /api/v1/bot/{id} ─────────────────────────────────────────────────

@router.delete("/{bot_id}", status_code=204)
async def delete_bot(
    bot_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Cancel the bot and return immediately.

    The lifecycle task catches the cancellation, salvages any captured audio,
    produces a transcript + analysis, and sets status = ``cancelled`` on its
    own — so the record (and transcript) remain accessible after this call.

    If the bot had already finished (``done`` / ``error``) nothing changes.
    """
    bot = await _get_or_404(db, bot_id)

    task = _running_tasks.get(bot_id)
    if task and not task.done():
        # Immediately mark the bot as call_ended so the UI updates right away
        # rather than staying on "in_call" for the duration of transcription.
        if bot.status == "in_call":
            from datetime import datetime, timezone as _tz
            bot.status = "call_ended"
            bot.ended_at = datetime.now(_tz.utc)
            bot.updated_at = datetime.now(_tz.utc)
            await db.commit()

        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        logger.info("Cancelled lifecycle task for bot %s", bot_id)


# ── GET /api/v1/bot/{id}/transcript ─────────────────────────────────────────

@router.get("/{bot_id}/transcript")
async def get_transcript(
    bot_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get the meeting transcript."""
    bot = await _get_or_404(db, bot_id)
    if bot.status not in ("call_ended", "done", "cancelled"):
        raise HTTPException(
            status_code=425,
            detail=f"Transcript not yet available (bot status: {bot.status})",
        )
    return {"bot_id": bot_id, "transcript": bot.transcript or []}


# ── POST /api/v1/bot/{id}/analyze ───────────────────────────────────────────

@router.post("/{bot_id}/analyze", response_model=MeetingAnalysis)
async def analyze_bot(
    bot_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """(Re-)run Claude analysis on the transcript."""
    bot = await _get_or_404(db, bot_id)
    if not bot.transcript:
        raise HTTPException(
            status_code=425,
            detail="No transcript available to analyse",
        )

    analysis = await intelligence_service.analyze_transcript(bot.transcript)
    bot.analysis = analysis
    await db.commit()

    return MeetingAnalysis(**analysis)


# ── GET /api/v1/bot/{id}/recording ──────────────────────────────────────────

@router.get("/{bot_id}/recording")
async def download_recording(
    bot_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Download the meeting audio recording (WAV)."""
    import os
    bot = await _get_or_404(db, bot_id)
    path = bot.recording_path
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Recording not available")
    return FileResponse(path, media_type="audio/wav", filename=f"recording-{bot_id[:8]}.wav")


# ── POST /api/v1/bot/{id}/ask ────────────────────────────────────────────────

@router.post("/{bot_id}/ask")
async def ask_bot(
    bot_id: str,
    payload: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Ask a free-form question about the meeting transcript."""
    question = (payload.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=422, detail="question is required")
    bot = await _get_or_404(db, bot_id)
    if not bot.transcript:
        raise HTTPException(status_code=425, detail="No transcript available yet")
    answer = await intelligence_service.ask_about_transcript(bot.transcript, question)
    return {"bot_id": bot_id, "question": question, "answer": answer}


# ── GET /api/v1/share/{token} ────────────────────────────────────────────────
# This endpoint is registered on a separate public router in main.py

share_router = APIRouter(prefix="/share", tags=["Share"])


@share_router.get("/{token}")
async def get_shared_bot(
    token: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Public read-only view of a meeting by share token."""
    result = await db.execute(select(Bot).where(Bot.share_token == token))
    bot = result.scalar_one_or_none()
    if bot is None:
        raise HTTPException(status_code=404, detail="Shared report not found")
    return _bot_to_response(bot)


# ── helpers ───────────────────────────────────────────────────────────────────

async def _get_or_404(db: AsyncSession, bot_id: str) -> Bot:
    result = await db.execute(select(Bot).where(Bot.id == bot_id))
    bot = result.scalar_one_or_none()
    if bot is None:
        raise HTTPException(status_code=404, detail=f"Bot {bot_id!r} not found")
    return bot
