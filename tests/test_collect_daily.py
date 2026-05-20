from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import collect_daily  # noqa: E402


def model_payload(model_id: int, version_ids: list[int], base_model: str = "Flux.1 D") -> dict:
    return {
        "id": model_id,
        "name": f"model-{model_id}",
        "type": "LORA",
        "creator": {"username": "tester"},
        "modelVersions": [
            {
                "id": version_id,
                "name": f"version-{version_id}",
                "createdAt": "2026-05-18T00:00:00Z",
                "baseModel": base_model,
            }
            for version_id in version_ids
        ],
    }


class CollectDailyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "civitai.sqlite"
        self.errors_csv = self.root / "errors.csv"
        self.conn = collect_daily.connect_db(self.db)
        collect_daily.init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_collect_model_type_records_http_error_without_writing_items(self) -> None:
        error = HTTPError("https://example.test/page1", 503, "Service Unavailable", None, None)

        with patch.object(collect_daily, "fetch_json", side_effect=error):
            pages, models, versions = collect_daily.collect_model_type(
                conn=self.conn,
                errors_csv=self.errors_csv,
                model_type="LORA",
                observed_at="2026-05-18T00:00:00+00:00",
                observed_date="2026-05-18",
                limit=100,
                max_pages=1,
                known_version_stop=200,
                request_delay=0,
                retries=0,
                retry_delay=0,
                extra_params={},
            )

        self.assertEqual((pages, models, versions), (0, 0, 0))
        row = self.conn.execute("select * from collection_errors").fetchone()
        self.assertEqual(row["scope"], "models:LORA")
        self.assertEqual(row["http_status"], 503)
        with self.errors_csv.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["http_status"], "503")

    def test_collect_model_type_follows_next_page_metadata(self) -> None:
        responses = [
            collect_daily.FetchResult(
                url="https://civitai.test/page1",
                data={
                    "items": [model_payload(1, [101, 102])],
                    "metadata": {"nextPage": "https://civitai.test/page2"},
                },
            ),
            collect_daily.FetchResult(
                url="https://civitai.test/page2",
                data={"items": [model_payload(2, [201])], "metadata": {}},
            ),
        ]

        with patch.object(collect_daily, "fetch_json", side_effect=responses) as fetch:
            pages, models, versions = collect_daily.collect_model_type(
                conn=self.conn,
                errors_csv=self.errors_csv,
                model_type="LORA",
                observed_at="2026-05-18T00:00:00+00:00",
                observed_date="2026-05-18",
                limit=100,
                max_pages=5,
                known_version_stop=200,
                request_delay=0,
                retries=0,
                retry_delay=0,
                extra_params={},
            )

        self.assertEqual((pages, models, versions), (2, 2, 3))
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(self.conn.execute("select count(*) from models").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("select count(*) from model_versions").fetchone()[0], 3)

    def test_collect_model_type_stops_after_consecutive_known_versions(self) -> None:
        observed_at = "2026-05-18T00:00:00+00:00"
        observed_date = "2026-05-18"
        known_model = model_payload(1, [101, 102])
        collect_daily.upsert_model(self.conn, known_model, "LORA", observed_at, observed_date)
        for version in known_model["modelVersions"]:
            collect_daily.upsert_version(self.conn, 1, "LORA", version, observed_at, observed_date)
        self.conn.commit()

        responses = [
            collect_daily.FetchResult(
                url="https://civitai.test/page1",
                data={
                    "items": [known_model],
                    "metadata": {"nextPage": "https://civitai.test/page2"},
                },
            ),
            collect_daily.FetchResult(
                url="https://civitai.test/page2",
                data={"items": [model_payload(2, [201])], "metadata": {}},
            ),
        ]

        with patch.object(collect_daily, "fetch_json", side_effect=responses) as fetch:
            pages, models, versions = collect_daily.collect_model_type(
                conn=self.conn,
                errors_csv=self.errors_csv,
                model_type="LORA",
                observed_at="2026-05-19T00:00:00+00:00",
                observed_date="2026-05-19",
                limit=100,
                max_pages=5,
                known_version_stop=2,
                request_delay=0,
                retries=0,
                retry_delay=0,
                extra_params={},
            )

        self.assertEqual((pages, models, versions), (1, 1, 2))
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(self.conn.execute("select count(*) from model_versions").fetchone()[0], 2)

    def test_rebuild_daily_counts_uses_first_seen_date_and_model_initial_base_model(self) -> None:
        observed_at = "2026-05-18T00:00:00+00:00"
        observed_date = "2026-05-18"
        model = model_payload(1, [101, 102], base_model="Illustrious")
        collect_daily.upsert_model(self.conn, model, "LORA", observed_at, observed_date)
        for version in model["modelVersions"]:
            collect_daily.upsert_version(self.conn, 1, "LORA", version, observed_at, observed_date)
        self.conn.commit()

        collect_daily.rebuild_daily_counts(
            conn=self.conn,
            errors_csv=self.errors_csv,
            observed_at=observed_at,
            observed_date=observed_date,
            include_active_totals=False,
            retries=0,
            retry_delay=0,
            request_delay=0,
            extra_params={},
        )

        row = self.conn.execute("select * from daily_counts").fetchone()
        self.assertEqual(row["observed_date"], observed_date)
        self.assertEqual(row["model_type"], "LORA")
        self.assertEqual(row["base_model_raw"], "Illustrious")
        self.assertEqual(row["new_version_count"], 2)
        self.assertEqual(row["new_model_count"], 1)
        self.assertIsNone(row["active_total"])

    def test_collection_runs_schema_tracks_coverage_and_stop_threshold(self) -> None:
        columns = {
            row["name"]
            for row in self.conn.execute("pragma table_info(collection_runs)").fetchall()
        }

        self.assertIn("coverage_started_at", columns)
        self.assertIn("coverage_finished_at", columns)
        self.assertIn("known_version_stop", columns)
        self.assertEqual(
            collect_daily.coverage_bounds(
                "2026-05-20",
                "2026-05-20",
                collect_daily.parse_timezone_offset("+09:00"),
            ),
            ("2026-05-19T15:00:00+00:00", "2026-05-20T14:59:59+00:00"),
        )


if __name__ == "__main__":
    unittest.main()
