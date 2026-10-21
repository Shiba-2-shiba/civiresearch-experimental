from __future__ import annotations

import sqlite3
import unittest
from datetime import timezone

from scripts.civitrends.metrics import aggregate_observations, period_for, publication_metrics


class MetricsTests(unittest.TestCase):
    def test_grouped_active_total_is_blank_for_multiple_raw_values(self) -> None:
        rows = [
            {"observed_date": "2026-10-20", "model_type": "LORA", "base_model_raw": "MOSS Image", "new_version_count": 2, "new_model_count": 1, "active_total": 5},
            {"observed_date": "2026-10-20", "model_type": "LORA", "base_model_raw": "MOSS Image Base", "new_version_count": 3, "new_model_count": 2, "active_total": 7},
        ]
        result = aggregate_observations(rows, {"MOSS Image": "MOSS Image", "MOSS Image Base": "MOSS Image"}, "daily")
        self.assertEqual(result[0]["new_version_count"], 5)
        self.assertIsNone(result[0]["active_total_last"])

    def test_multi_raw_group_does_not_imply_total_when_only_one_raw_was_observed(self) -> None:
        rows = [{"observed_date": "2026-10-20", "model_type": "LORA", "base_model_raw": "MOSS Image", "new_version_count": 1, "new_model_count": 0, "active_total": 5}]
        result = aggregate_observations(rows, {"MOSS Image": "MOSS Image", "MOSS Image Base": "MOSS Image"}, "daily")
        self.assertIsNone(result[0]["active_total_last"])

    def test_single_raw_group_retains_latest_active_total(self) -> None:
        rows = [
            {"observed_date": "2026-10-19", "model_type": "LORA", "base_model_raw": "Pony", "new_version_count": 1, "new_model_count": 0, "active_total": 4},
            {"observed_date": "2026-10-20", "model_type": "LORA", "base_model_raw": "Pony", "new_version_count": 2, "new_model_count": 1, "active_total": 6},
        ]
        result = aggregate_observations(rows, {"Pony": "Pony"}, "weekly")
        self.assertEqual(result[0]["active_total_last"], 6)

    def test_iso_week_uses_iso_year_across_calendar_boundary(self) -> None:
        self.assertEqual(period_for("2021-01-01", "weekly"), "2020-W53")

    def test_first_model_publication_uses_history_before_window(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("create table model_versions(version_id integer, model_id integer, model_type text, base_model_raw text, published_at text, raw_json text)")
        conn.executemany(
            "insert into model_versions values (?, ?, ?, ?, ?, ?)",
            [
                (1, 10, "Checkpoint", "MOSS Image", "2026-01-01T00:00:00+00:00", "{}"),
                (2, 10, "Checkpoint", "MOSS Image Base", "2026-10-20T00:00:00+00:00", "{}"),
                (3, 11, "Checkpoint", "MOSS Image Base", "2026-10-20T01:00:00+00:00", "{}"),
                (4, 12, "Checkpoint", "MOSS Image", None, "{}"),
            ],
        )
        result = publication_metrics(conn, {"MOSS Image": "MOSS Image", "MOSS Image Base": "MOSS Image"}, timezone.utc, "2026-10-20", "2026-10-20")
        self.assertEqual(result.series["Checkpoint"]["new_versions"]["MOSS Image"]["2026-10-20"], 2)
        self.assertEqual(result.series["Checkpoint"]["new_models"]["MOSS Image"]["2026-10-20"], 1)
        self.assertEqual(result.missing_publication_timestamps["Checkpoint"]["MOSS Image"], 1)
        conn.close()

    def test_invalid_publication_timestamp_is_missing_not_zero(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("create table model_versions(version_id integer, model_id integer, model_type text, base_model_raw text, published_at text, raw_json text)")
        conn.execute("insert into model_versions values (1, 1, 'LORA', 'UnknownRaw', 'not-a-date', '{}')")
        result = publication_metrics(conn, {}, timezone.utc, "2026-10-20", "2026-10-20")
        self.assertEqual(result.missing_publication_timestamps["LORA"]["UnknownRaw"], 1)
        self.assertNotIn("UnknownRaw", result.series["LORA"]["new_versions"])
        conn.close()

    def test_naive_publication_timestamp_is_rejected(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("create table model_versions(version_id integer, model_id integer, model_type text, base_model_raw text, published_at text, raw_json text)")
        conn.execute("insert into model_versions values (1, 1, 'LORA', 'MOSS Image', '2026-10-20T12:00:00', '{}')")
        result = publication_metrics(conn, {}, timezone.utc, "2026-10-20", "2026-10-20")
        self.assertEqual(result.missing_publication_timestamps["LORA"]["MOSS Image"], 1)
        self.assertNotIn("MOSS Image", result.series["LORA"]["new_versions"])
        conn.close()

    def test_migrated_publication_query_streams_without_raw_json(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("create table model_versions(version_id integer, model_id integer, model_type text, base_model_raw text, published_at text, raw_json text)")
        conn.execute("insert into model_versions values (1, 1, 'LORA', 'MOSS Image', '2026-10-20T00:00:00+00:00', '{\"publishedAt\":\"wrong\"}')")
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        publication_metrics(conn, {}, timezone.utc, "2026-10-20", "2026-10-20")
        select = next(statement for statement in statements if "from model_versions" in statement.lower())
        self.assertNotIn("raw_json", select.lower())
        conn.close()


if __name__ == "__main__":
    unittest.main()
