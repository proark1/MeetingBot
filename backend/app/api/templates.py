"""Templates API — list available built-in analysis templates.

Templates are static. Pass `template` in your bot creation request to use one.
You can also pass `prompt_override` to supply your own custom prompt.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/templates", tags=["Templates"])

_TEMPLATES = [
    {
        "name": "default",
        "label": "Default (General)",
        "description": (
            "General-purpose template for any meeting type. Produces: summary, key_points, "
            "action_items, decisions, next_steps, sentiment, and topics."
        ),
    },
    {
        "name": "sales",
        "label": "Sales Call",
        "description": "Optimised for B2B/B2C sales calls. Adds buying_signals, objections, and deal_stage.",
    },
    {
        "name": "standup",
        "label": "Daily Standup",
        "description": "For agile standups. Adds blockers, completed_yesterday, and planned_today.",
    },
    {
        "name": "1on1",
        "label": "1:1 Meeting",
        "description": "For manager–direct-report check-ins. Adds feedback_given and growth_areas.",
    },
    {
        "name": "retro",
        "label": "Sprint Retrospective",
        "description": "For agile retros. Adds went_well, went_poorly, and process_improvements.",
    },
    {
        "name": "kickoff",
        "label": "Client Kickoff",
        "description": "For project kickoffs. Adds scope_items, deliverables, risks, and success_metrics.",
    },
    {
        "name": "allhands",
        "label": "All-Hands / Town Hall",
        "description": "For company-wide meetings. Adds announcements, metrics_shared, employee_questions, and leadership_commitments.",
    },
    {
        "name": "postmortem",
        "label": "Incident Post-Mortem",
        "description": "For incident reviews. Adds timeline, root_causes, customer_impact, and remediation_items.",
    },
    {
        "name": "interview",
        "label": "Interview / Hiring Panel",
        "description": "For job interviews. Adds strengths, concerns, competency_ratings, and recommendation.",
    },
    {
        "name": "design-review",
        "label": "Design Review",
        "description": "For product/technical design discussions. Adds design_decisions, alternatives_rejected, open_questions, and usability_concerns.",
    },
]


@router.get("", tags=["Templates"])
async def list_templates():
    """List all available built-in analysis templates.

    Pass the `name` value as `template` when creating a bot:

    ```json
    POST /api/v1/bot
    {
      "meeting_url": "...",
      "template": "sales"
    }
    ```

    For a fully custom prompt, use `prompt_override` instead of `template`.
    """
    return {"templates": _TEMPLATES}


@router.get("/default-prompt", tags=["Templates"])
async def get_default_prompt():
    """Return the raw default analysis prompt used when no template or prompt_override is supplied.

    This is the exact system prompt sent to the AI model when `template` is omitted
    and `prompt_override` is not set. Useful for building custom prompts based on
    the default structure.
    """
    from app.services.intelligence_service import _ANALYSIS_PROMPT
    return {"prompt": _ANALYSIS_PROMPT}
