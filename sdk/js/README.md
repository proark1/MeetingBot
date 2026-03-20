# MeetingBot JavaScript/TypeScript SDK

Official JavaScript/TypeScript client for the [MeetingBot API](https://api.yourserver.com). Works in Node.js 18+ (native `fetch`) and can be polyfilled for older environments. Ships with full TypeScript type definitions.

## Requirements

- Node.js 18+ (for native `fetch`) **or** any environment with a `fetch` implementation
- TypeScript 5+ (optional, plain JS works too)

## Installation

```bash
npm install meetingbot-sdk
```

```bash
yarn add meetingbot-sdk
```

```bash
pnpm add meetingbot-sdk
```

> **Node.js < 18:** Install a `fetch` polyfill (e.g. `node-fetch`) and assign it before importing:
> ```js
> import fetch from "node-fetch";
> globalThis.fetch = fetch;
> ```

## Authentication

All API requests require a Bearer token. Pass your API key via the constructor:

```ts
import { MeetingBotClient } from "meetingbot-sdk";

const client = new MeetingBotClient({ apiKey: "sk_live_..." });
```

You can also configure a custom base URL (useful for self-hosted deployments):

```ts
const client = new MeetingBotClient({
  apiKey: "sk_live_...",
  baseUrl: "https://api.yourserver.com",
  timeoutMs: 30000, // optional, default 30 000 ms
});
```

---

## Quickstart — Create a bot and poll for completion

### TypeScript

```ts
import { MeetingBotClient, MeetingBotError, NotFoundError } from "meetingbot-sdk";

const client = new MeetingBotClient({ apiKey: "sk_live_your_key_here" });

async function main() {
  // 1. Create the bot
  const bot = await client.createBot({
    meeting_url: "https://zoom.us/j/123456789?pwd=abc",
    bot_name: "My Recorder",
    webhook_url: "https://myapp.com/webhooks/meetingbot",
    analysis_mode: "full",
    record_video: false,
  });
  console.log(`Bot created: id=${bot.id}, status=${bot.status}`);

  // 2. Poll until the bot finishes (or fails)
  const terminalStatuses = new Set(["completed", "failed", "cancelled"]);
  let current = bot;

  while (!terminalStatuses.has(current.status ?? "")) {
    await new Promise((r) => setTimeout(r, 10_000)); // wait 10 s
    current = await client.getBot(bot.id);
    console.log(`  ... status=${current.status}`);
  }

  console.log(`Bot finished with status: ${current.status}`);

  // 3. Download the audio recording
  if (current.status === "completed") {
    const audioBuffer = await client.downloadRecording(bot.id);
    console.log(`Recording size: ${audioBuffer.byteLength.toLocaleString()} bytes`);

    // Write to disk (Node.js)
    const fs = await import("fs/promises");
    await fs.writeFile(`${bot.id}_recording.mp3`, Buffer.from(audioBuffer));
    console.log("Recording saved.");

    // Export as SRT subtitles
    const srtBuffer = await client.exportSrt(bot.id);
    await fs.writeFile(`${bot.id}.srt`, Buffer.from(srtBuffer));
    console.log("Subtitles saved.");

    // Export full JSON analysis
    const jsonExport = await client.exportJson(bot.id);
    const segmentCount = jsonExport.transcript?.length ?? 0;
    console.log(`Transcript segments: ${segmentCount}`);
  }
}

main().catch(console.error);
```

### JavaScript (CommonJS)

```js
const { MeetingBotClient } = require("meetingbot-sdk");

const client = new MeetingBotClient({ apiKey: "sk_live_your_key_here" });

async function main() {
  const bot = await client.createBot({
    meeting_url: "https://zoom.us/j/123456789",
    bot_name: "My Recorder",
  });
  console.log("Bot created:", bot.id);

  // Poll for completion
  const terminal = new Set(["completed", "failed", "cancelled"]);
  let current = bot;
  while (!terminal.has(current.status)) {
    await new Promise((r) => setTimeout(r, 10000));
    current = await client.getBot(bot.id);
  }
  console.log("Done:", current.status);
}

main();
```

---

## API Reference

### Bots

```ts
// Create a bot
const bot = await client.createBot({
  meeting_url: "https://zoom.us/j/...",
  bot_name: "MeetingBot",              // optional, default "MeetingBot"
  bot_avatar_url: "https://...",       // optional
  webhook_url: "https://...",          // optional
  join_at: "2026-03-16T15:00:00Z",    // optional ISO 8601
  analysis_mode: "full",               // "full" | "transcript_only"
  template: "summary",                 // optional
  prompt_override: "Summarise...",     // optional
  vocabulary: ["TechTerm", "SDK"],     // optional
  respond_on_mention: true,            // optional
  start_muted: true,                   // optional
  live_transcription: true,            // optional
  sub_user_id: "user_123",             // optional (multi-tenant)
  metadata: { project: "Q1" },         // optional
  record_video: false,                 // optional, default false
  idempotency_key: "unique-key",       // optional
});

// List bots
const { results, total } = await client.listBots({
  limit: 20,
  offset: 0,
  status: "completed",
  sub_user_id: "user_123",
});

// Get a specific bot
const bot = await client.getBot("bot_abc123");

// Cancel a bot
await client.cancelBot("bot_abc123");

// Download audio recording (ArrayBuffer)
const audio = await client.downloadRecording("bot_abc123");

// Download video recording (ArrayBuffer)
const video = await client.downloadVideo("bot_abc123");

// Get aggregate statistics
const stats = await client.getBotStats();
console.log(stats.total, stats.completed, stats.failed);
```

### Webhooks

```ts
// Register a webhook
const wh = await client.createWebhook({
  url: "https://myapp.com/webhooks",
  events: ["bot.completed", "bot.failed"],
  secret: "my_signing_secret",  // optional
});

// List webhooks
const { results } = await client.listWebhooks();

// Get a webhook
const wh = await client.getWebhook("wh_abc123");

// Update a webhook
const updated = await client.updateWebhook("wh_abc123", {
  events: ["bot.completed"],
});

// Delete a webhook
await client.deleteWebhook("wh_abc123");

// List delivery logs
const deliveries = await client.listWebhookDeliveries("wh_abc123", {
  limit: 50,
  offset: 0,
});
```

### Auth & API Keys

```ts
// Register a new account
await client.register({
  email: "user@example.com",
  password: "secret",
  key_name: "my-first-key",
});

// Login (returns JWT)
const { access_token } = await client.login("user@example.com", "secret");

// List API keys
const { results: keys } = await client.listApiKeys();

// Create a new key
const newKey = await client.createApiKey("production-key");
console.log(newKey.key); // shown only once!

// Revoke a key
await client.revokeApiKey("key_abc123");

// Get plan info
const plan = await client.getPlan();
console.log(plan.plan, plan.limits);

// Notification preferences
const prefs = await client.getNotificationPrefs();
await client.updateNotificationPrefs({
  email_on_completion: true,
  email_on_failure: true,
});
```

### Billing

```ts
// Get balance and transactions
const { balance_usd, transactions } = await client.getBalance();
console.log(`Balance: $${balance_usd.toFixed(2)}`);

// Create a Stripe checkout session to add funds
const { checkout_url } = await client.createCheckout({
  amount_usd: 50.0,
  success_url: "https://myapp.com/billing/success",
  cancel_url: "https://myapp.com/billing/cancel",
});
console.log("Pay here:", checkout_url);
```

### Exports

```ts
// Export as PDF (ArrayBuffer)
const pdf = await client.exportPdf("bot_abc123");

// Export as JSON
const data = await client.exportJson("bot_abc123");
console.log(data.transcript);

// Export as SRT subtitles (ArrayBuffer)
const srt = await client.exportSrt("bot_abc123");
```

---

## Error Handling

All errors extend `MeetingBotError` and carry `statusCode` and `detail` properties.

```ts
import {
  MeetingBotError,
  AuthError,        // 401 / 403
  NotFoundError,    // 404
  ValidationError,  // 422
  RateLimitError,   // 429
  ServerError,      // 5xx
} from "meetingbot-sdk";

try {
  const bot = await client.getBot("nonexistent");
} catch (err) {
  if (err instanceof NotFoundError) {
    console.error("Not found:", err.detail);
  } else if (err instanceof RateLimitError) {
    console.error("Rate limited — back off and retry");
  } else if (err instanceof AuthError) {
    console.error("Auth failed:", err.message);
  } else if (err instanceof MeetingBotError) {
    console.error(`API error ${err.statusCode}:`, err.detail);
  } else {
    throw err; // unexpected
  }
}
```

---

## Building from Source

```bash
git clone https://github.com/your-org/meetingbot-sdk-js
cd meetingbot-sdk-js
npm install
npm run build
# Compiled output is in ./dist/
```

---

## TypeScript Types

All types are exported from the package root:

```ts
import type {
  BotResponse,
  BotSummary,
  BotListResponse,
  BotStats,
  CreateBotParams,
  WebhookResponse,
  WebhookListResponse,
  BalanceResponse,
  PlanInfo,
  // ... etc.
} from "meetingbot-sdk";
```

---

## License

MIT
