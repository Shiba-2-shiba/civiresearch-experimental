#!/usr/bin/env python3
"""Export daily, weekly, and monthly Civitai trend counts from SQLite."""

from __future__ import annotations

import argparse
import csv
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path


def parse_group_file(path: Path) -> dict[str, str]:
    """Parse the small YAML subset used by config/base_model_groups.yaml."""
    if not path.exists():
        return {}
    mapping: dict[str, str] = {}
    current_group: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not raw_line.startswith(" ") and line.endswith(":"):
            current_group = line[:-1].strip()
            continue
        if line == "-" and current_group:
            mapping[""] = current_group
            continue
        if line.startswith("- ") and current_group:
            mapping[line[2:].strip()] = current_group
    return mapping


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def rows_from_db(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            select observed_date, model_type, base_model_raw, new_version_count,
                   new_model_count, active_total, collected_at
            from daily_counts
            order by observed_date, model_type, base_model_raw
            """
        )
    )


def export_rows(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    ensure_parent(path)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def grouped_period(day: str, period: str) -> str:
    parsed = date.fromisoformat(day)
    if period == "weekly":
        iso = parsed.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if period == "monthly":
        return f"{parsed.year}-{parsed.month:02d}"
    return day


def aggregate(rows: list[sqlite3.Row], group_map: dict[str, str], period: str) -> list[dict[str, object]]:
    totals: dict[tuple[str, str, str], dict[str, object]] = {}
    for row in rows:
        key = (
            grouped_period(row["observed_date"], period),
            row["model_type"],
            group_map.get(row["base_model_raw"], row["base_model_raw"] or "Unknown"),
        )
        if key not in totals:
            totals[key] = {
                "period": key[0],
                "model_type": key[1],
                "base_model_group": key[2],
                "new_version_count": 0,
                "new_model_count": 0,
                "active_total_last": None,
            }
        totals[key]["new_version_count"] = int(totals[key]["new_version_count"]) + int(
            row["new_version_count"] or 0
        )
        totals[key]["new_model_count"] = int(totals[key]["new_model_count"]) + int(
            row["new_model_count"] or 0
        )
        if row["active_total"] is not None:
            totals[key]["active_total_last"] = row["active_total"]
    return [totals[key] for key in sorted(totals)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/civitai.sqlite"))
    parser.add_argument("--daily-csv", type=Path, default=Path("data/daily_counts.csv"))
    parser.add_argument("--weekly-csv", type=Path, default=Path("data/exports/weekly_counts.csv"))
    parser.add_argument("--monthly-csv", type=Path, default=Path("data/exports/monthly_counts.csv"))
    parser.add_argument("--groups", type=Path, default=Path("config/base_model_groups.yaml"))
    args = parser.parse_args()

    group_map = parse_group_file(args.groups)
    with connect(args.db) as conn:
        rows = rows_from_db(conn)

    daily_fields = [
        "observed_date",
        "model_type",
        "base_model_raw",
        "new_version_count",
        "new_model_count",
        "active_total",
        "collected_at",
    ]
    export_rows(args.daily_csv, [{field: row[field] for field in daily_fields} for row in rows], daily_fields)

    aggregate_fields = [
        "period",
        "model_type",
        "base_model_group",
        "new_version_count",
        "new_model_count",
        "active_total_last",
    ]
    export_rows(args.weekly_csv, aggregate(rows, group_map, "weekly"), aggregate_fields)
    export_rows(args.monthly_csv, aggregate(rows, group_map, "monthly"), aggregate_fields)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
