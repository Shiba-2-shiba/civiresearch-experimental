from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from audit_base_models import audit_rows  # noqa: E402
from export_counts import aggregate, parse_group_file  # noqa: E402
from plot_trends import complete_observed_dates, parse_timezone_offset, read_publication_series  # noqa: E402


class TrendScriptTests(unittest.TestCase):
    def test_parse_group_file_supports_project_yaml_subset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "groups.yaml"
            path.write_text(
                """
# comment
Flux:
  - Flux.1 D
  - Flux.1 S

NoobAI:
  - NoobAI

Legacy:
  -
""".lstrip(),
                encoding="utf-8",
            )

            self.assertEqual(
                parse_group_file(path),
                {
                    "Flux.1 D": "Flux",
                    "Flux.1 S": "Flux",
                    "NoobAI": "NoobAI",
                    "": "Legacy",
                },
            )

    def test_aggregate_applies_groups_and_periods(self) -> None:
        rows = [
            {
                "observed_date": "2026-05-18",
                "model_type": "LORA",
                "base_model_raw": "Flux.1 D",
                "new_version_count": 2,
                "new_model_count": 1,
                "active_total": None,
            },
            {
                "observed_date": "2026-05-19",
                "model_type": "LORA",
                "base_model_raw": "Flux.1 S",
                "new_version_count": 3,
                "new_model_count": 0,
                "active_total": 42,
            },
        ]

        result = aggregate(rows, {"Flux.1 D": "Flux", "Flux.1 S": "Flux"}, "weekly")

        self.assertEqual(
            result,
            [
                {
                    "period": "2026-W21",
                    "model_type": "LORA",
                    "base_model_group": "Flux",
                    "new_version_count": 5,
                    "new_model_count": 1,
                    "active_total_last": 42,
                }
            ],
        )

    def test_audit_rows_reports_only_ungrouped_by_default(self) -> None:
        rows = [
            {
                "model_type": "Checkpoint",
                "base_model_raw": "Illustrious",
                "new_version_count": 10,
                "new_model_count": 2,
            },
            {
                "model_type": "Checkpoint",
                "base_model_raw": "NewRaw",
                "new_version_count": 4,
                "new_model_count": 1,
            },
        ]

        result = audit_rows(rows, {"Illustrious": "Illustrious"}, show_all=False)

        self.assertEqual(
            result,
            [
                {
                    "status": "ungrouped",
                    "model_type": "Checkpoint",
                    "base_model_raw": "NewRaw",
                    "base_model_group": "",
                    "new_version_count": 4,
                    "new_model_count": 1,
                }
            ],
        )

    def test_publication_series_uses_version_published_at_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "civitai.sqlite"
            import collect_daily  # noqa: PLC0415

            conn = collect_daily.connect_db(db)
            try:
                collect_daily.init_db(conn)
                observed_at = "2026-05-20T00:00:00+00:00"
                model = {
                    "id": 1,
                    "name": "model-1",
                    "type": "Checkpoint",
                    "creator": {"username": "tester"},
                    "modelVersions": [
                        {
                            "id": 101,
                            "name": "v1",
                            "publishedAt": "2026-05-18T16:30:00.000Z",
                            "baseModel": "Illustrious",
                        },
                        {
                            "id": 102,
                            "name": "v2",
                            "publishedAt": "2026-05-20T02:00:00.000Z",
                            "baseModel": "Illustrious",
                        },
                    ],
                }
                collect_daily.upsert_model(conn, model, "Checkpoint", observed_at, "2026-05-20")
                for version in model["modelVersions"]:
                    collect_daily.upsert_version(conn, 1, "Checkpoint", version, observed_at, "2026-05-20")
                conn.commit()
            finally:
                conn.close()

            dates, series = read_publication_series(
                db,
                {"Illustrious": "Illustrious"},
                parse_timezone_offset("+09:00"),
                "Checkpoint",
            )

        self.assertEqual(dates, ["2026-05-19", "2026-05-20"])
        self.assertEqual(series["Checkpoint"]["new_versions"]["Illustrious"]["2026-05-19"], 1)
        self.assertEqual(series["Checkpoint"]["new_versions"]["Illustrious"]["2026-05-20"], 1)
        self.assertEqual(series["Checkpoint"]["new_models"]["Illustrious"]["2026-05-19"], 1)
        self.assertEqual(series["Checkpoint"]["new_models"]["Illustrious"].get("2026-05-20", 0), 0)

    def test_complete_observed_dates_uses_successful_coverage_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "civitai.sqlite"
            import collect_daily  # noqa: PLC0415

            conn = collect_daily.connect_db(db)
            try:
                collect_daily.init_db(conn)
                conn.execute(
                    """
                    insert into collection_runs (
                        run_id, started_at, finished_at, observed_date,
                        coverage_started_at, coverage_finished_at, status,
                        known_version_stop
                    ) values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "run-1",
                        "2026-05-20T14:55:00+00:00",
                        "2026-05-20T14:56:00+00:00",
                        "2026-05-20",
                        "2026-05-19T15:00:00+00:00",
                        "2026-05-20T14:59:59+00:00",
                        "success",
                        200,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            result = complete_observed_dates(
                db,
                ["2026-05-19", "2026-05-20"],
                parse_timezone_offset("+09:00"),
            )

        self.assertEqual(result, {"2026-05-20"})


if __name__ == "__main__":
    unittest.main()
