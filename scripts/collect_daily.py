#!/usr/bin/env python3
"""Collect daily Civitai model-version trend data."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from civitrends import api, collector, storage, timeutils


API_BASE = api.API_BASE
USER_AGENT = api.USER_AGENT
DEFAULT_MODEL_TYPES = collector.REQUIRED_MODEL_TYPES
FetchResult = api.FetchResult

parse_timezone_offset = timeutils.parse_timezone_offset
utc_now = timeutils.utc_now
coverage_bounds = timeutils.coverage_bounds
connect_db = storage.connect_db
init_db = storage.init_db
ensure_column = storage.ensure_column
ensure_parent = storage.ensure_parent
append_error_csv = storage.append_error_csv
record_error = storage.record_error
build_url = api.build_url
normalize_next_page = api.normalize_next_page
raw_json = storage.raw_json
earliest_base_model = storage.earliest_base_model
upsert_model = storage.upsert_model
upsert_version = storage.upsert_version
export_daily_counts = storage.export_daily_counts
write_run_start = collector.write_run_start
write_run_finish = collector.write_run_finish
count_run_errors = collector.count_run_errors


def fetch_json(url: str, retries: int, delay_seconds: float) -> FetchResult:
    """Patchable compatibility facade for the API transport."""
    return api.fetch_json(url, retries, delay_seconds)


def collect_model_type(*args: Any, **kwargs: Any) -> collector.CollectionResult:
    """Collect one model type while preserving patchable module-level transport."""
    kwargs.setdefault("fetcher", lambda url, retries, delay: fetch_json(url, retries, delay))
    return collector.collect_model_type(*args, **kwargs)


def fetch_active_total(model_type: str, base_model: str, extra_params: dict[str, str],
                       retries: int, retry_delay: float) -> tuple[int | None, str]:
    """Legacy explicit snapshot fetcher; aggregation never calls this function."""
    params: dict[str, Any] = {"limit": 1, "types": model_type}
    if base_model:
        params["baseModels"] = base_model
    params.update(extra_params)
    result = fetch_json(build_url(API_BASE, params), retries, retry_delay)
    total = result.data["metadata"].get("totalItems")
    return (int(total) if total is not None else None, result.url)


def collect_active_total_snapshots(conn, errors_csv: Path, run_id: str,
                                   observed_at: str, observed_date: str,
                                   extra_params: dict[str, str], retries: int,
                                   retry_delay: float, request_delay: float) -> int:
    return collector.collect_active_total_snapshots(
        conn, errors_csv, run_id, observed_at, observed_date, extra_params,
        retries, retry_delay, request_delay, fetch_active_total,
    )


def rebuild_daily_counts(conn, errors_csv: Path, observed_at: str,
                         observed_date: str | None, include_active_totals: bool,
                         retries: int, retry_delay: float, request_delay: float,
                         extra_params: dict[str, str]) -> None:
    """Rebuild first-observation counts without performing HTTP requests."""
    del errors_csv, include_active_totals, retries, retry_delay, request_delay, extra_params
    storage.rebuild_daily_counts(conn, observed_at, observed_date)


def parse_extra_params(values: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"extra params must use key=value form: {value}")
        key, val = value.split("=", 1)
        if not key:
            raise ValueError("extra parameter key must not be empty")
        if key in {"limit", "types", "sort", "cursor"}:
            raise ValueError(f"extra parameter uses reserved query key: {key}")
        params[key] = val
    return params


def validate_distinct_paths(db: Path, daily_csv: Path, errors_csv: Path) -> tuple[Path, Path, Path]:
    paths = storage.distinct_paths({"database": db, "daily CSV": daily_csv, "errors CSV": errors_csv})
    return paths["database"], paths["daily CSV"], paths["errors CSV"]


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _api_limit(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 100:
        raise argparse.ArgumentTypeError("must be at most 100")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("must be finite")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/civitai.sqlite", type=Path)
    parser.add_argument("--daily-csv", default="data/daily_counts.csv", type=Path)
    parser.add_argument("--errors-csv", default="data/errors.csv", type=Path)
    parser.add_argument("--model-types", nargs="+", default=list(DEFAULT_MODEL_TYPES))
    parser.add_argument("--limit", type=_api_limit, default=100)
    parser.add_argument("--max-pages", type=_positive_int,
                        default=int(os.getenv("CIVITAI_MAX_PAGES", "20")))
    parser.add_argument("--known-version-stop", type=_non_negative_int, default=200)
    parser.add_argument("--request-delay", type=_non_negative_float, default=0.25)
    parser.add_argument("--retries", type=_non_negative_int, default=3)
    parser.add_argument("--retry-delay", type=_non_negative_float, default=5.0)
    parser.add_argument("--timezone-offset", default="+09:00")
    parser.add_argument("--observed-date", help="Override observed date, YYYY-MM-DD")
    parser.add_argument("--coverage-start-date")
    parser.add_argument("--coverage-end-date")
    parser.add_argument("--extra-param", action="append", default=[])
    parser.add_argument("--skip-active-totals", action="store_true")
    parser.add_argument("--fail-on-errors", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.db, args.daily_csv, args.errors_csv = validate_distinct_paths(
            args.db, args.daily_csv, args.errors_csv
        )
    except ValueError as exc:
        parser.error(str(exc))
    tz = parse_timezone_offset(args.timezone_offset)
    now = utc_now()
    observed_date = args.observed_date or now.astimezone(tz).date().isoformat()
    date.fromisoformat(observed_date)
    coverage_start_date = args.coverage_start_date or observed_date
    coverage_end_date = args.coverage_end_date or observed_date
    coverage_started_at, coverage_finished_at = coverage_bounds(
        coverage_start_date, coverage_end_date, tz
    )
    observed_at = now.isoformat()
    extra_params = parse_extra_params(args.extra_param)
    conn = connect_db(args.db)
    run_id = str(uuid.uuid4())
    results: list[collector.CollectionResult] = []
    try:
        init_db(conn)
        write_run_start(conn, run_id, observed_at, observed_date,
                        coverage_started_at, coverage_finished_at,
                        args.known_version_stop)
        for model_type in args.model_types:
            results.append(collect_model_type(
                conn=conn, errors_csv=args.errors_csv, model_type=model_type,
                observed_at=observed_at, observed_date=observed_date,
                limit=args.limit, max_pages=args.max_pages,
                known_version_stop=args.known_version_stop,
                request_delay=args.request_delay, retries=args.retries,
                retry_delay=args.retry_delay, extra_params=extra_params,
                run_id=run_id, requested_started_at=coverage_started_at,
                requested_finished_at=coverage_finished_at,
            ))
        if not args.skip_active_totals:
            collect_active_total_snapshots(
                conn, args.errors_csv, run_id, observed_at, observed_date,
                extra_params, args.retries, args.retry_delay, args.request_delay,
            )
        storage.rebuild_daily_counts(conn, observed_at, None)
        export_daily_counts(conn, args.daily_csv)
        execution_status = collector.finish_batch(conn, run_id)
        error_count = count_run_errors(conn, run_id)
        if args.fail_on_errors and execution_status != "completed":
            print(
                f"collector recorded {error_count} error(s) or missed a required type; "
                "failing because --fail-on-errors is set", file=sys.stderr,
            )
            return 2
    except Exception as exc:
        conn.rollback()
        try:
            record_error(conn, args.errors_csv, observed_at, observed_date,
                         "collector", repr(exc), run_id=run_id)
            collector.finish_batch(conn, run_id, repr(exc))
        except Exception:
            conn.rollback()
        raise
    finally:
        conn.close()

    print(json.dumps({
        "run_id": run_id, "observed_date": observed_date,
        "coverage_started_at": coverage_started_at,
        "coverage_finished_at": coverage_finished_at,
        "execution_status": execution_status,
        "pages_fetched": sum(result.pages_fetched for result in results),
        "models_seen": sum(result.models_seen for result in results),
        "versions_seen": sum(result.versions_seen for result in results),
        "daily_csv": str(args.daily_csv),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
