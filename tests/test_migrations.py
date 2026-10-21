from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from civitrends import storage  # noqa: E402


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "legacy.sqlite"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _legacy_db(self) -> None:
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript(
                """
                create table models (
                    model_id integer primary key, first_seen_at text not null,
                    last_seen_at text not null, name text, model_type text not null,
                    creator_username text, nsfw integer, mode text, raw_json text
                );
                create table model_versions (
                    version_id integer primary key, model_id integer not null,
                    first_seen_at text not null, last_seen_at text not null,
                    created_at text, version_name text, model_type text not null,
                    base_model_raw text, raw_json text
                );
                create table daily_counts (
                    observed_date text not null, model_type text not null,
                    base_model_raw text not null, new_version_count integer not null,
                    new_model_count integer not null, active_total integer,
                    collected_at text not null,
                    primary key(observed_date, model_type, base_model_raw)
                );
                create table collection_runs (
                    run_id text primary key, started_at text not null, finished_at text,
                    observed_date text not null, status text not null
                );
                create table collection_errors (
                    id integer primary key autoincrement, observed_at text not null,
                    observed_date text not null, scope text not null, url text,
                    http_status integer, message text not null
                );
                """
            )
            conn.execute(
                "insert into collection_runs values (?, ?, ?, ?, ?)",
                ("old-run", "2026-05-17T18:00:00+00:00", "2026-05-17T18:01:00+00:00", "2026-05-18", "success"),
            )
            conn.execute(
                "insert into models values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (1, "2026-05-17T18:00:00+00:00", "2026-05-17T18:01:00+00:00", "m", "LORA", None, 0, None, "{}"),
            )
            conn.execute(
                "insert into model_versions values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (101, 1, "2026-05-17T18:00:00+00:00", "2026-05-17T18:01:00+00:00", None, "v", "LORA", "MOSS", json.dumps({"id": 101, "publishedAt": "2026-05-16T12:00:00Z"})),
            )
            conn.execute(
                "insert into daily_counts values (?, ?, ?, ?, ?, ?, ?)",
                ("2026-05-18", "LORA", "MOSS", 1, 1, 42, "2026-05-18T00:01:00+00:00"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_legacy_migration_orders_columns_before_indexes_and_backfills(self) -> None:
        self._legacy_db()

        conn = storage.connect_db(self.db)
        try:
            storage.init_db(conn)
            model = conn.execute("select * from models").fetchone()
            version = conn.execute("select * from model_versions").fetchone()
            self.assertEqual(model["first_seen_date"], "2026-05-18")
            self.assertEqual(version["first_seen_date"], "2026-05-18")
            self.assertEqual(version["published_at"], "2026-05-16T12:00:00Z")
            snapshot = conn.execute("select * from active_total_history").fetchone()
            self.assertEqual(
                (snapshot["observed_at"], snapshot["model_type"], snapshot["base_model_raw"], snapshot["active_total"]),
                ("2026-05-18T00:01:00+00:00", "LORA", "MOSS", 42),
            )
            self.assertEqual(conn.execute("pragma user_version").fetchone()[0], storage.SCHEMA_VERSION)
            self.assertEqual(conn.execute("pragma integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()
        self.assertTrue(list(self.db.parent.glob("legacy.sqlite.pre-migration-v*.bak")))

    def test_migration_is_idempotent_and_never_promotes_legacy_success(self) -> None:
        self._legacy_db()
        conn = storage.connect_db(self.db)
        try:
            storage.init_db(conn)
            before = tuple(conn.execute("select count(*) from model_versions").fetchone())
            storage.init_db(conn)
            after = tuple(conn.execute("select count(*) from model_versions").fetchone())
            run = conn.execute("select status, execution_status from collection_runs").fetchone()
        finally:
            conn.close()

        self.assertEqual(before, after)
        self.assertEqual((run["status"], run["execution_status"]), ("success", None))

    def test_corrupt_database_is_rejected_without_overwriting_file(self) -> None:
        original = b"not a sqlite database"
        self.db.write_bytes(original)

        with self.assertRaises(sqlite3.DatabaseError):
            storage.migrate_database(self.db)

        self.assertEqual(self.db.read_bytes(), original)

    def test_failed_migration_rolls_back_schema_and_keeps_backup(self) -> None:
        self._legacy_db()
        conn = storage.connect_db(self.db)
        try:
            with patch.object(storage, "_backfill_published_at", side_effect=RuntimeError("boom")):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    storage.init_db(conn)
            columns = {row[1] for row in conn.execute("pragma table_info(model_versions)")}
            self.assertNotIn("published_at", columns)
            self.assertEqual(conn.execute("pragma user_version").fetchone()[0], 0)
        finally:
            conn.close()
        backups = list(self.db.parent.glob("legacy.sqlite.pre-migration-v0.bak"))
        self.assertEqual(len(backups), 1)

    def test_existing_corrupt_migration_backup_is_rejected(self) -> None:
        self._legacy_db()
        backup = self.db.with_name("legacy.sqlite.pre-migration-v0.bak")
        backup.write_bytes(b"broken")
        conn = storage.connect_db(self.db)
        try:
            with self.assertRaisesRegex(sqlite3.DatabaseError, "backup"):
                storage.init_db(conn)
            self.assertNotIn(
                "published_at", {row[1] for row in conn.execute("pragma table_info(model_versions)")}
            )
        finally:
            conn.close()

    def test_newer_schema_version_is_refused_without_downgrade(self) -> None:
        conn = storage.connect_db(self.db)
        try:
            conn.execute(f"pragma user_version = {storage.SCHEMA_VERSION + 1}")
            with self.assertRaisesRegex(sqlite3.DatabaseError, "newer"):
                storage.init_db(conn)
            self.assertEqual(
                conn.execute("pragma user_version").fetchone()[0], storage.SCHEMA_VERSION + 1
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
