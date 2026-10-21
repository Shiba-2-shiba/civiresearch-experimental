"""Validated snapshot restore and conditional, immutable export publication.

The Hub client is injected so local validation and tests require no SDK/token.
Only the sync CLI imports huggingface_hub.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from .storage import distinct_paths

SNAPSHOT_PATH = "snapshots/civitai.sqlite.gz"
REQUIRED_EXPORTS = (
    "daily_counts.csv", "weekly_counts.csv", "monthly_counts.csv",
    "base_model_audit.csv", "publication_daily_counts.csv", "coverage_daily.csv",
    "series_diagnostics.csv", "trends-all.svg", "trends-90d.svg",
    "trends-30d.svg", "trends-14d.svg", "trends.svg",
)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_snapshot(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("SQLite snapshot is missing or empty")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        if conn.execute("pragma integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite integrity check failed")
        required = {
            "models": "model_id", "model_versions": "version_id",
            "daily_counts": "observed_date", "collection_runs": "run_id",
        }
        for table, column in required.items():
            columns = {row[1] for row in conn.execute(f"pragma table_info({table})")}
            if column not in columns:
                raise ValueError(f"SQLite snapshot is missing {table}.{column}")


def restore_snapshot(api, downloader, repo_id: str, token: str, db_path: Path,
                     revision_path: Path, *, bootstrap: bool = False) -> dict:
    """Freeze the remote revision before download; never initialize on failure."""
    distinct_paths({"database": db_path, "restore receipt": revision_path})
    if not token:
        raise ValueError("HF_TOKEN is required for dataset synchronization")
    revision = api.repo_info(repo_id=repo_id, repo_type="dataset", revision="main", token=token).sha
    files = api.list_repo_files(repo_id=repo_id, repo_type="dataset", revision=revision, token=token)
    receipt = {"repo_id": repo_id, "revision": revision, "bootstrap": False}
    if SNAPSHOT_PATH not in files:
        historical = any(name.startswith(("exports/", "daily_counts/", "snapshots/"))
                         or name.endswith((".sqlite", ".csv")) for name in files)
        if not bootstrap or historical or db_path.exists():
            raise ValueError("Snapshot missing; bootstrap requires an explicitly empty history")
        receipt["bootstrap"] = True
        write_json(revision_path, receipt)
        return receipt
    if bootstrap:
        raise ValueError("Bootstrap cannot replace an existing dataset snapshot")
    archive = Path(downloader(repo_id=repo_id, filename=SNAPSHOT_PATH, repo_type="dataset",
                              revision=revision, token=token))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=db_path.parent) as temporary:
        candidate = Path(temporary) / "candidate.sqlite"
        with gzip.open(archive, "rb") as source, candidate.open("wb") as target:
            shutil.copyfileobj(source, target)
        validate_snapshot(candidate)
        receipt["snapshot_id"] = file_hash(candidate)
        if db_path.exists():
            shutil.copy2(db_path, db_path.with_suffix(db_path.suffix + ".before-restore"))
        os.replace(candidate, db_path)
    write_json(revision_path, receipt)
    return receipt


def validate_exports(exports_dir: Path, db_path: Path, manifest_bytes: bytes | None = None) -> dict:
    validate_snapshot(db_path)
    manifest = json.loads(manifest_bytes if manifest_bytes is not None else
                          (exports_dir / "export_manifest.json").read_bytes())
    if manifest.get("snapshot_id") != file_hash(db_path):
        raise ValueError("Export snapshot does not match the database to publish")
    hashes = manifest.get("files", {})
    if not isinstance(hashes, dict) or not set(REQUIRED_EXPORTS).issubset(hashes):
        raise ValueError("Export manifest is missing required outputs")
    for name, expected in hashes.items():
        if Path(name).name != name or name in (".", "..") or "\\" in name:
            raise ValueError("Manifest paths must be filenames inside the export directory")
        path = exports_dir / name
        if not path.is_file() or file_hash(path) != expected:
            raise ValueError(f"Invalid or changed export: {name}")
    return manifest


def dataset_readme() -> str:
    return """# Civitai Model Version Trends

Publications among observed model versions, grouped by baseModel (JST).

| View | Resolution | SVG |
| --- | --- | --- |
| Full measurement period (since observation began) | Weekly | [Open](exports/trends-all.svg) |
| Last 90 days (about 3 months) | Daily | [Open](exports/trends-90d.svg) |
| Last 30 days (about 1 month) | Daily | [Open](exports/trends-30d.svg) |
| Last 14 days | Daily | [Open](exports/trends-14d.svg) |

![Full measurement period](exports/trends-all.svg)

![Last 14 days](exports/trends-14d.svg)

The anchor is the latest completed two-type collection date in the snapshot.
Observation-date counts in daily_counts/ differ from publication-date counts in
exports/publication_daily_counts.csv. Coverage is conservative: unknown days
are not confirmed zeros. Missing publication dates are excluded from releases.
The model panel shows the earliest publication among observed versions only.

See exports/series_diagnostics.csv, exports/coverage_daily.csv, and
exports/export_manifest.json for selection, quality, batch identity, and hashes.
The SQLite snapshot is in snapshots/civitai.sqlite.gz.
"""


def prepare_dataset(repo_id: str, db_path: Path, exports_dir: Path,
                    revision_path: Path, stage_dir: Path) -> dict:
    """Freeze a full retryable batch without any remote write."""
    manifest_bytes = (exports_dir / "export_manifest.json").read_bytes()
    manifest = validate_exports(exports_dir, db_path, manifest_bytes)
    receipt = json.loads(revision_path.read_text(encoding="utf-8"))
    if receipt.get("repo_id") != repo_id or not receipt.get("revision"):
        raise ValueError("Restore receipt does not belong to this dataset")
    if stage_dir.exists():
        raise ValueError("Upload stage already exists; preserve/retry the immutable batch separately")
    stage_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".upload-", dir=stage_dir.parent) as temporary:
        stage = Path(temporary) / "batch"
        (stage / "exports").mkdir(parents=True)
        (stage / "daily_counts").mkdir()
        (stage / "snapshots").mkdir()
        for name in manifest["files"]:
            shutil.copyfile(exports_dir / name, stage / "exports" / name)
        (stage / "exports/export_manifest.json").write_bytes(manifest_bytes)
        shutil.copyfile(exports_dir / "daily_counts.csv", stage / "daily_counts/daily_counts.csv")
        snapshot_hash = hashlib.sha256()
        with db_path.open("rb") as source, (stage / SNAPSHOT_PATH).open("wb") as target:
            with gzip.GzipFile(filename="", fileobj=target, mode="wb", mtime=0) as compressed:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    snapshot_hash.update(block)
                    compressed.write(block)
        # Verify the bytes again after copying so a changing source is never published.
        if snapshot_hash.hexdigest() != manifest["snapshot_id"] or file_hash(db_path) != manifest["snapshot_id"]:
            raise ValueError("Database changed while preparing publication")
        for name, expected in manifest["files"].items():
            if file_hash(stage / "exports" / name) != expected:
                raise ValueError("Exports changed while preparing publication")
        (stage / "README.md").write_text(dataset_readme(), encoding="utf-8", newline="\n")
        payload_hashes = {path.relative_to(stage).as_posix(): file_hash(path)
                          for path in sorted(stage.rglob("*")) if path.is_file()}
        write_json(stage / ".batch_receipt.json", {
            "repo_id": repo_id, "parent_commit": receipt["revision"],
            "snapshot_id": manifest["snapshot_id"], "anchor_date": manifest["anchor_date"],
            "files": payload_hashes,
        })
        os.replace(stage, stage_dir)
    return {"status": "prepared", "snapshot_id": manifest["snapshot_id"],
              "parent_commit": receipt["revision"], "anchor_date": manifest["anchor_date"]}


def publish_prepared(api, repo_id: str, token: str, stage_dir: Path, *,
                     operation_factory=None, dry_run: bool = False) -> dict:
    """Validate and retry a saved batch, requiring no source DB or collection."""
    if not token and not dry_run:
        raise ValueError("HF_TOKEN is required for publication")
    receipt = json.loads((stage_dir / ".batch_receipt.json").read_text(encoding="utf-8"))
    if receipt.get("repo_id") != repo_id or not receipt.get("parent_commit"):
        raise ValueError("Prepared batch belongs to a different dataset")
    payloads = receipt.get("files", {})
    required = {"README.md", "daily_counts/daily_counts.csv", SNAPSHOT_PATH,
                "exports/export_manifest.json", *("exports/" + name for name in REQUIRED_EXPORTS)}
    if set(payloads) != required:
        raise ValueError("Prepared batch payload set is invalid")
    actual = {path.relative_to(stage_dir).as_posix() for path in stage_dir.rglob("*")
              if path.is_file() and path.name not in {".batch_receipt.json", "publication_receipt.json"}}
    if actual != required:
        raise ValueError("Prepared batch contains missing or unexpected files")
    for name, digest in payloads.items():
        path = stage_dir / name
        if path.is_symlink() or file_hash(path) != digest:
            raise ValueError(f"Prepared batch changed: {name}")
    manifest = json.loads((stage_dir / "exports/export_manifest.json").read_bytes())
    if manifest.get("snapshot_id") != receipt["snapshot_id"] or manifest.get("anchor_date") != receipt["anchor_date"]:
        raise ValueError("Prepared manifest does not match the batch")
    if set(manifest.get("files", {})) != set(REQUIRED_EXPORTS):
        raise ValueError("Prepared manifest is incomplete")
    for name, digest in manifest["files"].items():
        if payloads.get("exports/" + name) != digest:
            raise ValueError("Prepared export hashes disagree with manifest")
    if payloads["daily_counts/daily_counts.csv"] != payloads["exports/daily_counts.csv"]:
        raise ValueError("Observation copies disagree")
    snapshot_hash = hashlib.sha256()
    with gzip.open(stage_dir / SNAPSHOT_PATH, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            snapshot_hash.update(block)
    if snapshot_hash.hexdigest() != receipt["snapshot_id"]:
        raise ValueError("Prepared snapshot does not match the export manifest")
    result = {"status": "dry_run" if dry_run else "published", "snapshot_id": receipt["snapshot_id"],
              "parent_commit": receipt["parent_commit"], "anchor_date": receipt["anchor_date"]}
    if dry_run:
        return result
    # The server performs this comparison atomically. Never replace with a preflight-only check.
    if operation_factory is None:
        raise ValueError("A conditional commit operation factory is required")
    operations = [operation_factory(path_in_repo=name, path_or_fileobj=str(stage_dir / name))
                  for name in sorted(required)]
    commit = api.create_commit(repo_id=repo_id, repo_type="dataset", revision="main",
                               operations=operations, token=token, parent_commit=receipt["parent_commit"],
                               commit_message=f"Update Civitai trends through {manifest['anchor_date']}")
    result["commit"] = commit.oid
    write_json(stage_dir / "publication_receipt.json", result)
    return result


def publish_dataset(api, repo_id: str, token: str, db_path: Path, exports_dir: Path,
                    revision_path: Path, stage_dir: Path, *, dry_run: bool = False,
                    operation_factory=None) -> dict:
    """Prepare and publish a new batch; use publish_prepared for a retry."""
    if not token and not dry_run:
        raise ValueError("HF_TOKEN is required for publication")
    prepare_dataset(repo_id, db_path, exports_dir, revision_path, stage_dir)
    return publish_prepared(api, repo_id, token, stage_dir,
                            operation_factory=operation_factory, dry_run=dry_run)
