"""Model Context Protocol (MCP) server API.

Exposes JustHereToListen.io as an MCP server so AI assistants (Claude Desktop, etc.)
can discover and call meeting tools.

Endpoints:
  GET  /api/v1/mcp/schema   — MCP server manifest (tool definitions)
  POST /api/v1/mcp/call     — Execute a named tool with arguments

Authentication: uses the same Bearer token as all other API endpoints.
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from pathlib import Path
from typing import Any, Optional

from app.config import settings

_VERSION_FILE = Path(__file__).resolve().parents[3] / "VERSION"
_APP_VERSION = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "2.68.2"

router = APIRouter(prefix="/mcp", tags=["MCP"])


class McpToolCall(BaseModel):
    tool: str = Field(description="Name of the tool to execute.")
    arguments: dict[str, Any] = Field(default={}, description="Tool input arguments.")

    model_config = {"json_schema_extra": {"examples": [
        {"tool": "list_meetings", "arguments": {"limit": 5}},
        {"tool": "search_meetings", "arguments": {"query": "v2 onboarding"}},
        {"tool": "get_meeting", "arguments": {"bot_id": "bot_8a72c5e1"}},
    ]}}


@router.get(
    "/schema",
    summary="MCP server manifest",
    responses={200: {"content": {"application/json": {"example": {
        "name": "justheretolisten.io",
        "version": _APP_VERSION,
        "description": "MCP server for JustHereToListen.io meeting bots.",
        "tools": [
            {"name": "list_meetings", "description": "List recent meetings."},
            {"name": "get_meeting", "description": "Fetch a single meeting by id."},
            {"name": "search_meetings", "description": "Semantic search across past meetings."},
            {"name": "get_action_items", "description": "List open action items."},
            {"name": "get_meeting_brief", "description": "Generate a pre-meeting brief."},
        ],
    }}}}},
)
async def get_mcp_schema():
    """Return the MCP server manifest with tool definitions.

    This endpoint conforms to the Model Context Protocol specification.
    Use it to register JustHereToListen.io as an MCP server in Claude Desktop or
    other MCP-compatible AI assistants.
    """
    if not settings.MCP_ENABLED:
        raise HTTPException(status_code=501, detail="MCP server is not enabled (set MCP_ENABLED=true)")

    from app.services.mcp_service import MCP_SERVER_MANIFEST
    return MCP_SERVER_MANIFEST


@router.post(
    "/call",
    summary="Execute MCP tool",
    responses={200: {"content": {"application/json": {"example": {
        "tool": "list_meetings",
        "result": {
            "meetings": [
                {"id": "bot_8a72c5e1", "title": "Sales sync", "date": "2026-05-04", "summary": "Team agreed to ship v2."},
            ],
            "total": 1,
        },
    }}}}},
)
async def call_mcp_tool(payload: McpToolCall, request: Request):
    """Execute an MCP tool and return the result.

    Supported tools: list_meetings, get_meeting, search_meetings,
    get_action_items, get_meeting_brief.
    """
    if not settings.MCP_ENABLED:
        raise HTTPException(status_code=501, detail="MCP server is not enabled (set MCP_ENABLED=true)")

    account_id: Optional[str] = getattr(request.state, "account_id", None)
    is_sandbox = bool(getattr(request.state, "sandbox", False))

    from app.services.mcp_service import execute_tool
    result = await execute_tool(payload.tool, payload.arguments, account_id, is_sandbox=is_sandbox)

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return {"tool": payload.tool, "result": result}
