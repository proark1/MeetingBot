"""Async SQLAlchemy database setup."""

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

def _engine_kwargs() -> dict:
    """Return extra kwargs for create_async_engine based on the configured DB."""
    url = settings.async_database_url
    if "postgresql" in url:
        connect_args: dict = {"timeout": 10}
        if "ssl=require" in url or "sslmode=require" in url:
            connect_args["ssl"] = True
        return {
            "connect_args": connect_args,
            # Connection pool tuning for production PostgreSQL.
            # pool_pre_ping: execute a lightweight "SELECT 1" before reusing a connection to
            #   detect stale/dropped connections (crucial when DB restarts or idles out).
            # pool_recycle: recycle connections older than 30 min to prevent silent drops
            #   from cloud-proxy or network idle timeouts (common on Railway/Fly/RDS).
            # pool_size / max_overflow: allow bursting to 20 concurrent DB connections under load.
            "pool_pre_ping": True,
            "pool_recycle": settings.DB_POOL_RECYCLE_SECONDS,
            "pool_size": settings.DB_POOL_SIZE,
            "max_overflow": settings.DB_POOL_MAX_OVERFLOW,
            "pool_timeout": settings.DB_POOL_TIMEOUT,
        }
    # SQLite (dev) — no pooling config needed
    return {}


engine = create_async_engine(settings.async_database_url, echo=False, **_engine_kwargs())
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def get_db():
    """FastAPI dependency: yield an async session, rolling back on errors.

    Without an explicit rollback, an exception raised by a downstream route
    after the session ran statements would leave the session in a half-open
    transactional state on close, occasionally returning corrupt connections
    to the pool.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def create_all_tables() -> None:
    from app.models import account  # noqa: F401 — registers models with Base.metadata
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_schema)


def _migrate_schema(conn) -> None:
    """Apply additive schema migrations for columns added after initial deployment.

    PostgreSQL path: uses ALTER TABLE ... ADD COLUMN IF NOT EXISTS — no inspector queries needed.
    SQLite path: falls back to inspector-based checks (SQLite lacks IF NOT EXISTS support).
    """
    import logging
    from sqlalchemy import inspect, text

    _log = logging.getLogger(__name__)

    # Detect backend to use the correct datetime type
    _is_pg = "postgresql" in str(conn.engine.url)
    _dt_type = "TIMESTAMP WITH TIME ZONE" if _is_pg else "DATETIME"
    _bool_false = "FALSE" if _is_pg else "0"
    _bool_true = "TRUE" if _is_pg else "1"

    if _is_pg:
        # PostgreSQL: use ADD COLUMN IF NOT EXISTS — zero inspector overhead, idempotent
        _pg_migrations = [
            f"ALTER TABLE accounts ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE",
            f"ALTER TABLE accounts ADD COLUMN IF NOT EXISTS account_type VARCHAR(20) NOT NULL DEFAULT 'personal'",
            f"ALTER TABLE accounts ADD COLUMN IF NOT EXISTS wallet_address VARCHAR(42)",
            f"ALTER TABLE accounts ADD COLUMN IF NOT EXISTS plan VARCHAR(20) NOT NULL DEFAULT 'free'",
            f"ALTER TABLE accounts ADD COLUMN IF NOT EXISTS monthly_bots_used INTEGER NOT NULL DEFAULT 0",
            f"ALTER TABLE accounts ADD COLUMN IF NOT EXISTS monthly_reset_at TIMESTAMP WITH TIME ZONE",
            f"ALTER TABLE accounts ADD COLUMN IF NOT EXISTS notify_on_done BOOLEAN NOT NULL DEFAULT TRUE",
            f"ALTER TABLE accounts ADD COLUMN IF NOT EXISTS notify_email VARCHAR(255)",
            f"ALTER TABLE accounts ADD COLUMN IF NOT EXISTS failed_login_attempts INTEGER NOT NULL DEFAULT 0",
            f"ALTER TABLE accounts ADD COLUMN IF NOT EXISTS last_failed_login_at TIMESTAMP WITH TIME ZONE",
            f"ALTER TABLE accounts ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR(255)",
            f"ALTER TABLE accounts ADD COLUMN IF NOT EXISTS stripe_subscription_id VARCHAR(255)",
            f"ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMP WITH TIME ZONE",
            f"ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS mode VARCHAR(10) NOT NULL DEFAULT 'live'",
            # round-3 fix #6 — peppered HMAC of the plaintext key + a prefix index for cheap lookup
            f"ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS key_prefix VARCHAR(16)",
            f"ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS key_hash VARCHAR(128)",
            f"ALTER TABLE bot_snapshots ADD COLUMN IF NOT EXISTS sub_user_id VARCHAR(255)",
            f"ALTER TABLE bot_snapshots ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP WITH TIME ZONE",
            f"ALTER TABLE bot_snapshots ADD COLUMN IF NOT EXISTS consent_given BOOLEAN NOT NULL DEFAULT FALSE",
            f"ALTER TABLE bot_snapshots ADD COLUMN IF NOT EXISTS opted_out_participants TEXT",
            f"ALTER TABLE bot_snapshots ADD COLUMN IF NOT EXISTS share_token_hash VARCHAR(128)",
            f"ALTER TABLE bot_snapshots ADD COLUMN IF NOT EXISTS share_token_expires_at TIMESTAMP WITH TIME ZONE",
            # Widen support_keys.key_hash to fit the new HMAC "h2:" prefix
            f"ALTER TABLE support_keys ALTER COLUMN key_hash TYPE VARCHAR(128)",
            f"ALTER TABLE webhooks ADD COLUMN IF NOT EXISTS account_id VARCHAR(36)",
            f"ALTER TABLE webhooks ADD COLUMN IF NOT EXISTS delivery_attempts INTEGER NOT NULL DEFAULT 0",
            f"ALTER TABLE webhooks ADD COLUMN IF NOT EXISTS last_delivery_at TIMESTAMP WITH TIME ZONE",
            f"ALTER TABLE webhooks ADD COLUMN IF NOT EXISTS last_delivery_status INTEGER",
            f"ALTER TABLE webhooks ADD COLUMN IF NOT EXISTS consecutive_failures INTEGER NOT NULL DEFAULT 0",
            # action_items columns added after initial deployment
            f"ALTER TABLE action_items ADD COLUMN IF NOT EXISTS sub_user_id VARCHAR(255)",
        ]
        for sql in _pg_migrations:
            try:
                conn.execute(text(sql))
            except Exception as e:
                _log.debug("PG migration skipped (%s): %s", sql.split("ADD COLUMN")[1][:40].strip(), e)
        # Indexes (IF NOT EXISTS works on both PG and SQLite)
        _pg_indexes = [
            "CREATE INDEX IF NOT EXISTS ix_bot_snapshots_account_created ON bot_snapshots (account_id, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_bot_snapshots_account_sub_user ON bot_snapshots (account_id, sub_user_id)",
            "CREATE INDEX IF NOT EXISTS ix_bot_snapshots_share_token_hash ON bot_snapshots (share_token_hash)",
            "CREATE INDEX IF NOT EXISTS ix_webhook_deliveries_retry ON webhook_deliveries (status, next_retry_at)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_idempotency_account_key ON idempotency_keys (account_id, key)",
            # round-3 fix #4 — partial unique on (type, reference_id) so the USDC
            # monitor + admin rescan can never double-credit the same on-chain tx
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_credit_tx_unique_ref ON credit_transactions (type, reference_id) WHERE reference_id IS NOT NULL",
        ]
        existing_tables = set(inspect(conn).get_table_names())
        for idx_sql in _pg_indexes:
            _tbl = idx_sql.split(" ON ")[1].split(" ")[0]
            if _tbl in existing_tables:
                try:
                    conn.execute(text(idx_sql))
                except Exception as _idx_exc:
                    _log.debug("Index migration skipped (already exists): %s", _idx_exc)
        return

    # ── SQLite fallback (dev/test only) — inspector-based checks ──────────────
    inspector = inspect(conn)

    # accounts table — columns added in v2.x
    if "accounts" in inspector.get_table_names():
        existing = {col["name"] for col in inspector.get_columns("accounts")}
        for col_name, col_def in [
            ("is_admin",      f"BOOLEAN NOT NULL DEFAULT {_bool_false}"),
            ("account_type",  "VARCHAR(20) NOT NULL DEFAULT 'personal'"),
            ("wallet_address", "VARCHAR(42)"),
        ]:
            if col_name not in existing:
                _log.info("Adding column accounts.%s", col_name)
                conn.execute(text(f"ALTER TABLE accounts ADD COLUMN {col_name} {col_def}"))

    # api_keys table — last_used_at added in v2.x; mode added in v5.x (test key support)
    if "api_keys" in inspector.get_table_names():
        existing = {col["name"] for col in inspector.get_columns("api_keys")}
        if "last_used_at" not in existing:
            _log.info("Adding column api_keys.last_used_at")
            conn.execute(text(f"ALTER TABLE api_keys ADD COLUMN last_used_at {_dt_type}"))
        if "mode" not in existing:
            _log.info("Adding column api_keys.mode")
            conn.execute(text("ALTER TABLE api_keys ADD COLUMN mode VARCHAR(10) NOT NULL DEFAULT 'live'"))
        # round-3 fix #6: peppered HMAC + prefix lookup for plaintext-storage retirement
        if "key_prefix" not in existing:
            _log.info("Adding column api_keys.key_prefix")
            conn.execute(text("ALTER TABLE api_keys ADD COLUMN key_prefix VARCHAR(16)"))
        if "key_hash" not in existing:
            _log.info("Adding column api_keys.key_hash")
            conn.execute(text("ALTER TABLE api_keys ADD COLUMN key_hash VARCHAR(128)"))

    # bot_snapshots — sub_user_id added in v2.1; expires_at added in v3.x
    if "bot_snapshots" in inspector.get_table_names():
        existing = {col["name"] for col in inspector.get_columns("bot_snapshots")}
        if "sub_user_id" not in existing:
            _log.info("Adding column bot_snapshots.sub_user_id")
            conn.execute(text("ALTER TABLE bot_snapshots ADD COLUMN sub_user_id VARCHAR(255)"))
        if "expires_at" not in existing:
            _log.info("Adding column bot_snapshots.expires_at")
            conn.execute(text(f"ALTER TABLE bot_snapshots ADD COLUMN expires_at {_dt_type}"))

    # accounts table — columns added in v3.x (plans, notifications, usage counters)
    if "accounts" in inspector.get_table_names():
        existing = {col["name"] for col in inspector.get_columns("accounts")}
        v3_migrations = [
            ("plan",                     "VARCHAR(20) NOT NULL DEFAULT 'free'"),
            ("monthly_bots_used",        "INTEGER NOT NULL DEFAULT 0"),
            ("monthly_reset_at",         f"{_dt_type}"),
            ("notify_on_done",           f"BOOLEAN NOT NULL DEFAULT {_bool_true}"),
            ("notify_email",             "VARCHAR(255)"),
            # v7.x: brute-force lockout tracking
            ("failed_login_attempts",    "INTEGER NOT NULL DEFAULT 0"),
            ("last_failed_login_at",     f"{_dt_type}"),
            # v11.x: Stripe subscription fields
            ("stripe_customer_id",       "VARCHAR(255)"),
            ("stripe_subscription_id",   "VARCHAR(255)"),
        ]
        for col_name, col_def in v3_migrations:
            if col_name not in existing:
                _log.info("Adding column accounts.%s", col_name)
                conn.execute(text(f"ALTER TABLE accounts ADD COLUMN {col_name} {col_def}"))

    # webhooks table — v4.x: delivery tracking columns + account_id for ownership scoping
    if "webhooks" in inspector.get_table_names():
        existing = {col["name"] for col in inspector.get_columns("webhooks")}
        wh_migrations = [
            ("account_id",           "VARCHAR(36)"),
            ("delivery_attempts",    "INTEGER NOT NULL DEFAULT 0"),
            ("last_delivery_at",     f"{_dt_type}"),
            ("last_delivery_status", "INTEGER"),
            ("consecutive_failures", "INTEGER NOT NULL DEFAULT 0"),
        ]
        for col_name, col_def in wh_migrations:
            if col_name not in existing:
                _log.info("Adding column webhooks.%s", col_name)
                conn.execute(text(f"ALTER TABLE webhooks ADD COLUMN {col_name} {col_def}"))

    # bot_snapshots — v5.x consent fields, v2.41 share-token columns
    if "bot_snapshots" in inspector.get_table_names():
        existing = {col["name"] for col in inspector.get_columns("bot_snapshots")}
        for col_name, col_def in [
            ("consent_given", f"BOOLEAN NOT NULL DEFAULT {_bool_false}"),
            ("opted_out_participants", "TEXT"),
            # v2.41: hash + expiry lifted from JSON blob so /share/{token} can do an indexed lookup
            ("share_token_hash", "VARCHAR(128)"),
            ("share_token_expires_at", _dt_type),
        ]:
            if col_name not in existing:
                _log.info("Adding column bot_snapshots.%s", col_name)
                conn.execute(text(f"ALTER TABLE bot_snapshots ADD COLUMN {col_name} {col_def}"))

    # v6.x: add composite performance indexes (idempotent — CREATE INDEX IF NOT EXISTS)
    _indexes = [
        ("ix_bot_snapshots_account_created",
         "CREATE INDEX IF NOT EXISTS ix_bot_snapshots_account_created ON bot_snapshots (account_id, created_at)"),
        ("ix_bot_snapshots_account_sub_user",
         "CREATE INDEX IF NOT EXISTS ix_bot_snapshots_account_sub_user ON bot_snapshots (account_id, sub_user_id)"),
        ("ix_bot_snapshots_share_token_hash",
         "CREATE INDEX IF NOT EXISTS ix_bot_snapshots_share_token_hash ON bot_snapshots (share_token_hash)"),
        ("ix_webhook_deliveries_retry",
         "CREATE INDEX IF NOT EXISTS ix_webhook_deliveries_retry ON webhook_deliveries (status, next_retry_at)"),
        ("ix_idempotency_account_key",
         "CREATE UNIQUE INDEX IF NOT EXISTS ix_idempotency_account_key ON idempotency_keys (account_id, key)"),
        # round-3 fix #4 — partial unique index for USDC tx replay protection
        ("ix_credit_tx_unique_ref",
         "CREATE UNIQUE INDEX IF NOT EXISTS ix_credit_tx_unique_ref ON credit_transactions (type, reference_id) WHERE reference_id IS NOT NULL"),
    ]
    existing_tables = set(inspector.get_table_names())
    for _idx_name, _idx_sql in _indexes:
        # SQLite supports IF NOT EXISTS on CREATE INDEX; PostgreSQL also supports it
        # Only run if the table the index references actually exists
        _tbl = _idx_sql.split(" ON ")[1].split(" ")[0]
        if _tbl in existing_tables:
            try:
                conn.execute(text(_idx_sql))
            except Exception as _idx_exc:
                _log.debug("Index migration skipped (already exists or schema mismatch): %s", _idx_exc)
