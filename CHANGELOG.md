# Changelog

All notable changes to MeetingBot are documented here.

Format: `## [version] - YYYY-MM-DD` followed by categorised bullet points.

> **Latest version:** 2.21.0 — **Last updated:** 2026-03-28

---

## [2.21.0] - 2026-03-28

### Added
- **Dark mode** — system-wide dark theme with toggle button in navbar on all pages (dashboard, landing, login, register, bot detail, share). Respects `prefers-color-scheme` OS setting, persists preference in localStorage, no flash of unstyled content on page load
- **Mobile responsive dashboard** — sidebar collapses to a fixed bottom navigation bar on mobile (< 768px) with horizontally scrollable icons, bottom padding for content, and responsive section headers
- **Mobile responsive login/register** — split-panel layout stacks vertically on mobile with condensed branding panel and full-width form
- **Mobile responsive bot detail** — action buttons wrap properly, tables scroll horizontally, meta cards maintain 2-column grid on small screens
- **Mobile responsive share page** — padding adjustments for small screens
- **Visual polish** — page fade-in animation (0.3s), card hover lift effect, pulse animation on in-progress status chips, loading skeleton CSS utility class, focus-visible keyboard navigation outlines
- **Loading button state** — `.btn-loading` CSS class with spinner animation, applied on form submit for login/register
- **Password visibility toggle** — eye icon button on login and register password fields
- **Confirmation dialog** — reusable `confirmAction(message)` JS helper using Bootstrap modal for destructive actions
- **Progress indicator** — animated progress bar shown on bot detail page for in-progress statuses (joining, in_call, transcribing)
- **Section transitions** — dashboard section switching now uses fade-in animation

## [2.20.6] - 2026-03-28

### Fixed
- **Webhook `/deliveries` endpoint unreachable** — `GET /api/v1/webhook/deliveries` was defined after `GET /api/v1/webhook/{webhook_id}`, so FastAPI matched "deliveries" as a webhook ID and returned 404. Moved the literal route before the parameterized one
- **Semantic search unreachable** — duplicate `GET /search` handler in analytics meant the second handler (with `semantic` embedding search) was shadowed by the first. Merged both into a single handler supporting `q`, `limit`, `include_archived`, `platform`, and `semantic` parameters

## [2.20.5] - 2026-03-28

### Fixed
- **Race condition in credit addition** — `add_credits()` read the account balance without a database lock, allowing concurrent additions to lose updates. Now uses `SELECT ... FOR UPDATE` matching the existing `deduct_credits_for_bot()` pattern
- **OAuth authorization URL not URL-encoded** — query parameters were concatenated with raw `f"{k}={v}"` instead of `urlencode()`. The `state` parameter (base64 HMAC) can contain `+` and `=` which broke the redirect URL
- **JS SDK dead code** — removed unused `new URL()` construction in the URL builder method

## [2.20.4] - 2026-03-28

### Fixed
- **HTML injection in error notification email** — `bot_id` and `error` were injected raw into HTML in `notify_meeting_error`. Now escaped with `html.escape()` to prevent XSS via crafted error messages
- **OAuth callback CSRF bypass** — the `state` parameter was only validated if present, allowing attackers to omit it entirely and bypass CSRF protection. Now required on all OAuth callbacks
- **Integration update skips config validation** — `PATCH /integrations/{id}` validated the type but not the config, allowing type changes without required fields (e.g. Slack without `webhook_url`). Extracted validation into a shared helper used by both create and update

## [2.20.3] - 2026-03-28

### Fixed
- **AI cost tracking returning $0 for Haiku calls** — `_estimate_cost()` failed to match model IDs with date suffixes (e.g. `claude-haiku-4-5-20251001`) against the pricing table keyed by short names (`claude-haiku-4-5`). Now strips `-YYYYMMDD` suffixes before lookup

## [2.20.2] - 2026-03-28

### Fixed
- **Bot cancellation broken** — `asyncio.shield()` in `delete_bot` was protecting the lifecycle task from the cancellation signal, so `DELETE /bot/{id}` appeared to succeed but the bot kept running
- **Webhook accessor race condition** — `get_webhook()`, `list_webhooks()`, and `active_webhooks()` accessed the shared webhook dict without acquiring the asyncio lock, risking `RuntimeError: dictionary changed size during iteration` when webhooks were deleted concurrently
- **Oversized error messages** — bot error messages from uncaught exceptions were stored without truncation, potentially producing very large API responses and webhook payloads. Now capped at 2000 characters

## [2.20.1] - 2026-03-28

### Fixed
- **Double credit deduction** — if webhook dispatch failed after credits were deducted on the success path, the exception handler would deduct again. Added idempotency guard to prevent duplicate charges
- **Webhook test signature broken** — `POST /webhooks/{id}/test` assigned the raw `(sig, ts)` tuple to the `X-MeetingBot-Signature` header instead of unpacking it; also missing `X-MeetingBot-Timestamp` header entirely
- **422 validation errors not in structured format** — Pydantic `RequestValidationError` responses now include `error_code` and `retryable` fields matching the new machine-readable error model

## [2.20.0] - 2026-03-28

### Added
- **`POST /api/v1/bot/validate-meeting-url`** — fast-fail pre-flight endpoint that checks URL validity, detects the meeting platform, and reports whether real recording is supported
- **Machine-readable error responses** — all HTTP error responses now include `error_code` (e.g. `not_found`, `rate_limited`) and `retryable` (boolean) fields alongside `detail`
- **Webhook payload enrichment** — `bot.error` and `bot.cancelled` webhook events now include `error_code`, `error_message`, and `retryable` fields for programmatic error handling
- **Meeting URL normalisation** — personalisation query params (`name`, `displayName`, `email`, `avatar`, etc.) are stripped from meeting URLs before passing to the browser, preventing unintended auto-fill behaviour

### Fixed
- **Bot stuck in "ready" — never joins** — direct (non-scheduled) bot creation was missing the `status="joining"` update before starting the lifecycle task, so the bot appeared to never start
- **Scheduled bots with `join_at` ≈ now never joining** — if `join_at` was less than 1 second in the future, the 0-second timer could misfire; now starts immediately via `_start_or_queue_bot`
- **Queue processor race condition** — `_queue_event` was cleared before checking slot availability, causing up to 30-second delays for queued bots when a slot freed up

## [2.19.0] - 2026-03-26

### Added
- **Meeting History tab** in dashboard — browse all past meetings from the database, not just the 24-hour in-memory window. Shows URL, platform, status, duration, participant count, and transcript/analysis availability badges
- **Bot detail page DB fallback** — `/bot/{id}` now loads from `BotSnapshot` DB when the bot has expired from RAM. Users can view transcripts, analysis, and all meeting details from any historical meeting

## [2.18.0] - 2026-03-26

### Added
- **Transcript search** on bot detail page — filter and highlight entries in real time with match counter
- **"Ask about this meeting"** button — AI-powered Q&A on any completed meeting via modal (wires existing `POST /ask` endpoint)
- **"Generate follow-up email"** button — one-click AI follow-up email generation with copy-to-clipboard (wires existing `POST /followup-email` endpoint)
- **Bot search/filter** on dashboard — search by ID or URL, filter by platform (Zoom/Teams/Meet)

### Fixed
- **Auth broken on bot detail page** — Share link and speaker rename were calling `/api/v1/...` directly with cookies (API only reads Bearer tokens). Added proxy routes: `/dashboard/bot/{id}/share`, `/dashboard/bot/{id}/speakers`, `/dashboard/bot/{id}/ask`, `/dashboard/bot/{id}/followup-email`
- **Rate limiter crash on dashboard bot creation** — Internal ASGI proxy requests had `request.client=None`, crashing `slowapi.get_remote_address()`. Added safe wrapper with `X-Forwarded-For` fallback
- **httpx 0.28+ compatibility** — Replaced removed `AsyncClient(app=...)` shortcut with `httpx.ASGITransport(app=...)` across all 6 proxy routes
- **Bot status polling crash** — `store.list_bots()` returns `(list, total)` tuple but code iterated it as a list. Fixed tuple unpacking
- **Alone detection broken for onepizza** — Empty `_ALONE_TEXTS` made `text_alone` always False; now falls back to tile-only detection for platforms without text patterns
- **DELETE race condition with queued bots** — Removing a queued bot now also cleans up the `_bot_queue` and re-signals the queue processor
- **JS-created bot rows missing attributes** — `_prependBotRow` now adds `data-bot-id`, `bot-status-cell`, `bot-actions-cell` classes, and cancel button so polling/cancel/filter work on newly created rows
- **onepizza.io join button** — Lobby join now tries direct click, JS click fallback, then text-match fallback for robustness
- **Missing DB migration** — Added `ALTER TABLE action_items ADD COLUMN IF NOT EXISTS sub_user_id` to PostgreSQL migration script
- **All proxy routes error handling** — Added try-except to all 6 cookie-auth proxy routes to return 502 instead of crashing

## [2.17.0] - 2026-03-26

### Added
- **Live bot status polling** — Dashboard auto-updates bot status chips every 10 seconds without page refresh (`GET /dashboard/bots/status`)
- **Cancel bot from dashboard** — Cancel button on each active bot row, with `POST /dashboard/bot/{id}/cancel` proxy route
- **Advanced bot options in Send Bot form** — Collapsible section with record video, live transcription, PII redaction, and translation language toggles
- **"See it in action" demo section** on landing page — terminal-style API demo with 3-step walkthrough between How It Works and Pricing

### Fixed
- **Mobile responsiveness** — Dashboard: section headers wrap, bot action buttons stack vertically, advanced options 2-column grid, KPI grids adapt to 2 columns, webhook events grid fits small screens. Landing: demo terminal scrollable and sized for mobile, demo widget full-width on phones
- **Robust alone detection** — `_is_bot_alone()` now requires BOTH text pattern AND DOM tile count to agree before flagging the bot as alone, eliminating false positives from tooltips or loading text
- **Scheduled bots no longer block concurrent slots** — Scheduled bots use deferred `call_later()` timers instead of occupying a `_running_tasks` slot while sleeping; slots are only claimed at join time
- **CORS restricted in production** — When `API_KEY` is set and `CORS_ORIGINS` is still `*`, CORS is now restricted to same-origin only (set `CORS_ORIGINS` explicitly to allow specific origins)

## [2.16.2] - 2026-03-26

### Fixed
- **Bot leaves meeting immediately after joining** — Added 60-second grace period after join before alone-detection activates, preventing false positives from DOM not fully rendering participant tiles

## [2.16.1] - 2026-03-26

### Fixed
- **Critical: Dashboard bot creation auth** — "Send Bot Now" button now works for logged-in users; added `/dashboard/bot` proxy route that accepts cookie auth and forwards to the API with proper Bearer token
- **Critical: XSS in bot table row** — HTML-escape all dynamic values (`meeting_url`, `bot.id`, etc.) in `_prependBotRow` to prevent injection
- **Bot status badge** — Immediate bots now show "Ready" or "Queued" chip instead of always showing "Scheduled"
- **Dead CSS cleanup** — Removed orphaned `.hero-badge` styles from landing page after badge removal

## [2.16.0] - 2026-03-26

### Added
- **Send Bot Now** button in dashboard — logged-in users can send a bot to a meeting immediately from the UI, not just via API
- Toggle between "Send Now" (immediate) and "Schedule for later" modes in the bot creation form

### Changed
- Removed "Live on Zoom · Google Meet · Microsoft Teams" badge from landing page hero section

## [2.15.0] - 2026-03-24

### Changed
- **UI redesign — dark navy + warm beige theme** — Complete visual overhaul inspired by modern SaaS design. Primary color changed from Warm Coral (#E05A33) to Dark Navy (#1B2033). Body background changed to warm pinkish beige (#EDE4DF) with white cards. All buttons, gradients, active states, focus rings, chart colors, brand icons, and avatars updated across all 11 templates.

---

## [2.14.0] - 2026-03-24

### Changed
- **UI color palette overhaul** — Replaced indigo/cyan theme with warm coral palette across all templates (landing, login, register, dashboard, admin, API dashboard, webhook playground, share, bot, topup). Primary color is now Warm Coral (#E05A33), with warm neutral backgrounds, borders, and text colors. Gradients, buttons, badges, form focus states, and chart colors all updated to match.

---

## [2.13.0] - 2026-03-24

### Added
- **ClickHint cursor animations** — Animated cursor click hints on primary CTA buttons across all pages (landing, login, register, dashboard, topup) to guide new users toward key actions. Includes cursor movement, ripple, and glow effects with a 4.5s lifecycle. Hidden on mobile via media query.

---

## [2.12.3] - 2026-03-24

### Fixed
- **Login 500 error — missing DB columns** — `stripe_customer_id` and `stripe_subscription_id` were defined in the Account model but missing from the database migration script, causing every `SELECT` on the accounts table to fail with an `OperationalError` on databases created before v2.11.0
- **OAuth login 500 error** — OAuth callback (Google/Microsoft SSO) imported non-existent `_create_access_token` function instead of `_create_jwt`, causing an `ImportError` on every OAuth login attempt
- **OAuth cookie name mismatch** — OAuth callback set cookie as `access_token` but the dashboard reads from `mb_token`, so OAuth users appeared logged out after redirect

---

## [2.12.2] - 2026-03-22

### Fixed — Performance (Round 6)
- **Dashboard: 7 queries → 5** — Integration and calendar feed queries consolidated (was querying each table twice: once for active, once for all). Derives active subset in Python from single query.
- **Dashboard: OAuth query bounded** — Added `.limit(20)` to OAuthAccount query (was unbounded)
- **Admin: PlatformConfig query bounded** — Added `.limit(500)` (was unbounded)
- **Admin: Chart.js deferred** — Added `defer` to Chart.js CDN script tag in admin.html
- **Background loops: imports hoisted** — Moved `import json`, `from app.services.email_service` out of `while True` loops in main.py retention/digest tasks

---

## [2.12.1] - 2026-03-22

### Added — SDK coverage for new endpoints
- **Python SDK** (sync + async): 3 new methods — `subscribe(plan, success_url?, cancel_url?)`, `get_usage()`, `get_trends(days=30)` with `SubscribeResponse`, `UsageResponse`, `TrendsResponse` models
- **TypeScript SDK**: 3 new methods — `subscribe(params)`, `getUsage()`, `getTrends(days?)` with full type interfaces (`SubscribeParams`, `SubscribeResponse`, `UsageResponse`, `TrendsResponse`)

---

## [2.12.0] - 2026-03-22

### Added — Dashboard UI for Monetization & Trends
- **"Usage & Billing" analytics tab** — New 4th sub-tab in analytics section. Shows: monthly bot usage progress bar (color-coded green/amber/red), plan badge, credits balance, credits spent this month, avg cost per bot, billing reset date, daily usage table. Lazy-loaded from `GET /analytics/usage`.
- **Longitudinal trends in Trends tab** — After loading personal trends, also fetches `GET /analytics/trends?days=30` and displays: top 10 topics across meetings, meetings per day table (last 14 days).
- **Plan upgrade button** — Billing section plan card now shows "Upgrade Plan" button (hidden for Business). Triggers `POST /billing/subscribe` and redirects to Stripe Checkout.

---

## [2.11.0] - 2026-03-22

### Added — Subscriptions, Usage Analytics, Longitudinal Trends
- **Stripe subscription billing** — New `POST /api/v1/billing/subscribe` endpoint creates Stripe Checkout in subscription mode for Starter/Pro/Business plans. Expanded webhook handler processes `invoice.paid` (renew), `checkout.session.completed` mode=subscription (activate plan), `customer.subscription.deleted` (downgrade to free).
- **Account model: Stripe fields** — `stripe_customer_id` and `stripe_subscription_id` columns for linking accounts to Stripe customers.
- **Usage analytics endpoint** — `GET /api/v1/analytics/usage` returns: bots_used, bots_limit, plan, credits_balance, credits_spent_this_month, avg_cost_per_bot, daily_usage chart data.
- **MeetingSummary model** — Permanent lightweight record of each meeting (bot_id, platform, duration, participant_count, sentiment, health_score, topics, ai_cost, word_count). Persisted in `bot_service._do_analysis_inner()` after analysis completes. Survives beyond BotSnapshot's 24h TTL.
- **Longitudinal trends API** — `GET /api/v1/analytics/trends?days=30` returns: meetings_per_day, sentiment_trend, health_trend, top_topics (frequency across all meetings), cost_trend. Powered by the MeetingSummary table.

---

## [2.10.0] - 2026-03-22

### Added — Monetization + CI Pipeline
- **Plan limit enforcement** — Bot creation now checks `monthly_bots_used` against plan limits (Free=5, Starter=50, Pro=500, Business=unlimited). Returns HTTP 402 with upgrade message when limit reached. Uses `SELECT ... FOR UPDATE` to prevent race conditions.
- **Monthly usage counter** — Atomically incremented on each bot creation; hourly background task resets counters for accounts past their `monthly_reset_at` date.
- **Feature gating** — Premium features locked by plan tier via `check_feature()` in deps.py. Calendar auto-join, integrations, translation → Starter+. PII redaction, workspaces, keyword alerts → Pro+. SAML SSO, org analytics → Business.
- **Gated endpoints** — `POST /calendar/feeds` checks `calendar_auto_join`; `POST /bot` checks `translation`, `pii_redaction`, `keyword_alerts` when those options are used.
- **Stripe subscription config** — Added `STRIPE_STARTER_PRICE_ID`, `STRIPE_PRO_PRICE_ID`, `STRIPE_BUSINESS_PRICE_ID` config vars (subscription endpoints coming next).
- **CI pipeline** — GitHub Actions workflow (`.github/workflows/test.yml`) runs 17 pytest tests on every push to main and every PR. Python 3.12, pip caching, 5-minute timeout.
- **pytest config** — `backend/pyproject.toml` with `asyncio_mode=auto` and test markers.

---

## [2.9.5] - 2026-03-22

### Fixed — Final consistency pass (Round 5)
- **Landing page speed** — Added `preconnect` hints for Google Fonts, gstatic, and jsDelivr CDN; deferred Bootstrap JS (was the only template still blocking)
- **Standalone template consistency** — Added `preconnect` hints to all 4 standalone templates (login, register, api_dashboard, webhook_playground) that load fonts without extending base.html

---

## [2.9.4] - 2026-03-22

### Fixed — Final cleanup (Round 4)
- **crypto_service: blocking `requests.post()`** — Fallback RPC test now wrapped in `asyncio.to_thread()` to avoid blocking event loop when httpx is unavailable
- **bot_service: silent SSE exception** — `except Exception: pass` replaced with `logger.debug()` for SSE push setup failures (was invisible in logs)
- **analytics: silent action items query failure** — Now logs a warning instead of silently defaulting to 0 (misleading analytics data)
- **webhook_service: lock eviction race condition** — LRU eviction now skips locks that are currently held (`lock.locked()` check), preventing in-flight delivery corruption

---

## [2.9.3] - 2026-03-22

### Fixed — Performance & Reliability (Round 3)
- **browser_bot.py: async HTML writes** — `_screenshot()` HTML dump now uses `asyncio.to_thread()` instead of blocking `write_text()` (unblocks event loop during 1-5MB writes)
- **browser_bot.py: caption_log memory leak** — Truncation to last 40 entries now runs unconditionally, not just when captions are non-empty (prevented unbounded growth during silent periods)
- **store.py: startup OOM prevention** — `load_persisted_bots()` capped at 10k rows; `load_persisted_webhooks()` filters to `is_active=True` only (was loading entire table)
- **store.py: lock contention** — `_persist_bot()` now builds dict inside lock but does JSON serialization outside (avoids blocking other bot operations during slow serialize of large transcripts)
- **main.py: parallel startup** — Bot restore, webhook restore, and USDC monitor now run concurrently via `asyncio.gather()` (cuts startup time from sequential ~15s to parallel ~5s)

---

## [2.9.2] - 2026-03-22

### Fixed — Production Bugs & Performance (Round 2)
- **Admin analytics crash** — `settings` and `func` now imported at module level (was `NameError` at runtime); initial account queries wrapped in try-except with rollback; bot snapshots query capped at 50k rows (was unlimited — OOM risk)
- **Credit deduction race condition** — `deduct_credits_for_bot()` now uses `SELECT ... FOR UPDATE` to prevent two concurrent bot completions from reading the same balance
- **Claude API timeout** — `messages.stream()` now has `timeout=300s`; `messages.create()` has `timeout=60s` (was indefinite — hung event loop)
- **Integration HTTP client waste** — Linear and Jira integrations now reuse the global `_http_client` instead of creating new `httpx.AsyncClient` per request
- **USDC monitor crash loop** — `_monitor_loop()` now uses exponential backoff (60s → 1h cap) instead of fixed 60s retry on all errors
- **Bootstrap version mismatch** — `share.html` upgraded from 5.3.0 to 5.3.2 (matches all other templates)
- **Render-blocking scripts** — Added `defer` to Bootstrap JS in `login.html` and `register.html`

---

## [2.9.1] - 2026-03-22

### Fixed — Performance & Reliability
- **Memory leak: webhook locks** — `_webhook_locks` dict now uses LRU-bounded `OrderedDict` (max 500 entries) to prevent unbounded growth
- **Race condition: duplicate analysis** — `_analysis_in_flight` check-and-add now protected by `asyncio.Lock` (TOCTOU fix)
- **N+1 query: action items upsert** — Single batch `SELECT ... WHERE hash IN (...)` replaces per-item query loop
- **N+1 query: retention policy enforcement** — Pre-loads all per-account policies in one query; batch-deletes expired snapshots in single `DELETE ... WHERE id IN (...)`
- **Missing DB index** — Added `index=True` on `Webhook.is_active` (queried in admin analytics aggregations)
- **Store.list_bots() lock contention** — Filtering now happens inside the lock to avoid copying unneeded bots to a snapshot list
- **Missing timeout: Gemini transcription** — Added 5-minute `asyncio.wait_for` safety net on `generate_content_async()`
- **Missing timeout: SMTP email** — Added 30-second timeout on `asyncio.to_thread(_send)` to prevent indefinite hangs
- **Page load speed: base.html** — Added `preconnect` hints for Google Fonts and jsDelivr CDN; deferred Bootstrap JS loading
- **Admin email parsing** — Cached parsed `ADMIN_EMAILS` set instead of re-parsing on every admin request

---

## [2.9.0] - 2026-03-22

### Added
- **Python SDK: ~50 new methods** covering all API endpoints — bots (transcript, analyze, ask, highlights, share, follow-up email, rename speakers), webhooks (events list, test delivery), auth (get_me, test keys, account management), templates, analytics (dashboard, recurring, API usage, personal, search, audit log), action items, keyword alerts, calendar feeds, integrations, workspaces (full CRUD + member management), retention policies, and MCP tools
- **TypeScript SDK: ~50 new methods** mirroring all Python SDK additions with full type safety — new interfaces for all response types, param types, and camelCase method names
- **Workspace management UI** in the dashboard — replaces "coming soon" placeholder with full list view, create form, member management panel with add/remove/role-change, and permission-aware actions (owner vs admin vs member vs viewer)
- **Test infrastructure** — pytest + pytest-asyncio setup with shared fixtures (in-memory SQLite, app instance, authenticated client), 17 smoke tests across health, auth, bots, and webhooks

---

## [2.8.0] - 2026-03-22

### Added
- **Landing page mode switcher** — pill-style toggle in the hero section lets visitors choose between "For Teams" (calendar-first, dashboard-focused) and "For Developers" (API-first, code-focused) experiences
- Teams mode: simplified hero messaging, calendar auto-join flow, dashboard preview widget, UI-focused "How It Works" steps, and team-oriented CTA
- Developers mode: API-focused hero with curl quickstart widget, SDK/WebSocket/MCP platform pills, code-first "How It Works" steps, and developer-oriented CTA
- Mode preference persists via localStorage across visits
- Smooth fade-in transitions between modes

---

## [2.7.0] - 2026-03-21

### Fixed
- **OnePizza bot: complete rewrite of join + admission + alone detection** —
  - `_join_onepizza()`: Simplified from 100+ lines to 40. Uses meetingservice's auto-join (`?name=` URL param triggers automatic `joinMeeting()` call). Falls back to manual lobby flow only if auto-join doesn't fire.
  - `_wait_for_admission()`: Now checks `#meetingRoom` visible + `#lobby` hidden (the definitive meetingservice signals), instead of checking `#leaveBtn` + `#waitingRoomOverlay` which didn't match the actual DOM.
  - `_is_bot_alone()`: Now counts `#videoGrid > div` children instead of `.video-tile:not(.is-local)` which didn't match meetingservice's actual CSS classes.
  - `_END_TEXTS["onepizza"]`: Added "the meeting has ended" for completeness.
  - Mic/camera controls: Now uses in-call controls (`#micBtn`, `#camBtn`) instead of lobby controls, since auto-join skips the lobby.

### Added
- **INTEGRATION_GUIDE.md** — Complete guide for 1tab and other consumers: bot creation, webhook-driven status tracking (not polling), supported platforms, error handling, lifecycle documentation. Highlights that polling with 404 loops forever after server restarts.

---

## [2.6.0] - 2026-03-21

### Fixed
- **Admin analytics: 3 critical bugs** —
  1. `func.strftime` → `cast(col, Date)` — strftime is SQLite-only, doesn't exist in PostgreSQL (Railway production DB). Now uses SQLAlchemy `cast(col, Date)` which works on both.
  2. `NameError: settings` → `from app.config import settings as _settings` — settings wasn't imported in the analytics function scope
  3. `InFailedSQLTransactionError` cascade — Added `await db.rollback()` in each except block so a failed billing query doesn't corrupt the DB transaction for subsequent webhook/action-item queries

---

## [2.5.9] - 2026-03-21

### Fixed
- **OnePizza lobby-skip handling** — When the room is already active, the page skips the lobby (`#lobby` hidden) and shows the meeting room (`#meetingRoom`) directly. Bot now detects both states: if `#meetingRoom` or `#videoGrid` is visible, skip lobby flow entirely. Prevents the 30s timeout on invisible `#lobbyName` that was causing join failures on active rooms.

---

## [2.5.8] - 2026-03-21

### Fixed
- **OnePizza join button disabled** — The lobby disables the join button until a name is entered; the `?name=` URL param doesn't always auto-populate. Now force-fills the name field, waits 0.5s for UI to react, and retries up to 3x if button is disabled (re-filling name each time). Added logging at every step for diagnostics.

---

## [2.5.7] - 2026-03-21

### Fixed
- **OnePizza bot join — Socket.IO compatibility** — `networkidle` never resolves on Socket.IO pages (WebSocket keeps connection open); switched to `load` + explicit `wait_for_selector("#lobby, #lobbyJoinBtn, #lobbyName", state="visible")` with 30s timeout; join button now waits for visibility before clicking

---

## [2.5.6] - 2026-03-21

### Fixed
- **OnePizza bot join failure** — Changed `page.goto` from `domcontentloaded` to `networkidle` to wait for SPA JavaScript to render lobby elements; increased lobby selector timeout from 15s to 20s; increased join button click timeout from 4s to 10s; added extra selectors (`button:has-text('join')`, `[data-action='join']`)

---

## [2.5.5] - 2026-03-21

### Fixed
- **OnePizza platform detection** — Added `meetingservice-production.up.railway.app` to the onepizza netloc set so bots correctly identify the platform instead of falling back to "unknown" demo mode

---

## [2.5.4] - 2026-03-20

### Fixed
- **Admin analytics error visibility** — Proxy endpoint now returns the actual exception type and message instead of generic "Internal Server Error", enabling debugging

---

## [2.5.3] - 2026-03-20

### Fixed
- **Admin analytics crash** — `func.date()` is PostgreSQL-only; replaced with `func.strftime('%Y-%m-%d', ...)` for SQLite compatibility
- **Graceful degradation** — All new analytics queries (billing, webhooks, action items) wrapped in try/except so individual query failures don't crash the entire analytics endpoint

---

## [2.5.2] - 2026-03-20

### Fixed
- **Landing page**: Renamed misleading `--dark` CSS variables to semantic `--bg`, `--bg-alt`, `--bg-muted` names — all resolve to white/light gray, eliminating any confusion about theme
- **Dashboard analytics consolidated**: Replaced bare 4-KPI section + external `/api-dashboard` links with 3 tabbed sub-sections (Overview, API Usage, Trends) that load data inline via existing REST endpoints
- **Removed external link fragmentation**: No more links to separate `/api-dashboard` page — all analytics data is now accessible within the dashboard's own Analytics tab

### Changed
- **Dashboard Analytics → Overview tab**: Monthly bots, integrations, calendar feeds, API keys + recent bot performance table (server-rendered)
- **Dashboard Analytics → API Usage tab**: Bots 7d/30d, tokens, cost, error rate, platform breakdown, tokens by operation (lazy-loaded from `/api/v1/analytics/api-usage`)
- **Dashboard Analytics → Trends tab**: Monthly meetings, AI cost, action items, avg duration, sentiment trend, cost by platform (lazy-loaded from `/api/v1/analytics/me`)

---

## [2.5.1] - 2026-03-20

### Changed
- **Analytics reorganized with tabbed sub-navigation** — All analytics data consolidated into 5 clear tabs: Overview, AI & Costs, Errors & Health, Features, Users
- **Admin Overview slimmed** — Removed duplicated Platform Features card and Bot Breakdown (same data now lives in Analytics tabs)
- **System Status moved** — Runtime metrics (running tasks, queue depth) relocated from Analytics to the System tab where they belong
- **Chart.js resize fix** — Charts properly resize when switching between hidden/visible tabs

---

## [2.5.0] - 2026-03-20

### Changed
- **Modern light theme UI redesign** — Complete visual overhaul across all 8 user-facing templates:
  - **base.html** — New CSS variables, frosted glass navbar (white + backdrop blur), mobile hamburger menu, softer shadows and borders
  - **landing.html** — Converted from full dark (#0a0f1e) to clean white/light gray with subtle indigo accents
  - **login.html & register.html** — Dark left panels replaced with soft indigo gradient, dark text
  - **admin.html** — Dark navy sidebar converted to white with light borders and indigo active states
  - **dashboard.html** — Sidebar polish, horizontal scrollable pill nav on mobile, sticky positioning
  - **webhook_playground.html & api_dashboard.html** — Light navbar and background
- **Mobile responsiveness** — Added hamburger menu to all pages, horizontal scrollable sidebar on mobile, proper sticky positioning, tablet breakpoints
- **Design system** — Updated color palette: softer borders (#e5e7eb), subtle shadows, 14px body text, rounded corners (14px cards)

---

## [2.4.0] - 2026-03-20

### Added
- **Comprehensive admin analytics** — Expanded `/admin#analytics` with 6 new visualization sections:
  - **Status & Plan distribution** — Horizontal bar chart of bot statuses + doughnut of plan tiers
  - **Revenue & Billing** — 30-day daily revenue line chart + credit flow breakdown (added/consumed/net by type)
  - **Error analysis** — Errors by platform bar chart + top 10 error messages table
  - **Webhook health** — Delivery success rate, status breakdown, recent failures list
  - **Action items** — Total/open/done counts with completion rate progress bar
  - **Template & transcription** — Template usage table + Gemini vs Whisper doughnut
  - **System status** — Running tasks, queue depth, max concurrent, in-memory bots
- **Expanded KPI grid** — 13 cards (was 8): added Bots (7d), Avg Duration, Error Rate, Revenue (30d), and more
- **Backend analytics API extended** — `platform_analytics()` now returns billing, webhook, action item, error, and system data

---

## [2.3.1] - 2026-03-20

### Fixed
- **RBAC fails closed** — Workspace role check now returns 500 on DB errors instead of silently allowing access
- **Exception details no longer leaked** — Webhook delivery list endpoints return generic error, log details server-side
- **Calendar feed SSRF protection** — iCal URLs validated against private/reserved IPs (reuses webhook `_block_ssrf`)
- **Webhook state race condition** — Per-webhook locking prevents concurrent `dispatch_event` calls from corrupting `consecutive_failures` / `is_active`
- **Action item sub-user isolation** — New `sub_user_id` column + filtering so sub-users only see their own action items
- **Live transcript flush resilience** — Failed flush retries on next entry instead of losing the timestamp
- **SSE push error handling** — Fire-and-forget tasks wrapped in safe handler to prevent silent "Task exception never retrieved" warnings

### Changed
- **Bot queue** — Uses `collections.deque` (O(1) popleft) instead of `list.pop(0)` (O(n))
- **URL parsing** — Recurring meeting intelligence parses meeting URL once instead of 4 times
- **Screenshot pruning** — Runs at session start in addition to session end
- **VERSION file** — Fixed sync (was `2.2.0`, now matches actual version)
- **Pre-commit hook** — `.githooks/pre-commit` warns if VERSION/README/CHANGELOG are stale
- **CLAUDE.md** — Added mandatory pre-commit checklist for version and date updates

---

## [2.3.0] - 2026-03-19

### Added
- **Async dashboard — zero page reloads** — All 10 dashboard actions (API key create/revoke, webhook register, integration add/toggle/delete, calendar feed add/toggle/delete) now use `fetch()` + in-place DOM updates with toast notifications. No full-page reload occurs for any dashboard action.
- **`apiFetch()` helper** — Shared JS utility in `base.html` for all dashboard mutations. Automatically attaches `Content-Type: application/json` and `Accept: application/json` headers; throws on non-2xx responses with the server's `detail` message.
- **Browser back button support** — `switchSection()` now uses `window.history.pushState()` (not `replaceState`). A `popstate` listener restores the correct section when the user presses Back — the browser history stack works fully within the dashboard.
- **Schedule Bot in-place update** — After scheduling a bot, a `<tr>` is inserted into the bots table immediately without calling `window.location.reload()`.
- **Admin endpoint rate limiting** — `PUT /api/v1/admin/wallet`, `PUT /api/v1/admin/rpc-url`, and `POST /api/v1/admin/credit` are now limited to **10 requests/minute per IP**. `POST /api/v1/admin/usdc/rescan` is limited to **5/minute**. Returns HTTP 429 when exceeded.
- **Webhook replay protection** — Signed webhook deliveries now include an `X-MeetingBot-Timestamp` header alongside `X-MeetingBot-Signature`. The HMAC is computed over `{timestamp}.{body}` instead of just `{body}`. Recipients should reject deliveries where `abs(now - timestamp) > 300 seconds`.

### Changed
- **Webhook HMAC format (BREAKING)** — The signed payload is now `f"{timestamp}.{body}"`. Update your HMAC verification to extract `X-MeetingBot-Timestamp`, prepend it to the body, and verify the combined string. The 5-minute replay window is enforced server-side on delivery; recipients are responsible for enforcing it client-side.
- **Bot queue latency: 10 s → near-zero** — The queue processor previously polled every 10 seconds with `asyncio.sleep(10)`. It now wakes immediately via `asyncio.Event` when a bot is enqueued. Slots are filled in under 100 ms.
- **Analytics response caching** — `GET /api/v1/analytics` results are cached for **30 seconds** per account. `GET /api/v1/analytics/api-usage` results are cached for **60 seconds**. Reduces `list_bots()` calls under high-frequency polling.

### Fixed
- **WebSocket DB error now fails explicitly** — Previously, a database error during token lookup would return `None`, which was indistinguishable from an unknown/invalid token. Now returns close code **4503 (Service Temporarily Unavailable)** with reason `"Service temporarily unavailable"` so clients can distinguish a transient DB failure from a bad token.
- **Calendar feed dedup memory leak** — The `_dispatched` set grew unbounded across long-running instances. Changed to a `dict[key, float]` with a 48-hour TTL. A prune sweep runs every 288 poll cycles (~24 h at default 5-minute intervals). Memory is now bounded.
- **Dashboard JSON response branches** — All 10 `POST /dashboard/*` handlers now return `JSONResponse` when the request includes `Accept: application/json`, enabling the new async fetch flow. The redirect-based form flow is preserved for non-JS clients.

---

## [2.2.0] - 2026-03-17

### Added
- **Split API documentation** — public Swagger UI at `/api/docs` exposes only user-facing endpoints (admin-only routes, platform analytics, and `ai_usage` cost fields are hidden). Full schema including all admin endpoints and AI cost data is available at `/api/v1/admin/docs` (admin accounts only)
- **`/bot/{id}` session viewer** — new web UI page showing transcript, AI analysis (summary, key points, action items, decisions, next steps, sentiment, topics), speaker breakdown, chapters, meeting metadata, and download links for audio/video/markdown/PDF
- **`GET /api/v1/templates/default-prompt`** — returns the raw default analysis prompt so callers can inspect or extend it before passing `prompt_override`
- **`GET /api/v1/search`** — full-text search across all transcripts in memory; query param `q` returns matching snippets with bot context
- **Modern landing page** — public marketing homepage at `/` replacing the previous redirect; shows features, quick-start examples, and sign-up CTA. Authenticated users are auto-redirected to `/dashboard`
- **Dashboard redesign** — full account management in the dashboard: API key copy-to-clipboard, integrations (Slack/Notion) add/pause/delete, calendar feed add/pause/remove, notification preferences, and recent bots overview — all without leaving the page

### Fixed
- **Startup hang fix** — asyncpg now uses a 10 s connection timeout; `create_all_tables()`, `load_persisted_bots()`, and `load_persisted_webhooks()` wrapped in `asyncio.wait_for()` so the server always becomes ready (and `/health` always responds) even when the database is temporarily unavailable at boot
- **DB startup retry** — `create_all_tables()` is retried up to 5 times with a 5 s delay between attempts (handles Railway where the PostgreSQL container starts in parallel with the app container)
- **PostgreSQL migration compatibility** — schema migration `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` now uses a `try/except` guard compatible with older asyncpg/PostgreSQL versions

## [2.2.0] - 2026-03-16

### Added
- **Business accounts** — new `account_type` field (`personal` or `business`) for platforms integrating MeetingBot on behalf of multiple end-users. Business accounts use a single API key and credit balance but can completely isolate data between end-users via the `X-Sub-User` header
- **Sub-user data isolation** — pass `X-Sub-User: <user-id>` header (or `sub_user_id` in bot creation body) to scope all bot data to a specific end-user. Users cannot see each other's bots, transcripts, or analyses. Omit the header for an account-wide view
- **Copy-to-clipboard for API keys** — clipboard icon beside each API key in the dashboard, with visual feedback on copy. Newly created keys show the full key once with a prominent copy button
- Account type selection on the registration page (Personal / Business)
- Business account info card on the dashboard with integration examples
- Account type column in the admin panel's user accounts table
- `sub_user_id` field in bot creation, bot response, and bot summary schemas

### Changed
- `POST /api/v1/auth/register` now accepts `account_type` field (`personal` | `business`)
- `GET /api/v1/auth/me` now returns `account_type` in the response
- All bot endpoints (`GET`, `DELETE`, transcript, recording, analyze, ask, highlight, follow-up email) now respect `X-Sub-User` header for data isolation
- Bot list and stats endpoints filter by `sub_user_id` when the header is present

---

## [2.1.0] - 2026-03-16

### Added
- **Admin interface** — platform administration panel at `/admin` (web UI) and `/api/v1/admin/*` (API), restricted to admin accounts only
- **Platform USDC collection wallet** — admins can set/change a single Ethereum wallet address where all users send USDC via `PUT /api/v1/admin/wallet` or the admin web UI
- **User wallet registration** — users register their Ethereum wallet on their account (`PUT /api/v1/auth/wallet`). The USDC monitor matches the `from` address of incoming transfers to the platform wallet against registered user wallets, automatically crediting the correct account
- **Admin API endpoints:** `GET /api/v1/admin/wallet`, `PUT /api/v1/admin/wallet`, `GET /api/v1/admin/config`
- **User wallet endpoints:** `GET /api/v1/auth/wallet`, `PUT /api/v1/auth/wallet`
- **Admin access control** — only `assad.dar@gmail.com` or accounts with `is_admin=true` can access admin endpoints and the `/admin` page; all others receive HTTP 403
- `is_admin` and `wallet_address` fields on Account model
- `PlatformConfig` database model for storing platform-level key/value settings
- Admin nav link (visible only to admin users) in the web UI navbar
- Wallet registration card on the user dashboard
- Wallet status shown on the top-up page with warnings if not registered

### Changed
- `GET /api/v1/billing/usdc/address` now returns the admin-configured platform wallet when set (with the user's registered wallet info), falling back to HD-derived per-user addresses
- USDC transfer monitor now supports two modes: platform wallet (matches `from` address to user wallets) and HD wallet (matches `to` address to per-user deposit addresses)
- Top-up page (`/topup`) shows the platform wallet when configured by an admin, with user wallet status
- `CRYPTO_HD_SEED` is no longer required for USDC deposits if a platform wallet is set via the admin panel
- `GET /api/v1/auth/me` now includes `wallet_address` in the response

---

## [1.5.1] - 2026-03-14

### Fixed
- Silent audio capture: disabled out-of-process audio service, corrected PulseAudio sink volume, fixed VAD streaming loop reliability
- Caption scraping failure and audio silence in Google Meet sessions
- VAD streaming loop now always runs when Gemini is available
- Removed blocking `socket.getaddrinfo()` DNS lookup from `WebhookCreate` Pydantic validator — the synchronous call was blocking the async event loop and raising "Network is unreachable" when DNS was unavailable (same fix previously applied to the bot URL validator)
- `POST /api/v1/bot` now returns HTTP 503 with a clear diagnostic message when the database is unreachable (e.g. misconfigured `DATABASE_URL` or Supabase credentials), instead of the opaque "Database error: [Errno 101] Network is unreachable"

---

## [1.4.0] - 2026-03-07

### Added
- **AI usage tracking** — every bot response now includes a full `ai_usage` breakdown: tokens, cost, provider, model, and per-operation timing
- **Stripe billing** — flat per-meeting fees, per-token usage billing, and a cost-markup multiplier; checkout and subscription endpoints added
- **Claude API integration** — `ANTHROPIC_API_KEY` enables `claude-opus-4-6` for meeting analysis; takes precedence over Gemini when both keys are set
- `GET /api/v1/billing/usage` — aggregated AI usage across all meetings
- `GET /api/v1/billing/meeting/{bot_id}` — per-meeting charge breakdown
- `POST /api/v1/billing/checkout` — Stripe one-time payment checkout
- `POST /api/v1/billing/subscribe` — Stripe metered subscription checkout
- `POST /api/v1/billing/webhook` — Stripe webhook handler

### Changed
- Frontend served from the correct `FRONTEND_DIR` path
- All new billing and usage panels added to the web UI

---

## [1.3.0] - 2026-02-28

### Added
- **Third-party integrations** — Slack, Notion, Linear, Jira, HubSpot post-meeting push
- **PDF and Markdown export** — `GET /api/v1/bot/{id}/export/pdf` and `/export/markdown`
- **Speaker profiles** — auto-created after each meeting; cross-meeting stats (talk time, meeting count, questions asked); CRUD endpoints under `/api/v1/speakers`
- **Bot queue** — `MAX_CONCURRENT_BOTS` (default 3) limits simultaneous bots; extras queue and start automatically when a slot opens
- **AI tools** — follow-up email draft (`POST /api/v1/bot/{id}/followup-email`), pre-meeting brief (`POST /api/v1/bot/{id}/brief`), recurring meeting intelligence (`GET /api/v1/bot/{id}/recurring`)
- **Ask Anything** — `POST /api/v1/bot/{id}/ask` for free-form transcript Q&A
- **Share links** — unique `share_token` per bot; public read-only report at `GET /api/v1/share/{token}`
- **Recording download** — `GET /api/v1/bot/{id}/recording` to retrieve raw WAV audio
- Hardened security: SSRF DNS checks on webhook URLs and meeting URLs, LIKE-escape injection fix, parallel webhook broadcasts

### Fixed
- SQLite-incompatible pool args removed from async engine
- SMTP calls moved to thread pool to avoid event loop freeze
- DB indexes added for status, `created_at`, `meeting_url`, `share_token`

---

## [1.2.0] - 2026-02-14

### Added
- **Weekly digest email** — sent every Monday 09:00 UTC; requires `SMTP_HOST` and `DIGEST_EMAIL`
- **Recording retention** — auto-deletes WAV files older than `RECORDING_RETENTION_DAYS` (default 30) via daily background job at 03:00 UTC
- **Calendar auto-join** — iCal feed polled every 5 min; set `CALENDAR_ICAL_URL` to auto-dispatch bots to upcoming meetings
- **APScheduler** — background job scheduler managing digest, cleanup, and calendar sync tasks
- **10 built-in meeting templates** — Default, Sales Call, Daily Standup, 1:1, Sprint Retro, Client Kickoff, All-Hands, Incident Post-Mortem, Interview/Hiring, Design Review
- **Customized template** — `seed-customized` + `prompt_override` for inline one-off prompts without saving a template
- `GET /api/v1/templates/default-prompt` — returns the raw default analysis prompt as a starting point
- **Action item tracking** — cross-meeting action items stored in DB; `GET /api/v1/action-items`, `PATCH` to update, `GET /api/v1/action-items/stats`
- **Full-text search** — `GET /api/v1/search?q=` across all transcripts with highlighted snippets
- **Analytics** — `GET /api/v1/analytics` returns sentiment distribution, meetings per day, top topics, top participants, platform breakdown
- **Highlights** — bookmark transcript moments via `POST/GET/DELETE /api/v1/bot/{id}/highlight`
- **Mobile-responsive UI** — hamburger sidebar, full mobile layout

### Changed
- Parallel AI analysis pipeline (summary, action items, chapters run concurrently)
- Faster transcription with reduced latency
- UI auto-polls for bot status updates

### Fixed
- Mobile sidebar hide/show with `display:none` and fixed overlay
- Custom radio pickers in Deploy Bot modal
- Mode pill selection reliability

---

## [1.1.0] - 2026-01-31

### Added
- **Gemini Live API** — real-time bidirectional audio streaming using `google-genai>=1.0.0`
- **Live transcription** — `live_transcription: true` transcribes audio in 15-second rolling chunks during the call; enables voice-based bot-name detection without DOM captions
- **Voice mention responses** — `respond_on_mention`, `mention_response_mode` (`text` / `voice` / `both`), `tts_provider` (`edge` / `gemini`)
- **Microsoft Edge TTS** (`edge-tts`) — fast (~300 ms) voice replies with no extra API key
- **Gemini TTS** — more natural voice via `gemini-2.5-flash-preview-tts`
- **`start_muted`** — controls whether the bot joins with its microphone muted
- Bot join retry logic — `BOT_JOIN_MAX_RETRIES` and `BOT_JOIN_RETRY_DELAY_S`
- `cancelled` bot status — `DELETE /api/v1/bot/{id}` triggers graceful shutdown with background transcript + analysis
- Debug screenshots — `GET /api/v1/debug/screenshots` to inspect join failures
- WebSocket real-time events at `ws://localhost:8080/ws`
- Bearer token auth via `API_KEY` environment variable
- `extra_metadata` arbitrary JSON field on bots

### Fixed
- Gemini Live session invalid argument (error 1007)
- Live transcription audio overlap and real-time frontend display
- Caption detection reliability improvements
- Railway deployment: nixpacks.toml, Procfile, Dockerfile auto-detection

---

## [1.0.0] - 2026-01-15

### Added
- Initial release
- Playwright-based browser bot joins **Google Meet**, **Zoom**, and **Microsoft Teams** as a guest
- ffmpeg + PulseAudio audio capture
- Gemini transcription and AI analysis (summary, key points, action items, decisions, next steps, sentiment, topics, chapters, speaker stats)
- `analysis_mode: "transcript_only"` to skip AI analysis and return raw transcript only
- Bot lifecycle: `joining` → `in_call` → `call_ended` → `done` / `error`
- REST API at `/api/v1` with Swagger UI at `/api/docs`
- `POST /api/v1/bot` — create bot (join meeting)
- `GET /api/v1/bot/{id}` — get bot status, transcript, and analysis
- `GET /api/v1/bot/{id}/transcript` — transcript only
- `POST /api/v1/bot/{id}/analyze` — re-run analysis on demand
- `GET /api/v1/bot` — list bots with status filter
- `DELETE /api/v1/bot/{id}` — stop bot
- `GET /api/v1/bot/stats` — aggregate statistics
- `POST/GET/DELETE /api/v1/webhook` — webhook registration and delivery
- `POST /api/v1/webhook/{id}/test` — test webhook endpoint
- CORS support with `CORS_ORIGINS`
- Docker Compose deployment
- Railway deployment config
- SQLite (WAL mode) with SQLAlchemy async
- `vocabulary` field for transcription hints
- SSRF protection on meeting URLs (blocks private/loopback ranges)
- Web UI with Reports, Search, Action Items, Templates, Analytics, Webhooks, Debug, Speakers tabs
