#!/usr/bin/env python3
"""Generate a small standalone SVG trend plot from exported counts."""

from __future__ import annotations

import argparse
import csv
import html
from collections import defaultdict
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
]


def read_daily(path: Path, group_map: dict[str, str], model_type: str | None) -> tuple[list[str], dict[str, dict[str, int]]]:
    dates: set[str] = set()
    series: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if model_type and row["model_type"] != model_type:
                continue
            day = row["observed_date"]
            group = group_map.get(row["base_model_raw"], row["base_model_raw"] or "Unknown")
            dates.add(day)
            series[group][day] += int(row["new_version_count"] or 0)
    return sorted(dates), series


def render_svg(dates: list[str], series: dict[str, dict[str, int]], limit: int) -> str:
    width, height = 1100, 620
    left, right, top, bottom = 90, 260, 40, 95
    plot_w = width - left - right
    plot_h = height - top - bottom
    ranked = sorted(
        series.items(),
        key=lambda item: sum(item[1].values()),
        reverse=True,
    )[:limit]
    max_y = max([1] + [max(values.values() or [0]) for _, values in ranked])

    def x_pos(index: int) -> float:
        if len(dates) <= 1:
            return left + plot_w / 2
        return left + plot_w * index / (len(dates) - 1)

    def y_pos(value: int) -> float:
        return top + plot_h - (plot_h * value / max_y)

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{left}" y="26" font-family="Arial" font-size="20" font-weight="700">Civitai new model versions by baseModel</text>',
        f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" stroke="#111827"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" stroke="#111827"/>',
    ]
    for tick in range(5):
        value = round(max_y * tick / 4)
        y = y_pos(value)
        lines.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" stroke="#e5e7eb"/>')
        lines.append(f'<text x="{left - 12}" y="{y + 4:.1f}" text-anchor="end" font-family="Arial" font-size="12">{value}</text>')
    for index, day in enumerate(dates):
        if index % max(1, len(dates) // 8) != 0 and index != len(dates) - 1:
            continue
        x = x_pos(index)
        lines.append(f'<text x="{x:.1f}" y="{top + plot_h + 28}" text-anchor="middle" font-family="Arial" font-size="12">{html.escape(day)}</text>')

    for idx, (name, values) in enumerate(ranked):
        color = PALETTE[idx % len(PALETTE)]
        points = " ".join(
            f"{x_pos(i):.1f},{y_pos(values.get(day, 0)):.1f}" for i, day in enumerate(dates)
        )
        lines.append(f'<polyline fill="none" stroke="{color}" stroke-width="2.5" points="{points}"/>')
        legend_y = top + 24 + idx * 24
        lines.append(f'<line x1="{left + plot_w + 32}" y1="{legend_y}" x2="{left + plot_w + 56}" y2="{legend_y}" stroke="{color}" stroke-width="3"/>')
        lines.append(f'<text x="{left + plot_w + 66}" y="{legend_y + 4}" font-family="Arial" font-size="13">{html.escape(name)}</text>')
    lines.append("</svg>")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daily-csv", type=Path, default=Path("data/daily_counts.csv"))
    parser.add_argument("--groups", type=Path, default=Path("config/base_model_groups.yaml"))
    parser.add_argument("--output", type=Path, default=Path("data/exports/trends.svg"))
    parser.add_argument("--model-type", choices=["Checkpoint", "LORA"])
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()

    group_map = parse_group_file(args.groups)
    dates, series = read_daily(args.daily_csv, group_map, args.model_type)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_svg(dates, series, args.limit), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
