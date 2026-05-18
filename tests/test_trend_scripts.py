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


if __name__ == "__main__":
    unittest.main()
