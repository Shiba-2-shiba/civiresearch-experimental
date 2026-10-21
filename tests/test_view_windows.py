from __future__ import annotations

import unittest

import json
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from scripts.civitrends.exports import ViewSpec, _partial_week_note, dates_for_view, load_view_config
from scripts.civitrends.rendering import render_svg, select_visible_groups, stable_color


class ViewWindowTests(unittest.TestCase):
    def test_windows_include_anchor_and_exact_lower_boundary(self) -> None:
        self.assertEqual(dates_for_view("2026-10-20", "2026-01-01", ViewSpec("14d", 14, "day", 8, ("MOSS Image",), 2))[0], "2026-10-07")
        self.assertEqual(dates_for_view("2026-10-20", "2026-01-01", ViewSpec("30d", 30, "day", 8, (), 5))[0], "2026-09-21")
        self.assertEqual(dates_for_view("2026-10-20", "2026-01-01", ViewSpec("90d", 90, "day", 8, (), 7))[0], "2026-07-23")

    def test_all_view_starts_at_measurement_start_and_handles_leap_day(self) -> None:
        dates = dates_for_view("2024-03-01", "2024-02-28", ViewSpec("all", None, "week", 8, (), 7))
        self.assertEqual(dates, ["2024-02-28", "2024-02-29", "2024-03-01"])

    def test_short_view_clamps_to_measurement_start(self) -> None:
        dates = dates_for_view("2026-10-20", "2026-10-19", ViewSpec("14d", 14, "day", 8, (), 2))
        self.assertEqual(dates, ["2026-10-19", "2026-10-20"])

    def test_pinned_ninth_group_consumes_one_top_eight_slot(self) -> None:
        totals = {f"G{i}": 20 - i for i in range(9)}
        totals["MOSS Image"] = 1
        visible = select_visible_groups(totals, 8, ("MOSS Image",))
        self.assertEqual(len(visible), 8)
        self.assertIn("MOSS Image", visible)
        self.assertNotIn("G7", visible)

    def test_multiple_pinned_groups_are_all_retained(self) -> None:
        totals = {f"G{i}": 20 - i for i in range(10)} | {"Pin A": 1, "Pin B": 0}
        visible = select_visible_groups(totals, 8, ("Pin A", "Pin B"))
        self.assertEqual(len(visible), 8)
        self.assertIn("Pin A", visible)
        self.assertIn("Pin B", visible)

    def test_global_view_defaults_and_colors_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "views.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "timezone": "+09:00",
                "max_series": 2,
                "pinned_groups": ["MOSS Image"],
                "colors": {"MOSS Image": "#123456"},
                "views": [
                    {"name": "all", "days": None, "bucket": "week", "label_interval": None},
                    {"name": "90d", "days": 90, "bucket": "day", "label_interval": None},
                    {"name": "30d", "days": 30, "bucket": "day", "label_interval": 5},
                    {"name": "14d", "days": 14, "bucket": "day", "label_interval": 2},
                ],
            }), encoding="utf-8")
            config = load_view_config(path)
        self.assertEqual(config.views[0].max_series, 2)
        self.assertEqual(config.views[0].pinned_groups, ("MOSS Image",))
        self.assertIsNone(config.views[0].label_interval)
        self.assertEqual(config.colors["MOSS Image"], "#123456")

    def test_invalid_view_schema_rejects_unknown_bucket_and_excess_pins(self) -> None:
        base = {
            "schema_version": 1, "timezone": "+09:00", "max_series": 1,
            "pinned_groups": ["A", "B"], "colors": {},
            "views": [
                {"name": "all", "days": None, "bucket": "month", "label_interval": None},
                {"name": "90d", "days": 90, "bucket": "day", "label_interval": None},
                {"name": "30d", "days": 30, "bucket": "day", "label_interval": None},
                {"name": "14d", "days": 14, "bucket": "day", "label_interval": None},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "views.json"
            path.write_text(json.dumps(base), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_view_config(path)

    def test_color_is_stable_across_windows_and_rank_changes(self) -> None:
        self.assertEqual(stable_color("MOSS Image"), stable_color("MOSS Image"))
        self.assertNotEqual(stable_color("MOSS Image"), stable_color("Pony"))
        self.assertRegex(stable_color("Pony"), r"^#[0-9a-f]{6}$")

    def test_missing_pinned_group_is_explained_in_svg(self) -> None:
        svg = render_svg(["2026-10-20"], {"Checkpoint": {"new_versions": {}, "new_models": {}}}, {"2026-10-20": "partial"}, pinned_groups=("MOSS Image",))
        self.assertIn("MOSS Image: no publications in this window", svg)

    def test_unknown_zero_keeps_observed_count_line_continuous(self) -> None:
        dates = ["2026-10-18", "2026-10-19", "2026-10-20"]
        series = {"Checkpoint": {"new_versions": {"MOSS Image": {dates[0]: 2, dates[2]: 4}}, "new_models": {}}}
        svg = render_svg(dates, series, dict.fromkeys(dates, "unknown"))
        root = ET.fromstring(svg)
        curves = root.findall("{http://www.w3.org/2000/svg}polyline")
        self.assertEqual(len(curves), 1)
        self.assertEqual(curves[0].get("points"), "90.0,250.0 500.0,365.0 910.0,135.0")
        self.assertNotIn('data-missing="true"', svg)
        self.assertIn('data-quality="unknown"', svg)
        self.assertIn("0 means no recorded publications, not a verified absence", svg)

    def test_connected_unknown_values_use_solid_lines_without_points_by_default(self) -> None:
        dates = ["2026-10-19", "2026-10-20"]
        series = {"Checkpoint": {"new_versions": {"MOSS Image": dict(zip(dates, [2, 3]))}, "new_models": {}}}
        svg = render_svg(dates, series, dict.fromkeys(dates, "unknown"))
        self.assertIn('<polyline', svg)
        self.assertNotIn('stroke-dasharray=', svg)
        self.assertNotIn('<circle', svg)

    def test_partial_coverage_is_shaded_with_solid_lines_without_points(self) -> None:
        series = {"Checkpoint": {"new_versions": {"A very long architecture group label": {"2026-10-19": 1, "2026-10-20": 2}}, "new_models": {}}}
        svg = render_svg(["2026-10-19", "2026-10-20"], series, {"2026-10-19": "partial", "2026-10-20": "partial"}, show_points=False)
        self.assertIn('data-quality="partial"', svg)
        self.assertIn('<polyline', svg)
        self.assertNotIn('stroke-dasharray=', svg)
        self.assertNotIn('<circle', svg)
        self.assertNotIn('data-coverage="lower-bound"', svg)
        self.assertIn("<tspan", svg)

    def test_single_bucket_stays_visible_without_a_circle(self) -> None:
        series = {"Checkpoint": {"new_versions": {"MOSS Image": {"2026-10-20": 0}}, "new_models": {}}}
        for state in ("verified", "partial", "unknown"):
            with self.subTest(state=state):
                svg = render_svg(["2026-10-20"], series, {"2026-10-20": state})
                self.assertNotIn('<circle', svg)
                self.assertIn('x1="496.0" y1="365.0" x2="504.0" y2="365.0"', svg)

    def test_hidden_summary_is_below_wrapped_legend_rows(self) -> None:
        values = {"2026-10-20": 1}
        groups = {f"A very long architecture group label {index}": values for index in range(9)}
        svg = render_svg(["2026-10-20"], {"Checkpoint": {"new_versions": groups, "new_models": {}}}, {"2026-10-20": "verified"}, max_series_per_panel=8)
        hidden_match = re.search(r'<text x="940" y="([0-9.]+)"[^>]*>1 hidden series', svg)
        legend_ys = [float(value) for value in re.findall(r'<text x="972" y="([0-9.]+)"', svg)]
        self.assertIsNotNone(hidden_match)
        self.assertGreater(float(hidden_match.group(1)), max(legend_ys) + 12)

    def test_optional_points_retain_quality_while_line_crosses_quality_changes(self) -> None:
        dates = [f"2026-10-{day:02d}" for day in range(15, 21)]
        values = {dates[0]: 0, dates[1]: 1, dates[2]: 0, dates[3]: 2, dates[4]: 3}
        statuses = {dates[0]: "verified", dates[1]: "verified", dates[2]: "partial", dates[3]: "partial", dates[4]: "unknown", dates[5]: "unknown"}
        series = {"Checkpoint": {"new_versions": {"MOSS Image": values}, "new_models": {}}}
        svg = render_svg(dates, series, statuses, show_points=True)
        self.assertEqual(svg.count('data-coverage="lower-bound"'), 4)
        self.assertNotIn('data-missing="true"', svg)
        self.assertIn('data-quality="partial"', svg)
        curves = ET.fromstring(svg).findall("{http://www.w3.org/2000/svg}polyline")
        self.assertEqual(len(curves), 1)
        self.assertEqual(len(curves[0].get("points").split()), 6)
        self.assertNotIn('stroke-dasharray=', svg)

    def test_four_panels_render_when_every_publication_timestamp_is_missing(self) -> None:
        svg = render_svg(["2026-10-20"], {}, {"2026-10-20": "partial"}, pinned_groups=("MOSS Image",), pinned_states={(kind, metric, "MOSS Image"): "publication timestamps missing" for kind in ("Checkpoint", "LORA") for metric in ("new_versions", "new_models")})
        self.assertEqual(svg.count("publication timestamps missing"), 4)
        self.assertIn("Checkpoint - Version publications", svg)
        self.assertIn("LORA - Earliest observed version per model", svg)

    def test_pinned_missing_state_is_specific_and_model_panel_is_not_overclaimed(self) -> None:
        svg = render_svg(["2026-10-20"], {"Checkpoint": {"new_versions": {}, "new_models": {}}}, {"2026-10-20": "verified"}, pinned_groups=("MOSS Image",), pinned_states={("Checkpoint", "new_versions", "MOSS Image"): "publication timestamps missing"})
        self.assertIn("MOSS Image: publication timestamps missing", svg)
        self.assertIn("Earliest observed version per model", svg)

    def test_partial_week_note_covers_first_last_and_same_week_edges(self) -> None:
        self.assertIn("2026-W43", _partial_week_note("2026-10-21", "2026-11-01"))
        self.assertIn("2026-W44", _partial_week_note("2026-10-19", "2026-10-31"))
        same_week = _partial_week_note("2026-10-21", "2026-10-23")
        self.assertEqual(same_week.count("2026-W43"), 1)


if __name__ == "__main__":
    unittest.main()
