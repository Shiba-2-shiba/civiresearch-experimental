from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class ScheduledArgumentsTests(unittest.TestCase):
    def arguments(self, env):
        from run_scheduled import workflow_arguments
        return workflow_arguments(env, datetime(2026, 10, 18, 15, 25, tzinfo=timezone.utc))

    def test_weekly_run_keeps_utc_date_and_seven_day_window(self):
        args = self.arguments({"EVENT_SCHEDULE": "25 15 * * 0"})
        self.assertEqual(args[args.index("--observed-date") + 1], "2026-10-18")
        self.assertEqual(args[args.index("--coverage-start-date") + 1], "2026-10-12")
        self.assertEqual(args[args.index("--max-pages") + 1], "80")
        self.assertEqual(args[args.index("--known-version-stop") + 1], "1000")

    def test_daily_run_accepts_explicit_overrides(self):
        args = self.arguments({"OBSERVED_DATE": "2026-10-20", "MAX_PAGES": "3",
                               "KNOWN_VERSION_STOP": "0", "SKIP_ACTIVE_TOTALS": "false"})
        self.assertEqual(args[args.index("--coverage-start-date") + 1], "2026-10-20")
        self.assertEqual(args[args.index("--max-pages") + 1], "3")
        self.assertEqual(args[args.index("--known-version-stop") + 1], "0")
        self.assertNotIn("--skip-active-totals", args)
        self.assertIn("--fail-on-errors", args)

    def test_invalid_window_is_rejected(self):
        with self.assertRaises(ValueError):
            self.arguments({"COVERAGE_START_DATE": "2026-10-20", "COVERAGE_END_DATE": "2026-10-19"})


if __name__ == "__main__":
    unittest.main()
