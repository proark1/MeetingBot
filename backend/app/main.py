"""JustHereToListen.io API — stateless meeting bot service.

Run with:
    uvicorn app.main:app --reload
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
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
_APP_VERSION = _VERSION_FILE.read_text().strip() if _VERSION_FILE else "2.45.0"

from fastapi import Depends, FastAPI, Request
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
    level=getattr(logging, str(settings.LOG_LEVEL).upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

# Task heartbeat registry — tracks last activity per background task
_task_heartbeats: dict[str, datetime] = {}

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
        # Production-ish deployments are detected by either ENVIRONMENT or by
        # using a non-sqlite database (Railway/Heroku/Fly all inject a real DB
        # URL). Either signal forces an explicit secret — never auto-generate
        # in something that looks like prod.
        _looks_like_prod = (
            settings.ENVIRONMENT.lower() in {"production", "prod", "staging"}
            or not settings.DATABASE_URL.startswith("sqlite")
        )
        if _looks_like_prod:
            raise SystemExit(
                "FATAL: JWT_SECRET is the insecure default in a non-development "
                "deployment. Set a strong JWT_SECRET before starting:\n"
                "  export JWT_SECRET=$(openssl rand -hex 32)"
            )
        # Round-2 fix #10: persist the auto-generated secret next to the SQLite
        # DB so non-prod deployments stop logging every user out on each restart.
        import secrets as _secrets
        from pathlib import Path as _Path
        secret_path = _Path("./jwt_secret.local").resolve()
        try:
            if secret_path.is_file():
                file_secret = secret_path.read_text().strip()
                if len(file_secret) >= 32:
                    settings.JWT_SECRET = file_secret
                    logger.info("JWT_SECRET loaded from %s", secret_path)
                else:
                    raise ValueError("persisted secret too short")
            else:
                new_secret = _secrets.token_hex(32)
                # Best-effort: write 0600 so only the running user can read it.
                secret_path.write_text(new_secret)
                try:
                    secret_path.chmod(0o600)
                except Exception:
                    pass
                settings.JWT_SECRET = new_secret
                logger.warning(
                    "⚠ JWT_SECRET was the insecure default — generated a new one and "
                    "persisted it at %s. Set JWT_SECRET in your environment to use a "
                    "managed secret instead.", secret_path,
                )
        except Exception as exc:
            settings.JWT_SECRET = _secrets.token_hex(32)
            logger.warning(
                "⚠ JWT_SECRET defaulted and could not be persisted (%s) — using a "
                "random per-process secret; sessions will reset on restart.", exc,
            )

    # ── Hard production guardrails (security audit H1 / C-2 / W-1) ─────────────
    _prod_like = settings.ENVIRONMENT.lower() in {"production", "prod", "staging"}
    if _prod_like:
        # A weak/short secret silently rotates per-restart (logging everyone out)
        # and is brute-forceable. Fail fast regardless of DB type — the default
        # JWT guard above only fires for the exact sentinel on a non-sqlite DB.
        if settings.JWT_SECRET == "change-me-in-production" or len(settings.JWT_SECRET) < 32:
            raise SystemExit(
                "FATAL: JWT_SECRET must be a strong value (>=32 chars) in "
                "production. Generate one with:  export JWT_SECRET=$(openssl rand -hex 32)"
            )
        if not settings.ENCRYPTION_KEY:
            logger.warning(
                "⚠ ENCRYPTION_KEY is not set — at-rest encryption of SSO/integration "
                "tokens and meeting snapshots falls back to JWT_SECRET. Set a separate "
                "ENCRYPTION_KEY (openssl rand -hex 32) for key separation."
            )
        # The in-memory bot Store is single-process; >1 worker/replica corrupts
        # live bot state and breaks the global MAX_CONCURRENT_BOTS cap. Refuse to
        # boot a multi-worker process while on the memory backend.
        if settings.BOT_STATE_BACKEND == "memory":
            import os as _os
            _workers = 0
            for _var in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS"):
                try:
                    _workers = max(_workers, int(_os.environ.get(_var, "0") or 0))
                except ValueError:
                    pass
            if _workers > 1:
                raise SystemExit(
                    f"FATAL: {_workers} workers requested but BOT_STATE_BACKEND=memory. "
                    "The in-memory bot store is single-process only — run exactly one "
                    "worker and one replica, or migrate to BOT_STATE_BACKEND=redis."
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
        # Round-2 fix #9: refuse unauthenticated requests once real accounts
        # exist. Without this guard, a missing-Bearer request would resolve to
        # account_id=None and silently bypass the per-tenant ownership checks.
        if not settings.ALLOW_UNAUTHENTICATED_DEV_MODE:
            try:
                from app.db import AsyncSessionLocal
                from app.models.account import Account
                from sqlalchemy import select as _select, func as _func
                async with AsyncSessionLocal() as _db:
                    n = await _db.execute(_select(_func.count(Account.id)))
                    account_count = int(n.scalar() or 0)
            except Exception as exc:
                logger.warning("Could not count accounts at startup: %s", exc)
                account_count = 0
            if account_count > 0:
                from app import deps as _deps
                _deps.require_bearer_in_dev_mode = True
                logger.warning(
                    "🔒 Found %d account(s) — dev-mode auth fail-closed: "
                    "requests without a Bearer token will now return 401. "
                    "Set ALLOW_UNAUTHENTICATED_DEV_MODE=true to keep the legacy behaviour.",
                    account_count,
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

    # NB: orphaned-subprocess cleanup + DB-pool disposal now happen in the
    # lifespan shutdown block below (after `yield`). A previous version installed
    # a custom SIGTERM handler here that *replaced* uvicorn's own — so uvicorn's
    # graceful shutdown, and this entire teardown block, never ran on a redeploy
    # (tasks left dangling, webhook HTTP client + DB pool never closed, bots
    # killed mid-meeting without persisting a terminal snapshot). Letting uvicorn
    # own SIGTERM means the shutdown block runs reliably on every stop.

    async def _supervised(name: str, coro_fn, *args, max_restarts: int = 20, **kwargs) -> None:
        """Run a background coroutine and restart it if it crashes.

        Without this, a single unhandled exception in a background task silently
        kills the task — webhooks stop retrying, cleanup stops running, etc.
        This wrapper logs the crash and restarts with exponential backoff.
        Records a heartbeat on each loop iteration for health monitoring.
        """
        from datetime import datetime as _dt, timezone as _tz
        restarts = 0
        backoff = 5.0
        while restarts < max_restarts:
            try:
                _task_heartbeats[name] = _dt.now(_tz.utc)
                await coro_fn(*args, **kwargs)
                # Coroutine returned normally (shouldn't happen for loops) — restart cleanly
                _task_heartbeats[name] = _dt.now(_tz.utc)
                restarts = 0
                backoff = 5.0
            except asyncio.CancelledError:
                logger.info("Background task %r cancelled", name)
                _task_heartbeats.pop(name, None)
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
        _task_heartbeats.pop(name, None)

    # Start bot queue processor
    queue_task = asyncio.create_task(_supervised("queue_processor", _queue_processor))
    logger.info("Bot queue processor started")

    # Start periodic cleanup of expired bots
    async def _cleanup_loop():
        from app.store import store
        from app.services.bot_service import reap_stuck_bots
        while True:
            await asyncio.sleep(settings.STORE_CLEANUP_INTERVAL_SECONDS)
            await store.cleanup_expired()
            # Safety net: force-terminate any bot stuck running past its hard
            # wall-clock ceiling so it can't hold a concurrency slot forever.
            try:
                await reap_stuck_bots()
            except Exception:
                logger.exception("stuck-bot reaper failed")

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
        # Small initial delay so retention doesn't compete with startup work,
        # then run immediately (previously it waited a full 24h before the first
        # sweep, so a freshly-deployed instance purged nothing for a day).
        await asyncio.sleep(300)
        while True:
            try:
                await _enforce_retention_policies()
            except Exception as exc:
                logger.error("Retention enforcement error: %s", exc)
            await asyncio.sleep(86400)  # every 24 hours

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
            from app.services import storage_service as _storage
            from app.services.secrets_at_rest import decrypt_text as _dec, encrypt_text as _enc
            _cloud_on = _storage.is_cloud_storage_enabled()

            async def _delete_artifacts(data: dict) -> bool:
                """Delete a snapshot's recording + video, whether stored as a
                local file or an S3 object. ``recording_path`` holds the S3 key
                when cloud storage is on (else a local path); ``video_path`` is
                always local. Returns True if anything was deleted."""
                deleted = False
                for path_key in ("recording_path", "video_path"):
                    val = data.get(path_key)
                    if not val:
                        continue
                    try:
                        if os.path.exists(val):
                            os.remove(val)
                            deleted = True
                        elif _cloud_on and await _storage.delete_recording(val):
                            deleted = True
                    except Exception as exc:
                        logger.warning("Retention: failed to delete %s (%s): %s", path_key, val, exc)
                return deleted

            # Load global policy (account_id IS NULL)
            g_result = await db.execute(
                select(RetentionPolicy).where(RetentionPolicy.account_id.is_(None))
            )
            global_policy = g_result.scalar_one_or_none()
            global_bot_days = settings.DEFAULT_BOT_RETENTION_DAYS if not global_policy else global_policy.bot_retention_days
            global_rec_days = settings.DEFAULT_RECORDING_RETENTION_DAYS if not global_policy else global_policy.recording_retention_days

            ids_to_delete: list = []
            # ── Snapshot retention: delete the whole record + its recordings ────
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

                for snap in expired_snaps:
                    acc_policy = policies_by_account.get(snap.account_id)
                    eff_days = acc_policy.bot_retention_days if acc_policy else global_bot_days

                    if eff_days == -1:
                        continue  # this account keeps data forever

                    if snap.created_at > now - timedelta(days=eff_days):
                        continue  # not yet expired under account policy

                    try:
                        data = _json.loads(_dec(snap.data) or "{}")
                        await _delete_artifacts(data)
                    except Exception as exc:
                        logger.warning("Retention: could not clean up files for snapshot %s: %s", snap.id, exc)

                    ids_to_delete.append(snap.id)

                # Batch delete all expired snapshots in one query
                if ids_to_delete:
                    await db.execute(_sqldelete(BotSnapshot).where(BotSnapshot.id.in_(ids_to_delete)))

            # ── Recording retention: shorter window, keep transcript/analysis ───
            # Deletes audio/video for snapshots past the (shorter) *recording*
            # window while preserving the transcript+analysis until the bot
            # window. Previously this window was computed but never enforced, so
            # recordings lived for the full bot-retention period.
            recs_cleaned = 0
            if global_rec_days != -1:
                _already = set(ids_to_delete)
                rec_cutoff = now - timedelta(days=global_rec_days)
                rec_result = await db.execute(
                    select(BotSnapshot).where(BotSnapshot.created_at < rec_cutoff)
                )
                rec_snaps = [s for s in rec_result.scalars().all() if s.id not in _already]

                rec_acct_ids = {s.account_id for s in rec_snaps if s.account_id}
                rec_policies: dict = {}
                if rec_acct_ids:
                    rp = await db.execute(
                        select(RetentionPolicy).where(RetentionPolicy.account_id.in_(rec_acct_ids))
                    )
                    rec_policies = {p.account_id: p for p in rp.scalars().all()}

                for snap in rec_snaps:
                    pol = rec_policies.get(snap.account_id)
                    eff = pol.recording_retention_days if pol else global_rec_days
                    if eff == -1:
                        continue
                    if snap.created_at > now - timedelta(days=eff):
                        continue
                    try:
                        sdata = _json.loads(_dec(snap.data) or "{}")
                    except Exception:
                        continue
                    if not (sdata.get("recording_path") or sdata.get("video_path")):
                        continue
                    if await _delete_artifacts(sdata):
                        sdata["recording_path"] = None
                        sdata["video_path"] = None
                        snap.data = _enc(_json.dumps(sdata))
                        recs_cleaned += 1

            # Clean up expired idempotency keys (independent of retention windows)
            from app.models.account import IdempotencyKey as _IKModel
            ik_result = await db.execute(
                _sqldelete(_IKModel).where(_IKModel.expires_at < now)
            )
            ik_deleted = ik_result.rowcount if ik_result.rowcount is not None else 0

            await db.commit()
            logger.info(
                "Retention enforcement complete: deleted %d snapshots, cleaned recordings "
                "for %d snapshots, %d idempotency keys",
                len(ids_to_delete), recs_cleaned, ik_deleted,
            )

    retention_task = asyncio.create_task(_supervised("retention_enforcement", _retention_loop))

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

    # Synthetic canary — periodically exercise the join→audio→transcript→leave
    # pipeline so browser_bot.py selector drift is caught before customers hit it.
    canary_task = None
    if settings.CANARY_ENABLED:
        from app.services.canary_service import canary_loop
        canary_task = asyncio.create_task(_supervised("canary", canary_loop))
        logger.info("Synthetic canary enabled (interval %ds)", settings.CANARY_INTERVAL_S)

    # Action-item due-date reminders — fire action_item.due_soon / .overdue
    # webhook events for open items with a parseable due_date.
    reminder_task = None
    if settings.ACTION_ITEM_REMINDERS_ENABLED:
        from app.services.action_item_reminder_service import reminder_loop
        reminder_task = asyncio.create_task(_supervised("action_item_reminders", reminder_loop))
        logger.info(
            "Action-item reminders enabled (interval %ds)",
            settings.ACTION_ITEM_REMINDER_INTERVAL_S,
        )

    logger.info("JustHereToListen.io ready — API docs at /api/docs")
    yield

    # Shutdown
    queue_task.cancel()
    cleanup_task.cancel()
    webhook_retry_task.cancel()
    calendar_task.cancel()
    retention_task.cancel()
    monthly_reset_task.cancel()
    digest_task.cancel()
    if canary_task is not None:
        canary_task.cancel()
    if reminder_task is not None:
        reminder_task.cancel()

    if _running_tasks:
        logger.info("Cancelling %d running bot task(s)…", len(_running_tasks))
        running_bot_tasks = [task for task in _running_tasks.values() if task is not None]
        for task in running_bot_tasks:
            task.cancel()
        await asyncio.gather(*running_bot_tasks, return_exceptions=True)

    from app.services.background_tasks import cancel_all_tracked_tasks
    await cancel_all_tracked_tasks()

    from app.services import webhook_service
    await webhook_service.close_http_client()

    # Final sweep: kill any browser/ffmpeg/Xvfb subprocess that outlived its bot
    # task so a redeploy doesn't leak processes (formerly done from the SIGTERM
    # handler that broke graceful shutdown).
    try:
        from app.services.browser_bot import kill_all_procs
        kill_all_procs()
    except Exception:
        logger.warning("kill_all_procs during shutdown failed", exc_info=True)

    # Dispose the SQLAlchemy connection pool so Postgres connections are released
    # promptly instead of being left for the server to time out.
    try:
        from app.db import engine
        await engine.dispose()
    except Exception:
        logger.warning("engine.dispose during shutdown failed", exc_info=True)

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
    "**Events (21):** lifecycle — `bot.joining`, `bot.in_call`, `bot.call_ended`, "
    "`bot.transcript_ready`, `bot.analysis_ready`, `bot.done`, `bot.error`, `bot.cancelled`; "
    "live — `bot.keyword_alert`, `bot.live_transcript`, `bot.live_transcript_translated`, "
    "`bot.live_chat_message`; advanced (opt-in) — `bot.decision_detected`, `bot.coaching_tip`, "
    "`bot.coaching_alert`, `bot.speaker_analytics`, `bot.agentic_action`, `bot.recurring_intel_ready`; "
    "action-item reminders — `action_item.due_soon`, `action_item.overdue`; and `bot.test`. "
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
    "`POST /api/v1/mcp/call` executes one of 16 tools (read, write, and reasoning) — "
    "e.g. `list_meetings`, `get_meeting`, `search_meetings`, `get_action_items`, "
    "`create_bot`, `cancel_bot`, `ask_chat_qa`, `get_meeting_brief`. "
    "Enable with `MCP_ENABLED=true` (default `false`).\n\n"

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

# Initialise error tracking before the app is created so the SDK can
# auto-instrument FastAPI/Starlette. No-op unless SENTRY_DSN is configured.
from app.observability import init_sentry as _init_sentry
_init_sentry()

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
        "**Events (21):** lifecycle — `bot.joining`, `bot.in_call`, `bot.call_ended`, "
        "`bot.transcript_ready`, `bot.analysis_ready`, `bot.done`, `bot.error`, `bot.cancelled`; "
        "live — `bot.keyword_alert`, `bot.live_transcript`, `bot.live_transcript_translated`, "
        "`bot.live_chat_message`; advanced (opt-in) — `bot.decision_detected`, `bot.coaching_tip`, "
        "`bot.coaching_alert`, `bot.speaker_analytics`, `bot.agentic_action`, `bot.recurring_intel_ready`; "
        "action-item reminders — `action_item.due_soon`, `action_item.overdue`; and `bot.test`. "
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
        "`POST /api/v1/mcp/call` executes one of 16 tools (read, write, and reasoning) — "
        "e.g. `list_meetings`, `get_meeting`, `search_meetings`, `get_action_items`, "
        "`create_bot`, `cancel_bot`, `ask_chat_qa`, `get_meeting_brief`. "
        "Enable with `MCP_ENABLED=true` (default `false`).\n\n"

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
    docs_url=None,       # served via custom request-aware route below
    redoc_url=None,      # served via custom request-aware route below
    openapi_url=None,    # served via custom request-aware route below
)

app.state.limiter = _limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

_origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
_wildcard = _origins == ["*"]
# Detect prod via ENVIRONMENT, a legacy API_KEY, OR a non-sqlite database
# (Railway/Heroku/Fly inject a real DB URL). The DB heuristic closes the gap
# where a JWT-only prod deploy forgets ENVIRONMENT=production and would
# otherwise run with allow_origins=["*"] + credentialed cookies.
_is_production = (
    settings.ENVIRONMENT.lower() == "production"
    or bool(settings.API_KEY)
    or not settings.DATABASE_URL.startswith("sqlite")
)
if _wildcard and _is_production:
    # Production: restrict CORS to same-origin only. The legacy gate was only
    # API_KEY, but most production deployments use JWT/per-user keys without
    # setting the legacy API_KEY env var, which left them with allow_origins=["*"]
    # and credentialed cookies. Trigger lockdown whenever ENVIRONMENT=production.
    # Override with an explicit CORS_ORIGINS env var to allow specific origins.
    logger.warning(
        "CORS_ORIGINS is '*' in production — restricting CORS to same-origin. "
        "Set CORS_ORIGINS explicitly to allow cross-origin requests."
    )
    _origins = []
    _wildcard = False
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins if not _wildcard else ["*"],
    allow_credentials=not _wildcard,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Idempotency-Key", "X-Sub-User"],
)
app.add_middleware(PrometheusMiddleware)


# ── UI CSRF / same-origin mutation guard ──────────────────────────────────────

_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _request_origin(request: Request) -> str:
    """Public origin for same-origin checks, proxy-aware but Host-bound."""
    scheme = (
        request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
        or request.url.scheme
        or "http"
    ).lower()
    host = (
        request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
        or request.headers.get("host", "")
        or request.url.netloc
    ).lower()
    return f"{scheme}://{host}"


def _origin_header_matches_request(request: Request) -> bool:
    """Return True when Origin/Referer proves a same-origin UI mutation."""
    from urllib.parse import urlparse

    expected = _request_origin(request)
    candidate = request.headers.get("origin") or request.headers.get("referer")
    if not candidate:
        return False
    try:
        parsed = urlparse(candidate)
    except Exception:
        return False
    if not parsed.scheme or not parsed.netloc:
        return False
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}" == expected


@app.middleware("http")
async def enforce_ui_same_origin_mutations(request: Request, call_next):
    """Reject cross-site browser form posts against cookie-authenticated UI routes.

    API routes use Bearer/API-key auth and are intentionally left to CORS/auth
    policy. The HTML dashboard/admin surface uses a session cookie, so unsafe
    non-API methods must prove same-origin via Origin or Referer before any
    route handler mutates account, billing, webhook, or bot state.
    """
    path = request.url.path
    if (
        request.method.upper() in _UNSAFE_METHODS
        and not path.startswith("/api/")
        and not _origin_header_matches_request(request)
    ):
        return _JSONResponse(
            {"detail": "Cross-site form submission rejected"},
            status_code=403,
        )
    return await call_next(request)


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
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
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

# Tag descriptions surfaced as a top-level `tags:` block. Generated SDKs and
# Swagger UI use this to group + document endpoint families.
_OPENAPI_TAGS: list[dict[str, str]] = [
    {"name": "Auth", "description": "Account registration, login, API key management, plan, notification prefs, GDPR erasure."},
    {"name": "Auth — SSO", "description": "Google / Microsoft OAuth2 sign-in and sign-up."},
    {"name": "Bots", "description": "Create, monitor, control, and inspect meeting bots. Primary surface for SDK consumers."},
    {"name": "Exports", "description": "Download a bot session as Markdown, PDF, JSON, or SRT."},
    {"name": "Webhooks", "description": "Subscribe to bot lifecycle events with HMAC-signed deliveries and exponential-backoff retries."},
    {"name": "Billing", "description": "Stripe Checkout, USDC top-ups, balance, transactions, plan subscriptions."},
    {"name": "Integrations", "description": "Push meeting summaries to Slack, Notion, and other 3rd-party tools."},
    {"name": "Calendar", "description": "iCal feeds — auto-dispatch bots to upcoming meetings."},
    {"name": "Action Items", "description": "List and update extracted action items across all bots."},
    {"name": "Templates", "description": "Built-in and custom analysis prompt templates."},
    {"name": "Keyword Alerts", "description": "Register keywords; bots fire `bot.keyword_alert` webhooks on match."},
    {"name": "Workspaces", "description": "Shared workspaces with roles for team-based bot access."},
    {"name": "Retention", "description": "Per-account retention policies for bots, transcripts, recordings."},
    {"name": "MCP", "description": "Model Context Protocol — expose bot data to AI agents."},
]


def _server_entries(*, include_admin: bool = False, request: "Request | None" = None) -> list[dict[str, Any]]:
    """Build the OpenAPI `servers` block.

    Resolution order (first match wins as `servers[0]`):
    1. `PUBLIC_BASE_URL` env var — explicit production override.
    2. The base URL of the current request (host + scheme from headers,
       respecting `X-Forwarded-Proto` / `X-Forwarded-Host` when behind a
       trusted proxy). This makes the schema self-correcting when an
       operator forgets to set `PUBLIC_BASE_URL` — clients see the URL they
       actually reached.
    3. `http://localhost:8000` for local dev / regen-script callers that
       have no request context.
    """
    servers: list[dict[str, Any]] = []
    seen: set[str] = set()
    label = "Production (admin auth required)" if include_admin else "Production"

    public = (settings.PUBLIC_BASE_URL or "").rstrip("/")
    if public:
        servers.append({"url": public, "description": label})
        seen.add(public)

    if request is not None:
        # Starlette resolves `base_url` from forwarded headers when we have
        # `X-Forwarded-Proto` / `X-Forwarded-Host` upstream. Strip the
        # trailing slash so it matches the PUBLIC_BASE_URL form.
        try:
            req_base = str(request.base_url).rstrip("/")
        except Exception:
            req_base = ""
        if req_base and req_base not in seen:
            servers.append({"url": req_base, "description": "Current host"})
            seen.add(req_base)

    if "http://localhost:8000" not in seen:
        servers.append({"url": "http://localhost:8000", "description": "Local development"})

    return servers


def _security_components() -> dict[str, Any]:
    """Reusable `components.securitySchemes` definitions.

    Three accepted credentials, in priority order:
    1. Bearer per-user API key (`sk_live_...` / `sk_test_...`)
    2. Bearer JWT (web UI sessions)
    3. Legacy superadmin `API_KEY` env var (HTTP Bearer; not exposed to integrators)

    Swagger UI shows a single Authorize button covering both Bearer flows.
    """
    return {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "sk_live_… or sk_test_… or JWT",
            "description": (
                "Per-user API key (`sk_live_…` for production, `sk_test_…` for sandbox), "
                "or a JWT issued by `/api/v1/auth/login`. Pass as "
                "`Authorization: Bearer <token>`."
            ),
        },
        "SubUserHeader": {
            "type": "apiKey",
            "in": "header",
            "name": "X-Sub-User",
            "description": (
                "Business-account multi-tenant scope. Optional. When set, all "
                "data is scoped to that opaque sub-user identifier. Different "
                "sub-users on the same account cannot see each other's bots."
            ),
        },
        "IdempotencyKeyHeader": {
            "type": "apiKey",
            "in": "header",
            "name": "Idempotency-Key",
            "description": (
                "Optional idempotency key for `POST /api/v1/bot`. A second request "
                "with the same key within `IDEMPOTENCY_TTL_HOURS` returns the "
                "original bot rather than creating a duplicate."
            ),
        },
    }


# Per-route summaries that show up in the Swagger sidebar / SDK method names.
# Keyed by `(METHOD, path)` — paths use the public `/api/v1/...` prefix from
# main.py's router mounts. Adding entries here is the cheapest way to give SDK
# generators clean method names without touching 30+ route definitions.
_ROUTE_SUMMARIES: dict[tuple[str, str], str] = {
    # — Auth —
    ("post", "/api/v1/auth/register"): "Register a new account",
    ("post", "/api/v1/auth/login"): "Log in (email + password)",
    ("post", "/api/v1/auth/keys"): "Create a new API key",
    ("get", "/api/v1/auth/keys"): "List API keys",
    ("delete", "/api/v1/auth/keys/{key_id}"): "Revoke an API key",
    ("get", "/api/v1/auth/plan"): "Get current plan and monthly usage",
    ("get", "/api/v1/auth/notify"): "Get notification preferences",
    ("put", "/api/v1/auth/notify"): "Update notification preferences",
    ("put", "/api/v1/auth/wallet"): "Register USDC wallet for top-ups",
    ("delete", "/api/v1/auth/account"): "Permanently delete account (GDPR erasure)",
    # — Bots —
    ("post", "/api/v1/bot/validate-meeting-url"): "Pre-flight check a meeting URL",
    ("post", "/api/v1/bot"): "Create a meeting bot",
    ("get", "/api/v1/bot"): "List bots",
    ("get", "/api/v1/bot/stats"): "Aggregate bot stats for the account",
    ("get", "/api/v1/bot/{bot_id}"): "Get a bot by ID",
    ("delete", "/api/v1/bot/{bot_id}"): "Cancel and delete a bot",
    ("post", "/api/v1/bot/{bot_id}/leave"): "Make the bot leave the meeting now",
    ("get", "/api/v1/bot/{bot_id}/transcript"): "Download the transcript",
    ("get", "/api/v1/bot/{bot_id}/recording"): "Download the audio recording",
    ("get", "/api/v1/bot/{bot_id}/video"): "Download the video recording",
    ("post", "/api/v1/bot/{bot_id}/analyze"): "Re-run analysis (optionally with a different template)",
    ("post", "/api/v1/bot/{bot_id}/ask"): "Ask a free-form question about a finished meeting",
    ("post", "/api/v1/bot/{bot_id}/share"): "Mint a public share link for the meeting",
    ("post", "/api/v1/bot/{bot_id}/say"): "Make the bot speak text into the meeting (TTS)",
    ("post", "/api/v1/bot/{bot_id}/chat"): "Make the bot post a chat message into the meeting",
    ("get", "/api/v1/bot/{bot_id}/stream"): "Server-sent live transcript + status events",
    # — Exports —
    ("get", "/api/v1/bot/{bot_id}/export/markdown"): "Export meeting report as Markdown",
    ("get", "/api/v1/bot/{bot_id}/export/pdf"): "Export meeting report as PDF",
    ("get", "/api/v1/bot/{bot_id}/export/json"): "Export full session as JSON",
    ("get", "/api/v1/bot/{bot_id}/export/srt"): "Export transcript as SRT subtitles",
    # — Webhooks —
    ("post", "/api/v1/webhook"): "Register a global webhook",
    ("get", "/api/v1/webhook"): "List webhooks",
    ("get", "/api/v1/webhook/{webhook_id}"): "Get a webhook",
    ("patch", "/api/v1/webhook/{webhook_id}"): "Update a webhook",
    ("delete", "/api/v1/webhook/{webhook_id}"): "Delete a webhook",
    ("get", "/api/v1/webhook/{webhook_id}/deliveries"): "List webhook delivery attempts",
    ("post", "/api/v1/webhook/{webhook_id}/test"): "Send a test event to the webhook",
    # — Billing —
    ("get", "/api/v1/billing/balance"): "Get credit balance and recent transactions",
    ("post", "/api/v1/billing/stripe/checkout"): "Create a Stripe Checkout session for a top-up",
    ("get", "/api/v1/billing/usdc/address"): "Get USDC deposit address",
    ("post", "/api/v1/billing/subscribe"): "Subscribe to a paid plan via Stripe",
    # — Action items —
    ("get", "/api/v1/action-items"): "List action items across all bots",
    ("patch", "/api/v1/action-items/{item_id}"): "Update an action item (status, assignee, due_date)",
    # — Integrations —
    ("post", "/api/v1/integrations"): "Create an integration (Slack/Notion)",
    ("get", "/api/v1/integrations"): "List integrations",
    ("patch", "/api/v1/integrations/{integration_id}"): "Update an integration",
    ("delete", "/api/v1/integrations/{integration_id}"): "Delete an integration",
    # — Calendar —
    ("post", "/api/v1/calendar"): "Add an iCal feed for auto-join",
    ("get", "/api/v1/calendar"): "List calendar feeds",
    ("patch", "/api/v1/calendar/{feed_id}"): "Update a calendar feed",
    ("delete", "/api/v1/calendar/{feed_id}"): "Delete a calendar feed",
    ("post", "/api/v1/calendar/{feed_id}/sync"): "Manually trigger a calendar sync",
    # — Templates —
    ("get", "/api/v1/templates"): "List built-in + custom analysis templates",
    ("get", "/api/v1/templates/default-prompt"): "Get the raw default analysis prompt",
    # — Keyword alerts —
    ("post", "/api/v1/keyword-alerts"): "Register a keyword alert",
    ("get", "/api/v1/keyword-alerts"): "List keyword alerts",
    ("delete", "/api/v1/keyword-alerts/{alert_id}"): "Delete a keyword alert",
    # — Retention —
    ("get", "/api/v1/auth/retention"): "Get retention policy",
    ("put", "/api/v1/auth/retention"): "Update retention policy",
    ("delete", "/api/v1/auth/retention"): "Reset retention policy to defaults",
    # — Auth (extended) —
    ("get", "/api/v1/auth/me"): "Get the calling account profile",
    ("get", "/api/v1/auth/wallet"): "Get registered USDC wallet address",
    ("put", "/api/v1/auth/account-type"): "Switch between personal and business account",
    ("get", "/api/v1/auth/test-keys"): "List sandbox `sk_test_…` API keys",
    ("post", "/api/v1/auth/test-keys"): "Create a sandbox `sk_test_…` API key",
    ("post", "/api/v1/auth/support-key"): "Mint a short-lived support-access key",
    ("get", "/api/v1/auth/support-keys"): "List active support-access keys",
    ("delete", "/api/v1/auth/support-key/{key_id}"): "Revoke a support-access key",
    # — Bots (extended) —
    ("get", "/api/v1/bot/{bot_id}/debug"): "Internal debug view of a bot session",
    ("get", "/api/v1/bot/{bot_id}/highlight"): "Get an auto-generated meeting highlight clip",
    ("post", "/api/v1/bot/{bot_id}/ask-live"): "Ask the bot a question while the meeting is live",
    ("post", "/api/v1/bot/{bot_id}/followup-email"): "Generate a follow-up email draft from the meeting",
    ("patch", "/api/v1/bot/{bot_id}/speakers"): "Rename or merge speakers in the transcript",
    ("get", "/api/v1/bot/{bot_id}/analytics/live"): "Live speaker / sentiment analytics",
    ("get", "/api/v1/bot/{bot_id}/analytics/history"): "Historical analytics snapshots for a bot",
    ("get", "/api/v1/bot/{bot_id}/analytics/stream"): "Server-sent stream of live analytics",
    ("get", "/api/v1/bot/{bot_id}/coaching/tips"): "List host coaching tips for the meeting",
    ("get", "/api/v1/bot/{bot_id}/coaching/stream"): "Server-sent stream of host coaching tips",
    ("get", "/api/v1/bot/{bot_id}/decisions"): "List decisions detected in the meeting",
    ("get", "/api/v1/bot/{bot_id}/memory/related"): "Find related past meetings via vector memory",
    ("post", "/api/v1/bot/{bot_id}/memory/refresh"): "Recompute related-meeting embeddings",
    ("get", "/api/v1/bot/{bot_id}/agentic/instructions"): "List agentic instructions configured for this bot",
    ("put", "/api/v1/bot/{bot_id}/agentic/instructions"): "Update agentic instructions for this bot",
    ("post", "/api/v1/bot/{bot_id}/agentic/trigger"): "Manually trigger an agentic instruction",
    ("post", "/api/v1/bot/{bot_id}/chat-qa/ask"): "Ask a question against the meeting chat history",
    # — Webhooks (extended) —
    ("get", "/api/v1/webhook/events"): "List supported webhook event names",
    ("get", "/api/v1/webhook/deliveries"): "List recent deliveries across all webhooks",
    # — Exports (extended) —
    ("post", "/api/v1/bot/{bot_id}/export/drive"): "Export meeting report to Google Drive",
    # — Search & audit —
    ("get", "/api/v1/search"): "Full-text search across transcripts",
    ("get", "/api/v1/audit-log"): "Account audit log (security-relevant events)",
    # — Keyword alerts (extended) —
    ("get", "/api/v1/keyword-alerts/{alert_id}"): "Get a keyword alert",
    ("patch", "/api/v1/keyword-alerts/{alert_id}"): "Update a keyword alert",
    # — Health —
    ("get", "/api/health"): "Liveness probe",
    ("get", "/health"): "Liveness probe (legacy path)",
    ("get", "/api/ready"): "Readiness probe",
    ("get", "/ready"): "Readiness probe (legacy path)",
}

# Reusable error envelope component. Every HTTPException raised by the app is
# wrapped by `_http_exception_handler` into this shape, so SDKs can deserialise
# all 4xx/5xx responses to the same type.
# Stable machine-readable error codes. Mirrors `_ERROR_CODE_MAP` plus a
# catch-all. Generated SDKs emit a typed enum from this list so callers can
# `match`/`switch` on `error_code` without comparing strings.
_ERROR_CODE_VALUES: list[str] = [
    "bad_request",
    "unauthorized",
    "forbidden",
    "not_found",
    "conflict",
    "validation_error",
    "too_early",
    "rate_limited",
    "internal_error",
    "bad_gateway",
    "service_unavailable",
    "unknown_error",
]

_ERROR_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["detail", "error_code", "retryable"],
    "properties": {
        "detail": {
            "description": "Human-readable error message, or a list of Pydantic validation errors.",
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "object"}},
            ],
        },
        "error_code": {
            "type": "string",
            "enum": _ERROR_CODE_VALUES,
            "description": (
                "Stable machine-readable error code. SDK generators emit a typed "
                "enum from these values so callers can branch on the code without "
                "string comparison."
            ),
        },
        "retryable": {
            "type": "boolean",
            "description": "Whether the caller should retry the same request after a backoff.",
        },
        "incident_id": {
            "type": "string",
            "description": "Set on 5xx responses for support tracing.",
        },
    },
    "example": {
        "detail": "Bot 'bot_abc' not found",
        "error_code": "not_found",
        "retryable": False,
    },
}

# Header definitions reused across responses. Declaring them at the schema
# level lets SDK generators surface the headers in their typed Response objects.
_RATE_LIMIT_HEADERS: dict[str, dict[str, Any]] = {
    "X-RateLimit-Remaining": {
        "description": "Requests remaining in the current rate-limit window.",
        "schema": {"type": "integer", "minimum": 0},
    },
    "X-RateLimit-Reset": {
        "description": "Unix epoch seconds at which the current window resets.",
        "schema": {"type": "integer"},
    },
}

_RETRY_AFTER_HEADER: dict[str, dict[str, Any]] = {
    "Retry-After": {
        "description": (
            "Seconds the caller should wait before retrying. Honour this on "
            "429 and 503 responses; it overrides any backoff your client computes."
        ),
        "schema": {"type": "integer", "minimum": 0},
    },
}


# Standard error responses surfaced in `responses:` for every authenticated
# route. SDK generators emit named exception classes from these and reuse the
# `ErrorResponse` component instead of inlining the schema 100+ times.
def _err(description: str, *, headers: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {
        "description": description,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ErrorResponse"},
            },
        },
    }
    if headers:
        response["headers"] = headers
    return response


_STANDARD_ERROR_RESPONSES: dict[str, dict[str, Any]] = {
    "401": _err("Missing or invalid Authorization header."),
    "403": _err("Authenticated but not permitted (e.g. admin-only resource, sandbox limits)."),
    "404": _err("Resource not found, or not owned by the calling account."),
    "429": _err(
        "Rate-limit exceeded. Retry after the seconds in the `Retry-After` header.",
        headers={**_RATE_LIMIT_HEADERS, **_RETRY_AFTER_HEADER},
    ),
}


def _apply_route_summaries(schema: dict[str, Any]) -> None:
    """Inject summaries + standard error responses into individual operations."""
    paths = schema.get("paths") or {}
    for path, ops in paths.items():
        if not isinstance(ops, dict):
            continue
        for method, op in ops.items():
            method_lc = method.lower()
            if method_lc not in {"get", "post", "put", "patch", "delete"}:
                continue
            if not isinstance(op, dict):
                continue
            summary = _ROUTE_SUMMARIES.get((method_lc, path))
            if summary:
                # Override FastAPI's auto-generated "Function Name" summary.
                op["summary"] = summary
            elif not op.get("summary"):
                pass  # Leave the default; better than empty.
            # Add standard error responses for routes that already declare auth-required.
            # Skip the unauthenticated register/login endpoints.
            responses = op.setdefault("responses", {})
            if path in {"/api/v1/auth/register", "/api/v1/auth/login"}:
                continue
            for code, body in _STANDARD_ERROR_RESPONSES.items():
                responses.setdefault(code, body)


def _webhook_components() -> dict[str, Any]:
    """Formal OpenAPI components describing webhook delivery payloads.

    Until v2.49.0 the webhook contract was prose-only in the description.
    Generators now have a concrete `WebhookPayload` schema and a string-enum
    `WebhookEvent` covering all 14 events fired by the platform.
    """
    return {
        "WebhookEvent": {
            "type": "string",
            "description": "Webhook event names emitted by the platform.",
            "enum": [
                "bot.joining",
                "bot.in_call",
                "bot.call_ended",
                "bot.transcript_ready",
                "bot.analysis_ready",
                "bot.done",
                "bot.error",
                "bot.cancelled",
                "bot.keyword_alert",
                "bot.live_transcript",
                "bot.live_transcript_translated",
                "bot.live_chat_message",
                "bot.recurring_intel_ready",
                "bot.test",
            ],
        },
        "WebhookPayload": {
            "type": "object",
            "description": (
                "Body POSTed to your `webhook_url`. Signed with HMAC-SHA256 in the "
                "`X-MeetingBot-Signature` header (format: `sha256=<hex>`). "
                "`X-MeetingBot-Timestamp` carries the unix-seconds delivery time."
            ),
            "required": ["event", "delivery_id", "timestamp", "data"],
            "properties": {
                "event": {"$ref": "#/components/schemas/WebhookEvent"},
                "delivery_id": {
                    "type": "string",
                    "description": "Unique ID for this delivery attempt.",
                },
                "timestamp": {
                    "type": "string",
                    "format": "date-time",
                    "description": "ISO-8601 UTC timestamp the event was emitted.",
                },
                "bot_id": {
                    "type": "string",
                    "description": "Bot the event relates to (omitted for `bot.test`).",
                },
                "account_id": {
                    "type": "string",
                    "description": "Account that owns the bot.",
                },
                "data": {
                    "type": "object",
                    "description": (
                        "Event-specific payload. For `bot.done` this includes the "
                        "full BotResponse; for `bot.live_transcript` it carries "
                        "`{entry: {speaker, text, source, timestamp}}`; for "
                        "`bot.live_chat_message` it carries an additional "
                        "`message_id`."
                    ),
                },
            },
            "example": {
                "event": "bot.done",
                "delivery_id": "evt_2c8f4b9a",
                "timestamp": "2026-05-04T12:34:56Z",
                "bot_id": "bot_abc123",
                "account_id": "acct_xyz",
                "data": {"status": "done", "transcript": [], "analysis": {}},
            },
        },
    }


def _apply_global_extras(
    schema: dict[str, Any],
    *,
    admin: bool,
    request: "Request | None" = None,
) -> None:
    """Augment a FastAPI-generated schema with servers, security, and tags.

    Mutates `schema` in place. Called on both the public and admin schemas so
    SDK generators always get a self-contained spec. When called from a live
    HTTP handler with `request` set, the `servers` block uses the actual
    request host as a fallback when `PUBLIC_BASE_URL` is empty.
    """
    schema["servers"] = _server_entries(include_admin=admin, request=request)
    components = schema.setdefault("components", {})
    components.setdefault("securitySchemes", {}).update(_security_components())
    component_schemas = components.setdefault("schemas", {})
    component_schemas.setdefault("ErrorResponse", _ERROR_RESPONSE_SCHEMA)
    component_schemas.update(_webhook_components())
    # Reusable header components — every response emitted by the app carries
    # these (see `add_rate_limit_headers` middleware), so SDKs can `$ref` them
    # without redefining per route.
    component_headers = components.setdefault("headers", {})
    for name, definition in {**_RATE_LIMIT_HEADERS, **_RETRY_AFTER_HEADER}.items():
        component_headers.setdefault(name, definition)
    schema["security"] = [{"BearerAuth": []}]
    _apply_route_summaries(schema)
    # Preserve any existing `tags` (FastAPI auto-generates entries for tags it
    # discovers on routes); merge our descriptions in.
    existing = {t.get("name"): t for t in schema.get("tags", []) if isinstance(t, dict)}
    merged = []
    seen = set()
    for tag in _OPENAPI_TAGS:
        seen.add(tag["name"])
        merged.append({**existing.get(tag["name"], {}), **tag})
    for name, tag in existing.items():
        if name not in seen:
            merged.append(tag)
    schema["tags"] = merged
    # Top-level contact + license help SDK generators emit cleaner package metadata.
    schema.setdefault("info", {}).setdefault(
        "contact",
        {"name": "JustHereToListen.io", "url": "https://justheretolisten.io"},
    )


def _make_public_openapi() -> dict[str, Any]:
    """Return a filtered OpenAPI schema for the public docs.

    Returns a deep copy so callers (or FastAPI internals) that mutate the
    returned dict don't poison the cached schema for subsequent requests.
    """
    import copy

    if _public_openapi_cache:
        return copy.deepcopy(_public_openapi_cache)

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

    _apply_global_extras(schema, admin=False)

    _public_openapi_cache.update(schema)
    return copy.deepcopy(_public_openapi_cache)


app.openapi = _make_public_openapi


# ── Public docs (request-aware) ───────────────────────────────────────────────
# We disable FastAPI's auto-generated docs/redoc/openapi routes and serve our
# own so the schema's `servers:` block reflects the actual host the client
# reached — without depending on `PUBLIC_BASE_URL` being configured.

from fastapi.openapi.docs import get_redoc_html as _get_redoc_html


@app.get("/api/openapi.json", include_in_schema=False)
async def public_openapi_schema(request: Request):
    """Public OpenAPI schema (no admin / analytics routes; no `ai_usage` cost fields)."""
    import copy

    schema = copy.deepcopy(_make_public_openapi())
    schema["servers"] = _server_entries(include_admin=False, request=request)
    return schema


@app.get("/api/docs", include_in_schema=False, response_class=HTMLResponse)
async def public_api_docs():
    """Swagger UI for the public API."""
    return _get_swagger_ui_html(
        openapi_url="/api/openapi.json",
        title="JustHereToListen.io API",
        swagger_favicon_url="",
    )


@app.get("/api/redoc", include_in_schema=False, response_class=HTMLResponse)
async def public_api_redoc():
    """ReDoc reference for the public API."""
    return _get_redoc_html(
        openapi_url="/api/openapi.json",
        title="JustHereToListen.io API — Reference",
        redoc_favicon_url="",
    )


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
async def admin_openapi_schema(request: Request):
    """Full OpenAPI schema — includes admin-only endpoints, platform analytics, and ai_usage fields."""
    schema = _get_openapi_util(
        title="JustHereToListen.io Admin API",
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    _apply_global_extras(schema, admin=True, request=request)
    return schema


_QUICKSTART_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>JustHereToListen.io — API Quickstart</title>
<style>
  :root { --fg:#1a1a1a; --muted:#666; --accent:#3b5fbc; --bg:#fafafa; --code:#f4f4f5; }
  body { font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         color: var(--fg); background: var(--bg); margin: 0; padding: 0; }
  main { max-width: 820px; margin: 0 auto; padding: 48px 24px 96px; }
  h1 { font-size: 32px; margin: 0 0 8px; }
  h2 { font-size: 22px; margin: 36px 0 12px; border-bottom: 1px solid #ddd; padding-bottom: 6px; }
  h3 { font-size: 17px; margin: 24px 0 8px; }
  .lede { color: var(--muted); margin: 0 0 24px; }
  .links { display: flex; gap: 12px; flex-wrap: wrap; margin: 16px 0 32px; }
  .links a { padding: 10px 16px; background: var(--accent); color: #fff; border-radius: 6px;
             text-decoration: none; font-weight: 500; font-size: 14px; }
  .links a:hover { background: #2c4a96; }
  pre { background: var(--code); padding: 14px 16px; border-radius: 6px; overflow-x: auto;
        font-size: 13px; line-height: 1.5; }
  code { background: var(--code); padding: 1px 6px; border-radius: 3px; font-size: 0.9em; }
  pre code { background: transparent; padding: 0; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 12px 0; }
  .grid > div { background: #fff; border: 1px solid #e3e3e3; padding: 12px 14px; border-radius: 6px; }
  .grid h4 { margin: 0 0 4px; font-size: 14px; color: var(--accent); }
  .grid p { margin: 0; font-size: 14px; color: var(--muted); }
  ul { padding-left: 22px; }
  li { margin: 4px 0; }
  small { color: var(--muted); }
</style>
</head>
<body>
<main>
  <h1>JustHereToListen.io — API Quickstart</h1>
  <p class="lede">Send headless bots into Zoom / Google Meet / Microsoft Teams / onepizza.io meetings to record, transcribe (Gemini or Whisper), and analyse (Claude or Gemini) them.</p>

  <div class="links">
    <a href="/api/docs">Interactive API reference (Swagger UI)</a>
    <a href="/api/redoc">ReDoc reference</a>
    <a href="/api/openapi.json">OpenAPI 3.1 schema (JSON)</a>
  </div>

  <h2>1. Get an API key</h2>
  <pre><code>curl -X POST {BASE}/api/v1/auth/register \\
  -H "Content-Type: application/json" \\
  -d '{"email":"you@example.com","password":"supersecret"}'</code></pre>
  <p>Copy the <code>api_key</code> from the response — it's shown <strong>once</strong>. Format: <code>sk_live_…</code>.</p>

  <h2>2. Top up credits</h2>
  <p>Bot runs cost $0.10 each (flat fee). Top up via Stripe Checkout or USDC.</p>
  <pre><code>curl -X POST {BASE}/api/v1/billing/stripe/checkout \\
  -H "Authorization: Bearer sk_live_…" \\
  -H "Content-Type: application/json" \\
  -d '{"amount_usd": 25}'</code></pre>

  <h2>3. Send a bot into a meeting</h2>
  <h3>cURL</h3>
  <pre><code>curl -X POST {BASE}/api/v1/bot \\
  -H "Authorization: Bearer sk_live_…" \\
  -H "Content-Type: application/json" \\
  -d '{
    "meeting_url": "https://meet.google.com/abc-defg-hij",
    "bot_name": "Notes Bot",
    "webhook_url": "https://your-app.example.com/webhook",
    "template": "sales"
  }'</code></pre>

  <h3>Python</h3>
  <pre><code>from meetingbot import MeetingBotClient

mb = MeetingBotClient(api_key="sk_live_…")
bot = mb.create_bot(
    meeting_url="https://meet.google.com/abc-defg-hij",
    template="sales",
    webhook_url="https://your-app.example.com/webhook",
)
print(bot.id, bot.status)</code></pre>

  <h3>JavaScript / TypeScript</h3>
  <pre><code>import { MeetingBotClient } from "meetingbot-sdk";

const mb = new MeetingBotClient({ apiKey: "sk_live_…" });
const bot = await mb.createBot({
  meeting_url: "https://meet.google.com/abc-defg-hij",
  template: "sales",
  webhook_url: "https://your-app.example.com/webhook",
});
console.log(bot.id, bot.status);</code></pre>

  <h2>4. Receive results</h2>
  <p>Either poll <code>GET /api/v1/bot/{id}</code> until <code>status</code> is <code>done</code>, or register a webhook URL and let us POST results to you when each bot finishes.</p>
  <p>Webhook payloads are signed with HMAC-SHA256 (header <code>X-MeetingBot-Signature: sha256=&lt;hex&gt;</code>) and retried with exponential backoff up to 5 times.</p>

  <h2>Authentication options</h2>
  <div class="grid">
    <div><h4>API key</h4><p><code>Authorization: Bearer sk_live_…</code> — primary auth for backend integrations.</p></div>
    <div><h4>Sandbox key</h4><p><code>Authorization: Bearer sk_test_…</code> — same surface but bills $0 and runs in test mode.</p></div>
    <div><h4>JWT</h4><p>Issued by <code>POST /api/v1/auth/login</code> for browser sessions.</p></div>
    <div><h4>Sub-user</h4><p><code>X-Sub-User: &lt;id&gt;</code> — multi-tenant isolation on a business account.</p></div>
  </div>

  <h2>Errors</h2>
  <p>Every 4xx/5xx response returns:</p>
  <pre><code>{
  "detail": "Bot 'bot_abc' not found",
  "error_code": "not_found",
  "retryable": false
}</code></pre>
  <p>Stable <code>error_code</code> values include <code>unauthorized</code>, <code>forbidden</code>, <code>not_found</code>, <code>rate_limited</code>, <code>validation_error</code>, <code>internal_error</code>. Honour <code>retryable</code>; on rate limits also honour the <code>Retry-After</code> header.</p>

  <h2>Webhooks events</h2>
  <ul>
    <li><code>bot.joining</code> · <code>bot.in_call</code> · <code>bot.call_ended</code></li>
    <li><code>bot.transcript_ready</code> · <code>bot.analysis_ready</code> · <code>bot.done</code></li>
    <li><code>bot.error</code> · <code>bot.cancelled</code> · <code>bot.keyword_alert</code></li>
    <li><code>bot.live_transcript</code> · <code>bot.live_transcript_translated</code> · <code>bot.live_chat_message</code></li>
    <li><code>bot.recurring_intel_ready</code> · <code>bot.decision_detected</code> · <code>bot.coaching_tip</code> · <code>bot.coaching_alert</code></li>
    <li><code>bot.speaker_analytics</code> · <code>bot.agentic_action</code> · <code>action_item.due_soon</code> · <code>action_item.overdue</code> · <code>bot.test</code></li>
  </ul>

  <p><small>Need help? Contact JustHereToListen.io support.</small></p>
</main>
</body>
</html>
"""


@app.get("/api/quickstart", include_in_schema=False, response_class=HTMLResponse)
async def quickstart_page(request: Request) -> HTMLResponse:
    """Friendly landing page with curl/Python/JS quickstarts.

    Linked from the top of `/api/docs` and from the OpenAPI description so
    integrators have a fast onramp before hitting the full schema.
    """
    base = (settings.PUBLIC_BASE_URL or "").rstrip("/") or str(request.base_url).rstrip("/")
    return HTMLResponse(_QUICKSTART_HTML.replace("{BASE}", base))


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


def _incident_id() -> str:
    """Generate a short unique incident ID for error tracking."""
    import uuid as _uuid
    return _uuid.uuid4().hex[:12]


@app.exception_handler(_RequestValidationError)
async def _validation_exception_handler(request, exc: _RequestValidationError):
    """Wrap Pydantic validation errors into the machine-readable structure."""
    iid = _incident_id()
    return _JSONResponse(
        status_code=422,
        content={
            "detail": exc.errors(),
            "error_code": "validation_error",
            "incident_id": iid,
            "retryable": False,
        },
    )


@app.exception_handler(_HTTPException)
async def _http_exception_handler(request, exc: _HTTPException):
    """Wrap FastAPI HTTPExceptions into a machine-readable structure."""
    code, retryable = _ERROR_CODE_MAP.get(exc.status_code, ("unknown_error", exc.status_code >= 500))
    body: dict = {
        "detail": exc.detail,
        "error_code": code,
        "retryable": retryable,
    }
    if exc.status_code >= 500:
        body["incident_id"] = _incident_id()
    return _JSONResponse(
        status_code=exc.status_code,
        content=body,
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request, exc):
    """Log full traceback for unhandled exceptions so production errors are diagnosable."""
    import traceback
    iid = _incident_id()
    logger.error(
        "Unhandled %s on %s %s (incident=%s):\n%s",
        type(exc).__name__,
        request.method,
        request.url.path,
        iid,
        traceback.format_exc(),
    )
    return _JSONResponse(
        status_code=500,
        content={
            "detail": "Internal Server Error",
            "error_code": "internal_error",
            "incident_id": iid,
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

@app.get(
    "/health",
    tags=["Health"],
    responses={200: {"content": {"application/json": {"example": {
        "status": "ok",
        "service": "JustHereToListen.io",
        "version": "2.52.0",
        "background_tasks": {
            "queue_processor": {"status": "healthy", "last_heartbeat": "2026-05-04T15:34:18Z", "age_seconds": 12},
            "cleanup_loop": {"status": "healthy", "last_heartbeat": "2026-05-04T15:00:00Z", "age_seconds": 2058},
        },
    }}}}},
)
@app.get(
    "/api/health",
    tags=["Health"],
    responses={200: {"content": {"application/json": {"example": {
        "status": "ok",
        "service": "JustHereToListen.io",
        "version": "2.52.0",
        "background_tasks": {
            "queue_processor": {"status": "healthy", "last_heartbeat": "2026-05-04T15:34:18Z", "age_seconds": 12},
        },
    }}}}},
)
async def health():
    """Liveness probe — returns 200 when the process is running.

    Does NOT check external dependencies (use /ready for that).
    Kubernetes: use this as the `livenessProbe`.
    Includes background task heartbeat status.
    """
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc)
    # Expected task names and their max allowed silence (seconds)
    _EXPECTED_TASKS = {
        "queue_processor": 120,
        "cleanup_loop": 7200,
        "webhook_retry": 120,
        "calendar_poll": 600,
        "retention_enforcement": 7200,
        "monthly_reset": 7200,
        "weekly_digest": 604800,
    }
    tasks_status = {}
    for name, max_silence_s in _EXPECTED_TASKS.items():
        hb = _task_heartbeats.get(name)
        if hb is None:
            tasks_status[name] = {"status": "not_started"}
        else:
            age_s = (now - hb).total_seconds()
            tasks_status[name] = {
                "status": "healthy" if age_s < max_silence_s else "stale",
                "last_heartbeat": hb.isoformat(),
                "age_seconds": round(age_s),
            }
    return {
        "status": "ok",
        "service": "JustHereToListen.io",
        "version": _APP_VERSION,
        "background_tasks": tasks_status,
    }


@app.get(
    "/ready",
    tags=["Health"],
    responses={
        200: {"content": {"application/json": {"example": {
            "status": "ok",
            "checks": {"database": "ok", "ai_provider": "ok"},
        }}}},
        503: {"content": {"application/json": {"example": {
            "status": "degraded",
            "checks": {"database": "error: connection refused", "ai_provider": "ok"},
        }}}},
    },
)
@app.get(
    "/api/ready",
    tags=["Health"],
    responses={
        200: {"content": {"application/json": {"example": {
            "status": "ok",
            "checks": {"database": "ok", "ai_provider": "ok"},
        }}}},
        503: {"content": {"application/json": {"example": {
            "status": "degraded",
            "checks": {"database": "error: connection refused", "ai_provider": "ok"},
        }}}},
    },
)
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
        # Don't echo the raw exception (may include DSN host/port/driver
        # internals) on this unauthenticated probe — log it, return generic.
        logger.error("Readiness DB check failed: %s", exc)
        checks["database"] = "error"
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
