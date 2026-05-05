# JustHereToListen.io Official SDKs

Two first-party clients, one shared design:

| Language | Package | Source |
|---|---|---|
| Python 3.9+ | `meetingbot-sdk` (PyPI) | [`sdk/python/`](../sdk/python/) |
| Node.js 18+ / TypeScript | `meetingbot-sdk` (npm) | [`sdk/js/`](../sdk/js/) |

Each SDK wraps the same REST surface documented in [API.md](./API.md). For exhaustive method-by-method reference see the per-package READMEs:

- [`sdk/python/README.md`](../sdk/python/README.md)
- [`sdk/js/README.md`](../sdk/js/README.md)

---

## Python

### Install

```bash
pip install meetingbot-sdk
```

### Quickstart — sync

```python
from meetingbot import MeetingBotClient

client = MeetingBotClient(api_key="sk_live_your_key_here")

bot = client.create_bot(
    meeting_url="https://zoom.us/j/123456789?pwd=abc",
    bot_name="My Recorder",
    webhook_url="https://myapp.com/webhooks/meetingbot",
    analysis_mode="full",
)

print(bot.id, bot.status)
```

### Quickstart — async

```python
import asyncio
from meetingbot import AsyncMeetingBotClient

async def main():
    async with AsyncMeetingBotClient(api_key="sk_live_...") as client:
        bot = await client.create_bot(
            meeting_url="https://meet.google.com/abc-defg-hij",
            template="standup",
        )
        result = await client.wait_for_bot(bot.id, timeout=1800)
        print(result.analysis.summary)

asyncio.run(main())
```

### Self-hosted base URL

```python
client = MeetingBotClient(
    api_key="sk_live_...",
    base_url="http://localhost:8000",
)
```

---

## JavaScript / TypeScript

### Install

```bash
npm install meetingbot-sdk
# or
pnpm add meetingbot-sdk
```

Node.js 18+ is required (uses native `fetch`). For older versions, polyfill `globalThis.fetch` before importing.

### Quickstart

```ts
import { MeetingBotClient } from "meetingbot-sdk";

const client = new MeetingBotClient({ apiKey: "sk_live_your_key_here" });

const bot = await client.createBot({
  meeting_url: "https://teams.microsoft.com/l/meetup-join/...",
  bot_name: "My Recorder",
  webhook_url: "https://myapp.com/webhooks/meetingbot",
  analysis_mode: "full",
});

console.log(bot.id, bot.status);
```

### Self-hosted base URL

```ts
const client = new MeetingBotClient({
  apiKey: "sk_live_...",
  baseUrl: "http://localhost:8000",
  timeoutMs: 30_000,
});
```

---

## Calling MCP tools through the SDK

Both SDKs expose the underlying transport, so MCP calls work without a second dependency:

```python
result = client.request("POST", "/api/v1/mcp/call", json={
    "tool": "search_meetings",
    "arguments": {"query": "v2 onboarding", "limit": 5},
})
```

```ts
const result = await client.request("POST", "/api/v1/mcp/call", {
  tool: "search_meetings",
  arguments: { query: "v2 onboarding", limit: 5 },
});
```

For the full tool catalogue see [MCP.md](./MCP.md).

---

## Webhook signature verification

### Python

```python
import hmac, hashlib

def verify(signature_header: str, timestamp_header: str, raw_body: bytes, secret: str) -> bool:
    expected = hmac.new(
        secret.encode(),
        f"{timestamp_header}.{raw_body.decode()}".encode(),
        hashlib.sha256,
    ).hexdigest()
    # X-MeetingBot-Signature is "t=<unix>,v1=<hex>"
    parts = dict(p.split("=", 1) for p in signature_header.split(","))
    return hmac.compare_digest(expected, parts.get("v1", ""))
```

### TypeScript

```ts
import { createHmac, timingSafeEqual } from "node:crypto";

export function verify(signatureHeader: string, timestampHeader: string, rawBody: string, secret: string): boolean {
  const expected = createHmac("sha256", secret).update(`${timestampHeader}.${rawBody}`).digest("hex");
  const v1 = Object.fromEntries(signatureHeader.split(",").map(p => p.split("=", 2))).v1 ?? "";
  return v1.length === expected.length && timingSafeEqual(Buffer.from(v1), Buffer.from(expected));
}
```

The header names — `X-MeetingBot-Signature`, `X-MeetingBot-Timestamp` — are part of the public contract and never change.

---

## Error handling

Both SDKs raise typed exceptions / errors that mirror HTTP status codes:

| Class | Status |
|---|---|
| `AuthenticationError` | 401 |
| `PaymentRequiredError` | 402 |
| `PermissionError` | 403 |
| `NotFoundError` | 404 |
| `ConflictError` | 409 |
| `RateLimitError` | 429 |
| `ServerError` | 5xx |
| `MeetingBotError` | base class for all of the above |

```python
from meetingbot.exceptions import RateLimitError

try:
    client.create_bot(meeting_url="...")
except RateLimitError as e:
    time.sleep(e.retry_after or 30)
```
