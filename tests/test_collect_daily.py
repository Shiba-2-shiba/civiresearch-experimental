from __future__ import annotations

import csv
import io
import json
import os
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import collect_daily  # noqa: E402
from civitrends import collector  # noqa: E402


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

    def test_collect_model_type_error_after_observations_has_partial_coverage(self) -> None:
        error = HTTPError("https://civitai.com/api/v1/models?page=2", 503, "busy", None, None)
        responses = [
            collect_daily.FetchResult(
                url="https://civitai.com/api/v1/models?page=1",
                data={
                    "items": [model_payload(1, [101])],
                    "metadata": {"nextPage": "https://civitai.com/api/v1/models?page=2"},
                },
            ),
            error,
        ]
        with patch.object(collect_daily, "fetch_json", side_effect=responses):
            result = collect_daily.collect_model_type(
                conn=self.conn, errors_csv=self.errors_csv, model_type="LORA",
                observed_at="2026-05-18T00:00:00+00:00", observed_date="2026-05-18",
                limit=100, max_pages=5, known_version_stop=0, request_delay=0,
                retries=0, retry_delay=0, extra_params={},
            )

        self.assertEqual(result.stop_reason, "error")
        self.assertEqual(result.coverage_status, "partial")

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

    def test_collect_model_type_carries_next_cursor_in_original_query(self) -> None:
        responses = [
            collect_daily.FetchResult(
                url="https://civitai.com/api/v1/models?limit=100&types=LORA&sort=Newest",
                data={"items": [model_payload(1, [101])], "metadata": {"nextCursor": "opaque"}},
            ),
            collect_daily.FetchResult(
                url="https://civitai.com/api/v1/models?limit=100&types=LORA&sort=Newest&cursor=opaque",
                data={"items": [], "metadata": {}},
            ),
        ]
        requested: list[str] = []

        def fetch(url: str, retries: int, delay_seconds: float):
            del retries, delay_seconds
            requested.append(url)
            return responses.pop(0)

        with patch.object(collect_daily, "fetch_json", side_effect=fetch):
            result = collect_daily.collect_model_type(
                conn=self.conn, errors_csv=self.errors_csv, model_type="LORA",
                observed_at="2026-05-18T00:00:00+00:00", observed_date="2026-05-18",
                limit=100, max_pages=5, known_version_stop=0, request_delay=0,
                retries=0, retry_delay=0, extra_params={},
            )

        self.assertEqual(result.stop_reason, "exhausted")
        self.assertIn("types=LORA", requested[1])
        self.assertIn("cursor=opaque", requested[1])

    def test_next_cursor_takes_precedence_over_unsafe_numeric_next_page(self) -> None:
        responses = [
            collect_daily.FetchResult(
                url="https://civitai.com/api/v1/models?limit=100&types=LORA&sort=Newest",
                data={
                    "items": [model_payload(1, [101])],
                    "metadata": {"nextCursor": "opaque", "nextPage": 2},
                },
            ),
            collect_daily.FetchResult(
                url="https://civitai.com/api/v1/models?limit=100&types=LORA&sort=Newest&cursor=opaque",
                data={"items": [], "metadata": {}},
            ),
        ]
        requested: list[str] = []

        def fetch(url: str, retries: int, delay_seconds: float):
            del retries, delay_seconds
            requested.append(url)
            return responses.pop(0)

        with patch.object(collect_daily, "fetch_json", side_effect=fetch):
            result = collect_daily.collect_model_type(
                conn=self.conn, errors_csv=self.errors_csv, model_type="LORA",
                observed_at="2026-05-18T00:00:00+00:00", observed_date="2026-05-18",
                limit=100, max_pages=5, known_version_stop=0, request_delay=0,
                retries=0, retry_delay=0, extra_params={},
            )

        self.assertEqual(result.stop_reason, "exhausted")
        self.assertIn("cursor=opaque", requested[1])

    def test_collect_model_type_rejects_non_finite_delays(self) -> None:
        for value in (float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "finite"):
                    collect_daily.collect_model_type(
                        conn=self.conn, errors_csv=self.errors_csv, model_type="LORA",
                        observed_at="2026-05-18T00:00:00+00:00", observed_date="2026-05-18",
                        limit=100, max_pages=1, known_version_stop=0,
                        request_delay=value, retries=0, retry_delay=0, extra_params={},
                    )

    def test_collect_model_type_rejects_limit_above_api_contract(self) -> None:
        with self.assertRaisesRegex(ValueError, "limit"):
            collect_daily.collect_model_type(
                conn=self.conn, errors_csv=self.errors_csv, model_type="LORA",
                observed_at="2026-05-18T00:00:00+00:00", observed_date="2026-05-18",
                limit=101, max_pages=1, known_version_stop=0, request_delay=0,
                retries=0, retry_delay=0, extra_params={},
            )

    def test_collect_model_type_rejects_reserved_query_overrides(self) -> None:
        with self.assertRaisesRegex(ValueError, "reserved"):
            collect_daily.collect_model_type(
                conn=self.conn, errors_csv=self.errors_csv, model_type="LORA",
                observed_at="2026-05-18T00:00:00+00:00", observed_date="2026-05-18",
                limit=100, max_pages=1, known_version_stop=0, request_delay=0,
                retries=0, retry_delay=0, extra_params={"types": "Checkpoint"},
            )

    def test_page_fallback_stops_partial_before_api_thousand_item_boundary(self) -> None:
        response = collect_daily.FetchResult(
            url="https://civitai.com/api/v1/models?limit=100&types=LORA&page=10",
            data={
                "items": [model_payload(1, [101])],
                "metadata": {"currentPage": 10, "totalPages": 20},
            },
        )
        with patch.object(collect_daily, "fetch_json", return_value=response) as fetch:
            result = collect_daily.collect_model_type(
                conn=self.conn, errors_csv=self.errors_csv, model_type="LORA",
                observed_at="2026-05-18T00:00:00+00:00", observed_date="2026-05-18",
                limit=100, max_pages=20, known_version_stop=0, request_delay=0,
                retries=0, retry_delay=0, extra_params={"page": "10"},
            )

        self.assertEqual(result.stop_reason, "page_budget")
        self.assertEqual(fetch.call_count, 1)

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

    def test_recollection_preserves_first_seen_and_does_not_duplicate_ids(self) -> None:
        original = model_payload(1, [101])
        collect_daily.upsert_model(
            self.conn, original, "LORA", "2026-05-18T00:00:00+00:00", "2026-05-18"
        )
        collect_daily.upsert_version(
            self.conn,
            1,
            "LORA",
            original["modelVersions"][0],
            "2026-05-18T00:00:00+00:00",
            "2026-05-18",
        )
        updated = model_payload(1, [101])
        updated["name"] = "renamed"
        updated["modelVersions"][0]["name"] = "renamed-version"
        collect_daily.upsert_model(
            self.conn, updated, "LORA", "2026-05-20T00:00:00+00:00", "2026-05-20"
        )
        collect_daily.upsert_version(
            self.conn,
            1,
            "LORA",
            updated["modelVersions"][0],
            "2026-05-20T00:00:00+00:00",
            "2026-05-20",
        )

        model = self.conn.execute("select * from models").fetchone()
        version = self.conn.execute("select * from model_versions").fetchone()
        self.assertEqual((model["first_seen_at"], model["first_seen_date"]), (
            "2026-05-18T00:00:00+00:00", "2026-05-18"
        ))
        self.assertEqual((version["first_seen_at"], version["first_seen_date"]), (
            "2026-05-18T00:00:00+00:00", "2026-05-18"
        ))
        self.assertEqual(self.conn.execute("select count(*) from models").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("select count(*) from model_versions").fetchone()[0], 1)

    def test_collector_moss_fixture_preserves_ids_and_timestamp_meanings(self) -> None:
        fixture = json.loads(
            (ROOT / "tests" / "fixtures" / "collector_moss_api_snapshots.json").read_text(
                encoding="utf-8"
            )
        )
        for snapshot in fixture["snapshots"]:
            for model in snapshot["items"]:
                collect_daily.upsert_model(
                    self.conn, model, model["type"], snapshot["observed_at"],
                    snapshot["observed_date"],
                )
                for version in model["modelVersions"]:
                    collect_daily.upsert_version(
                        self.conn, model["id"], model["type"], version,
                        snapshot["observed_at"], snapshot["observed_date"],
                    )
        self.conn.commit()

        versions = self.conn.execute(
            "select version_id, first_seen_date, published_at, base_model_raw "
            "from model_versions order by version_id"
        ).fetchall()
        self.assertEqual(len(versions), len({row["version_id"] for row in versions}))
        self.assertEqual([row["base_model_raw"] for row in versions], ["M.O.S.S", "MOSS", "MOSS"])
        self.assertEqual(versions[0]["first_seen_date"], "2026-05-18")
        self.assertEqual(versions[0]["published_at"], "2026-05-16T12:00:00Z")
        self.assertEqual(versions[1]["first_seen_date"], "2026-05-20")
        self.assertIsNone(versions[2]["published_at"])

    def test_patchable_facade_fetcher_records_per_type_page_budget_result(self) -> None:
        responses = [
            collect_daily.FetchResult(
                url="https://civitai.com/api/v1/models?types=LORA&page=1",
                data={
                    "items": [model_payload(1, [101])],
                    "metadata": {
                        "nextPage": "https://civitai.com/api/v1/models?types=LORA&page=2"
                    },
                },
            )
        ]
        collect_daily.write_run_start(
            self.conn,
            "run-1",
            "2026-05-18T00:00:00+00:00",
            "2026-05-18",
            "2026-05-17T15:00:00+00:00",
            "2026-05-18T14:59:59+00:00",
            200,
        )

        with patch.object(collect_daily, "fetch_json", side_effect=responses):
            result = collect_daily.collect_model_type(
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
                run_id="run-1",
            )

        self.assertIsInstance(result, collector.CollectionResult)
        self.assertEqual(tuple(result), (1, 1, 1))
        self.assertEqual(result.stop_reason, "page_budget")
        self.assertEqual(result.coverage_status, "partial")
        row = self.conn.execute("select * from collection_run_types").fetchone()
        self.assertEqual((row["run_id"], row["model_type"], row["stop_reason"]), (
            "run-1", "LORA", "page_budget"
        ))
        self.assertEqual(row["query_json"], '{"limit":100,"sort":"Newest","types":"LORA"}')

    def test_required_type_failure_marks_batch_failed(self) -> None:
        collect_daily.write_run_start(
            self.conn,
            "run-2",
            "2026-05-18T00:00:00+00:00",
            "2026-05-18",
            "2026-05-17T15:00:00+00:00",
            "2026-05-18T14:59:59+00:00",
            200,
        )
        for model_type, errors in (("Checkpoint", 0), ("LORA", 1)):
            collector.write_collection_result(
                self.conn,
                collector.CollectionResult(
                    run_id="run-2",
                    model_type=model_type,
                    query={"types": model_type, "sort": "Newest", "limit": 100},
                    stop_reason="error" if errors else "exhausted",
                    pages_fetched=1,
                    models_seen=1,
                    versions_seen=1,
                    new_models=1,
                    new_versions=1,
                    error_count=errors,
                    requested_started_at="2026-05-17T15:00:00+00:00",
                    requested_finished_at="2026-05-18T14:59:59+00:00",
                    fetch_finished_at="2026-05-18T00:01:00+00:00",
                    coverage_status="unknown" if errors else "partial",
                ),
            )

        status = collector.finish_batch(self.conn, "run-2")

        self.assertEqual(status, "failed")
        row = self.conn.execute("select status, execution_status from collection_runs").fetchone()
        self.assertEqual((row["status"], row["execution_status"]), ("partial", "failed"))

    def test_supplemental_results_do_not_satisfy_required_normal_scans(self) -> None:
        collect_daily.write_run_start(
            self.conn, "run-supplement", "2026-05-18T00:00:00+00:00", "2026-05-18",
            "2026-05-17T15:00:00+00:00", "2026-05-18T14:59:59+00:00", 200,
        )
        for model_type in ("Checkpoint", "LORA"):
            collector.write_collection_result(
                self.conn,
                collector.CollectionResult(
                    run_id="run-supplement", model_type=model_type,
                    query={"types": model_type, "baseModels": "MOSS"},
                    stop_reason="exhausted", pages_fetched=1, models_seen=1,
                    versions_seen=1, new_models=1, new_versions=1, error_count=0,
                    requested_started_at=None, requested_finished_at=None,
                    fetch_finished_at="2026-05-18T00:01:00+00:00",
                    coverage_status="partial", query_kind="supplemental",
                ),
            )

        self.assertEqual(collector.finish_batch(self.conn, "run-supplement"), "failed")

    def test_run_scoped_error_prevents_completed_status(self) -> None:
        collect_daily.write_run_start(
            self.conn, "run-error", "2026-05-18T00:00:00+00:00", "2026-05-18",
            "2026-05-17T15:00:00+00:00", "2026-05-18T14:59:59+00:00", 200,
        )
        for model_type in ("Checkpoint", "LORA"):
            collector.write_collection_result(
                self.conn,
                collector.CollectionResult(
                    run_id="run-error", model_type=model_type,
                    query={"types": model_type}, stop_reason="exhausted",
                    pages_fetched=1, models_seen=1, versions_seen=1,
                    new_models=1, new_versions=1, error_count=0,
                    requested_started_at=None, requested_finished_at=None,
                    fetch_finished_at="2026-05-18T00:01:00+00:00", coverage_status="partial",
                ),
            )
        collect_daily.record_error(
            self.conn, self.errors_csv, "2026-05-18T00:02:00+00:00", "2026-05-18",
            "collector", "post-fetch failure", run_id="run-error",
        )
        self.conn.commit()

        self.assertEqual(collector.finish_batch(self.conn, "run-error"), "failed")

    def test_unhandled_batch_exception_preserves_legacy_failed_status(self) -> None:
        collect_daily.write_run_start(
            self.conn, "run-crash", "2026-05-18T00:00:00+00:00", "2026-05-18",
            "2026-05-17T15:00:00+00:00", "2026-05-18T14:59:59+00:00", 200,
        )

        self.assertEqual(collector.finish_batch(self.conn, "run-crash", "boom"), "failed")
        row = self.conn.execute(
            "select status, execution_status, error_message from collection_runs where run_id='run-crash'"
        ).fetchone()
        self.assertEqual(tuple(row), ("failed", "failed", "boom"))

    def test_rebuild_all_dates_preserves_active_total_without_network(self) -> None:
        for observed_date, version_id in (("2026-05-18", 101), ("2026-05-19", 102)):
            model = model_payload(version_id, [version_id], base_model="Illustrious")
            collect_daily.upsert_model(
                self.conn, model, "LORA", f"{observed_date}T00:00:00+00:00", observed_date
            )
            collect_daily.upsert_version(
                self.conn,
                version_id,
                "LORA",
                model["modelVersions"][0],
                f"{observed_date}T00:00:00+00:00",
                observed_date,
            )
        self.conn.execute(
            """insert into daily_counts values (?, ?, ?, ?, ?, ?, ?)""",
            ("2026-05-18", "LORA", "Illustrious", 99, 99, 42, "old"),
        )
        self.conn.commit()

        with patch.object(collect_daily, "fetch_json", side_effect=AssertionError("no HTTP")):
            collect_daily.rebuild_daily_counts(
                conn=self.conn,
                errors_csv=self.errors_csv,
                observed_at="2026-05-20T00:00:00+00:00",
                observed_date=None,
                include_active_totals=True,
                retries=0,
                retry_delay=0,
                request_delay=0,
                extra_params={},
            )

        rows = self.conn.execute(
            "select observed_date, new_version_count, active_total from daily_counts order by observed_date"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [
            ("2026-05-18", 1, 42),
            ("2026-05-19", 1, None),
        ])

    def test_rebuild_all_dates_preserves_active_only_historical_row(self) -> None:
        self.conn.execute(
            "insert into daily_counts values (?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-17", "LORA", "MOSS", 0, 0, 42, "old"),
        )
        self.conn.commit()

        collect_daily.rebuild_daily_counts(
            conn=self.conn, errors_csv=self.errors_csv,
            observed_at="2026-05-20T00:00:00+00:00", observed_date=None,
            include_active_totals=False, retries=0, retry_delay=0,
            request_delay=0, extra_params={},
        )

        row = self.conn.execute("select * from daily_counts").fetchone()
        self.assertEqual(
            (row["observed_date"], row["new_version_count"], row["new_model_count"], row["active_total"]),
            ("2026-05-17", 0, 0, 42),
        )

    def test_active_total_fetch_is_stored_as_separate_snapshot(self) -> None:
        model = model_payload(1, [101], base_model="MOSS")
        collect_daily.upsert_model(
            self.conn, model, "LORA", "2026-05-18T00:00:00+00:00", "2026-05-18"
        )
        collect_daily.upsert_version(
            self.conn, 1, "LORA", model["modelVersions"][0],
            "2026-05-18T00:00:00+00:00", "2026-05-18",
        )
        self.conn.commit()

        with patch.object(
            collect_daily, "fetch_active_total",
            return_value=(42, "https://civitai.com/api/v1/models?types=LORA&baseModels=MOSS"),
        ) as fetch:
            errors = collect_daily.collect_active_total_snapshots(
                self.conn, self.errors_csv, "run-snapshot",
                "2026-05-18T00:00:00+00:00", "2026-05-18", {}, 0, 0, 0,
            )

        self.assertEqual(errors, 0)
        fetch.assert_called_once_with("LORA", "MOSS", {}, 0, 0)
        row = self.conn.execute("select * from active_total_history").fetchone()
        self.assertEqual(
            (row["observed_at"], row["model_type"], row["base_model_raw"], row["active_total"]),
            ("2026-05-18T00:00:00+00:00", "LORA", "MOSS", 42),
        )

    def test_empty_base_model_skips_typewide_inventory_fetch_with_warning(self) -> None:
        model = model_payload(1, [101], base_model="")
        collect_daily.upsert_model(
            self.conn, model, "LORA", "2026-05-18T00:00:00+00:00", "2026-05-18"
        )
        collect_daily.upsert_version(
            self.conn, 1, "LORA", model["modelVersions"][0],
            "2026-05-18T00:00:00+00:00", "2026-05-18",
        )
        self.conn.commit()

        with patch.object(collect_daily, "fetch_active_total") as fetch, warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            errors = collect_daily.collect_active_total_snapshots(
                self.conn, self.errors_csv, "run-empty",
                "2026-05-18T00:00:00+00:00", "2026-05-18", {}, 0, 0, 0,
            )

        self.assertEqual(errors, 0)
        fetch.assert_not_called()
        self.assertRegex(str(caught[0].message), "empty baseModel")
        self.assertEqual(self.conn.execute("select count(*) from active_total_history").fetchone()[0], 0)

    def test_main_rejects_db_output_collision_before_opening_database(self) -> None:
        shared = self.root / "same-file"
        shared.write_bytes(b"preserve me")

        with patch.object(collect_daily, "connect_db") as connect, patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                collect_daily.main([
                    "--db", str(shared), "--daily-csv", str(shared),
                    "--errors-csv", str(self.root / "errors-other.csv"),
                ])

        connect.assert_not_called()
        self.assertEqual(shared.read_bytes(), b"preserve me")

    def test_path_validation_rejects_existing_hardlink_alias(self) -> None:
        db = self.root / "db.sqlite"
        alias = self.root / "alias.csv"
        db.write_bytes(b"data")
        os.link(db, alias)

        with self.assertRaisesRegex(ValueError, "same file"):
            collect_daily.validate_distinct_paths(db, alias, self.errors_csv)


if __name__ == "__main__":
    unittest.main()
