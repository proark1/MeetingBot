"""MeetingBot API — stateless meeting bot service.

Run with:
    uvicorn app.main:app --reload
"""

import asyncio
import logging
import signal
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.api.bots import router as bots_router, _queue_processor, _running_tasks
from app.api.webhooks import router as webhooks_router
from app.api.exports import router as exports_router
from app.api.templates import router as templates_router
from app.api.ws import router as ws_router
from app.api.analytics import router as analytics_router
from app.api.auth import router as auth_router
from app.api.billing import router as billing_router
from app.api.ui import router as ui_router
from app.api.admin import router as admin_router
from app.deps import require_auth

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Database init ──────────────────────────────────────────────────────
    from app.db import create_all_tables
    await create_all_tables()
    logger.info("Database tables ready (%s)", settings.DATABASE_URL.split("///")[0])

    # Startup warnings
    if not settings.GEMINI_API_KEY and not settings.ANTHROPIC_API_KEY:
        logger.warning(
            "⚠ Neither GEMINI_API_KEY nor ANTHROPIC_API_KEY is set — "
            "transcription and AI analysis will be DISABLED."
        )
    if not settings.API_KEY:
        logger.warning(
            "⚠ API_KEY is not set — using per-user account authentication. "
            "Register at POST /api/v1/auth/register"
        )
    if not settings.STRIPE_SECRET_KEY:
        logger.warning("⚠ STRIPE_SECRET_KEY not set — Stripe card payments disabled")
    if not settings.CRYPTO_RPC_URL:
        logger.info("USDC payments disabled — set CRYPTO_RPC_URL to enable")

    # ── USDC monitor ──────────────────────────────────────────────────────
    from app.services.crypto_service import start_usdc_monitor
    await start_usdc_monitor()

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
        "A **multi-tenant meeting bot API** service. Send bots into **Zoom**, **Google Meet**, "
        "and **Microsoft Teams** meetings to record, transcribe, and analyse them with "
        "**Claude** (Anthropic) or **Gemini** (Google) AI.\n\n"

        "## How it works\n"
        "1. Register an account → receive an `sk_live_...` API key\n"
        "2. Top up credits via **Stripe card** or **USDC (ERC-20)**\n"
        "3. `POST /api/v1/bot` with your `meeting_url` and optional `webhook_url`\n"
        "4. A headless Chromium bot joins the meeting, records audio, and transcribes it\n"
        "5. Results are POSTed to your `webhook_url` when done (or poll `GET /api/v1/bot/{id}`)\n"
        "6. **You store the data** — this service keeps results in memory for 24 h only\n\n"

        "## Authentication\n"
        "All API calls (except `/api/v1/auth/register` and `/api/v1/auth/login`) require:\n"
        "```\nAuthorization: Bearer sk_live_<your-api-key>\n```\n"
        "The legacy `API_KEY` environment variable acts as a superadmin bypass and skips "
        "per-user account checks. Leave it unset to enforce per-user auth.\n\n"

        "## Accounts & API keys\n"
        "Register at `POST /api/v1/auth/register` to receive your first `sk_live_...` key. "
        "Generate additional named keys with `POST /api/v1/auth/keys`. "
        "Revoke individual keys with `DELETE /api/v1/auth/keys/{id}`.\n\n"
        "**USDC wallet:** Register your Ethereum wallet with `PUT /api/v1/auth/wallet` so "
        "the platform can automatically attribute USDC deposits to your account.\n\n"

        "## Credits & billing\n"
        "Each bot run deducts credits equal to the raw AI cost × `CREDIT_MARKUP` (default 3×). "
        "A minimum balance of `MIN_CREDITS_USD` (default $0.05) is required to create a bot.\n\n"
        "**Top up via Stripe card:** `POST /api/v1/billing/stripe/checkout` — returns a "
        "Stripe Checkout URL. Credits are added automatically once payment is confirmed via webhook.\n\n"
        "**Top up via USDC (ERC-20):** First register your Ethereum wallet via "
        "`PUT /api/v1/auth/wallet`. Then `GET /api/v1/billing/usdc/address` returns the "
        "platform deposit address. Send USDC **from your registered wallet** to that address; "
        "the system matches the `from` address to your account. 1 USDC = $1 credit, "
        "credited automatically within ~1 minute after on-chain confirmation.\n\n"
        "**Check balance:** `GET /api/v1/billing/balance` — returns current `credits_usd` and "
        "the last 50 transactions. Transaction `type` values: `stripe_topup`, `usdc_topup`, `bot_usage`.\n\n"

        "## Admin\n"
        "Admin endpoints are restricted to designated admin accounts only.\n\n"
        "- `GET /api/v1/admin/wallet` — view the current platform USDC collection wallet\n"
        "- `PUT /api/v1/admin/wallet` — set or update the platform wallet address\n"
        "- `GET /api/v1/admin/config` — list all platform configuration values\n\n"
        "Admin web UI is available at `/admin`. "
        "Only accounts with admin privileges can access these endpoints (HTTP 403 for others).\n\n"

        "## AI providers\n"
        "Set `ANTHROPIC_API_KEY` for Claude (preferred) or `GEMINI_API_KEY` for Gemini. "
        "Claude is used for both transcription (Haiku) and analysis (Sonnet/Opus).\n\n"

        "## Bot lifecycle\n"
        "`ready` / `scheduled` / `queued` → `joining` → `in_call` → `call_ended` → "
        "`transcribing` → `done` (or `error` / `cancelled`)\n\n"
        "- **`scheduled`** — bot has a future `join_at` time and is waiting\n"
        "- **`queued`** — concurrency limit reached; bot is waiting for a free slot\n\n"

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

_auth = [Depends(require_auth)]
app.include_router(auth_router,      prefix="/api/v1")             # no auth on register/login
app.include_router(billing_router,   prefix="/api/v1")             # billing has its own auth handling
app.include_router(bots_router,      prefix="/api/v1", dependencies=_auth)
app.include_router(webhooks_router,  prefix="/api/v1", dependencies=_auth)
app.include_router(exports_router,   prefix="/api/v1", dependencies=_auth)
app.include_router(templates_router, prefix="/api/v1", dependencies=_auth)
app.include_router(analytics_router, prefix="/api/v1", dependencies=_auth)
app.include_router(admin_router,     prefix="/api/v1")             # admin has its own auth (require_admin)
app.include_router(ws_router,        prefix="/api/v1")             # WS auth handled separately
app.include_router(ui_router)                                       # web UI (no prefix)


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
