"""JustHereToListen.io API — stateless meeting bot service.

Run with:
    uvicorn app.main:app --reload
"""

import asyncio
import logging
import signal
from contextlib import asynccontextmanager
from pathlib import Path

# Read version from VERSION file (check multiple possible locations)
_VERSION_FILE = None
for _candidate in [
    Path(__file__).resolve().parent.parent.parent / "VERSION",  # repo root (local dev)
    Path(__file__).resolve().parent.parent / "VERSION",          # /app/VERSION (Docker)
    Path("/app/VERSION"),                                        # absolute Docker path
]:
    if _candidate.exists():
        _VERSION_FILE = _candidate
        break
_APP_VERSION = _VERSION_FILE.read_text().strip() if _VERSION_FILE else "2.19.0"

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app._limiter import limiter as _limiter

from fastapi.responses import JSONResponse as _JSONResponse

from app.config import settings
from fastapi.openapi.docs import get_swagger_ui_html as _get_swagger_ui_html
from fastapi.openapi.utils import get_openapi as _get_openapi_util
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
# New feature routers
from app.api.retention import router as retention_router
from app.api.keyword_alerts import router as keyword_alerts_router
from app.api.workspaces import router as workspaces_router
from app.api.saml import router as saml_router
from app.api.mcp import router as mcp_router
from app.api.action_items import router as action_items_router
from app.deps import require_auth
from typing import Any

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

    # ── Load persisted state (parallel for faster startup) ─────────────────
    from app.store import load_persisted_bots, load_persisted_webhooks
    from app.services.crypto_service import start_usdc_monitor

    async def _load_bots():
        try:
            restored = await asyncio.wait_for(load_persisted_bots(), timeout=15.0)
            if restored:
                logger.info("Restored %d bot(s) from previous run", restored)
        except Exception as exc:
            logger.warning("Could not restore persisted bots: %s", exc)

    async def _load_webhooks():
        try:
            restored_webhooks = await asyncio.wait_for(load_persisted_webhooks(), timeout=15.0)
            if restored_webhooks:
                logger.info("Restored %d webhook(s) from previous run", restored_webhooks)
        except Exception as exc:
            logger.warning("Could not restore persisted webhooks: %s", exc)

    await asyncio.gather(_load_bots(), _load_webhooks(), start_usdc_monitor())

    # Clean up orphaned subprocesses on SIGTERM
    def _handle_sigterm(signum, frame):
        from app.services.browser_bot import kill_all_procs
        logger.info("SIGTERM received — killing active subprocesses")
        kill_all_procs()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    async def _supervised(name: str, coro_fn, *args, max_restarts: int = 20, **kwargs) -> None:
        """Run a background coroutine and restart it if it crashes.

        Without this, a single unhandled exception in a background task silently
        kills the task — webhooks stop retrying, cleanup stops running, etc.
        This wrapper logs the crash and restarts with exponential backoff.
        """
        restarts = 0
        backoff = 5.0
        while restarts < max_restarts:
            try:
                await coro_fn(*args, **kwargs)
                # Coroutine returned normally (shouldn't happen for loops) — restart cleanly
                restarts = 0
                backoff = 5.0
            except asyncio.CancelledError:
                logger.info("Background task %r cancelled", name)
                return
            except Exception:
                restarts += 1
                logger.exception(
                    "Background task %r crashed (restart %d/%d) — retrying in %.0f s",
                    name, restarts, max_restarts, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300.0)
        logger.critical("Background task %r exceeded max restarts (%d) — giving up", name, max_restarts)

    # Start bot queue processor
    queue_task = asyncio.create_task(_supervised("queue_processor", _queue_processor))
    logger.info("Bot queue processor started")

    # Start periodic cleanup of expired bots
    async def _cleanup_loop():
        from app.store import store
        while True:
            await asyncio.sleep(3600)  # every hour
            await store.cleanup_expired()

    cleanup_task = asyncio.create_task(_supervised("cleanup_loop", _cleanup_loop))

    # Start webhook retry loop
    from app.services.webhook_service import webhook_retry_loop
    webhook_retry_task = asyncio.create_task(_supervised("webhook_retry", webhook_retry_loop))
    logger.info("Webhook retry loop started")

    # Start calendar auto-join poll loop
    from app.services.calendar_service import calendar_poll_loop
    calendar_task = asyncio.create_task(
        _supervised("calendar_poll", calendar_poll_loop, interval_s=settings.CALENDAR_POLL_INTERVAL_S)
    )

    # Start retention enforcement loop (daily)
    async def _retention_loop():
        while True:
            await asyncio.sleep(86400)  # every 24 hours
            try:
                await _enforce_retention_policies()
            except Exception as exc:
                logger.error("Retention enforcement error: %s", exc)

    async def _enforce_retention_policies():
        """Delete recordings and bot snapshots beyond their retention period."""
        import json as _json
        import os
        from datetime import datetime, timezone, timedelta
        from app.db import AsyncSessionLocal
        from app.models.account import BotSnapshot, RetentionPolicy
        from sqlalchemy import select, delete as _sqldelete

        now = datetime.now(timezone.utc)
        logger.info("Running retention policy enforcement…")

        async with AsyncSessionLocal() as db:
            # Load global policy (account_id IS NULL)
            g_result = await db.execute(
                select(RetentionPolicy).where(RetentionPolicy.account_id.is_(None))
            )
            global_policy = g_result.scalar_one_or_none()
            global_bot_days = settings.DEFAULT_BOT_RETENTION_DAYS if not global_policy else global_policy.bot_retention_days
            global_rec_days = settings.DEFAULT_RECORDING_RETENTION_DAYS if not global_policy else global_policy.recording_retention_days

            # Find snapshots past their global retention window
            if global_bot_days != -1:
                cutoff = now - timedelta(days=global_bot_days)
                result = await db.execute(
                    select(BotSnapshot).where(BotSnapshot.created_at < cutoff)
                )
                expired_snaps = result.scalars().all()

                # Pre-load ALL per-account retention policies in one query (avoids N+1)
                account_ids = {snap.account_id for snap in expired_snaps if snap.account_id}
                policies_by_account: dict = {}
                if account_ids:
                    pol_result = await db.execute(
                        select(RetentionPolicy).where(RetentionPolicy.account_id.in_(account_ids))
                    )
                    policies_by_account = {p.account_id: p for p in pol_result.scalars().all()}

                ids_to_delete = []
                for snap in expired_snaps:
                    acc_policy = policies_by_account.get(snap.account_id)
                    eff_days = acc_policy.bot_retention_days if acc_policy else global_bot_days

                    if eff_days == -1:
                        continue  # this account keeps data forever

                    snap_cutoff = now - timedelta(days=eff_days)
                    if snap.created_at > snap_cutoff:
                        continue  # not yet expired under account policy

                    # Try to delete recording files
                    try:
                        data = _json.loads(snap.data or "{}")
                        for path_key in ("recording_path", "video_path"):
                            fpath = data.get(path_key)
                            if fpath and os.path.exists(fpath):
                                os.remove(fpath)
                                logger.debug("Retention: deleted %s", fpath)
                    except Exception as exc:
                        logger.warning("Retention: could not clean up files for snapshot %s: %s", snap.id, exc)

                    ids_to_delete.append(snap.id)

                # Batch delete all expired snapshots in one query
                if ids_to_delete:
                    await db.execute(_sqldelete(BotSnapshot).where(BotSnapshot.id.in_(ids_to_delete)))

                await db.commit()
                logger.info("Retention enforcement complete: deleted %d snapshots", len(ids_to_delete))

    retention_task = asyncio.create_task(_supervised("retention_loop", _retention_loop))

    # Monthly usage counter reset (hourly check)
    async def _monthly_reset_loop():
        from datetime import datetime, timezone, timedelta
        from app.db import AsyncSessionLocal
        from app.models.account import Account
        from sqlalchemy import select

        while True:
            await asyncio.sleep(3600)  # check every hour
            try:
                now = datetime.now(timezone.utc)
                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(Account).where(
                            Account.monthly_reset_at.isnot(None),
                            Account.monthly_reset_at <= now,
                        )
                    )
                    accounts = result.scalars().all()
                    for acct in accounts:
                        acct.monthly_bots_used = 0
                        acct.monthly_reset_at = now + timedelta(days=30)
                    if accounts:
                        await db.commit()
                        logger.info("Monthly reset: zeroed usage for %d account(s)", len(accounts))
            except Exception as exc:
                logger.error("Monthly reset error: %s", exc)

    monthly_reset_task = asyncio.create_task(_supervised("monthly_reset", _monthly_reset_loop))

    # Weekly digest — fires every Monday at 08:00 UTC
    async def _weekly_digest_loop():
        from datetime import datetime, timezone, timedelta
        from app.services.email_service import send_weekly_digest
        while True:
            now = datetime.now(timezone.utc)
            days_until_monday = (7 - now.weekday()) % 7
            if days_until_monday == 0 and now.hour >= 8:
                days_until_monday = 7  # already past today's 08:00 window
            next_run = now.replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=days_until_monday)
            sleep_s = (next_run - now).total_seconds()
            logger.info("Weekly digest: next run in %.0f s (%s UTC)", sleep_s, next_run.strftime("%Y-%m-%d %H:%M"))
            await asyncio.sleep(sleep_s)
            try:
                await send_weekly_digest()
            except Exception as exc:
                logger.error("Weekly digest error: %s", exc)

    digest_task = asyncio.create_task(_supervised("weekly_digest", _weekly_digest_loop))

    logger.info("JustHereToListen.io ready — API docs at /api/docs")
    yield

    # Shutdown
    queue_task.cancel()
    cleanup_task.cancel()
    webhook_retry_task.cancel()
    calendar_task.cancel()
    retention_task.cancel()
    digest_task.cancel()

    if _running_tasks:
        logger.info("Cancelling %d running bot task(s)…", len(_running_tasks))
        for task in list(_running_tasks.values()):
            task.cancel()
        await asyncio.gather(*list(_running_tasks.values()), return_exceptions=True)

    from app.services import webhook_service
    await webhook_service.close_http_client()
    logger.info("JustHereToListen.io shut down")


# ── App ───────────────────────────────────────────────────────────────────────

# Public-facing description — excludes Admin, Analytics sections and ai_usage cost details.
# The full description (including those sections) is stored on app.description for admin docs.
_PUBLIC_DESCRIPTION = (
    "A **multi-tenant meeting bot API** service. Send bots into **Zoom**, **Google Meet**, "
    "**Microsoft Teams**, and **onepizza.io** meetings to record, transcribe, and analyse them with "
    "**Claude** (Anthropic) or **Gemini** (Google) AI.\n\n"

    "## How it works\n"
    "1. Register an account (email/password or Google/Microsoft SSO) → receive an `sk_live_...` API key\n"
    "2. Top up credits via **Stripe card** or **USDC (ERC-20)**\n"
    "3. `POST /api/v1/bot` with your `meeting_url` and optional `webhook_url`\n"
    "4. A headless Chromium bot joins the meeting, records audio (and optionally video), and transcribes it\n"
    "5. Results are POSTed to your `webhook_url` when done (or poll `GET /api/v1/bot/{id}`)\n"
    "6. Results persist in the database — browse past meetings via the **Meeting History** tab in the dashboard\n\n"

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
    "Business accounts are for **platforms integrating JustHereToListen.io on behalf of multiple "
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
    "Connect an iCal feed so JustHereToListen.io automatically dispatches bots to upcoming meetings.\n\n"
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

    "## Consent & recording announcement\n"
    "Set `consent_enabled: true` on bot creation to have the bot announce recording at join. "
    "Transcripts are scanned for the opt-out phrase (default: `'opt out'`) — opted-out "
    "participants' content is automatically redacted. Configure globally via "
    "`CONSENT_ANNOUNCEMENT_ENABLED` and `CONSENT_OPT_OUT_PHRASE` env vars.\n\n"

    "## Data retention policies\n"
    "`GET/PUT /api/v1/retention` — configure per-account retention days for bots, recordings, "
    "and transcripts. Use `-1` for keep-forever. Platform defaults: "
    "`DEFAULT_BOT_RETENTION_DAYS` (90 days), `DEFAULT_RECORDING_RETENTION_DAYS` (30 days). "
    "A background task enforces policies nightly.\n\n"

    "## Keyword alerts\n"
    "`POST /api/v1/keyword-alerts` — register a keyword. When it is detected in any completed "
    "transcript, a `bot.keyword_alert` webhook event fires. Keywords can also be set per-bot "
    "at creation via `keyword_alerts: [{keyword, webhook_url?}]`.\n\n"

    "## Workspaces\n"
    "`POST /api/v1/workspaces` — create a shared workspace. Invite members with roles "
    "(`admin` / `member` / `viewer`). Tag bots with `workspace_id` at creation to make "
    "them visible to all workspace members. `WORKSPACES_ENABLED=true` (default).\n\n"

    "## MCP (Model Context Protocol)\n"
    "`GET /api/v1/mcp/schema` returns the server manifest. "
    "`POST /api/v1/mcp/call` executes tools: `list_meetings`, `get_meeting`, "
    "`search_meetings`, `get_action_items`, `get_meeting_brief`. "
    "Enable/disable with `MCP_ENABLED` (default `true`).\n\n"

    "## SAML 2.0 SSO\n"
    "Set `SAML_ENABLED=true` and `SAML_SP_BASE_URL` to enable enterprise SSO. "
    "Admins register IdP configs at `POST /api/v1/auth/saml/configs`. "
    "Users authenticate at `GET /api/v1/auth/saml/{org_slug}/authorize`.\n\n"

    "## Exports\n"
    "Export meeting reports in multiple formats:\n"
    "- `GET /api/v1/bot/{id}/export/markdown` — Markdown report with transcript, summary, and action items\n"
    "- `GET /api/v1/bot/{id}/export/pdf` — PDF report (same content)\n"
    "- `GET /api/v1/bot/{id}/export/json` — Full session as structured JSON\n"
    "- `GET /api/v1/bot/{id}/export/srt` — Transcript as SRT subtitle file\n\n"

    "## Analysis templates\n"
    "Use predefined templates to customize analysis output:\n"
    "- `GET /api/v1/templates` — list all templates (default, sales, standup, 1:1, retro, etc.)\n"
    "- Pass `template: \"<seed>\"` when creating a bot, or `prompt_override` for a one-off prompt\n"
    "- `GET /api/v1/templates/default-prompt` — get the raw default analysis prompt\n\n"

    "## Prometheus metrics\n"
    "`GET /metrics` — Prometheus-compatible metrics endpoint (unauthenticated). "
    "Includes HTTP request counts/latencies, active bot counts, and billing totals.\n\n"

    "## Bot response & analysis\n"
    "Bot responses include: `id`, `status`, `meeting_platform`, `participants`, `transcript` "
    "(`[{speaker, text, timestamp}]`), `analysis` (`{summary, key_points, action_items, "
    "decisions, next_steps, sentiment, topics}`), `chapters` (`[{title, start_time, summary}]`), "
    "`speaker_stats` (`[{name, talk_time_s, talk_pct, turns}]`), `recording_available`, "
    "`is_demo_transcript`, `sub_user_id`, and `metadata`.\n\n"

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
    "| `/bot/{id}` | Session viewer — transcript, AI analysis (summary, key points, action items, "
    "decisions, next steps, sentiment, topics), speaker breakdown, chapters, meeting metadata, "
    "and download links for audio/video/markdown/PDF |\n"
)

app = FastAPI(
    title="JustHereToListen.io API",
    description=(
        "A **multi-tenant meeting bot API** service. Send bots into **Zoom**, **Google Meet**, "
        "**Microsoft Teams**, and **onepizza.io** meetings to record, transcribe, and analyse them with "
        "**Claude** (Anthropic) or **Gemini** (Google) AI.\n\n"

        "## How it works\n"
        "1. Register an account (email/password or Google/Microsoft SSO) → receive an `sk_live_...` API key\n"
        "2. Top up credits via **Stripe card** or **USDC (ERC-20)**\n"
        "3. `POST /api/v1/bot` with your `meeting_url` and optional `webhook_url`\n"
        "4. A headless Chromium bot joins the meeting, records audio (and optionally video), and transcribes it\n"
        "5. Results are POSTed to your `webhook_url` when done (or poll `GET /api/v1/bot/{id}`)\n"
        "6. Results persist in the database — browse past meetings via the **Meeting History** tab in the dashboard\n\n"

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
        "Business accounts are for **platforms integrating JustHereToListen.io on behalf of multiple "
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
        "**Subscribe to a plan:** `POST /api/v1/billing/subscribe` — create a Stripe Checkout "
        "session for a subscription plan (`starter`, `pro`, or `business`). Returns "
        "`{session_id, checkout_url, plan}`. Pass optional `success_url` and `cancel_url`.\n\n"
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
        "Connect an iCal feed so JustHereToListen.io automatically dispatches bots to upcoming meetings.\n\n"
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

        "## Consent & recording announcement\n"
        "Set `consent_enabled: true` on bot creation to announce recording at join. "
        "Transcripts are scanned for the opt-out phrase — opted-out participants' content is "
        "redacted. Configure globally via `CONSENT_ANNOUNCEMENT_ENABLED` and "
        "`CONSENT_OPT_OUT_PHRASE` env vars.\n\n"

        "## Data retention policies\n"
        "`GET/PUT /api/v1/retention` — configure per-account retention days for bots, recordings, "
        "and transcripts. Use `-1` for keep-forever. Platform defaults: "
        "`DEFAULT_BOT_RETENTION_DAYS` (90 days), `DEFAULT_RECORDING_RETENTION_DAYS` (30 days). "
        "A background task enforces policies nightly.\n\n"

        "## Keyword alerts\n"
        "`POST /api/v1/keyword-alerts` — register a keyword. When detected in a completed transcript "
        "a `bot.keyword_alert` webhook event fires. Set per-bot via "
        "`keyword_alerts: [{keyword, webhook_url?}]` at creation.\n\n"

        "## Workspaces\n"
        "`POST /api/v1/workspaces` — create a shared workspace. Invite members with roles "
        "(`admin` / `member` / `viewer`). Tag bots with `workspace_id` to share with workspace members. "
        "Enable/disable with `WORKSPACES_ENABLED` (default `true`).\n\n"

        "## MCP (Model Context Protocol)\n"
        "`GET /api/v1/mcp/schema` returns the server manifest. "
        "`POST /api/v1/mcp/call` executes tools: `list_meetings`, `get_meeting`, "
        "`search_meetings`, `get_action_items`, `get_meeting_brief`. "
        "Enable/disable with `MCP_ENABLED` (default `true`).\n\n"

        "## SAML 2.0 SSO\n"
        "Set `SAML_ENABLED=true` and `SAML_SP_BASE_URL`. Admins register IdP configs at "
        "`POST /api/v1/auth/saml/configs`. Users authenticate at "
        "`GET /api/v1/auth/saml/{org_slug}/authorize`.\n\n"

        "## Exports\n"
        "Export meeting reports in multiple formats:\n"
        "- `GET /api/v1/bot/{id}/export/markdown` — Markdown report with transcript, summary, and action items\n"
        "- `GET /api/v1/bot/{id}/export/pdf` — PDF report (same content)\n"
        "- `GET /api/v1/bot/{id}/export/json` — Full session as structured JSON\n"
        "- `GET /api/v1/bot/{id}/export/srt` — Transcript as SRT subtitle file\n\n"

        "## Analytics\n"
        "`GET /api/v1/analytics` — platform-wide bot statistics including sentiment distribution, "
        "meetings per day, top topics, top participants, and platform breakdown.\n\n"
        "`GET /api/v1/analytics/usage` — per-account usage dashboard: `bots_used`, `bots_limit`, "
        "`plan`, `credits_balance`, `credits_spent_this_month`, `avg_cost_per_bot`, "
        "`billing_cycle_reset`, and `daily_usage[]` (last 30 days).\n\n"
        "`GET /api/v1/analytics/trends?days=30` — longitudinal meeting trends: "
        "`total_meetings`, `total_hours`, `meetings_per_day[]`, `sentiment_trend[]`, "
        "`health_trend[]`, `top_topics[]`, `cost_trend[]`.\n\n"

        "## Analysis templates\n"
        "Use predefined templates to customize analysis output:\n"
        "- `GET /api/v1/templates` — list all templates (default, sales, standup, 1:1, retro, etc.)\n"
        "- Pass `template: \"<seed>\"` when creating a bot, or `prompt_override` for a one-off prompt\n"
        "- `GET /api/v1/templates/default-prompt` — get the raw default analysis prompt\n\n"

        "## Admin\n"
        "Admin endpoints are restricted to accounts in `ADMIN_EMAILS` (env var) or with "
        "`is_admin=true` in the database. All others receive HTTP 403. "
        "Full interactive docs at `GET /api/v1/admin/docs`.\n\n"
        "**USDC & billing:**\n"
        "- `GET/PUT /api/v1/admin/wallet` — view or set the platform USDC collection wallet\n"
        "- `GET/PUT /api/v1/admin/rpc-url` — view or set the Ethereum RPC URL (no restart needed)\n"
        "- `GET /api/v1/admin/config` — list all platform configuration values\n"
        "- `POST /api/v1/admin/credit` — manually credit a user account\n"
        "- `GET /api/v1/admin/usdc/unmatched` — list USDC transfers that couldn't be attributed\n"
        "- `POST /api/v1/admin/usdc/unmatched/{tx_hash}/resolve` — mark unmatched transfer resolved\n"
        "- `POST /api/v1/admin/usdc/rescan` — reset USDC monitor block pointer for rescan\n\n"
        "**Account management:**\n"
        "- `POST /api/v1/admin/accounts/{account_id}/set-account-type` — change any account's type\n"
        "- `POST /api/v1/auth/saml/configs` — register SAML IdP config (admin only)\n\n"
        "**Admin web UI** at `/admin` provides:\n"
        "- Platform stats (accounts, credits, revenue, unmatched transfers)\n"
        "- Subscription plan breakdown (Free/Starter/Pro/Business counts)\n"
        "- Bot activity stats + platform feature counters (webhooks, integrations, calendar feeds, SSO)\n"
        "- System status indicators (Stripe, RPC, HD seed, Email, Storage, Video, Google SSO, Microsoft SSO, SAML, Whisper)\n"
        "- Inline plan management — change any account's plan via a dropdown\n"
        "- Monthly bot usage per account\n"
        "- Manual credit form, USDC rescan, wallet & RPC configuration\n"
        "- Unmatched USDC transfer resolution\n"
        "- User account enable/disable/admin-toggle/account-type-switch\n\n"

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
        "| `/bot/{id}` | Session viewer — transcript, AI analysis (summary, key points, action items, "
        "decisions, next steps, sentiment, topics), speaker breakdown, chapters, meeting metadata, "
        "and download links for audio/video/markdown/PDF |\n"
        "| `/admin` | Platform admin — plan stats, bot activity, system status, "
        "user accounts with inline plan management, unmatched USDC transfers, manual credit |\n"
    ),
    version=_APP_VERSION,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

app.state.limiter = _limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

_origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
_wildcard = _origins == ["*"]
if _wildcard and settings.API_KEY:
    # Production (API_KEY set): restrict CORS to same-origin only.
    # Override with an explicit CORS_ORIGINS env var to allow specific origins.
    logger.warning(
        "CORS_ORIGINS is '*' but API_KEY is set — restricting CORS to same-origin. "
        "Set CORS_ORIGINS explicitly to allow cross-origin requests."
    )
    _origins = []
    _wildcard = False
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins if not _wildcard else ["*"],
    allow_credentials=not _wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(PrometheusMiddleware)


# ── Security headers ───────────────────────────────────────────────────────────

@app.middleware("http")
async def add_security_headers(request, call_next):
    """Add defensive HTTP security headers to every response.

    These headers protect against XSS, clickjacking, MIME-sniffing, and
    information leakage without requiring application-level changes.
    """
    response = await call_next(request)
    # Prevent browsers from MIME-sniffing the content-type
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Deny embedding in iframes (clickjacking protection)
    response.headers["X-Frame-Options"] = "DENY"
    # Limit Referer header to same-origin
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Disable potentially dangerous browser features
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    # Content-Security-Policy: allow scripts/styles only from self + CDNs used by the UI
    # API responses (application/json) are unaffected — browsers don't execute them.
    if "text/html" in response.headers.get("content-type", ""):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' cdn.jsdelivr.net fonts.googleapis.com; "
            "font-src 'self' fonts.gstatic.com; "
            "img-src 'self' data: https:; "
            "connect-src 'self' wss: ws:; "
            "frame-ancestors 'none';"
        )
    # HSTS: tell browsers to always use HTTPS for this domain (1 year)
    # Only set if we're serving over HTTPS (detected via X-Forwarded-Proto or direct TLS)
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    if forwarded_proto == "https" or request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# ── Rate limit response headers ────────────────────────────────────────────────

@app.middleware("http")
async def add_rate_limit_headers(request, call_next):
    """Attach X-RateLimit-Remaining and X-RateLimit-Reset to every response."""
    response = await call_next(request)
    remaining = getattr(request.state, "rate_limit_remaining", None)
    reset = getattr(request.state, "rate_limit_reset", None)
    if remaining is not None:
        response.headers["X-RateLimit-Remaining"] = str(remaining)
    if reset is not None:
        response.headers["X-RateLimit-Reset"] = str(reset)
    return response


# ── Public OpenAPI override ────────────────────────────────────────────────────
# The default /api/docs and /api/openapi.json show only public endpoints.
# Admin endpoints, platform analytics, and ai_usage cost fields are excluded.
# Full schema (all routes + ai_usage) is available at /api/v1/admin/openapi.json
# (requires admin auth).

_public_openapi_cache: dict[str, Any] = {}


def _make_public_openapi() -> dict[str, Any]:
    """Return a filtered OpenAPI schema for the public docs."""
    if _public_openapi_cache:
        return _public_openapi_cache

    from fastapi.openapi.utils import get_openapi as _get_openapi_util

    _HIDDEN_TAGS = {"Admin", "Analytics"}
    public_routes = [
        r for r in app.routes
        if not (hasattr(r, "tags") and _HIDDEN_TAGS.intersection(getattr(r, "tags") or []))
    ]

    schema = _get_openapi_util(
        title=app.title,
        version=app.version,
        description=_PUBLIC_DESCRIPTION,
        routes=public_routes,
    )

    # Remove ai_usage from BotResponse component schema
    comp_schemas = schema.get("components", {}).get("schemas", {})
    if "BotResponse" in comp_schemas:
        comp_schemas["BotResponse"].get("properties", {}).pop("ai_usage", None)
    for _name in ("AIUsageSummary", "AIUsageEntry"):
        comp_schemas.pop(_name, None)

    _public_openapi_cache.update(schema)
    return _public_openapi_cache


app.openapi = _make_public_openapi


# ── Admin API docs (unauthenticated — actual endpoints still require admin auth) ─
# These two routes are intentionally public so the browser Swagger UI can load
# the OpenAPI schema without needing the Authorization header.  Security is
# enforced on every individual admin data endpoint via require_admin.

@app.get("/api/v1/admin/docs", include_in_schema=False, response_class=HTMLResponse)
async def admin_api_docs():
    """Swagger UI for the full admin API (includes all endpoints and ai_usage data)."""
    return _get_swagger_ui_html(
        openapi_url="/api/v1/admin/openapi.json",
        title="JustHereToListen.io Admin API",
        swagger_favicon_url="",
    )


@app.get("/api/v1/admin/openapi.json", include_in_schema=False)
async def admin_openapi_schema():
    """Full OpenAPI schema — includes admin-only endpoints, platform analytics, and ai_usage fields."""
    return _get_openapi_util(
        title="JustHereToListen.io Admin API",
        version=app.version,
        description=app.description,
        routes=app.routes,
    )


# ── Machine-readable error responses ─────────────────────────────────────────
# Every error now includes {detail, error_code, retryable} for integrators.

from fastapi.exceptions import HTTPException as _HTTPException

_ERROR_CODE_MAP: dict[int, tuple[str, bool]] = {
    400: ("bad_request", False),
    401: ("unauthorized", False),
    403: ("forbidden", False),
    404: ("not_found", False),
    409: ("conflict", False),
    422: ("validation_error", False),
    425: ("too_early", True),
    429: ("rate_limited", True),
    500: ("internal_error", True),
    502: ("bad_gateway", True),
    503: ("service_unavailable", True),
}


from fastapi.exceptions import RequestValidationError as _RequestValidationError


@app.exception_handler(_RequestValidationError)
async def _validation_exception_handler(request, exc: _RequestValidationError):
    """Wrap Pydantic validation errors into the machine-readable structure."""
    return _JSONResponse(
        status_code=422,
        content={
            "detail": exc.errors(),
            "error_code": "validation_error",
            "retryable": False,
        },
    )


@app.exception_handler(_HTTPException)
async def _http_exception_handler(request, exc: _HTTPException):
    """Wrap FastAPI HTTPExceptions into a machine-readable structure."""
    code, retryable = _ERROR_CODE_MAP.get(exc.status_code, ("unknown_error", exc.status_code >= 500))
    return _JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "error_code": code,
            "retryable": retryable,
        },
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request, exc):
    """Log full traceback for unhandled exceptions so production errors are diagnosable."""
    import traceback
    logger.error(
        "Unhandled %s on %s %s:\n%s",
        type(exc).__name__,
        request.method,
        request.url.path,
        traceback.format_exc(),
    )
    return _JSONResponse(
        status_code=500,
        content={
            "detail": "Internal Server Error",
            "error_code": "internal_error",
            "retryable": True,
        },
    )


_auth = [Depends(require_auth)]


# ── Developer tool web routes ──────────────────────────────────────────────────

_TEMPLATES_DIR = Path(__file__).parent / "templates"


@app.get("/webhook-playground", include_in_schema=False, response_class=HTMLResponse)
async def webhook_playground():
    """Webhook testing playground — view delivery history and send test events."""
    tmpl = _TEMPLATES_DIR / "webhook_playground.html"
    if tmpl.exists():
        return HTMLResponse(tmpl.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Webhook Playground</h1><p>Template not found.</p>", status_code=500)


@app.get("/api-dashboard", include_in_schema=False, response_class=HTMLResponse)
async def api_dashboard():
    """API usage dashboard — view bot counts, token usage, and cost breakdowns."""
    tmpl = _TEMPLATES_DIR / "api_dashboard.html"
    if tmpl.exists():
        return HTMLResponse(tmpl.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>API Dashboard</h1><p>Template not found.</p>", status_code=500)


app.include_router(metrics_router)                                       # unauthenticated /metrics
app.include_router(auth_router,           prefix="/api/v1")              # no auth on register/login
app.include_router(oauth_router,          prefix="/api/v1")              # SSO OAuth (no prefix auth)
app.include_router(saml_router,           prefix="/api/v1")              # SAML SSO (own auth logic)
app.include_router(billing_router,        prefix="/api/v1")              # billing has its own auth handling
app.include_router(bots_router,           prefix="/api/v1", dependencies=_auth)
app.include_router(webhooks_router,       prefix="/api/v1", dependencies=_auth)
app.include_router(exports_router,        prefix="/api/v1", dependencies=_auth)
app.include_router(templates_router,      prefix="/api/v1", dependencies=_auth)
app.include_router(analytics_router,      prefix="/api/v1", dependencies=_auth)
app.include_router(integrations_router,   prefix="/api/v1", dependencies=_auth)
app.include_router(calendar_router,       prefix="/api/v1", dependencies=_auth)
app.include_router(retention_router,      prefix="/api/v1", dependencies=_auth)
app.include_router(keyword_alerts_router, prefix="/api/v1", dependencies=_auth)
app.include_router(workspaces_router,     prefix="/api/v1", dependencies=_auth)
app.include_router(mcp_router,            prefix="/api/v1", dependencies=_auth)
app.include_router(action_items_router,   prefix="/api/v1", dependencies=_auth)
app.include_router(admin_router,          prefix="/api/v1")              # admin has its own auth (require_admin)
app.include_router(ws_router,             prefix="/api/v1")              # WS auth handled separately
app.include_router(ui_router)                                             # web UI (no prefix)


# ── Health & readiness probes ──────────────────────────────────────────────────

@app.get("/health", tags=["Health"])
@app.get("/api/health", tags=["Health"])
async def health():
    """Liveness probe — returns 200 when the process is running.

    Does NOT check external dependencies (use /ready for that).
    Kubernetes: use this as the `livenessProbe`.
    """
    return {"status": "ok", "service": "JustHereToListen.io", "version": _APP_VERSION}


@app.get("/ready", tags=["Health"])
@app.get("/api/ready", tags=["Health"])
async def ready():
    """Readiness probe — returns 200 only when all critical dependencies are healthy.

    Checks:
    - Database connectivity (SELECT 1)
    - At least one AI provider key is configured

    Kubernetes: use this as the `readinessProbe` to stop routing traffic to
    instances that can't serve requests.
    """
    checks: dict = {}
    ok = True

    # ── Database ──────────────────────────────────────────────────────────────
    try:
        from app.db import AsyncSessionLocal
        from sqlalchemy import text as _text
        async with AsyncSessionLocal() as _db:
            await _db.execute(_text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"
        ok = False

    # ── AI provider ───────────────────────────────────────────────────────────
    if settings.ANTHROPIC_API_KEY or settings.GEMINI_API_KEY:
        checks["ai_provider"] = "ok"
    else:
        checks["ai_provider"] = "no key configured (demo mode only)"
        # Not fatal — service can still return demo transcripts

    from fastapi.responses import JSONResponse as _JR
    return _JR(
        content={"status": "ok" if ok else "degraded", "checks": checks},
        status_code=200 if ok else 503,
    )


# ── Serve frontend ────────────────────────────────────────────────────────────

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_frontend(full_path: str = ""):
        # Skip paths already handled by ui_router (/, /login, /register, etc.)
        # and API paths
        if full_path.startswith("api/"):
            return _JSONResponse(
                status_code=404,
                content={"detail": f"API endpoint not found: /{full_path}"},
            )
        index = FRONTEND_DIR / "index.html"
        if index.exists():
            return FileResponse(str(index))
        return {"error": "Frontend not found"}
