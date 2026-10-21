"""Orchestrate validated API pages and transactional collection persistence."""

from __future__ import annotations

import json
import math
import sqlite3
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.error import HTTPError

from . import api, storage
from .timeutils import utc_now


REQUIRED_MODEL_TYPES = ("Checkpoint", "LORA")


@dataclass(frozen=True)
class CollectionResult:
    run_id: str | None
    model_type: str
    query: dict[str, Any]
    stop_reason: str
    pages_fetched: int
    models_seen: int
    versions_seen: int
    new_models: int
    new_versions: int
    error_count: int
    requested_started_at: str | None
    requested_finished_at: str | None
    fetch_finished_at: str
    coverage_status: str
    query_kind: str = "normal"

    def __iter__(self) -> Iterator[int]:
        yield self.pages_fetched
        yield self.models_seen
        yield self.versions_seen


def _query_json(query: dict[str, Any]) -> str:
    return json.dumps(query, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_collection_result(conn: sqlite3.Connection, result: CollectionResult) -> None:
    if result.run_id is None:
        return
    conn.execute(
        """insert or replace into collection_run_types
           (run_id, model_type, query_json, query_kind, stop_reason, pages_fetched, models_seen,
            versions_seen, new_models, new_versions, error_count,
            requested_started_at, requested_finished_at, fetch_finished_at, coverage_status)
           values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (result.run_id, result.model_type, _query_json(result.query), result.query_kind,
         result.stop_reason,
         result.pages_fetched, result.models_seen, result.versions_seen,
         result.new_models, result.new_versions, result.error_count,
         result.requested_started_at, result.requested_finished_at,
         result.fetch_finished_at, result.coverage_status),
    )
    conn.commit()


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
    *,
    run_id: str | None = None,
    requested_started_at: str | None = None,
    requested_finished_at: str | None = None,
    fetcher: Callable[[str, int, float], api.FetchResult] = api.fetch_json,
    query_kind: str | None = None,
) -> CollectionResult:
    if not model_type or limit <= 0 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    if max_pages <= 0 or known_version_stop < 0:
        raise ValueError("max_pages must be positive and known_version_stop non-negative")
    if request_delay < 0 or retries < 0 or retry_delay < 0:
        raise ValueError("delay and retry arguments must be non-negative")
    if not math.isfinite(request_delay) or not math.isfinite(retry_delay):
        raise ValueError("delay arguments must be finite")
    reserved = {"limit", "types", "sort", "cursor"}.intersection(extra_params)
    if reserved:
        raise ValueError(f"extra_params contains reserved query keys: {sorted(reserved)}")
    resolved_query_kind = query_kind or ("supplemental" if extra_params else "normal")
    query: dict[str, Any] = {"limit": limit, "types": model_type, "sort": "Newest"}
    query.update(extra_params)
    next_url: str | None = api.build_url(api.API_BASE, query)
    visited: set[str] = set()
    pages = models = versions = new_models = new_versions = errors = 0
    consecutive_known_versions = 0
    stop_reason = "exhausted"

    while next_url:
        if pages >= max_pages:
            stop_reason = "page_budget"
            break
        if next_url in visited:
            storage.record_error(conn, errors_csv, observed_at, observed_date,
                                 f"models:{model_type}", "paging loop detected",
                                 url=next_url, run_id=run_id)
            errors += 1
            stop_reason = "error"
            break
        visited.add(next_url)
        try:
            result = fetcher(next_url, retries, retry_delay)
            page = api.validate_page(result.data)
        except HTTPError as exc:
            storage.record_error(conn, errors_csv, observed_at, observed_date,
                                 f"models:{model_type}", str(exc), next_url, exc.code, run_id)
            errors += 1
            stop_reason = "error"
            break
        except Exception as exc:
            storage.record_error(conn, errors_csv, observed_at, observed_date,
                                 f"models:{model_type}", repr(exc), next_url, run_id=run_id)
            errors += 1
            stop_reason = "error"
            break

        pages += 1
        try:
            conn.execute("savepoint collection_page")
            for model in page["items"]:
                models += 1
                model_id = int(model["id"])
                if storage.upsert_model(conn, model, model_type, observed_at, observed_date):
                    new_models += 1
                for version in model.get("modelVersions", []):
                    is_new = storage.upsert_version(
                        conn, model_id, model.get("type") or model_type,
                        version, observed_at, observed_date,
                    )
                    versions += 1
                    if is_new:
                        new_versions += 1
                        consecutive_known_versions = 0
                    else:
                        consecutive_known_versions += 1
            conn.execute("release collection_page")
            conn.commit()
        except Exception as exc:
            conn.execute("rollback to collection_page")
            conn.execute("release collection_page")
            storage.record_error(conn, errors_csv, observed_at, observed_date,
                                 f"models:{model_type}", repr(exc), result.url, run_id=run_id)
            conn.commit()
            errors += 1
            stop_reason = "error"
            break

        if known_version_stop and consecutive_known_versions >= known_version_stop:
            stop_reason = "known_threshold"
            break
        metadata = page["metadata"]
        raw_next = metadata.get("nextPage")
        try:
            cursor_url = api.cursor_next_page(query, metadata)
        except ValueError as exc:
            storage.record_error(conn, errors_csv, observed_at, observed_date,
                                 f"models:{model_type}", str(exc), result.url,
                                 run_id=run_id)
            conn.commit()
            errors += 1
            stop_reason = "error"
            break
        normalized = None if cursor_url else api.normalize_next_page(raw_next, result.url)
        if not cursor_url and raw_next and normalized is None:
            storage.record_error(conn, errors_csv, observed_at, observed_date,
                                 f"models:{model_type}", "unsafe paging URL", str(raw_next),
                                 run_id=run_id)
            conn.commit()
            errors += 1
            stop_reason = "error"
            break
        if not cursor_url and not normalized:
            current_page = metadata.get("currentPage")
            total_pages = metadata.get("totalPages")
            if (isinstance(current_page, int) and isinstance(total_pages, int)
                    and current_page < total_pages and current_page * limit >= 1000):
                stop_reason = "page_budget"
                break
        next_url = cursor_url or normalized or api.fallback_next_page(result.url, metadata)
        if next_url and request_delay:
            time.sleep(request_delay)

    coverage_status = "partial" if models > 0 or versions > 0 else "unknown"
    collection_result = CollectionResult(
        run_id=run_id, model_type=model_type, query=query, stop_reason=stop_reason,
        pages_fetched=pages, models_seen=models, versions_seen=versions,
        new_models=new_models, new_versions=new_versions, error_count=errors,
        requested_started_at=requested_started_at,
        requested_finished_at=requested_finished_at,
        fetch_finished_at=utc_now().isoformat(), coverage_status=coverage_status,
        query_kind=resolved_query_kind,
    )
    write_collection_result(conn, collection_result)
    return collection_result


def write_run_start(conn: sqlite3.Connection, run_id: str, observed_at: str,
                    observed_date: str, coverage_started_at: str,
                    coverage_finished_at: str, known_version_stop: int) -> None:
    conn.execute(
        """insert into collection_runs
           (run_id, started_at, observed_date, coverage_started_at,
            coverage_finished_at, status, execution_status, known_version_stop)
           values (?, ?, ?, ?, ?, 'running', 'running', ?)""",
        (run_id, observed_at, observed_date, coverage_started_at,
         coverage_finished_at, known_version_stop),
    )
    conn.commit()


def collect_active_total_snapshots(
    conn: sqlite3.Connection,
    errors_csv: Path,
    run_id: str,
    observed_at: str,
    observed_date: str,
    extra_params: dict[str, str],
    retries: int,
    retry_delay: float,
    request_delay: float,
    fetcher: Callable[[str, str, dict[str, str], int, float], tuple[int | None, str]],
) -> int:
    """Fetch opt-in inventory snapshots separately from metric aggregation."""
    errors = 0
    pairs = conn.execute(
        """select distinct model_type, coalesce(base_model_raw, '') as base_model_raw
           from model_versions order by model_type, base_model_raw"""
    ).fetchall()
    fetchable_pairs = [row for row in pairs if str(row["base_model_raw"])]
    for row in pairs:
        if not str(row["base_model_raw"]):
            warnings.warn(
                f"Skipping active_total snapshot for {row['model_type']} with empty baseModel; "
                "a type-wide total would be mislabeled",
                RuntimeWarning,
                stacklevel=2,
            )
    for index, row in enumerate(fetchable_pairs):
        model_type = str(row["model_type"])
        base_model = str(row["base_model_raw"])
        try:
            total, source_url = fetcher(
                model_type, base_model, extra_params, retries, retry_delay
            )
            if total is not None:
                conn.execute(
                    """insert or replace into active_total_history
                       (observed_at, model_type, base_model_raw, active_total, source_url, run_id)
                       values (?, ?, ?, ?, ?, ?)""",
                    (observed_at, model_type, base_model, total, source_url, run_id),
                )
                conn.commit()
        except HTTPError as exc:
            storage.record_error(
                conn, errors_csv, observed_at, observed_date,
                f"active_total:{model_type}:{base_model}", str(exc),
                http_status=exc.code, run_id=run_id,
            )
            conn.commit()
            errors += 1
        except Exception as exc:
            storage.record_error(
                conn, errors_csv, observed_at, observed_date,
                f"active_total:{model_type}:{base_model}", repr(exc), run_id=run_id,
            )
            conn.commit()
            errors += 1
        if request_delay and index < len(fetchable_pairs) - 1:
            time.sleep(request_delay)
    return errors


def finish_batch(conn: sqlite3.Connection, run_id: str,
                 error_message: str | None = None) -> str:
    rows = conn.execute(
        "select * from collection_run_types where run_id=?", (run_id,)
    ).fetchall()
    by_type = {
        row["model_type"]: row for row in rows if row["query_kind"] == "normal"
    }
    complete = all(
        model_type in by_type
        and by_type[model_type]["stop_reason"] != "error"
        and int(by_type[model_type]["error_count"]) == 0
        for model_type in REQUIRED_MODEL_TYPES
    )
    execution_status = "completed" if complete and error_message is None else "failed"
    result_errors = sum(int(row["error_count"]) for row in rows)
    recorded_errors = int(conn.execute(
        "select count(*) from collection_errors where run_id=?", (run_id,)
    ).fetchone()[0])
    total_errors = result_errors + recorded_errors
    if total_errors:
        execution_status = "failed"
    legacy_status = (
        "success" if execution_status == "completed"
        else "failed" if error_message is not None
        else "partial"
    )
    conn.execute(
        """update collection_runs set finished_at=?, status=?, execution_status=?,
           pages_fetched=?, models_seen=?, versions_seen=?, error_message=? where run_id=?""",
        (utc_now().isoformat(), legacy_status, execution_status,
         sum(int(row["pages_fetched"]) for row in rows),
         sum(int(row["models_seen"]) for row in rows),
         sum(int(row["versions_seen"]) for row in rows),
         error_message if error_message is not None else
         (f"{total_errors} collection error(s)" if total_errors else None), run_id),
    )
    conn.commit()
    return execution_status


def write_run_finish(conn: sqlite3.Connection, run_id: str, status: str,
                     pages_fetched: int, models_seen: int, versions_seen: int,
                     error_message: str | None = None) -> None:
    execution_status = "completed" if status == "success" else "failed"
    conn.execute(
        """update collection_runs set finished_at=?, status=?, execution_status=?,
           pages_fetched=?, models_seen=?, versions_seen=?, error_message=? where run_id=?""",
        (utc_now().isoformat(), status, execution_status, pages_fetched,
         models_seen, versions_seen, error_message, run_id),
    )
    conn.commit()


def count_run_errors(conn: sqlite3.Connection, run_id_or_observed_at: str) -> int:
    row = conn.execute(
        "select count(*) from collection_errors where run_id=? or (run_id is null and observed_at=?)",
        (run_id_or_observed_at, run_id_or_observed_at),
    ).fetchone()
    return int(row[0])
