# MeetingBot Python SDK

Official Python client for the [MeetingBot API](https://api.yourserver.com). Supports both synchronous and asynchronous usage via `httpx` and Pydantic v2 models.

## Requirements

- Python 3.9+
- `httpx >= 0.24.0`
- `pydantic >= 2.0.0`

## Installation

```bash
pip install meetingbot-sdk
```

Or install from source:

```bash
git clone https://github.com/your-org/meetingbot-sdk-python
cd meetingbot-sdk-python
pip install .
```

## Authentication

All API requests require a Bearer token. Pass your API key to the client constructor:

```python
from meetingbot import MeetingBotClient

client = MeetingBotClient(api_key="sk_live_...")
```

You can also configure a custom base URL (useful for self-hosted deployments):

```python
client = MeetingBotClient(
    api_key="sk_live_...",
    base_url="https://api.yourserver.com",
)
```

---

## Quickstart — Create a bot and poll for completion

### Synchronous

```python
import time
from meetingbot import MeetingBotClient
from meetingbot.exceptions import MeetingBotError

client = MeetingBotClient(api_key="sk_live_your_key_here")

# 1. Create the bot
bot = client.create_bot(
    meeting_url="https://zoom.us/j/123456789?pwd=abc",
    bot_name="My Recorder",
    webhook_url="https://myapp.com/webhooks/meetingbot",
    analysis_mode="full",
    record_video=False,
)
print(f"Bot created: id={bot.id}, status={bot.status}")

# 2. Poll until the bot finishes (or fails)
terminal_statuses = {"completed", "failed", "cancelled"}
while bot.status not in terminal_statuses:
    time.sleep(10)
    bot = client.get_bot(bot.id)
    print(f"  ... status={bot.status}")

print(f"Bot finished with status: {bot.status}")

# 3. Download the audio recording
if bot.status == "completed":
    audio_bytes = client.download_recording(bot.id)
    with open(f"{bot.id}_recording.mp3", "wb") as f:
        f.write(audio_bytes)
    print(f"Recording saved ({len(audio_bytes):,} bytes)")

    # Export transcript as SRT
    srt_bytes = client.export_srt(bot.id)
    with open(f"{bot.id}.srt", "wb") as f:
        f.write(srt_bytes)
    print("Subtitles saved")

    # Export full JSON analysis
    export = client.export_json(bot.id)
    print(f"Transcript segments: {len(export.transcript or [])}")
```

### Asynchronous

```python
import asyncio
from meetingbot import AsyncMeetingBotClient

async def main():
    async with AsyncMeetingBotClient(api_key="sk_live_your_key_here") as client:
        # 1. Create the bot
        bot = await client.create_bot(
            meeting_url="https://zoom.us/j/123456789?pwd=abc",
            bot_name="Async Recorder",
            webhook_url="https://myapp.com/webhooks/meetingbot",
            analysis_mode="full",
        )
        print(f"Bot created: id={bot.id}, status={bot.status}")

        # 2. Poll until the bot finishes
        terminal_statuses = {"completed", "failed", "cancelled"}
        while bot.status not in terminal_statuses:
            await asyncio.sleep(10)
            bot = await client.get_bot(bot.id)
            print(f"  ... status={bot.status}")

        print(f"Bot finished: {bot.status}")

        # 3. Download recording
        if bot.status == "completed":
            audio = await client.download_recording(bot.id)
            print(f"Recording size: {len(audio):,} bytes")

asyncio.run(main())
```

---

## API Reference

### Bots

```python
# Create a bot
bot = client.create_bot(
    meeting_url="https://zoom.us/j/...",
    bot_name="MeetingBot",           # optional, default "MeetingBot"
    bot_avatar_url="https://...",    # optional
    webhook_url="https://...",       # optional
    join_at="2026-03-16T15:00:00Z",  # optional ISO 8601
    analysis_mode="full",            # "full" | "transcript_only"
    template="summary",              # optional
    prompt_override="Summarise...",  # optional
    vocabulary=["TechTerm", "SDK"],  # optional
    respond_on_mention=True,         # optional
    start_muted=True,                # optional
    live_transcription=True,         # optional
    sub_user_id="user_123",          # optional (multi-tenant)
    metadata={"project": "Q1"},      # optional
    record_video=False,              # optional, default False
    idempotency_key="unique-key",    # optional
)

# List bots
bots = client.list_bots(limit=20, offset=0, status="completed")

# Get a specific bot
bot = client.get_bot("bot_abc123")

# Cancel a bot
client.cancel_bot("bot_abc123")

# Download audio recording (returns bytes)
audio = client.download_recording("bot_abc123")

# Download video recording (returns bytes)
video = client.download_video("bot_abc123")

# Get aggregate statistics
stats = client.get_bot_stats()
print(stats.total, stats.completed, stats.failed)
```

### Webhooks

```python
# Register a webhook
wh = client.create_webhook(
    url="https://myapp.com/webhooks",
    events=["bot.completed", "bot.failed"],
    secret="my_signing_secret",  # optional
)

# List webhooks
webhooks = client.list_webhooks()

# Get a webhook
wh = client.get_webhook("wh_abc123")

# Update a webhook
wh = client.update_webhook("wh_abc123", events=["bot.completed"])

# Delete a webhook
client.delete_webhook("wh_abc123")

# List delivery logs
deliveries = client.list_webhook_deliveries("wh_abc123", limit=50)
```

### Auth & API Keys

```python
# List API keys
keys = client.list_api_keys()

# Create a new API key
new_key = client.create_api_key(name="production-key")
print(new_key.key)  # shown only once!

# Revoke a key
client.revoke_api_key("key_abc123")

# Get plan info
plan = client.get_plan()
print(plan.plan, plan.limits)

# Notification preferences
prefs = client.get_notification_prefs()
client.update_notification_prefs(email_on_completion=True, email_on_failure=True)
```

### Billing

```python
# Get balance and transactions
balance = client.get_balance()
print(f"Balance: ${balance.balance_usd:.2f}")

# Create a Stripe checkout session to add funds
checkout = client.create_checkout(
    amount_usd=50.00,
    success_url="https://myapp.com/billing/success",
    cancel_url="https://myapp.com/billing/cancel",
)
print(f"Pay here: {checkout.checkout_url}")
```

### Exports

```python
# Export as PDF (returns bytes)
pdf = client.export_pdf("bot_abc123")

# Export as JSON
data = client.export_json("bot_abc123")
print(data.transcript)

# Export as SRT subtitles (returns bytes)
srt = client.export_srt("bot_abc123")
```

---

## Error Handling

All errors inherit from `MeetingBotError`. Each carries `status_code` and `detail` attributes.

```python
from meetingbot.exceptions import (
    MeetingBotError,
    AuthError,        # 401 / 403
    NotFoundError,    # 404
    ValidationError,  # 422
    RateLimitError,   # 429
    ServerError,      # 5xx
)

try:
    bot = client.get_bot("nonexistent")
except NotFoundError as e:
    print(f"Not found: {e.detail}")
except RateLimitError:
    print("Rate limited — back off and retry")
except AuthError as e:
    print(f"Auth failed: {e.message}")
except MeetingBotError as e:
    print(f"API error {e.status_code}: {e.detail}")
```

---

## Context Manager Usage

Both clients support the context manager protocol for automatic cleanup:

```python
# Sync
with MeetingBotClient(api_key="sk_live_...") as client:
    bot = client.create_bot(meeting_url="https://...")

# Async
async with AsyncMeetingBotClient(api_key="sk_live_...") as client:
    bot = await client.create_bot(meeting_url="https://...")
```

---

## License

MIT
