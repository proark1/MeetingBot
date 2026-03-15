"""MeetingBot API — stateless meeting bot service.

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

from app.config import settings
from app.api.bots import router as bots_router, _queue_processor, _running_tasks
from app.api.webhooks import router as webhooks_router
from app.api.exports import router as exports_router
from app.api.templates import router as templates_router
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
    """If API_KEY is configured, validate the Bearer token on every API request."""
    if not settings.API_KEY:
        return
    if credentials is None or credentials.credentials != settings.API_KEY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing API key. Use: Authorization: Bearer <API_KEY>",
        )


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup warnings
    if not settings.GEMINI_API_KEY and not settings.ANTHROPIC_API_KEY:
        logger.warning(
            "⚠ Neither GEMINI_API_KEY nor ANTHROPIC_API_KEY is set — "
            "transcription and AI analysis will be DISABLED."
        )
    if not settings.API_KEY:
        logger.warning(
            "⚠ API_KEY is not set — all /api/v1/* endpoints are UNAUTHENTICATED. "
            "Set API_KEY in your environment to enable Bearer-token auth."
        )

    # Clean up orphaned subprocesses on SIGTERM
    def _handle_sigterm(signum, frame):
        from app.services.browser_bot import kill_all_procs
        logger.info("SIGTERM received — killing active subprocesses")
        kill_all_procs()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    # Start bot queue processor
    queue_task = asyncio.create_task(_queue_processor())
    logger.info("Bot queue processor started")

    # Start periodic cleanup of expired bots
    async def _cleanup_loop():
        from app.store import store
        while True:
            await asyncio.sleep(3600)  # every hour
            await store.cleanup_expired()

    cleanup_task = asyncio.create_task(_cleanup_loop())

    logger.info("MeetingBot ready — API docs at /api/docs")
    yield

    # Shutdown
    queue_task.cancel()
    cleanup_task.cancel()

    if _running_tasks:
        logger.info("Cancelling %d running bot task(s)…", len(_running_tasks))
        for task in list(_running_tasks.values()):
            task.cancel()
        await asyncio.gather(*list(_running_tasks.values()), return_exceptions=True)

    from app.services import webhook_service
    await webhook_service.close_http_client()
    logger.info("MeetingBot shut down")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="MeetingBot API",
    description=(
        "A **stateless meeting bot API** service. Send bots into **Zoom**, **Google Meet**, "
        "and **Microsoft Teams** meetings to record, transcribe, and analyse them with "
        "**Claude** (Anthropic) or **Gemini** (Google) AI.\n\n"
        "## How it works\n"
        "1. `POST /api/v1/bot` with your `meeting_url` and optional `webhook_url`\n"
        "2. The bot joins the meeting, records audio, and transcribes it\n"
        "3. Results are POSTed to your `webhook_url` when done (or poll `GET /api/v1/bot/{id}`)\n"
        "4. **You store the data** — this service keeps results in memory for 24 h only\n\n"
        "## Authentication\n"
        "If `API_KEY` is set, include `Authorization: Bearer <key>` on every request.\n\n"
        "## AI providers\n"
        "Set `ANTHROPIC_API_KEY` for Claude (preferred) or `GEMINI_API_KEY` for Gemini.\n\n"
        "## Bot lifecycle\n"
        "`ready` → `joining` → `in_call` → `call_ended` → `done` (or `error` / `cancelled`)\n\n"
        "## Auto-leave\n"
        "The bot leaves when alone for `BOT_ALONE_TIMEOUT` seconds (default 5 min).\n"
    ),
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

_origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
_wildcard = _origins == ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=not _wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
)

_auth = [Depends(require_api_key)]
app.include_router(bots_router,     prefix="/api/v1", dependencies=_auth)
app.include_router(webhooks_router, prefix="/api/v1", dependencies=_auth)
app.include_router(exports_router,  prefix="/api/v1", dependencies=_auth)
app.include_router(templates_router, prefix="/api/v1", dependencies=_auth)
app.include_router(ws_router,       prefix="/api/v1")  # WS auth handled separately


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["Health"])
@app.get("/api/health", tags=["Health"])
async def health():
    return {"status": "ok", "service": "MeetingBot", "version": "2.0.0"}


# ── Serve frontend ────────────────────────────────────────────────────────────

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_frontend(full_path: str = ""):
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
