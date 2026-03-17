# Changelog

All notable changes to MeetingBot are documented here.

Format: `## [version] - YYYY-MM-DD` followed by categorised bullet points.

> **Latest version:** 2.2.0 — **Last updated:** 2026-03-17

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
