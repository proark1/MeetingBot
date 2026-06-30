from __future__ import annotations


def test_pg_migration_handles_drop_index_without_on_clause(monkeypatch):
    """The PostgreSQL migration loop must not parse DROP INDEX as CREATE INDEX."""
    import sqlalchemy

    from app import db

    class FakeEngine:
        url = "postgresql+asyncpg://user:pass@localhost/db"

    class FakeConn:
        engine = FakeEngine()

        def __init__(self) -> None:
            self.statements: list[str] = []

        def execute(self, statement) -> None:
            self.statements.append(str(statement))

    class FakeInspector:
        def get_table_names(self) -> list[str]:
            return [
                "accounts",
                "api_keys",
                "bot_snapshots",
                "webhooks",
                "webhook_deliveries",
                "idempotency_keys",
                "credit_transactions",
                "meeting_summaries",
                "action_items",
            ]

    monkeypatch.setattr(sqlalchemy, "inspect", lambda _conn: FakeInspector())

    conn = FakeConn()
    db._migrate_schema(conn)

    assert "DROP INDEX IF EXISTS ix_idempotency_account_key" in conn.statements
