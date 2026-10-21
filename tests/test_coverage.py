from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from civitrends import coverage, storage  # noqa: E402


class CoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = storage.connect_db(Path(self.tmp.name) / "coverage.sqlite")
        storage.init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _run(self, run_id: str, day: str, execution_status: str | None) -> None:
        self.conn.execute(
            """insert into collection_runs
               (run_id, started_at, finished_at, observed_date, status, execution_status)
               values (?, ?, ?, ?, ?, ?)""",
            (run_id, f"{day}T00:00:00+00:00", f"{day}T00:05:00+00:00", day, "success", execution_status),
        )

    def _type(
        self,
        run_id: str,
        model_type: str,
        coverage_status: str,
        versions: int = 1,
        requested_start: str | None = "2026-05-19T15:00:00+00:00",
        requested_end: str | None = "2026-05-20T14:59:59+00:00",
        fetch_finished: str = "2026-05-20T15:00:00+00:00",
    ) -> None:
        self.conn.execute(
            """insert into collection_run_types
               (run_id, model_type, query_json, stop_reason, pages_fetched,
                models_seen, versions_seen, new_models, new_versions, error_count,
                requested_started_at, requested_finished_at, fetch_finished_at, coverage_status)
               values (?, ?, '{}', 'exhausted', 1, 1, ?, 1, 1, 0, ?, ?, ?, ?)""",
            (run_id, model_type, versions, requested_start, requested_end, fetch_finished, coverage_status),
        )

    def test_daily_coverage_is_conservative_and_requires_both_types(self) -> None:
        self._run("legacy", "2026-05-18", None)
        self._run("partial", "2026-05-19", "completed")
        self._type("partial", "Checkpoint", "partial")
        self._type("partial", "LORA", "partial")
        self._run("verified", "2026-05-20", "completed")
        self._type("verified", "Checkpoint", "verified")
        self._type("verified", "LORA", "verified")
        self.conn.commit()

        actual = coverage.daily_coverage(
            self.conn, ["2026-05-18", "2026-05-19", "2026-05-20", "2026-05-21"]
        )

        self.assertEqual(actual, {
            "2026-05-18": "unknown",
            "2026-05-19": "partial",
            "2026-05-20": "verified",
            "2026-05-21": "unknown",
        })

    def test_anchor_requires_completed_both_type_batch_and_measurement_falls_back(self) -> None:
        self._run("complete", "2026-05-19", "completed")
        self._type("complete", "Checkpoint", "partial")
        self._type("complete", "LORA", "partial")
        self._run("newer-failed", "2026-05-20", "failed")
        self._type("newer-failed", "Checkpoint", "partial")
        self._type("newer-failed", "LORA", "unknown", versions=0)
        self.conn.commit()

        self.assertEqual(coverage.anchor_date(self.conn), "2026-05-19")
        self.assertEqual(coverage.measurement_start(self.conn), "2026-05-19")

        self.conn.execute("delete from collection_run_types")
        self.conn.execute("delete from collection_runs")
        self.conn.execute(
            "insert into daily_counts values ('2026-05-17','LORA','MOSS',1,1,null,'x')"
        )
        self.conn.commit()
        self.assertEqual(coverage.measurement_start(self.conn), "2026-05-17")

    def test_anchor_raises_when_no_publishable_batch_exists(self) -> None:
        with self.assertRaisesRegex(ValueError, "completed collection batch"):
            coverage.anchor_date(self.conn)

    def test_verified_label_without_bounds_is_only_partial_when_observed(self) -> None:
        self._run("unbounded", "2026-05-20", "completed")
        self._type("unbounded", "Checkpoint", "verified", requested_start=None, requested_end=None)
        self._type("unbounded", "LORA", "verified", requested_start=None, requested_end=None)
        self.conn.commit()

        self.assertEqual(coverage.daily_coverage(self.conn, ["2026-05-20"]), {
            "2026-05-20": "partial"
        })

    def test_fetch_before_local_day_end_prevents_verified(self) -> None:
        self._run("early", "2026-05-20", "completed")
        for model_type in ("Checkpoint", "LORA"):
            self._type(
                "early", model_type, "verified",
                fetch_finished="2026-05-20T14:55:00+00:00",
            )
        self.conn.commit()

        self.assertEqual(coverage.daily_coverage(self.conn, ["2026-05-20"])["2026-05-20"], "partial")

    def test_gapless_intervals_can_jointly_verify_each_required_type(self) -> None:
        for suffix, start, end in (
            ("a", "2026-05-19T15:00:00+00:00", "2026-05-20T02:59:59+00:00"),
            ("b", "2026-05-20T03:00:00+00:00", "2026-05-20T14:59:59+00:00"),
        ):
            run_id = f"split-{suffix}"
            self._run(run_id, "2026-05-20", "completed")
            for model_type in ("Checkpoint", "LORA"):
                self._type(
                    run_id, model_type, "verified", requested_start=start,
                    requested_end=end, fetch_finished="2026-05-20T15:00:00+00:00",
                )
        self.conn.commit()

        self.assertEqual(coverage.daily_coverage(self.conn, ["2026-05-20"])["2026-05-20"], "verified")


if __name__ == "__main__":
    unittest.main()
