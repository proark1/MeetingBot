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
| `BOT_JOIN_MAX_RETRIES` | `2` | Number of retry attempts if the bot fails to join before being admitted. |
| `BOT_JOIN_RETRY_DELAY_S` | `30` | Seconds to wait between join retry attempts. |
| `BOT_NAME_DEFAULT` | `MeetingBot` | Display name shown inside the meeting |
| `BOT_ADMISSION_TIMEOUT` | `300` | Seconds to wait for the host to admit the bot before giving up |
| `BOT_MAX_DURATION` | `7200` | Maximum meeting recording length in seconds (2 hours) |
| `BOT_ALONE_TIMEOUT` | `300` | Seconds the bot stays alone before leaving automatically (5 minutes) |
| `DATABASE_URL` | `sqlite+aiosqlite:///./meetingbot.db` | SQLAlchemy async DB URL. Connection timeout is 15 s. |
| `SECRET_KEY` | *(dev default)* | Change in production |
| `SLACK_WEBHOOK_URL` | *(empty)* | Slack Incoming Webhook URL — post meeting summaries to Slack after each meeting. |
| `SMTP_HOST` | *(empty)* | SMTP server for email summaries. Leave empty to disable. |
| `SMTP_PORT` | `587` | SMTP port |
| `SMTP_USER` | *(empty)* | SMTP username |
| `SMTP_PASS` | *(empty)* | SMTP password |
| `SMTP_FROM` | `meetingbot@example.com` | From address for emails |
| `BASE_URL` | *(empty)* | Public URL used in share links, e.g. `https://meetingbot.example.com` |
| `NOTION_API_KEY` | *(empty)* | Notion API key — push meeting summaries to a Notion database. |
| `NOTION_DATABASE_ID` | *(empty)* | Notion database ID to write meeting pages into. |
| `LINEAR_API_KEY` | *(empty)* | Linear API key — push action items as Linear issues. |
| `LINEAR_TEAM_ID` | *(empty)* | Linear team ID to create issues in. |
| `DIGEST_EMAIL` | *(empty)* | Comma-separated recipients for the weekly digest email (Mondays 09:00 UTC). Requires `SMTP_HOST`. |
| `RECORDING_RETENTION_DAYS` | `30` | Auto-delete WAV recordings older than this many days. Set to `0` to keep recordings forever. |

---

## API Reference

All endpoints are prefixed with `/api/v1`. Requests to unknown `/api/v1/…` paths return a JSON `404` response — not the frontend HTML page.

### Bots

#### Create a bot — join a meeting

```
POST /api/v1/bot
```

**Body:**
```json
{
  "meeting_url": "https://meet.google.com/abc-defg-hij",
  "bot_name": "MeetingBot",
  "join_at": null,
  "notify_email": "you@example.com",
  "template_id": "seed-sales",
  "vocabulary": ["Acme", "SKU-123"],
  "analysis_mode": "full",
  "respond_on_mention": true,
  "mention_response_mode": "text",
  "tts_provider": "edge",
  "start_muted": true,
  "live_transcription": false,
  "extra_metadata": {}
}
```

`analysis_mode` — controls post-meeting processing:
- `"full"` *(default)* — runs full AI analysis: summary, key points, action items, smart chapters, sentiment, speaker stats, and all post-meeting notifications (email, Slack, Notion, Linear).
- `"transcript_only"` — skips all AI processing and returns only the raw speaker-labelled transcript. Faster completion, lower cost, full privacy. Speaker stats are still computed locally. This setting is respected on all exit paths, including cancel and error.

`meeting_url` — must be a publicly reachable URL. Requests resolving to private, loopback, or link-local addresses are rejected to prevent SSRF.

`start_muted` — whether the bot joins with its microphone muted (default `false`). With the default of `false` the bot joins with the mic already on, so TTS voice replies play immediately without toggling the mic. Set to `true` if you want the bot to join muted and only unmute briefly while it speaks.

`respond_on_mention` — when `true` (default), the bot monitors live captions during the call and replies whenever its name is mentioned. Responses are debounced to once every 8 seconds.

`mention_response_mode` — controls how the bot replies when mentioned:
- `"text"` *(default)* — sends a message to the meeting chat.
- `"voice"` — speaks the reply aloud via TTS so all participants hear it. Requires the PulseAudio virtual mic to be available.
- `"both"` — sends a chat message and speaks simultaneously.

`tts_provider` — TTS engine used for voice responses (only relevant when `mention_response_mode` is `"voice"` or `"both"`):
- `"edge"` *(default)* — Microsoft Edge TTS: fast (~300 ms), free, no extra API key required.
- `"gemini"` — Google Gemini TTS (`gemini-2.5-flash-preview-tts`): more natural voice, uses your `GEMINI_API_KEY`.

`live_transcription` — when `true`, the bot transcribes audio in 15-second rolling chunks **during** the call using Gemini inline audio. This gives the bot real-time meeting context (it can answer "what did we just discuss?") and enables voice-based bot-name detection without relying on DOM captions. Requires `GEMINI_API_KEY`. Default `false` — audio is only transcribed after the meeting ends.

`template_id` — optional ID of a meeting template (see `/api/v1/templates`). Templates customise the AI analysis prompt.
`vocabulary` — optional list of domain-specific terms to hint at during transcription.
`extra_metadata` — arbitrary JSON object stored with the bot record and returned in all responses.


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

Cancels the bot if still in a call. Returns `204 No Content` immediately. The status is set to `call_ended` right away (so the UI updates instantly), then the lifecycle task continues in the background to:

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

> **Security:** Webhook URLs must be publicly reachable. URLs targeting `localhost`, `127.0.0.1`, RFC-1918 private ranges (`10.x`, `172.16.x`, `192.168.x`), link-local addresses (`169.254.x`), or hostnames that resolve to any of these are rejected with HTTP 400 to prevent SSRF attacks.

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

#### Test a webhook

```
POST /api/v1/webhook/{webhook_id}/test
```

Sends a sample `bot.test` payload to the webhook URL immediately. Returns `{"status_code": 200, "url": "..."}` on success or 502 if the endpoint is unreachable. Webhooks are auto-disabled after 5 consecutive delivery failures.

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

### Search

```
GET /api/v1/search?q=keyword        # full-text search across all transcripts
```

Returns matching meetings with highlighted snippets.

---

### Analytics

```
GET /api/v1/analytics               # sentiment, topics, platform breakdown, participant stats
```

Returns: `total_meetings`, `avg_duration_s`, `avg_duration_fmt`, `sentiment_distribution`,
`meetings_per_day` (last 30 days), `top_topics`, `top_participants`, `platform_breakdown`.

---

### Highlights

```
POST   /api/v1/bot/{id}/highlight   # bookmark a transcript moment
GET    /api/v1/bot/{id}/highlight   # list highlights for a meeting
DELETE /api/v1/bot/highlight/{id}   # remove a highlight
```

---

### Share

```
GET /api/v1/share/{token}           # public read-only meeting report (no auth required)
```

Each bot gets a unique `share_token` (24-byte URL-safe random token) at creation. Copy the share link from the detail page.

---

### Recording

```
GET /api/v1/bot/{id}/recording      # download meeting audio (WAV)
```

Available when real audio was captured (not demo mode).

---

### Ask Anything

```
POST /api/v1/bot/{id}/ask
{ "question": "What were the main decisions?" }
```

Asks Gemini a free-form question about the meeting transcript.

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

---

## Action Items

Cross-meeting action item tracking. Action items extracted by AI from each meeting are automatically stored in the database and can be queried across all meetings.

```
GET  /api/v1/action-items?done=false&assignee=Alice&limit=100
PATCH /api/v1/action-items/{id}   — toggle done, update assignee/due_date
GET  /api/v1/action-items/stats   — {total, done, pending} (SQL-aggregated, O(1))
```

---

## Meeting Templates

Templates let you customise the AI analysis prompt per meeting type.

```
GET    /api/v1/templates                — list all templates (built-ins + custom)
GET    /api/v1/templates/default-prompt — return the raw default analysis prompt text
POST   /api/v1/templates                — create custom template {name, description, prompt_override}
DELETE /api/v1/templates/{id}           — delete a custom template (built-ins cannot be deleted)
```

**Built-in templates** (always available, id prefix `seed-`):

| ID | Name | Best for |
|----|------|----------|
| `seed-default` | Default (General) | Baseline prompt — use this as a starting point for custom templates |
| `seed-sales` | Sales Call | Buying signals, objections, deal stage |
| `seed-standup` | Daily Standup | Blockers, yesterday / today items |
| `seed-1on1` | 1:1 Meeting | Feedback, career growth areas |
| `seed-retro` | Sprint Retrospective | Went well / poorly, process improvements |
| `seed-kickoff` | Client Kickoff | Scope, deliverables, risks, success metrics |
| `seed-allhands` | All-Hands / Town Hall | Announcements, employee Q&A, leadership commitments |
| `seed-postmortem` | Incident Post-Mortem | Timeline, root causes, customer impact, remediation |
| `seed-interview` | Interview / Hiring Panel | Competency ratings, strengths, concerns, recommendation |
| `seed-design-review` | Design Review | Design decisions, rejected alternatives, open questions |

**Creating a custom template** — write any prompt you like as `prompt_override`:

```
1. Start with a role:   "You are a sales coach."
2. Add instruction:     "Analyze this meeting transcript and return ONLY valid JSON."
3. Define JSON shape:   include standard fields + any extras you need
                        (e.g. buying_signals, blockers, root_causes)
```

Fetch `GET /api/v1/templates/default-prompt` to get the baseline prompt text as a starting point,
then modify and POST as a new custom template.

Pass `template_id` when creating a bot to activate its custom analysis prompt.
