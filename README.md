# MeetingBot API

**Version 2.1.0** — A stateless meeting bot API service with multi-tenant billing.

> **Last updated:** 2026-03-16 · **API version in Swagger UI:** 2.1.0 <!-- auto-updated on each release -->

Send bots into **Zoom**, **Google Meet**, and **Microsoft Teams** meetings to record, transcribe, and analyse them with **Claude** (Anthropic) or **Gemini** (Google) AI.

**Multi-tenant:** Each external service registers an account and gets its own API key. Pre-fund a credit balance via Stripe (card) or USDC (ERC-20) — credits are deducted automatically per bot run.

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
Interactive docs at `http://localhost:8000/api/docs`
Web UI at `http://localhost:8000/register`
Admin panel at `http://localhost:8000/admin` (admin accounts only)

### 2. Register an account and get an API key

```bash
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com", "password": "yourpassword", "key_name": "Default"}'
# → {"account_id": "...", "email": "...", "api_key": "<your-api-key>", "message": "..."}
```

> **`key_name`** (optional, default `"Default"`) — a label for the first API key created with your account.

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

> **USDC deposits — how attribution works:** Each user registers their Ethereum wallet on their account (`PUT /api/v1/auth/wallet`). The admin sets a single platform collection wallet via `/admin`. When a user sends USDC to the platform wallet, the system matches the `from` address to the user's registered wallet and credits their account automatically. If no platform wallet is configured, users get per-user HD-derived addresses (requires `CRYPTO_HD_SEED`).

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

Response:
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "joining",
  "meeting_url": "https://meet.google.com/abc-defg-hij",
  "created_at": "2026-03-15T10:00:00Z"
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

## Webhook payload

```json
{
  "event": "bot.done",
  "ts": "2026-03-15T11:00:00Z",
  "data": {
    "bot_id": "550e8400-...",
    "status": "done",
    "participants": ["Alice", "Bob", "Carol"],
    "duration_seconds": 3612,
    "transcript": [
      { "speaker": "Alice", "text": "Let's kick off the review.", "timestamp": 2.0 }
    ],
    "analysis": {
      "summary": "The team reviewed sprint progress.",
      "key_points": ["Auth module complete", "Dashboard work next"],
      "action_items": [{ "task": "Set up staging access", "assignee": "Alice" }],
      "decisions": ["Use virtual scrolling for performance"],
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
    "ai_usage": { "total_tokens": 4200, "total_cost_usd": 0.021 }
  }
}
```

---

## API Reference

### Auth & Accounts
| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/auth/register` | Create account → returns first API key. Body: `{email, password, key_name?}` |
| `POST` | `/api/v1/auth/login` | Email+password (OAuth2 **form data**: `username`, `password`) → JWT for web UI |
| `GET` | `/api/v1/auth/me` | Account info + credit balance |
| `POST` | `/api/v1/auth/keys` | Generate a new named API key. Body: `{name?}` |
| `GET` | `/api/v1/auth/keys` | List active API keys |
| `DELETE` | `/api/v1/auth/keys/{id}` | Revoke an API key |
| `GET` | `/api/v1/auth/wallet` | Get your registered Ethereum wallet address |
| `PUT` | `/api/v1/auth/wallet` | Set or update your Ethereum wallet address. Body: `{wallet_address}`. Required for USDC deposits to the platform wallet |

> **Login note:** `POST /api/v1/auth/login` expects **`application/x-www-form-urlencoded`** (not JSON) with fields `username` (your email) and `password`. The returned JWT is for the web UI only; use your `sk_live_...` API key as a Bearer token for all other API calls.

### Billing
| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/billing/balance` | Current balance + last 50 transactions (each with `id`, `amount_usd`, `type`, `description`, `reference_id`, `created_at`) |
| `POST` | `/api/v1/billing/stripe/checkout` | Create Stripe Checkout session. Body: `{amount_usd, success_url?, cancel_url?}`. `amount_usd` must be one of the values in `STRIPE_TOP_UP_AMOUNTS`. A pending record is stored immediately; credits are applied on webhook confirmation. |
| `POST` | `/api/v1/billing/stripe/webhook` | Stripe webhook receiver — register this URL in your Stripe dashboard for `checkout.session.completed` events |
| `GET` | `/api/v1/billing/usdc/address` | Get USDC/ERC-20 deposit address (platform wallet if admin-configured, otherwise HD-derived per-user address). 1 USDC = $1 credit, credited within ~1 min |

**Transaction types** (the `type` field in balance transactions):
| Type | Meaning |
|------|---------|
| `stripe_topup` | Credits added via Stripe card payment |
| `usdc_topup` | Credits added via USDC deposit |
| `bot_usage` | Credits deducted on bot completion (raw AI cost × `CREDIT_MARKUP`) |

### Bots
| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/bot` | Create a bot & join a meeting |
| `GET` | `/api/v1/bot` | List bots (scoped to your account). Query params: `limit`, `offset`, `status` |
| `GET` | `/api/v1/bot/stats` | Aggregate counts by status: `{total, active, done, error, by_status}` |
| `GET` | `/api/v1/bot/{id}` | Full details (transcript, analysis) |
| `DELETE` | `/api/v1/bot/{id}` | Stop & remove a bot |
| `GET` | `/api/v1/bot/{id}/transcript` | Raw transcript only |
| `GET` | `/api/v1/bot/{id}/recording` | Download WAV audio |
| `GET` | `/api/v1/bot/{id}/highlight` | Curated highlights |
| `POST` | `/api/v1/bot/{id}/analyze` | Re-run AI analysis |
| `POST` | `/api/v1/bot/{id}/ask` | Q&A on the transcript |
| `POST` | `/api/v1/bot/{id}/followup-email` | Draft follow-up email |
| `GET` | `/api/v1/bot/{id}/export/markdown` | Export as Markdown |
| `GET` | `/api/v1/bot/{id}/export/pdf` | Export as PDF |
| `POST` | `/api/v1/webhook` | Register global webhook |
| `GET` | `/api/v1/webhook` | List webhooks |
| `DELETE` | `/api/v1/webhook/{id}` | Remove webhook |
| `POST` | `/api/v1/webhook/{id}/test` | Test webhook delivery |
| `GET` | `/api/v1/templates` | List analysis templates |
| `GET` | `/api/v1/analytics` | Aggregate stats (bots, durations, AI cost) |
| `GET` | `/api/v1/action-items/stats` | Aggregate action-item counts by assignee |
| `GET` | `/api/health` or `/health` | Health check |

### Admin (requires admin account)
| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/admin/wallet` | Get the current platform USDC collection wallet address |
| `PUT` | `/api/v1/admin/wallet` | Set or update the platform USDC collection wallet address. Body: `{wallet_address}` |
| `GET` | `/api/v1/admin/config` | List all platform configuration values |

> **Admin access:** Accounts listed in `ADMIN_EMAILS` (comma-separated env var) or accounts with `is_admin=true` in the database can access these endpoints. All other users receive a 403 error.

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

Or use the admin web UI at `/admin` to manage the wallet address through a form.

### Web UI
| Path | Description |
|------|-------------|
| `/register` | Create account |
| `/login` | Login |
| `/dashboard` | Balance, API keys, transaction history |
| `/topup` | Add credits (Stripe card or USDC) |
| `/admin` | Platform administration — manage USDC collection wallet (admin only) |

Full interactive docs (with request/response examples): `GET /api/docs`
Alternative ReDoc view: `GET /api/redoc`
Raw OpenAPI JSON: `GET /api/openapi.json`

---

## Bot creation options

```json
{
  "meeting_url": "https://meet.google.com/...",   // required
  "bot_name": "MeetingBot",                        // display name in meeting
  "webhook_url": "https://your-app.com/hook",      // where to POST results when done
  "join_at": "2026-03-15T14:00:00Z",               // schedule for future join
  "analysis_mode": "full",                         // "full" | "transcript_only"
  "template": "default",                           // see templates below
  "prompt_override": "Custom AI prompt...",        // overrides template
  "vocabulary": ["ProductName", "TechTerm"],       // transcription hints
  "respond_on_mention": true,                      // bot replies when name mentioned
  "mention_response_mode": "text",                 // "text" | "voice" | "both"
  "tts_provider": "edge",                          // "edge" | "gemini"
  "start_muted": false,
  "live_transcription": false,                     // stream transcript in real-time
  "metadata": { "your_key": "your_value" }         // pass-through, echoed in responses
}
```

---

## Templates

Pass `template` in bot creation. Use `prompt_override` for a fully custom prompt.

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

## Environment variables

### AI providers
| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Claude API key (takes precedence over Gemini) |
| `GEMINI_API_KEY` | — | Gemini API key (transcription + analysis) |

### Auth
| Variable | Default | Description |
|----------|---------|-------------|
| `API_KEY` | — | Legacy superadmin key (bypasses per-user auth; leave empty to use accounts only) |
| `JWT_SECRET` | auto-generated | Secret for signing web UI JWT tokens. Generate a stable value with `openssl rand -hex 32`. If unset, a random secret is generated on each startup (sessions are invalidated on every restart) |
| `JWT_EXPIRE_HOURS` | `24` | JWT token lifetime in hours |

### Billing
| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite+aiosqlite:///./meetingbot.db` | SQLAlchemy async URL. The bundled `docker-compose.yml` sets this to PostgreSQL automatically via the `db` service. |
| `STRIPE_SECRET_KEY` | — | Stripe secret key (`sk_live_...`) |
| `STRIPE_WEBHOOK_SECRET` | — | Stripe webhook signing secret (`whsec_...`) |
| `STRIPE_TOP_UP_AMOUNTS` | `10,25,50,100` | Comma-separated USD top-up options |
| `CRYPTO_HD_SEED` | — | 64-char hex seed for HD wallet (generate once, keep secret). Not required if the admin sets a platform wallet via `/admin` |
| `CRYPTO_RPC_URL` | — | Infura/Alchemy RPC endpoint for USDC monitoring |
| `USDC_CONTRACT` | `0xA0b8...eB48` | USDC ERC-20 contract address |
| `CREDIT_MARKUP` | `3.0` | Multiply raw AI cost by this factor when deducting credits |
| `MIN_CREDITS_USD` | `0.05` | Minimum balance required to create a bot |

> **USDC wallet configuration:** The platform USDC collection wallet can be set by an admin via `PUT /api/v1/admin/wallet` or the `/admin` web UI. When set, this wallet address is returned to all users at `GET /api/v1/billing/usdc/address`, overriding the HD-derived per-user addresses. This means you can accept USDC without configuring `CRYPTO_HD_SEED` — just set the wallet via the admin panel.

### Bot settings
| Variable | Default | Description |
|----------|---------|-------------|
| `CORS_ORIGINS` | `*` | Comma-separated allowed origins |
| `MAX_CONCURRENT_BOTS` | `3` | Max simultaneous browser bots |
| `BOT_ADMISSION_TIMEOUT` | `300` | Seconds to wait for host to admit the bot |
| `BOT_MAX_DURATION` | `7200` | Max meeting duration in seconds |
| `BOT_ALONE_TIMEOUT` | `300` | Seconds alone before auto-leave |
| `BOT_JOIN_MAX_RETRIES` | `2` | Join retry attempts |
| `TRANSCRIPTION_LANGUAGE` | — | BCP-47 language code (e.g. `en`, `es`). Empty = auto |
| `WEBHOOK_TIMEOUT_SECONDS` | `10` | HTTP timeout for webhook delivery |

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
| `error` | An unrecoverable error occurred |
| `cancelled` | Bot was stopped via `DELETE /api/v1/bot/{id}` |

The bot auto-leaves when it has been the only participant for `BOT_ALONE_TIMEOUT` seconds (default 5 min).

---

## Webhooks

### Per-bot webhook
Pass `webhook_url` when creating a bot. A single POST with full results is sent when the bot reaches a terminal state (`done`, `error`, or `cancelled`).

### Global webhooks
Register via `POST /api/v1/webhook` to receive all events for all bots. Webhook registrations are persisted to the database and survive server restarts.

**Events:** `bot.joining`, `bot.in_call`, `bot.call_ended`, `bot.transcript_ready`, `bot.analysis_ready`, `bot.done`, `bot.error`, `bot.cancelled`

**HMAC signing:** Pass `secret` when registering. Deliveries include `X-MeetingBot-Signature: sha256=<hmac>`. After 5 consecutive delivery failures the webhook is automatically disabled.

### WebSocket
Connect to `ws://host/api/v1/ws?token=<your-api-key-or-jwt>` for real-time events.
Authenticated connections only receive events for their own bots.
Send `ping` to keep alive. Returns WebSocket close code `4001` if auth is required
but no token is provided, or `4003` for an invalid token.

> **Rate limits:** `POST /api/v1/auth/register` is limited to 3 requests/min per IP,
> `POST /api/v1/auth/login` to 5/min, and `POST /api/v1/bot` to 20/min. Exceeded
> limits return HTTP 429.

---

## Supported platforms

| Platform | Real bot | Notes |
|----------|----------|-------|
| Google Meet | ✅ | Full recording + transcription |
| Zoom | ✅ | Full recording + transcription |
| Microsoft Teams | ✅ | Full recording + transcription |
| Others | Demo mode | AI-generated sample transcript |

---

## Deployment

### Docker (recommended)

```bash
docker compose up --build
```

The `docker-compose.yml` automatically starts a **PostgreSQL 16** service and wires the
`DATABASE_URL` and `JWT_SECRET` from your `.env` file. Copy `.env.example` (or create `.env`)
with at least:

```
JWT_SECRET=$(openssl rand -hex 32)
POSTGRES_PASSWORD=$(openssl rand -hex 16)
DATABASE_URL=postgresql://meetingbot:${POSTGRES_PASSWORD}@db:5432/meetingbot
```

### Railway / Heroku

Set environment variables and deploy. Add a **PostgreSQL plugin** in Railway — the
`DATABASE_URL` is injected automatically and the app translates it to the correct asyncpg
driver format with no extra configuration required.

### Manual

```bash
cd backend
pip install -r requirements.txt
playwright install chromium
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

> **Note:** `requirements.txt` uses `bcrypt>=4.0.0` directly for password hashing.
> `passlib` is not required.
