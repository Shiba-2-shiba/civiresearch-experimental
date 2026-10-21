from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.civitrends.exports import build_exports


class ExportTests(unittest.TestCase):
    def make_db(self, path: Path, anchor: str = "2026-10-20") -> None:
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            create table daily_counts(observed_date text, model_type text, base_model_raw text, new_version_count integer, new_model_count integer, active_total integer, collected_at text);
            create table models(model_id integer, model_type text, initial_base_model_raw text, first_seen_date text);
            create table model_versions(version_id integer, model_id integer, model_type text, base_model_raw text, published_at text, raw_json text, first_seen_date text);
            create table collection_runs(run_id text, observed_date text, execution_status text, started_at text);
            create table collection_run_types(run_id text, model_type text, query_kind text, error_count integer, coverage_status text, stop_reason text, models_seen integer, versions_seen integer);
            """
        )
        conn.execute("insert into daily_counts values (?, 'Checkpoint', 'MOSS Image', 1, 1, null, ?)", (anchor, anchor + "T00:00:00+00:00"))
        conn.execute("insert into model_versions values (1, 1, 'Checkpoint', 'MOSS Image', ?, '{}', ?)", (anchor + "T00:00:00+00:00", anchor))
        conn.execute("insert into model_versions values (2, 2, 'Checkpoint', 'MOSS Image', null, '{}', ?)", (anchor,))
        conn.execute("insert into model_versions values (3, 3, 'LORA', 'MOSS Image Base', null, '{}', '2026-10-01')")
        conn.executemany("insert into models values (?, 'Checkpoint', 'MOSS Image', ?)", [(1, anchor), (2, anchor)])
        conn.execute("insert into models values (3, 'LORA', 'MOSS Image Base', '2026-10-01')")
        conn.execute("insert into collection_runs values ('run-old', '2026-10-01', 'completed', '2026-10-01T00:00:00+00:00')")
        conn.execute("insert into collection_runs values ('run-1', ?, 'completed', ?)", (anchor, anchor + "T00:00:00+00:00"))
        conn.executemany("insert into collection_run_types values ('run-1', ?, 'normal', 0, 'partial', 'page_budget', 1, 1)", [("Checkpoint",), ("LORA",)])
        conn.commit()
        conn.close()

    def test_bundle_is_deterministic_and_contains_required_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "db.sqlite"
            self.make_db(db)
            database_hash_before = hashlib.sha256(db.read_bytes()).hexdigest()
            out1, out2 = root / "out1", root / "out2"
            kwargs = {"db_path": db, "groups_path": Path("config/base_model_groups.yaml"), "views_path": Path("config/trend_views.json"), "generated_at": "2026-10-21T00:00:00+00:00"}
            build_exports(output_dir=out1, **kwargs)
            build_exports(output_dir=out2, **kwargs)
            expected = {"daily_counts.csv", "weekly_counts.csv", "monthly_counts.csv", "publication_daily_counts.csv", "base_model_audit.csv", "series_diagnostics.csv", "coverage_daily.csv", "trends-all.svg", "trends-90d.svg", "trends-30d.svg", "trends-14d.svg", "trends.svg", "export_manifest.json"}
            self.assertEqual({p.name for p in out1.iterdir()}, expected)
            self.assertEqual({p.name: p.read_bytes() for p in out1.iterdir()}, {p.name: p.read_bytes() for p in out2.iterdir()})
            self.assertEqual((out1 / "trends.svg").read_bytes(), (out1 / "trends-all.svg").read_bytes())
            self.assertIn("full measurement period (since observation began)", (out1 / "trends-all.svg").read_text(encoding="utf-8"))
            manifest = json.loads((out1 / "export_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["anchor_date"], "2026-10-20")
            self.assertEqual(manifest["snapshot_id"], database_hash_before)
            self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), database_hash_before)
            self.assertEqual(set(manifest["files"]), expected - {"export_manifest.json"})
            self.assertEqual(set(manifest["views"]), {"all", "90d", "30d", "14d"})
            self.assertEqual(manifest["views"]["all"]["bucket"], "week")
            self.assertIn("selected_series", manifest["views"]["14d"])
            diagnostics = (out1 / "series_diagnostics.csv").read_text(encoding="utf-8")
            self.assertIn("MOSS Image", diagnostics)
            self.assertIn("pinned", diagnostics)
            with (out1 / "series_diagnostics.csv").open(newline="", encoding="utf-8") as handle:
                diagnostic_rows = list(csv.DictReader(handle))
            moss_versions = next(row for row in diagnostic_rows if row["window"] == "14d" and row["model_type"] == "Checkpoint" and row["metric"] == "new_versions" and row["base_model_group"] == "MOSS Image")
            self.assertEqual(moss_versions["observation_count"], "2")
            self.assertEqual(moss_versions["missing_timestamp_count_through_anchor"], "1")
            missing_lora = next(row for row in diagnostic_rows if row["window"] == "14d" and row["model_type"] == "LORA" and row["metric"] == "new_versions" and row["base_model_group"] == "MOSS Image")
            self.assertEqual(missing_lora["reason"], "missing_publication_timestamp")
            self.assertEqual(missing_lora["observation_count"], "0")
            self.assertEqual(missing_lora["missing_version_ids_through_anchor"], "[3]")
            self.assertEqual(missing_lora["missing_raw_values_through_anchor"], '["MOSS Image Base"]')
            self.assertIn("MOSS Image: publication timestamps missing", (out1 / "trends-14d.svg").read_text(encoding="utf-8"))
            for name in ("all", "90d", "30d", "14d"):
                svg = (out1 / f"trends-{name}.svg").read_text(encoding="utf-8")
                self.assertNotIn('<circle', svg, name)
                self.assertNotIn('stroke-dasharray=', svg, name)
                self.assertNotIn('data-missing="true"', svg, name)

    def test_publication_csv_is_same_daily_data_used_by_graphs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db, out = root / "db.sqlite", root / "out"
            self.make_db(db)
            build_exports(db, out, Path("config/base_model_groups.yaml"), Path("config/trend_views.json"), "2026-10-21T00:00:00+00:00")
            with (out / "publication_daily_counts.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["base_model_group"], "MOSS Image")
            self.assertEqual(rows[0]["published_version_count"], "1")
            self.assertEqual(rows[0]["first_known_model_publication_count"], "1")

    def test_export_hashes_survive_git_lf_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db, out = root / "db.sqlite", root / "out"
            self.make_db(db)
            manifest = build_exports(db, out, Path("config/base_model_groups.yaml"), Path("config/trend_views.json"), "2026-10-21T00:00:00+00:00")
            for name, expected in manifest["files"].items():
                git_bytes = (out / name).read_bytes().replace(b"\r\n", b"\n")
                self.assertEqual(hashlib.sha256(git_bytes).hexdigest(), expected, name)

    def test_output_directory_cannot_replace_source_database_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "db.sqlite"
            self.make_db(db)
            before = db.read_bytes()
            with self.assertRaises(ValueError):
                build_exports(db, root, Path("config/base_model_groups.yaml"), Path("config/trend_views.json"), "2026-10-21T00:00:00+00:00")
            self.assertEqual(db.read_bytes(), before)

    def test_unrelated_output_directory_files_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db, out = root / "db.sqlite", root / "out"
            self.make_db(db)
            out.mkdir()
            sentinel = out / "notes.txt"
            sentinel.write_text("unrelated user content")
            with self.assertRaises(ValueError):
                build_exports(db, out, Path("config/base_model_groups.yaml"), Path("config/trend_views.json"), "2026-10-21T00:00:00+00:00")
            self.assertEqual(sentinel.read_text(), "unrelated user content")

    def test_same_anchor_can_be_regenerated_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db, out = root / "db.sqlite", root / "out"
            self.make_db(db)
            args = (db, out, Path("config/base_model_groups.yaml"), Path("config/trend_views.json"), "2026-10-21T00:00:00+00:00")
            build_exports(*args)
            before = {p.name: p.read_bytes() for p in out.iterdir()}
            build_exports(*args)
            self.assertEqual({p.name: p.read_bytes() for p in out.iterdir()}, before)

    def test_backward_anchor_is_rejected_and_existing_bundle_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db, out = root / "db.sqlite", root / "out"
            self.make_db(db)
            args = (db, out, Path("config/base_model_groups.yaml"), Path("config/trend_views.json"), "2026-10-21T00:00:00+00:00")
            build_exports(*args)
            manifest_path = out / "export_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["anchor_date"] = "2026-10-21"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            before = {p.name: p.read_bytes() for p in out.iterdir()}
            with self.assertRaisesRegex(ValueError, "backward"):
                build_exports(*args)
            self.assertEqual({p.name: p.read_bytes() for p in out.iterdir()}, before)

    def test_latest_failed_or_running_collection_blocks_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db, out = root / "db.sqlite", root / "out"
            self.make_db(db)
            conn = sqlite3.connect(db)
            conn.execute("insert into collection_runs values ('run-2', '2026-10-21', 'running', '2026-10-21T00:00:00+00:00')")
            conn.commit()
            conn.close()
            with self.assertRaisesRegex(ValueError, "latest collection"):
                build_exports(db, out, Path("config/base_model_groups.yaml"), Path("config/trend_views.json"), "2026-10-21T00:00:00+00:00")
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
