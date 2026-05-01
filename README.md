# JustHereToListen.io API

**Version 2.44.0** — A stateless meeting bot API service with multi-tenant billing, business account support, Google/Microsoft SSO, Python & JS SDKs, webhook retry/delivery logs, bot persona customization, video recording, Prometheus metrics, idempotency keys, cloud storage, email notifications, calendar auto-join, Slack/Notion integrations, and GDPR compliance.

> **Last updated:** 2026-05-01 · **API version in Swagger UI:** 2.44.0 · **Build:** Round-4 security & correctness pass — closed an unauthenticated bot-access path, scoped idempotency keys to the authenticated tenant, hardened CORS lockdown to trigger on `ENVIRONMENT=production`, shipped `verify_webhook` helpers in both SDKs, and fixed several concurrency races in the bot queue and webhook lock LRU. <!-- auto-updated on each release -->


Send bots into **Zoom**, **Google Meet**, **Microsoft Teams**, and **onepizza.io** meetings to record, transcribe, and analyse them with **Claude** (Anthropic) or **Gemini** (Google) AI.

**Multi-tenant:** Each external service registers an account and gets its own API key. Pre-fund a credit balance via Stripe (card) or USDC (ERC-20) — credits are deducted automatically per bot run.

---

## Recent changes (2026-04-16)

### Interactive bot — voice + chat, both ways (v2.34.0)
- **Unified live transcript** — every voice utterance AND every chat message now flows through the same `transcript` array with a new `source: "voice" | "chat"` field. Chat messages are captured at 4 Hz from the meeting chat panel on Google Meet / Zoom / Teams / onepizza, deduped by stable per-line hash, and persisted alongside voice entries.
- **`POST /api/v1/bot/{id}/say`** — make the bot speak arbitrary text in the live meeting. Body: `{text, voice?: "gemini"|"edge", interrupt?: bool}`. Default voice is **Gemini TTS** for natural sound. Returns 202 immediately; concurrent calls queue behind a per-bot lock so audio never overlaps; `interrupt:true` cancels in-flight speech.
- **`POST /api/v1/bot/{id}/chat`** — make the bot post arbitrary text into the meeting chat panel. Body: `{text}`. Same 202 + per-bot-lock pattern.
- **New webhook event `bot.live_chat_message`** — fires for every captured chat message; payload mirrors `bot.live_transcript` but with `source: "chat"`.
- See [Driving the Bot Mid-Meeting](INTEGRATION_GUIDE.md#driving-the-bot-mid-meeting-v2340) in the integration guide for end-to-end examples.

### Integration improvements (v2.20.x)
- **`POST /api/v1/bot/validate-meeting-url`** — Pre-flight endpoint to check URL validity, detect platform, and report whether real recording is supported. Use before `POST /bot` to fast-fail on unsupported URLs.
- **Machine-readable error responses** — All HTTP errors now include `error_code` and `retryable` fields alongside `detail`, making programmatic error handling easier.
- **Webhook payload enrichment** — `bot.error` events now include `error_code`, `error_message`, and `retryable` for automated retry decisions.
- **Meeting URL normalisation** — Personalisation query params (`name`, `displayName`, `email`) are stripped from meeting URLs before joining.
- **Semantic search** — `GET /api/v1/search?semantic=true` uses embedding cosine similarity to rank meetings by relevance.

### UI/UX (v2.21.x)
- **Dark mode** — System-wide dark theme on all pages. Respects OS `prefers-color-scheme`, persists in localStorage, toggle in navbar.
- **Mobile responsive** — Dashboard sidebar collapses to bottom nav, login/register stacks vertically, navbar scales on small screens, KPI cards reflow to single column on phones.
- **Visual polish** — Page fade-in animations, card hover lift, pulse on in-progress statuses, loading skeleton CSS, focus-visible keyboard outlines.
- **UX flows** — Password visibility toggle, loading spinner on form submit, reusable confirmation dialog, animated progress bar for in-progress bots.

### Security hardening (v2.20.x–2.21.x)
- 20 bugs fixed: OAuth CSRF bypass, double credit deduction, HTML injection in emails, `asyncio.shield` preventing bot cancellation, webhook race conditions, AI cost tracking, credit addition race condition, OAuth URL encoding, and more.
- Rate limits on AI endpoints (`/analyze`, `/ask`, `/ask-live`, `/followup-email`).
- Bounded `vocabulary` (max 100) and `keyword_alerts` (max 50) arrays to prevent DoS.
- See [CHANGELOG.md](CHANGELOG.md) for the complete list.

### Previous (2026-03-26)

### Dashboard — Send bots directly from the UI
- **Send Bot Now** — Primary CTA button on the dashboard. Enter a meeting URL and the bot joins immediately. Toggle "Schedule for later" to pick a future date/time.
- **Advanced bot options** — Collapsible section with record video, live transcription, PII redaction, and translation language toggles.
- **Live status polling** — Bot status chips update automatically every 10 seconds without page refresh.
- **Cancel bot from dashboard** — Cancel button on each active bot row. Works for scheduled, queued, and running bots.
- **Bot search & filter** — Search by ID or URL, filter by platform (Zoom/Teams/Meet/onepizza).
- **Meeting History tab** — Browse all past meetings from the database (beyond the 24-hour in-memory window). Shows URL, platform, status, duration, participants, and whether transcript/analysis are available.

### Bot detail page — Productivity features
- **Transcript search** — Real-time filter and highlight with match counter. Type to find any word across the transcript.
- **"Ask about this meeting"** — AI-powered Q&A on any completed meeting. Ask free-form questions and get answers with context.
- **"Generate follow-up email"** — One-click AI follow-up email generation with copy-to-clipboard.
- **DB fallback for past meetings** — Bot detail page (`/bot/{id}`) loads from the database when the bot has expired from the 24-hour in-memory window. Users can view transcripts and analysis from any historical meeting.

### Reliability
- **Robust alone detection** — `_is_bot_alone()` now requires BOTH text pattern AND DOM tile count to agree before flagging the bot as alone, eliminating false positive exits.
- **60-second post-join grace period** — Alone detection is skipped for the first 60 seconds after joining, preventing false exits from DOM not fully rendering.
- **Scheduled bots don't block concurrent slots** — Scheduled bots use deferred `call_later()` timers instead of occupying a `_running_tasks` slot while sleeping.
- **Queue processor re-signals** — When a bot finishes, the queue processor is woken immediately to start the next queued bot (no 30-second delay).

### Security
- **Cookie-auth proxy routes** — All dashboard API calls (bot creation, cancel, share, speakers, ask, email) route through `/dashboard/*` proxy endpoints that accept the JWT cookie and forward with proper Bearer token.
- **XSS prevention** — All dynamic values in bot table rows are HTML-escaped via `_escHtml()`.
- **CORS restricted in production** — When `API_KEY` is set and `CORS_ORIGINS` is still `*`, CORS auto-restricts to same-origin.

### Landing page
- **Dark navy + warm beige theme** — Complete visual overhaul across all 11 templates.
- **"See it in action" demo section** — Terminal-style API walkthrough with 3-step visual guide.
- **Mobile responsive** — All new features render properly on phones and tablets.

---

## How it works

1. **POST** your meeting URL (+ optional `webhook_url`) to `/api/v1/bot`
2. A headless Chromium browser joins the call, records audio, and transcribes it
3. AI analyses the transcript (summary, action items, decisions, sentiment, topics, chapters)
4. Full results are **POSTed to your `webhook_url`** when done, or you poll `GET /api/v1/bot/{id}`
5. Results stay in memory for **24 hours** — save them to your own storage before then

---

## Quick start

### 1. Run with Docker Compose

```bash
git clone <repo>
cd MeetingBot

# Set your API keys
export ANTHROPIC_API_KEY=sk-ant-...   # or GEMINI_API_KEY
export STRIPE_SECRET_KEY=sk_live_...  # for card payments
export CRYPTO_HD_SEED=<64-char hex>   # for USDC payments (optional — admin can also set wallet via UI)

docker compose up
```

API available at `http://localhost:8000`
Interactive docs at `http://localhost:8000/api/docs` (public endpoints only)
Admin API docs at `http://localhost:8000/api/v1/admin/docs` (admin accounts only)
Web UI at `http://localhost:8000/register`
Admin panel at `http://localhost:8000/admin` (admin accounts only)

### 2. Register an account and get an API key

```bash
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com", "password": "yourpassword", "key_name": "Default"}'
```

Response:
```json
{
  "account_id": "550e8400-e29b-41d4-a716-446655440000",
  "email": "you@example.com",
  "account_type": "personal",
  "api_key": "sk_live_<40-char-token>",
  "message": "Account created. Use the api_key as your Bearer token: Authorization: Bearer <api_key>"
}
```

> **`key_name`** (optional, default `"Default"`) — a label for the first API key created with your account.
>
> **`account_type`** (optional, default `"personal"`) — set to `"business"` if you are a platform integrating MeetingBot for multiple end-users. See [Business accounts](#business-accounts-multi-user-data-isolation) below.

**API key format:** All keys are prefixed with `sk_live_` followed by 40 URL-safe characters (e.g. `sk_live_AbCdEfGh...`). The full key is shown **once** at creation — copy it immediately. Subsequent `GET /api/v1/auth/keys` calls return only a preview of the first 16 characters.

### Business accounts (multi-user data isolation)

Business accounts are designed for **platforms that integrate MeetingBot on behalf of multiple end-users**. A single business account uses one API key and one credit balance, but can completely isolate data between end-users so they never see each other's bots, transcripts, or analyses.

#### How it works

1. **Register a business account:**
```bash
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "platform@example.com", "password": "yourpassword", "account_type": "business"}'
```

Response includes `"account_type": "business"` and a reminder to use the `X-Sub-User` header:
```json
{
  "account_id": "...",
  "email": "platform@example.com",
  "account_type": "business",
  "api_key": "sk_live_...",
  "message": "Account created. Use the api_key as your Bearer token ... — Business account: pass X-Sub-User header with each request to isolate data per end-user."
}
```

2. **Pass `X-Sub-User` header on every request** to scope data to a specific end-user:
```bash
# Create a bot for user "alice"
curl -X POST http://localhost:8000/api/v1/bot \
  -H "Authorization: Bearer sk_live_..." \
  -H "X-Sub-User: alice" \
  -H "Content-Type: application/json" \
  -d '{"meeting_url": "https://meet.google.com/abc-defg-hij"}'

# List only alice's bots — bob's bots are not visible
curl http://localhost:8000/api/v1/bot \
  -H "Authorization: Bearer sk_live_..." \
  -H "X-Sub-User: alice"

# List only bob's bots — alice's bots are not visible
curl http://localhost:8000/api/v1/bot \
  -H "Authorization: Bearer sk_live_..." \
  -H "X-Sub-User: bob"
```

3. **Omit `X-Sub-User` for an account-wide view** of all bots across all sub-users (platform/admin view).

#### Key points

- **`X-Sub-User`** is an opaque string (max 255 chars) — use any identifier: user ID, email, UUID, etc.
- The header applies to **all bot endpoints**: create, list, get, delete, transcript, recording, analyze, ask, highlight, follow-up email.
- **Alternatively**, pass `sub_user_id` in the `POST /api/v1/bot` request body instead of (or in addition to) the header. The body field takes precedence over the header.
- Credits are shared across all sub-users under the business account — billing is at the account level.
- When `X-Sub-User` is omitted on a business account, the API returns all bots regardless of sub-user (useful for platform-level dashboards).
- Personal accounts can also use `X-Sub-User` for organisational purposes, but it is designed primarily for business accounts.

#### Full business account workflow

```bash
# Step 1: Register platform account
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "platform@acme.com", "password": "secure-pass", "account_type": "business"}'
# → save the returned api_key

# Step 2: Add credits (Stripe)
curl -X POST http://localhost:8000/api/v1/billing/stripe/checkout \
  -H "Authorization: Bearer sk_live_<platform-key>" \
  -H "Content-Type: application/json" \
  -d '{"amount_usd": 50, "success_url": "https://acme.com/billing/ok"}'
# → open the returned checkout_url to complete payment

# Step 3: Send bots on behalf of your users
curl -X POST http://localhost:8000/api/v1/bot \
  -H "Authorization: Bearer sk_live_<platform-key>" \
  -H "X-Sub-User: user_abc123" \
  -H "Content-Type: application/json" \
  -d '{"meeting_url": "https://zoom.us/j/...", "webhook_url": "https://acme.com/hooks/meeting"}'

# Step 4: Retrieve results for that user only
curl http://localhost:8000/api/v1/bot/<bot-id> \
  -H "Authorization: Bearer sk_live_<platform-key>" \
  -H "X-Sub-User: user_abc123"

# Step 5: Check platform-wide balance and usage
curl http://localhost:8000/api/v1/billing/balance \
  -H "Authorization: Bearer sk_live_<platform-key>"
```

### 3. Register your USDC wallet (for crypto top-ups)

```bash
# Register the Ethereum wallet you'll send USDC from
curl -X PUT http://localhost:8000/api/v1/auth/wallet \
  -H "Authorization: Bearer sk_live_..." \
  -H "Content-Type: application/json" \
  -d '{"wallet_address": "0xYourEthereumWalletAddress..."}'
```

### 4. Top up credits

```bash
# Via Stripe — returns a checkout URL to complete payment
# amount_usd must be one of the values in STRIPE_TOP_UP_AMOUNTS (default: 10, 25, 50, 100)
curl -X POST http://localhost:8000/api/v1/billing/stripe/checkout \
  -H "Authorization: Bearer sk_live_..." \
  -H "Content-Type: application/json" \
  -d '{"amount_usd": 25, "success_url": "https://your-app.com/thanks", "cancel_url": "https://your-app.com/topup"}'

# Via USDC — get the platform USDC deposit address (1 USDC = $1 credit)
curl http://localhost:8000/api/v1/billing/usdc/address \
  -H "Authorization: Bearer sk_live_..."
```

Or use the web UI at `/topup`.

> **USDC deposits — how attribution works:** Each user registers their Ethereum wallet on their account (`PUT /api/v1/auth/wallet`). The admin sets a single platform collection wallet via `/admin`. When a user sends USDC to the platform wallet, the system matches the `from` address to the user's registered wallet and credits their account automatically (within ~1 minute of on-chain confirmation). If no platform wallet is configured, users get per-user HD-derived addresses (requires `CRYPTO_HD_SEED`).
>
> **Important:** In platform wallet mode, users **must register their wallet before sending USDC**. Transfers from an unregistered address are recorded in the `unmatched_usdc_transfers` table and logged as warnings, but are not credited automatically. Admins can view these via `GET /api/v1/admin/usdc/unmatched` and use `POST /api/v1/admin/credit` to apply the funds manually.

### 5. Create a bot

```bash
curl -X POST http://localhost:8000/api/v1/bot \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk_live_..." \
  -d '{
    "meeting_url": "https://meet.google.com/abc-defg-hij",
    "bot_name": "Notetaker",
    "webhook_url": "https://your-app.com/webhook/meeting-done",
    "template": "default"
  }'
```

Response (HTTP 201):
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "joining",
  "meeting_url": "https://meet.google.com/abc-defg-hij",
  "meeting_platform": "google_meet",
  "bot_name": "Notetaker",
  "created_at": "2026-03-15T10:00:00Z",
  "updated_at": "2026-03-15T10:00:00Z",
  "analysis_mode": "full",
  "recording_available": false,
  "is_demo_transcript": false,
  "sub_user_id": null,
  "metadata": {},
  "ai_usage": null
}
```

### 6. Get results

Poll until `status` is `done`:
```bash
curl http://localhost:8000/api/v1/bot/550e8400-... \
  -H "Authorization: Bearer sk_live_..."
```

Or receive them via your `webhook_url` — a POST with the full payload is delivered automatically.

---

## Data objects

### Bot response object

Returned by `GET /api/v1/bot/{id}` and `POST /api/v1/bot`.

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique bot UUID |
| `meeting_url` | string | The meeting URL the bot joined |
| `meeting_platform` | string | Detected platform: `google_meet`, `zoom`, `microsoft_teams`, `onepizza`, or `unknown` |
| `bot_name` | string | Display name shown in the meeting |
| `status` | string | Current lifecycle status (see [Bot lifecycle](#bot-lifecycle)) |
| `error_message` | string\|null | Human-readable error if `status` is `error` |
| `created_at` | ISO-8601 | When the bot was created |
| `updated_at` | ISO-8601 | Last status change time |
| `started_at` | ISO-8601\|null | When the bot joined the meeting |
| `ended_at` | ISO-8601\|null | When the meeting ended |
| `duration_seconds` | float\|null | Meeting duration in seconds |
| `participants` | string[] | Names of meeting participants |
| `transcript` | object[] | Array of `{speaker, text, timestamp, source, message_id?, bot_generated?}` entries. `source` is `voice` (default — spoken utterance) or `chat` (captured from the meeting chat panel). `message_id` is a stable short hash for chat dedup. `bot_generated: true` marks entries produced by `POST /say` or `POST /chat`. Available once `status` is `done` |
| `analysis` | object\|null | AI-generated analysis (see [Analysis object](#analysis-object)). Available once `status` is `done` and `analysis_mode` is `full` |
| `chapters` | object[] | Array of `{title, start_time, summary}` chapter segments |
| `speaker_stats` | object[] | Array of `{name, talk_time_s, talk_pct, turns}` per speaker |
| `recording_available` | boolean | `true` when audio can be downloaded via `GET /bot/{id}/recording` |
| `analysis_mode` | string | `full` or `transcript_only` |
| `is_demo_transcript` | boolean | `true` when the platform is unsupported and an AI-generated demo transcript was used |
| `sub_user_id` | string\|null | Business account sub-user identifier (if set via `X-Sub-User` or `sub_user_id` body field) |
| `metadata` | object | Arbitrary key-value pairs echoed from bot creation |
| `ai_usage` | object\|null | AI token usage breakdown (see [AI usage object](#ai-usage-object)) |

### Bot summary object

Returned in the `results` array by `GET /api/v1/bot` (list endpoint). Identical to the Bot response object but **omits** `transcript` and `analysis` to keep the response lightweight.

### Bot list response

`GET /api/v1/bot` returns:

```json
{
  "results": [ /* array of Bot summary objects */ ],
  "total": 42,
  "limit": 20,
  "offset": 0
}
```

| Field | Description |
|-------|-------------|
| `results` | Array of Bot summary objects for the current page |
| `total` | Total number of bots matching the query (for pagination) |
| `limit` | Number of results requested |
| `offset` | Pagination offset |

### Analysis object

Returned in the `analysis` field of the Bot response and in webhook payloads.

| Field | Type | Description |
|-------|------|-------------|
| `summary` | string | One-paragraph meeting summary |
| `key_points` | string[] | Bulleted list of key discussion points |
| `action_items` | object[] | Array of `{task, assignee}` items |
| `decisions` | string[] | Explicit decisions made during the meeting |
| `next_steps` | string[] | Planned follow-up steps agreed in the meeting |
| `sentiment` | string | Overall tone: `positive`, `neutral`, or `negative` |
| `topics` | string[] | Main topics discussed |

### AI usage object

The `ai_usage` field on every bot response provides full cost and token tracking.

```json
{
  "total_tokens": 5200,
  "total_cost_usd": 0.026,
  "primary_model": "claude-opus-4-6",
  "operations": [
    {
      "operation": "transcription",
      "provider": "anthropic",
      "model": "claude-opus-4-6",
      "input_tokens": 100,
      "output_tokens": 3000,
      "total_tokens": 3100,
      "cost_usd": 0.015,
      "duration_s": 4.2
    },
    {
      "operation": "analysis",
      "provider": "anthropic",
      "model": "claude-opus-4-6",
      "input_tokens": 1800,
      "output_tokens": 300,
      "total_tokens": 2100,
      "cost_usd": 0.011,
      "duration_s": 3.1
    }
  ]
}
```

| Field | Description |
|-------|-------------|
| `total_tokens` | Cumulative token count across all operations |
| `total_cost_usd` | Total AI cost in USD before markup |
| `primary_model` | Model used for the main analysis step |
| `operations` | Per-operation breakdown (transcription, analysis, followup_email, etc.) |

---

## Webhook payload

### Supported events

| Event | Fires when |
|-------|------------|
| `bot.joining` | Browser is launching, joining the meeting |
| `bot.in_call` | Bot has been admitted and is recording |
| `bot.call_ended` | Meeting ended, bot is leaving |
| `bot.transcript_ready` | Transcript is finalised |
| `bot.analysis_ready` | AI analysis is complete |
| `bot.done` | Fully complete — final results in payload |
| `bot.error` | Unrecoverable failure |
| `bot.cancelled` | Bot was deleted or `/leave`-d |
| `bot.keyword_alert` | A configured keyword was matched |
| `bot.live_transcript` | New voice transcript entry (~1 s after speech ends). `data.entry = {speaker, text, timestamp, source: "voice"}` |
| `bot.live_transcript_translated` | Translated entry (when `translation_language` is set) |
| `bot.live_chat_message` | New chat message captured from the meeting chat panel. `data.entry = {speaker, text, timestamp, source: "chat", message_id}` |
| `bot.recurring_intel_ready` | Recurring-meeting intelligence finished |
| `bot.test` | Sent by `POST /api/v1/webhook/{id}/test` |

Subscribe via `POST /api/v1/webhook` with `events: ["bot.done", "bot.live_chat_message", ...]`, or use `["*"]` for everything.

### Payload format

Full payload posted to `webhook_url` when a bot finishes.

```json
{
  "event": "bot.done",
  "ts": "2026-03-15T11:00:00Z",
  "data": {
    "bot_id": "550e8400-...",
    "status": "done",
    "error_code": null,
    "error_message": null,
    "retryable": null,
    "participants": ["Alice", "Bob", "Carol"],
    "duration_seconds": 3612,
    "transcript": [
      { "speaker": "Alice", "text": "Let's kick off the review.", "timestamp": 2.0, "source": "voice" },
      { "speaker": "Bob",   "text": "Sharing the doc",            "timestamp": 5.4, "source": "chat", "message_id": "a1b2c3d4e5f60718" }
    ],
    "analysis": {
      "summary": "The team reviewed sprint progress.",
      "key_points": ["Auth module complete", "Dashboard work next"],
      "action_items": [{ "task": "Set up staging access", "assignee": "Alice" }],
      "decisions": ["Use virtual scrolling for performance"],
      "next_steps": ["Alice to provision staging by Friday"],
      "sentiment": "positive",
      "topics": ["sprint review", "authentication"]
    },
    "chapters": [
      { "title": "Sprint Review", "start_time": 0, "summary": "..." }
    ],
    "speaker_stats": [
      { "name": "Alice", "talk_time_s": 120, "talk_pct": 40.0, "turns": 8 }
    ],
    "recording_available": true,
    "is_demo_transcript": false,
    "sub_user_id": null,
    "metadata": {},
    "ai_usage": {
      "total_tokens": 4200,
      "total_cost_usd": 0.021,
      "primary_model": "claude-opus-4-6",
      "operations": [
        {
          "operation": "transcription",
          "provider": "anthropic",
          "model": "claude-opus-4-6",
          "input_tokens": 80,
          "output_tokens": 2500,
          "total_tokens": 2580,
          "cost_usd": 0.013,
          "duration_s": 3.8
        }
      ]
    }
  }
}
```

---

## API Reference

### Auth & Accounts

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/auth/register` | Create account → returns first API key. Body: `{email, password, key_name?, account_type?}`. Set `account_type: "business"` for multi-user platforms. Returns HTTP 409 if email already registered. Rate limit: 3/min per IP |
| `POST` | `/api/v1/auth/login` | Email+password (OAuth2 **form data**: `username`, `password`) → JWT for web UI. Rate limit: 5/min per IP |
| `GET` | `/api/v1/auth/me` | Account info + credit balance. Returns `{id, email, account_type, credits_usd, wallet_address, is_active, created_at}` |
| `POST` | `/api/v1/auth/keys` | Generate a new named API key. Body: `{name?}`. Returns `{id, name, key_preview, is_active, created_at, last_used_at}` — the full key is only shown at creation time |
| `GET` | `/api/v1/auth/keys` | List active API keys. Returns array of `{id, name, key_preview, is_active, created_at, last_used_at}` |
| `DELETE` | `/api/v1/auth/keys/{id}` | Revoke an API key. Returns HTTP 204 |
| `GET` | `/api/v1/auth/wallet` | Get your registered Ethereum wallet address. Returns `{wallet_address, message}` |
| `PUT` | `/api/v1/auth/wallet` | Set or update your Ethereum wallet address. Body: `{wallet_address}`. Each address can only be linked to one account (returns 409 if already taken). Validates `0x` + 40 hex chars format |
| `GET` | `/api/v1/auth/notify` | Get email notification preferences. Returns `{notify_on_done, notify_email}` |
| `PUT` | `/api/v1/auth/notify` | Update notification preferences. Body: `{notify_on_done, notify_email?}` |
| `GET` | `/api/v1/auth/plan` | Get subscription plan and monthly usage. Returns `{plan, monthly_bots_used, monthly_limit, monthly_reset_at}` |
| `PUT` | `/api/v1/auth/account-type` | Switch account type. Body: `{account_type: "personal"\|"business"}`. Returns `{account_type, message}`. No effect on existing data or credits |
| `DELETE` | `/api/v1/auth/account` | **GDPR erasure** — permanently delete account and all data. Irreversible. Deletes recordings from cloud storage |

### Integrations (Slack, Notion, HubSpot, Salesforce)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/integrations` | List all integrations for the current account |
| `POST` | `/api/v1/integrations` | Create integration. Body: `{type: "slack"\|"notion"\|"hubspot"\|"salesforce"\|"linear"\|"jira", name?, config}` |
| `PATCH` | `/api/v1/integrations/{id}` | Update integration config |
| `DELETE` | `/api/v1/integrations/{id}` | Delete integration. HTTP 204 |

Integration config by type:
- **Slack** — `config.webhook_url` (Incoming Webhook URL)
- **Notion** — `config.api_token` + `config.database_id`
- **HubSpot** — `config.access_token` (private app access token); meeting summaries are posted as HubSpot Note engagements
- **Salesforce** — `config.instance_url` + `config.access_token`; meeting summaries are posted as Salesforce Tasks
- **Linear** — `config.api_key` + `config.team_id`
- **Jira** — `config.base_url` + `config.api_token` + `config.project_key`

When a bot completes, all active integrations fire automatically:
- **Slack** — posts a rich Block Kit message to the webhook URL (summary, action items, decisions, participants)
- **Notion** — creates a page in the configured database (summary, action items as to-dos, decisions, transcript)
- **HubSpot / Salesforce** — posts meeting notes as CRM engagements/tasks

### Retention Policies

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/retention` | Get per-account retention policy. Returns `{bot_retention_days, recording_retention_days, transcript_retention_days}` |
| `PUT` | `/api/v1/retention` | Set retention policy. Body: `{bot_retention_days?, recording_retention_days?, transcript_retention_days?}`. Use `-1` for keep-forever |

A background task enforces policies nightly. Platform-wide defaults are set via `DEFAULT_BOT_RETENTION_DAYS` (90 days) and `DEFAULT_RECORDING_RETENTION_DAYS` (30 days) env vars. Per-account policies override the global defaults.

### Keyword Alerts

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/keyword-alerts` | List all registered keyword alerts for the current account |
| `POST` | `/api/v1/keyword-alerts` | Register keyword alert. Body: `{keyword, webhook_url?}` |
| `DELETE` | `/api/v1/keyword-alerts/{id}` | Delete keyword alert. HTTP 204 |

When a keyword is detected in a completed transcript, a `bot.keyword_alert` webhook event fires. Keywords can also be set per-bot at creation time via `keyword_alerts: [{"keyword": "...", "webhook_url": "..."}]`.

### Workspaces

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/workspaces` | List all workspaces the current account belongs to |
| `POST` | `/api/v1/workspaces` | Create a workspace. Body: `{name, description?}` |
| `PATCH` | `/api/v1/workspaces/{id}` | Update workspace name or description |
| `DELETE` | `/api/v1/workspaces/{id}` | Delete workspace. HTTP 204 |

Tag bots with a `workspace_id` at creation to make them visible to all workspace members. Invite members with roles: `admin`, `member`, or `viewer`.

### MCP (Model Context Protocol)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/mcp/schema` | Returns the MCP server manifest with available tools |
| `POST` | `/api/v1/mcp/call` | Execute an MCP tool. Body: `{tool, params}` |

Available MCP tools: `list_meetings`, `get_meeting`, `search_meetings`, `get_action_items`, `get_meeting_brief`. Enable/disable with `MCP_ENABLED` env var (default `true`).

### Calendar Auto-Join

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/calendar` | List iCal feeds for the current account |
| `POST` | `/api/v1/calendar` | Add iCal feed. Body: `{name?, ical_url, bot_name?, auto_record?, is_active?}` |
| `PATCH` | `/api/v1/calendar/{id}` | Update feed settings |
| `DELETE` | `/api/v1/calendar/{id}` | Delete feed. HTTP 204 |
| `POST` | `/api/v1/calendar/{id}/sync` | Manually trigger an immediate sync of a feed |

The background poll loop checks all active feeds every `CALENDAR_POLL_INTERVAL_S` seconds (default 5 min). When a meeting with a video URL (Google Meet, Zoom, Teams, etc.) is found starting within 15 minutes, a bot is automatically dispatched to join 60 seconds early.

> **Login note:** `POST /api/v1/auth/login` expects **`application/x-www-form-urlencoded`** (not JSON) with fields `username` (your email) and `password`. The returned JWT is for the web UI only; use your `sk_live_...` API key as a Bearer token for all other API calls.

### Billing

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/billing/balance` | Current balance + last 50 transactions (each with `id`, `amount_usd`, `type`, `description`, `reference_id`, `created_at`) |
| `POST` | `/api/v1/billing/stripe/checkout` | Create Stripe Checkout session. Body: `{amount_usd, success_url?, cancel_url?}`. `amount_usd` must be one of the values in `STRIPE_TOP_UP_AMOUNTS`. Returns `{checkout_url}`. A pending record is stored immediately; credits are applied on webhook confirmation |
| `POST` | `/api/v1/billing/stripe/webhook` | Stripe webhook receiver — register this URL in your Stripe dashboard for `checkout.session.completed` events |
| `GET` | `/api/v1/billing/usdc/address` | Get USDC/ERC-20 deposit address (platform wallet if admin-configured, otherwise HD-derived per-user address). Returns `{deposit_address, network, token, rate_usd_per_token, note}`. 1 USDC = $1 credit, credited within ~1 min |

**Transaction types** (the `type` field in balance transactions):

| Type | Meaning |
|------|---------|
| `stripe_topup` | Credits added via Stripe card payment |
| `usdc_topup` | Credits added via USDC deposit |
| `bot_usage` | Credits deducted on bot completion ($0.10 flat fee per bot, or raw AI cost × `CREDIT_MARKUP` if flat fee is disabled) |

### Bots

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/bot/validate-meeting-url` | Pre-flight URL check. Body: `{meeting_url}`. Returns `{valid, platform, supported, error_code?, message}`. Rate limit: 60/min per IP |
| `POST` | `/api/v1/bot` | Create a bot & join a meeting. Returns Bot response object (HTTP 201). Rate limit: 20/min per IP. Requires minimum `MIN_CREDITS_USD` balance (HTTP 402 if insufficient) |
| `GET` | `/api/v1/bot` | List bots (lightweight summaries, no transcript/analysis). Query params: `limit` (1–100, default 20), `offset` (default 0), `status` (filter by status string). Returns Bot list response |
| `GET` | `/api/v1/bot/stats` | Aggregate counts: `{total, active, done, error, by_status}`. `active` includes: ready/scheduled/queued/joining/in_call/call_ended |
| `GET` | `/api/v1/bot/{id}` | Full details (transcript, analysis). Poll until `status` is `done` or `error` |
| `DELETE` | `/api/v1/bot/{id}` | Stop & remove a bot. HTTP 204. Running bots are cancelled (transcript salvaged if possible); finished bots are removed from memory |
| `GET` | `/api/v1/bot/{id}/transcript` | Raw transcript only. Blocks up to 25 s if transcription is in progress, then returns automatically. Returns `{bot_id, transcript}`. HTTP 425 (Too Early) if bot is not yet at `call_ended`/`done`/`cancelled` |
| `GET` | `/api/v1/bot/{id}/recording` | Download WAV audio. HTTP 404 if recording not available |
| `GET` | `/api/v1/bot/{id}/highlight` | Curated highlights derived from AI analysis. Returns `{bot_id, highlights: [{type, text, detail}]}` where `type` is one of `key_point`, `action_item`, or `decision`. HTTP 425 if analysis not yet available |
| `POST` | `/api/v1/bot/{id}/analyze` | (Re-)run AI analysis. Body: `{template?, prompt_override?}`. Blocks up to 25 s waiting for transcript. Returns Analysis object. Use to switch templates or apply a custom prompt on an existing transcript |
| `POST` | `/api/v1/bot/{id}/ask` | Q&A on the transcript. Body: `{question}`. Returns `{bot_id, question, answer}`. Rate limit: 10/min. HTTP 425 if no transcript yet |
| `POST` | `/api/v1/bot/{id}/ask-live` | Q&A on a bot currently in a call (uses live buffer). Body: `{question}`. Rate limit: 10/min |
| `POST` | `/api/v1/bot/{id}/say` | **Speak text aloud in the live meeting.** Body: `{text, voice?: "gemini"\|"edge", interrupt?: bool}`. Returns 202 with `{bot_id, task_id, queued, interrupted_previous}`. TTS+playback runs in the background; concurrent calls serialise behind a per-bot lock. Default voice: Gemini TTS (~1–2 s); use `edge` for ~300–500 ms latency. `interrupt:true` cancels in-flight speech. HTTP 409 if not `in_call`. Rate limit: 30/min |
| `POST` | `/api/v1/bot/{id}/chat` | **Post text to the live meeting chat.** Body: `{text}`. Returns 202 with `{bot_id, task_id, queued}`. Keyboard typing runs in the background behind a per-bot lock. Works on Google Meet, Zoom, Teams, onepizza. HTTP 409 if not `in_call`. Rate limit: 30/min |
| `GET` | `/api/v1/bot/{id}/stream` | **Server-Sent Events** delivering every transcript entry (voice + chat) as it's created. Each event is a JSON object with `{speaker, text, timestamp, source: "voice"\|"chat", message_id?, bot_generated?}`. Closes with `{__terminal__: true, status}` when the bot finishes |
| `POST` | `/api/v1/bot/{id}/share` | Generate a shareable public link. Returns `{share_url, bot_id}`. Only works for `done` bots |
| `POST` | `/api/v1/bot/{id}/followup-email` | Draft follow-up email. Returns `{bot_id, subject, body}`. Rate limit: 5/min. HTTP 425 if no transcript or analysis yet |
| `GET` | `/api/v1/bot/{id}/export/markdown` | Export full report as Markdown file |
| `GET` | `/api/v1/bot/{id}/export/pdf` | Export full report as PDF file |
| `GET` | `/api/v1/bot/{id}/export/json` | Export full session as structured JSON (transcript, analysis, chapters, speaker stats, metadata) |
| `GET` | `/api/v1/bot/{id}/export/srt` | Export transcript as an SRT subtitle file |
| `POST` | `/api/v1/webhook` | Register global webhook. Body: `{url, events, secret?}` |
| `GET` | `/api/v1/webhook` | List all registered webhooks |
| `DELETE` | `/api/v1/webhook/{id}` | Remove webhook. HTTP 204 |
| `POST` | `/api/v1/webhook/{id}/test` | Test webhook delivery — sends a sample `bot.test` event. Signed with HMAC if secret set |
| `GET` | `/api/v1/webhook/deliveries` | List recent delivery attempts across all webhooks. Query params: `limit` (default 50), `offset` (default 0) |
| `GET` | `/api/v1/templates` | List analysis templates with names and descriptions |
| `GET` | `/api/v1/templates/default-prompt` | Return the raw default analysis prompt used when no `template` or `prompt_override` is set |
| `GET` | `/api/v1/analytics` | Aggregate stats for all bots in memory. Returns `{total_bots, active_bots, by_status, by_platform, success_rate, avg_duration_seconds, total_transcript_entries, total_ai_tokens, total_ai_cost_usd}` |
| `GET` | `/api/v1/action-items/stats` | Cross-meeting action-item counts. Returns `{total, by_assignee, recent}` where `recent` contains up to 20 most recent action items with `bot_id` and `meeting_url` |
| `GET` | `/api/v1/search` | Full-text search across all transcripts (DB-persisted + in-memory). Query params: `q` (required), `platform?`, `include_archived?`, `semantic?` (boolean — uses embedding similarity when true). Returns matching transcript snippets with bot context |
| `GET` | `/api/v1/bot/{id}/brief` | Pre-meeting brief: AI-generated agenda and background for an upcoming meeting. HTTP 425 if no transcript yet |
| `GET` | `/api/v1/bot/{id}/recurring` | Recurring meeting intelligence: links to previous meetings at the same URL and surfaces recurring action items |
| `GET` | `/api/v1/bot/{id}/video` | Download screen recording as MP4. HTTP 404 if not available. Enable per-bot with `record_video: true` |
| `GET` | `/api/v1/health` or `/health` | Health check → `{"status": "ok", "service": "JustHereToListen.io", "version": "<current>"}` |

### Admin (requires admin account)

> **Admin access:** Accounts with their email listed in `ADMIN_EMAILS` env var (comma-separated) or with `is_admin=true` in the database can access these endpoints. All others receive HTTP 403. See the full interactive admin API docs at `GET /api/v1/admin/docs`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/admin/wallet` | Get the current platform USDC collection wallet address |
| `PUT` | `/api/v1/admin/wallet` | Set or update the platform USDC collection wallet address. Body: `{wallet_address}` |
| `GET` | `/api/v1/admin/rpc-url` | Check whether a `CRYPTO_RPC_URL` is configured (env var or admin-set) |
| `PUT` | `/api/v1/admin/rpc-url` | Set the Ethereum RPC URL used by the USDC monitor (stored in DB, no restart needed). Validates connectivity via `eth_blockNumber` before saving. Body: `{rpc_url}` |
| `GET` | `/api/v1/admin/config` | List all platform configuration values |
| `POST` | `/api/v1/admin/credit` | Manually credit a user account. Body: `{email, amount_usd, note?}` — use to fix missed USDC deposits |
| `POST` | `/api/v1/admin/accounts/{account_id}/set-account-type` | Change any user's account type. Body: `{account_type: "personal"\|"business"}`. Returns `{account_id, email, account_type, message}` |
| `GET` | `/api/v1/admin/usdc/unmatched` | List USDC transfers received at the platform wallet that couldn't be attributed to any account (sender wallet not registered). Query: `?resolved=false` (default) / `?resolved=true` / omit for all |
| `POST` | `/api/v1/admin/usdc/unmatched/{tx_hash}/resolve` | Mark an unmatched transfer as resolved after crediting the account. Body: `{note?}` |
| `POST` | `/api/v1/admin/usdc/rescan` | Reset the USDC monitor's block pointer so it rescans from `from_block` on the next cycle. Body: `{from_block}` |
| `POST` | `/api/v1/auth/saml/configs` | **(Admin only)** Register a SAML IdP configuration for an organisation. Body: `{org_slug, idp_metadata_url, ...}`. Requires `SAML_ENABLED=true` |

### Admin UI actions (web panel at `/admin`)

| Action | Description |
|--------|-------------|
| Enable/Disable account | Toggle `is_active` on any user account (prevents login and API access) |
| Make Admin / Revoke Admin | Toggle `is_admin` to grant or revoke admin privileges |
| Set Plan | Inline dropdown to change any account's subscription plan (free/starter/pro/business) |
| Set Account Type | Inline dropdown to switch any account between `personal` and `business` modes — backed by `POST /api/v1/admin/accounts/{id}/set-account-type` |

> **Admin access:** Accounts listed in `ADMIN_EMAILS` (comma-separated env var) or accounts with `is_admin=true` in the database can access these endpoints. All other users receive HTTP 403.

**Set the platform USDC wallet (admin only):**
```bash
# Set the wallet where all users will send USDC
curl -X PUT http://localhost:8000/api/v1/admin/wallet \
  -H "Authorization: Bearer sk_live_<admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"wallet_address": "0xYourEthereumWalletAddress1234567890abcdef"}'

# Check the current wallet
curl http://localhost:8000/api/v1/admin/wallet \
  -H "Authorization: Bearer sk_live_<admin-api-key>"
```

Or use the admin web UI at `/admin` to manage all settings through a form.

### Web UI

| Path | Description |
|------|-------------|
| `/` | Landing page (marketing homepage) — redirects to `/dashboard` if already logged in |
| `/register` | Create account (Personal or Business); Google/Microsoft SSO sign-up buttons when configured |
| `/login` | Login with email/password or SSO (Google/Microsoft when configured) |
| `/dashboard` | **Send Bot Now** (immediate) and **Schedule a Bot** (future) with advanced options (video, live transcription, PII, translation). Live status polling, bot search/filter by ID/URL/platform, cancel active bots. **Meeting History** tab with full DB-backed browsing of all past meetings. Balance, API keys, subscription plan & usage, email notifications, USDC wallet, SSO, account type switcher, integrations, calendar feeds, action items, analytics, transaction history |
| `/topup` | Add credits — Stripe card (redirect to secure checkout) or USDC (ERC-20 deposit address with amount selector) |
| `/bot/{id}` | Session viewer — transcript with search, AI analysis (summary, key points, action items, decisions, next steps, sentiment, topics), speaker breakdown, chapters, meeting metadata, download links for audio/video/markdown/PDF, "Ask about this meeting" AI Q&A, "Generate follow-up email" one-click drafting, share link generation |
| `/admin` | Platform administration — plan breakdown stats, bot activity & platform feature counters (webhooks/integrations/calendar/SSO), system status (Stripe/RPC/HD seed/email/storage/video/SSO), unmatched USDC transfers, user accounts with inline plan management, manual credit, rescan, wallet config, RPC URL (admin only) |

Full interactive docs (public endpoints): `GET /api/docs`
Alternative ReDoc view: `GET /api/redoc`
Raw OpenAPI JSON: `GET /api/openapi.json`

> The public Swagger UI at `/api/docs` shows all user-facing endpoints. Admin-only endpoints, platform analytics, and the `ai_usage` cost breakdown field are excluded. Admins can access the full schema — including all admin endpoints, analytics, and AI usage cost data — at `GET /api/v1/admin/docs` (requires admin auth).

Admin API docs (full schema including admin endpoints, analytics & ai_usage): `GET /api/v1/admin/docs`
Admin raw OpenAPI JSON: `GET /api/v1/admin/openapi.json`

---

## Bot creation options

Full set of fields accepted by `POST /api/v1/bot`:

```json
{
  "meeting_url": "https://meet.google.com/...",   // required; blocks private/loopback IPs
  "bot_name": "MeetingBot",                        // display name in meeting (max 100 chars)
  "webhook_url": "https://your-app.com/hook",      // where to POST results when done
  "join_at": "2026-03-15T14:00:00Z",               // schedule for future join (ISO-8601)
  "analysis_mode": "full",                         // "full" | "transcript_only"
  "template": "default",                           // see Templates below
  "prompt_override": "Custom AI prompt...",        // overrides template (max 8000 chars)
  "vocabulary": ["ProductName", "TechTerm"],       // transcription accuracy hints (max 100 terms)
  "respond_on_mention": true,                      // bot replies when name is mentioned
  "mention_response_mode": "text",                 // "text" | "voice" | "both"
  "tts_provider": "edge",                          // "edge" (fast, free) | "gemini" (natural)
  "start_muted": false,                            // join with microphone muted
  "live_transcription": false,                     // stream transcript in 15-s chunks during call
  "sub_user_id": "user-123",                       // business accounts: scope data to this end-user
  "metadata": { "your_key": "your_value" },        // pass-through, echoed in all responses
  "consent_enabled": false,                        // announce recording at join; redact opted-out participants
  "record_video": false,                           // capture screen recording (MP4 via ffmpeg)
  "workspace_id": "ws-uuid",                       // tag this bot to a workspace (visible to all members)
  "transcription_provider": "gemini",              // "gemini" | "anthropic" | "whisper" (local)
  "auto_followup_email": false,                    // auto-generate & send follow-up email on completion
  "keyword_alerts": [                              // per-bot keyword monitoring (max 50)
    { "keyword": "budget", "webhook_url": "https://your-app.com/alert" }
  ]
}
```

---

## Templates

Pass `template` in bot creation. Use `prompt_override` for a fully custom prompt (overrides `template` when both are set).

| Template | Best for |
|----------|----------|
| `default` | Any general meeting |
| `sales` | B2B/B2C sales calls — adds buying signals, objections, deal stage |
| `standup` | Daily standups — adds blockers, completed/planned |
| `1on1` | Manager check-ins — adds feedback, growth areas |
| `retro` | Sprint retros — adds went well/poorly, improvements |
| `kickoff` | Project kickoffs — adds scope, deliverables, risks |
| `allhands` | Town halls — adds announcements, employee questions |
| `postmortem` | Incident reviews — adds timeline, root causes |
| `interview` | Hiring panels — adds strengths, concerns, recommendation |
| `design-review` | Design discussions — adds design decisions, open questions |

---

## HTTP status codes

| Code | Meaning |
|------|---------|
| `200 OK` | Successful GET |
| `201 Created` | Bot or API key created |
| `204 No Content` | Successful DELETE or revoke |
| `400 Bad Request` | Malformed request body |
| `401 Unauthorized` | Missing or invalid API key / JWT |
| `402 Payment Required` | Insufficient credits to create a bot |
| `403 Forbidden` | Valid credentials but not authorised (e.g. non-admin accessing `/admin`) |
| `404 Not Found` | Resource not found, or sub-user mismatch (returns 404 rather than 403 to prevent enumeration) |
| `409 Conflict` | Email already registered, or wallet already linked to another account |
| `422 Unprocessable Entity` | Validation error (invalid meeting URL, bad wallet address format, etc.) |
| `425 Too Early` | Transcript or analysis not yet available — retry after the `Retry-After` header value (seconds) |
| `429 Too Many Requests` | Rate limit exceeded. Limits: register 3/min, login 5/min, create bot 20/min, validate URL 60/min, analyze/ask/ask-live 10/min, followup-email 5/min, admin write endpoints 10/min (5/min for rescan) |
| `503 Service Unavailable` | Database unreachable or service misconfigured |

**Error response body:**
```json
{
  "detail": "Human-readable error message",
  "error_code": "not_found",
  "retryable": false
}
```

Machine-readable `error_code` values: `bad_request`, `unauthorized`, `forbidden`, `not_found`, `conflict`, `validation_error`, `too_early`, `rate_limited`, `internal_error`, `bad_gateway`, `service_unavailable`. The `retryable` flag indicates whether the client should retry the request (typically `true` for 425, 429, 5xx; `false` for 4xx).

---

## Environment variables

### AI providers

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Claude API key. Takes precedence over Gemini when both are set |
| `GEMINI_API_KEY` | — | Gemini API key (transcription + analysis) |

### Auth

| Variable | Default | Description |
|----------|---------|-------------|
| `API_KEY` | — | Legacy superadmin key (bypasses per-user auth; leave empty to use per-user accounts) |
| `JWT_SECRET` | auto-generated | Secret for signing web UI JWT tokens. Generate a stable value with `openssl rand -hex 32`. If unset, a random secret is generated on each startup (sessions are invalidated on every restart) |
| `JWT_EXPIRE_HOURS` | `24` | JWT token lifetime in hours |
| `ADMIN_EMAILS` | — | Comma-separated list of email addresses granted admin access on login (e.g. `admin@acme.com,ops@acme.com`). Accounts in this list get admin access without needing `is_admin=true` in the database |

### SSO (Google / Microsoft OAuth2)

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_CLIENT_ID` | — | Google OAuth2 client ID (create at console.cloud.google.com) |
| `GOOGLE_CLIENT_SECRET` | — | Google OAuth2 client secret |
| `MICROSOFT_CLIENT_ID` | — | Microsoft Entra (Azure AD) application (client) ID |
| `MICROSOFT_CLIENT_SECRET` | — | Microsoft Entra client secret |
| `OAUTH_REDIRECT_BASE_URL` | `http://localhost:8000` | Base URL for OAuth callback — set to your public domain in production |

SSO endpoints:
- `GET /api/v1/auth/oauth/google/authorize` → redirects to Google login
- `GET /api/v1/auth/oauth/google/callback` → exchanges code, returns `{"api_key": "sk_live_..."}` (or JWT cookie)
- Same pattern for `microsoft`

### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite+aiosqlite:///./meetingbot.db` | SQLAlchemy async URL. The bundled `docker-compose.yml` sets this to PostgreSQL automatically via the `db` service. `postgresql://` URLs are automatically translated to `postgresql+asyncpg://` |

### Billing — Stripe

| Variable | Default | Description |
|----------|---------|-------------|
| `STRIPE_SECRET_KEY` | — | Stripe secret key (`sk_live_...`) |
| `STRIPE_WEBHOOK_SECRET` | — | Stripe webhook signing secret (`whsec_...`) |
| `STRIPE_TOP_UP_AMOUNTS` | `10,25,50,100` | Comma-separated USD top-up options shown in the UI and validated on checkout |

### Billing — USDC

| Variable | Default | Description |
|----------|---------|-------------|
| `CRYPTO_HD_SEED` | — | 64-char hex seed for HD wallet derivation (generate once, keep secret). Not required if the admin sets a platform wallet via `/admin` |
| `CRYPTO_RPC_URL` | — | Infura/Alchemy Ethereum RPC endpoint for USDC monitoring. Can also be set via `PUT /api/v1/admin/rpc-url` (no restart required) |
| `USDC_CONTRACT` | `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48` | USDC ERC-20 contract address (Ethereum mainnet) |

> **USDC wallet configuration:** The platform USDC collection wallet can be set by an admin via `PUT /api/v1/admin/wallet` or the `/admin` web UI. When set, this wallet address is returned to all users at `GET /api/v1/billing/usdc/address`, overriding the HD-derived per-user addresses. This means you can accept USDC without configuring `CRYPTO_HD_SEED` — just set the wallet via the admin panel.

### Billing — Credit control

| Variable | Default | Description |
|----------|---------|-------------|
| `BOT_FLAT_FEE_USD` | `0.10` | Flat fee charged per bot run in USD. Set to `0` to switch to markup-based pricing (raw AI cost × `CREDIT_MARKUP`) |
| `CREDIT_MARKUP` | `3.0` | Multiply raw AI cost by this factor when deducting credits. Only applies when `BOT_FLAT_FEE_USD` is `0` |
| `MIN_CREDITS_USD` | `0.10` | Minimum balance required to create a bot (HTTP 402 if below this threshold) |

### Cloud storage

| Variable | Default | Description |
|----------|---------|-------------|
| `STORAGE_BACKEND` | `local` | `local` (in-container) or `s3` (AWS S3 / Cloudflare R2 / MinIO) |
| `S3_BUCKET` | — | S3 bucket name |
| `S3_ENDPOINT_URL` | — | Custom endpoint for R2 or MinIO (leave empty for AWS S3) |
| `S3_ACCESS_KEY_ID` | — | S3 access key ID |
| `S3_SECRET_ACCESS_KEY` | — | S3 secret access key |
| `S3_REGION` | `us-east-1` | AWS region (ignored for R2/MinIO) |
| `S3_PUBLIC_URL` | — | Public base URL for recordings (e.g. `https://pub.r2.dev/my-bucket`). When set, download links point here instead of generating presigned URLs |

> When `STORAGE_BACKEND=s3` the recording WAV is uploaded to S3/R2/MinIO immediately after the call ends. The local copy is retained as a fallback but the `recording_path` field in bot responses will be the cloud object key. Use `GET /api/v1/bot/{id}/recording` to download (redirects to a presigned URL or the public URL).

### Video recording

| Variable | Default | Description |
|----------|---------|-------------|
| `VIDEO_FPS` | `10` | ffmpeg frame rate for X11grab video capture |
| `VIDEO_CRF` | `28` | H.264 constant rate factor (lower = better quality, larger file) |
| `VIDEO_SCALE` | `1280x720` | Capture resolution (`WxH`) |

Enable per-bot with `record_video: true` in `POST /api/v1/bot`. Requires `ffmpeg` installed in the container. Download via `GET /api/v1/bot/{id}/video` (returns `video/mp4`).

### Idempotency

| Variable | Default | Description |
|----------|---------|-------------|
| `IDEMPOTENCY_TTL_HOURS` | `24` | How long idempotency keys are retained. After TTL, the same key may create a new bot |

Pass `Idempotency-Key: <unique-string>` in the `POST /api/v1/bot` request. Replayed requests return the original response with header `X-Idempotency-Replayed: true`.

### Email notifications

| Variable | Default | Description |
|----------|---------|-------------|
| `EMAIL_BACKEND` | `none` | `none`, `smtp`, or `sendgrid` |
| `SMTP_HOST` | — | SMTP server hostname |
| `SMTP_PORT` | `587` | SMTP port (587 = STARTTLS, 465 = SSL) |
| `SMTP_USERNAME` | — | SMTP login username |
| `SMTP_PASSWORD` | — | SMTP login password |
| `SMTP_FROM_ADDRESS` | — | From address for outgoing emails |
| `SMTP_USE_TLS` | `true` | Use STARTTLS (`true`) or plain (`false`) |
| `SENDGRID_API_KEY` | — | SendGrid API key (required when `EMAIL_BACKEND=sendgrid`) |

> Each account can independently enable/disable email notifications and set a custom notification address via `PUT /api/v1/auth/notify`. The platform-level `EMAIL_BACKEND` controls whether the sending infrastructure is available at all.

### Calendar auto-join

| Variable | Default | Description |
|----------|---------|-------------|
| `CALENDAR_POLL_INTERVAL_S` | `300` | How often (in seconds) iCal feeds are checked for upcoming meetings. Default: 5 minutes |

> Each account can register multiple iCal feed URLs via `POST /api/v1/calendar`. The background loop polls all active feeds and auto-dispatches a bot 60 seconds before any meeting that has a video URL (Google Meet, Zoom, Teams) starting within 15 minutes.

### Subscription plans

| Variable | Default | Description |
|----------|---------|-------------|
| `PLAN_FREE_BOTS_PER_MONTH` | `5` | Monthly bot limit for `free` plan accounts |
| `PLAN_STARTER_BOTS_PER_MONTH` | `50` | Monthly bot limit for `starter` plan accounts |
| `PLAN_PRO_BOTS_PER_MONTH` | `500` | Monthly bot limit for `pro` plan accounts |
| `PLAN_BUSINESS_BOTS_PER_MONTH` | `-1` | Monthly bot limit for `business` plan accounts (`-1` = unlimited) |

> Account plans are managed by admins via `POST /api/v1/admin/credit` (credit top-up also upgrades plan) or directly in the database. The current plan and usage are visible to users at `GET /api/v1/auth/plan`.

### Bot settings

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_CONCURRENT_BOTS` | `3` | Max simultaneous browser bots. Extras are queued and auto-started when a slot opens |
| `BOT_ADMISSION_TIMEOUT` | `300` | Seconds to wait for a meeting host to admit the bot before timing out |
| `BOT_MAX_DURATION` | `7200` | Maximum meeting duration in seconds (2 hours) |
| `BOT_ALONE_TIMEOUT` | `300` | Seconds the bot will remain alone in a meeting before auto-leaving (5 minutes) |
| `BOT_JOIN_MAX_RETRIES` | `2` | Number of join retry attempts on failure |
| `TRANSCRIPTION_LANGUAGE` | — | BCP-47 language code for transcription (e.g. `en`, `es`, `fr`). Leave empty for auto-detection |

### Network

| Variable | Default | Description |
|----------|---------|-------------|
| `CORS_ORIGINS` | `*` | Comma-separated allowed origins. Set to specific domains in production (e.g. `https://app.acme.com`) |
| `WEBHOOK_TIMEOUT_SECONDS` | `10` | HTTP timeout in seconds for webhook delivery attempts |
| `WEBHOOK_MAX_ATTEMPTS` | `5` | Max delivery attempts per webhook before auto-disabling |
| `WEBHOOK_RETRY_DELAYS` | `60,300,1500,7200,36000` | Backoff delays in seconds between retry attempts |
| `WEBHOOK_DELIVERY_RETENTION_DAYS` | `30` | Days to retain webhook delivery logs |

### Consent & recording announcement

| Variable | Default | Description |
|----------|---------|-------------|
| `CONSENT_ANNOUNCEMENT_ENABLED` | `false` | Set `true` to globally enable recording announcements for all bots |
| `CONSENT_MESSAGE` | (built-in) | Message read/typed when the bot joins to announce recording |
| `CONSENT_OPT_OUT_PHRASE` | `opt out` | Case-insensitive phrase that triggers transcript redaction for that participant |

Per-bot override: set `consent_enabled: true` in `POST /api/v1/bot`.

### Data retention

| Variable | Default | Description |
|----------|---------|-------------|
| `DEFAULT_BOT_RETENTION_DAYS` | `90` | Days to keep bot data in the database (`-1` = keep forever) |
| `DEFAULT_RECORDING_RETENTION_DAYS` | `30` | Days to keep audio/video recording files on disk (`-1` = keep forever) |

Per-account overrides via `GET/PUT /api/v1/retention`. A background task enforces policies nightly.

### Keyword alerts

| Variable | Default | Description |
|----------|---------|-------------|
| `KEYWORD_ALERTS_ENABLED` | `true` | Set `false` to globally disable keyword alert processing |

### Local Whisper transcription

| Variable | Default | Description |
|----------|---------|-------------|
| `WHISPER_ENABLED` | `false` | Set `true` to default to local Whisper transcription instead of Gemini |
| `WHISPER_MODEL` | `base` | Model size: `tiny`, `base`, `small`, `medium`, `large` (larger = more accurate, slower) |
| `WHISPER_DEVICE` | `cpu` | `cpu` or `cuda` for GPU-accelerated transcription |

Per-bot override: set `transcription_provider: "whisper"` in `POST /api/v1/bot`. Falls back to Gemini if `faster-whisper` / `openai-whisper` is not installed.

### Team workspaces

| Variable | Default | Description |
|----------|---------|-------------|
| `WORKSPACES_ENABLED` | `true` | Set `false` to disable the workspaces feature |

### SAML 2.0 SSO

| Variable | Default | Description |
|----------|---------|-------------|
| `SAML_ENABLED` | `false` | Set `true` to enable SAML 2.0 identity provider support |
| `SAML_SP_BASE_URL` | — | Base URL of this service (e.g. `https://app.meetingbot.io`) — used in SP metadata and ACS URL |

SAML endpoints (when `SAML_ENABLED=true`):
- `POST /api/v1/auth/saml/configs` — **(admin only)** register an IdP config for an org slug
- `GET /api/v1/auth/saml/{org_slug}/authorize` — redirect users to the IdP login
- `POST /api/v1/auth/saml/{org_slug}/acs` — SAML assertion consumer service (ACS) callback

### MCP server

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_ENABLED` | `true` | Set `false` to disable MCP server endpoints |

### CRM integrations

| Variable | Default | Description |
|----------|---------|-------------|
| `HUBSPOT_API_KEY` | — | HubSpot private app access token (platform-level fallback; per-account config via `POST /api/v1/integrations`) |
| `SALESFORCE_CLIENT_ID` | — | Salesforce connected app client ID |
| `SALESFORCE_CLIENT_SECRET` | — | Salesforce connected app client secret |
| `SALESFORCE_USERNAME` | — | Salesforce username |
| `SALESFORCE_PASSWORD` | — | Salesforce password |
| `SALESFORCE_SECURITY_TOKEN` | — | Salesforce security token (appended to password for API auth) |
| `SALESFORCE_INSTANCE_URL` | — | Salesforce instance URL (e.g. `https://yourorg.salesforce.com`) |

---

## SDKs

### Python

```bash
pip install meetingbot-sdk
```

```python
import time
from meetingbot import MeetingBotClient

client = MeetingBotClient(api_key="sk_live_...")
bot = client.create_bot(meeting_url="https://meet.google.com/xyz-abc-def")
while bot.status not in {"done", "error", "cancelled"}:
    time.sleep(10)
    bot = client.get_bot(bot.id)
print(bot.transcript)
```

Async usage:

```python
import asyncio
from meetingbot import AsyncMeetingBotClient

async with AsyncMeetingBotClient(api_key="sk_live_...") as client:
    bot = await client.create_bot(meeting_url="https://meet.google.com/xyz-abc-def")
    while bot.status not in {"done", "error", "cancelled"}:
        await asyncio.sleep(10)
        bot = await client.get_bot(bot.id)
    print(bot.transcript)
```

See [`sdk/python/README.md`](sdk/python/README.md) for full reference.

### JavaScript / TypeScript

```bash
npm install meetingbot-sdk
```

```typescript
import { MeetingBotClient } from "meetingbot-sdk";

const client = new MeetingBotClient({ apiKey: "sk_live_..." });
const bot = await client.createBot({ meeting_url: "https://meet.google.com/xyz" });
const terminal = new Set(["done", "error", "cancelled"]);
let current = bot;
while (!terminal.has(current.status ?? "")) {
  await new Promise((r) => setTimeout(r, 10_000));
  current = await client.getBot(bot.id);
}
console.log(current.transcript);
```

See [`sdk/js/README.md`](sdk/js/README.md) for full reference.

---

## Prometheus metrics

`GET /metrics` returns metrics in the Prometheus text exposition format (no authentication required).

Scrape configuration example:

```yaml
scrape_configs:
  - job_name: meetingbot
    static_configs:
      - targets: ["your-host:8000"]
```

Available metrics:

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `meetingbot_http_requests_total` | Counter | `method`, `path`, `status_code` | Total HTTP requests |
| `meetingbot_http_request_duration_seconds` | Histogram | `method`, `path` | Request latency |
| `meetingbot_bots_created_total` | Counter | `platform` | Bots created by platform |
| `meetingbot_bots_active` | Gauge | — | Currently active bots |
| `meetingbot_bots_completed_total` | Counter | `status` | Bots reaching terminal state |
| `meetingbot_ai_tokens_total` | Counter | `operation`, `provider` | AI tokens consumed |
| `meetingbot_ai_cost_usd_total` | Counter | `provider` | AI cost in USD |
| `meetingbot_webhook_deliveries_total` | Counter | `status` | Webhook delivery attempts |

Install `prometheus-client` to enable (included in `requirements.txt`). Graceful degradation if not installed — `/metrics` returns a plain-text stub.

---

## Bot lifecycle

```
                     ┌─ queued ──────────────────────┐
                     │  (max concurrent bots reached) │
ready ───────────────┴──────────────────────────────► joining → in_call → call_ended → transcribing → done
  ↑                                                                                                  ↘ error
scheduled (join_at set)                                                                              ↘ cancelled
```

| Status | Meaning |
|--------|---------|
| `ready` | Created, about to join immediately |
| `scheduled` | Created with a future `join_at` time, waiting to join |
| `queued` | Waiting for a free bot slot (`MAX_CONCURRENT_BOTS` reached) |
| `joining` | Chromium browser launching and joining the meeting |
| `in_call` | Recording in progress |
| `call_ended` | Meeting ended, audio saved |
| `transcribing` | Sending audio to AI for transcription |
| `done` | Transcript + analysis complete, results available |
| `error` | An unrecoverable error occurred (see `error_message`) |
| `cancelled` | Bot was stopped via `DELETE /api/v1/bot/{id}` |

The bot auto-leaves when it has been the only participant for `BOT_ALONE_TIMEOUT` seconds (default 5 min).

**Result retention:** Results are kept in memory for 24 hours after completion. Save them to your own storage before then.

---

## Webhooks

### Per-bot webhook

Pass `webhook_url` when creating a bot. A single POST with full results is sent when the bot reaches a terminal state (`done`, `error`, or `cancelled`).

### Global webhooks

Register via `POST /api/v1/webhook` to receive all events for all bots. Webhook registrations are persisted to the database and survive server restarts.

**Events:** `bot.joining`, `bot.in_call`, `bot.call_ended`, `bot.transcript_ready`, `bot.analysis_ready`, `bot.done`, `bot.error`, `bot.cancelled`

**Registration:**
```bash
curl -X POST http://localhost:8000/api/v1/webhook \
  -H "Authorization: Bearer sk_live_..." \
  -H "Content-Type: application/json" \
  -d '{"url": "https://your-app.com/hook", "events": ["bot.done", "bot.error"], "secret": "my-signing-secret"}'
```

**HMAC signing:** Pass `secret` when registering. All deliveries include two headers:
- `X-MeetingBot-Signature: sha256=<hmac-sha256>` — HMAC-SHA256 computed over `"{timestamp}.{body}"`
- `X-MeetingBot-Timestamp` — Unix timestamp (seconds) of when the delivery was created

Verify by recomputing the HMAC over `f"{X-MeetingBot-Timestamp}.{raw_body}"` with your secret. Reject deliveries where `abs(time.time() - int(X-MeetingBot-Timestamp)) > 300` to prevent replay attacks.

After **5 consecutive delivery failures** the webhook is automatically disabled. Re-enable it by deleting and re-registering.

**Error event fields:** When `event` is `bot.error`, the `data` object includes `error_code` (e.g. `timeout`, `no_audio_captured`, `browser_error`, `meeting_access_denied`), `error_message` (human-readable), and `retryable` (boolean). These are `null` for non-error events.

### WebSocket

Connect to `ws://host/api/v1/ws?token=<your-api-key-or-jwt>` for real-time events.
- Authenticated connections only receive events for their own bots.
- Send `ping` to keep the connection alive.
- Close code `4001` — auth required but no token provided.
- Close code `4003` — invalid token.
- Close code `4503` — database error during token lookup (transient; retry shortly).

> **Rate limits:** `POST /api/v1/auth/register` is limited to 3 requests/min per IP, `POST /api/v1/auth/login` to 5/min, and `POST /api/v1/bot` to 20/min. Exceeded limits return HTTP 429.

---

## Supported platforms

| Platform | Real bot | Notes |
|----------|----------|-------|
| Google Meet | ✅ | Full recording + transcription |
| Zoom | ✅ | Full recording + transcription |
| Microsoft Teams | ✅ | Full recording + transcription |
| Others | Demo mode | AI-generated sample transcript; `is_demo_transcript: true` in response |

---

## Deployment

### Docker (recommended)

```bash
docker compose up --build
```

The `docker-compose.yml` automatically starts a **PostgreSQL 16** service and wires the `DATABASE_URL` and `JWT_SECRET` from your `.env` file. Copy `.env.example` (or create `.env`) with at least:

```
JWT_SECRET=$(openssl rand -hex 32)
POSTGRES_PASSWORD=$(openssl rand -hex 16)
DATABASE_URL=postgresql://meetingbot:${POSTGRES_PASSWORD}@db:5432/meetingbot
```

### Railway / Heroku

Set environment variables and deploy. Add a **PostgreSQL plugin** in Railway — the `DATABASE_URL` is injected automatically and the app translates it to the correct asyncpg driver format with no extra configuration required.

### Manual

```bash
cd backend
pip install -r requirements.txt
pip install -r requirements-crypto.txt  # For USDC support
playwright install chromium
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

> **Note:** `requirements.txt` uses `bcrypt>=4.0.0` directly for password hashing. `passlib` is not required.
