"""Observation and publication metrics without rendering or network access."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Mapping

from .grouping import grouped_name


MetricSeries = dict[str, dict[str, dict[str, dict[str, int]]]]


@dataclass(frozen=True)
class PublicationMetrics:
    dates: list[str]
    series: MetricSeries
    missing_publication_timestamps: dict[str, dict[str, int]]


def period_for(day: str, period: str) -> str:
    parsed = date.fromisoformat(day)
    if period == "weekly":
        iso = parsed.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if period == "monthly":
        return f"{parsed.year}-{parsed.month:02d}"
    if period != "daily":
        raise ValueError(f"unsupported period: {period}")
    return day


def aggregate_observations(
    rows: Iterable[Mapping[str, object]], groups: dict[str, str], period: str
) -> list[dict[str, object]]:
    totals: dict[tuple[str, str, str], dict[str, object]] = {}
    raw_members: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    configured_members: dict[str, set[str]] = defaultdict(set)
    for raw_value, group in groups.items():
        configured_members[group].add(raw_value)
    active_candidates: dict[tuple[str, str, str], tuple[str, object]] = {}
    for row in rows:
        raw = str(row["base_model_raw"] or "")
        key = (
            period_for(str(row["observed_date"]), period),
            str(row["model_type"]),
            grouped_name(raw, groups),
        )
        raw_members[key].add(raw)
        value = totals.setdefault(
            key,
            {"period": key[0], "model_type": key[1], "base_model_group": key[2], "new_version_count": 0, "new_model_count": 0, "active_total_last": None},
        )
        value["new_version_count"] = int(value["new_version_count"]) + int(row["new_version_count"] or 0)
        value["new_model_count"] = int(value["new_model_count"]) + int(row["new_model_count"] or 0)
        if row["active_total"] is not None:
            candidate = (str(row["observed_date"]), row["active_total"])
            if key not in active_candidates or candidate[0] >= active_candidates[key][0]:
                active_candidates[key] = candidate
    for key, value in totals.items():
        if len(raw_members[key]) == 1 and len(configured_members.get(key[2], raw_members[key])) == 1 and key in active_candidates:
            value["active_total_last"] = active_candidates[key][1]
    return [totals[key] for key in sorted(totals)]


def inclusive_dates(start: str, end: str) -> list[str]:
    current, finish = date.fromisoformat(start), date.fromisoformat(end)
    if current > finish:
        raise ValueError("start date must be on or before end date")
    result: list[str] = []
    while current <= finish:
        result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def _published_at(row: sqlite3.Row, legacy_json_fallback: bool) -> str | None:
    value = row["published_at"]
    if value or not legacy_json_fallback:
        return value
    try:
        payload = json.loads(row["raw_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    return payload.get("publishedAt")


def publication_metrics(
    conn: sqlite3.Connection,
    groups: dict[str, str],
    tz: timezone,
    start_date: str,
    end_date: str,
    model_type: str | None = None,
    *,
    legacy_json_fallback: bool = False,
) -> PublicationMetrics:
    columns = {row[1] for row in conn.execute("pragma table_info(model_versions)")}
    published_expr = "published_at" if "published_at" in columns else "null as published_at"
    raw_json_field = ", raw_json" if legacy_json_fallback else ""
    rows = conn.execute(
        f"select version_id, model_id, model_type, base_model_raw, {published_expr}{raw_json_field} from model_versions order by version_id"
    )
    series: MetricSeries = defaultdict(
        lambda: {"new_versions": defaultdict(lambda: defaultdict(int)), "new_models": defaultdict(lambda: defaultdict(int))}
    )
    missing: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    first_by_model: dict[int, tuple[datetime, str, str]] = {}
    for row in rows:
        row_type = str(row["model_type"])
        if model_type and row_type != model_type:
            continue
        group = grouped_name(row["base_model_raw"], groups)
        raw_published = _published_at(row, legacy_json_fallback)
        if not raw_published:
            missing[row_type][group] += 1
            continue
        try:
            published = datetime.fromisoformat(str(raw_published).replace("Z", "+00:00"))
            if published.tzinfo is None:
                raise ValueError("publication timestamp must include a timezone")
            published = published.astimezone(tz)
        except ValueError:
            missing[row_type][group] += 1
            continue
        day = published.date().isoformat()
        if start_date <= day <= end_date:
            series[row_type]["new_versions"][group][day] += 1
        model_id = int(row["model_id"])
        current = first_by_model.get(model_id)
        if current is None or published < current[0]:
            first_by_model[model_id] = (published, row_type, group)
    for published, row_type, group in first_by_model.values():
        day = published.date().isoformat()
        if start_date <= day <= end_date:
            series[row_type]["new_models"][group][day] += 1
    return PublicationMetrics(inclusive_dates(start_date, end_date), series, missing)


def bucket_publications(metrics: PublicationMetrics, period: str) -> PublicationMetrics:
    if period == "daily":
        return metrics
    bucketed: MetricSeries = defaultdict(
        lambda: {"new_versions": defaultdict(lambda: defaultdict(int)), "new_models": defaultdict(lambda: defaultdict(int))}
    )
    for model_type, metric_map in metrics.series.items():
        for metric, group_map in metric_map.items():
            for group, values in group_map.items():
                for day, count in values.items():
                    bucketed[model_type][metric][group][period_for(day, period)] += count
    dates = sorted({period_for(day, period) for day in metrics.dates})
    return PublicationMetrics(dates, bucketed, metrics.missing_publication_timestamps)


def slice_publications(
    metrics: PublicationMetrics, start_date: str, end_date: str
) -> PublicationMetrics:
    sliced: MetricSeries = defaultdict(
        lambda: {
            "new_versions": defaultdict(lambda: defaultdict(int)),
            "new_models": defaultdict(lambda: defaultdict(int)),
        }
    )
    for model_type, metric_map in metrics.series.items():
        for metric, group_map in metric_map.items():
            for group, values in group_map.items():
                for day, count in values.items():
                    if start_date <= day <= end_date:
                        sliced[model_type][metric][group][day] = count
    return PublicationMetrics(
        inclusive_dates(start_date, end_date),
        sliced,
        metrics.missing_publication_timestamps,
    )
