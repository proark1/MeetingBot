"""Meeting template / playbook CRUD."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.template import MeetingTemplate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/templates", tags=["Templates"])

# Seed templates available to all users
_SEED_TEMPLATES = [
    {
        "id": "seed-sales",
        "name": "Sales Call",
        "description": "Extract buying signals, objections, and next steps",
        "prompt_override": (
            'You are a sales coach. Analyze this sales call transcript and return ONLY valid JSON.\n'
            'Required JSON shape:\n'
            '{\n'
            '  "summary": "<2–3 sentence overview>",\n'
            '  "key_points": ["<point>"],\n'
            '  "action_items": [{"task": "...", "assignee": "...", "due_date": "..."}],\n'
            '  "decisions": ["<decision>"],\n'
            '  "next_steps": ["<step>"],\n'
            '  "sentiment": "positive|neutral|negative",\n'
            '  "topics": ["<topic>"],\n'
            '  "buying_signals": ["<signal>"],\n'
            '  "objections": ["<objection>"],\n'
            '  "deal_stage": "discovery|evaluation|negotiation|closed|unknown"\n'
            '}'
        ),
    },
    {
        "id": "seed-standup",
        "name": "Daily Standup",
        "description": "Blockers, progress, and next actions",
        "prompt_override": (
            'You are a scrum master. Analyze this standup transcript and return ONLY valid JSON.\n'
            'Required JSON shape:\n'
            '{\n'
            '  "summary": "<1–2 sentence overview>",\n'
            '  "key_points": ["<point>"],\n'
            '  "action_items": [{"task": "...", "assignee": "...", "due_date": "..."}],\n'
            '  "decisions": ["<decision>"],\n'
            '  "next_steps": ["<step>"],\n'
            '  "sentiment": "positive|neutral|negative",\n'
            '  "topics": ["<topic>"],\n'
            '  "blockers": ["<blocker>"],\n'
            '  "completed_yesterday": ["<item>"],\n'
            '  "planned_today": ["<item>"]\n'
            '}'
        ),
    },
    {
        "id": "seed-1on1",
        "name": "1:1 Meeting",
        "description": "Career growth, feedback, and follow-ups",
        "prompt_override": (
            'You are an executive coach. Analyze this 1:1 meeting transcript and return ONLY valid JSON.\n'
            'Required JSON shape:\n'
            '{\n'
            '  "summary": "<2–3 sentence overview>",\n'
            '  "key_points": ["<point>"],\n'
            '  "action_items": [{"task": "...", "assignee": "...", "due_date": "..."}],\n'
            '  "decisions": ["<decision>"],\n'
            '  "next_steps": ["<step>"],\n'
            '  "sentiment": "positive|neutral|negative",\n'
            '  "topics": ["<topic>"],\n'
            '  "feedback_given": ["<feedback>"],\n'
            '  "growth_areas": ["<area>"]\n'
            '}'
        ),
    },
]


class TemplateCreate(BaseModel):
    name: str
    description: str | None = None
    prompt_override: str | None = None


@router.get("")
async def list_templates(db: Annotated[AsyncSession, Depends(get_db)]):
    """List all templates including built-in seeds."""
    custom = (await db.execute(
        select(MeetingTemplate).order_by(MeetingTemplate.created_at)
    )).scalars().all()

    return _SEED_TEMPLATES + [
        {
            "id": t.id, "name": t.name, "description": t.description,
            "prompt_override": t.prompt_override,
            "created_at": t.created_at.isoformat(),
        }
        for t in custom
    ]


@router.post("", status_code=201)
async def create_template(
    payload: TemplateCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    t = MeetingTemplate(
        name=payload.name,
        description=payload.description,
        prompt_override=payload.prompt_override,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return {"id": t.id, "name": t.name, "description": t.description, "prompt_override": t.prompt_override}


@router.delete("/{template_id}", status_code=204)
async def delete_template(
    template_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if template_id.startswith("seed-"):
        raise HTTPException(status_code=400, detail="Cannot delete built-in templates")
    result = await db.execute(select(MeetingTemplate).where(MeetingTemplate.id == template_id))
    t = result.scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Template not found")
    await db.delete(t)
    await db.commit()
