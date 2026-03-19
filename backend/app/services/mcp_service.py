"""Model Context Protocol (MCP) server implementation.

Exposes JustHereToListen.io data as MCP tools so AI assistants (Claude, etc.) can
query meetings, transcripts, and action items in real-time.

MCP endpoint: GET /api/v1/mcp/schema — returns the MCP server manifest
Tool execution: POST /api/v1/mcp/call — executes a named tool

Supported tools:
  list_meetings      — list recent meetings with status/platform/duration
  get_meeting        — get full transcript and analysis for a meeting by ID
  search_meetings    — search transcript text across all meetings
  get_action_items   — retrieve action items across all meetings
  get_meeting_brief  — pre-meeting preparation brief (agenda + talking points)
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── MCP Schema ────────────────────────────────────────────────────────────────

MCP_SERVER_MANIFEST = {
    "schema_version": "v1",
    "name": "meetingbot",
    "description": (
        "Access meeting transcripts, analysis, action items, and search across "
        "all meetings recorded by JustHereToListen.io."
    ),
    "tools": [
        {
            "name": "list_meetings",
            "description": "List recent meeting recordings with status, platform, duration, and participants.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Number of meetings to return (1-50, default 10).",
                        "default": 10,
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter by status: done, error, in_call, etc.",
                    },
                },
            },
        },
        {
            "name": "get_meeting",
            "description": "Get the full transcript and AI analysis for a specific meeting.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "bot_id": {
                        "type": "string",
                        "description": "The bot/meeting ID.",
                    },
                },
                "required": ["bot_id"],
            },
        },
        {
            "name": "search_meetings",
            "description": "Search transcript text across all recent meetings.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Text to search for in transcripts.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (1-50, default 20).",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_action_items",
            "description": "Retrieve all action items across recent meetings, optionally filtered by assignee.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "assignee": {
                        "type": "string",
                        "description": "Filter by assignee name (case-insensitive substring match).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max items to return (1-100, default 50).",
                        "default": 50,
                    },
                },
            },
        },
        {
            "name": "get_meeting_brief",
            "description": "Generate a pre-meeting preparation brief with talking points and questions to raise.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "agenda": {
                        "type": "string",
                        "description": "The meeting agenda or topic description.",
                    },
                    "participants": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of expected participant names.",
                    },
                },
                "required": ["agenda"],
            },
        },
    ],
}


# ── Tool implementations ───────────────────────────────────────────────────────

async def _tool_list_meetings(args: dict, account_id: Optional[str]) -> dict:
    from app.store import store
    from app.deps import SUPERADMIN_ACCOUNT_ID

    limit = min(max(int(args.get("limit", 10)), 1), 50)
    status_filter = args.get("status")
    filter_account = account_id if (account_id and account_id != SUPERADMIN_ACCOUNT_ID) else None

    bots, total = await store.list_bots(
        limit=limit,
        status=status_filter,
        account_id=filter_account,
    )

    meetings = []
    for bot in bots:
        meetings.append({
            "id": bot.id,
            "meeting_url": bot.meeting_url,
            "platform": bot.meeting_platform,
            "status": bot.status,
            "bot_name": bot.bot_name,
            "participants": bot.participants[:10],
            "duration_seconds": bot.duration_seconds,
            "created_at": bot.created_at.isoformat() if bot.created_at else None,
            "summary": (bot.analysis or {}).get("summary", "") if bot.analysis else "",
        })

    return {"meetings": meetings, "total": total}


async def _tool_get_meeting(args: dict, account_id: Optional[str]) -> dict:
    from app.store import store
    from app.deps import SUPERADMIN_ACCOUNT_ID

    bot_id = args.get("bot_id", "").strip()
    if not bot_id:
        return {"error": "bot_id is required"}

    bot = await store.get_bot(bot_id)
    if bot is None:
        return {"error": f"Meeting {bot_id!r} not found"}

    # Ownership check
    if (
        account_id
        and account_id != SUPERADMIN_ACCOUNT_ID
        and bot.account_id is not None
        and bot.account_id != account_id
    ):
        return {"error": f"Meeting {bot_id!r} not found"}

    return {
        "id": bot.id,
        "meeting_url": bot.meeting_url,
        "platform": bot.meeting_platform,
        "status": bot.status,
        "participants": bot.participants,
        "duration_seconds": bot.duration_seconds,
        "transcript": bot.transcript[:200],  # cap for context window
        "analysis": bot.analysis,
        "chapters": bot.chapters,
        "speaker_stats": bot.speaker_stats,
        "created_at": bot.created_at.isoformat() if bot.created_at else None,
    }


async def _tool_search_meetings(args: dict, account_id: Optional[str]) -> dict:
    from app.store import store
    from app.deps import SUPERADMIN_ACCOUNT_ID

    query = args.get("query", "").strip()
    if not query:
        return {"error": "query is required"}

    limit = min(max(int(args.get("limit", 20)), 1), 50)
    filter_account = account_id if (account_id and account_id != SUPERADMIN_ACCOUNT_ID) else None

    all_bots, _ = await store.list_bots(limit=10000, account_id=filter_account)
    q_lower = query.lower()
    matches = []

    for bot in all_bots:
        for entry in bot.transcript:
            text = entry.get("text", "") or ""
            if q_lower in text.lower():
                matches.append({
                    "bot_id": bot.id,
                    "meeting_url": bot.meeting_url,
                    "platform": bot.meeting_platform,
                    "speaker": entry.get("speaker"),
                    "text": text,
                    "timestamp": entry.get("timestamp"),
                })
                if len(matches) >= limit:
                    break
        if len(matches) >= limit:
            break

    return {"query": query, "total": len(matches), "results": matches}


async def _tool_get_action_items(args: dict, account_id: Optional[str]) -> dict:
    from app.store import store
    from app.deps import SUPERADMIN_ACCOUNT_ID

    limit = min(max(int(args.get("limit", 50)), 1), 100)
    assignee_filter = (args.get("assignee") or "").lower().strip()
    filter_account = account_id if (account_id and account_id != SUPERADMIN_ACCOUNT_ID) else None

    all_bots, _ = await store.list_bots(limit=10000, account_id=filter_account)
    items = []

    for bot in all_bots:
        if not bot.analysis:
            continue
        for item in bot.analysis.get("action_items", []):
            assignee = (item.get("assignee") or item.get("owner") or "Unassigned")
            if assignee_filter and assignee_filter not in assignee.lower():
                continue
            items.append({
                **item,
                "bot_id": bot.id,
                "meeting_url": bot.meeting_url,
                "platform": bot.meeting_platform,
                "created_at": bot.created_at.isoformat() if bot.created_at else None,
            })
            if len(items) >= limit:
                break
        if len(items) >= limit:
            break

    return {"total": len(items), "action_items": items}


async def _tool_get_meeting_brief(args: dict, account_id: Optional[str]) -> dict:
    from app.services.intelligence_service import generate_meeting_brief

    agenda = args.get("agenda", "").strip()
    if not agenda:
        return {"error": "agenda is required"}

    participants = args.get("participants") or []

    # Gather recent summaries for context
    previous_summaries: list[str] = []
    try:
        from app.store import store
        from app.deps import SUPERADMIN_ACCOUNT_ID
        filter_account = account_id if (account_id and account_id != SUPERADMIN_ACCOUNT_ID) else None
        recent_bots, _ = await store.list_bots(limit=5, status="done", account_id=filter_account)
        for bot in recent_bots:
            if bot.analysis:
                s = bot.analysis.get("summary", "")
                if s:
                    previous_summaries.append(s)
    except Exception:
        pass

    result = await generate_meeting_brief(agenda, participants, previous_summaries)
    return result


# ── Dispatch ──────────────────────────────────────────────────────────────────

_TOOL_HANDLERS = {
    "list_meetings": _tool_list_meetings,
    "get_meeting": _tool_get_meeting,
    "search_meetings": _tool_search_meetings,
    "get_action_items": _tool_get_action_items,
    "get_meeting_brief": _tool_get_meeting_brief,
}


async def execute_tool(tool_name: str, args: dict, account_id: Optional[str]) -> dict:
    """Execute an MCP tool by name and return the result."""
    handler = _TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return {"error": f"Unknown tool: {tool_name!r}"}
    try:
        return await handler(args, account_id)
    except Exception as exc:
        logger.error("MCP tool %s error: %s", tool_name, exc)
        return {"error": f"Tool execution failed: {exc}"}
