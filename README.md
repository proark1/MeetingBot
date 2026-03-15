# MeetingBot API

**Version 2.0** — A stateless meeting bot API service.

Send bots into **Zoom**, **Google Meet**, and **Microsoft Teams** meetings to record, transcribe, and analyse them with **Claude** (Anthropic) or **Gemini** (Google) AI.

**No database required.** Results are returned via webhook or polling. You store the data.

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
export API_KEY=your-secret-key        # optional, for auth

docker compose up
```

API available at `http://localhost:8000`
Interactive docs at `http://localhost:8000/api/docs`

### 2. Create a bot

```bash
curl -X POST http://localhost:8000/api/v1/bot \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-secret-key" \
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

### 3. Get results

Poll until `status` is `done`:
```bash
curl http://localhost:8000/api/v1/bot/550e8400-... \
  -H "Authorization: Bearer your-secret-key"
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

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/bot` | Create a bot & join a meeting |
| `GET` | `/api/v1/bot` | List all in-memory bots |
| `GET` | `/api/v1/bot/{id}` | Full details (transcript, analysis) |
| `DELETE` | `/api/v1/bot/{id}` | Stop & remove a bot |
| `GET` | `/api/v1/bot/{id}/transcript` | Raw transcript only |
| `GET` | `/api/v1/bot/{id}/recording` | Download WAV audio |
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
| `GET` | `/api/health` | Health check |

Full interactive docs: `GET /api/docs`

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

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Claude API key (takes precedence over Gemini) |
| `GEMINI_API_KEY` | — | Gemini API key (transcription + analysis) |
| `API_KEY` | — | Bearer token required on all requests (leave empty to disable) |
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
ready → joining → in_call → call_ended → done
                                       ↘ error
                                       ↘ cancelled
```

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

Set environment variables and deploy. No database needed — everything is in-memory.

### Manual

```bash
cd backend
pip install -r requirements.txt
playwright install chromium
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
