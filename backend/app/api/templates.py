"""Meeting template / playbook CRUD."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
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
        "description": "Optimised for B2B and B2C sales conversations. Surfaces buying signals, objections raised by the prospect, deal stage, and concrete next steps so the sales rep can update the CRM immediately after the call.",
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
        "description": "Tailored for agile/scrum standups. Captures what each team member completed yesterday, what they plan today, and any blockers so the team lead has an instant snapshot without reading through the entire transcript.",
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
        "description": "Designed for manager–direct-report check-ins. Highlights feedback exchanged, career growth areas discussed, morale signals, and personal action items so both parties leave with clear commitments.",
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
    {
        "id": "seed-retro",
        "name": "Sprint Retrospective",
        "description": "Built for agile retrospectives (Start / Stop / Continue or similar formats). Extracts team mood, what went well, what didn't, process improvements agreed upon, and owners so the team can track whether retro actions are actually implemented next sprint.",
        "prompt_override": (
            'You are an agile coach. Analyze this sprint retrospective transcript and return ONLY valid JSON.\n'
            'Required JSON shape:\n'
            '{\n'
            '  "summary": "<2–3 sentence overview>",\n'
            '  "key_points": ["<point>"],\n'
            '  "action_items": [{"task": "...", "assignee": "...", "due_date": "..."}],\n'
            '  "decisions": ["<decision>"],\n'
            '  "next_steps": ["<step>"],\n'
            '  "sentiment": "positive|neutral|negative",\n'
            '  "topics": ["<topic>"],\n'
            '  "went_well": ["<item>"],\n'
            '  "went_poorly": ["<item>"],\n'
            '  "process_improvements": ["<improvement>"]\n'
            '}'
        ),
    },
    {
        "id": "seed-kickoff",
        "name": "Client Kickoff",
        "description": "Ideal for project or engagement kickoffs with external clients. Captures agreed scope, deliverables, success metrics, open risks, and owner assignments so the project manager can populate the project plan without a manual note review.",
        "prompt_override": (
            'You are a project manager. Analyze this client kickoff meeting transcript and return ONLY valid JSON.\n'
            'Required JSON shape:\n'
            '{\n'
            '  "summary": "<2–3 sentence overview>",\n'
            '  "key_points": ["<point>"],\n'
            '  "action_items": [{"task": "...", "assignee": "...", "due_date": "..."}],\n'
            '  "decisions": ["<decision>"],\n'
            '  "next_steps": ["<step>"],\n'
            '  "sentiment": "positive|neutral|negative",\n'
            '  "topics": ["<topic>"],\n'
            '  "scope_items": ["<scope>"],\n'
            '  "deliverables": ["<deliverable>"],\n'
            '  "risks": ["<risk>"],\n'
            '  "success_metrics": ["<metric>"]\n'
            '}'
        ),
    },
    {
        "id": "seed-allhands",
        "name": "All-Hands / Town Hall",
        "description": "Suited for company-wide or department-wide announcements. Pulls out strategic announcements, key metrics shared, questions raised by employees, and leadership commitments so attendees who missed the live session can catch up quickly.",
        "prompt_override": (
            'You are a communications specialist. Analyze this all-hands meeting transcript and return ONLY valid JSON.\n'
            'Required JSON shape:\n'
            '{\n'
            '  "summary": "<2–3 sentence overview>",\n'
            '  "key_points": ["<point>"],\n'
            '  "action_items": [{"task": "...", "assignee": "...", "due_date": "..."}],\n'
            '  "decisions": ["<decision>"],\n'
            '  "next_steps": ["<step>"],\n'
            '  "sentiment": "positive|neutral|negative",\n'
            '  "topics": ["<topic>"],\n'
            '  "announcements": ["<announcement>"],\n'
            '  "metrics_shared": ["<metric>"],\n'
            '  "employee_questions": ["<question>"],\n'
            '  "leadership_commitments": ["<commitment>"]\n'
            '}'
        ),
    },
    {
        "id": "seed-postmortem",
        "name": "Incident Post-Mortem",
        "description": "Structured for engineering or operational post-incident reviews. Reconstructs the incident timeline, identifies root causes, quantifies customer impact, and tracks remediation items with owners so the team can close gaps before the next incident.",
        "prompt_override": (
            'You are a site reliability engineer. Analyze this incident post-mortem meeting transcript and return ONLY valid JSON.\n'
            'Required JSON shape:\n'
            '{\n'
            '  "summary": "<2–3 sentence overview>",\n'
            '  "key_points": ["<point>"],\n'
            '  "action_items": [{"task": "...", "assignee": "...", "due_date": "..."}],\n'
            '  "decisions": ["<decision>"],\n'
            '  "next_steps": ["<step>"],\n'
            '  "sentiment": "positive|neutral|negative",\n'
            '  "topics": ["<topic>"],\n'
            '  "timeline": ["<event>"],\n'
            '  "root_causes": ["<cause>"],\n'
            '  "customer_impact": "<description>",\n'
            '  "remediation_items": [{"item": "...", "owner": "...", "priority": "high|medium|low"}]\n'
            '}'
        ),
    },
    {
        "id": "seed-interview",
        "name": "Interview / Hiring Panel",
        "description": "Crafted for structured job interviews or panel debriefs. Evaluates candidate strengths and concerns per competency, hiring recommendation, and suggested follow-up questions — enabling a faster, bias-aware debrief.",
        "prompt_override": (
            'You are a talent acquisition specialist. Analyze this interview transcript and return ONLY valid JSON.\n'
            'Required JSON shape:\n'
            '{\n'
            '  "summary": "<2–3 sentence overview>",\n'
            '  "key_points": ["<point>"],\n'
            '  "action_items": [{"task": "...", "assignee": "...", "due_date": "..."}],\n'
            '  "decisions": ["<decision>"],\n'
            '  "next_steps": ["<step>"],\n'
            '  "sentiment": "positive|neutral|negative",\n'
            '  "topics": ["<topic>"],\n'
            '  "strengths": ["<strength>"],\n'
            '  "concerns": ["<concern>"],\n'
            '  "competency_ratings": [{"competency": "...", "rating": "strong|acceptable|weak", "evidence": "..."}],\n'
            '  "recommendation": "strong_yes|yes|no|strong_no|undecided"\n'
            '}'
        ),
    },
    {
        "id": "seed-design-review",
        "name": "Design Review",
        "description": "Focused on product or technical design discussions. Records design decisions, alternatives that were considered and rejected, open design questions, usability concerns raised, and follow-up experiments or spikes assigned.",
        "prompt_override": (
            'You are a product designer. Analyze this design review meeting transcript and return ONLY valid JSON.\n'
            'Required JSON shape:\n'
            '{\n'
            '  "summary": "<2–3 sentence overview>",\n'
            '  "key_points": ["<point>"],\n'
            '  "action_items": [{"task": "...", "assignee": "...", "due_date": "..."}],\n'
            '  "decisions": ["<decision>"],\n'
            '  "next_steps": ["<step>"],\n'
            '  "sentiment": "positive|neutral|negative",\n'
            '  "topics": ["<topic>"],\n'
            '  "design_decisions": ["<decision>"],\n'
            '  "alternatives_rejected": [{"option": "...", "reason": "..."}],\n'
            '  "open_questions": ["<question>"],\n'
            '  "usability_concerns": ["<concern>"]\n'
            '}'
        ),
    },
]


class TemplateCreate(BaseModel):
    name: str = Field(..., description="Display name for the template")
    description: str | None = Field(None, description="Human-readable explanation of what meeting type and use-case this template is designed for")
    prompt_override: str | None = Field(None, description="Custom AI analysis prompt. Must instruct the model to return valid JSON. If omitted the default analysis prompt is used.")


class TemplateOut(BaseModel):
    id: str = Field(..., description="Unique template identifier. Built-in templates have ids prefixed with 'seed-'.")
    name: str = Field(..., description="Display name")
    description: str | None = Field(None, description="Full description of the template's purpose and the meeting type it is optimised for")
    prompt_override: str | None = Field(None, description="Custom AI prompt sent to the analysis model. Returns the full prompt text — no truncation.")
    created_at: str | None = Field(None, description="ISO-8601 creation timestamp. Null for built-in seed templates.")


@router.get("", response_model=list[TemplateOut], summary="List all templates")
async def list_templates(db: Annotated[AsyncSession, Depends(get_db)]):
    """Return all meeting templates: built-in seed templates followed by user-created custom templates.

    Built-in templates (`id` starts with `seed-`) cover common meeting types out of the box and
    cannot be deleted. Custom templates can be created via `POST /templates` and deleted via
    `DELETE /templates/{template_id}`.

    Every template exposes its full `description` and full `prompt_override` text — nothing is
    truncated. Clients should render the description to help users choose the right template, and
    may display the prompt in an expandable or scrollable element.
    """
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


@router.post("", response_model=TemplateOut, status_code=201, summary="Create a custom template")
async def create_template(
    payload: TemplateCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a new custom meeting template.

    Supply a `name` and optionally a `description` and a `prompt_override`.
    If `prompt_override` is omitted the bot will use the default analysis prompt when this
    template is selected.
    """
    t = MeetingTemplate(
        name=payload.name,
        description=payload.description,
        prompt_override=payload.prompt_override,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return {
        "id": t.id, "name": t.name, "description": t.description,
        "prompt_override": t.prompt_override,
        "created_at": t.created_at.isoformat(),
    }


@router.delete("/{template_id}", status_code=204, summary="Delete a custom template")
async def delete_template(
    template_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete a user-created template by ID. Built-in seed templates (id prefix `seed-`) cannot be deleted."""
    if template_id.startswith("seed-"):
        raise HTTPException(status_code=400, detail="Cannot delete built-in templates")
    result = await db.execute(select(MeetingTemplate).where(MeetingTemplate.id == template_id))
    t = result.scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Template not found")
    await db.delete(t)
    await db.commit()
