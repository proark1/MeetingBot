"""Guard tests for the Alembic migration setup.

Verifies the baseline migration applies cleanly to a fresh SQLite database and
produces the full table set, and that there is a single migration head (no
accidentally-forked revision tree).
"""
import os
import sys
import tempfile
from pathlib import Path


_BACKEND = Path(__file__).resolve().parent.parent


def _alembic_config(db_url: str):
    from alembic.config import Config
    cfg = Config(str(_BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND / "alembic"))
    # env.py reads the URL from app settings, so point settings at our temp DB.
    os.environ["DATABASE_URL"] = db_url
    return cfg


def test_single_head():
    """Exactly one migration head — catches accidental revision forks."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(_BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1, "expected a single Alembic head"


def test_baseline_upgrade_creates_schema():
    """`alembic upgrade head` builds the full schema on a fresh DB.

    Runs the Alembic CLI in a subprocess so the app's ``settings`` singleton
    (which caches DATABASE_URL at import time) picks up the temp DB — an
    in-process ``command.upgrade`` would migrate the already-imported DB.
    """
    import subprocess
    import sqlite3

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "alembic_test.db")
        env = dict(os.environ)
        env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
        env.setdefault("JWT_SECRET", "test-secret")
        env.setdefault("CORS_ORIGINS", "*")

        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(_BACKEND), env=env, capture_output=True, text=True,
        )
        assert result.returncode == 0, f"alembic upgrade failed:\n{result.stderr}"

        con = sqlite3.connect(db_path)
        tables = {
            r[0] for r in con.execute(
                "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
            )
        }
        con.close()

    for expected in ("accounts", "api_keys", "bot_snapshots", "webhooks",
                     "action_items", "credit_transactions", "alembic_version"):
        assert expected in tables, f"{expected} missing after upgrade"
