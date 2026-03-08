# MeetingBot

A self-hosted meeting bot that joins Google Meet, Zoom, and Microsoft Teams calls, records audio, transcribes it with Gemini, and analyses the transcript with Gemini.

---

## Quick Start

```bash
# Required environment variable
GEMINI_API_KEY=your-key docker compose up
```

The web UI (Reports) is available at `http://localhost:8080`.
The API is available at `http://localhost:8080/api/v1`.
Interactive docs (Swagger UI): `http://localhost:8080/api/docs`

---

## Configuration

Set these in a `.env` file or as environment variables:

| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | *(required)* | Google Gemini API key — used for transcription and analysis. |
| `API_KEY` | *(empty = no auth)* | If set, all `/api/v1/*` requests must include `Authorization: Bearer <API_KEY>`. Leave empty to disable. |
| `CORS_ORIGINS` | `*` | Comma-separated allowed origins, e.g. `https://app.example.com,https://admin.example.com`. `*` allows all origins. |
| `MAX_CONCURRENT_BOTS` | `3` | Maximum number of browser bots running simultaneously. Returns 429 if exceeded. |
| `BOT_NAME_DEFAULT` | `MeetingBot` | Display name shown inside the meeting |
| `BOT_ADMISSION_TIMEOUT` | `300` | Seconds to wait for the host to admit the bot before giving up |
| `BOT_MAX_DURATION` | `7200` | Maximum meeting recording length in seconds (2 hours) |
| `BOT_ALONE_TIMEOUT` | `300` | Seconds the bot stays alone before leaving automatically (5 minutes) |
| `DATABASE_URL` | `sqlite+aiosqlite:///./meetingbot.db` | SQLAlchemy async DB URL |
| `SECRET_KEY` | *(dev default)* | Change in production |

---

## API Reference

All endpoints are prefixed with `/api/v1`.

### Bots

#### Create a bot — join a meeting

```
POST /api/v1/bot
```

**Body:**
```json
{
  "meeting_url": "https://meet.google.com/abc-defg-hij",
  "bot_name": "1tab.ai Notetaker",
  "join_at": null,
  "extra_metadata": {}
}
```

**Supported platforms** (auto-detected from URL):
- `meet.google.com` → Google Meet
- `zoom.us` → Zoom
- `teams.microsoft.com` → Microsoft Teams

**Response** (`201 Created`):
```json
{
  "id": "ea69a8bc-491a-4ac2-894c-2465904e3b0a",
  "meeting_url": "https://meet.google.com/abc-defg-hij",
  "meeting_platform": "google_meet",
  "bot_name": "1tab.ai Notetaker",
  "status": "joining",
  "created_at": "2026-03-07T03:01:48Z",
  "updated_at": "2026-03-07T03:01:48Z",
  "started_at": null,
  "ended_at": null,
  "participants": [],
  "transcript": [],
  "analysis": null,
  "error_message": null,
  "extra_metadata": {}
}
```

---

#### Get bot status

```
GET /api/v1/bot/{bot_id}
```

Poll this until `status` is `done` (or `error`). The full `transcript` and `analysis` are included in the response once available.

**Bot lifecycle statuses:**

| Status | Meaning |
|---|---|
| `joining` | Bot is opening the browser and navigating to the meeting |
| `in_call` | Host admitted the bot — recording in progress |
| `call_ended` | Meeting ended — transcription and analysis running |
| `done` | Transcript and analysis are ready |
| `cancelled` | Bot was stopped via `DELETE` — record kept, transcript accessible if captured |
| `error` | Something failed — check `error_message` |

**Auto-leave behaviour:**

The bot automatically leaves in two cases, both controlled by `BOT_ALONE_TIMEOUT` (default 5 minutes):

- **Empty room on join** — the bot is admitted but no other participants are present. If no one joins within 5 minutes, the bot leaves.
- **Everyone left** — participants were in the call but all left. If no one rejoins within 5 minutes, the bot leaves.

If a participant rejoins before the timer expires, the timer resets and the bot stays. The timeout is configurable via `BOT_ALONE_TIMEOUT`.

---

#### Get transcript

```
GET /api/v1/bot/{bot_id}/transcript
```

Returns `425 Too Early` if the meeting hasn't ended yet.

**Response:**
```json
{
  "bot_id": "ea69a8bc-491a-4ac2-894c-2465904e3b0a",
  "transcript": [
    { "speaker": "Alice", "text": "Good morning everyone.", "timestamp": 2.0 },
    { "speaker": "Bob",   "text": "Morning! Ready to start.", "timestamp": 6.5 }
  ]
}
```

Each entry:
- `speaker` — name detected from audio, or `"Participant 1"` etc.
- `text` — what was said
- `timestamp` — seconds from the start of the recording

---

#### Get analysis

Analysis is also returned in the main `GET /api/v1/bot/{bot_id}` response inside the `analysis` field once status is `done`. To re-run analysis on demand:

```
POST /api/v1/bot/{bot_id}/analyze
```

**Response:**
```json
{
  "summary": "The team reviewed sprint progress and planned dashboard performance improvements.",
  "key_points": ["Auth module completed", "Dashboard has performance issues"],
  "action_items": [
    { "task": "Implement virtual scrolling", "assignee": "Bob", "due_date": "next sprint" }
  ],
  "decisions": ["Use Lighthouse for performance baselines"],
  "next_steps": ["Bob owns virtual scrolling", "Carol creates skeleton mockups"],
  "sentiment": "positive",
  "topics": ["sprint review", "performance", "authentication"]
}
```

---

#### List all bots

```
GET /api/v1/bot?limit=20&offset=0&status=done
```

Returns lightweight summaries — `transcript` and `analysis` are omitted to keep responses small. Use `GET /api/v1/bot/{id}` for the full payload.

- `status` filter is optional: `joining`, `in_call`, `call_ended`, `done`, `error`
- `is_demo_transcript: true` is set when no real audio was captured and a Gemini-generated transcript was used as fallback

---

#### Remove a bot

```
DELETE /api/v1/bot/{bot_id}
```

Cancels the bot if still in a call. Returns `204 No Content` immediately. The bot record is **kept** and the lifecycle task continues in the background to:

1. Transcribe any audio that was captured before cancellation
2. Fall back to a Gemini-generated demo transcript if no audio was recorded
3. Run analysis on the transcript
4. Set status to `cancelled` when done

This means `GET /bot/{id}/transcript` will always return a non-empty transcript eventually, even for cancelled bots.

---

#### Stats

```
GET /api/v1/bot/stats
```

```json
{
  "total": 42,
  "active": 3,
  "done": 38,
  "error": 1,
  "by_status": { "done": 38, "in_call": 2, "joining": 1, "error": 1 }
}
```

---

### Webhooks

Register a URL to receive push notifications for bot lifecycle events.

#### Register a webhook

```
POST /api/v1/webhook
```

```json
{
  "url": "https://your-server.com/meetingbot-events",
  "events": ["bot.in_call", "bot.done", "bot.transcript_ready", "bot.analysis_ready"],
  "secret": "optional-hmac-secret"
}
```

Pass `"events": ["*"]` to receive all events.

**Available events:**

| Event | Fired when |
|---|---|
| `bot.joining` | Bot starts navigating to the meeting |
| `bot.in_call` | Host admitted the bot |
| `bot.call_ended` | Meeting ended, transcription starting |
| `bot.transcript_ready` | Transcript is available |
| `bot.analysis_ready` | Analysis is available |
| `bot.done` | Everything complete |
| `bot.error` | Bot encountered an error |

**Payload sent to your URL:**
```json
{
  "event": "bot.transcript_ready",
  "data": {
    "bot_id": "ea69a8bc-491a-4ac2-894c-2465904e3b0a",
    "bot_name": "1tab.ai Notetaker",
    "status": "call_ended",
    "meeting_url": "https://meet.google.com/abc-defg-hij",
    "meeting_platform": "google_meet",
    "ts": "2026-03-07T03:45:00Z"
  }
}
```

After receiving `bot.transcript_ready` or `bot.done`, fetch the data:

```
GET /api/v1/bot/{bot_id}
```

---

#### List / delete webhooks

```
GET    /api/v1/webhook
GET    /api/v1/webhook/{webhook_id}
DELETE /api/v1/webhook/{webhook_id}
```

---

### Debug

Browser screenshots and HTML page dumps saved when a bot fails to join a meeting are accessible via:

```
GET /api/v1/debug/screenshots              # list all files (name, type, size, modified)
GET /api/v1/debug/screenshots/{filename}   # view/download a PNG or HTML dump
```

These are also visible in the dashboard under the **Debug** tab.

---

### Real-time (WebSocket)

Connect once to receive all bot events in real time:

```
ws://localhost:8080/ws
```

Send `ping` to receive `pong` (keepalive). Events arrive as JSON:

```json
{ "event": "bot.in_call", "data": { "bot_id": "...", ... } }
```

---

## Typical Integration Flow

```
1.  POST /api/v1/bot          → get bot_id, status = "joining"
2.  (host admits bot in call)  → bot status → "in_call"
3.  (meeting ends)             → bot status → "call_ended" → "done"
4a. Poll: GET /api/v1/bot/{id} until status == "done"
  OR
4b. Webhook: receive "bot.done" event, then GET /api/v1/bot/{id}
5.  Read response.transcript and response.analysis
```

---

## Supported Platforms

| Platform | Join as guest | Audio recording | Transcription |
|---|---|---|---|
| Google Meet | Yes | Yes | Yes (Gemini) |
| Zoom | Yes | Yes | Yes (Gemini) |
| Microsoft Teams | Yes | Yes | Yes (Gemini) |
| Others | — | — | Demo transcript only |
