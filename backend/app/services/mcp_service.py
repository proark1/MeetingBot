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
                    "semantic": {
                        "type": "boolean",
                        "description": "Use semantic (embedding-based) search instead of substring match.",
                        "default": False,
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
        {
            "name": "create_bot",
            "description": "Create and dispatch a meeting bot to join a meeting URL.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "meeting_url": {"type": "string", "description": "Full Zoom/Meet/Teams URL."},
                    "bot_name": {"type": "string", "description": "Display name for the bot.", "default": "JustHereToListen.io"},
                    "template": {"type": "string", "description": "Analysis template (default, sales, standup, 1on1, retro, etc.)."},
                    "respond_on_mention": {"type": "boolean", "description": "Whether the bot replies when its name is mentioned.", "default": True},
                },
                "required": ["meeting_url"],
            },
        },
        {
            "name": "cancel_bot",
            "description": "Cancel a running or scheduled meeting bot.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "bot_id": {"type": "string", "description": "The bot ID to cancel."},
                },
                "required": ["bot_id"],
            },
        },
        {
            "name": "get_speaker_analytics",
            "description": "Get detailed speaker analytics for a meeting, including talk time, sentiment, and filler words.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "bot_id": {"type": "string", "description": "The bot/meeting ID."},
                },
                "required": ["bot_id"],
            },
        },
        {
            "name": "get_meeting_cost_summary",
            "description": "Aggregate meeting cost and AI usage costs across recent meetings, broken down by platform.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Number of past days to include (1-90, default 30).", "default": 30},
                },
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
    semantic = bool(args.get("semantic", False))
    filter_account = account_id if (account_id and account_id != SUPERADMIN_ACCOUNT_ID) else None

    all_bots, _ = await store.list_bots(limit=10000, account_id=filter_account)

    if semantic:
        from app.services.intelligence_service import embed_text
        import math
        query_embedding = await embed_text(query)
        if query_embedding:
            def _cosine(a: list, b: list) -> float:
                dot = sum(x * y for x, y in zip(a, b))
                mag_a = math.sqrt(sum(x * x for x in a))
                mag_b = math.sqrt(sum(x * x for x in b))
                return dot / (mag_a * mag_b) if mag_a and mag_b else 0.0

            scored = []
            for bot in all_bots:
                if bot.summary_embedding:
                    score = _cosine(query_embedding, bot.summary_embedding)
                    if score >= 0.6:
                        scored.append((score, bot))
            scored.sort(key=lambda x: x[0], reverse=True)
            results = [
                {
                    "bot_id": bot.id,
                    "meeting_url": bot.meeting_url,
                    "platform": bot.meeting_platform,
                    "score": round(score, 3),
                    "summary": (bot.analysis or {}).get("summary", ""),
                }
                for score, bot in scored[:limit]
            ]
            return {"query": query, "semantic": True, "total": len(results), "results": results}

    # Substring fallback
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

    return {"query": query, "semantic": False, "total": len(matches), "results": matches}


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


async def _tool_create_bot(args: dict, account_id: Optional[str]) -> dict:
    from app.schemas.bot import BotCreate
    from app.services import bot_service
    from app.store import store

    meeting_url = args.get("meeting_url", "").strip()
    if not meeting_url:
        return {"error": "meeting_url is required"}

    try:
        payload = BotCreate(
            meeting_url=meeting_url,  # type: ignore[arg-type]
            bot_name=args.get("bot_name", "JustHereToListen.io"),
            template=args.get("template"),
            respond_on_mention=args.get("respond_on_mention", True),
        )
    except Exception as exc:
        return {"error": f"Invalid arguments: {exc}"}

    import uuid
    from datetime import datetime, timezone
    from app.store import BotSession, _now
    from app.config import settings

    bot_id = str(uuid.uuid4())
    platform = bot_service._detect_platform(str(payload.meeting_url))
    bot = BotSession(
        id=bot_id,
        meeting_url=str(payload.meeting_url),
        meeting_platform=platform,
        bot_name=payload.bot_name,
        account_id=account_id,
        template=payload.template,
        respond_on_mention=payload.respond_on_mention,
    )
    await store.new_bot(bot)
    use_real_bot = bool(settings.USE_REAL_BOT)
    import asyncio as _asyncio
    task = _asyncio.create_task(bot_service.run_bot_lifecycle(bot_id, use_real_bot))
    from app.api.bots import _running_tasks
    _running_tasks[bot_id] = task
    return {"bot_id": bot_id, "status": bot.status, "platform": platform}


async def _tool_cancel_bot(args: dict, account_id: Optional[str]) -> dict:
    from app.store import store
    from app.deps import SUPERADMIN_ACCOUNT_ID
    from app.api.bots import _running_tasks

    bot_id = args.get("bot_id", "").strip()
    if not bot_id:
        return {"error": "bot_id is required"}

    bot = await store.get_bot(bot_id)
    if bot is None:
        return {"error": f"Bot {bot_id!r} not found"}
    if (
        account_id
        and account_id != SUPERADMIN_ACCOUNT_ID
        and bot.account_id is not None
        and bot.account_id != account_id
    ):
        return {"error": f"Bot {bot_id!r} not found"}

    task = _running_tasks.pop(bot_id, None)
    if task and not task.done():
        task.cancel()
    await store.update_bot(bot_id, status="cancelled")
    return {"bot_id": bot_id, "status": "cancelled"}


async def _tool_get_speaker_analytics(args: dict, account_id: Optional[str]) -> dict:
    from app.store import store
    from app.deps import SUPERADMIN_ACCOUNT_ID

    bot_id = args.get("bot_id", "").strip()
    if not bot_id:
        return {"error": "bot_id is required"}

    bot = await store.get_bot(bot_id)
    if bot is None:
        return {"error": f"Bot {bot_id!r} not found"}
    if (
        account_id
        and account_id != SUPERADMIN_ACCOUNT_ID
        and bot.account_id is not None
        and bot.account_id != account_id
    ):
        return {"error": f"Bot {bot_id!r} not found"}

    return {
        "bot_id": bot_id,
        "speaker_stats": bot.speaker_stats,
        "participant_count": len(bot.participants),
        "participants": bot.participants,
    }


async def _tool_get_meeting_cost_summary(args: dict, account_id: Optional[str]) -> dict:
    from app.store import store
    from app.deps import SUPERADMIN_ACCOUNT_ID
    from datetime import datetime, timezone, timedelta

    days = min(max(int(args.get("days", 30)), 1), 90)
    filter_account = account_id if (account_id and account_id != SUPERADMIN_ACCOUNT_ID) else None

    all_bots, _ = await store.list_bots(limit=10000, account_id=filter_account)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    total_meeting_cost = 0.0
    total_ai_cost = 0.0
    by_platform: dict[str, dict] = {}

    for bot in all_bots:
        if bot.created_at and bot.created_at.replace(tzinfo=timezone.utc if bot.created_at.tzinfo is None else bot.created_at.tzinfo) < cutoff:
            continue
        plat = bot.meeting_platform or "unknown"
        entry = by_platform.setdefault(plat, {"meeting_cost_usd": 0.0, "ai_cost_usd": 0.0, "count": 0})
        entry["count"] += 1
        if bot.meeting_cost_usd:
            entry["meeting_cost_usd"] += bot.meeting_cost_usd
            total_meeting_cost += bot.meeting_cost_usd
        entry["ai_cost_usd"] += bot.ai_total_cost_usd
        total_ai_cost += bot.ai_total_cost_usd

    return {
        "days": days,
        "total_meeting_cost_usd": round(total_meeting_cost, 4),
        "total_ai_cost_usd": round(total_ai_cost, 4),
        "by_platform": {k: {**v, "meeting_cost_usd": round(v["meeting_cost_usd"], 4), "ai_cost_usd": round(v["ai_cost_usd"], 4)} for k, v in by_platform.items()},
    }


# ── Dispatch ──────────────────────────────────────────────────────────────────

_TOOL_HANDLERS = {
    "list_meetings": _tool_list_meetings,
    "get_meeting": _tool_get_meeting,
    "search_meetings": _tool_search_meetings,
    "get_action_items": _tool_get_action_items,
    "get_meeting_brief": _tool_get_meeting_brief,
    "create_bot": _tool_create_bot,
    "cancel_bot": _tool_cancel_bot,
    "get_speaker_analytics": _tool_get_speaker_analytics,
    "get_meeting_cost_summary": _tool_get_meeting_cost_summary,
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
