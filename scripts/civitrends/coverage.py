"""Conservative coverage and publishable-anchor calculations."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, timezone

from .collector import REQUIRED_MODEL_TYPES
from .timeutils import JST


def _completed_type_rows(conn: sqlite3.Connection, observed_date: str):
    return conn.execute(
        """select rt.* from collection_run_types rt
           join collection_runs r on r.run_id=rt.run_id
           where r.observed_date=? and r.execution_status='completed'
             and rt.query_kind='normal'""",
        (observed_date,),
    ).fetchall()


def daily_coverage(conn: sqlite3.Connection, dates: list[str],
                   tz: timezone = JST) -> dict[str, str]:
    result: dict[str, str] = {}
    for day in dates:
        rows = _completed_type_rows(conn, day)
        by_type: dict[str, list[sqlite3.Row]] = {name: [] for name in REQUIRED_MODEL_TYPES}
        for row in rows:
            if row["model_type"] in by_type:
                by_type[row["model_type"]].append(row)
        if not all(by_type.values()):
            result[day] = "unknown"
        elif all(_covers_local_day(by_type[model_type], day, tz)
                 for model_type in REQUIRED_MODEL_TYPES):
            result[day] = "verified"
        elif any(
            int(row["models_seen"]) > 0 or int(row["versions_seen"]) > 0
            for rows_for_type in by_type.values() for row in rows_for_type
        ):
            result[day] = "partial"
        else:
            result[day] = "unknown"
    return result


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _covers_local_day(rows: list[sqlite3.Row], day: str, tz: timezone) -> bool:
    local_day = date.fromisoformat(day)
    target_start = datetime.combine(local_day, time.min, tzinfo=tz).astimezone(timezone.utc)
    target_end = (
        datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=tz)
        .astimezone(timezone.utc)
        - timedelta(seconds=1)
    )
    intervals: list[tuple[datetime, datetime]] = []
    for row in rows:
        if row["coverage_status"] != "verified":
            continue
        requested_start = _parse_timestamp(row["requested_started_at"])
        requested_end = _parse_timestamp(row["requested_finished_at"])
        fetched = _parse_timestamp(row["fetch_finished_at"])
        if requested_start is None or requested_end is None or fetched is None:
            continue
        start = max(requested_start, target_start)
        end = min(requested_end, fetched, target_end)
        if start <= end:
            intervals.append((start, end))
    covered_until: datetime | None = None
    for start, end in sorted(intervals):
        if covered_until is None:
            if start > target_start:
                return False
            covered_until = end
        elif start > covered_until + timedelta(seconds=1):
            return False
        else:
            covered_until = max(covered_until, end)
        if covered_until >= target_end:
            return True
    return False


def anchor_date(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        """select max(r.observed_date) from collection_runs r
           where r.execution_status='completed'
             and exists (select 1 from collection_run_types t where t.run_id=r.run_id
                         and t.model_type='Checkpoint' and t.query_kind='normal'
                         and t.error_count=0 and t.stop_reason!='error')
             and exists (select 1 from collection_run_types t where t.run_id=r.run_id
                         and t.model_type='LORA' and t.query_kind='normal'
                         and t.error_count=0 and t.stop_reason!='error')"""
    ).fetchone()
    if not row or row[0] is None:
        raise ValueError("no completed collection batch with both required model types")
    return str(row[0])


def measurement_start(conn: sqlite3.Connection) -> str:
    row = conn.execute("select min(observed_date) from collection_runs").fetchone()
    if row and row[0] is not None:
        return str(row[0])
    row = conn.execute("select min(observed_date) from daily_counts").fetchone()
    if row and row[0] is not None:
        return str(row[0])
    raise ValueError("no collection history")
