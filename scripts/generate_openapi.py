#!/usr/bin/env python3
"""Regenerate the committed OpenAPI snapshots from the live FastAPI app.

Usage (from repo root):
    python scripts/generate_openapi.py            # writes api/openapi.json
    python scripts/generate_openapi.py --check    # exit 1 if snapshot is stale
    python scripts/generate_openapi.py --admin    # also write api/openapi.admin.json

The committed `api/openapi.json` is the public schema (no admin / analytics
routes, no `ai_usage` cost fields). It exists so SDK consumers can generate
clients without standing up the backend, and so CI can diff against the live
schema and catch accidental breaking changes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
PUBLIC_SNAPSHOT = REPO_ROOT / "api" / "openapi.json"
ADMIN_SNAPSHOT = REPO_ROOT / "api" / "openapi.admin.json"


def _load_app():
    sys.path.insert(0, str(BACKEND))
    from app.main import app  # noqa: WPS433 — deferred so sys.path is set
    return app


def _public_schema(app) -> dict:
    return app.openapi()


def _admin_schema(app) -> dict:
    from fastapi.openapi.utils import get_openapi
    from app.main import _apply_global_extras  # noqa: WPS433

    schema = get_openapi(
        title="JustHereToListen.io Admin API",
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    _apply_global_extras(schema, admin=True)
    return schema


def _serialize(schema: dict) -> str:
    return json.dumps(schema, indent=2, sort_keys=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="Exit non-zero if the committed snapshot is stale.")
    parser.add_argument("--admin", action="store_true",
                        help="Also write the full admin schema snapshot.")
    args = parser.parse_args()

    app = _load_app()

    targets = [(PUBLIC_SNAPSHOT, _public_schema(app))]
    if args.admin:
        targets.append((ADMIN_SNAPSHOT, _admin_schema(app)))

    drift = False
    for path, schema in targets:
        new = _serialize(schema)
        old = path.read_text() if path.exists() else ""
        if new == old:
            print(f"  unchanged: {path.relative_to(REPO_ROOT)}")
            continue
        if args.check:
            print(f"  STALE:     {path.relative_to(REPO_ROOT)} (run scripts/generate_openapi.py)")
            drift = True
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new)
        print(f"  wrote:     {path.relative_to(REPO_ROOT)} ({len(new)} bytes)")

    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
