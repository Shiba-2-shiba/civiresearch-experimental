#!/usr/bin/env python3
"""Generate a standalone daily SVG trend plot from publication dates."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from civitrends.storage import distinct_paths

from civitrends.grouping import grouped_name, load_group_map
from civitrends.metrics import MetricSeries, inclusive_dates, publication_metrics
from civitrends.rendering import PALETTE, render_svg


def parse_timezone_offset(value: str) -> timezone:
    if value in ("Z", "UTC", "+00:00"):
        return timezone.utc
    sign = -1 if value.startswith("-") else 1
    raw = value[1:] if value[:1] in "+-" else value
    hours, minutes = raw.split(":", 1)
    return timezone(sign * timedelta(hours=int(hours), minutes=int(minutes)))


def local_day(value: str, tz: timezone) -> str:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(tz).date().isoformat()


def complete_date_range(days: set[str]) -> list[str]:
    return [] if not days else inclusive_dates(min(days), max(days))


def date_range(start_date: str, end_date: str) -> list[str]:
    return inclusive_dates(start_date, end_date)


def observed_date_range(db_path: Path) -> tuple[str | None, str | None]:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("select min(observed_date), max(observed_date) from daily_counts").fetchone()
        return row[0], row[1]
    finally:
        conn.close()


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"pragma table_info({table})")}


def complete_observed_dates(db_path: Path, dates: list[str], tz: timezone) -> set[str]:
    if not dates:
        return set()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if "execution_status" in table_columns(conn, "collection_runs") and table_columns(conn, "collection_run_types"):
            from civitrends.coverage import daily_coverage
            return {day for day, status in daily_coverage(conn, dates, tz).items() if status == "verified"}
        columns = table_columns(conn, "collection_runs")
        if not {"coverage_started_at", "coverage_finished_at"}.issubset(columns):
            return set()
        rows = conn.execute("select coverage_started_at, coverage_finished_at from collection_runs where status='success'").fetchall()
    finally:
        conn.close()
    complete: set[str] = set()
    for day in dates:
        start = datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
        end = datetime.combine(date.fromisoformat(day), datetime.max.time(), tzinfo=tz).astimezone(timezone.utc).replace(microsecond=0)
        if any(raw_start and raw_end and datetime.fromisoformat(raw_start) <= start and datetime.fromisoformat(raw_end) >= end for raw_start, raw_end in rows):
            complete.add(day)
    return complete


def grouped_base_model(raw_value: str | None, group_map: dict[str, str]) -> str:
    return grouped_name(raw_value, group_map)


def read_publication_series(
    db_path: Path,
    group_map: dict[str, str],
    tz: timezone,
    model_type: str | None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[list[str], MetricSeries]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        observed_start, observed_end = observed_date_range(db_path)
        start, end = start_date or observed_start, end_date or observed_end
        if not start or not end:
            columns = table_columns(conn, "model_versions")
            published_expr = "published_at" if "published_at" in columns else "null as published_at"
            publication_days = []
            for row in conn.execute(f"select {published_expr}, raw_json from model_versions"):
                value = row["published_at"]
                if not value:
                    try:
                        value = json.loads(row["raw_json"] or "{}").get("publishedAt")
                    except (TypeError, json.JSONDecodeError):
                        value = None
                if value:
                    publication_days.append(local_day(value, tz))
            if not publication_days:
                return [], {}
            start, end = min(publication_days), max(publication_days)
        metrics = publication_metrics(conn, group_map, tz, start, end, model_type, legacy_json_fallback=True)
        return metrics.dates, metrics.series
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/civitai.sqlite"))
    parser.add_argument("--groups", type=Path, default=Path("config/base_model_groups.yaml"))
    parser.add_argument("--output", type=Path, default=Path("data/exports/trends.svg"))
    parser.add_argument("--model-type", choices=["Checkpoint", "LORA"])
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--timezone-offset", default="+09:00")
    parser.add_argument("--max-series-per-panel", type=int, default=8)
    parser.add_argument("--date-label-interval-days", type=int, default=7)
    args = parser.parse_args(argv)
    distinct_paths({"database": args.db, "groups": args.groups, "SVG": args.output})
    if bool(args.start_date) != bool(args.end_date):
        parser.error("--start-date and --end-date must be supplied together")
    if args.max_series_per_panel < 0 or args.date_label_interval_days <= 0:
        parser.error("series limit must be non-negative and label interval positive")
    tz = parse_timezone_offset(args.timezone_offset)
    dates, series = read_publication_series(args.db, load_group_map(args.groups), tz, args.model_type, args.start_date, args.end_date)
    complete = complete_observed_dates(args.db, dates, tz)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_svg(dates, series, complete, args.max_series_per_panel, args.date_label_interval_days), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
