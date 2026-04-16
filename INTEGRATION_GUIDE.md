# Integration Guide — JustHereToListen.io Bot API

How to send a meeting bot, track its status, and receive results.

---

## Quick Start

### 1. Register & Get API Key

```bash
# Register
curl -X POST https://your-instance.railway.app/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "you@company.com", "password": "YourPassword123"}'

# Create an API key (use the JWT from registration)
curl -X POST https://your-instance.railway.app/api/v1/api-keys \
  -H "Authorization: Bearer <JWT>" \
  -H "Content-Type: application/json" \
  -d '{"name": "Production", "mode": "live"}'
# Returns: { "key": "sk_live_abc123..." }
```

### 2. Send a Bot to a Meeting

```bash
curl -X POST https://your-instance.railway.app/api/v1/bot \
  -H "Authorization: Bearer sk_live_abc123..." \
  -H "Content-Type: application/json" \
  -d '{
    "meeting_url": "https://www.onepizza.io/join/abc-defg-hij",
    "bot_name": "1tab.ai Notetaker",
    "webhook_url": "https://your-backend.com/webhook/bot-status",
    "live_transcription": true
  }'
```

**Response:**
```json
{
  "id": "61d4b9aa-76f0-412e-a3b0-f2f5d79d4507",
  "status": "joining",
  "meeting_url": "https://www.onepizza.io/join/abc-defg-hij",
  "meeting_platform": "onepizza",
  "bot_name": "1tab.ai Notetaker",
  "created_at": "2026-03-21T00:00:00Z"
}
```

### 3. Receive Status via Webhooks (Recommended)

The bot sends webhook POST requests to your `webhook_url` at each status change:

| Event | When | What to Do |
|-------|------|------------|
| `bot.joining` | Bot is loading the meeting page | Show "Connecting..." in UI |
| `bot.in_call` | Bot is in the meeting, recording | Show "Recording" in UI |
| `bot.call_ended` | Meeting ended, processing | Show "Processing..." |
| `bot.transcript_ready` | Transcript available | Fetch transcript |
| `bot.analysis_ready` | AI analysis complete | Fetch analysis |
| `bot.done` | Fully complete | Show results |
| `bot.error` | Something failed | Show error, offer retry |
| `bot.cancelled` | Bot was deleted/cancelled | Clean up |
| `bot.live_transcript` | New voice transcript entry (~1 s after speech ends) | Stream into your UI / agent |
| `bot.live_chat_message` | New chat message captured from the meeting chat panel | Stream into your UI / agent |
| `bot.keyword_alert` | A configured keyword was spoken or typed | Trigger your alert workflow |

**Webhook payload example:**
```json
{
  "event": "bot.in_call",
  "bot_id": "61d4b9aa-76f0-412e-a3b0-f2f5d79d4507",
  "status": "in_call",
  "meeting_url": "https://www.onepizza.io/join/abc-defg-hij",
  "meeting_platform": "onepizza"
}
```

### 4. Fetch Results

```bash
# Get transcript
curl https://your-instance.railway.app/api/v1/bot/{bot_id}/transcript \
  -H "Authorization: Bearer sk_live_abc123..."

# Get full bot data (includes analysis, speaker stats, etc.)
curl https://your-instance.railway.app/api/v1/bot/{bot_id} \
  -H "Authorization: Bearer sk_live_abc123..."
```

---

## Driving the Bot Mid-Meeting (v2.34.0+)

The bot exposes two endpoints that let an external agent (your code, your AI, a
human-in-the-loop UI) drive the bot while it's in the call:

### Speak text aloud — `POST /api/v1/bot/{id}/say`

```bash
curl -X POST https://your-instance.railway.app/api/v1/bot/{bot_id}/say \
  -H "Authorization: Bearer sk_live_..." \
  -H "Content-Type: application/json" \
  -d '{"text": "Sorry to interrupt — quick question.", "voice": "gemini"}'
```

- Returns 202 immediately with a `task_id`. Audio plays through the bot's
  virtual mic ~1–2 s later (Gemini TTS) or ~300–500 ms later with `"voice":"edge"`.
- Concurrent calls **queue** behind a per-bot lock — speech never overlaps.
- Pass `"interrupt": true` to cancel any in-flight speak and jump ahead.
- Requires `bot.status == "in_call"` (HTTP 409 otherwise).
- The spoken text is appended to the unified transcript with
  `"source": "voice", "bot_generated": true`.

### Post text to the chat — `POST /api/v1/bot/{id}/chat`

```bash
curl -X POST https://your-instance.railway.app/api/v1/bot/{bot_id}/chat \
  -H "Authorization: Bearer sk_live_..." \
  -H "Content-Type: application/json" \
  -d '{"text": "Sharing the doc: https://example.com/spec"}'
```

- Returns 202 immediately. Message appears in the meeting chat ~300–500 ms later.
- Works on Google Meet, Zoom, Microsoft Teams, and onepizza.
- The bot's own message is filtered from `bot.live_chat_message` so you don't
  get an echo for messages you posted yourself.

### Consume the live stream — `GET /api/v1/bot/{id}/stream`

Server-Sent Events delivering every transcript entry as it's created. Each
event is one JSON line:

```json
data: {"speaker":"Alice","text":"Should we move to the next topic?","timestamp":42.31,"source":"voice"}
data: {"speaker":"Bob","text":"+1","timestamp":44.10,"source":"chat","message_id":"a1b2c3d4e5f60718"}
```

The same entries are also broadcast over WebSocket (`/api/v1/ws`) and via
webhooks (`bot.live_transcript` for voice, `bot.live_chat_message` for chat).

### A typical interactive loop

1. Subscribe to `bot.live_transcript` + `bot.live_chat_message`.
2. When you decide a response is needed, call your AI of choice with the
   recent transcript window as context.
3. Call `POST /say` (voice) or `POST /chat` (text) with the generated reply.
4. The bot speaks/posts within ~1–2 s — and your reply also appears in the
   stream (with `bot_generated: true`) so multi-turn context stays consistent.

---

## Important: Use Webhooks, Not Polling

**Bad pattern (causes 404 storms):**
```javascript
// DON'T DO THIS
setInterval(async () => {
  const res = await fetch(`/api/v1/bot/${botId}`);
  if (res.status === 404) return; // bot gone after redeploy — loops forever!
}, 20000);
```

**Good pattern (webhook-driven):**
```javascript
// Webhook handler receives status updates
app.post('/webhook/bot-status', (req, res) => {
  const { event, bot_id, status } = req.body;
  // Update your DB with the new status
  await db.bots.update(bot_id, { status });

  if (event === 'bot.done') {
    // Fetch final results
    const bot = await fetch(`/api/v1/bot/${bot_id}`).then(r => r.json());
    await processResults(bot);
  }
  res.sendStatus(200);
});
```

If you must poll, **stop polling on 404** — it means the bot no longer exists (server restarted or bot expired after 24h).

---

## Bot Lifecycle

```
ready → joining → in_call → call_ended → transcribing → done
                                                       → error
```

- Bots are **in-memory** during their lifecycle (fast, real-time)
- After completion, bots are **persisted to DB** for 24 hours
- After 24h, bot data expires — fetch results promptly via webhooks

---

## Supported Platforms

| Platform | URL Pattern |
|----------|------------|
| Zoom | `https://zoom.us/j/...` or `https://us06web.zoom.us/j/...` |
| Google Meet | `https://meet.google.com/abc-defg-hij` |
| Microsoft Teams | `https://teams.microsoft.com/l/meetup-join/...` |
| onepizza.io | `https://www.onepizza.io/join/abc-defg-hij` |
| meetingservice (Railway) | `https://meetingservice-production.up.railway.app/join/abc-defg-hij` |

---

## Full Bot Creation Parameters

```json
{
  "meeting_url": "https://...",
  "bot_name": "My Bot",
  "webhook_url": "https://your-backend/webhook",
  "live_transcription": true,
  "analysis_mode": "full",
  "template": "default",
  "start_muted": true,
  "record_video": false,
  "consent_enabled": false,
  "metadata": { "user_id": "abc", "session": "xyz" }
}
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `meeting_url` | string | required | Full meeting URL |
| `bot_name` | string | "JustHereToListen.io" | Name shown in meeting |
| `webhook_url` | string | optional | Per-bot webhook for status updates |
| `live_transcription` | bool | false | Enable real-time transcript streaming |
| `analysis_mode` | string | "full" | "full" or "transcript_only" |
| `template` | string | "default" | Analysis template (default, sales, standup, 1on1, etc.) |
| `start_muted` | bool | true | Join with mic muted |
| `record_video` | bool | false | Record video in addition to audio |
| `consent_enabled` | bool | false | Show consent message to participants |
| `metadata` | object | {} | Custom metadata (returned in webhooks) |

---

## Error Handling

| HTTP Status | Meaning | Action |
|-------------|---------|--------|
| 201 | Bot created | Store bot_id, wait for webhooks |
| 400 | Invalid parameters | Check request body |
| 401 | Invalid API key | Check Authorization header |
| 402 | Insufficient credits | Top up balance |
| 404 | Bot not found | Stop polling — bot expired or server restarted |
| 429 | Rate limited | Back off and retry |

---

## For meetingservice Integration

When sending a bot to an onepizza.io / meetingservice room:

1. The bot navigates to the join URL with `?name=BotName` appended
2. meetingservice's client-side JS auto-joins when `?name=` is present
3. The bot appears in the meeting within ~5 seconds
4. Audio is captured via the browser's PulseAudio routing
5. After the meeting ends (or bot is alone for 5 min), transcription + analysis runs

**No changes needed on the meetingservice side** — the bot integrates through the standard browser-based join flow.
