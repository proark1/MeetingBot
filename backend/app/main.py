"""MeetingBot — Recall.ai clone.

Run with:
    uvicorn app.main:app --reload
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initialising database…")
    await init_db()
    if not settings.GEMINI_API_KEY:
        logger.warning(
            "⚠ GEMINI_API_KEY is NOT set — transcription and analysis will be "
            "DISABLED.  Set it in Railway variables or your .env file."
        )
    logger.info("MeetingBot ready")
    yield
    logger.info("MeetingBot shutting down")


app = FastAPI(
    title="MeetingBot API",
    description=(
        "Send bots into Zoom, Google Meet, and Teams meetings to record, transcribe, "
        "and analyse them with Gemini AI.\n\n"
        "## Bot lifecycle\n"
        "`joining` → `in_call` → `call_ended` → `done` (or `error`)\n\n"
        "## Auto-leave behaviour\n"
        "The bot leaves automatically when it has been the **only participant** for "
        "`BOT_ALONE_TIMEOUT` seconds (default **5 minutes**). This covers two cases:\n"
        "- **Empty room on join** — admitted to an empty meeting; leaves if no one joins.\n"
        "- **Everyone left** — all other participants leave mid-call; leaves if no one rejoins.\n\n"
        "The timer resets whenever other participants are detected again."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routes
app.include_router(bots_router, prefix="/api/v1")
app.include_router(debug_router, prefix="/api/v1")
app.include_router(webhooks_router, prefix="/api/v1")
app.include_router(ws_router, prefix="/api/v1")


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
