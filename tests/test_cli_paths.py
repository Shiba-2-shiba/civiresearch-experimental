from __future__ import annotations

import contextlib
import importlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class CliPathSafetyTests(unittest.TestCase):
    def test_output_clis_reject_database_and_configuration_aliases(self):
        from civitrends import storage
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for module_name in ("export_counts", "audit_base_models", "plot_trends"):
                for target_name in ("database", "groups"):
                    with self.subTest(command=module_name, target=target_name):
                        db = root / f"{module_name}-{target_name}.sqlite"
                        groups = root / f"{module_name}-{target_name}.yaml"
                        groups.write_text("MOSS Image:\n  - MOSS Image\n", encoding="utf-8")
                        conn = storage.connect_db(db)
                        storage.init_db(conn)
                        conn.close()
                        before_db, before_groups = db.read_bytes(), groups.read_bytes()
                        target = db if target_name == "database" else groups
                        args = ["--db", str(db), "--groups", str(groups)]
                        if module_name == "export_counts":
                            args += ["--daily-csv", str(target), "--weekly-csv", str(root / "weekly.csv"),
                                     "--monthly-csv", str(root / "monthly.csv")]
                        else:
                            args += ["--output", str(target)]
                        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises((ValueError, SystemExit)):
                            importlib.import_module(module_name).main(args)
                        self.assertEqual(db.read_bytes(), before_db)
                        self.assertEqual(groups.read_bytes(), before_groups)


if __name__ == "__main__":
    unittest.main()
