#!/usr/bin/env python3
"""Collect daily Civitai model-version trend data.

This collector stores ID-level model and model-version records so later
analysis can regroup by baseModel, model type, day, week, or month.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, parse_qsl
from urllib.request import Request, urlopen


API_BASE = "https://civitai.com/api/v1/models"
USER_AGENT = "civiresearch-civitai-trend-collector/0.1"
DEFAULT_MODEL_TYPES = ("Checkpoint", "LORA")


@dataclass(frozen=True)
class FetchResult:
    url: str
    data: dict[str, Any]


def parse_timezone_offset(value: str) -> timezone:
    if value in ("Z", "UTC", "+00:00"):
        return timezone.utc
    sign = 1
    raw = value
    if raw.startswith("-"):
        sign = -1
        raw = raw[1:]
    elif raw.startswith("+"):
        raw = raw[1:]
    hours_raw, minutes_raw = raw.split(":", 1)
    delta = timedelta(hours=int(hours_raw), minutes=int(minutes_raw))
    return timezone(sign * delta)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def connect_db(path: Path) -> sqlite3.Connection:
    ensure_parent(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists models (
            model_id integer primary key,
            first_seen_at text not null,
            first_seen_date text not null,
            last_seen_at text not null,
            name text,
            model_type text not null,
            creator_username text,
            nsfw integer,
            mode text,
            initial_base_model_raw text,
            raw_json text
        );

        create table if not exists model_versions (
            version_id integer primary key,
            model_id integer not null,
            first_seen_at text not null,
            first_seen_date text not null,
            last_seen_at text not null,
            created_at text,
            version_name text,
            model_type text not null,
            base_model_raw text,
            raw_json text,
            foreign key(model_id) references models(model_id)
        );

        create table if not exists daily_counts (
            observed_date text not null,
            model_type text not null,
            base_model_raw text not null,
            new_version_count integer not null,
            new_model_count integer not null,
            active_total integer,
            collected_at text not null,
            primary key(observed_date, model_type, base_model_raw)
        );

        create table if not exists collection_runs (
            run_id text primary key,
            started_at text not null,
            finished_at text,
            observed_date text not null,
            status text not null,
            pages_fetched integer default 0,
            models_seen integer default 0,
            versions_seen integer default 0,
            error_message text
        );

        create table if not exists collection_errors (
            id integer primary key autoincrement,
            observed_at text not null,
            observed_date text not null,
            scope text not null,
            url text,
            http_status integer,
            message text not null
        );

        create index if not exists idx_model_versions_first_seen
            on model_versions(first_seen_date, model_type, base_model_raw);
        create index if not exists idx_models_first_seen
            on models(first_seen_date, model_type, initial_base_model_raw);
        """
    )
    ensure_column(conn, "models", "first_seen_date", "text")
    ensure_column(conn, "models", "initial_base_model_raw", "text")
    ensure_column(conn, "model_versions", "first_seen_date", "text")
    conn.commit()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"alter table {table} add column {column} {column_type}")


def append_error_csv(path: Path, row: dict[str, Any]) -> None:
    ensure_parent(path)
    fieldnames = ["observed_at", "observed_date", "scope", "url", "http_status", "message"]
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow({key: row.get(key) for key in fieldnames})


def record_error(
    conn: sqlite3.Connection,
    errors_csv: Path,
    observed_at: str,
    observed_date: str,
    scope: str,
    message: str,
    url: str | None = None,
    http_status: int | None = None,
) -> None:
    conn.execute(
        """
        insert into collection_errors (
            observed_at, observed_date, scope, url, http_status, message
        ) values (?, ?, ?, ?, ?, ?)
        """,
        (observed_at, observed_date, scope, url, http_status, message),
    )
    append_error_csv(
        errors_csv,
        {
            "observed_at": observed_at,
            "observed_date": observed_date,
            "scope": scope,
            "url": url,
            "http_status": http_status,
            "message": message,
        },
    )


def build_url(base_url: str, params: dict[str, Any]) -> str:
    clean = {key: value for key, value in params.items() if value is not None and value != ""}
    return f"{base_url}?{urlencode(clean, doseq=True)}"


def fetch_json(url: str, retries: int, delay_seconds: float) -> FetchResult:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        req = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with urlopen(req, timeout=60) as response:
                body = response.read().decode("utf-8")
                return FetchResult(url=url, data=json.loads(body))
        except HTTPError as exc:
            last_error = exc
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(delay_seconds * (attempt + 1))
                continue
            raise
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(delay_seconds * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"request failed: {last_error}")


def normalize_next_page(next_page: str | None) -> str | None:
    if not next_page:
        return None
    parsed = urlparse(next_page)
    if parsed.scheme and parsed.netloc:
        return next_page
    return None


def raw_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def earliest_base_model(model: dict[str, Any]) -> str:
    versions = model.get("modelVersions") or []
    if not isinstance(versions, list) or not versions:
        return ""
    ordered = sorted(versions, key=lambda item: item.get("createdAt") or "")
    return str(ordered[0].get("baseModel") or "")


def upsert_model(
    conn: sqlite3.Connection,
    model: dict[str, Any],
    model_type: str,
    observed_at: str,
    observed_date: str,
) -> bool:
    model_id = model.get("id")
    if model_id is None:
        return False
    existing = conn.execute("select 1 from models where model_id = ?", (model_id,)).fetchone()
    creator = model.get("creator") or {}
    initial_base_model_raw = earliest_base_model(model)
    if existing:
        conn.execute(
            """
            update models
            set last_seen_at = ?,
                name = ?,
                model_type = ?,
                creator_username = ?,
                nsfw = ?,
                mode = ?,
                initial_base_model_raw = coalesce(initial_base_model_raw, ?),
                raw_json = ?
            where model_id = ?
            """,
            (
                observed_at,
                model.get("name"),
                model.get("type") or model_type,
                creator.get("username"),
                int(bool(model.get("nsfw"))) if model.get("nsfw") is not None else None,
                model.get("mode"),
                initial_base_model_raw,
                raw_json(model),
                model_id,
            ),
        )
        return False
    conn.execute(
        """
        insert into models (
            model_id, first_seen_at, first_seen_date, last_seen_at, name, model_type,
            creator_username, nsfw, mode, initial_base_model_raw, raw_json
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            model_id,
            observed_at,
            observed_date,
            observed_at,
            model.get("name"),
            model.get("type") or model_type,
            creator.get("username"),
            int(bool(model.get("nsfw"))) if model.get("nsfw") is not None else None,
            model.get("mode"),
            initial_base_model_raw,
            raw_json(model),
        ),
    )
    return True


def upsert_version(
    conn: sqlite3.Connection,
    model_id: int,
    model_type: str,
    version: dict[str, Any],
    observed_at: str,
    observed_date: str,
) -> bool:
    version_id = version.get("id")
    if version_id is None:
        return False
    existing = conn.execute(
        "select 1 from model_versions where version_id = ?", (version_id,)
    ).fetchone()
    if existing:
        conn.execute(
            """
            update model_versions
            set last_seen_at = ?,
                created_at = ?,
                version_name = ?,
                model_type = ?,
                base_model_raw = ?,
                raw_json = ?
            where version_id = ?
            """,
            (
                observed_at,
                version.get("createdAt"),
                version.get("name"),
                model_type,
                version.get("baseModel") or "",
                raw_json(version),
                version_id,
            ),
        )
        return False
    conn.execute(
        """
        insert into model_versions (
            version_id, model_id, first_seen_at, first_seen_date, last_seen_at,
            created_at, version_name, model_type, base_model_raw, raw_json
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id,
            model_id,
            observed_at,
            observed_date,
            observed_at,
            version.get("createdAt"),
            version.get("name"),
            model_type,
            version.get("baseModel") or "",
            raw_json(version),
        ),
    )
    return True


def parse_extra_params(values: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"extra params must use key=value form: {value}")
        key, val = value.split("=", 1)
        params[key] = val
    return params


def collect_model_type(
    conn: sqlite3.Connection,
    errors_csv: Path,
    model_type: str,
    observed_at: str,
    observed_date: str,
    limit: int,
    max_pages: int,
    known_version_stop: int,
    request_delay: float,
    retries: int,
    retry_delay: float,
    extra_params: dict[str, str],
) -> tuple[int, int, int]:
    pages_fetched = 0
    models_seen = 0
    versions_seen = 0
    consecutive_known_versions = 0

    params: dict[str, Any] = {
        "limit": limit,
        "types": model_type,
        "sort": "Newest",
    }
    params.update(extra_params)
    next_url: str | None = build_url(API_BASE, params)

    while next_url and pages_fetched < max_pages:
        try:
            result = fetch_json(next_url, retries=retries, delay_seconds=retry_delay)
        except HTTPError as exc:
            record_error(
                conn,
                errors_csv,
                observed_at,
                observed_date,
                f"models:{model_type}",
                str(exc),
                url=next_url,
                http_status=exc.code,
            )
            break
        except Exception as exc:
            record_error(
                conn,
                errors_csv,
                observed_at,
                observed_date,
                f"models:{model_type}",
                repr(exc),
                url=next_url,
            )
            break

        pages_fetched += 1
        items = result.data.get("items") or []
        if not isinstance(items, list):
            record_error(
                conn,
                errors_csv,
                observed_at,
                observed_date,
                f"models:{model_type}",
                "API response did not contain a list at items",
                url=result.url,
            )
            break

        for model in items:
            if not isinstance(model, dict) or model.get("id") is None:
                continue
            models_seen += 1
            model_id = int(model["id"])
            upsert_model(conn, model, model_type, observed_at, observed_date)
            versions = model.get("modelVersions") or []
            if not isinstance(versions, list):
                continue
            for version in versions:
                if not isinstance(version, dict):
                    continue
                is_new = upsert_version(
                    conn, model_id, model.get("type") or model_type, version, observed_at, observed_date
                )
                versions_seen += 1
                if is_new:
                    consecutive_known_versions = 0
                else:
                    consecutive_known_versions += 1

        conn.commit()
        if known_version_stop > 0 and consecutive_known_versions >= known_version_stop:
            break

        metadata = result.data.get("metadata") or {}
        next_url = normalize_next_page(metadata.get("nextPage"))
        if not next_url:
            # Older API responses may expose only paging fields. Preserve query
            # params and advance page if possible.
            current_page = metadata.get("currentPage")
            total_pages = metadata.get("totalPages")
            if current_page and total_pages and int(current_page) < int(total_pages):
                parsed = urlparse(result.url)
                page_params = dict(parse_qsl(parsed.query))
                page_params["page"] = str(int(current_page) + 1)
                next_url = build_url(API_BASE, page_params)
            else:
                next_url = None

        if next_url and request_delay > 0:
            time.sleep(request_delay)

    return pages_fetched, models_seen, versions_seen


def fetch_active_total(
    model_type: str,
    base_model: str,
    extra_params: dict[str, str],
    retries: int,
    retry_delay: float,
) -> tuple[int | None, str]:
    params: dict[str, Any] = {"limit": 1, "types": model_type}
    if base_model:
        params["baseModels"] = base_model
    params.update(extra_params)
    url = build_url(API_BASE, params)
    result = fetch_json(url, retries=retries, delay_seconds=retry_delay)
    total = (result.data.get("metadata") or {}).get("totalItems")
    return int(total) if total is not None else None, result.url


def rebuild_daily_counts(
    conn: sqlite3.Connection,
    errors_csv: Path,
    observed_at: str,
    observed_date: str,
    include_active_totals: bool,
    retries: int,
    retry_delay: float,
    request_delay: float,
    extra_params: dict[str, str],
) -> None:
    rows = conn.execute(
        """
        select model_type, coalesce(base_model_raw, '') as base_model_raw, count(*) as count
        from model_versions
        where first_seen_date = ?
        group by model_type, coalesce(base_model_raw, '')
        order by model_type, base_model_raw
        """,
        (observed_date,),
    ).fetchall()

    new_model_rows = conn.execute(
        """
        select model_type, coalesce(initial_base_model_raw, '') as base_model_raw, count(*) as count
        from models
        where first_seen_date = ?
        group by model_type, coalesce(initial_base_model_raw, '')
        """,
        (observed_date,),
    ).fetchall()
    new_model_counts = {
        (row["model_type"], row["base_model_raw"]): int(row["count"]) for row in new_model_rows
    }

    seen_keys = {(row["model_type"], row["base_model_raw"]) for row in rows}
    seen_keys.update(new_model_counts.keys())

    conn.execute("delete from daily_counts where observed_date = ?", (observed_date,))
    for model_type, base_model_raw in sorted(seen_keys):
        version_count = next(
            (
                int(row["count"])
                for row in rows
                if row["model_type"] == model_type and row["base_model_raw"] == base_model_raw
            ),
            0,
        )
        active_total: int | None = None
        if include_active_totals:
            try:
                active_total, _ = fetch_active_total(
                    model_type, base_model_raw, extra_params, retries, retry_delay
                )
                if request_delay > 0:
                    time.sleep(request_delay)
            except HTTPError as exc:
                record_error(
                    conn,
                    errors_csv,
                    observed_at,
                    observed_date,
                    f"active_total:{model_type}:{base_model_raw}",
                    str(exc),
                    http_status=exc.code,
                )
            except Exception as exc:
                record_error(
                    conn,
                    errors_csv,
                    observed_at,
                    observed_date,
                    f"active_total:{model_type}:{base_model_raw}",
                    repr(exc),
                )
        conn.execute(
            """
            insert into daily_counts (
                observed_date, model_type, base_model_raw, new_version_count,
                new_model_count, active_total, collected_at
            ) values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observed_date,
                model_type,
                base_model_raw,
                version_count,
                new_model_counts.get((model_type, base_model_raw), 0),
                active_total,
                observed_at,
            ),
        )
    conn.commit()


def export_daily_counts(conn: sqlite3.Connection, path: Path) -> None:
    ensure_parent(path)
    rows = conn.execute(
        """
        select observed_date, model_type, base_model_raw, new_version_count,
               new_model_count, active_total, collected_at
        from daily_counts
        order by observed_date, model_type, base_model_raw
        """
    ).fetchall()
    fieldnames = [
        "observed_date",
        "model_type",
        "base_model_raw",
        "new_version_count",
        "new_model_count",
        "active_total",
        "collected_at",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row[key] for key in fieldnames})


def write_run_start(conn: sqlite3.Connection, run_id: str, observed_at: str, observed_date: str) -> None:
    conn.execute(
        """
        insert into collection_runs (
            run_id, started_at, observed_date, status
        ) values (?, ?, ?, ?)
        """,
        (run_id, observed_at, observed_date, "running"),
    )
    conn.commit()


def write_run_finish(
    conn: sqlite3.Connection,
    run_id: str,
    status: str,
    pages_fetched: int,
    models_seen: int,
    versions_seen: int,
    error_message: str | None = None,
) -> None:
    conn.execute(
        """
        update collection_runs
        set finished_at = ?, status = ?, pages_fetched = ?, models_seen = ?,
            versions_seen = ?, error_message = ?
        where run_id = ?
        """,
        (
            utc_now().isoformat(),
            status,
            pages_fetched,
            models_seen,
            versions_seen,
            error_message,
            run_id,
        ),
    )
    conn.commit()


def count_run_errors(conn: sqlite3.Connection, observed_at: str) -> int:
    row = conn.execute(
        "select count(*) as count from collection_errors where observed_at = ?",
        (observed_at,),
    ).fetchone()
    return int(row["count"] if row else 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/civitai.sqlite", type=Path)
    parser.add_argument("--daily-csv", default="data/daily_counts.csv", type=Path)
    parser.add_argument("--errors-csv", default="data/errors.csv", type=Path)
    parser.add_argument("--model-types", nargs="+", default=list(DEFAULT_MODEL_TYPES))
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=int(os.getenv("CIVITAI_MAX_PAGES", "20")))
    parser.add_argument("--known-version-stop", type=int, default=200)
    parser.add_argument("--request-delay", type=float, default=0.25)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=5.0)
    parser.add_argument("--timezone-offset", default="+09:00")
    parser.add_argument("--observed-date", help="Override observed date, YYYY-MM-DD")
    parser.add_argument(
        "--extra-param",
        action="append",
        default=[],
        help="Additional API query param in key=value form. Repeatable.",
    )
    parser.add_argument(
        "--skip-active-totals",
        action="store_true",
        help="Skip baseModel active_total API calls.",
    )
    parser.add_argument(
        "--fail-on-errors",
        action="store_true",
        help="Exit non-zero when any collection error is recorded.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tz = parse_timezone_offset(args.timezone_offset)
    now = utc_now()
    observed_date = args.observed_date or now.astimezone(tz).date().isoformat()
    observed_at = now.isoformat()
    extra_params = parse_extra_params(args.extra_param)

    conn = connect_db(args.db)
    init_db(conn)
    run_id = str(uuid.uuid4())
    write_run_start(conn, run_id, observed_at, observed_date)

    pages_fetched = 0
    models_seen = 0
    versions_seen = 0
    try:
        for model_type in args.model_types:
            pages, models, versions = collect_model_type(
                conn=conn,
                errors_csv=args.errors_csv,
                model_type=model_type,
                observed_at=observed_at,
                observed_date=observed_date,
                limit=args.limit,
                max_pages=args.max_pages,
                known_version_stop=args.known_version_stop,
                request_delay=args.request_delay,
                retries=args.retries,
                retry_delay=args.retry_delay,
                extra_params=extra_params,
            )
            pages_fetched += pages
            models_seen += models
            versions_seen += versions

        rebuild_daily_counts(
            conn=conn,
            errors_csv=args.errors_csv,
            observed_at=observed_at,
            observed_date=observed_date,
            include_active_totals=not args.skip_active_totals,
            retries=args.retries,
            retry_delay=args.retry_delay,
            request_delay=args.request_delay,
            extra_params=extra_params,
        )
        export_daily_counts(conn, args.daily_csv)
        error_count = count_run_errors(conn, observed_at)
        status = "success" if error_count == 0 else "partial"
        write_run_finish(conn, run_id, status, pages_fetched, models_seen, versions_seen)
        if args.fail_on_errors and error_count > 0:
            print(
                f"collector recorded {error_count} error(s); failing because --fail-on-errors is set",
                file=sys.stderr,
            )
            return 2
    except Exception as exc:
        conn.rollback()
        record_error(
            conn,
            args.errors_csv,
            observed_at,
            observed_date,
            "collector",
            repr(exc),
        )
        write_run_finish(
            conn, run_id, "failed", pages_fetched, models_seen, versions_seen, repr(exc)
        )
        raise
    finally:
        conn.close()

    print(
        json.dumps(
            {
                "observed_date": observed_date,
                "pages_fetched": pages_fetched,
                "models_seen": models_seen,
                "versions_seen": versions_seen,
                "daily_csv": str(args.daily_csv),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
