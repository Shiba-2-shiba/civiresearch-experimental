#!/usr/bin/env python3
"""Report observed baseModel values that are not covered by group config."""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

from export_counts import parse_group_file


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def read_base_model_totals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            select model_type,
                   coalesce(base_model_raw, '') as base_model_raw,
                   sum(new_version_count) as new_version_count,
                   sum(new_model_count) as new_model_count
            from daily_counts
            group by model_type, coalesce(base_model_raw, '')
            order by model_type, new_version_count desc, base_model_raw
            """
        )
    )


def audit_rows(rows: list[sqlite3.Row], group_map: dict[str, str], show_all: bool) -> list[dict[str, object]]:
    audited: list[dict[str, object]] = []
    for row in rows:
        raw = row["base_model_raw"] or ""
        group = group_map.get(raw)
        if group is None and not show_all:
            audited.append(
                {
                    "status": "ungrouped",
                    "model_type": row["model_type"],
                    "base_model_raw": raw,
                    "base_model_group": "",
                    "new_version_count": int(row["new_version_count"] or 0),
                    "new_model_count": int(row["new_model_count"] or 0),
                }
            )
        elif show_all:
            audited.append(
                {
                    "status": "grouped" if group is not None else "ungrouped",
                    "model_type": row["model_type"],
                    "base_model_raw": raw,
                    "base_model_group": group or "",
                    "new_version_count": int(row["new_version_count"] or 0),
                    "new_model_count": int(row["new_model_count"] or 0),
                }
            )
    return audited


def write_rows(path: Path | None, rows: list[dict[str, object]]) -> None:
    fieldnames = [
        "status",
        "model_type",
        "base_model_raw",
        "base_model_group",
        "new_version_count",
        "new_model_count",
    ]
    handle = path.open("w", newline="", encoding="utf-8") if path else sys.stdout
    try:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if path:
            handle.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/civitai.sqlite"))
    parser.add_argument("--groups", type=Path, default=Path("config/base_model_groups.yaml"))
    parser.add_argument("--output", type=Path, help="Write CSV report to this path instead of stdout.")
    parser.add_argument("--all", action="store_true", help="Include grouped values in the report.")
    parser.add_argument(
        "--fail-on-ungrouped",
        action="store_true",
        help="Exit non-zero when any observed baseModel is ungrouped.",
    )
    args = parser.parse_args(argv)

    group_map = parse_group_file(args.groups)
    with connect(args.db) as conn:
        rows = read_base_model_totals(conn)
    audited = audit_rows(rows, group_map, args.all)
    write_rows(args.output, audited)

    ungrouped_count = sum(1 for row in audited if row["status"] == "ungrouped")
    if args.fail_on_ungrouped and ungrouped_count:
        print(f"{ungrouped_count} ungrouped baseModel row(s) found", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
