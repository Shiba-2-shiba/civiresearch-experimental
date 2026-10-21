"""Civitai HTTP transport, validation, retries, and paging safety."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse
from urllib.request import Request, urlopen


API_BASE = "https://civitai.com/api/v1/models"
USER_AGENT = "civiresearch-civitai-trend-collector/0.1"


@dataclass(frozen=True)
class FetchResult:
    url: str
    data: dict[str, Any]


def build_url(base_url: str, params: dict[str, Any]) -> str:
    clean = {key: value for key, value in params.items() if value is not None and value != ""}
    return f"{base_url}?{urlencode(clean, doseq=True)}"


def validate_page(data: object) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("API response must be an object")
    items = data.get("items")
    metadata = data.get("metadata")
    if not isinstance(items, list):
        raise ValueError("API response did not contain a list at items")
    if not isinstance(metadata, dict):
        raise ValueError("API response did not contain an object at metadata")
    for model in items:
        if not isinstance(model, dict):
            raise ValueError("API response contained a non-object model")
        model_id = model.get("id")
        if not isinstance(model_id, int) or isinstance(model_id, bool) or model_id <= 0:
            raise ValueError("API response contained an invalid model id")
        versions = model.get("modelVersions", [])
        if not isinstance(versions, list):
            raise ValueError(f"model {model_id} modelVersions must be a list")
        for version in versions:
            if not isinstance(version, dict):
                raise ValueError(f"model {model_id} contained a non-object version")
            version_id = version.get("id")
            if not isinstance(version_id, int) or isinstance(version_id, bool) or version_id <= 0:
                raise ValueError("API response contained an invalid version id")
    return data


def fetch_json(url: str, retries: int, delay_seconds: float) -> FetchResult:
    if retries < 0 or delay_seconds < 0:
        raise ValueError("retry arguments must be non-negative")
    if not math.isfinite(delay_seconds):
        raise ValueError("retry delay must be finite")
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        request = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=60) as response:
                data = json.loads(response.read().decode("utf-8"))
            return FetchResult(url=url, data=validate_page(data))
        except HTTPError as exc:
            last_error = exc
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(delay_seconds * (attempt + 1))
                continue
            raise
        except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(delay_seconds * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"request failed: {last_error}")


def normalize_next_page(next_page: str | None, current_url: str = API_BASE) -> str | None:
    if not next_page or not isinstance(next_page, str):
        return None
    candidate = urlparse(next_page)
    current = urlparse(current_url)
    if candidate.scheme != "https" or not candidate.netloc:
        return None
    if candidate.hostname != current.hostname:
        return None
    if candidate.port != current.port:
        return None
    if candidate.username or candidate.password:
        return None
    return next_page


def fallback_next_page(result_url: str, metadata: dict[str, Any]) -> str | None:
    current_page = metadata.get("currentPage")
    total_pages = metadata.get("totalPages")
    if not isinstance(current_page, int) or not isinstance(total_pages, int):
        return None
    if current_page < 1 or total_pages < current_page or current_page >= total_pages:
        return None
    parsed = urlparse(result_url)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    params = dict(parse_qsl(parsed.query))
    params["page"] = str(current_page + 1)
    return build_url(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", params)


def cursor_next_page(query: dict[str, Any], metadata: dict[str, Any]) -> str | None:
    """Carry an opaque API cursor in the original validated query."""
    cursor = metadata.get("nextCursor")
    if cursor is None:
        return None
    if not isinstance(cursor, (str, int)) or str(cursor) == "":
        raise ValueError("API response contained an invalid nextCursor")
    params = dict(query)
    params.pop("page", None)
    params["cursor"] = str(cursor)
    return build_url(API_BASE, params)
