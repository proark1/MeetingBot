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

import logging
import json
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── MCP Schema ────────────────────────────────────────────────────────────────

MCP_SERVER_MANIFEST = {
    "schema_version": "v1",
    "name": "justheretolisten.io",
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
                    "workspace_id": {"type": "string", "description": "Optional workspace ID; caller must have member role or higher."},
                    "respond_on_mention": {"type": "boolean", "description": "Whether the bot replies when its name is mentioned.", "default": True},
                    "allow_demo_mode": {"type": "boolean", "description": "Allow a demo transcript for recognized platforms that cannot be recorded live.", "default": False},
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
        {
            "name": "get_decisions",
            "description": "List decision and action moments detected during a meeting (requires enable_decision_detection).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "bot_id": {"type": "string", "description": "The bot/meeting ID."},
                    "kind": {"type": "string", "description": "Optional filter: 'decision' or 'action'."},
                },
                "required": ["bot_id"],
            },
        },
        {
            "name": "get_live_analytics",
            "description": "Get the latest live speaker-analytics snapshot (requires enable_speaker_analytics).",
            "input_schema": {
                "type": "object",
                "properties": {"bot_id": {"type": "string"}},
                "required": ["bot_id"],
            },
        },
        {
            "name": "get_coaching_tips",
            "description": "Get the most recent coaching tips emitted for a bot (requires enable_coaching).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "bot_id": {"type": "string"},
                    "limit": {"type": "integer", "description": "Max tips (1-200, default 50).", "default": 50},
                },
                "required": ["bot_id"],
            },
        },
        {
            "name": "get_related_meetings",
            "description": "List semantically related past meetings retrieved for this bot (requires enable_cross_meeting_memory).",
            "input_schema": {
                "type": "object",
                "properties": {"bot_id": {"type": "string"}},
                "required": ["bot_id"],
            },
        },
        {
            "name": "set_agentic_instructions",
            "description": "Replace the agentic delegation instructions and (optionally) autonomy level for a bot.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "bot_id": {"type": "string"},
                    "instructions": {
                        "type": "array",
                        "description": "Replacement list of instructions. Each: {instruction, trigger, interval_seconds?, speak?, max_invocations?}",
                        "items": {"type": "object"},
                    },
                    "autonomy": {"type": "string", "description": "off | low | medium | high"},
                },
                "required": ["bot_id"],
            },
        },
        {
            "name": "trigger_agentic_instruction",
            "description": "Manually fire a single agentic instruction by index.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "bot_id": {"type": "string"},
                    "index": {"type": "integer", "description": "Zero-based instruction index."},
                },
                "required": ["bot_id", "index"],
            },
        },
        {
            "name": "ask_chat_qa",
            "description": "Ask a transcript-grounded question. The answer is generated from the bot's transcript.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "bot_id": {"type": "string"},
                    "question": {"type": "string"},
                },
                "required": ["bot_id", "question"],
            },
        },
    ],
}


# ── Visibility helpers ────────────────────────────────────────────────────────

def _bot_to_meeting_summary(bot) -> dict:
    return {
        "id": bot.id,
        "meeting_url": bot.meeting_url,
        "platform": bot.meeting_platform,
        "status": bot.status,
        "bot_name": bot.bot_name,
        "participants": bot.participants[:10],
        "duration_seconds": bot.duration_seconds,
        "created_at": bot.created_at.isoformat() if bot.created_at else None,
        "summary": (bot.analysis or {}).get("summary", "") if bot.analysis else "",
    }


def _bot_to_meeting_detail(bot) -> dict:
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


def _snapshot_to_bot(snap):
    from app.store import BotSession
    from app.services.secrets_at_rest import decrypt_text

    data = json.loads(decrypt_text(snap.data) or "{}")
    bot = BotSession.from_state_dict(data)
    # Older snapshots may predate lifted query columns; trust scalar columns for
    # access fields if the blob did not carry them.
    bot.account_id = bot.account_id if bot.account_id is not None else snap.account_id
    bot.sub_user_id = bot.sub_user_id if bot.sub_user_id is not None else getattr(snap, "sub_user_id", None)
    bot.workspace_id = bot.workspace_id if bot.workspace_id is not None else getattr(snap, "workspace_id", None)
    return bot


async def _visible_snapshot_conditions(account_id: Optional[str], status: Optional[str] = None):
    from app.api.bots import _workspace_ids_for_account
    from app.deps import SUPERADMIN_ACCOUNT_ID
    from app.models.account import BotSnapshot
    from sqlalchemy import or_

    conditions = []
    if status:
        conditions.append(BotSnapshot.status == status)
    if account_id == SUPERADMIN_ACCOUNT_ID:
        return conditions
    if not account_id:
        conditions.append(BotSnapshot.account_id.is_(None))
        return conditions

    visible = [BotSnapshot.account_id == account_id]
    workspace_ids = await _workspace_ids_for_account(account_id)
    if workspace_ids:
        visible.append(BotSnapshot.workspace_id.in_(workspace_ids))
    conditions.append(or_(*visible))
    return conditions


async def _visible_snapshot_bots(
    account_id: Optional[str],
    *,
    status: Optional[str] = None,
    limit: int = 1000,
) -> list:
    from app.db import AsyncSessionLocal
    from app.models.account import BotSnapshot
    from sqlalchemy import select

    conditions = await _visible_snapshot_conditions(account_id, status)
    query = select(BotSnapshot)
    for condition in conditions:
        query = query.where(condition)
    query = query.order_by(BotSnapshot.created_at.desc()).limit(limit)

    bots = []
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(query)).scalars().all()
    for snap in rows:
        try:
            bots.append(_snapshot_to_bot(snap))
        except Exception as exc:
            logger.warning("MCP skipped unreadable bot snapshot %s: %s", getattr(snap, "id", "?"), exc)
    return bots


async def _visible_bots(
    account_id: Optional[str],
    *,
    status: Optional[str] = None,
    limit: int = 10000,
) -> list:
    from app.api.bots import _list_visible_bots

    live_bots, _, _ = await _list_visible_bots(
        account_id=account_id,
        sub_user_id=None,
        status=status,
        limit=limit,
    )
    visible: dict[str, object] = {bot.id: bot for bot in live_bots}

    # Add terminal snapshots that have fallen out of the in-memory 24h window.
    snapshot_bots = await _visible_snapshot_bots(account_id, status=status, limit=limit)
    for bot in snapshot_bots:
        visible.setdefault(bot.id, bot)

    bots = sorted(
        visible.values(),
        key=lambda b: (-(b.created_at.timestamp() if b.created_at else 0), b.id),
    )
    return bots[:limit]


async def _resolve_visible_bot(bot_id: str, account_id: Optional[str]):
    from app.api.bots import _get_or_404
    from app.db import AsyncSessionLocal
    from app.models.account import BotSnapshot
    from fastapi import HTTPException
    from sqlalchemy import select

    try:
        return await _get_or_404(bot_id, account_id)
    except HTTPException:
        pass

    conditions = await _visible_snapshot_conditions(account_id)
    query = select(BotSnapshot).where(BotSnapshot.id == bot_id)
    for condition in conditions:
        query = query.where(condition)
    async with AsyncSessionLocal() as db:
        snap = (await db.execute(query)).scalar_one_or_none()
    if snap is None:
        return None
    try:
        return _snapshot_to_bot(snap)
    except Exception as exc:
        logger.warning("MCP could not read bot snapshot %s: %s", bot_id, exc)
        return None


async def _resolve_live_bot_for_mutation(
    bot_id: str,
    account_id: Optional[str],
    *,
    workspace_role: str = "admin",
):
    from app.api.bots import _check_workspace_role, _get_or_404
    from fastapi import HTTPException

    try:
        bot = await _get_or_404(bot_id, account_id)
        await _check_workspace_role(bot, account_id, workspace_role)
        return bot, None
    except HTTPException as exc:
        return None, str(exc.detail)


# ── Tool implementations ───────────────────────────────────────────────────────

async def _tool_list_meetings(args: dict, account_id: Optional[str]) -> dict:
    limit = min(max(int(args.get("limit", 10)), 1), 50)
    status_filter = args.get("status")
    bots = await _visible_bots(account_id, status=status_filter, limit=limit)
    meetings = [_bot_to_meeting_summary(bot) for bot in bots]

    return {"meetings": meetings, "total": len(meetings)}


async def _tool_get_meeting(args: dict, account_id: Optional[str]) -> dict:
    bot_id = args.get("bot_id", "").strip()
    if not bot_id:
        return {"error": "bot_id is required"}

    bot = await _resolve_visible_bot(bot_id, account_id)
    if bot is None:
        return {"error": f"Meeting {bot_id!r} not found"}

    return _bot_to_meeting_detail(bot)


async def _tool_search_meetings(args: dict, account_id: Optional[str]) -> dict:
    query = args.get("query", "").strip()
    if not query:
        return {"error": "query is required"}

    limit = min(max(int(args.get("limit", 20)), 1), 50)
    semantic = bool(args.get("semantic", False))
    all_bots = await _visible_bots(account_id, limit=10000)

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
    limit = min(max(int(args.get("limit", 50)), 1), 100)
    assignee_filter = (args.get("assignee") or "").lower().strip()

    all_bots = await _visible_bots(account_id, limit=10000)
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
        recent_bots = await _visible_bots(account_id, status="done", limit=5)
        for bot in recent_bots:
            if bot.analysis:
                s = bot.analysis.get("summary", "")
                if s:
                    previous_summaries.append(s)
    except Exception:
        pass

    result = await generate_meeting_brief(agenda, participants, previous_summaries)
    return result


async def _tool_create_bot(args: dict, account_id: Optional[str], is_sandbox: bool = False) -> dict:
    from app.schemas.bot import BotCreate

    meeting_url = args.get("meeting_url", "").strip()
    if not meeting_url:
        return {"error": "meeting_url is required"}

    try:
        payload = BotCreate(
            meeting_url=meeting_url,  # type: ignore[arg-type]
            bot_name=args.get("bot_name", "JustHereToListen.io"),
            template=args.get("template"),
            workspace_id=args.get("workspace_id"),
            respond_on_mention=args.get("respond_on_mention", True),
            allow_demo_mode=bool(args.get("allow_demo_mode", False)),
        )
    except Exception as exc:
        return {"error": f"Invalid arguments: {exc}"}

    from app.api.bots import create_bot_with_guardrails

    bot, _replayed = await create_bot_with_guardrails(
        payload,
        account_id=account_id,
        is_sandbox=is_sandbox,
    )
    result = {
        "bot_id": bot.id,
        "status": bot.status,
        "platform": bot.meeting_platform,
    }
    if is_sandbox:
        result["sandbox"] = True
    return result


async def _tool_cancel_bot(args: dict, account_id: Optional[str]) -> dict:
    from app.store import store
    from app.api.bots import _running_tasks

    bot_id = args.get("bot_id", "").strip()
    if not bot_id:
        return {"error": "bot_id is required"}

    bot, err = await _resolve_live_bot_for_mutation(bot_id, account_id, workspace_role="admin")
    if bot is None:
        return {"error": err or f"Bot {bot_id!r} not found"}

    task = _running_tasks.pop(bot_id, None)
    if task and not task.done():
        task.cancel()
    await store.update_bot(bot_id, status="cancelled")
    return {"bot_id": bot_id, "status": "cancelled"}


async def _tool_get_speaker_analytics(args: dict, account_id: Optional[str]) -> dict:
    bot_id = args.get("bot_id", "").strip()
    if not bot_id:
        return {"error": "bot_id is required"}

    bot = await _resolve_visible_bot(bot_id, account_id)
    if bot is None:
        return {"error": f"Bot {bot_id!r} not found"}

    return {
        "bot_id": bot_id,
        "speaker_stats": bot.speaker_stats,
        "participant_count": len(bot.participants),
        "participants": bot.participants,
    }


async def _tool_get_meeting_cost_summary(args: dict, account_id: Optional[str]) -> dict:
    from datetime import timedelta

    days = min(max(int(args.get("days", 30)), 1), 90)

    all_bots = await _visible_bots(account_id, limit=10000)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    total_meeting_cost = 0.0
    total_ai_cost = 0.0
    by_platform: dict[str, dict] = {}

    for bot in all_bots:
        if bot.created_at and (bot.created_at if bot.created_at.tzinfo else bot.created_at.replace(tzinfo=timezone.utc)) < cutoff:
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


async def _resolve_owned_bot(bot_id: str, account_id: Optional[str]):
    """Look up a bot, including workspace-visible live bots and snapshots."""
    return await _resolve_visible_bot(bot_id, account_id)


async def _tool_get_decisions(args: dict, account_id: Optional[str]) -> dict:
    bot_id = (args.get("bot_id") or "").strip()
    if not bot_id:
        return {"error": "bot_id is required"}
    bot = await _resolve_owned_bot(bot_id, account_id)
    if bot is None:
        return {"error": f"Bot {bot_id!r} not found"}
    if not getattr(bot, "enable_decision_detection", False):
        return {"error": "Decision detection is not enabled for this bot"}
    decisions = list(getattr(bot, "detected_decisions", []) or [])
    kind = (args.get("kind") or "").strip()
    if kind in ("decision", "action"):
        decisions = [d for d in decisions if d.get("kind") == kind]
    return {"bot_id": bot_id, "decisions": decisions, "total": len(decisions)}


async def _tool_get_live_analytics(args: dict, account_id: Optional[str]) -> dict:
    bot_id = (args.get("bot_id") or "").strip()
    if not bot_id:
        return {"error": "bot_id is required"}
    bot = await _resolve_owned_bot(bot_id, account_id)
    if bot is None:
        return {"error": f"Bot {bot_id!r} not found"}
    if not getattr(bot, "enable_speaker_analytics", False):
        return {"error": "Live speaker analytics is not enabled for this bot"}
    snaps = list(getattr(bot, "speaker_analytics_snapshots", []) or [])
    return {
        "bot_id": bot_id,
        "latest": snaps[-1] if snaps else None,
        "history_count": len(snaps),
    }


async def _tool_get_coaching_tips(args: dict, account_id: Optional[str]) -> dict:
    bot_id = (args.get("bot_id") or "").strip()
    if not bot_id:
        return {"error": "bot_id is required"}
    bot = await _resolve_owned_bot(bot_id, account_id)
    if bot is None:
        return {"error": f"Bot {bot_id!r} not found"}
    if not getattr(bot, "enable_coaching", False):
        return {"error": "Host coaching is not enabled for this bot"}
    limit = min(max(int(args.get("limit", 50)), 1), 200)
    tips = list(getattr(bot, "coaching_tips", []) or [])
    return {"bot_id": bot_id, "tips": tips[-limit:], "total": len(tips)}


async def _tool_get_related_meetings(args: dict, account_id: Optional[str]) -> dict:
    bot_id = (args.get("bot_id") or "").strip()
    if not bot_id:
        return {"error": "bot_id is required"}
    bot = await _resolve_owned_bot(bot_id, account_id)
    if bot is None:
        return {"error": f"Bot {bot_id!r} not found"}
    if not getattr(bot, "enable_cross_meeting_memory", False):
        return {"error": "Cross-meeting memory is not enabled for this bot"}
    return {
        "bot_id": bot_id,
        "related_meetings": list(getattr(bot, "related_meetings", []) or []),
    }


async def _tool_set_agentic_instructions(args: dict, account_id: Optional[str]) -> dict:
    from app.store import store
    bot_id = (args.get("bot_id") or "").strip()
    if not bot_id:
        return {"error": "bot_id is required"}
    bot, err = await _resolve_live_bot_for_mutation(bot_id, account_id, workspace_role="admin")
    if bot is None:
        return {"error": err or f"Bot {bot_id!r} not found"}
    instructions = args.get("instructions") or []
    if not isinstance(instructions, list) or len(instructions) > 20:
        return {"error": "instructions must be a list of at most 20 items"}
    update: dict = {"agentic_instructions": instructions}
    autonomy = args.get("autonomy")
    if autonomy is not None:
        if autonomy not in ("off", "low", "medium", "high"):
            return {"error": "autonomy must be off|low|medium|high"}
        update["agentic_autonomy"] = autonomy
    await store.update_bot(bot_id, **update)
    return {"bot_id": bot_id, **update}


async def _tool_trigger_agentic_instruction(args: dict, account_id: Optional[str]) -> dict:
    bot_id = (args.get("bot_id") or "").strip()
    index = args.get("index")
    if not bot_id or not isinstance(index, int):
        return {"error": "bot_id and integer index are required"}
    bot, err = await _resolve_live_bot_for_mutation(bot_id, account_id, workspace_role="admin")
    if bot is None:
        return {"error": err or f"Bot {bot_id!r} not found"}
    if (getattr(bot, "agentic_autonomy", "off") or "off") == "off":
        return {"error": "Agentic autonomy is off for this bot"}
    instructions = list(getattr(bot, "agentic_instructions", []) or [])
    if not (0 <= index < len(instructions)):
        return {"error": f"No instruction at index {index}"}
    from app.services.agentic_service import AgenticEngine, deliver_action
    engine = AgenticEngine(bot)
    action = await engine.trigger_manual(index)
    if not action:
        return {"bot_id": bot_id, "delivered": False, "reason": "engine declined to act"}
    delivered = await deliver_action(bot, action)
    return {"bot_id": bot_id, "action": action, "delivered": delivered}


async def _tool_ask_chat_qa(args: dict, account_id: Optional[str]) -> dict:
    from app.services import intelligence_service as _is
    bot_id = (args.get("bot_id") or "").strip()
    question = (args.get("question") or "").strip()
    if not bot_id or not question:
        return {"error": "bot_id and question are required"}
    bot = await _resolve_owned_bot(bot_id, account_id)
    if bot is None:
        return {"error": f"Bot {bot_id!r} not found"}
    answer = await _is.ask_about_transcript(list(bot.transcript or []), question)
    return {"bot_id": bot_id, "question": question, "answer": answer}


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
    "get_decisions": _tool_get_decisions,
    "get_live_analytics": _tool_get_live_analytics,
    "get_coaching_tips": _tool_get_coaching_tips,
    "get_related_meetings": _tool_get_related_meetings,
    "set_agentic_instructions": _tool_set_agentic_instructions,
    "trigger_agentic_instruction": _tool_trigger_agentic_instruction,
    "ask_chat_qa": _tool_ask_chat_qa,
}


async def execute_tool(tool_name: str, args: dict, account_id: Optional[str], is_sandbox: bool = False) -> dict:
    """Execute an MCP tool by name and return the result."""
    handler = _TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return {"error": f"Unknown tool: {tool_name!r}"}
    try:
        if tool_name == "create_bot":
            return await handler(args, account_id, is_sandbox)
        return await handler(args, account_id)
    except Exception as exc:
        try:
            from fastapi import HTTPException
            if isinstance(exc, HTTPException):
                return {"error": str(exc.detail)}
        except Exception:
            pass
        logger.error("MCP tool %s error: %s", tool_name, exc)
        return {"error": f"Tool execution failed: {exc}"}
