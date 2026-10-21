#!/usr/bin/env python3
"""Translate scheduled workflow inputs into validated collector arguments."""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Mapping


def workflow_arguments(env: Mapping[str, str], now: datetime) -> list[str]:
    weekly = env.get("EVENT_SCHEDULE") == "25 15 * * 0"
    observed = date.fromisoformat(env.get("OBSERVED_DATE") or now.astimezone(timezone.utc).date().isoformat())
    start = date.fromisoformat(env.get("COVERAGE_START_DATE") or
                               (observed - timedelta(days=6 if weekly else 0)).isoformat())
    end = date.fromisoformat(env.get("COVERAGE_END_DATE") or observed.isoformat())
    if start > end:
        raise ValueError("Coverage start must not be after coverage end")
    args = ["--fail-on-errors", "--timezone-offset", "+09:00", "--observed-date", observed.isoformat(),
            "--coverage-start-date", start.isoformat(), "--coverage-end-date", end.isoformat(),
            "--max-pages", env.get("MAX_PAGES") or ("80" if weekly else "20"),
            "--known-version-stop", env.get("KNOWN_VERSION_STOP") or ("1000" if weekly else "200")]
    if env.get("SKIP_ACTIVE_TOTALS", "true") == "true":
        args.append("--skip-active-totals")
    return args


def main() -> int:
    from collect_daily import main as collect
    return collect(workflow_arguments(os.environ, datetime.now(timezone.utc)))


if __name__ == "__main__":
    raise SystemExit(main())
