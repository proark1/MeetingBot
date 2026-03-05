"""Bot management API — mirrors Recall.ai's /api/v1/bot endpoints."""

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, AsyncSessionLocal
from app.models.bot import Bot
from app.schemas.bot import BotCreate, BotListResponse, BotResponse, MeetingAnalysis
from app.services import bot_service, intelligence_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bot", tags=["Bots"])

# Track running lifecycle tasks so we can cancel them
_running_tasks: dict[str, asyncio.Task] = {}


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
        transcript=bot.transcript or [],
        analysis=MeetingAnalysis(**bot.analysis) if bot.analysis else None,
        recording_url=bot.recording_url,
        extra_metadata=bot.extra_metadata or {},
    )


# ── POST /api/v1/bot ────────────────────────────────────────────────────────

@router.post("", response_model=BotResponse, status_code=201)
async def create_bot(
    payload: BotCreate,
    background_tasks: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a new meeting bot and immediately start its lifecycle."""
    bot = Bot(
        meeting_url=payload.meeting_url,
        meeting_platform=bot_service.detect_platform(payload.meeting_url),
        bot_name=payload.bot_name,
        join_at=payload.join_at,
        extra_metadata=payload.extra_metadata,
    )
    db.add(bot)
    await db.commit()
    await db.refresh(bot)

    # Launch lifecycle as an asyncio task (not a BackgroundTask) so we can cancel it
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
):
    """List all bots with optional status filter."""
    query = select(Bot).order_by(Bot.created_at.desc())
    count_query = select(func.count()).select_from(Bot)

    if status:
        query = query.where(Bot.status == status)
        count_query = count_query.where(Bot.status == status)

    total = (await db.execute(count_query)).scalar_one()
    bots = (await db.execute(query.limit(limit).offset(offset))).scalars().all()

    return BotListResponse(
        results=[_bot_to_response(b) for b in bots],
        count=total,
    )


# ── GET /api/v1/bot/{id} ────────────────────────────────────────────────────

@router.get("/{bot_id}", response_model=BotResponse)
async def get_bot(
    bot_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get a single bot by ID."""
    bot = await _get_or_404(db, bot_id)
    return _bot_to_response(bot)


# ── DELETE /api/v1/bot/{id} ─────────────────────────────────────────────────

@router.delete("/{bot_id}", status_code=204)
async def delete_bot(
    bot_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Remove bot from meeting (cancels lifecycle if still running)."""
    bot = await _get_or_404(db, bot_id)

    # Cancel lifecycle task if running
    task = _running_tasks.get(bot_id)
    if task and not task.done():
        task.cancel()
        logger.info("Cancelled lifecycle task for bot %s", bot_id)

    await db.delete(bot)
    await db.commit()


# ── GET /api/v1/bot/{id}/transcript ─────────────────────────────────────────

@router.get("/{bot_id}/transcript")
async def get_transcript(
    bot_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get the meeting transcript."""
    bot = await _get_or_404(db, bot_id)
    if bot.status not in ("call_ended", "done"):
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


# ── helper ───────────────────────────────────────────────────────────────────

async def _get_or_404(db: AsyncSession, bot_id: str) -> Bot:
    result = await db.execute(select(Bot).where(Bot.id == bot_id))
    bot = result.scalar_one_or_none()
    if bot is None:
        raise HTTPException(status_code=404, detail=f"Bot {bot_id!r} not found")
    return bot
