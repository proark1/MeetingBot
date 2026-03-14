"""MeetingBot — Recall.ai clone.

Run with:
    uvicorn app.main:app --reload
"""

import asyncio
import logging
import signal
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings
from app.database import init_db, AsyncSessionLocal
from app.api.action_items import router as action_items_router
from app.api.analytics import router as analytics_router
from app.api.bots import router as bots_router, share_router, _queue_processor
from app.api.debug import router as debug_router
from app.api.exports import router as exports_router
from app.api.highlights import router as highlights_router
from app.api.search import router as search_router
from app.api.speakers import router as speakers_router
from app.api.templates import router as templates_router
from app.api.billing import router as billing_router
from app.api.webhooks import router as webhooks_router
from app.api.ws import router as ws_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

# ── Auth ──────────────────────────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)


async def require_api_key(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)],
) -> None:
    """If API_KEY is configured, validate the Bearer token on every API request.
    When API_KEY is empty (default), auth is disabled for backward compatibility."""
    if not settings.API_KEY:
        return  # auth disabled
    if credentials is None or credentials.credentials != settings.API_KEY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing API key. Use: Authorization: Bearer <API_KEY>",
        )


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initialising database…")
    await init_db()
    if not settings.GEMINI_API_KEY:
        logger.warning(
            "⚠ GEMINI_API_KEY is NOT set — transcription and analysis will be "
            "DISABLED.  Set it in Railway variables or your .env file."
        )
    if settings.SECRET_KEY == "meetingbot-dev-secret-change-in-production":
        logger.warning(
            "⚠ SECRET_KEY is using the insecure default value — set a strong random "
            "SECRET_KEY in your environment variables before deploying to production."
        )
    if not settings.API_KEY:
        logger.warning(
            "⚠ API_KEY is not set — all /api/v1/* endpoints are UNAUTHENTICATED. "
            "Set API_KEY in your environment variables to enable Bearer-token auth."
        )

    # Register SIGTERM handler to clean up orphaned browser subprocesses
    # (ffmpeg, Xvfb) that may be left running when Railway redeploys the container.
    def _handle_sigterm(signum, frame):
        from app.services.browser_bot import kill_all_procs
        logger.info("SIGTERM received — killing active subprocesses")
        kill_all_procs()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    # ── Scheduled background jobs ──────────────────────────────────────────────
    scheduler = AsyncIOScheduler(timezone="UTC")

    if settings.DIGEST_EMAIL:
        from app.services.digest_service import send_weekly_digest
        scheduler.add_job(
            send_weekly_digest,
            "cron",
            day_of_week="mon",
            hour=9,
            minute=0,
            args=[AsyncSessionLocal],
            id="weekly_digest",
            replace_existing=True,
        )
        logger.info(
            "Weekly digest scheduled — Mondays 09:00 UTC → %s",
            settings.DIGEST_EMAIL,
        )

    # ── Bot queue processor ────────────────────────────────────────────────────
    asyncio.create_task(_queue_processor())
    logger.info("Bot queue processor started")

    if settings.RECORDING_RETENTION_DAYS > 0:
        from app.services.cleanup_service import purge_old_recordings
        scheduler.add_job(
            purge_old_recordings,
            "cron",
            hour=3,
            minute=0,
            args=[AsyncSessionLocal],
            id="recording_cleanup",
            replace_existing=True,
        )
        logger.info(
            "Recording cleanup scheduled — daily 03:00 UTC (retention=%d days)",
            settings.RECORDING_RETENTION_DAYS,
        )

    if settings.CALENDAR_ICAL_URL:
        from app.services.calendar_service import sync_calendar
        scheduler.add_job(
            sync_calendar,
            "interval",
            minutes=5,
            args=[AsyncSessionLocal],
            id="calendar_sync",
            replace_existing=True,
        )
        logger.info("Calendar auto-join scheduled — polling every 5 min")

    scheduler.start()

    logger.info("MeetingBot ready")
    yield

    scheduler.shutdown(wait=False)

    # Cancel all running bot tasks so they clean up before the process exits
    from app.api.bots import _running_tasks
    if _running_tasks:
        logger.info("Cancelling %d running bot task(s)…", len(_running_tasks))
        for task in list(_running_tasks.values()):
            task.cancel()
        await asyncio.gather(*list(_running_tasks.values()), return_exceptions=True)

    # Close the persistent httpx client used by the webhook service
    from app.services import webhook_service
    await webhook_service.close_http_client()
    logger.info("MeetingBot shutting down")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="MeetingBot API",
    description=(
        "Send bots into **Zoom**, **Google Meet**, and **Microsoft Teams** meetings to record, "
        "transcribe, and analyse them with **Claude** (Anthropic) or **Gemini** (Google) AI.\n\n"
        "## Authentication\n"
        "If `API_KEY` is configured, include `Authorization: Bearer <key>` on every request. "
        "Leave `API_KEY` empty to disable authentication (development default).\n\n"
        "## AI providers\n"
        "Set `ANTHROPIC_API_KEY` to use **Claude** (takes precedence). "
        "Set `GEMINI_API_KEY` to use **Gemini**. Both keys can coexist; Claude is preferred.\n\n"
        "## Bot lifecycle\n"
        "`ready` / `scheduled` / `queued` → `joining` → `in_call` → `call_ended` → `done` (or `error` / `cancelled`)\n\n"
        "## Auto-leave behaviour\n"
        "The bot leaves automatically when it has been the **only participant** for "
        "`BOT_ALONE_TIMEOUT` seconds (default **5 minutes**).\n\n"
        "## Key features\n"
        "- **Recording** — WAV audio download (`GET /api/v1/bot/{id}/recording`)\n"
        "- **Transcription** — speaker-diarised transcript with timestamps\n"
        "- **AI analysis** — summary, key points, action items, decisions, sentiment, topics\n"
        "- **Chapter segmentation** — auto-generated named chapters with timestamps\n"
        "- **Live transcription** — real-time transcript streaming via WebSocket (`/api/v1/ws`)\n"
        "- **Voice responses** — bot speaks when its name is mentioned (Gemini Live / edge-tts)\n"
        "- **Ask Anything** — free-form Q&A on any transcript (`POST /api/v1/bot/{id}/ask`)\n"
        "- **Follow-up email** — AI-drafted email after the meeting\n"
        "- **Highlights** — bookmark key moments in a transcript\n"
        "- **Action items** — cross-meeting action item tracking\n"
        "- **Templates** — reusable custom analysis prompts\n"
        "- **Speaker profiles** — cross-meeting speaker stats (talk time, questions, meetings)\n"
        "- **Search** — full-text search across all transcripts\n"
        "- **Analytics** — usage analytics and dashboards\n"
        "- **Exports** — Markdown and PDF exports\n"
        "- **Webhooks** — event delivery to external URLs\n"
        "- **Billing** — Stripe checkout and usage-based subscription\n"
        "- **Bot queue** — concurrent-bot limit with FIFO queue (`MAX_CONCURRENT_BOTS`)\n"
        "- **Scheduled joins** — `join_at` for future meetings\n"
        "- **Integrations** — Slack, Notion, Linear, Jira, HubSpot, iCal auto-join\n"
        "- **Share links** — public read-only meeting reports\n"
    ),
    version="1.5.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# CORS — wildcard with credentials=True is rejected by browsers; when origins
# are restricted to specific domains, credentials are permitted.
_origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
_wildcard = _origins == ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=not _wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routes — all protected by the optional API key dependency
_auth = [Depends(require_api_key)]
app.include_router(bots_router,         prefix="/api/v1", dependencies=_auth)
app.include_router(exports_router,      prefix="/api/v1", dependencies=_auth)
app.include_router(debug_router,        prefix="/api/v1", dependencies=_auth)
app.include_router(webhooks_router,     prefix="/api/v1", dependencies=_auth)
app.include_router(highlights_router,   prefix="/api/v1", dependencies=_auth)
app.include_router(search_router,       prefix="/api/v1", dependencies=_auth)
app.include_router(analytics_router,    prefix="/api/v1", dependencies=_auth)
app.include_router(action_items_router, prefix="/api/v1", dependencies=_auth)
app.include_router(templates_router,    prefix="/api/v1", dependencies=_auth)
app.include_router(speakers_router,     prefix="/api/v1", dependencies=_auth)
app.include_router(billing_router,      prefix="/api/v1", dependencies=_auth)
app.include_router(ws_router,           prefix="/api/v1")  # WS auth handled separately
# Share endpoint is public (no API key required)
app.include_router(share_router, prefix="/api/v1")


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health", tags=["Health"])
@app.get("/api/health", tags=["Health"])
async def health():
    return {"status": "ok", "service": "MeetingBot"}


# ── Serve frontend ────────────────────────────────────────────────────────────

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_frontend(full_path: str = ""):
        # Do not swallow requests that look like API calls — return a proper 404
        # instead of serving index.html, which would give API clients a 200 HTML
        # response and make endpoint typos extremely hard to debug.
        if full_path.startswith("api/"):
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=404,
                content={"detail": f"API endpoint not found: /{full_path}"},
            )
        index = FRONTEND_DIR / "index.html"
        if index.exists():
            return FileResponse(str(index))
        return {"error": "Frontend not found"}
