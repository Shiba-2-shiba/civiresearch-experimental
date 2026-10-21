"""Build a complete, deterministic export batch from one SQLite snapshot."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping

from .grouping import grouped_name, grouping_hash, load_group_map
from .metrics import PublicationMetrics, aggregate_observations, bucket_publications, inclusive_dates, period_for, publication_metrics, slice_publications
from .rendering import render_svg, select_visible_groups
from .timeutils import parse_timezone_offset


REQUIRED_EXPORTS = (
    "daily_counts.csv", "weekly_counts.csv", "monthly_counts.csv",
    "base_model_audit.csv", "publication_daily_counts.csv", "coverage_daily.csv",
    "series_diagnostics.csv", "trends-all.svg", "trends-90d.svg",
    "trends-30d.svg", "trends-14d.svg", "trends.svg",
)


@dataclass(frozen=True)
class ViewSpec:
    name: str
    days: int | None
    bucket: str
    max_series: int
    pinned_groups: tuple[str, ...]
    label_interval: int | None


@dataclass(frozen=True)
class TrendViewConfig:
    schema_version: int
    timezone_name: str
    timezone: timezone
    max_series: int
    pinned_groups: tuple[str, ...]
    colors: dict[str, str]
    views: tuple[ViewSpec, ...]


def dates_for_view(anchor: str, measurement_start: str, view: ViewSpec) -> list[str]:
    end = date.fromisoformat(anchor)
    measured = date.fromisoformat(measurement_start)
    start = measured if view.days is None else max(measured, end - timedelta(days=view.days - 1))
    return inclusive_dates(start.isoformat(), end.isoformat())


def load_view_config(path: Path) -> TrendViewConfig:
    data = json.loads(path.read_text(encoding="utf-8"))
    allowed_global = {"schema_version", "timezone", "max_series", "pinned_groups", "colors", "views"}
    unknown_global = set(data) - allowed_global
    if unknown_global:
        raise ValueError(f"unknown view config fields: {sorted(unknown_global)}")
    if data.get("schema_version") != 1:
        raise ValueError("trend view schema_version must be 1")
    timezone_name = str(data.get("timezone", ""))
    tz = parse_timezone_offset(timezone_name)
    default_max = data.get("max_series")
    if not isinstance(default_max, int) or isinstance(default_max, bool) or default_max <= 0:
        raise ValueError("max_series must be a positive integer")
    default_pins = tuple(str(value) for value in data.get("pinned_groups", []))
    if len(default_pins) != len(set(default_pins)) or len(default_pins) > default_max:
        raise ValueError("pinned_groups must be unique and cannot exceed max_series")
    colors = data.get("colors", {})
    if not isinstance(colors, dict) or any(
        not isinstance(group, str) or not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color)
        for group, color in colors.items()
    ):
        raise ValueError("colors must map group names to six-digit hex colors")
    result: list[ViewSpec] = []
    names: set[str] = set()
    for raw in data.get("views", []):
        allowed_view = {"name", "days", "bucket", "max_series", "pinned_groups", "label_interval"}
        unknown_view = set(raw) - allowed_view
        if unknown_view:
            raise ValueError(f"unknown fields for view {raw.get('name')}: {sorted(unknown_view)}")
        maximum = raw.get("max_series", default_max)
        pins = tuple(str(value) for value in raw.get("pinned_groups", default_pins))
        label_interval = raw.get("label_interval")
        view = ViewSpec(
            str(raw["name"]), raw.get("days"), str(raw["bucket"]), maximum,
            pins, label_interval,
        )
        days_invalid = view.days is not None and (
            not isinstance(view.days, int) or isinstance(view.days, bool) or view.days <= 0
        )
        label_invalid = view.label_interval is not None and (
            not isinstance(view.label_interval, int) or isinstance(view.label_interval, bool) or view.label_interval <= 0
        )
        if (
            view.name in names or days_invalid or view.bucket not in {"day", "week"}
            or not isinstance(view.max_series, int) or isinstance(view.max_series, bool)
            or view.max_series <= 0 or label_invalid or len(view.pinned_groups) > view.max_series
            or len(view.pinned_groups) != len(set(view.pinned_groups))
        ):
            raise ValueError(f"invalid view configuration: {view.name}")
        names.add(view.name)
        result.append(view)
    if names != {"all", "90d", "30d", "14d"}:
        raise ValueError("views must define all, 90d, 30d, and 14d")
    by_name = {view.name: view for view in result}
    if by_name["all"].days is not None or by_name["all"].bucket != "week":
        raise ValueError("all view must use days=null and bucket=week")
    for name, days in (("90d", 90), ("30d", 30), ("14d", 14)):
        if by_name[name].days != days or by_name[name].bucket != "day":
            raise ValueError(f"{name} view must use days={days} and bucket=day")
    return TrendViewConfig(1, timezone_name, tz, default_max, default_pins, dict(colors), tuple(result))


def load_views(path: Path) -> list[ViewSpec]:
    return list(load_view_config(path).views)


def _write_csv(path: Path, rows: Iterable[Mapping[str, object]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_id(path: Path) -> str:
    return _sha256(path)


def _anchor_date(conn: sqlite3.Connection) -> str:
    from .coverage import anchor_date
    value = anchor_date(conn)
    if not value:
        raise ValueError("no completed collection batch is available")
    return value


def _require_latest_collection_completed(conn: sqlite3.Connection) -> str:
    columns = {row[1] for row in conn.execute("pragma table_info(collection_runs)")}
    if "execution_status" not in columns:
        raise ValueError("collection_runs lacks execution_status")
    order = "observed_date desc" + (", started_at desc" if "started_at" in columns else "")
    row = conn.execute(f"select execution_status from collection_runs order by {order} limit 1").fetchone()
    if not row or row[0] != "completed":
        status = row[0] if row else "missing"
        raise ValueError(f"latest collection is not completed: {status}")
    observed = conn.execute(
        f"select observed_date from collection_runs order by {order} limit 1"
    ).fetchone()
    return str(observed[0])


def _measurement_start(conn: sqlite3.Connection) -> str:
    from .coverage import measurement_start
    value = measurement_start(conn)
    if not value:
        raise ValueError("measurement start is unavailable")
    return value


def _daily_coverage(conn: sqlite3.Connection, dates: list[str]) -> dict[str, str]:
    from .coverage import JST, daily_coverage
    return daily_coverage(conn, dates, JST)


def _graph_coverage(daily: Mapping[str, str], dates: list[str], period: str) -> dict[str, str]:
    if period == "daily":
        return {day: daily.get(day, "unknown") for day in dates}
    buckets: dict[str, list[str]] = {}
    for day in dates:
        buckets.setdefault(period_for(day, period), []).append(daily.get(day, "unknown"))
    result: dict[str, str] = {}
    for bucket, statuses in buckets.items():
        result[bucket] = "unknown" if "unknown" in statuses else ("partial" if "partial" in statuses else "verified")
    return result


def _daily_rows(conn: sqlite3.Connection, anchor: str) -> list[dict[str, object]]:
    fields = ["observed_date", "model_type", "base_model_raw", "new_version_count", "new_model_count", "active_total", "collected_at"]
    return [dict(row) for row in conn.execute(f"select {', '.join(fields)} from daily_counts where observed_date <= ? order by observed_date, model_type, base_model_raw", (anchor,))]


def _publication_rows(metrics: PublicationMetrics) -> list[dict[str, object]]:
    keys: set[tuple[str, str, str]] = set()
    for model_type, metric_map in metrics.series.items():
        for group_map in metric_map.values():
            for group, values in group_map.items():
                keys.update((day, model_type, group) for day in values)
    rows = []
    for day, model_type, group in sorted(keys):
        rows.append({
            "publication_date": day, "model_type": model_type, "base_model_group": group,
            "published_version_count": metrics.series[model_type]["new_versions"].get(group, {}).get(day, 0),
            "first_known_model_publication_count": metrics.series[model_type]["new_models"].get(group, {}).get(day, 0),
        })
    return rows


def _audit_rows(conn: sqlite3.Connection, groups: dict[str, str]) -> list[dict[str, object]]:
    daily = {(row[0], row[1]) for row in conn.execute("select distinct model_type, coalesce(base_model_raw, '') from daily_counts")}
    rows = conn.execute("select model_type, coalesce(base_model_raw, ''), count(*) from model_versions group by model_type, coalesce(base_model_raw, '') order by model_type, 2").fetchall()
    result = []
    for model_type, raw, count in rows:
        result.append({"status": "grouped" if raw in groups else "ungrouped", "model_type": model_type, "base_model_raw": raw, "base_model_group": groups.get(raw, ""), "version_count": count, "present_in_daily_counts": "yes" if (model_type, raw) in daily else "no"})
    return result


def _observation_counts(
    conn: sqlite3.Connection,
    groups: dict[str, str],
    start_date: str,
    end_date: str,
) -> dict[tuple[str, str, str], int]:
    result: dict[tuple[str, str, str], int] = {}
    sources = (
        ("new_versions", "model_versions", "base_model_raw"),
        ("new_models", "models", "initial_base_model_raw"),
    )
    for metric, table, raw_column in sources:
        rows = conn.execute(
            f"""select model_type, coalesce({raw_column}, '') as base_model_raw, count(*)
                from {table} where first_seen_date between ? and ?
                group by model_type, coalesce({raw_column}, '')""",
            (start_date, end_date),
        )
        for model_type, raw, count in rows:
            key = (str(model_type), metric, grouped_name(raw, groups))
            result[key] = result.get(key, 0) + int(count)
    return result


def _diagnostics(
    view: ViewSpec,
    metrics: PublicationMetrics,
    observation_counts: Mapping[tuple[str, str, str], int],
    missing_timestamp_counts: Mapping[tuple[str, str], int],
    publication_errors: Mapping[tuple[str, str], dict[str, object]],
) -> list[dict[str, object]]:
    result = []
    model_types = sorted(set(metrics.series) | {"Checkpoint", "LORA"})
    for model_type in model_types:
        for metric in ("new_versions", "new_models"):
            group_values = metrics.series.get(model_type, {}).get(metric, {})
            missing_groups = {group for (kind, group), count in missing_timestamp_counts.items() if kind == model_type and count}
            groups = sorted(set(group_values) | missing_groups | set(view.pinned_groups))
            totals = {group: sum(group_values.get(group, {}).values()) for group in groups}
            ranked = sorted(groups, key=lambda group: (-totals[group], group))
            visible = set(select_visible_groups(totals, view.max_series, view.pinned_groups))
            for group in groups:
                count = totals[group]
                missing = missing_timestamp_counts.get((model_type, group), 0)
                observations = observation_counts.get((model_type, metric, group), 0)
                if count == 0 and missing:
                    reason = "missing_publication_timestamp"
                elif count == 0 and observations == 0:
                    reason = "not_observed"
                elif count == 0:
                    reason = "no_window_publications"
                elif group in view.pinned_groups and group in visible:
                    reason = "pinned"
                elif group in visible:
                    reason = "ranked"
                else:
                    reason = "outside_rank"
                errors = publication_errors.get((model_type, group), {"version_ids": [], "raw_values": []})
                result.append({"window": view.name, "model_type": model_type, "metric": metric, "base_model_group": group, "observation_count": observation_counts.get((model_type, metric, group), 0), "publication_count": count, "missing_timestamp_count_through_anchor": missing, "missing_version_ids_through_anchor": json.dumps(errors["version_ids"], ensure_ascii=False), "missing_raw_values_through_anchor": json.dumps(errors["raw_values"], ensure_ascii=False), "rank": ranked.index(group) + 1, "visible": "yes" if count > 0 and group in visible else "no", "reason": reason})
    return result


def _publication_error_details(
    conn: sqlite3.Connection,
    groups: dict[str, str],
    anchor_date: str,
) -> dict[tuple[str, str], dict[str, object]]:
    result: dict[tuple[str, str], dict[str, object]] = {}
    rows = conn.execute(
        """select version_id, model_type, coalesce(base_model_raw, ''), published_at
           from model_versions where first_seen_date <= ?""",
        (anchor_date,),
    )
    for version_id, model_type, raw, published_at in rows:
        valid = False
        if published_at:
            try:
                parsed = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
                valid = parsed.tzinfo is not None
            except ValueError:
                pass
        if not valid:
            key = (str(model_type), grouped_name(raw, groups))
            detail = result.setdefault(key, {"version_ids": [], "raw_values": set()})
            detail["version_ids"].append(int(version_id))
            detail["raw_values"].add(str(raw))
    for detail in result.values():
        detail["version_ids"] = sorted(detail["version_ids"])
        detail["raw_values"] = sorted(detail["raw_values"])
    return result


def _pinned_states(
    view: ViewSpec,
    observation_counts: Mapping[tuple[str, str, str], int],
    missing_counts: Mapping[tuple[str, str], int],
) -> dict[tuple[str, str, str], str]:
    result: dict[tuple[str, str, str], str] = {}
    for model_type in ("Checkpoint", "LORA"):
        for metric in ("new_versions", "new_models"):
            for group in view.pinned_groups:
                observations = observation_counts.get((model_type, metric, group), 0)
                missing = missing_counts.get((model_type, group), 0)
                if missing:
                    reason = "publication timestamps missing"
                elif observations == 0:
                    reason = "not observed in this window"
                else:
                    reason = "no publications in this window"
                result[(model_type, metric, group)] = reason
    return result


def _label_interval(view: ViewSpec, bucket_count: int) -> int:
    if view.label_interval is not None:
        return view.label_interval
    return max(1, (bucket_count + 9) // 10)


def _partial_week_note(measurement_start: str, anchor: str) -> str | None:
    start = date.fromisoformat(measurement_start)
    end = date.fromisoformat(anchor)
    start_week = period_for(measurement_start, "weekly")
    end_week = period_for(anchor, "weekly")
    if start_week == end_week:
        if start.isoweekday() == 1 and end.isoweekday() == 7:
            return None
        return f"Partial ISO week {start_week}: {measurement_start} through {anchor}"
    notes: list[str] = []
    if start.isoweekday() != 1:
        notes.append(f"Partial ISO week {start_week}: starts {measurement_start}")
    if end.isoweekday() != 7:
        notes.append(f"Partial ISO week {end_week}: through {anchor}")
    return "; ".join(notes) or None


def _view_manifest(
    view: ViewSpec,
    metrics: PublicationMetrics,
    coverage: Mapping[str, str],
) -> dict[str, object]:
    selected: dict[str, dict[str, list[str]]] = {}
    for model_type, metric_map in metrics.series.items():
        selected[model_type] = {}
        for metric, group_map in metric_map.items():
            totals = {group: sum(values.values()) for group, values in group_map.items()}
            selected[model_type][metric] = select_visible_groups(totals, view.max_series, view.pinned_groups)
    quality = {status: sum(1 for value in coverage.values() if value == status) for status in ("verified", "partial", "unknown")}
    return {
        "start_date": metrics.dates[0],
        "end_date": metrics.dates[-1],
        "bucket": view.bucket,
        "max_series": view.max_series,
        "pinned_groups": list(view.pinned_groups),
        "selected_series": selected,
        "quality": quality,
    }


def _install_stage(stage: Path, output_dir: Path) -> None:
    backup = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.backup-", dir=output_dir.parent))
    backup.rmdir()
    had_previous = output_dir.exists()
    try:
        if had_previous:
            os.replace(output_dir, backup)
        os.replace(stage, output_dir)
    except Exception:
        if had_previous and backup.exists() and not output_dir.exists():
            os.replace(backup, output_dir)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def build_exports(
    db_path: Path,
    output_dir: Path,
    groups_path: Path,
    views_path: Path,
    generated_at: str,
) -> dict[str, object]:
    destination = output_dir.resolve()
    for source in (db_path, groups_path, views_path):
        resolved = source.resolve()
        if destination == resolved or destination in resolved.parents:
            raise ValueError("Export directory must not contain the input database or configuration")
    if output_dir.is_symlink():
        raise ValueError("Export directory must not be a symbolic link")
    if output_dir.exists():
        allowed = set(REQUIRED_EXPORTS) | {"export_manifest.json"}
        if not output_dir.is_dir() or any(
            path.name not in allowed or not path.is_file() or path.is_symlink()
            for path in output_dir.iterdir()
        ):
            raise ValueError("Export directory contains unrelated files; choose a dedicated directory")
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    if generated.tzinfo is None:
        raise ValueError("generated_at must include a timezone")
    groups = load_group_map(groups_path)
    view_config = load_view_config(views_path)
    views = list(view_config.views)
    previous_manifest = output_dir / "export_manifest.json"
    previous_anchor = None
    if previous_manifest.exists():
        previous_anchor = json.loads(previous_manifest.read_text(encoding="utf-8")).get("anchor_date")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent))
    snapshot_id = _snapshot_id(db_path)
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    view_metadata: dict[str, object] = {}
    try:
        conn.execute("begin")
        latest_date = _require_latest_collection_completed(conn)
        anchor, measurement_start = _anchor_date(conn), _measurement_start(conn)
        if latest_date != anchor:
            raise ValueError("latest completed collection is not a publishable anchor")
        if previous_anchor and anchor < previous_anchor:
            raise ValueError("completed collection anchor would move backward")
        all_dates = inclusive_dates(measurement_start, anchor)
        coverage = _daily_coverage(conn, all_dates)
        daily = _daily_rows(conn, anchor)
        full_metrics = publication_metrics(conn, groups, view_config.timezone, measurement_start, anchor)
        publication_errors_through_anchor = _publication_error_details(conn, groups, anchor)
        missing_counts_through_anchor = {
            key: len(detail["version_ids"])
            for key, detail in publication_errors_through_anchor.items()
        }
        _write_csv(stage / "daily_counts.csv", daily, ["observed_date", "model_type", "base_model_raw", "new_version_count", "new_model_count", "active_total", "collected_at"])
        agg_fields = ["period", "model_type", "base_model_group", "new_version_count", "new_model_count", "active_total_last"]
        _write_csv(stage / "weekly_counts.csv", aggregate_observations(daily, groups, "weekly"), agg_fields)
        _write_csv(stage / "monthly_counts.csv", aggregate_observations(daily, groups, "monthly"), agg_fields)
        _write_csv(stage / "publication_daily_counts.csv", _publication_rows(full_metrics), ["publication_date", "model_type", "base_model_group", "published_version_count", "first_known_model_publication_count"])
        _write_csv(stage / "base_model_audit.csv", _audit_rows(conn, groups), ["status", "model_type", "base_model_raw", "base_model_group", "version_count", "present_in_daily_counts"])
        _write_csv(stage / "coverage_daily.csv", ({"date": day, "coverage_status": coverage.get(day, "unknown")} for day in all_dates), ["date", "coverage_status"])
        diagnostics: list[dict[str, object]] = []
        for view in views:
            view_dates = dates_for_view(anchor, measurement_start, view)
            metrics = slice_publications(full_metrics, view_dates[0], view_dates[-1])
            graph_metrics = bucket_publications(metrics, "weekly" if view.bucket == "week" else "daily")
            daily_view_coverage = {day: coverage.get(day, "unknown") for day in metrics.dates}
            graph_coverage = _graph_coverage(daily_view_coverage, metrics.dates, "weekly" if view.bucket == "week" else "daily")
            partial_note = _partial_week_note(measurement_start, anchor) if view.bucket == "week" else None
            observation_counts = _observation_counts(conn, groups, view_dates[0], view_dates[-1])
            svg = render_svg(
                graph_metrics.dates,
                graph_metrics.series,
                graph_coverage,
                view.max_series,
                _label_interval(view, len(graph_metrics.dates)),
                pinned_groups=view.pinned_groups,
                title=(
                    "Civitai publication trends — full measurement period (since observation began)"
                    if view.name == "all"
                    else f"Civitai publication trends — {view.name}"
                ),
                bucket=view.bucket,
                anchor_date=anchor,
                measurement_start=measurement_start,
                generated_at=generated.isoformat(),
                partial_period_note=partial_note,
                color_overrides=view_config.colors,
                show_points=view.name == "14d",
                pinned_states=_pinned_states(view, observation_counts, missing_counts_through_anchor),
            )
            (stage / f"trends-{view.name}.svg").write_text(svg, encoding="utf-8", newline="\n")
            diagnostics.extend(_diagnostics(view, metrics, observation_counts, missing_counts_through_anchor, publication_errors_through_anchor))
            view_metadata[view.name] = _view_manifest(view, metrics, daily_view_coverage)
        shutil.copyfile(stage / "trends-all.svg", stage / "trends.svg")
        _write_csv(stage / "series_diagnostics.csv", diagnostics, ["window", "model_type", "metric", "base_model_group", "observation_count", "publication_count", "missing_timestamp_count_through_anchor", "missing_version_ids_through_anchor", "missing_raw_values_through_anchor", "rank", "visible", "reason"])
        conn.commit()
    except Exception:
        conn.rollback()
        shutil.rmtree(stage, ignore_errors=True)
        raise
    finally:
        conn.close()
    if _snapshot_id(db_path) != snapshot_id:
        shutil.rmtree(stage, ignore_errors=True)
        raise RuntimeError("database changed while exports were being built")
    try:
        manifest: dict[str, object] = {
            "snapshot_id": snapshot_id,
            "anchor_date": anchor,
            "measurement_start": measurement_start,
            "generated_at": generated.isoformat(),
            "timezone": view_config.timezone_name,
            "grouping_hash": grouping_hash(groups_path),
            "view_config_hash": _sha256(views_path),
            "views": view_metadata,
            "files": {name: _sha256(stage / name) for name in REQUIRED_EXPORTS},
        }
        (stage / "export_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8", newline="\n",
        )
        _install_stage(stage, output_dir)
        return manifest
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise
