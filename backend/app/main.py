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

from app.config import settings
from app.database import init_db
from app.api.bots import router as bots_router
from app.api.debug import router as debug_router
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

    # Register SIGTERM handler to clean up orphaned browser subprocesses
    # (ffmpeg, Xvfb) that may be left running when Railway redeploys the container.
    def _handle_sigterm(signum, frame):
        from app.services.browser_bot import kill_all_procs
        logger.info("SIGTERM received — killing active subprocesses")
        kill_all_procs()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    logger.info("MeetingBot ready")
    yield

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
        "Send bots into Zoom, Google Meet, and Teams meetings to record, transcribe, "
        "and analyse them with Gemini AI.\n\n"
        "## Authentication\n"
        "If `API_KEY` is configured, include `Authorization: Bearer <key>` on every request.\n\n"
        "## Bot lifecycle\n"
        "`joining` → `in_call` → `call_ended` → `done` (or `error`)\n\n"
        "## Auto-leave behaviour\n"
        "The bot leaves automatically when it has been the **only participant** for "
        "`BOT_ALONE_TIMEOUT` seconds (default **5 minutes**)."
    ),
    version="1.0.0",
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
app.include_router(bots_router,     prefix="/api/v1", dependencies=_auth)
app.include_router(debug_router,    prefix="/api/v1", dependencies=_auth)
app.include_router(webhooks_router, prefix="/api/v1", dependencies=_auth)
app.include_router(ws_router,       prefix="/api/v1")  # WS auth handled separately


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
        index = FRONTEND_DIR / "index.html"
        if index.exists():
            return FileResponse(str(index))
        return {"error": "Frontend not found"}
