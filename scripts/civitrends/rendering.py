"""Deterministic SVG rendering for precomputed trend series."""

from __future__ import annotations

import hashlib
import html
import colorsys
from typing import Mapping, Sequence

from .metrics import MetricSeries


PALETTE = ("#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2", "#4f46e5", "#be123c", "#0f766e", "#a16207", "#7c2d12", "#0369a1", "#65a30d", "#c026d3", "#475569", "#db2777")


def stable_color(group: str, overrides: Mapping[str, str] | None = None) -> str:
    if overrides and group in overrides:
        return overrides[group]
    value = int(hashlib.sha256(group.encode("utf-8")).hexdigest()[:12], 16)
    hue = value % 360
    saturation = 62 + (value // 360) % 13
    lightness = 36 + (value // 4680) % 10
    red, green, blue = colorsys.hls_to_rgb(hue / 360, lightness / 100, saturation / 100)
    return f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"


def select_visible_groups(totals: Mapping[str, int], maximum: int, pinned: Sequence[str]) -> list[str]:
    ranked = sorted(totals, key=lambda name: (-totals[name], name))
    if maximum <= 0:
        return ranked
    present_pins = list(dict.fromkeys(group for group in pinned if group in totals))[:maximum]
    selected = present_pins + [group for group in ranked if group not in present_pins]
    selected = selected[:maximum]
    return sorted(selected, key=lambda name: (-totals[name], name))


def _legend_lines(value: str, maximum: int = 26) -> list[str]:
    if len(value) <= maximum:
        return [value]
    words = value.split()
    if len(words) == 1:
        return [value[:maximum], value[maximum: maximum * 2]]
    lines = [""]
    for word in words:
        candidate = f"{lines[-1]} {word}".strip()
        if lines[-1] and len(candidate) > maximum and len(lines) == 1:
            lines.append(word)
        else:
            lines[-1] = candidate
    return lines[:2]


def render_svg(
    dates: list[str],
    series: MetricSeries,
    coverage: Mapping[str, str] | set[str] | None = None,
    max_series_per_panel: int = 8,
    date_label_interval_days: int = 7,
    *,
    pinned_groups: Sequence[str] = (),
    title: str = "Civitai publication trends by architecture",
    bucket: str = "day",
    anchor_date: str | None = None,
    measurement_start: str | None = None,
    generated_at: str | None = None,
    partial_period_note: str | None = None,
    color_overrides: Mapping[str, str] | None = None,
    show_points: bool = False,
    pinned_states: Mapping[tuple[str, str, str], str] | None = None,
) -> str:
    statuses = {day: ("verified" if day in coverage else "unknown") for day in dates} if isinstance(coverage, set) else dict(coverage or {})
    width, left, right, top, gap = 1260, 90, 350, 135, 56
    panels = [
        (kind, metric, label)
        for kind in ("Checkpoint", "LORA")
        for metric, label in (
            ("new_versions", "Version publications"),
            ("new_models", "Earliest observed version per model"),
        )
    ]
    panel_count = max(1, len(panels))
    plot_h, bottom = 230, 70
    height = int(top + panel_count * plot_h + (panel_count - 1) * gap + bottom)
    plot_w = width - left - right

    def x_pos(index: int) -> float:
        return left + plot_w / 2 if len(dates) <= 1 else left + plot_w * index / (len(dates) - 1)

    metadata = " · ".join(
        part for part in (
            f"measurement {measurement_start}" if measurement_start else None,
            f"anchor {anchor_date}" if anchor_date else None,
            f"generated {generated_at}" if generated_at else None,
        ) if part
    )
    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">', '<rect width="100%" height="100%" fill="#ffffff"/>', f'<text x="{left}" y="30" font-family="Arial" font-size="20" font-weight="700">{html.escape(title)}</text>', f'<text x="{left}" y="51" font-family="Arial" font-size="12" fill="#4b5563">Counts use modelVersion.publishedAt; {html.escape(bucket)} buckets; pinned groups consume the series limit.</text>', f'<text x="{left}" y="68" font-family="Arial" font-size="11" fill="#4b5563">{html.escape(metadata)}</text>']
    unknown = sum(1 for day in dates if statuses.get(day, "unknown") == "unknown")
    partial = sum(1 for day in dates if statuses.get(day) == "partial")
    lines.append(f'<text x="{left}" y="86" font-family="Arial" font-size="12" fill="#b45309">Coverage: {partial} partial (yellow), {unknown} unknown (gray); unverified counts are lower bounds. 0 means no recorded publications, not a verified absence.</text>')
    if partial_period_note:
        lines.append(f'<text x="{left}" y="103" font-family="Arial" font-size="11" fill="#b45309">{html.escape(partial_period_note)}</text>')
    for panel_index, (model_type, metric, label) in enumerate(panels):
        panel_top = top + panel_index * (plot_h + gap)
        panel_bottom = panel_top + plot_h
        values_by_group = series.get(model_type, {}).get(metric, {})
        totals = {group: sum(values.get(day, 0) for day in dates) for group, values in values_by_group.items()}
        absent_pinned = [group for group in pinned_groups if totals.get(group, 0) == 0]
        visible = select_visible_groups(totals, max_series_per_panel, pinned_groups)
        hidden = [group for group in totals if group not in visible]
        panel_max = max([1] + [max((values_by_group[group].get(day, 0) for day in dates), default=0) for group in visible])
        lines.append(f'<text x="{left}" y="{panel_top - 14:.1f}" font-family="Arial" font-size="15" font-weight="700">{html.escape(model_type + " - " + label)}</text>')
        band_width = plot_w if len(dates) <= 1 else plot_w / (len(dates) - 1)
        for day_index, day in enumerate(dates):
            state = statuses.get(day, "unknown")
            if state in {"partial", "unknown"}:
                x = max(left, x_pos(day_index) - band_width / 2)
                right_edge = min(left + plot_w, x_pos(day_index) + band_width / 2)
                fill = "#fef3c7" if state == "partial" else "#f3f4f6"
                lines.append(f'<rect data-quality="{state}" x="{x:.1f}" y="{panel_top:.1f}" width="{right_edge - x:.1f}" height="{plot_h}" fill="{fill}" opacity="0.55"/>')
        lines.append(f'<line x1="{left}" y1="{panel_bottom:.1f}" x2="{left + plot_w}" y2="{panel_bottom:.1f}" stroke="#111827"/>')
        for tick in range(5):
            value = round(panel_max * tick / 4)
            y = panel_bottom - plot_h * value / panel_max
            lines.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" stroke="#e5e7eb"/>')
            lines.append(f'<text x="{left - 12}" y="{y + 4:.1f}" text-anchor="end" font-family="Arial" font-size="12">{value}</text>')
        legend_x = left + plot_w + 30
        legend_cursor = panel_top + 18
        for group in visible:
            color = stable_color(group, color_overrides)
            values = values_by_group[group]
            points: list[tuple[float, float, str]] = []
            for day_index, day in enumerate(dates):
                value = values.get(day, 0)
                state = statuses.get(day, "unknown")
                x = x_pos(day_index)
                y = panel_bottom - plot_h * value / panel_max
                # Plot the recorded count even when collection completeness is
                # unverified. Coverage is conveyed by the background and note.
                points.append((x, y, state))
            if len(points) > 1:
                coordinates = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in points)
                lines.append(
                    f'<polyline fill="none" stroke="{color}" stroke-width="2.4" '
                    f'stroke-linejoin="round" stroke-linecap="round" points="{coordinates}"/>'
                )
            elif points and not show_points:
                x, y, _ = points[0]
                lines.append(
                    f'<line x1="{x - 4:.1f}" y1="{y:.1f}" x2="{x + 4:.1f}" y2="{y:.1f}" '
                    f'stroke="{color}" stroke-width="2.4" stroke-linecap="round"/>'
                )
            for x, y, state in points if show_points else ():
                if state in {"unknown", "partial"}:
                    lines.append(
                        f'<circle data-coverage="lower-bound" cx="{x:.1f}" cy="{y:.1f}" '
                        f'r="4" fill="{color}" stroke="#b45309" stroke-width="2" '
                        f'data-quality="{state}"/>'
                    )
                else:
                    lines.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/>')
            legend_y = legend_cursor
            lines.append(f'<line x1="{legend_x}" y1="{legend_y}" x2="{legend_x + 22}" y2="{legend_y}" stroke="{color}" stroke-width="3"/>')
            legend_parts = _legend_lines(f"{group} ({totals[group]})")
            tspans = "".join(f'<tspan x="{legend_x + 32}" dy="{0 if line_index == 0 else 12}">{html.escape(part)}</tspan>' for line_index, part in enumerate(legend_parts))
            lines.append(f'<text x="{legend_x + 32}" y="{legend_y + 4}" font-family="Arial" font-size="11">{tspans}</text>')
            legend_cursor += 16 + 12 * (len(legend_parts) - 1)
        if hidden:
            hidden_total = sum(totals[group] for group in hidden)
            y = legend_cursor
            lines.append(f'<text x="{legend_x}" y="{y + 4}" font-family="Arial" font-size="11" fill="#6b7280">{len(hidden)} hidden series ({hidden_total} total)</text>')
        for offset, group in enumerate(absent_pinned):
            y = panel_top + plot_h - 8 - offset * 15
            reason = (pinned_states or {}).get((model_type, metric, group), "no publications in this window")
            lines.append(f'<text x="{legend_x}" y="{y}" font-family="Arial" font-size="11" fill="#b45309">{html.escape(group)}: {html.escape(reason)}</text>')
    if dates:
        label_y = height - 32
        interval = max(1, date_label_interval_days)
        for index, day in enumerate(dates):
            if index not in (0, len(dates) - 1) and index % interval:
                continue
            if index != len(dates) - 1 and x_pos(len(dates) - 1) - x_pos(index) < 85:
                continue
            state = statuses.get(day, "unknown")
            suffix = " ?" if state == "unknown" else (" ~" if state == "partial" else "")
            lines.append(f'<text x="{x_pos(index):.1f}" y="{label_y}" text-anchor="middle" font-family="Arial" font-size="12">{html.escape(day + suffix)}</text>')
    lines.append("</svg>")
    return "\n".join(lines)
