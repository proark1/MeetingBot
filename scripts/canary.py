#!/usr/bin/env python3
"""Run the synthetic meeting-pipeline canary against a deployed API.

The canary joins a known test meeting per platform and asserts the end-to-end
pipeline still works (admitted → audio flowing → transcript captured → clean
leave). It's the earliest warning when a platform ships a UI change that breaks
the DOM selectors in browser_bot.py.

Usage (from repo root):
    CANARY_BASE_URL=https://api.example.com \
    CANARY_API_KEY=sk_live_... \
    CANARY_MEET_URL=https://meet.google.com/your-test-room \
    python scripts/canary.py

    python scripts/canary.py --dry-run    # validate config only, no bots launched

Exit code is non-zero if any configured platform's canary fails — wire it into
a scheduler (cron / GitHub Actions schedule / Railway cron) and alert on failure.

Recognised env vars: CANARY_BASE_URL, CANARY_API_KEY, and per-platform meeting
URLs CANARY_MEET_URL / CANARY_ZOOM_URL / CANARY_TEAMS_URL / CANARY_ONEPIZZA_URL.
See app/services/canary_service.CanaryConfig.from_env for the full list.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"


def _load():
    sys.path.insert(0, str(BACKEND))
    from app.services import canary_service  # noqa: WPS433 — deferred so sys.path is set
    return canary_service


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the meeting-pipeline canary.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate configuration and exit without launching bots.")
    args = parser.parse_args()

    cs = _load()
    cfg = cs.CanaryConfig.from_env()

    if not cfg.meeting_urls:
        print("No canary meeting URLs configured (set CANARY_MEET_URL / CANARY_ZOOM_URL / …).")
        return 2
    if not args.dry_run and not cfg.base_url:
        print("CANARY_BASE_URL is required (unless --dry-run).")
        return 2

    if args.dry_run:
        print("Canary configuration OK. Would check platforms:")
        for platform, url in cfg.meeting_urls.items():
            print(f"  - {platform}: {url}")
        print(f"base_url={cfg.base_url or '(unset)'} "
              f"require_audio={cfg.require_audio} require_transcript={cfg.require_transcript}")
        return 0

    reports = asyncio.run(cs.run_all(cfg))
    failed = 0
    for r in reports:
        print(r.summary())
        for name, detail in r.details.items():
            mark = "ok" if r.checks.get(name) else "XX"
            print(f"    [{mark}] {name}: {detail}")
        failed += 0 if r.ok else 1

    print(f"\n{len(reports) - failed}/{len(reports)} canary checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
