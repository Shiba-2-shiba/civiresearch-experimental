from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class FakeHub:
    def __init__(self, snapshot: Path, files=None):
        self.snapshot = snapshot
        self.files = files if files is not None else ["snapshots/civitai.sqlite.gz"]
        self.revision = "restored-parent"
        self.uploads = []
        self.fail_next = False

    def repo_info(self, **kwargs):
        return SimpleNamespace(sha=self.revision)

    def list_repo_files(self, **kwargs):
        return self.files

    def download(self, **kwargs):
        if kwargs["revision"] != "restored-parent":
            raise AssertionError("download was not frozen to restored revision")
        return str(self.snapshot)

    def upload_folder(self, **kwargs):
        raise AssertionError("Folder uploads may split a snapshot across commits")

    def create_commit(self, **kwargs):
        if self.fail_next:
            self.fail_next = False
            raise OSError("temporary network failure")
        if kwargs["parent_commit"] != self.revision:
            raise RuntimeError("parent revision conflict")
        self.uploads.append(kwargs)
        return SimpleNamespace(oid="published-commit")


class DatasetPipelineTests(unittest.TestCase):
    def setUp(self):
        from civitrends import dataset
        self.dataset = dataset
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "source.sqlite"
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            create table models (model_id integer primary key);
            create table model_versions (version_id integer primary key);
            create table daily_counts (observed_date text);
            create table collection_runs (run_id text);
            insert into model_versions values (101);
        """)
        conn.close()
        self.gz = self.root / "source.sqlite.gz"
        self.gz.write_bytes(gzip.compress(self.db.read_bytes(), mtime=0))
        self.hub = FakeHub(self.gz)
        self.restored = self.root / "restored.sqlite"
        self.receipt = self.root / "revision.json"

    def restore(self, **kwargs):
        return self.dataset.restore_snapshot(
            self.hub, self.hub.download, "owner/dataset", "test-token",
            self.restored, self.receipt, **kwargs,
        )

    def test_restore_pins_revision_and_keeps_ids(self):
        self.restore()
        conn = sqlite3.connect(self.restored)
        try:
            self.assertEqual(conn.execute("select version_id from model_versions").fetchall(), [(101,)])
        finally:
            conn.close()
        self.assertEqual(json.loads(self.receipt.read_text())["revision"], "restored-parent")

    def test_restore_receipt_cannot_overwrite_database(self):
        self.restored.write_bytes(b"existing history")
        self.receipt = self.restored
        with self.assertRaises(ValueError):
            self.restore()
        self.assertEqual(self.restored.read_bytes(), b"existing history")

    def test_corrupt_restore_preserves_existing_database(self):
        self.restored.write_bytes(b"previous database must survive")
        self.gz.write_bytes(gzip.compress(b"corrupt sqlite", mtime=0))
        with self.assertRaises((ValueError, sqlite3.DatabaseError)):
            self.restore()
        self.assertEqual(self.restored.read_bytes(), b"previous database must survive")
        self.assertFalse(self.receipt.exists())

    def test_missing_snapshot_requires_explicit_bootstrap(self):
        self.hub.files = ["README.md"]
        with self.assertRaises(ValueError):
            self.restore()
        self.assertFalse(self.restored.exists())
        self.restore(bootstrap=True)
        self.assertEqual(json.loads(self.receipt.read_text())["bootstrap"], True)
        self.assertFalse(self.restored.exists())

    def test_bootstrap_refuses_existing_history(self):
        self.hub.files = ["exports/weekly_counts.csv"]
        with self.assertRaises(ValueError):
            self.restore(bootstrap=True)
        self.assertFalse(self.receipt.exists())

    def prepare_exports(self):
        self.restore()
        exports = self.root / "exports"
        exports.mkdir()
        hashes = {}
        for name in self.dataset.REQUIRED_EXPORTS:
            path = exports / name
            path.write_text("fixture: " + name, encoding="utf-8")
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = {
            "snapshot_id": hashlib.sha256(self.restored.read_bytes()).hexdigest(),
            "anchor_date": "2026-10-20", "generated_at": "2026-10-21T00:00:00+00:00",
            "files": hashes,
        }
        (exports / "export_manifest.json").write_text(json.dumps(manifest))
        return exports

    def publish(self, exports, **kwargs):
        return self.dataset.publish_dataset(
            self.hub, "owner/dataset", "test-token", self.restored,
            exports, self.receipt, self.root / "upload", operation_factory=SimpleNamespace, **kwargs,
        )

    def test_conflicting_writer_does_not_upload_or_mark_published(self):
        exports = self.prepare_exports()
        self.hub.revision = "competing-commit"
        with self.assertRaises(RuntimeError):
            self.publish(exports)
        self.assertEqual(self.hub.uploads, [])
        self.assertFalse((self.root / "upload" / "publication_receipt.json").exists())

    def test_invalid_batch_never_uploads(self):
        exports = self.prepare_exports()
        (exports / "trends-14d.svg").write_text("tampered")
        with self.assertRaises(ValueError):
            self.publish(exports)
        self.assertEqual(self.hub.uploads, [])

    def test_changed_source_db_never_uploads(self):
        exports = self.prepare_exports()
        conn = sqlite3.connect(self.restored)
        conn.execute("insert into model_versions values (102)")
        conn.commit()
        conn.close()
        with self.assertRaises(ValueError):
            self.publish(exports)
        self.assertEqual(self.hub.uploads, [])

    def test_dry_run_builds_batch_without_external_write(self):
        exports = self.prepare_exports()
        result = self.publish(exports, dry_run=True)
        self.assertEqual(self.hub.uploads, [])
        self.assertEqual(result["status"], "dry_run")
        staged = self.root / "upload"
        self.assertTrue((staged / "snapshots/civitai.sqlite.gz").exists())
        self.assertTrue((staged / "exports/export_manifest.json").exists())
        self.assertTrue((staged / "README.md").exists())
        self.assertEqual(gzip.decompress((staged / "snapshots/civitai.sqlite.gz").read_bytes()), self.restored.read_bytes())

    def test_upload_uses_conditional_parent_and_records_revision(self):
        exports = self.prepare_exports()
        result = self.publish(exports)
        self.assertEqual(result["commit"], "published-commit")
        self.assertEqual(len(self.hub.uploads), 1)
        self.assertEqual(self.hub.uploads[0]["parent_commit"], "restored-parent")
        self.assertEqual(self.hub.uploads[0]["revision"], "main")
        paths = {operation.path_in_repo for operation in self.hub.uploads[0]["operations"]}
        self.assertIn("snapshots/civitai.sqlite.gz", paths)
        self.assertIn("exports/trends-14d.svg", paths)
        self.assertTrue((self.root / "upload/publication_receipt.json").exists())

    def test_retry_uses_identical_stage_without_restore_or_collection(self):
        exports = self.prepare_exports()
        self.hub.fail_next = True
        with self.assertRaises(OSError):
            self.publish(exports)
        staged_snapshot = (self.root / "upload/snapshots/civitai.sqlite.gz").read_bytes()
        self.restored.unlink()
        self.receipt.unlink()
        for path in exports.iterdir():
            path.unlink()
        result = self.dataset.publish_prepared(
            self.hub, "owner/dataset", "test-token", self.root / "upload",
            operation_factory=SimpleNamespace,
        )
        self.assertEqual(result["commit"], "published-commit")
        self.assertEqual((self.root / "upload/snapshots/civitai.sqlite.gz").read_bytes(), staged_snapshot)
        self.assertEqual(len(self.hub.uploads), 1)

    def test_staged_manifest_is_the_exact_manifest_validated(self):
        exports = self.prepare_exports()
        original_bytes = (exports / "export_manifest.json").read_bytes()
        validate = self.dataset.validate_exports

        def rewrite_after_validation(*args, **kwargs):
            result = validate(*args, **kwargs)
            (exports / "export_manifest.json").write_text('{"snapshot_id":"wrong"}')
            return result

        with patch.object(self.dataset, "validate_exports", side_effect=rewrite_after_validation):
            self.publish(exports)
        self.assertEqual((self.root / "upload/exports/export_manifest.json").read_bytes(), original_bytes)


if __name__ == "__main__":
    unittest.main()
