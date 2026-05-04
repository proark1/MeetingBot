# JustHereToListen.io SDKs

Official client libraries for the [JustHereToListen.io API](https://api.justheretolisten.io) — multi-tenant meeting bots that join Zoom, Google Meet, Microsoft Teams, and onepizza.io meetings to record, transcribe, and analyse them with Claude or Gemini.

## Available SDKs

| Language | Path | Package | Status |
|---|---|---|---|
| Python (sync + async) | [`sdk/python/`](./python/) | `meetingbot-sdk` | Hand-curated, idiomatic |
| TypeScript / JavaScript | [`sdk/js/`](./js/) | `meetingbot-sdk` | Hand-curated, idiomatic |

Both SDKs cover the same surface: account / API key management, bot creation and lifecycle polling, transcript and analysis retrieval, exports, webhooks (registration + signature verification), keyword alerts, calendar feeds, and the opt-in advanced features (chat Q&A, speaker analytics, decision detection, cross-meeting memory, host coaching, agentic delegation).

## Choosing an SDK

- **Python** → [`sdk/python/README.md`](./python/README.md). Sync `MeetingBotClient` + async `AsyncMeetingBotClient`, both built on `httpx` + Pydantic v2.
- **TypeScript / Node.js** → [`sdk/js/README.md`](./js/README.md). Native `fetch`, zero runtime dependencies, full `.d.ts` type definitions.

## Authentication

Every API call uses Bearer auth with a `sk_live_…` (production) or `sk_test_…` (sandbox) key:

```
Authorization: Bearer sk_live_…
```

Get your first key by registering at `POST /api/v1/auth/register` or via the dashboard at `https://api.justheretolisten.io/dashboard`. Sandbox keys return demo data without deducting credits.

## Source of truth

The canonical contract is the OpenAPI 3.1 schema:

| File | Description |
|---|---|
| [`api/openapi.json`](../api/openapi.json) | Public schema — 114 operations, 100% example coverage |
| [`api/openapi.admin.json`](../api/openapi.admin.json) | Full schema including admin + analytics — 133 operations |

Both snapshots are committed and CI fails on drift (`scripts/generate_openapi.py --check`). The hand-curated SDKs in this directory are kept in sync manually; auto-generated reference clients are produced on every tagged release by `.github/workflows/sdk-gen.yml` (using `openapi-generator-cli` 7.4.0) and published into `sdk/python/generated/` and `sdk/js/generated/`. **For day-to-day use, prefer the hand-curated SDKs** — they are smaller, idiomatic, and stable across schema additions.

## Versioning

The SDKs follow the API version. The current API version is in [`VERSION`](../VERSION) at the repo root and surfaces as `info.version` in the OpenAPI schema and the `X-API-Version` response header.

## Links

- API docs (Swagger UI): `https://api.justheretolisten.io/api/docs`
- API docs (ReDoc): `https://api.justheretolisten.io/api/redoc`
- Quickstart guide: `https://api.justheretolisten.io/api/quickstart`
- Webhook playground: `https://api.justheretolisten.io/webhook-playground`
- Issues / source: <https://github.com/proark1/MeetingBot>
