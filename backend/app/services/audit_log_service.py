"""Fire-and-forget audit logging service.

Writes tamper-evident, append-only entries to the `audit_logs` table.
Never raises — callers should not be affected by logging failures.
"""

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def log_event(
    account_id: Optional[str],
    action: str,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    actor_email: Optional[str] = None,
) -> None:
    """Append an audit log entry.

    Args:
        account_id:    The account performing the action.
        action:        Dot-namespaced event name, e.g. ``"bot.created"``.
        resource_type: Resource category, e.g. ``"bot"``, ``"api_key"``.
        resource_id:   ID of the affected resource.
        details:       Arbitrary JSON-serialisable context (actor email, etc.).
        ip_address:    Requester IP (IPv4 or IPv6).
        actor_email:   Email of the acting user — stored inside ``details``.
    """
    try:
        from app.db import AsyncSessionLocal
        from app.models.account import AuditLog

        # Merge actor_email into details so it's captured without a schema change.
        merged: dict[str, Any] = {}
        if actor_email:
            merged["actor_email"] = actor_email
        if details:
            merged.update(details)

        entry = AuditLog(
            account_id=account_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            ip_address=ip_address,
            details=json.dumps(merged) if merged else None,
        )

        async with AsyncSessionLocal() as db:
            db.add(entry)
            await db.commit()
    except Exception:
        logger.exception("audit_log_service: failed to write audit entry for action=%r", action)
