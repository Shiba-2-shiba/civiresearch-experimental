from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from civitrends import api  # noqa: E402


class _Response:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


class ApiTests(unittest.TestCase):
    def test_fetch_json_rejects_non_finite_delay(self) -> None:
        for value in (float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "finite"):
                    api.fetch_json("https://civitai.com/api/v1/models", retries=0, delay_seconds=value)

    def test_validate_page_rejects_invalid_model_and_version_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "model id"):
            api.validate_page({"items": [{"id": "bad", "modelVersions": []}], "metadata": {}})
        with self.assertRaisesRegex(ValueError, "version id"):
            api.validate_page({
                "items": [{"id": 1, "modelVersions": [{"id": None}]}], "metadata": {}
            })

    def test_validate_page_rejects_missing_metadata(self) -> None:
        with self.assertRaisesRegex(ValueError, "metadata"):
            api.validate_page({"items": []})

    def test_normalize_next_page_rejects_cross_origin_and_non_https_urls(self) -> None:
        current = "https://civitai.com/api/v1/models?page=1"
        self.assertEqual(
            api.normalize_next_page("https://civitai.com/api/v1/models?page=2", current),
            "https://civitai.com/api/v1/models?page=2",
        )
        self.assertIsNone(api.normalize_next_page("https://evil.test/steal", current))
        self.assertIsNone(api.normalize_next_page("http://civitai.com/api/v1/models?page=2", current))

    def test_fetch_json_retries_retryable_http_errors(self) -> None:
        error = HTTPError("https://civitai.com/api/v1/models", 503, "busy", None, None)
        with patch.object(api, "urlopen", side_effect=[error, _Response({"items": [], "metadata": {}})]) as opened, patch.object(api.time, "sleep"):
            result = api.fetch_json("https://civitai.com/api/v1/models", retries=1, delay_seconds=0)

        self.assertEqual(result.data["items"], [])
        self.assertEqual(opened.call_count, 2)


if __name__ == "__main__":
    unittest.main()
