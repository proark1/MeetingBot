"""Team workspaces API.

Workspaces provide shared contexts for teams. Members can view bots
belonging to the workspace based on their role (admin/member/viewer).

Workspace features:
- Shared bot history across workspace members
- Role-based access (admin / member / viewer)
- Workspace-level settings (default bot name, consent requirement, etc.)
- Owner transfers and member management
"""

import json
import re
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.deps import SUPERADMIN_ACCOUNT_ID

router = APIRouter(prefix="/workspaces", tags=["Workspaces"])


def _account_id(request: Request) -> str:
    """Extract required account_id from request state."""
    from fastapi import HTTPException
    account_id = getattr(request.state, "account_id", None)
    if not account_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    return account_id


class WorkspaceCreate(BaseModel):
    name: str = Field(max_length=100, description="Display name for the workspace.")
    slug: Optional[str] = Field(
        default=None,
        max_length=100,
        description="URL-safe identifier (auto-generated from name if omitted).",
    )
    settings: dict = Field(
        default={},
        description="Optional workspace settings (e.g. default_bot_name, require_consent).",
    )


class WorkspaceUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=100)
    settings: Optional[dict] = None


class WorkspaceMemberAdd(BaseModel):
    account_id: str = Field(description="Account ID of the member to add.")
    role: str = Field(
        default="member",
        description="Role: 'admin', 'member', or 'viewer'.",
    )


class WorkspaceMemberUpdate(BaseModel):
    role: str = Field(description="New role: 'admin', 'member', or 'viewer'.")


def _slugify(text: str) -> str:
    """Convert a name to a URL-safe slug."""
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "-", slug)
    slug = slug.strip("-")
    return slug or "workspace"


def _to_response(ws, member_role: Optional[str] = None) -> dict:
    try:
        settings = json.loads(ws.settings or "{}")
    except Exception:
        settings = {}
    return {
        "id": ws.id,
        "name": ws.name,
        "slug": ws.slug,
        "owner_account_id": ws.owner_account_id,
        "settings": settings,
        "is_active": ws.is_active,
        "created_at": ws.created_at.isoformat(),
        "updated_at": ws.updated_at.isoformat(),
        "member_role": member_role,
    }


def _member_to_response(member) -> dict:
    return {
        "id": member.id,
        "workspace_id": member.workspace_id,
        "account_id": member.account_id,
        "role": member.role,
        "invited_by": member.invited_by,
        "joined_at": member.joined_at.isoformat(),
    }


async def _get_workspace_with_access(workspace_id: str, account_id: str, db, require_role: str = "viewer"):
    """Load workspace and verify the caller has at least the specified role."""
    from app.models.account import Workspace, WorkspaceMember
    from sqlalchemy import select

    result = await db.execute(
        select(Workspace).where(Workspace.id == workspace_id, Workspace.is_active == True)  # noqa
    )
    ws = result.scalar_one_or_none()
    if ws is None:
        raise HTTPException(status_code=404, detail=f"Workspace {workspace_id!r} not found")

    # Owner always has admin access
    if ws.owner_account_id == account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        return ws, "admin"

    # Check membership
    m_result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.account_id == account_id,
        )
    )
    member = m_result.scalar_one_or_none()
    if member is None:
        # 404 (not 403) so we don't leak existence of the workspace per CLAUDE.md
        raise HTTPException(status_code=404, detail=f"Workspace {workspace_id!r} not found")

    role_order = {"viewer": 0, "member": 1, "admin": 2}
    if role_order.get(member.role, 0) < role_order.get(require_role, 0):
        raise HTTPException(
            status_code=403,
            detail=f"This action requires the '{require_role}' role",
        )

    return ws, member.role


@router.get("", summary="List workspaces")
async def list_workspaces(request: Request):
    """List all workspaces you are a member of (or own)."""
    account_id = _account_id(request)

    from app.db import AsyncSessionLocal
    from app.models.account import Workspace, WorkspaceMember
    from sqlalchemy import select, or_

    async with AsyncSessionLocal() as db:
        # Get workspaces where user is owner or member
        owned_result = await db.execute(
            select(Workspace).where(
                Workspace.owner_account_id == account_id,
                Workspace.is_active == True,  # noqa
            )
        )
        owned = {ws.id: (ws, "admin") for ws in owned_result.scalars().all()}

        member_result = await db.execute(
            select(WorkspaceMember, Workspace)
            .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
            .where(
                WorkspaceMember.account_id == account_id,
                Workspace.is_active == True,  # noqa
            )
        )
        for member, ws in member_result.all():
            if ws.id not in owned:
                owned[ws.id] = (ws, member.role)

    return [_to_response(ws, role) for ws, role in owned.values()]


@router.post("", status_code=201, summary="Create workspace")
async def create_workspace(payload: WorkspaceCreate, request: Request):
    """Create a new team workspace. You will be the owner with admin role."""
    account_id = _account_id(request)

    slug = payload.slug or _slugify(payload.name)

    from app.db import AsyncSessionLocal
    from app.models.account import Workspace, WorkspaceMember
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        # Ensure slug is unique
        existing = await db.execute(select(Workspace).where(Workspace.slug == slug))
        if existing.scalar_one_or_none():
            # Make slug unique by appending a short UUID fragment
            slug = f"{slug}-{str(uuid.uuid4())[:8]}"

        ws = Workspace(
            name=payload.name,
            slug=slug,
            owner_account_id=account_id,
            settings=json.dumps(payload.settings),
        )
        db.add(ws)
        await db.flush()

        # Add owner as admin member
        member = WorkspaceMember(
            workspace_id=ws.id,
            account_id=account_id,
            role="admin",
        )
        db.add(member)
        await db.commit()
        await db.refresh(ws)

    return _to_response(ws, "admin")


@router.get("/{workspace_id}", summary="Get workspace")
async def get_workspace(workspace_id: str, request: Request):
    """Get workspace details."""
    account_id = _account_id(request)

    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        ws, role = await _get_workspace_with_access(workspace_id, account_id, db)

    return _to_response(ws, role)


@router.patch("/{workspace_id}", summary="Update workspace")
async def update_workspace(workspace_id: str, payload: WorkspaceUpdate, request: Request):
    """Update workspace name or settings. Requires admin role."""
    account_id = _account_id(request)

    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        ws, role = await _get_workspace_with_access(workspace_id, account_id, db, require_role="admin")

        if payload.name is not None:
            ws.name = payload.name
        if payload.settings is not None:
            ws.settings = json.dumps(payload.settings)

        await db.commit()
        await db.refresh(ws)

    return _to_response(ws, role)


@router.delete("/{workspace_id}", status_code=204, summary="Delete workspace")
async def delete_workspace(workspace_id: str, request: Request):
    """Soft-delete a workspace. Requires owner access."""
    account_id = _account_id(request)

    from app.db import AsyncSessionLocal
    from app.models.account import Workspace
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        ws, role = await _get_workspace_with_access(workspace_id, account_id, db, require_role="admin")

        if ws.owner_account_id != account_id and account_id != SUPERADMIN_ACCOUNT_ID:
            raise HTTPException(status_code=403, detail="Only the workspace owner can delete it")

        ws.is_active = False
        await db.commit()


# ── Members ───────────────────────────────────────────────────────────────────

@router.get("/{workspace_id}/members", summary="List members")
async def list_workspace_members(workspace_id: str, request: Request):
    """List all members of a workspace."""
    account_id = _account_id(request)

    from app.db import AsyncSessionLocal
    from app.models.account import WorkspaceMember
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        await _get_workspace_with_access(workspace_id, account_id, db, require_role="viewer")

        result = await db.execute(
            select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id)
        )
        members = result.scalars().all()

    return [_member_to_response(m) for m in members]


@router.post("/{workspace_id}/members", status_code=201, summary="Add member")
async def add_workspace_member(workspace_id: str, payload: WorkspaceMemberAdd, request: Request):
    """Add a member to the workspace. Requires admin role."""
    account_id = _account_id(request)

    if payload.role not in ("admin", "member", "viewer"):
        raise HTTPException(status_code=422, detail="role must be 'admin', 'member', or 'viewer'")

    from app.db import AsyncSessionLocal
    from app.models.account import WorkspaceMember, Account
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        await _get_workspace_with_access(workspace_id, account_id, db, require_role="admin")

        # Verify target account exists
        acc_result = await db.execute(
            select(Account).where(Account.id == payload.account_id)
        )
        if acc_result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail=f"Account {payload.account_id!r} not found")

        # Check if already a member
        existing = await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.account_id == payload.account_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(status_code=409, detail="Account is already a workspace member")

        member = WorkspaceMember(
            workspace_id=workspace_id,
            account_id=payload.account_id,
            role=payload.role,
            invited_by=account_id,
        )
        db.add(member)
        await db.commit()
        await db.refresh(member)

    return _member_to_response(member)


@router.patch("/{workspace_id}/members/{member_account_id}", summary="Update member role")
async def update_member_role(
    workspace_id: str,
    member_account_id: str,
    payload: WorkspaceMemberUpdate,
    request: Request,
):
    """Update a member's role. Requires admin access."""
    account_id = _account_id(request)

    if payload.role not in ("admin", "member", "viewer"):
        raise HTTPException(status_code=422, detail="role must be 'admin', 'member', or 'viewer'")

    from app.db import AsyncSessionLocal
    from app.models.account import WorkspaceMember
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        await _get_workspace_with_access(workspace_id, account_id, db, require_role="admin")

        result = await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.account_id == member_account_id,
            )
        )
        member = result.scalar_one_or_none()
        if member is None:
            raise HTTPException(status_code=404, detail="Member not found")

        member.role = payload.role
        await db.commit()
        await db.refresh(member)

    return _member_to_response(member)


@router.delete("/{workspace_id}/members/{member_account_id}", status_code=204, summary="Remove member")
async def remove_workspace_member(
    workspace_id: str,
    member_account_id: str,
    request: Request,
):
    """Remove a member from the workspace. Requires admin access or self-removal."""
    account_id = _account_id(request)

    from app.db import AsyncSessionLocal
    from app.models.account import WorkspaceMember, Workspace
    from sqlalchemy import select, delete

    async with AsyncSessionLocal() as db:
        ws, role = await _get_workspace_with_access(workspace_id, account_id, db, require_role="viewer")

        # Allow self-removal; otherwise require admin
        if member_account_id != account_id and role not in ("admin",) and account_id != SUPERADMIN_ACCOUNT_ID:
            raise HTTPException(status_code=403, detail="Removing other members requires admin role")

        # Cannot remove the workspace owner
        if member_account_id == ws.owner_account_id:
            raise HTTPException(status_code=409, detail="Cannot remove the workspace owner")

        await db.execute(
            delete(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.account_id == member_account_id,
            )
        )
        await db.commit()
