#!/usr/bin/env python3
"""Build one validated export batch from a completed Civitai snapshot."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from civitrends.exports import build_exports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/civitai.sqlite"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/exports"))
    parser.add_argument("--views", type=Path, default=Path("config/trend_views.json"))
    parser.add_argument("--groups", type=Path, default=Path("config/base_model_groups.yaml"))
    parser.add_argument("--generated-at")
    args = parser.parse_args(argv)
    generated_at = args.generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    build_exports(args.db, args.output_dir, args.groups, args.views, generated_at)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
