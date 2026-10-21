#!/usr/bin/env python3
"""Restore a validated dataset snapshot or publish one conditional export batch."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from civitrends.dataset import prepare_dataset, publish_dataset, publish_prepared, restore_snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("restore", "prepare", "publish", "retry"))
    parser.add_argument("--repo", default=os.getenv("HF_DATASET_REPO", "Shiba-2-shiba/civitai-model-version-trends"))
    parser.add_argument("--db", type=Path, default=Path("data/civitai.sqlite"))
    parser.add_argument("--revision-file", type=Path, default=Path(".verify/dataset_revision.json"))
    parser.add_argument("--exports", type=Path, default=Path("data/exports"))
    parser.add_argument("--stage", type=Path, default=Path(".verify/hf-upload"))
    parser.add_argument("--bootstrap", action="store_true", help="Restore only: initialize a dataset with no historical files")
    parser.add_argument("--dry-run", action="store_true", help="Publish only: prepare and validate without an external write")
    args = parser.parse_args(argv)
    token = os.getenv("HF_TOKEN", "")
    if not token and args.action != "prepare" and not (args.action in {"publish", "retry"} and args.dry_run):
        parser.error("HF_TOKEN is required")
    try:
        from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download
    except ImportError:
        parser.error("Install the pinned optional Hub dependency in requirements-sync.txt")
    if args.action == "restore":
        if args.dry_run:
            parser.error("--dry-run is only available for publish")
        result = restore_snapshot(HfApi(), hf_hub_download, args.repo, token, args.db,
                                  args.revision_file, bootstrap=args.bootstrap)
    elif args.action == "prepare":
        if args.bootstrap:
            parser.error("--bootstrap is only available for restore")
        result = prepare_dataset(args.repo, args.db, args.exports, args.revision_file, args.stage)
    elif args.action == "retry":
        if args.bootstrap:
            parser.error("--bootstrap is only available for restore")
        result = publish_prepared(HfApi(), args.repo, token, args.stage,
                                  operation_factory=CommitOperationAdd, dry_run=args.dry_run)
    else:
        if args.bootstrap:
            parser.error("--bootstrap is only available for restore")
        result = publish_dataset(HfApi(), args.repo, token, args.db, args.exports,
                                 args.revision_file, args.stage, dry_run=args.dry_run,
                                 operation_factory=CommitOperationAdd)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
