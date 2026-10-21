"""Validated base-model grouping configuration."""

from __future__ import annotations

import hashlib
from pathlib import Path


class GroupConfigError(ValueError):
    """Raised when the grouping file is ambiguous."""


def load_group_map(path: Path) -> dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(path)
    mapping: dict[str, str] = {}
    current_group: str | None = None
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not raw_line.startswith((" ", "\t")) and line.endswith(":"):
            current_group = line[:-1].strip()
            if not current_group:
                raise GroupConfigError(f"empty group name at line {line_number}")
            continue
        if line == "-" or line.startswith("- "):
            if current_group is None:
                raise GroupConfigError(f"raw value without a group at line {line_number}")
            raw_value = "" if line == "-" else line[2:].strip()
            if raw_value in mapping:
                raise GroupConfigError(
                    f"duplicate raw value {raw_value!r} in {mapping[raw_value]!r} and {current_group!r}"
                )
            mapping[raw_value] = current_group
            continue
        raise GroupConfigError(f"unsupported grouping syntax at line {line_number}: {line}")
    return mapping


def grouping_hash(path: Path) -> str:
    load_group_map(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def grouped_name(raw_value: str | None, groups: dict[str, str]) -> str:
    raw = raw_value or ""
    return groups.get(raw, raw or "Unknown")
