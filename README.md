# MeetingBot — Recall.ai Clone

A fully functional [Recall.ai](https://recall.ai) clone that deploys bots into video meetings, generates transcripts, and produces AI-powered meeting intelligence using **Claude Opus 4.6**.

---

## Features

| Feature | Description |
|---------|-------------|
| 🤖 **Meeting Bots** | Deploy bots into Zoom, Google Meet, Teams, and more |
| 📝 **Transcripts** | Real-time transcript capture (simulated; plug in your SDK) |
| ✨ **AI Analysis** | Claude Opus 4.6 generates summaries, action items, decisions, key points |
| 🔔 **Webhooks** | Real-time HTTP callbacks for every bot lifecycle event |
| 📊 **Dashboard** | Clean web UI to manage bots and view results |
| 🔌 **REST API** | Mirrors the Recall.ai API surface — easy to swap in |

---

## Quick Start

### 1. Clone & configure

```bash
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
```

### 2. Run with Docker

```bash
docker compose up --build
```

### 3. Run locally

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open **http://localhost:8000** — the dashboard is served at the root.

---

## API Reference

Interactive docs: **http://localhost:8000/api/docs**

### Bots

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/bot` | Create a bot and start lifecycle |
| `GET` | `/api/v1/bot` | List all bots |
| `GET` | `/api/v1/bot/{id}` | Get bot details |
| `DELETE` | `/api/v1/bot/{id}` | Remove/cancel bot |
| `GET` | `/api/v1/bot/{id}/transcript` | Get meeting transcript |
| `POST` | `/api/v1/bot/{id}/analyze` | (Re-)run Claude analysis |

#### Create a bot

```bash
curl -X POST http://localhost:8000/api/v1/bot \
  -H "Content-Type: application/json" \
  -d '{
    "meeting_url": "https://zoom.us/j/123456789",
    "bot_name": "My Bot"
  }'
```

#### Bot lifecycle states

```
ready → joining → in_call → call_ended → done
                                        ↘ error
```

### Webhooks

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/webhook` | Register a webhook |
| `GET` | `/api/v1/webhook` | List webhooks |
| `DELETE` | `/api/v1/webhook/{id}` | Delete webhook |

#### Register a webhook

```bash
curl -X POST http://localhost:8000/api/v1/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://your-server.com/meetingbot-events",
    "events": ["bot.done", "bot.transcript_ready"],
    "secret": "your-signing-secret"
  }'
```

#### Webhook payload format

```json
{
  "event": "bot.done",
  "data": {
    "bot_id": "abc-123",
    "status": "done",
    "meeting_url": "https://zoom.us/j/123",
    "meeting_platform": "zoom"
  },
  "ts": "2025-03-05T12:00:00Z"
}
```

Webhook requests include a `X-MeetingBot-Signature: sha256=<hmac>` header when a secret is set.

---

## How it works

1. **POST /api/v1/bot** → creates a `Bot` row and starts a background asyncio task
2. The task transitions through states, firing webhooks at each step
3. After the meeting, it calls Claude to generate a realistic transcript (demo mode)
4. Claude then analyses the transcript → summary, action items, decisions, topics
5. All data is persisted in SQLite (swap for Postgres in production)

To connect real meeting platforms, replace the `generate_demo_transcript()` call in `bot_service.py` with your Zoom/Meet/Teams SDK integration.

---

## Architecture

```
MeetingBot/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app + lifespan
│   │   ├── config.py            # Settings (pydantic-settings)
│   │   ├── database.py          # SQLAlchemy async engine
│   │   ├── models/              # ORM models (Bot, Webhook)
│   │   ├── schemas/             # Pydantic request/response models
│   │   ├── api/                 # FastAPI routers
│   │   └── services/
│   │       ├── bot_service.py        # Bot lifecycle (asyncio task)
│   │       ├── intelligence_service.py  # Claude integration
│   │       └── webhook_service.py    # Webhook delivery
│   └── requirements.txt
├── frontend/                    # Vanilla JS dashboard
├── docker-compose.yml
└── .env.example
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | Yes | — | Claude API key |
| `BOT_SIMULATION_DURATION` | No | `60` | Simulated meeting length (seconds) |
| `DATABASE_URL` | No | SQLite | Database connection string |
| `SECRET_KEY` | No | dev value | App secret key |
