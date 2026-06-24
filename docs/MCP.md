# JustHereToListen.io MCP Server

JustHereToListen.io speaks the [Model Context Protocol](https://modelcontextprotocol.io). Any MCP-compatible AI client — Claude Desktop, Cursor, Cline, Continue, Zed — can list your meetings, search transcripts, dispatch bots, and ask transcript-grounded questions, all under your normal API key. **16 tools** are exposed across read, write, and reasoning categories.

Two endpoints, both under `/api/v1/mcp`:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/mcp/schema` | Returns the MCP server manifest (tool list + JSON schemas) |
| `POST` | `/api/v1/mcp/call` | Executes a single tool. Body: `{"tool": "<name>", "arguments": {...}}` |

Auth is the same Bearer token used everywhere else (`sk_live_...` / `sk_test_...`). The MCP surface is opt-in; enable it with `MCP_ENABLED=true` (default `false`).

---

## Connecting from Claude Desktop

JustHereToListen.io is an **HTTP-transport** MCP server. Add it to Claude Desktop with the `mcp-remote` shim:

`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) /
`%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "justheretolisten": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://api.justheretolisten.io/api/v1/mcp",
        "--header",
        "Authorization: Bearer sk_live_your_key_here"
      ]
    }
  }
}
```

Restart Claude Desktop. Type "List my last 5 meetings" — Claude will call `list_meetings` automatically.

### Cursor / Cline / Continue

Same `mcp-remote` shim, dropped into each editor's MCP config block. The base URL and the `Authorization` header are the only two values you change between clients.

### Self-hosted

Replace `https://api.justheretolisten.io` with your deployment's base URL (e.g. `http://localhost:8000`) and use any valid API key for that instance.

---

## Quick test from the shell

```bash
# 1. List the server manifest
curl https://api.justheretolisten.io/api/v1/mcp/schema \
  -H "Authorization: Bearer sk_live_your_key_here"

# 2. Call a tool
curl -X POST https://api.justheretolisten.io/api/v1/mcp/call \
  -H "Authorization: Bearer sk_live_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{"tool": "list_meetings", "arguments": {"limit": 5}}'
```

Response shape:

```json
{
  "tool": "list_meetings",
  "result": {
    "meetings": [ { "id": "bot_8a72c5e1", "platform": "zoom", "status": "done", "...": "..." } ],
    "total": 1
  }
}
```

Errors come back as HTTP 400 with `{"detail": "..."}`.

---

## Available tools (16)

All tools are scoped to the calling account — you never see meetings that don't belong to your tenant. Some tools require a per-bot opt-in flag (noted below).

### Read

| Tool | Required args | Optional args | Returns |
|---|---|---|---|
| `list_meetings` | — | `limit` (1–50, default 10), `status` | Recent bots with status, platform, duration, summary |
| `get_meeting` | `bot_id` | — | Full transcript (truncated), analysis, chapters, speaker stats |
| `search_meetings` | `query` | `limit` (1–50, default 20), `semantic` (bool) | Substring or embedding-cosine matches across all transcripts |
| `get_action_items` | — | `assignee`, `limit` (1–100, default 50) | Action items pulled from every meeting's analysis |
| `get_speaker_analytics` | `bot_id` | — | Talk time, sentiment, filler words per speaker |
| `get_meeting_cost_summary` | — | `days` (1–90, default 30) | Aggregated meeting + AI cost, by platform |
| `get_decisions` | `bot_id` | `kind` (`decision`\|`action`) | Decision moments — needs `enable_decision_detection` |
| `get_live_analytics` | `bot_id` | — | Latest live analytics snapshot — needs `enable_speaker_analytics` |
| `get_coaching_tips` | `bot_id` | `limit` (1–200, default 50) | Recent coaching tips — needs `enable_coaching` |
| `get_related_meetings` | `bot_id` | — | Semantically similar past meetings — needs `enable_cross_meeting_memory` |

### Write

| Tool | Required args | Optional args | Effect |
|---|---|---|---|
| `create_bot` | `meeting_url` | `bot_name`, `template`, `respond_on_mention` | Dispatches a new bot. Returns `{bot_id, status, platform}` |
| `cancel_bot` | `bot_id` | — | Cancels a running or scheduled bot |
| `set_agentic_instructions` | `bot_id` | `instructions` (≤20 items), `autonomy` (`off`\|`low`\|`medium`\|`high`) | Replaces the bot's agentic instruction list |
| `trigger_agentic_instruction` | `bot_id`, `index` | — | Manually fires one instruction by zero-based index |

### Reasoning

| Tool | Required args | Optional args | Returns |
|---|---|---|---|
| `get_meeting_brief` | `agenda` | `participants` (string[]) | Pre-meeting prep brief generated from your last 5 done meetings |
| `ask_chat_qa` | `bot_id`, `question` | — | Transcript-grounded answer for a single meeting |

---

## Example flows

### "Summarise everything I missed yesterday"

The model picks tools on its own. A typical trace:

1. `list_meetings({"limit": 20})` — find yesterday's meetings
2. `get_meeting({"bot_id": "..."})` × N — pull transcripts
3. The model synthesises the summary in-context

### "Schedule the 3pm Zoom"

1. `create_bot({"meeting_url": "https://zoom.us/j/...", "template": "sales"})`
2. Receives `{"bot_id": "bot_...", "status": "ready"}`
3. Subsequent `list_meetings` shows the new bot

### "What did Alex commit to in the standup?"

1. `search_meetings({"query": "Alex", "limit": 5})`
2. `ask_chat_qa({"bot_id": "...", "question": "What did Alex commit to?"})`

---

## Caveats

- `get_meeting` truncates `transcript` to the first 200 entries to fit the model's context window. For full text, fall back to `GET /api/v1/bot/{id}/transcript` over plain HTTP.
- `search_meetings` with `semantic=true` only matches bots whose `summary_embedding` has been generated (requires `GEMINI_API_KEY`).
- The four advanced tools (`get_decisions`, `get_live_analytics`, `get_coaching_tips`, `get_related_meetings`) return `{"error": "... not enabled"}` unless the corresponding feature flag was set when the bot was created.
- Tools enforce strict per-account ownership: a missing-or-not-owned bot returns the same error to prevent enumeration.

---

## See also

- [API.md](./API.md) — full HTTP surface
- [SDKs.md](./SDKs.md) — Python / TypeScript clients (also call `/mcp/call` for free)
- Server source: `backend/app/services/mcp_service.py`, `backend/app/api/mcp.py`
