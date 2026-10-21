#!/usr/bin/env python3
"""Export daily, weekly, and monthly Civitai trend counts from SQLite."""

from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path

from civitrends.grouping import load_group_map
from civitrends.metrics import aggregate_observations, period_for
from civitrends.storage import distinct_paths


def parse_group_file(path: Path) -> dict[str, str]:
    return load_group_map(path)


def grouped_period(day: str, period: str) -> str:
    normalized = {"weekly": "weekly", "monthly": "monthly"}.get(period, "daily")
    return period_for(day, normalized)


def aggregate(rows, group_map: dict[str, str], period: str) -> list[dict[str, object]]:
    normalized = {"weekly": "weekly", "monthly": "monthly"}.get(period, "daily")
    return aggregate_observations(rows, group_map, normalized)


def rows_from_db(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """select observed_date, model_type, base_model_raw,
                      new_version_count, new_model_count, active_total, collected_at
               from daily_counts
               order by observed_date, model_type, base_model_raw"""
        )
    )


def export_rows(path: Path, rows, fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/civitai.sqlite"))
    parser.add_argument("--daily-csv", type=Path, default=Path("data/daily_counts.csv"))
    parser.add_argument("--weekly-csv", type=Path, default=Path("data/exports/weekly_counts.csv"))
    parser.add_argument("--monthly-csv", type=Path, default=Path("data/exports/monthly_counts.csv"))
    parser.add_argument("--groups", type=Path, default=Path("config/base_model_groups.yaml"))
    args = parser.parse_args(argv)

    distinct_paths({"database": args.db, "groups": args.groups, "daily CSV": args.daily_csv,
                    "weekly CSV": args.weekly_csv, "monthly CSV": args.monthly_csv})
    groups = parse_group_file(args.groups)

    conn = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = rows_from_db(conn)
    finally:
        conn.close()

    daily_fields = [
        "observed_date", "model_type", "base_model_raw", "new_version_count",
        "new_model_count", "active_total", "collected_at",
    ]
    export_rows(
        args.daily_csv,
        ({field: row[field] for field in daily_fields} for row in rows),
        daily_fields,
    )
    aggregate_fields = [
        "period", "model_type", "base_model_group", "new_version_count",
        "new_model_count", "active_total_last",
    ]
    export_rows(args.weekly_csv, aggregate(rows, groups, "weekly"), aggregate_fields)
    export_rows(args.monthly_csv, aggregate(rows, groups, "monthly"), aggregate_fields)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
