from __future__ import annotations

import os
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "ops" / "update-site" / "upsert_cloudflare_dns.py"

spec = spec_from_file_location("upsert_cloudflare_dns", SCRIPT_PATH)
module = module_from_spec(spec)
assert spec.loader is not None
sys.modules["upsert_cloudflare_dns"] = module
spec.loader.exec_module(module)  # type: ignore[arg-type]


def _fake_api_factory(staged_calls, responses):
    def _fake_request(method, url, api_token, payload=None):
        staged_calls.append((method, url, payload is None, dict(payload or {})))
        if method == "GET" and "/zones?name=" in url:
            return responses["zone"]
        if method == "GET" and "/dns_records?type=A&name=" in url:
            return responses["records"]
        if method == "POST" and "/dns_records" in url:
            return responses["post"]
        if method == "PUT" and "/dns_records/" in url:
            return responses["put"]
        raise AssertionError(f"Unexpected request: {method} {url}")

    return _fake_request


def _build_success_payload(result):
    return {"success": True, "result": result, "errors": []}


class TestCloudflareDnsCli(unittest.TestCase):
    def setUp(self):
        os.environ["CLOUDFLARE_API_TOKEN"] = "test-token"

    def tearDown(self):
        os.environ.pop("CLOUDFLARE_API_TOKEN", None)

    @patch.object(module, "_safe_request")
    def test_create_record_if_missing(self, mock_request):
        calls = []
        mock_request.side_effect = _fake_api_factory(calls, {
            "zone": _build_success_payload([{"id": "zone-1", "name": "v2ex.com.cn"}]),
            "records": _build_success_payload([]),
            "post": _build_success_payload({"id": "rec-1"}),
            "put": _build_success_payload({"id": "should-not-call"}),
        })

        result = module.upsert_dns_record(dry_run=False)

        self.assertEqual(result, "created")
        self.assertEqual([c[0] for c in calls], ["GET", "GET", "POST"])
        self.assertTrue(calls[2][1].endswith("/zones/zone-1/dns_records"))

    @patch.object(module, "_safe_request")
    def test_update_when_record_differs(self, mock_request):
        calls = []
        mock_request.side_effect = _fake_api_factory(calls, {
            "zone": _build_success_payload([{"id": "zone-1", "name": "v2ex.com.cn"}]),
            "records": _build_success_payload([
                {
                    "id": "rec-old",
                    "name": "dcmget.v2ex.com.cn",
                    "type": "A",
                    "content": "1.1.1.1",
                    "proxied": False,
                    "ttl": 120,
                },
            ]),
            "post": _build_success_payload({"id": "should-not-call"}),
            "put": _build_success_payload({"id": "rec-old"}),
        })

        result = module.upsert_dns_record(dry_run=False)

        self.assertEqual(result, "updated")
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[2][0], "PUT")
        self.assertTrue(calls[2][1].endswith("/zones/zone-1/dns_records/rec-old"))
        self.assertEqual(calls[2][3]["content"], module.DEFAULT_IPV4)
        self.assertIs(calls[2][3]["proxied"], True)

    @patch.object(module, "_safe_request")
    def test_no_change_when_record_equal(self, mock_request):
        calls = []
        mock_request.side_effect = _fake_api_factory(calls, {
            "zone": _build_success_payload([{"id": "zone-1", "name": "v2ex.com.cn"}]),
            "records": _build_success_payload([
                {
                    "id": "rec-old",
                    "name": "dcmget.v2ex.com.cn",
                    "type": "A",
                    "content": module.DEFAULT_IPV4,
                    "proxied": True,
                    "ttl": module.DEFAULT_TTL,
                },
            ]),
            "post": _build_success_payload({"id": "should-not-call"}),
            "put": _build_success_payload({"id": "should-not-call"}),
        })

        result = module.upsert_dns_record(dry_run=False)

        self.assertEqual(result, "unchanged")
        self.assertEqual([c[0] for c in calls], ["GET", "GET"])

    @patch.object(module, "_safe_request")
    def test_fail_closed_if_multiple_a_records(self, mock_request):
        calls = []
        mock_request.side_effect = _fake_api_factory(calls, {
            "zone": _build_success_payload([{"id": "zone-1", "name": "v2ex.com.cn"}]),
            "records": _build_success_payload([
                {
                    "id": "rec-1",
                    "name": "dcmget.v2ex.com.cn",
                    "type": "A",
                    "content": "1.1.1.1",
                    "proxied": False,
                    "ttl": 120,
                },
                {
                    "id": "rec-2",
                    "name": "dcmget.v2ex.com.cn",
                    "type": "A",
                    "content": "2.2.2.2",
                    "proxied": True,
                    "ttl": 1,
                },
            ]),
            "post": _build_success_payload({"id": "should-not-call"}),
            "put": _build_success_payload({"id": "should-not-call"}),
        })

        with self.assertRaises(module.CloudflareApiError):
            module.upsert_dns_record(dry_run=False)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "GET")
        self.assertEqual(calls[1][0], "GET")

    @patch.object(module, "_safe_request")
    def test_dry_run_does_not_write(self, mock_request):
        calls = []
        mock_request.side_effect = _fake_api_factory(calls, {
            "zone": _build_success_payload([{"id": "zone-1", "name": "v2ex.com.cn"}]),
            "records": _build_success_payload([]),
            "post": _build_success_payload({"id": "should-not-call"}),
            "put": _build_success_payload({"id": "should-not-call"}),
        })

        result = module.upsert_dns_record(dry_run=True)

        self.assertEqual(result, "created")
        self.assertEqual([c[0] for c in calls], ["GET", "GET"])
        self.assertNotEqual(len(calls), 3)

    def test_missing_token_raises(self):
        os.environ.pop("CLOUDFLARE_API_TOKEN", None)
        with self.assertRaises(module.CloudflareApiError):
            module.upsert_dns_record(api_token=None)


if __name__ == "__main__":
    unittest.main()
