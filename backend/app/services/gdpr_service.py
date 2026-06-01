"""GDPR data-erasure helper.

Several per-account tables store ``account_id`` as a bare string with no foreign
key, so deleting an ``Account`` row does not cascade to them — they'd be
orphaned (the audit found ``meeting_summaries`` and ``retention_policies``
surviving deletion). Rather than hand-maintain a purge list that drifts as new
tables are added, :func:`purge_account_owned_rows` discovers every mapped table
with an ``account_id`` column reflectively and deletes the matching rows.

``AuditLog`` is intentionally retained for regulatory traceability — the
user-identifying account row is removed by the caller, but the action record
("account.deleted") stays. ``Account`` itself is left to the caller so the whole
erasure remains one transaction.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete as _sa_delete

logger = logging.getLogger(__name__)

# Tables deliberately NOT purged here.
#   - audit_logs: retained for regulatory traceability (account_id FK is nullable
#     so the rows survive the account row's deletion).
#   - accounts: deleted by the caller to keep the erasure a single transaction.
_RETAINED_TABLES = frozenset({"audit_logs", "accounts"})


def account_owned_models() -> list:
    """All mapped ORM classes with an ``account_id`` column, minus retained ones.

    Discovered reflectively so a newly-added per-account table is covered without
    editing this list.
    """
    from app.models import account as _m
    import inspect as _pyinspect

    models = []
    for name in dir(_m):
        obj = getattr(_m, name)
        if not (_pyinspect.isclass(obj) and hasattr(obj, "__table__")):
            continue
        if obj.__tablename__ in _RETAINED_TABLES:
            continue
        if "account_id" in obj.__table__.columns:
            models.append(obj)
    return models


async def purge_account_owned_rows(account_id: str, db) -> dict[str, int]:
    """Delete every row owned by ``account_id`` across per-account tables.

    Best-effort per table: a failure on one table is logged and skipped so a
    single problem doesn't abort the whole erasure. Returns a per-table count of
    deleted rows. Does NOT delete the Account row or commit — the caller owns
    transaction boundaries.
    """
    counts: dict[str, int] = {}
    for model in account_owned_models():
        try:
            result = await db.execute(
                _sa_delete(model).where(model.account_id == account_id)
            )
            counts[model.__tablename__] = result.rowcount or 0
        except Exception:
            logger.exception("GDPR purge failed for table %s", model.__tablename__)
    logger.info("GDPR purge for account %s removed rows: %s", account_id, counts)
    return counts
