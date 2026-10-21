from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.civitrends.grouping import GroupConfigError, load_group_map


class GroupingTests(unittest.TestCase):
    def test_moss_spellings_map_to_one_group(self) -> None:
        groups = load_group_map(Path("config/base_model_groups.yaml"))
        self.assertEqual(groups["MOSS Image"], "MOSS Image")
        self.assertEqual(groups["MOSS Image Base"], "MOSS Image")

    def test_duplicate_raw_value_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "groups.yaml"
            path.write_text("One:\n  - Same\nTwo:\n  - Same\n", encoding="utf-8")
            with self.assertRaisesRegex(GroupConfigError, "Same"):
                load_group_map(path)

    def test_missing_group_file_is_rejected(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_group_map(Path("does-not-exist.yaml"))


if __name__ == "__main__":
    unittest.main()
