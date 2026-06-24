"""Approval queue helpers for action-item and CRM integration delivery."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.models.account import ActionItem, ActionItemApproval, Integration
from app.services.secrets_at_rest import decrypt_json

logger = logging.getLogger(__name__)


def _bool_config(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def integration_requires_approval(config: dict) -> bool:
    return _bool_config(config.get("approval_required"))


async def queue_task_approvals(
    session,
    integration: Integration,
    config: dict,
    bot_data: dict,
) -> bool:
    """Queue Linear/Jira action-item approvals. Returns True when auto-send should be skipped."""
    if integration.type not in {"linear", "jira"} or not integration_requires_approval(config):
        return False

    bot_id = bot_data.get("bot_id") or bot_data.get("id")
    if not bot_id:
        return True
    account_id = integration.account_id
    summary = (bot_data.get("analysis") or {}).get("summary", "")

    result = await session.execute(
        select(ActionItem).where(
            ActionItem.account_id == account_id,
            ActionItem.bot_id == bot_id,
        )
    )
    items = result.scalars().all()
    if not items:
        # Analysis may exist before the DB upsert in unusual retry flows.
        for raw in (bot_data.get("analysis") or {}).get("action_items", []) or []:
            task = raw.get("task", raw) if isinstance(raw, dict) else str(raw)
            if not task:
                continue
            item = ActionItem(
                id=str(uuid.uuid4()),
                account_id=account_id,
                sub_user_id=bot_data.get("sub_user_id"),
                bot_id=bot_id,
                content_hash=str(uuid.uuid4()),
                task=str(task),
                assignee=raw.get("assignee") if isinstance(raw, dict) else None,
                due_date=raw.get("due_date") if isinstance(raw, dict) else None,
                status="open",
            )
            session.add(item)
            items.append(item)

    queued = 0
    for item in items:
        existing = await session.execute(
            select(ActionItemApproval.id).where(
                ActionItemApproval.integration_id == integration.id,
                ActionItemApproval.action_item_id == item.id,
                ActionItemApproval.destination_type == "task",
            )
        )
        if existing.scalar_one_or_none():
            continue
        payload = {
            "task": item.task,
            "assignee": item.assignee,
            "due_date": item.due_date,
            "confidence": float(item.confidence) if item.confidence is not None else None,
            "summary": summary,
            "bot_id": bot_id,
            "meeting_url": bot_data.get("meeting_url"),
        }
        session.add(ActionItemApproval(
            id=str(uuid.uuid4()),
            account_id=account_id,
            sub_user_id=item.sub_user_id,
            bot_id=bot_id,
            action_item_id=item.id,
            integration_id=integration.id,
            integration_type=integration.type,
            destination_type="task",
            payload=json.dumps(payload),
            status="pending",
        ))
        queued += 1

    if queued:
        logger.info("Queued %d %s task approval(s) for bot %s", queued, integration.type, bot_id)
    return True


async def queue_crm_approval(
    session,
    integration: Integration,
    config: dict,
    bot_data: dict,
) -> bool:
    """Queue HubSpot/Salesforce approval. Returns True when auto-send should be skipped."""
    if integration.type not in {"hubspot", "salesforce"} or not integration_requires_approval(config):
        return False

    bot_id = bot_data.get("bot_id") or bot_data.get("id")
    if not bot_id:
        return True
    destination_type = "crm_note" if integration.type == "hubspot" else "crm_task"
    existing = await session.execute(
        select(ActionItemApproval.id).where(
            ActionItemApproval.integration_id == integration.id,
            ActionItemApproval.bot_id == bot_id,
            ActionItemApproval.action_item_id.is_(None),
            ActionItemApproval.destination_type == destination_type,
        )
    )
    if existing.scalar_one_or_none():
        return True

    analysis = bot_data.get("analysis") or {}
    payload = {
        "bot_data": {
            "bot_id": bot_id,
            "meeting_url": bot_data.get("meeting_url"),
            "meeting_platform": bot_data.get("meeting_platform"),
            "duration_seconds": bot_data.get("duration_seconds"),
            "participants": bot_data.get("participants") or [],
            "analysis": {
                "summary": analysis.get("summary"),
                "action_items": analysis.get("action_items") or [],
                "decisions": analysis.get("decisions") or [],
            },
        }
    }
    session.add(ActionItemApproval(
        id=str(uuid.uuid4()),
        account_id=integration.account_id,
        sub_user_id=bot_data.get("sub_user_id"),
        bot_id=bot_id,
        action_item_id=None,
        integration_id=integration.id,
        integration_type=integration.type,
        destination_type=destination_type,
        payload=json.dumps(payload),
        status="pending",
    ))
    logger.info("Queued %s approval for bot %s", integration.type, bot_id)
    return True


async def dispatch_approval(approval: ActionItemApproval, integration: Integration, config: dict) -> bool:
    """Deliver an approved queue row to its external integration."""
    payload = json.loads(approval.payload or "{}")
    if approval.integration_type == "linear":
        from app.services.integration_service import _post_to_linear
        bot_data = {
            "bot_id": approval.bot_id,
            "meeting_url": payload.get("meeting_url"),
            "analysis": {"summary": payload.get("summary", ""), "action_items": [payload]},
        }
        return await _post_to_linear(config.get("api_key", ""), config.get("team_id", ""), bot_data)

    if approval.integration_type == "jira":
        from app.services.integration_service import _post_to_jira
        bot_data = {
            "bot_id": approval.bot_id,
            "meeting_url": payload.get("meeting_url"),
            "analysis": {"summary": payload.get("summary", ""), "action_items": [payload]},
        }
        return await _post_to_jira(
            config.get("base_url", ""),
            config.get("token", ""),
            config.get("email", ""),
            config.get("project_key", ""),
            bot_data,
        )

    if approval.integration_type == "hubspot":
        from app.services.crm_service import _post_to_hubspot
        return await _post_to_hubspot(config.get("access_token", ""), config, payload.get("bot_data") or {})

    if approval.integration_type == "salesforce":
        from app.services.crm_service import _get_salesforce_token, _post_to_salesforce
        access_token = config.get("access_token", "")
        instance_url = config.get("instance_url", "")
        if not access_token:
            instance_url, access_token = await _get_salesforce_token(config)
        return await _post_to_salesforce(instance_url, access_token, config, payload.get("bot_data") or {})

    logger.warning("Unsupported approval integration type: %s", approval.integration_type)
    return False


async def approve_and_dispatch(approval: ActionItemApproval, session, reviewer_account_id: str) -> ActionItemApproval:
    result = await session.execute(
        select(Integration).where(
            Integration.id == approval.integration_id,
            Integration.account_id == reviewer_account_id,
            Integration.is_active == True,  # noqa: E712
        )
    )
    integration = result.scalar_one_or_none()
    if integration is None:
        approval.status = "failed"
        approval.error_message = "Integration is missing or inactive"
        approval.reviewed_by = reviewer_account_id
        approval.reviewed_at = datetime.now(timezone.utc)
        return approval

    config = decrypt_json(integration.config)
    approval.status = "approved"
    approval.reviewed_by = reviewer_account_id
    approval.reviewed_at = datetime.now(timezone.utc)

    try:
        delivered = await dispatch_approval(approval, integration, config)
    except Exception as exc:
        logger.exception("Approval dispatch failed for %s", approval.id)
        delivered = False
        approval.error_message = str(exc)

    if delivered:
        approval.status = "sent"
        approval.delivered_at = datetime.now(timezone.utc)
        approval.error_message = None
    else:
        approval.status = "failed"
        approval.error_message = approval.error_message or "Integration delivery failed"
    return approval
