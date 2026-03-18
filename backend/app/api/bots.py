"""Bot management API."""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings
from app.deps import get_current_account_id, get_sub_user_id, SUPERADMIN_ACCOUNT_ID
from app.schemas.bot import (
    BotCreate, BotListResponse, BotResponse, BotSummary,
    MeetingAnalysis, AIUsageSummary, AIUsageEntry,
    Highlight, HighlightResponse,
)
from app.services import bot_service, intelligence_service
from app.store import store, BotSession, _now

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bot", tags=["Bots"])
_limiter = Limiter(key_func=get_remote_address)


class BotStatsResponse(BaseModel):
    """Aggregate bot counts by status."""
    total: int = Field(description="Total number of bots in memory (24-hour window).")
    active: int = Field(description="Number of bots currently in an active state (ready/scheduled/queued/joining/in_call/call_ended).")
    done: int = Field(description="Number of bots that completed successfully.")
    error: int = Field(description="Number of bots that ended in an error state.")
    by_status: dict[str, int] = Field(description="Count of bots broken down by each status string.")

# Running lifecycle tasks (single-process only)
_running_tasks: dict[str, asyncio.Task] = {}

# FIFO queue of bot IDs waiting for a free slot
_bot_queue: list[str] = []

_ACTIVE_STATUSES = ("ready", "scheduled", "queued", "joining", "in_call", "call_ended")


def _get_sub_user_from_request(request: Request) -> Optional[str]:
    """Extract sub_user_id from X-Sub-User header."""
    val = request.headers.get("X-Sub-User", "").strip()[:255]
    return val or None


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
        sub_user_id=bot.sub_user_id,
        metadata=bot.metadata,
        ai_total_tokens=bot.ai_total_tokens,
        ai_total_cost_usd=bot.ai_total_cost_usd,
        ai_primary_model=bot.ai_primary_model,
    )


def _video_available(bot: BotSession) -> bool:
    import os
    return bool(bot.video_path and os.path.exists(bot.video_path))


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
        video_available=_video_available(bot),
        bot_avatar_url=bot.bot_avatar_url,
        analysis_mode=bot.analysis_mode,
        is_demo_transcript=bot.is_demo_transcript,
        sub_user_id=bot.sub_user_id,
        metadata=bot.metadata,
        ai_usage=AIUsageSummary(
            total_tokens=bot.ai_total_tokens,
            total_cost_usd=bot.ai_total_cost_usd,
            primary_model=bot.ai_primary_model,
            operations=[AIUsageEntry(**r) for r in bot.ai_usage],
        ),
    )


async def _get_or_404(
    bot_id: str,
    account_id: Optional[str] = None,
    sub_user_id: Optional[str] = None,
) -> BotSession:
    bot = await store.get_bot(bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail=f"Bot {bot_id!r} not found")
    # Ownership check: per-user accounts can only see their own bots
    if (
        account_id
        and account_id != SUPERADMIN_ACCOUNT_ID
        and bot.account_id is not None
        and bot.account_id != account_id
    ):
        raise HTTPException(status_code=404, detail=f"Bot {bot_id!r} not found")
    # Sub-user isolation: when sub_user_id is provided, only show matching bots
    if sub_user_id is not None and bot.sub_user_id != sub_user_id:
        raise HTTPException(status_code=404, detail=f"Bot {bot_id!r} not found")
    return bot


# ── POST /api/v1/bot ──────────────────────────────────────────────────────────

@router.post("", response_model=BotResponse, status_code=201)
@_limiter.limit("20/minute")
async def create_bot(payload: BotCreate, request: Request):
    """Create a new meeting bot.

    The bot joins the meeting, records audio, transcribes with Gemini/Claude,
    and delivers results to your `webhook_url` when done.

    Poll `GET /api/v1/bot/{id}` until `status` is `done` (or `error`).

    **Business accounts:** Pass `sub_user_id` in the body or the `X-Sub-User` header
    to scope this bot to a specific end-user. When set, only requests with the same
    sub-user identifier can access this bot's data.

    **Idempotency:** Supply an `Idempotency-Key` header to safely retry the request.
    A second call with the same key returns the original bot (with header
    `X-Idempotency-Replayed: true`) instead of creating a duplicate.

    **Platforms supported for real recording:** Google Meet, Zoom, Microsoft Teams.
    Other platforms run in demo mode (AI-generated sample transcript).
    """
    account_id: Optional[str] = getattr(request.state, "account_id", None)

    # Resolve sub_user_id: body field takes precedence, then X-Sub-User header
    sub_user_id = payload.sub_user_id
    if not sub_user_id:
        header_val = request.headers.get("X-Sub-User", "").strip()[:255]
        sub_user_id = header_val or None

    # ── Idempotency key check ─────────────────────────────────────────────────
    idempotency_key_raw = request.headers.get("Idempotency-Key", "").strip()[:255]
    if idempotency_key_raw:
        try:
            from app.db import AsyncSessionLocal
            from app.models.account import IdempotencyKey as IKModel
            from sqlalchemy import select as _iselect
            async with AsyncSessionLocal() as db:
                ik_result = await db.execute(
                    _iselect(IKModel).where(
                        IKModel.account_id == (account_id or "__anon__"),
                        IKModel.key == idempotency_key_raw,
                    )
                )
                ik_row = ik_result.scalar_one_or_none()
                if ik_row and ik_row.expires_at > _now():
                    existing = await store.get_bot(ik_row.bot_id)
                    if existing:
                        from fastapi.responses import JSONResponse
                        import json
                        resp_data = _to_response(existing).model_dump(mode="json")
                        return JSONResponse(
                            content=resp_data,
                            status_code=200,
                            headers={"X-Idempotency-Replayed": "true"},
                        )
        except Exception:
            logger.exception("Idempotency key lookup failed")

    # Check credits for per-user accounts (not superadmin / unauthenticated)
    if account_id and account_id != SUPERADMIN_ACCOUNT_ID:
        from app.db import AsyncSessionLocal
        from app.services.credit_service import check_credits
        async with AsyncSessionLocal() as db:
            await check_credits(account_id, db)

    is_scheduled = (
        payload.join_at is not None
        and payload.join_at.replace(tzinfo=timezone.utc) > _now()
    )

    from app.config import settings as _settings

    # Resolve consent: per-bot override > platform default
    consent_enabled = payload.consent_enabled or _settings.CONSENT_ANNOUNCEMENT_ENABLED
    consent_message = payload.consent_message  # None = use platform default

    # Convert keyword alert configs to plain dicts for storage
    keyword_alerts_dicts = [
        {"keyword": ka.keyword, "webhook_url": ka.webhook_url}
        for ka in (payload.keyword_alerts or [])
    ]

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
        account_id=account_id if account_id != SUPERADMIN_ACCOUNT_ID else None,
        sub_user_id=sub_user_id,
        bot_avatar_url=payload.bot_avatar_url,
        record_video=payload.record_video,
        # New fields
        consent_enabled=consent_enabled,
        consent_message=consent_message,
        keyword_alerts=keyword_alerts_dicts,
        auto_followup_email=payload.auto_followup_email,
        workspace_id=payload.workspace_id,
        transcription_provider=payload.transcription_provider,
    )
    await store.create_bot(bot)

    # ── Store idempotency key ─────────────────────────────────────────────────
    if idempotency_key_raw:
        try:
            from app.db import AsyncSessionLocal
            from app.models.account import IdempotencyKey as IKModel
            from datetime import timedelta
            async with AsyncSessionLocal() as db:
                ik = IKModel(
                    account_id=account_id or "__anon__",
                    key=idempotency_key_raw,
                    bot_id=bot.id,
                    expires_at=_now() + timedelta(hours=settings.IDEMPOTENCY_TTL_HOURS),
                )
                db.add(ik)
                await db.commit()
        except Exception:
            logger.exception("Failed to store idempotency key")

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

@router.get("/stats", response_model=BotStatsResponse, tags=["Bots"])
async def get_stats(request: Request):
    """
    Aggregate bot counts by status for your account.

    Returns totals across all bots currently in memory (24-hour window).
    Per-user accounts see only their own bots; superadmin sees all.
    """
    account_id: Optional[str] = getattr(request.state, "account_id", None)
    sub_user_id = _get_sub_user_from_request(request)
    filter_account = account_id if (account_id and account_id != SUPERADMIN_ACCOUNT_ID) else None
    all_bots, _ = await store.list_bots(limit=10000, account_id=filter_account, sub_user_id=sub_user_id)
    counts: dict[str, int] = {}
    for b in all_bots:
        counts[b.status] = counts.get(b.status, 0) + 1
    total = sum(counts.values())
    active = sum(counts.get(s, 0) for s in _ACTIVE_STATUSES)
    return BotStatsResponse(
        total=total,
        active=active,
        done=counts.get("done", 0),
        error=counts.get("error", 0),
        by_status=counts,
    )


# ── GET /api/v1/bot ───────────────────────────────────────────────────────────

@router.get("", response_model=BotListResponse)
async def list_bots(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: Optional[str] = Query(default=None, description="Filter by status"),
):
    """List bots (lightweight summaries, no transcript/analysis)."""
    account_id: Optional[str] = getattr(request.state, "account_id", None)
    sub_user_id = _get_sub_user_from_request(request)
    # Superadmin and unauthenticated see all bots; per-user accounts see only their own
    filter_account = (
        account_id if (account_id and account_id != SUPERADMIN_ACCOUNT_ID) else None
    )
    bots, total = await store.list_bots(status=status, limit=limit, offset=offset, account_id=filter_account, sub_user_id=sub_user_id)
    return BotListResponse(
        results=[_to_summary(b) for b in bots],
        total=total,
        limit=limit,
        offset=offset,
    )


# ── GET /api/v1/bot/{id} ──────────────────────────────────────────────────────

@router.get("/{bot_id}", response_model=BotResponse)
async def get_bot(bot_id: str, request: Request):
    """Get a bot by ID with full transcript and analysis.

    Poll until `status` is `done` (or `error`).

    **Note:** Results are kept in memory for 24 hours after completion.
    Save the data to your own storage before then.
    """
    account_id: Optional[str] = getattr(request.state, "account_id", None)
    sub_user_id = _get_sub_user_from_request(request)
    bot = await _get_or_404(bot_id, account_id, sub_user_id)
    return _to_response(bot)


# ── DELETE /api/v1/bot/{id} ───────────────────────────────────────────────────

@router.delete("/{bot_id}", status_code=204)
async def delete_bot(bot_id: str, request: Request):
    """Stop a running bot and cancel its lifecycle.

    If the bot already finished (`done` / `error`), it is removed from memory
    immediately. If still running, it is cancelled (transcript salvaged if possible).
    """
    account_id: Optional[str] = getattr(request.state, "account_id", None)
    sub_user_id = _get_sub_user_from_request(request)
    bot = await _get_or_404(bot_id, account_id, sub_user_id)

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
async def get_transcript(bot_id: str, request: Request):
    """Get the raw transcript.

    If transcription is still running, this request blocks until it finishes
    (up to 25 s) and then returns the result automatically.
    """
    account_id: Optional[str] = getattr(request.state, "account_id", None)
    sub_user_id = _get_sub_user_from_request(request)
    bot = await _get_or_404(bot_id, account_id, sub_user_id)
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
async def download_recording(bot_id: str, request: Request):
    """Download the meeting audio recording (WAV)."""
    import os
    account_id: Optional[str] = getattr(request.state, "account_id", None)
    sub_user_id = _get_sub_user_from_request(request)
    bot = await _get_or_404(bot_id, account_id, sub_user_id)
    if not bot.recording_path or not os.path.exists(bot.recording_path):
        raise HTTPException(status_code=404, detail="Recording not available")
    return FileResponse(
        bot.recording_path,
        media_type="audio/wav",
        filename=f"recording-{bot_id[:8]}.wav",
    )


# ── GET /api/v1/bot/{id}/video ───────────────────────────────────────────────

@router.get("/{bot_id}/video")
async def download_video(bot_id: str, request: Request):
    """Download the meeting video recording (MP4).

    Available only when `record_video=true` was set at bot creation and
    `video_available` is `true` in the bot response.
    """
    import os
    account_id: Optional[str] = getattr(request.state, "account_id", None)
    sub_user_id = _get_sub_user_from_request(request)
    bot = await _get_or_404(bot_id, account_id, sub_user_id)
    if not bot.video_path or not os.path.exists(bot.video_path):
        raise HTTPException(status_code=404, detail="Video recording not available")
    return FileResponse(
        bot.video_path,
        media_type="video/mp4",
        filename=f"recording-{bot_id[:8]}.mp4",
    )


# ── POST /api/v1/bot/{id}/analyze ────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    template: Optional[str] = None
    prompt_override: Optional[str] = Field(default=None, max_length=8000)


@router.post("/{bot_id}/analyze", response_model=MeetingAnalysis)
async def analyze_bot(bot_id: str, request: Request, payload: AnalyzeRequest = AnalyzeRequest()):
    """(Re-)run AI analysis on the transcript.

    If transcription is still running, this request blocks until it finishes
    (up to 25 s) before running analysis.

    Use this to switch templates or run a custom prompt on an existing transcript.
    """
    account_id: Optional[str] = getattr(request.state, "account_id", None)
    sub_user_id = _get_sub_user_from_request(request)
    bot = await _get_or_404(bot_id, account_id, sub_user_id)
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


# ── GET /api/v1/bot/{id}/highlight ───────────────────────────────────────────

@router.get("/{bot_id}/highlight", response_model=HighlightResponse)
async def get_highlights(bot_id: str, request: Request):
    """Return curated meeting highlights derived from AI analysis.

    Aggregates key points, action items, and decisions into a flat highlight list.
    Returns 425 if analysis is not yet available.
    """
    account_id: Optional[str] = getattr(request.state, "account_id", None)
    sub_user_id = _get_sub_user_from_request(request)
    bot = await _get_or_404(bot_id, account_id, sub_user_id)
    bot = await _wait_for_transcript(bot)
    if not bot.analysis:
        raise HTTPException(
            status_code=425,
            detail="Analysis not yet available",
            headers={"Retry-After": "10"},
        )
    highlights: list[Highlight] = []
    for kp in bot.analysis.get("key_points", []):
        highlights.append(Highlight(type="key_point", text=kp))
    for ai in bot.analysis.get("action_items", []):
        highlights.append(Highlight(type="action_item", text=ai.get("task", str(ai)), detail=ai))
    for d in bot.analysis.get("decisions", []):
        highlights.append(Highlight(type="decision", text=d))
    return HighlightResponse(bot_id=bot_id, highlights=highlights)


# ── POST /api/v1/bot/{id}/ask ─────────────────────────────────────────────────

class AskRequest(BaseModel):
    question: str = Field(description="Free-form question about the meeting transcript.")


@router.post("/{bot_id}/ask")
async def ask_bot(bot_id: str, request: Request, payload: AskRequest):
    """Ask a free-form question about the meeting transcript."""
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="question is required")
    account_id: Optional[str] = getattr(request.state, "account_id", None)
    sub_user_id = _get_sub_user_from_request(request)
    bot = await _get_or_404(bot_id, account_id, sub_user_id)
    if not bot.transcript:
        raise HTTPException(status_code=425, detail="No transcript available yet")
    answer = await intelligence_service.ask_about_transcript(bot.transcript, question)
    return {"bot_id": bot_id, "question": question, "answer": answer}


# ── POST /api/v1/bot/{id}/followup-email ─────────────────────────────────────

@router.post("/{bot_id}/followup-email")
async def generate_followup_email(bot_id: str, request: Request):
    """Generate a draft follow-up email for the meeting."""
    account_id: Optional[str] = getattr(request.state, "account_id", None)
    sub_user_id = _get_sub_user_from_request(request)
    bot = await _get_or_404(bot_id, account_id, sub_user_id)
    if not bot.transcript and not bot.analysis:
        raise HTTPException(status_code=425, detail="No transcript or analysis available yet")
    result = await intelligence_service.generate_followup_email(
        transcript=bot.transcript or [],
        analysis=bot.analysis or {},
        participants=bot.participants or [],
    )
    return {"bot_id": bot_id, **result}
