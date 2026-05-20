#!/usr/bin/env python3
"""Generate a standalone SVG trend plot from Civitai publication dates."""

from __future__ import annotations

import argparse
import html
import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from export_counts import parse_group_file


PALETTE = [
    "#2563eb",
    "#dc2626",
    "#16a34a",
    "#9333ea",
    "#ea580c",
    "#0891b2",
    "#4f46e5",
    "#be123c",
    "#0f766e",
    "#a16207",
    "#7c2d12",
    "#0369a1",
    "#65a30d",
    "#c026d3",
    "#475569",
    "#db2777",
]


MetricSeries = dict[str, dict[str, dict[str, dict[str, int]]]]


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
    return timezone(sign * timedelta(hours=int(hours_raw), minutes=int(minutes_raw)))


def local_day(value: str, tz: timezone) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(tz).date().isoformat()


def complete_date_range(days: set[str]) -> list[str]:
    if not days:
        return []
    current = date.fromisoformat(min(days))
    end = date.fromisoformat(max(days))
    result = []
    while current <= end:
        result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def date_range(start_date: str, end_date: str) -> list[str]:
    current = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    result = []
    while current <= end:
        result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def observed_date_range(db_path: Path) -> tuple[str | None, str | None]:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("select min(observed_date), max(observed_date) from daily_counts").fetchone()
    return row[0], row[1]


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"pragma table_info({table})")}


def complete_observed_dates(db_path: Path, dates: list[str], tz: timezone) -> set[str]:
    if not dates:
        return set()
    with sqlite3.connect(db_path) as conn:
        columns = table_columns(conn, "collection_runs")
        required = {"coverage_started_at", "coverage_finished_at"}
        if not required.issubset(columns):
            return set()
        rows = conn.execute(
            """
            select coverage_started_at, coverage_finished_at
            from collection_runs
            where status = 'success'
            """
        ).fetchall()

    complete: set[str] = set()
    for day in dates:
        start = datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=tz)
        end = datetime.combine(date.fromisoformat(day), datetime.max.time(), tzinfo=tz)
        start_utc = start.astimezone(timezone.utc)
        end_utc = end.astimezone(timezone.utc).replace(microsecond=0)
        for coverage_started_at, coverage_finished_at in rows:
            if not coverage_started_at or not coverage_finished_at:
                continue
            covered_start = datetime.fromisoformat(coverage_started_at)
            covered_end = datetime.fromisoformat(coverage_finished_at)
            if covered_start <= start_utc and covered_end >= end_utc:
                complete.add(day)
                break
    return complete


def grouped_base_model(raw_value: str | None, group_map: dict[str, str]) -> str:
    raw = raw_value or ""
    return group_map.get(raw, raw or "Unknown")


def read_publication_series(
    db_path: Path,
    group_map: dict[str, str],
    tz: timezone,
    model_type: str | None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[list[str], MetricSeries]:
    observed_start, observed_end = observed_date_range(db_path)
    start = start_date or observed_start
    end = end_date or observed_end
    series: MetricSeries = defaultdict(
        lambda: {
            "new_versions": defaultdict(lambda: defaultdict(int)),
            "new_models": defaultdict(lambda: defaultdict(int)),
        }
    )
    first_model_publication: dict[int, tuple[datetime, str, str]] = {}
    publication_days: set[str] = set()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select version_id, model_id, model_type, raw_json
            from model_versions
            where raw_json is not null
            """
        ).fetchall()

    for row in rows:
        row_model_type = row["model_type"]
        if model_type and row_model_type != model_type:
            continue
        data = json.loads(row["raw_json"])
        published_at = data.get("publishedAt")
        if not published_at:
            continue

        published_dt = datetime.fromisoformat(published_at.replace("Z", "+00:00")).astimezone(tz)
        day = published_dt.date().isoformat()
        arch = grouped_base_model(data.get("baseModel"), group_map)
        publication_days.add(day)
        if (start is None or day >= start) and (end is None or day <= end):
            series[row_model_type]["new_versions"][arch][day] += 1

        model_id = int(row["model_id"])
        current = first_model_publication.get(model_id)
        if current is None or published_dt < current[0]:
            first_model_publication[model_id] = (published_dt, row_model_type, arch)

    for published_dt, row_model_type, arch in first_model_publication.values():
        day = published_dt.date().isoformat()
        if (start is None or day >= start) and (end is None or day <= end):
            series[row_model_type]["new_models"][arch][day] += 1

    if start and end:
        return date_range(start, end), series
    return complete_date_range(publication_days), series


def render_svg(dates: list[str], series: MetricSeries, complete_dates: set[str] | None = None) -> str:
    complete_dates = complete_dates or set()
    incomplete_dates = [day for day in dates if day not in complete_dates]
    width, height = 1260, 1280
    left, right, top, bottom = 90, 350, 86, 78
    gap = 56
    panels = []
    for model_type in [name for name in ("Checkpoint", "LORA") if name in series]:
        panels.append((model_type, "new_versions", "Version releases"))
        panels.append((model_type, "new_models", "Model publications"))
    for model_type in sorted(name for name in series if name not in ("Checkpoint", "LORA")):
        panels.append((model_type, "new_versions", "Version releases"))
        panels.append((model_type, "new_models", "Model publications"))
    panel_count = max(1, len(panels))
    plot_w = width - left - right
    plot_h = (height - top - bottom - gap * (panel_count - 1)) / panel_count

    def x_pos(index: int) -> float:
        if len(dates) <= 1:
            return left + plot_w / 2
        return left + plot_w * index / (len(dates) - 1)

    def y_pos(value: int, panel_top: float, panel_max: int) -> float:
        return panel_top + plot_h - (plot_h * value / panel_max)

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{left}" y="30" font-family="Arial" font-size="20" font-weight="700">Civitai daily publication trends by architecture</text>',
        f'<text x="{left}" y="50" font-family="Arial" font-size="12" fill="#4b5563">Counts use modelVersion.publishedAt converted to the selected local date; architecture uses grouped baseModel.</text>',
    ]
    if incomplete_dates:
        note = "Partial coverage: " + ", ".join(incomplete_dates)
        lines.append(f'<text x="{left}" y="68" font-family="Arial" font-size="12" fill="#b45309">{html.escape(note)}</text>')

    for panel_index, (model_type, metric_key, metric_label) in enumerate(panels):
        panel_top = top + panel_index * (plot_h + gap)
        panel_bottom = panel_top + plot_h
        panel_values = series[model_type][metric_key]
        ranked = sorted(
            panel_values.items(),
            key=lambda item: sum(item[1].values()),
            reverse=True,
        )
        panel_max = max(
            [1]
            + [
                max(values.get(day, 0) for day in dates)
                for _, values in ranked
            ]
        )
        panel_title = f"{model_type} - {metric_label}"
        lines.append(f'<text x="{left}" y="{panel_top - 14:.1f}" font-family="Arial" font-size="15" font-weight="700">{html.escape(panel_title)}</text>')
        lines.append(f'<line x1="{left}" y1="{panel_bottom:.1f}" x2="{left + plot_w}" y2="{panel_bottom:.1f}" stroke="#111827"/>')
        lines.append(f'<line x1="{left}" y1="{panel_top:.1f}" x2="{left}" y2="{panel_bottom:.1f}" stroke="#111827"/>')
        for tick in range(5):
            value = round(panel_max * tick / 4)
            y = y_pos(value, panel_top, panel_max)
            lines.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" stroke="#e5e7eb"/>')
            lines.append(f'<text x="{left - 12}" y="{y + 4:.1f}" text-anchor="end" font-family="Arial" font-size="12">{value}</text>')

        for idx, (arch, values) in enumerate(ranked):
            color = PALETTE[idx % len(PALETTE)]
            points = " ".join(
                f"{x_pos(i):.1f},{y_pos(values.get(day, 0), panel_top, panel_max):.1f}"
                for i, day in enumerate(dates)
            )
            lines.append(f'<polyline fill="none" stroke="{color}" stroke-width="2.4" points="{points}"/>')
            for day_index, day in enumerate(dates):
                x = x_pos(day_index)
                y = y_pos(values.get(day, 0), panel_top, panel_max)
                lines.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/>')

        legend_x = left + plot_w + 30
        for idx, (arch, values) in enumerate(ranked):
            color = PALETTE[idx % len(PALETTE)]
            legend_y = panel_top + 18 + idx * 15
            total = sum(values.get(day, 0) for day in dates)
            lines.append(f'<line x1="{legend_x}" y1="{legend_y:.1f}" x2="{legend_x + 22}" y2="{legend_y:.1f}" stroke="{color}" stroke-width="3"/>')
            lines.append(f'<text x="{legend_x + 32}" y="{legend_y + 4:.1f}" font-family="Arial" font-size="11">{html.escape(arch)} ({total})</text>')

    label_y = height - 34
    for index, day in enumerate(dates):
        x = x_pos(index)
        fill = "#b45309" if day in incomplete_dates else "#111827"
        suffix = " *" if day in incomplete_dates else ""
        lines.append(f'<text x="{x:.1f}" y="{label_y}" text-anchor="middle" font-family="Arial" font-size="12" fill="{fill}">{html.escape(day + suffix)}</text>')
    lines.append("</svg>")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/civitai.sqlite"))
    parser.add_argument("--groups", type=Path, default=Path("config/base_model_groups.yaml"))
    parser.add_argument("--output", type=Path, default=Path("data/exports/trends.svg"))
    parser.add_argument("--model-type", choices=["Checkpoint", "LORA"])
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--timezone-offset", default="+09:00")
    args = parser.parse_args()

    dates, series = read_publication_series(
        args.db,
        parse_group_file(args.groups),
        parse_timezone_offset(args.timezone_offset),
        args.model_type,
        args.start_date,
        args.end_date,
    )
    complete_dates = complete_observed_dates(
        args.db,
        dates,
        parse_timezone_offset(args.timezone_offset),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_svg(dates, series, complete_dates), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
