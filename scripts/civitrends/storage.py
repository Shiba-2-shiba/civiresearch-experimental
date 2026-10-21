"""SQLite connection, migrations, persistence, and daily observation rebuilds."""

from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .timeutils import JST, local_date


SCHEMA_VERSION = 4


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def distinct_paths(named_paths: dict[str, Path]) -> dict[str, Path]:
    """Reject output/input aliases before opening any of the files."""
    resolved = {name: path.resolve() for name, path in named_paths.items()}
    entries = list(resolved.items())
    for index, (left_name, left) in enumerate(entries):
        for right_name, right in entries[index + 1:]:
            if left == right or (left.exists() and right.exists() and os.path.samefile(left, right)):
                raise ValueError(f"{left_name} and {right_name} resolve to the same file: {left}")
    return resolved


def connect_db(path: Path) -> sqlite3.Connection:
    ensure_parent(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"pragma table_info({table})")}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("select name from sqlite_master where type='table'")}


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _columns(conn, table):
        conn.execute(f"alter table {table} add column {column} {definition}")


def _database_path(conn: sqlite3.Connection) -> Path | None:
    for row in conn.execute("pragma database_list"):
        if row[1] == "main" and row[2]:
            return Path(row[2])
    return None


def _backup_before_migration(conn: sqlite3.Connection, old_version: int) -> Path | None:
    path = _database_path(conn)
    if path is None or not path.exists() or path.stat().st_size == 0:
        return None
    backup = path.with_name(f"{path.name}.pre-migration-v{old_version}.bak")
    if backup.exists():
        _validate_backup(conn, backup)
        return backup
    backup_conn = sqlite3.connect(backup)
    try:
        conn.backup(backup_conn)
    finally:
        backup_conn.close()
    _validate_backup(conn, backup)
    return backup


def _validate_backup(source: sqlite3.Connection, backup: Path) -> None:
    try:
        with backup.open("rb") as handle:
            if handle.read(16) != b"SQLite format 3\x00":
                raise sqlite3.DatabaseError("migration backup is not a SQLite database")
        check = sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True)
        try:
            if check.execute("pragma quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("migration backup failed integrity validation")
            source_tables = _tables(source)
            for table in ("models", "model_versions", "daily_counts"):
                if table in source_tables:
                    source_count = source.execute(f"select count(*) from {table}").fetchone()[0]
                    backup_count = check.execute(f"select count(*) from {table}").fetchone()[0]
                    if source_count != backup_count:
                        raise sqlite3.DatabaseError(
                            f"migration backup row count mismatch for {table}"
                        )
        finally:
            check.close()
    except sqlite3.DatabaseError:
        raise
    except OSError as exc:
        raise sqlite3.DatabaseError(f"migration backup validation failed: {exc}") from exc


def _create_core_tables(conn: sqlite3.Connection) -> None:
    statements = (
        """create table if not exists models (
            model_id integer primary key, first_seen_at text not null,
            first_seen_date text not null, last_seen_at text not null, name text,
            model_type text not null, creator_username text, nsfw integer, mode text,
            initial_base_model_raw text, raw_json text)""",
        """create table if not exists model_versions (
            version_id integer primary key, model_id integer not null,
            first_seen_at text not null, first_seen_date text not null,
            last_seen_at text not null, created_at text, published_at text,
            version_name text, model_type text not null, base_model_raw text,
            raw_json text, foreign key(model_id) references models(model_id))""",
        """create table if not exists daily_counts (
            observed_date text not null, model_type text not null,
            base_model_raw text not null, new_version_count integer not null,
            new_model_count integer not null, active_total integer,
            collected_at text not null,
            primary key(observed_date, model_type, base_model_raw))""",
        """create table if not exists collection_runs (
            run_id text primary key, started_at text not null, finished_at text,
            observed_date text not null, coverage_started_at text,
            coverage_finished_at text, status text not null, execution_status text,
            known_version_stop integer, pages_fetched integer default 0,
            models_seen integer default 0, versions_seen integer default 0,
            error_message text)""",
        """create table if not exists collection_errors (
            id integer primary key autoincrement, run_id text,
            observed_at text not null, observed_date text not null,
            scope text not null, url text, http_status integer, message text not null,
            foreign key(run_id) references collection_runs(run_id))""",
        """create table if not exists collection_run_types (
            run_id text not null, model_type text not null, query_json text not null,
            query_kind text not null default 'normal',
            stop_reason text not null, pages_fetched integer not null,
            models_seen integer not null, versions_seen integer not null,
            new_models integer not null, new_versions integer not null,
            error_count integer not null, requested_started_at text,
            requested_finished_at text, fetch_finished_at text not null,
            coverage_status text not null,
            primary key(run_id, model_type, query_json),
            foreign key(run_id) references collection_runs(run_id))""",
        """create table if not exists active_total_history (
            observed_at text not null, model_type text not null,
            base_model_raw text not null, active_total integer not null,
            source_url text, run_id text,
            primary key(observed_at, model_type, base_model_raw))""",
        """create table if not exists migration_audit (
            schema_version integer primary key, applied_at text not null,
            inferred_first_seen_dates integer not null default 0)""",
    )
    for statement in statements:
        conn.execute(statement)


def _add_legacy_columns(conn: sqlite3.Connection) -> None:
    ensure_column(conn, "models", "first_seen_date", "text")
    ensure_column(conn, "models", "initial_base_model_raw", "text")
    ensure_column(conn, "model_versions", "first_seen_date", "text")
    ensure_column(conn, "model_versions", "published_at", "text")
    for column, definition in (
        ("coverage_started_at", "text"),
        ("coverage_finished_at", "text"),
        ("execution_status", "text"),
        ("known_version_stop", "integer"),
        ("pages_fetched", "integer default 0"),
        ("models_seen", "integer default 0"),
        ("versions_seen", "integer default 0"),
        ("error_message", "text"),
    ):
        ensure_column(conn, "collection_runs", column, definition)
    ensure_column(conn, "collection_errors", "run_id", "text")
    ensure_column(conn, "collection_run_types", "query_kind", "text not null default 'normal'")


def _backfill_first_seen_dates(conn: sqlite3.Connection) -> int:
    inferred = 0
    for table in ("models", "model_versions"):
        rows = conn.execute(
            f"select rowid as migration_rowid, first_seen_at from {table} "
            "where first_seen_date is null or first_seen_date = ''"
        ).fetchall()
        for row in rows:
            run = conn.execute(
                "select observed_date from collection_runs where started_at = ? order by observed_date limit 1",
                (row["first_seen_at"],),
            ).fetchone()
            if run:
                day = str(run["observed_date"])
            else:
                day = local_date(str(row["first_seen_at"]), JST)
                inferred += 1
            conn.execute(
                f"update {table} set first_seen_date = ? where rowid = ?",
                (day, row["migration_rowid"]),
            )
    return inferred


def _backfill_published_at(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "select version_id, raw_json from model_versions where published_at is null and raw_json is not null"
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["raw_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        published_at = payload.get("publishedAt") if isinstance(payload, dict) else None
        if isinstance(published_at, str) and published_at:
            conn.execute(
                "update model_versions set published_at = ? where version_id = ?",
                (published_at, row["version_id"]),
            )


def _backfill_active_total_history(conn: sqlite3.Connection) -> None:
    conn.execute(
        """insert or ignore into active_total_history
           (observed_at, model_type, base_model_raw, active_total, source_url, run_id)
           select collected_at, model_type, base_model_raw, active_total, null, null
           from daily_counts where active_total is not null"""
    )


def init_db(conn: sqlite3.Connection) -> None:
    integrity = conn.execute("pragma quick_check").fetchone()[0]
    if integrity != "ok":
        raise sqlite3.DatabaseError(f"database integrity check failed: {integrity}")
    old_version = int(conn.execute("pragma user_version").fetchone()[0])
    if old_version > SCHEMA_VERSION:
        raise sqlite3.DatabaseError(
            f"database schema version {old_version} is newer than supported {SCHEMA_VERSION}"
        )
    existing = _tables(conn)
    needs_migration = old_version < SCHEMA_VERSION and bool(existing - {"sqlite_sequence"})
    if needs_migration:
        _backup_before_migration(conn, old_version)
    try:
        conn.execute("begin immediate")
        _create_core_tables(conn)
        _add_legacy_columns(conn)
        inferred = _backfill_first_seen_dates(conn)
        _backfill_published_at(conn)
        _backfill_active_total_history(conn)
        conn.execute(
            "create index if not exists idx_model_versions_first_seen on model_versions(first_seen_date, model_type, base_model_raw)"
        )
        conn.execute(
            "create index if not exists idx_model_versions_published on model_versions(published_at, model_type, base_model_raw)"
        )
        conn.execute(
            "create index if not exists idx_models_first_seen on models(first_seen_date, model_type, initial_base_model_raw)"
        )
        conn.execute(f"pragma user_version = {SCHEMA_VERSION}")
        if old_version < SCHEMA_VERSION:
            conn.execute(
                "insert or replace into migration_audit values (?, strftime('%Y-%m-%dT%H:%M:%SZ','now'), ?)",
                (SCHEMA_VERSION, inferred),
            )
        foreign_key_errors = conn.execute("pragma foreign_key_check").fetchall()
        if foreign_key_errors:
            raise sqlite3.IntegrityError(f"foreign key validation failed: {foreign_key_errors!r}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def migrate_database(path: Path) -> Path | None:
    if path.exists():
        with path.open("rb") as handle:
            if handle.read(16) != b"SQLite format 3\x00":
                raise sqlite3.DatabaseError("file is not a SQLite database")
    conn = connect_db(path)
    try:
        old_version = int(conn.execute("pragma user_version").fetchone()[0])
        init_db(conn)
        backup = path.with_name(f"{path.name}.pre-migration-v{old_version}.bak")
        return backup if backup.exists() else None
    finally:
        conn.close()


def raw_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def earliest_base_model(model: dict[str, Any]) -> str:
    versions = model.get("modelVersions") or []
    if not isinstance(versions, list) or not versions:
        return ""
    ordered = sorted(versions, key=lambda item: item.get("createdAt") or "")
    return str(ordered[0].get("baseModel") or "")


def _positive_id(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"invalid {label} id")
    return value


def upsert_model(conn: sqlite3.Connection, model: dict[str, Any], model_type: str,
                 observed_at: str, observed_date: str) -> bool:
    model_id = _positive_id(model.get("id"), "model")
    existing = conn.execute("select 1 from models where model_id = ?", (model_id,)).fetchone()
    creator = model.get("creator") if isinstance(model.get("creator"), dict) else {}
    values = (
        observed_at, model.get("name"), model.get("type") or model_type,
        creator.get("username"),
        int(bool(model.get("nsfw"))) if model.get("nsfw") is not None else None,
        model.get("mode"), earliest_base_model(model), raw_json(model), model_id,
    )
    if existing:
        conn.execute(
            """update models set last_seen_at=?, name=?, model_type=?, creator_username=?,
               nsfw=?, mode=?, initial_base_model_raw=coalesce(initial_base_model_raw, ?),
               raw_json=? where model_id=?""", values,
        )
        return False
    conn.execute(
        """insert into models (model_id, first_seen_at, first_seen_date, last_seen_at,
           name, model_type, creator_username, nsfw, mode, initial_base_model_raw, raw_json)
           values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (model_id, observed_at, observed_date, observed_at, model.get("name"),
         model.get("type") or model_type, creator.get("username"),
         int(bool(model.get("nsfw"))) if model.get("nsfw") is not None else None,
         model.get("mode"), earliest_base_model(model), raw_json(model)),
    )
    return True


def upsert_version(conn: sqlite3.Connection, model_id: int, model_type: str,
                   version: dict[str, Any], observed_at: str, observed_date: str) -> bool:
    model_id = _positive_id(model_id, "model")
    version_id = _positive_id(version.get("id"), "version")
    existing = conn.execute("select 1 from model_versions where version_id=?", (version_id,)).fetchone()
    current = (observed_at, version.get("createdAt"), version.get("publishedAt"),
               version.get("name"), model_type, version.get("baseModel") or "",
               raw_json(version), version_id)
    if existing:
        conn.execute(
            """update model_versions set last_seen_at=?, created_at=?, published_at=?,
               version_name=?, model_type=?, base_model_raw=?, raw_json=? where version_id=?""",
            current,
        )
        return False
    conn.execute(
        """insert into model_versions (version_id, model_id, first_seen_at, first_seen_date,
           last_seen_at, created_at, published_at, version_name, model_type, base_model_raw, raw_json)
           values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (version_id, model_id, observed_at, observed_date, observed_at,
         version.get("createdAt"), version.get("publishedAt"), version.get("name"),
         model_type, version.get("baseModel") or "", raw_json(version)),
    )
    return True


def append_error_csv(path: Path, row: dict[str, Any]) -> None:
    ensure_parent(path)
    fields = ["run_id", "observed_at", "observed_date", "scope", "url", "http_status", "message"]
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if write_header:
            writer.writeheader()
        writer.writerow({key: row.get(key) for key in fields})


def record_error(conn: sqlite3.Connection, errors_csv: Path, observed_at: str,
                 observed_date: str, scope: str, message: str, url: str | None = None,
                 http_status: int | None = None, run_id: str | None = None) -> None:
    conn.execute(
        """insert into collection_errors
           (run_id, observed_at, observed_date, scope, url, http_status, message)
           values (?, ?, ?, ?, ?, ?, ?)""",
        (run_id, observed_at, observed_date, scope, url, http_status, message),
    )
    append_error_csv(errors_csv, {
        "run_id": run_id, "observed_at": observed_at, "observed_date": observed_date,
        "scope": scope, "url": url, "http_status": http_status, "message": message,
    })


def rebuild_daily_counts(conn: sqlite3.Connection, observed_at: str,
                         observed_date: str | None = None) -> None:
    active = {
        (row["observed_date"], row["model_type"], row["base_model_raw"]):
            (row["active_total"], row["collected_at"])
        for row in conn.execute(
            "select observed_date, model_type, base_model_raw, active_total, collected_at from daily_counts"
        )
        if row["active_total"] is not None
    }
    if observed_date is None:
        dates = {row[0] for row in conn.execute(
            "select first_seen_date from model_versions union select first_seen_date from models order by 1"
        ) if row[0]}
        dates.update(key[0] for key in active)
        conn.execute("delete from daily_counts")
    else:
        dates = {observed_date}
        conn.execute("delete from daily_counts where observed_date=?", (observed_date,))
    for day in sorted(dates):
        version_counts = {
            (row[0], row[1]): int(row[2]) for row in conn.execute(
                """select model_type, coalesce(base_model_raw,''), count(*) from model_versions
                   where first_seen_date=? group by model_type, coalesce(base_model_raw,'')""", (day,)
            )
        }
        model_counts = {
            (row[0], row[1]): int(row[2]) for row in conn.execute(
                """select model_type, coalesce(initial_base_model_raw,''), count(*) from models
                   where first_seen_date=? group by model_type, coalesce(initial_base_model_raw,'')""", (day,)
            )
        }
        active_keys = {(key[1], key[2]) for key in active if key[0] == day}
        for model_type, base_model in sorted(set(version_counts) | set(model_counts) | active_keys):
            active_entry = active.get((day, model_type, base_model))
            has_observations = (model_type, base_model) in version_counts or (model_type, base_model) in model_counts
            conn.execute(
                "insert into daily_counts values (?, ?, ?, ?, ?, ?, ?)",
                (day, model_type, base_model, version_counts.get((model_type, base_model), 0),
                 model_counts.get((model_type, base_model), 0),
                 active_entry[0] if active_entry else None,
                 observed_at if has_observations or active_entry is None else active_entry[1]),
            )
    conn.commit()


def export_daily_counts(conn: sqlite3.Connection, path: Path) -> None:
    ensure_parent(path)
    fields = ["observed_date", "model_type", "base_model_raw", "new_version_count",
              "new_model_count", "active_total", "collected_at"]
    rows = conn.execute(
        "select * from daily_counts order by observed_date, model_type, base_model_raw"
    ).fetchall()
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row[field] for field in fields})
