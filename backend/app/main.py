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

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

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
from app.api.integrations import router as integrations_router
from app.api.calendar import router as calendar_router
from app.api.oauth import router as oauth_router
from app.api.metrics import router as metrics_router, PrometheusMiddleware
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
    # ── Database init (with retry) ────────────────────────────────────────
    # Retry up to 5 times with a 5 s delay so a slow-starting PostgreSQL
    # (common in Railway where the DB and app containers boot in parallel)
    # doesn't permanently break all DB-backed endpoints.
    from app.db import create_all_tables
    _db_ready = False
    for _attempt in range(1, 6):
        try:
            await asyncio.wait_for(create_all_tables(), timeout=15.0)
            _db_ready = True
            logger.info("Database tables ready (%s)", settings.DATABASE_URL.split("///")[0])
            break
        except asyncio.TimeoutError:
            logger.warning("DB init attempt %d/5 timed out after 15 s", _attempt)
        except Exception as _exc:
            logger.warning("DB init attempt %d/5 failed: %s", _attempt, _exc)
        if _attempt < 5:
            await asyncio.sleep(5)
    if not _db_ready:
        logger.error(
            "Database initialization failed after 5 attempts — "
            "check DATABASE_URL / DB connectivity. DB-backed endpoints will fail."
        )

    # ── Startup validation ────────────────────────────────────────────────
    if settings.JWT_SECRET == "change-me-in-production":
        import secrets as _secrets
        settings.JWT_SECRET = _secrets.token_hex(32)
        logger.warning(
            "⚠ JWT_SECRET is the insecure default — a random secret was generated for this "
            "session. Web UI sessions will be invalidated on every restart. "
            "Set JWT_SECRET to a stable value in your environment:\n"
            "  export JWT_SECRET=$(openssl rand -hex 32)"
        )

    if settings.CORS_ORIGINS == "*":
        logger.warning(
            "⚠ CORS_ORIGINS='*' — all browser origins can call this API. "
            "Set CORS_ORIGINS to your frontend domain(s) before going to production."
        )

    if not settings.ADMIN_EMAILS:
        logger.warning(
            "⚠ ADMIN_EMAILS is not set — admin endpoints are only accessible to "
            "accounts with is_admin=True in the database."
        )

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

    # ── Load persisted bots ───────────────────────────────────────────────
    from app.store import load_persisted_bots, load_persisted_webhooks
    try:
        restored = await asyncio.wait_for(load_persisted_bots(), timeout=15.0)
        if restored:
            logger.info("Restored %d bot(s) from previous run", restored)
    except Exception as exc:
        logger.warning("Could not restore persisted bots: %s", exc)

    # ── Load persisted webhooks ───────────────────────────────────────────
    try:
        restored_webhooks = await asyncio.wait_for(load_persisted_webhooks(), timeout=15.0)
        if restored_webhooks:
            logger.info("Restored %d webhook(s) from previous run", restored_webhooks)
    except Exception as exc:
        logger.warning("Could not restore persisted webhooks: %s", exc)

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

    # Start webhook retry loop
    from app.services.webhook_service import webhook_retry_loop
    webhook_retry_task = asyncio.create_task(webhook_retry_loop())
    logger.info("Webhook retry loop started")

    # Start calendar auto-join poll loop
    from app.services.calendar_service import calendar_poll_loop
    calendar_task = asyncio.create_task(
        calendar_poll_loop(interval_s=settings.CALENDAR_POLL_INTERVAL_S)
    )

    logger.info("MeetingBot ready — API docs at /api/docs")
    yield

    # Shutdown
    queue_task.cancel()
    cleanup_task.cancel()
    webhook_retry_task.cancel()
    calendar_task.cancel()

    if _running_tasks:
        logger.info("Cancelling %d running bot task(s)…", len(_running_tasks))
        for task in list(_running_tasks.values()):
            task.cancel()
        await asyncio.gather(*list(_running_tasks.values()), return_exceptions=True)

    from app.services import webhook_service
    await webhook_service.close_http_client()
    logger.info("MeetingBot shut down")


# ── App ───────────────────────────────────────────────────────────────────────

_limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="MeetingBot API",
    description=(
        "A **multi-tenant meeting bot API** service. Send bots into **Zoom**, **Google Meet**, "
        "and **Microsoft Teams** meetings to record, transcribe, and analyse them with "
        "**Claude** (Anthropic) or **Gemini** (Google) AI.\n\n"

        "## How it works\n"
        "1. Register an account (email/password or Google/Microsoft SSO) → receive an `sk_live_...` API key\n"
        "2. Top up credits via **Stripe card** or **USDC (ERC-20)**\n"
        "3. `POST /api/v1/bot` with your `meeting_url` and optional `webhook_url`\n"
        "4. A headless Chromium bot joins the meeting, records audio (and optionally video), and transcribes it\n"
        "5. Results are POSTed to your `webhook_url` when done (or poll `GET /api/v1/bot/{id}`)\n"
        "6. **You store the data** — this service keeps results in memory for 24 h only\n\n"

        "## Authentication\n"
        "All API calls (except `/api/v1/auth/register` and `/api/v1/auth/login`) require:\n"
        "```\nAuthorization: Bearer sk_live_<your-api-key>\n```\n"
        "API keys are prefixed with `sk_live_` and shown **once** at creation — copy immediately. "
        "The legacy `API_KEY` environment variable acts as a superadmin bypass and skips "
        "per-user account checks. Leave it unset to enforce per-user auth.\n\n"

        "## Accounts & API keys\n"
        "Register at `POST /api/v1/auth/register` to receive your first `sk_live_...` key. "
        "Generate additional named keys with `POST /api/v1/auth/keys`. "
        "Revoke individual keys with `DELETE /api/v1/auth/keys/{id}`.\n\n"
        "**Account types:** Pass `account_type: \"personal\"` (default) or `\"business\"` at "
        "registration. See the **Business accounts** section below.\n\n"
        "**USDC wallet:** Register your Ethereum wallet with `PUT /api/v1/auth/wallet` so "
        "the platform can automatically attribute USDC deposits to your account.\n\n"
        "**Notification preferences:** Configure email alerts with `GET/PUT /api/v1/auth/notify`. "
        "Enable `notify_on_done` to receive an email when each bot finishes analysis.\n\n"
        "**Subscription plan:** View your plan and monthly usage with `GET /api/v1/auth/plan`. "
        "Plans: `free` (5 bots/mo), `starter` (50), `pro` (500), `business` (unlimited).\n\n"
        "**GDPR erasure:** Permanently delete your account and all data with "
        "`DELETE /api/v1/auth/account`.\n\n"

        "## SSO — Google & Microsoft OAuth2\n"
        "Sign in or register with an existing Google or Microsoft account (when configured by "
        "the platform admin via `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` or "
        "`MICROSOFT_CLIENT_ID`/`MICROSOFT_CLIENT_SECRET`).\n\n"
        "- `GET /api/v1/auth/oauth/{provider}/authorize` — redirect to provider login "
        "(`provider`: `google` or `microsoft`). Pass `?redirect=1` to use the cookie flow "
        "for the web UI.\n"
        "- `GET /api/v1/auth/oauth/{provider}/callback` — OAuth2 callback; returns "
        "`{account_id, email, api_key, access_token, is_new_account}`.\n"
        "On first login the response includes a new `sk_live_...` API key. Subsequent logins "
        "return an empty `api_key` — use your existing key.\n\n"

        "## Business accounts (multi-user data isolation)\n"
        "Business accounts are for **platforms integrating MeetingBot on behalf of multiple "
        "end-users**. A single business account uses one API key and one shared credit balance, "
        "but isolates all bot data between end-users.\n\n"
        "**How to use:** Pass the `X-Sub-User: <user-id>` header on every request to scope "
        "data to a specific end-user. Users with different sub-user IDs cannot see each other's "
        "bots, transcripts, or analyses. Omit the header for an account-wide view of all bots.\n\n"
        "**Alternatively**, pass `sub_user_id` in the `POST /api/v1/bot` request body — "
        "the body field takes precedence over the header.\n\n"
        "`X-Sub-User` is an opaque string (max 255 chars): user ID, email, UUID, etc.\n\n"

        "## Credits & billing\n"
        "Credits are deducted per bot run. Default: `BOT_FLAT_FEE_USD` = $0.10 flat fee per bot. "
        "When flat fee is disabled (`BOT_FLAT_FEE_USD=0`), billing uses raw AI cost × "
        "`CREDIT_MARKUP` (default 3×). "
        "A minimum balance of `MIN_CREDITS_USD` (default $0.10) is required to create a bot "
        "(HTTP 402 if below this threshold).\n\n"
        "**Top up via Stripe card:** `POST /api/v1/billing/stripe/checkout` — returns a "
        "Stripe Checkout URL. Credits are applied automatically once payment is confirmed "
        "via the Stripe webhook. Valid amounts are set by `STRIPE_TOP_UP_AMOUNTS` "
        "(default: 10, 25, 50, 100 USD).\n\n"
        "**Top up via USDC (ERC-20):** First register your Ethereum wallet via "
        "`PUT /api/v1/auth/wallet`. Then `GET /api/v1/billing/usdc/address` returns the "
        "platform deposit address. Send USDC **from your registered wallet** — the system "
        "matches the `from` address to your account. 1 USDC = $1 credit, credited "
        "automatically within ~1 minute of on-chain confirmation.\n\n"
        "**Check balance:** `GET /api/v1/billing/balance` — returns current `credits_usd` and "
        "the last 50 transactions. Transaction `type` values: `stripe_topup`, `usdc_topup`, "
        "`bot_usage`.\n\n"

        "## Webhooks (global, with retry & delivery logs)\n"
        "Register a global webhook with `POST /api/v1/webhook` to receive events for **all** bots. "
        "For per-bot webhooks, pass `webhook_url` when creating a bot instead.\n\n"
        "**Events:** `bot.joining`, `bot.in_call`, `bot.call_ended`, `bot.transcript_ready`, "
        "`bot.analysis_ready`, `bot.done`, `bot.error`, `bot.cancelled`. "
        "Use `events: [\"*\"]` to receive all (default).\n\n"
        "**Retry logic:** Failed deliveries are retried up to `WEBHOOK_MAX_ATTEMPTS` times "
        "(default 5) with exponential backoff: 1 min, 5 min, 25 min, 2 h, 10 h.\n\n"
        "**Signatures:** Set a `secret` when registering — each delivery includes an "
        "`X-MeetingBot-Signature: sha256=<hmac>` header for verification.\n\n"
        "**Delivery logs:** `GET /api/v1/webhook/{id}/deliveries` — paginated delivery history "
        "with status, HTTP response code, error message, and next retry time.\n\n"
        "**Test endpoint:** `POST /api/v1/webhook/{id}/test` — send a test event immediately.\n\n"

        "## Integrations (Slack & Notion)\n"
        "Push meeting notes automatically to third-party tools after each bot session.\n\n"
        "- `POST /api/v1/integrations` — create an integration (`type`: `slack` or `notion`)\n"
        "- **Slack:** provide `config.webhook_url` (Incoming Webhook URL)\n"
        "- **Notion:** provide `config.api_token` and `config.database_id`\n"
        "- `GET /api/v1/integrations` — list all integrations (secrets redacted)\n"
        "- `PATCH /api/v1/integrations/{id}` — update config\n"
        "- `DELETE /api/v1/integrations/{id}` — remove integration\n\n"
        "After bot analysis completes, summaries and action items are pushed to all active "
        "integrations for that account.\n\n"

        "## Calendar auto-join\n"
        "Connect an iCal feed so MeetingBot automatically dispatches bots to upcoming meetings.\n\n"
        "- `POST /api/v1/calendar` — add an iCal feed (`ical_url`, `name`, `bot_name?`, `auto_record`)\n"
        "- `GET /api/v1/calendar` — list all calendar feeds\n"
        "- `PATCH /api/v1/calendar/{id}` — update a feed\n"
        "- `DELETE /api/v1/calendar/{id}` — remove a feed\n"
        "- `POST /api/v1/calendar/{id}/sync` — manually trigger an immediate sync\n\n"
        "The background service polls all active feeds every `CALENDAR_POLL_INTERVAL_S` seconds "
        "(default 5 min) and auto-creates bots for meetings starting within the next 10 minutes.\n\n"

        "## Idempotency keys\n"
        "Prevent duplicate bots from network retries by passing a unique key on bot creation:\n"
        "```\nIdempotency-Key: <your-unique-key>\n```\n"
        "If a second request with the same key arrives within `IDEMPOTENCY_TTL_HOURS` (default 24 h), "
        "the original bot is returned instead of creating a new one. Keys are scoped to your account.\n\n"

        "## Bot persona\n"
        "Customize how the bot appears in meetings with `bot_name` and `bot_avatar_url` fields "
        "on `POST /api/v1/bot`. The platform default avatar is set via `DEFAULT_BOT_AVATAR_URL`.\n\n"

        "## Video recording\n"
        "When `VIDEO_RECORDING_ENABLED=true` (default), the bot captures a screen recording "
        "alongside audio. Video is encoded with ffmpeg (configurable `VIDEO_CRF`, `VIDEO_FPS`, "
        "`VIDEO_SCALE`). Download the recording via `GET /api/v1/bot/{id}/recording`.\n\n"

        "## Cloud storage\n"
        "Set `STORAGE_BACKEND=s3` to upload recordings to S3-compatible storage (AWS S3, "
        "Cloudflare R2, MinIO). Configure with `S3_BUCKET`, `S3_ENDPOINT_URL`, "
        "`S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`. Set `S3_PUBLIC_URL` for CDN-served links.\n"
        "Default: `local` — recordings are stored on disk with automatic cleanup after "
        "`RECORDING_RETENTION_DAYS` days.\n\n"

        "## Exports\n"
        "Export meeting reports in multiple formats:\n"
        "- `GET /api/v1/bot/{id}/export/markdown` — Markdown report with transcript, summary, and action items\n"
        "- `GET /api/v1/bot/{id}/export/pdf` — PDF report (same content)\n\n"

        "## Analytics\n"
        "`GET /api/v1/analytics` — platform-wide bot statistics including sentiment distribution, "
        "meetings per day, top topics, top participants, and platform breakdown.\n\n"

        "## Analysis templates\n"
        "Use predefined templates to customize analysis output:\n"
        "- `GET /api/v1/templates` — list all templates (default, sales, standup, 1:1, retro, etc.)\n"
        "- Pass `template: \"<seed>\"` when creating a bot, or `prompt_override` for a one-off prompt\n"
        "- `GET /api/v1/templates/default-prompt` — get the raw default analysis prompt\n\n"

        "## Admin\n"
        "Admin endpoints are restricted to accounts in `ADMIN_EMAILS` (env var) or with "
        "`is_admin=true` in the database. All others receive HTTP 403.\n\n"
        "**USDC & billing:**\n"
        "- `GET/PUT /api/v1/admin/wallet` — view or set the platform USDC collection wallet\n"
        "- `GET/PUT /api/v1/admin/rpc-url` — view or set the Ethereum RPC URL (no restart needed)\n"
        "- `GET /api/v1/admin/config` — list all platform configuration values\n"
        "- `POST /api/v1/admin/credit` — manually credit a user account\n"
        "- `GET /api/v1/admin/usdc/unmatched` — list USDC transfers that couldn't be attributed\n"
        "- `POST /api/v1/admin/usdc/unmatched/{tx_hash}/resolve` — mark unmatched transfer resolved\n"
        "- `POST /api/v1/admin/usdc/rescan` — reset USDC monitor block pointer for rescan\n\n"
        "**Admin web UI** at `/admin` provides:\n"
        "- Platform stats (accounts, credits, revenue, unmatched transfers)\n"
        "- Subscription plan breakdown (Free/Starter/Pro/Business counts)\n"
        "- Bot activity stats + platform feature counters (webhooks, integrations, calendar feeds, SSO)\n"
        "- System status indicators (Stripe, RPC, HD seed, Email, Storage, Video, Google SSO, Microsoft SSO)\n"
        "- Inline plan management — change any account's plan via a dropdown (`POST /admin/accounts/{id}/set-plan`)\n"
        "- Monthly bot usage per account\n"
        "- Manual credit form, USDC rescan, wallet & RPC configuration\n"
        "- Unmatched USDC transfer resolution\n"
        "- User account enable/disable/admin-toggle\n\n"

        "## Prometheus metrics\n"
        "`GET /metrics` — Prometheus-compatible metrics endpoint (unauthenticated). "
        "Includes HTTP request counts/latencies, active bot counts, and billing totals.\n\n"

        "## Bot response & analysis\n"
        "Bot responses include: `id`, `status`, `meeting_platform`, `participants`, `transcript` "
        "(`[{speaker, text, timestamp}]`), `analysis` (`{summary, key_points, action_items, "
        "decisions, next_steps, sentiment, topics}`), `chapters` (`[{title, start_time, summary}]`), "
        "`speaker_stats` (`[{name, talk_time_s, talk_pct, turns}]`), `recording_available`, "
        "`is_demo_transcript`, `sub_user_id`, `metadata`, and `ai_usage` "
        "(`{total_tokens, total_cost_usd, primary_model, operations: [{operation, provider, model, "
        "input_tokens, output_tokens, total_tokens, cost_usd, duration_s}]}`).\n\n"

        "## AI providers\n"
        "Set `ANTHROPIC_API_KEY` for Claude (preferred) or `GEMINI_API_KEY` for Gemini. "
        "When both are set, Claude takes precedence for transcription and analysis.\n\n"

        "## Bot lifecycle\n"
        "`ready` / `scheduled` / `queued` → `joining` → `in_call` → `call_ended` → "
        "`transcribing` → `done` (or `error` / `cancelled`)\n\n"
        "- **`scheduled`** — bot has a future `join_at` time and is waiting\n"
        "- **`queued`** — `MAX_CONCURRENT_BOTS` limit reached; waiting for a free slot\n"
        "- **`done`** — transcript + analysis complete; results available for 24 hours\n\n"

        "## Rate limits\n"
        "- `POST /api/v1/auth/register` — 3 requests/min per IP\n"
        "- `POST /api/v1/auth/login` — 5 requests/min per IP\n"
        "- `POST /api/v1/bot` — 20 requests/min per IP\n\n"
        "Exceeded limits return HTTP 429.\n\n"

        "## Real-time events (WebSocket)\n"
        "Connect to `ws://<host>/api/v1/ws` for live bot status updates. "
        "The server broadcasts events whenever a bot's status changes.\n\n"

        "## Auto-leave\n"
        "The bot leaves automatically when it has been the only participant for "
        "`BOT_ALONE_TIMEOUT` seconds (default 5 min).\n\n"

        "## Web UI\n"
        "| Path | Description |\n"
        "|------|-------------|\n"
        "| `/register` | Create account (Personal or Business); Google/Microsoft SSO sign-up |\n"
        "| `/login` | Login with email/password or SSO |\n"
        "| `/dashboard` | Balance, API keys, plan & usage, notification prefs, USDC wallet, "
        "linked SSO accounts, integrations summary, calendar feeds, transactions |\n"
        "| `/topup` | Add credits via Stripe card or USDC |\n"
        "| `/admin` | Platform admin — plan stats, bot activity, system status, "
        "user accounts with inline plan management, unmatched USDC transfers, manual credit |\n"
    ),
    version="2.2.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

app.state.limiter = _limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

_origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
_wildcard = _origins == ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=not _wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(PrometheusMiddleware)

_auth = [Depends(require_auth)]
app.include_router(metrics_router)                                     # unauthenticated /metrics
app.include_router(auth_router,         prefix="/api/v1")             # no auth on register/login
app.include_router(oauth_router,        prefix="/api/v1")             # SSO OAuth (no prefix auth)
app.include_router(billing_router,      prefix="/api/v1")             # billing has its own auth handling
app.include_router(bots_router,         prefix="/api/v1", dependencies=_auth)
app.include_router(webhooks_router,     prefix="/api/v1", dependencies=_auth)
app.include_router(exports_router,      prefix="/api/v1", dependencies=_auth)
app.include_router(templates_router,    prefix="/api/v1", dependencies=_auth)
app.include_router(analytics_router,    prefix="/api/v1", dependencies=_auth)
app.include_router(integrations_router, prefix="/api/v1", dependencies=_auth)
app.include_router(calendar_router,     prefix="/api/v1", dependencies=_auth)
app.include_router(admin_router,        prefix="/api/v1")             # admin has its own auth (require_admin)
app.include_router(ws_router,           prefix="/api/v1")             # WS auth handled separately
app.include_router(ui_router)                                          # web UI (no prefix)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["Health"])
@app.get("/api/health", tags=["Health"])
async def health():
    return {"status": "ok", "service": "MeetingBot", "version": "2.2.0"}


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
