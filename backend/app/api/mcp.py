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
from typing import Any, Optional

from app.config import settings

router = APIRouter(prefix="/mcp", tags=["MCP"])


class McpToolCall(BaseModel):
    tool: str = Field(description="Name of the tool to execute.")
    arguments: dict[str, Any] = Field(default={}, description="Tool input arguments.")


@router.get("/schema", summary="MCP server manifest")
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


@router.post("/call", summary="Execute MCP tool")
async def call_mcp_tool(payload: McpToolCall, request: Request):
    """Execute an MCP tool and return the result.

    Supported tools: list_meetings, get_meeting, search_meetings,
    get_action_items, get_meeting_brief.
    """
    if not settings.MCP_ENABLED:
        raise HTTPException(status_code=501, detail="MCP server is not enabled (set MCP_ENABLED=true)")

    account_id: Optional[str] = getattr(request.state, "account_id", None)

    from app.services.mcp_service import execute_tool
    result = await execute_tool(payload.tool, payload.arguments, account_id)

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return {"tool": payload.tool, "result": result}
