#!/usr/bin/env python3
"""Report raw baseModel values and their grouping status."""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

from civitrends.grouping import load_group_map
from civitrends.storage import distinct_paths


def read_base_model_totals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """with raw_values as (
                   select model_type, coalesce(base_model_raw, '') as base_model_raw,
                          sum(new_version_count) as new_version_count,
                          sum(new_model_count) as new_model_count
                   from daily_counts group by model_type, coalesce(base_model_raw, '')
                   union all
                   select v.model_type, coalesce(v.base_model_raw, ''), count(*), 0
                   from model_versions v
                   where not exists (
                       select 1 from daily_counts d
                       where d.model_type=v.model_type
                         and coalesce(d.base_model_raw, '')=coalesce(v.base_model_raw, '')
                   )
                   group by v.model_type, coalesce(v.base_model_raw, '')
               )
               select model_type, base_model_raw,
                      sum(new_version_count) as new_version_count,
                      sum(new_model_count) as new_model_count
               from raw_values group by model_type, base_model_raw
               order by model_type, new_version_count desc, base_model_raw"""
        )
    )


def audit_rows(rows, group_map: dict[str, str], show_all: bool) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for row in rows:
        raw = row["base_model_raw"] or ""
        group = group_map.get(raw)
        if group is None or show_all:
            result.append(
                {
                    "status": "grouped" if group is not None else "ungrouped",
                    "model_type": row["model_type"],
                    "base_model_raw": raw,
                    "base_model_group": group or "",
                    "new_version_count": int(row["new_version_count"] or 0),
                    "new_model_count": int(row["new_model_count"] or 0),
                }
            )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/civitai.sqlite"))
    parser.add_argument("--groups", type=Path, default=Path("config/base_model_groups.yaml"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--fail-on-ungrouped", action="store_true")
    args = parser.parse_args(argv)

    paths = {"database": args.db, "groups": args.groups}
    if args.output:
        paths["output"] = args.output
    distinct_paths(paths)
    groups = load_group_map(args.groups)

    conn = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = read_base_model_totals(conn)
    finally:
        conn.close()
    audited = audit_rows(rows, groups, args.all)
    fields = [
        "status", "model_type", "base_model_raw", "base_model_group",
        "new_version_count", "new_model_count",
    ]
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
    handle = args.output.open("w", newline="", encoding="utf-8") if args.output else sys.stdout
    try:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(audited)
    finally:
        if args.output:
            handle.close()
    if args.fail_on_ungrouped and any(row["status"] == "ungrouped" for row in audited):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
