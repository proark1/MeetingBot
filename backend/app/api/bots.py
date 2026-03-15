"""Bot management API."""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.schemas.bot import (
    BotCreate, BotListResponse, BotResponse, BotSummary,
    MeetingAnalysis, AIUsageSummary, AIUsageEntry,
)
from app.services import bot_service, intelligence_service
from app.store import store, BotSession, _now

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bot", tags=["Bots"])

# Running lifecycle tasks (single-process only)
_running_tasks: dict[str, asyncio.Task] = {}

# FIFO queue of bot IDs waiting for a free slot
_bot_queue: list[str] = []

_ACTIVE_STATUSES = ("ready", "scheduled", "queued", "joining", "in_call", "call_ended")


async def _queue_processor() -> None:
    """Background loop: start queued bots when a slot is free."""
    while True:
        await asyncio.sleep(10)
        if not _bot_queue:
            continue
        active = sum(1 for t in _running_tasks.values() if not t.done())
        if active >= settings.MAX_CONCURRENT_BOTS:
            continue
        bot_id = _bot_queue.pop(0)
        await store.update_bot(bot_id, status="joining")
        task = asyncio.create_task(bot_service.run_bot_lifecycle(bot_id))
        _running_tasks[bot_id] = task
        task.add_done_callback(lambda _t, bid=bot_id: _running_tasks.pop(bid, None))
        logger.info("Queue: started bot %s (%d remaining in queue)", bot_id, len(_bot_queue))


# ── Helper: session → response ────────────────────────────────────────────────

def _to_summary(bot: BotSession) -> BotSummary:
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
        duration_seconds=bot.duration_seconds,
        participants=bot.participants,
        recording_available=bot.recording_available(),
        analysis_mode=bot.analysis_mode,
        is_demo_transcript=bot.is_demo_transcript,
        metadata=bot.metadata,
        ai_total_tokens=bot.ai_total_tokens,
        ai_total_cost_usd=bot.ai_total_cost_usd,
        ai_primary_model=bot.ai_primary_model,
    )


def _to_response(bot: BotSession) -> BotResponse:
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
        duration_seconds=bot.duration_seconds,
        participants=bot.participants,
        transcript=bot.transcript,
        analysis=MeetingAnalysis(**bot.analysis) if bot.analysis else None,
        chapters=bot.chapters,
        speaker_stats=bot.speaker_stats,
        recording_available=bot.recording_available(),
        analysis_mode=bot.analysis_mode,
        is_demo_transcript=bot.is_demo_transcript,
        metadata=bot.metadata,
        ai_usage=AIUsageSummary(
            total_tokens=bot.ai_total_tokens,
            total_cost_usd=bot.ai_total_cost_usd,
            primary_model=bot.ai_primary_model,
            operations=[AIUsageEntry(**r) for r in bot.ai_usage],
        ),
    )


async def _get_or_404(bot_id: str) -> BotSession:
    bot = await store.get_bot(bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail=f"Bot {bot_id!r} not found")
    return bot


# ── POST /api/v1/bot ──────────────────────────────────────────────────────────

@router.post("", response_model=BotResponse, status_code=201)
async def create_bot(payload: BotCreate):
    """Create a new meeting bot.

    The bot joins the meeting, records audio, transcribes with Gemini/Claude,
    and delivers results to your `webhook_url` when done.

    Poll `GET /api/v1/bot/{id}` until `status` is `done` (or `error`).

    **Platforms supported for real recording:** Google Meet, Zoom, Microsoft Teams.
    Other platforms run in demo mode (AI-generated sample transcript).
    """
    is_scheduled = (
        payload.join_at is not None
        and payload.join_at.replace(tzinfo=timezone.utc) > _now()
    )

    bot = BotSession(
        id=str(uuid.uuid4()),
        meeting_url=str(payload.meeting_url),
        meeting_platform=bot_service.detect_platform(str(payload.meeting_url)),
        bot_name=payload.bot_name,
        status="scheduled" if is_scheduled else "ready",
        webhook_url=payload.webhook_url,
        join_at=payload.join_at,
        analysis_mode=payload.analysis_mode,
        template=payload.template,
        prompt_override=payload.prompt_override,
        vocabulary=payload.vocabulary or [],
        respond_on_mention=payload.respond_on_mention,
        mention_response_mode=payload.mention_response_mode,
        tts_provider=payload.tts_provider,
        start_muted=payload.start_muted,
        live_transcription=payload.live_transcription,
        metadata=payload.metadata,
    )
    await store.create_bot(bot)

    active = sum(1 for t in _running_tasks.values() if not t.done())
    if active >= settings.MAX_CONCURRENT_BOTS:
        _bot_queue.append(bot.id)
        await store.update_bot(bot.id, status="queued")
        bot.status = "queued"
        queue_pos = _bot_queue.index(bot.id) + 1
        logger.info("Bot %s queued (position %d)", bot.id, queue_pos)
    else:
        task = asyncio.create_task(bot_service.run_bot_lifecycle(bot.id))
        _running_tasks[bot.id] = task
        task.add_done_callback(lambda _: _running_tasks.pop(bot.id, None))

    logger.info("Created bot %s for %s (status=%s)", bot.id, bot.meeting_url, bot.status)
    return _to_response(bot)


# ── GET /api/v1/bot/stats ─────────────────────────────────────────────────────

@router.get("/stats", tags=["Bots"])
async def get_stats():
    """Aggregate counts by status."""
    all_bots, _ = await store.list_bots(limit=10000)
    counts: dict[str, int] = {}
    for b in all_bots:
        counts[b.status] = counts.get(b.status, 0) + 1
    total = sum(counts.values())
    active = sum(counts.get(s, 0) for s in _ACTIVE_STATUSES)
    return {
        "total": total,
        "active": active,
        "done": counts.get("done", 0),
        "error": counts.get("error", 0),
        "by_status": counts,
    }


# ── GET /api/v1/bot ───────────────────────────────────────────────────────────

@router.get("", response_model=BotListResponse)
async def list_bots(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: Optional[str] = Query(default=None, description="Filter by status"),
):
    """List bots (lightweight summaries, no transcript/analysis)."""
    bots, total = await store.list_bots(status=status, limit=limit, offset=offset)
    return BotListResponse(
        results=[_to_summary(b) for b in bots],
        total=total,
        limit=limit,
        offset=offset,
    )


# ── GET /api/v1/bot/{id} ──────────────────────────────────────────────────────

@router.get("/{bot_id}", response_model=BotResponse)
async def get_bot(bot_id: str):
    """Get a bot by ID with full transcript and analysis.

    Poll until `status` is `done` (or `error`).

    **Note:** Results are kept in memory for 24 hours after completion.
    Save the data to your own storage before then.
    """
    bot = await _get_or_404(bot_id)
    return _to_response(bot)


# ── DELETE /api/v1/bot/{id} ───────────────────────────────────────────────────

@router.delete("/{bot_id}", status_code=204)
async def delete_bot(bot_id: str):
    """Stop a running bot and cancel its lifecycle.

    If the bot already finished (`done` / `error`), it is removed from memory
    immediately. If still running, it is cancelled (transcript salvaged if possible).
    """
    bot = await _get_or_404(bot_id)

    task = _running_tasks.get(bot_id)
    if task and not task.done():
        if bot.status == "in_call":
            await store.update_bot(bot_id, status="call_ended", ended_at=_now())
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=30.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        logger.info("Cancelled lifecycle task for bot %s", bot_id)
    else:
        # Already finished — remove from memory immediately
        await store.delete_bot(bot_id)


# ── Helper: wait for transcription to finish ──────────────────────────────────

async def _wait_for_transcript(bot: BotSession, timeout: int = 25) -> BotSession:
    """If the bot is actively transcribing, block until it finishes (or timeout)."""
    for _ in range(timeout):
        if bot.status != "transcribing":
            break
        await asyncio.sleep(1)
        refreshed = await store.get_bot(bot.id)
        if refreshed is None:
            break
        bot = refreshed
    return bot


# ── GET /api/v1/bot/{id}/transcript ──────────────────────────────────────────

@router.get("/{bot_id}/transcript")
async def get_transcript(bot_id: str):
    """Get the raw transcript.

    If transcription is still running, this request blocks until it finishes
    (up to 25 s) and then returns the result automatically.
    """
    bot = await _get_or_404(bot_id)
    bot = await _wait_for_transcript(bot)
    if bot.status not in ("call_ended", "done", "cancelled"):
        raise HTTPException(
            status_code=425,
            detail=f"Transcript not yet available (status: {bot.status})",
            headers={"Retry-After": "5"},
        )
    return {"bot_id": bot_id, "transcript": bot.transcript}


# ── GET /api/v1/bot/{id}/recording ───────────────────────────────────────────

@router.get("/{bot_id}/recording")
async def download_recording(bot_id: str):
    """Download the meeting audio recording (WAV)."""
    import os
    bot = await _get_or_404(bot_id)
    if not bot.recording_path or not os.path.exists(bot.recording_path):
        raise HTTPException(status_code=404, detail="Recording not available")
    return FileResponse(
        bot.recording_path,
        media_type="audio/wav",
        filename=f"recording-{bot_id[:8]}.wav",
    )


# ── POST /api/v1/bot/{id}/analyze ────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    template: Optional[str] = None
    prompt_override: Optional[str] = Field(default=None, max_length=8000)


@router.post("/{bot_id}/analyze", response_model=MeetingAnalysis)
async def analyze_bot(bot_id: str, payload: AnalyzeRequest = AnalyzeRequest()):
    """(Re-)run AI analysis on the transcript.

    If transcription is still running, this request blocks until it finishes
    (up to 25 s) before running analysis.

    Use this to switch templates or run a custom prompt on an existing transcript.
    """
    bot = await _get_or_404(bot_id)
    bot = await _wait_for_transcript(bot)
    if not bot.transcript:
        raise HTTPException(
            status_code=425,
            detail="No transcript available to analyse",
            headers={"Retry-After": "10"},
        )

    prompt = payload.prompt_override
    if not prompt and payload.template:
        prompt = intelligence_service.get_template_prompt(payload.template)

    analysis = await intelligence_service.analyze_transcript(
        bot.transcript,
        prompt_override=prompt,
        vocabulary=bot.vocabulary or [],
    )
    await store.update_bot(bot_id, analysis=analysis)
    return MeetingAnalysis(**analysis)


# ── POST /api/v1/bot/{id}/ask ─────────────────────────────────────────────────

class AskRequest(BaseModel):
    question: str = Field(description="Free-form question about the meeting transcript.")


@router.post("/{bot_id}/ask")
async def ask_bot(bot_id: str, payload: AskRequest):
    """Ask a free-form question about the meeting transcript."""
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="question is required")
    bot = await _get_or_404(bot_id)
    if not bot.transcript:
        raise HTTPException(status_code=425, detail="No transcript available yet")
    answer = await intelligence_service.ask_about_transcript(bot.transcript, question)
    return {"bot_id": bot_id, "question": question, "answer": answer}


# ── POST /api/v1/bot/{id}/followup-email ─────────────────────────────────────

@router.post("/{bot_id}/followup-email")
async def generate_followup_email(bot_id: str):
    """Generate a draft follow-up email for the meeting."""
    bot = await _get_or_404(bot_id)
    if not bot.transcript and not bot.analysis:
        raise HTTPException(status_code=425, detail="No transcript or analysis available yet")
    result = await intelligence_service.generate_followup_email(
        transcript=bot.transcript or [],
        analysis=bot.analysis or {},
        participants=bot.participants or [],
    )
    return {"bot_id": bot_id, **result}
