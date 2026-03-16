# MeetingBot API

**Version 2.0** — A stateless meeting bot API service with multi-tenant billing.

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
export CRYPTO_HD_SEED=<64-char hex>   # for USDC payments

docker compose up
```

API available at `http://localhost:8000`
Interactive docs at `http://localhost:8000/api/docs`
Web UI at `http://localhost:8000/register`

### 2. Register an account and get an API key

```bash
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com", "password": "yourpassword", "key_name": "Default"}'
# → {"account_id": "...", "email": "...", "api_key": "<your-api-key>", "message": "..."}
```

> **`key_name`** (optional, default `"Default"`) — a label for the first API key created with your account.

### 3. Top up credits

```bash
# Via Stripe — returns a checkout URL to complete payment
# amount_usd must be one of the values in STRIPE_TOP_UP_AMOUNTS (default: 10, 25, 50, 100)
curl -X POST http://localhost:8000/api/v1/billing/stripe/checkout \
  -H "Authorization: Bearer sk_live_..." \
  -H "Content-Type: application/json" \
  -d '{"amount_usd": 25, "success_url": "https://your-app.com/thanks", "cancel_url": "https://your-app.com/topup"}'

# Via USDC — get your unique deposit address (1 USDC = $1 credit)
curl http://localhost:8000/api/v1/billing/usdc/address \
  -H "Authorization: Bearer sk_live_..."
```

Or use the web UI at `/topup`.

### 4. Create a bot

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

### 5. Get results

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

> **Login note:** `POST /api/v1/auth/login` expects **`application/x-www-form-urlencoded`** (not JSON) with fields `username` (your email) and `password`. The returned JWT is for the web UI only; use your `sk_live_...` API key as a Bearer token for all other API calls.

### Billing
| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/billing/balance` | Current balance + last 50 transactions (each with `id`, `amount_usd`, `type`, `description`, `reference_id`, `created_at`) |
| `POST` | `/api/v1/billing/stripe/checkout` | Create Stripe Checkout session. Body: `{amount_usd, success_url?, cancel_url?}`. `amount_usd` must be one of the values in `STRIPE_TOP_UP_AMOUNTS`. |
| `POST` | `/api/v1/billing/stripe/webhook` | Stripe webhook receiver — register this URL in your Stripe dashboard for `checkout.session.completed` events |
| `GET` | `/api/v1/billing/usdc/address` | Get unique USDC/ERC-20 deposit address (1 USDC = $1 credit, credited within ~1 min) |

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

### Web UI
| Path | Description |
|------|-------------|
| `/register` | Create account |
| `/login` | Login |
| `/dashboard` | Balance, API keys, transaction history |
| `/topup` | Add credits (Stripe card or USDC) |

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
| `JWT_SECRET` | `change-me-in-production` | Secret for signing web UI JWT tokens |
| `JWT_EXPIRE_HOURS` | `24` | JWT token lifetime in hours |

### Billing
| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite+aiosqlite:///./meetingbot.db` | SQLAlchemy async URL (use `postgresql+asyncpg://...` on Railway) |
| `STRIPE_SECRET_KEY` | — | Stripe secret key (`sk_live_...`) |
| `STRIPE_WEBHOOK_SECRET` | — | Stripe webhook signing secret (`whsec_...`) |
| `STRIPE_TOP_UP_AMOUNTS` | `10,25,50,100` | Comma-separated USD top-up options |
| `CRYPTO_HD_SEED` | — | 64-char hex seed for HD wallet (generate once, keep secret) |
| `CRYPTO_RPC_URL` | — | Infura/Alchemy RPC endpoint for USDC monitoring |
| `USDC_CONTRACT` | `0xA0b8...eB48` | USDC ERC-20 contract address |
| `CREDIT_MARKUP` | `3.0` | Multiply raw AI cost by this factor when deducting credits |
| `MIN_CREDITS_USD` | `0.05` | Minimum balance required to create a bot |

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
Register via `POST /api/v1/webhook` to receive all events for all bots.

**Events:** `bot.joining`, `bot.in_call`, `bot.call_ended`, `bot.transcript_ready`, `bot.analysis_ready`, `bot.done`, `bot.error`, `bot.cancelled`

**HMAC signing:** Pass `secret` when registering. Deliveries include `X-MeetingBot-Signature: sha256=<hmac>`.

### WebSocket
Connect to `ws://host/api/v1/ws` for real-time events. Send `ping` to keep alive.

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

### Railway / Heroku

Set environment variables and deploy. Accounts/billing data persists in SQLite by default.
For production, set `DATABASE_URL` to a PostgreSQL connection string provided by Railway.

### Manual

```bash
cd backend
pip install -r requirements.txt
playwright install chromium
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

> **Note:** `requirements.txt` uses `bcrypt>=4.0.0` directly for password hashing.
> `passlib` is not required.
