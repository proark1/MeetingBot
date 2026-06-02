# JustHereToListen.io REST API

Base URLs:

| Environment | Base URL |
|---|---|
| Local dev | `http://localhost:8000` |
| Production | `https://api.justheretolisten.io` |

All endpoints live under `/api/v1`. The full machine-readable surface — 116 public + 135 admin operations, every one with summary, description, tags, request example, and a 2xx response example — is at [`/api/docs`](http://localhost:8000/api/docs) (Swagger UI) and [`/api/redoc`](http://localhost:8000/api/redoc) (ReDoc).

---

## Authentication

Every request that returns user data needs a Bearer token. Three credential types are accepted:

| Credential | Format | How to get it |
|---|---|---|
| Production API key | `sk_live_...` | Dashboard → Settings → API keys |
| Sandbox API key | `sk_test_...` | Dashboard → Settings → API keys (toggle Test mode) |
| JWT (web UI only) | `eyJ...` | `POST /api/v1/auth/login` |

Pass the token as a Bearer:

```http
Authorization: Bearer sk_live_your_key_here
```

> Sandbox keys (`sk_test_*`) write to a separate, isolated namespace. Bot lifecycle still runs in real Chromium, but billing and webhook delivery follow the sandbox quota.

> `POST /api/v1/auth/login` expects **`application/x-www-form-urlencoded`** (not JSON) with `username` (your email) and `password`. The returned JWT is for the dashboard; use your `sk_live_*` key as a Bearer for everything else.

### Business sub-users

If your account represents an entire downstream platform, scope a bot to a single end-user with either:

```http
X-Sub-User: customer_42
```

…or by passing `"sub_user_id": "customer_42"` in the create-bot body. After that, only requests carrying the same `sub_user_id` can read or mutate that bot.

### Idempotency

Add `Idempotency-Key: <uuid>` to `POST /api/v1/bot`. A retry with the same key returns the original bot and the response carries `X-Idempotency-Replayed: true`. Anonymous callers cannot use this header.

---

## Quickstart (cURL)

### 1. Create a bot

```bash
curl -X POST https://api.justheretolisten.io/api/v1/bot \
  -H "Authorization: Bearer sk_live_your_key_here" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{
    "meeting_url": "https://zoom.us/j/123456789?pwd=abc",
    "bot_name": "JustHereToListen.io",
    "template": "default",
    "webhook_url": "https://yourapp.com/webhooks/meetingbot",
    "respond_on_mention": true
  }'
```

Response (`201 Created`):

```json
{
  "id": "bot_8a72c5e1",
  "status": "ready",
  "meeting_platform": "zoom",
  "created_at": "2026-05-05T10:42:00Z"
}
```

### 2. Poll status (or subscribe to webhooks instead — see below)

```bash
curl https://api.justheretolisten.io/api/v1/bot/bot_8a72c5e1 \
  -H "Authorization: Bearer sk_live_your_key_here"
```

Status progresses: `ready → scheduled → queued → joining → in_call → call_ended → transcribing → done` (or `error` / `cancelled`).

### 3. Fetch results when `status == "done"`

```bash
# Full bot record (transcript + analysis JSON)
curl https://api.justheretolisten.io/api/v1/bot/bot_8a72c5e1 \
  -H "Authorization: Bearer sk_live_your_key_here"

# Audio recording (mp3)
curl -O https://api.justheretolisten.io/api/v1/bot/bot_8a72c5e1/recording \
  -H "Authorization: Bearer sk_live_your_key_here"

# Transcript only (SRT)
curl https://api.justheretolisten.io/api/v1/bot/bot_8a72c5e1/transcript.srt \
  -H "Authorization: Bearer sk_live_your_key_here"
```

---

## Endpoint surface (high-level)

The router prefixes are mounted under `/api/v1`. See Swagger for full schemas.

| Prefix | Purpose |
|---|---|
| `/auth` | Register, login, password reset, account info |
| `/auth/oauth` | Google / Microsoft SSO |
| `/auth/saml` | Enterprise SSO |
| `/bot` | Create / read / cancel bots, transcripts, recordings, exports, agentic instructions, Q&A |
| `/webhook` | Register webhook endpoints, view delivery logs, replay |
| `/templates` | List analysis templates and create custom ones |
| `/action-items` | Cross-meeting action item queries |
| `/calendar` | iCal feeds for calendar auto-join |
| `/integrations` | Slack, Notion, Linear, Jira |
| `/keyword-alerts` | Real-time keyword webhook triggers |
| `/workspaces` | Multi-user shared workspaces |
| `/billing` | Stripe / USDC top-ups, credit balance, invoices |
| `/mcp` | Model Context Protocol server (see [MCP.md](./MCP.md)) |
| `/admin` | Platform admin (requires admin account) |

---

## Webhooks

Register your endpoint:

```bash
curl -X POST https://api.justheretolisten.io/api/v1/webhook \
  -H "Authorization: Bearer sk_live_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://yourapp.com/webhooks/meetingbot",
    "events": ["bot.done", "bot.error", "bot.transcript_ready"],
    "secret": "whsec_pick_a_long_random_string"
  }'
```

Every delivery carries two headers:

```http
X-MeetingBot-Signature: sha256=<hmac_sha256_hex>
X-MeetingBot-Timestamp: 1730000000
```

Verify in any language by computing HMAC-SHA256 over `f"{timestamp}.{raw_body}"` with your `secret` and comparing (constant-time) against the hex after the `sha256=` prefix. Reject deliveries older than 5 minutes (replay protection). The official SDKs ship a ready-made verifier — `verify_webhook` (Python) / `verifyWebhook` (TypeScript); see [SDKs.md](./SDKs.md#webhook-signature-verification).

Available events (20 total):

```
# Lifecycle
bot.joining               bot.in_call               bot.call_ended
bot.transcript_ready      bot.analysis_ready        bot.done
bot.error                 bot.cancelled

# Live (streamed during the meeting)
bot.keyword_alert         bot.live_transcript       bot.live_transcript_translated
bot.live_chat_message

# Advanced features (require the matching per-bot opt-in flag)
bot.decision_detected     bot.coaching_tip          bot.speaker_analytics
bot.agentic_action        bot.recurring_intel_ready

# Action-item reminders (fired by the background scheduler)
action_item.due_soon      action_item.overdue

# Test
bot.test
```

The five advanced events only fire when the bot was created with the
corresponding opt-in (`enable_decision_detection`, `enable_coaching`,
`enable_speaker_analytics`, agentic instructions, or a recurring meeting key).
The two `action_item.*` events come from the reminder scheduler, not a live bot,
so their payloads carry an `action_item_id` / `task` / `due_date` / `stage`
instead of a `bot` block.

Failed deliveries retry with exponential backoff up to ~24 h. Inspect attempts at `GET /api/v1/webhook/{id}/deliveries`.

---

## Errors

Errors are always JSON of shape `{"detail": "..."}` with the appropriate HTTP status:

| Status | Meaning |
|---|---|
| 400 | Validation error in body or query |
| 401 | Missing or invalid Bearer token |
| 402 | Out of credits — top up at `/api/v1/billing` |
| 403 | Authenticated but not allowed (admin-only endpoint, etc.) |
| 404 | Resource not found *or* not owned by your account (we deliberately collapse 403→404 for ownership checks to prevent enumeration) |
| 409 | Idempotency conflict / duplicate resource |
| 422 | Pydantic validation error (per-field) |
| 429 | Rate-limited (default: 20 bot creations / min / account) |
| 5xx | Retry with backoff |

---

## Rate limits

| Endpoint | Default |
|---|---|
| `POST /api/v1/bot` | 20 / min / account |
| `POST /api/v1/auth/login` | 10 / min / IP |
| Other authenticated endpoints | unrestricted (subject to global concurrency cap) |

Concurrent active bots per account default to `MAX_CONCURRENT_BOTS=3` on self-hosted and a tier-based limit on production.

---

## Next steps

- **AI assistants** → connect via [MCP.md](./MCP.md)
- **Python / TypeScript apps** → use the official [SDKs.md](./SDKs.md)
- **Full schemas + try-it-now** → [`/api/docs`](http://localhost:8000/api/docs)
