# JustHereToListen.io — Claude Code Guide

## Project Identity
Multi-tenant meeting bot API. Headless Chromium (Playwright) joins Zoom / Google Meet / Microsoft Teams / onepizza.io meetings, records audio, transcribes with Gemini or Whisper, and analyses with Claude or Gemini.
Stack: **FastAPI 0.115** + **SQLAlchemy 2.0 async** + **Pydantic v2** + **Playwright 1.49** + **Python 3.12**

Brand name is **JustHereToListen.io** everywhere (UI, emails, API descriptions). Never write "MeetingBot" in user-facing text.

---

## Repo Layout

```
backend/app/
├── main.py                   # Entry point, lifespan, all 20+ router mounts
├── config.py                 # 80+ env vars via pydantic-settings (Settings class)
├── store.py                  # In-memory BotSession dataclass + Store singleton (asyncio.Lock)
├── deps.py                   # Auth dependencies: require_auth, require_admin, get_current_account_id
├── db.py                     # AsyncSessionLocal factory, create_all_tables, schema migrations
├── models/account.py         # 23 SQLAlchemy ORM models (Account, BotSnapshot, Webhook, ActionItem, ...)
├── schemas/bot.py            # Pydantic: BotCreate, BotResponse, MeetingAnalysis
├── schemas/webhook.py        # WebhookCreate, WebhookResponse
├── api/                      # FastAPI routers (bots, auth, billing, analytics, webhooks, ...)
├── services/
│   ├── browser_bot.py        # 3700-line Playwright automation — DO NOT casually edit
│   ├── bot_service.py        # Bot lifecycle: queue → join → transcribe → analyse → notify
│   ├── intelligence_service.py  # AI prompts, Claude/Gemini calls, token/cost tracking
│   ├── transcription_service.py # Gemini Speech-to-Text + local Whisper fallback
│   ├── webhook_service.py    # Delivery + HMAC signing + exponential backoff retry
│   ├── email_service.py      # SMTP / SendGrid + weekly digest
│   ├── mcp_service.py        # MCP tool implementations
│   └── ...                   # integration_service, pii_service, tts_service, etc.
└── templates/                # 11 Jinja2 HTML templates
sdk/python/                   # Python SDK (httpx + pydantic)
sdk/js/                       # TypeScript SDK
```

---

## Architecture: In-Memory vs DB

This distinction matters — get it wrong and you'll read stale data or lose live state.

| State | Where | When |
|---|---|---|
| Active bots (ready/scheduled/queued/joining/in_call/call_ended/transcribing) | **RAM** — `Store` singleton | During lifecycle |
| Terminal bots (done/error/cancelled) | **DB** — `BotSnapshot` table (JSON blob) | After completion, 24h TTL |
| Accounts, webhooks, action items, delivery logs | **DB** — `AsyncSessionLocal` | Always |

- Use `await store.get_bot(id)` for live status during a meeting
- Use `BotSnapshot` / `AsyncSessionLocal` for analytics, history, and post-completion queries
- `store.update_bot(bot_id, **kwargs)` mutates in-memory state via `setattr`; also persists terminal bots to DB

---

## Authentication

Three-tier priority (resolved in `deps.py → get_current_account_id`):

1. `API_KEY` env var present → returns `SUPERADMIN_ACCOUNT_ID = "__superadmin__"` (bypasses all per-user checks)
2. JWT (`eyJ...`) → decoded via `JWT_SECRET`, returns `account_id` from `sub` claim
3. Per-user API key (`sk_live_...` / `sk_test_...`) → DB lookup on `ApiKey` table

`request.state.account_id` is set by middleware for all protected routes.
`request.state.sandbox = True` when a `sk_test_*` key is used.

Protected routes: `Depends(require_auth)` — raises 401 if unauthenticated.
Admin routes: `Depends(require_admin)` — requires email in `ADMIN_EMAILS` or `account.is_admin`.

---

## Coding Patterns — Follow These

**Async-first**
```python
# Fire-and-forget background work
asyncio.create_task(some_async_fn())          # correct
# Never use FastAPI BackgroundTasks — not used anywhere in this codebase
```

**Optional field access on BotSession**
```python
getattr(bot, "translation_language", None)    # safe — field may not exist on old sessions
bot.transcript                                 # fine — guaranteed in dataclass
```

**Database sessions**
```python
async with AsyncSessionLocal() as session:
    result = await session.execute(select(Account).where(...))
    row = result.scalar_one_or_none()
    await session.commit()
```

**Error responses — always HTTPException**
```python
raise HTTPException(status_code=404, detail=f"Bot {bot_id!r} not found")
# Never return {"error": "..."} dicts from route handlers
```

**Ownership check → 404 (not 403)**
```python
if bot.account_id != account_id:
    raise HTTPException(status_code=404, detail=f"Bot {bot_id!r} not found")
# 404 prevents info leakage about resource existence
```

**AI provider priority**
```python
# Claude is primary (ANTHROPIC_API_KEY), Gemini is fallback (GEMINI_API_KEY)
# intelligence_service.py handles this internally — don't bypass it
```

**HTML safety in emails / PDFs**
```python
from html import escape as _he
f"<li>{_he(user_text)}</li>"   # required — Python f-strings do NOT auto-escape
# Jinja2 templates DO auto-escape — no manual escaping needed in .html files
```

---

## Supported Platforms

| Key | Hosts | Browser bot? |
|---|---|---|
| `google_meet` | `meet.google.com` | Yes |
| `zoom` | `zoom.us`, `zoom.com` | Yes |
| `microsoft_teams` | `teams.microsoft.com`, `teams.live.com` | Yes |
| `onepizza` | `onepizza.io` | Yes — lobby: `#lobbyName`, `#lobbyJoinBtn`; waiting room: `#waitingRoomOverlay` |
| `webex`, `whereby`, `bluejeans`, `gotomeeting` | various | URL detection only |

To add a new platform: update `_PLATFORM_NETLOC` + `_REAL_PLATFORMS` in `bot_service.py`, add `_join_<platform>()` in `browser_bot.py`, add entries in `_IN_CALL_TEXTS`, `_END_TEXTS`, `_ALONE_TEXTS`, `_WAITING_TEXTS`.

---

## Analysis Templates

10 built-in templates in `_BUILTIN_TEMPLATE_PROMPTS` (intelligence_service.py):
`default` `sales` `standup` `1on1` `retro` `kickoff` `allhands` `postmortem` `interview` `design-review`

All templates return JSON matching `MeetingAnalysis` schema:
`summary`, `key_points`, `action_items` (with `confidence`), `decisions`, `next_steps`, `sentiment`, `topics` (with `start_time`/`end_time`), `risks_blockers`, `next_meeting`, `unresolved_items`

`MeetingAnalysis` uses `model_config = {"extra": "allow"}` — adding new fields to the prompt is safe for old clients.

---

## Webhook Events (13 total)

Defined in `WEBHOOK_EVENTS` in `api/webhooks.py`. Signed with HMAC-SHA256.

```
bot.joining  bot.in_call  bot.call_ended  bot.transcript_ready  bot.analysis_ready
bot.done  bot.error  bot.cancelled  bot.keyword_alert
bot.live_transcript  bot.live_transcript_translated  bot.recurring_intel_ready  bot.test
```

**Do not rename** `X-MeetingBot-Signature` / `X-MeetingBot-Timestamp` headers — SDK consumers depend on them.

---

## Critical Env Vars

```bash
JWT_SECRET              # REQUIRED — auto-randomised if missing (all sessions lost on restart)
ANTHROPIC_API_KEY       # Primary AI — Claude Sonnet 4.6
GEMINI_API_KEY          # Fallback AI, transcription, embeddings
DATABASE_URL            # Default: sqlite+aiosqlite:///./meetingbot.db  (use postgresql+asyncpg:// in prod)
CORS_ORIGINS            # Default: * — MUST restrict in production
ADMIN_EMAILS            # Comma-separated list for admin panel access
MAX_CONCURRENT_BOTS     # Default: 3 — each bot spawns a Chromium process
```

Full list in `backend/app/config.py` (~80 settings).

---

## Running Locally

```bash
cd backend
pip install -r requirements.txt
playwright install chromium
uvicorn app.main:app --reload --port 8000
```

- API docs: `http://localhost:8000/api/docs`
- Dashboard: `http://localhost:8000/dashboard`
- Webhook playground: `http://localhost:8000/webhook-playground`

Docker (recommended — includes PostgreSQL):
```bash
docker-compose up
```

---

## Deployment (Railway)

- **Config**: `railway.toml` + `backend/Dockerfile`
- **Build**: Python 3.12-slim + ffmpeg + PulseAudio + Xvfb + Playwright Chromium
- **Release command**: `python init_db.py` (runs DB migrations before new instance starts)
- **Start**: `/app/start.sh` → PulseAudio init → uvicorn on `$PORT`
- **Health check**: `GET /health`

---

## No Automated Tests

No `tests/` directory. Verify changes via:
- Swagger UI at `/api/docs`
- Webhook playground at `/webhook-playground`
- Manual bot creation with a real or simulated meeting URL

---

## Do NOT Change These

| Thing | Why |
|---|---|
| `X-MeetingBot-Signature` / `X-MeetingBot-Timestamp` | SDK and integration consumers validate these header names |
| `MeetingBotError` class name | Internal exception used throughout browser_bot.py |
| `SUPERADMIN_ACCOUNT_ID = "__superadmin__"` | Sentinel matched across many files |
| `sk_live_` / `sk_test_` API key prefixes | Sandbox detection logic depends on these |
| Bot status strings (`ready`, `joining`, `in_call`, `transcribing`, `done`, etc.) | Stored in DB, returned in API, matched in frontend |
| `bot_snapshots`, `accounts`, `webhooks` table names | Migrations and existing DB depend on them |
