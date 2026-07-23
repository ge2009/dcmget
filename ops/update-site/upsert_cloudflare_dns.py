#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


API_BASE = "https://api.cloudflare.com/client/v4"
DEFAULT_ZONE = "v2ex.com.cn"
DEFAULT_RECORD_HOST = "dcmget"
DEFAULT_IPV4 = "144.34.233.165"
DEFAULT_TTL = 1
DEFAULT_PROXIED = True


class CloudflareApiError(RuntimeError):
    pass


class ApiResponseError(CloudflareApiError):
    pass


@dataclass(frozen=True)
class ApiDnsRecord:
    record_id: str
    name: str
    type: str
    content: str
    proxied: bool
    ttl: int


def _mask_target(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 5) + value[-3:]


def _api_headers(api_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }


def _compose_url(path: str) -> str:
    if not path.startswith("/"):
        raise ValueError("path must start with /")
    return f"{API_BASE}{path}"


def _safe_request(
    method: str,
    url: str,
    api_token: str,
    payload: Mapping[str, Any] | None = None,
) -> Any:
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        method=method,
        headers=_api_headers(api_token),
        data=body,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:  # pragma: no cover - exercised in tests via monkeypatch
        raw = exc.read().decode("utf-8", errors="ignore")
        raise CloudflareApiError(
            f"HTTP {exc.code} {exc.reason} for {method} {url}: {raw}"
        ) from exc
    except OSError as exc:
        raise CloudflareApiError(f"Network failure for {method} {url}: {exc}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiResponseError(f"Invalid JSON response from {method} {url}: {raw}") from exc


def _require_api_result_ok(result: Mapping[str, Any], context: str) -> Mapping[str, Any]:
    if not bool(result.get("success")):
        errors = result.get("errors") or []
        if errors:
            first_error = errors[0]
            code = first_error.get("code", "unknown")
            msg = first_error.get("message", "unknown error")
            raise CloudflareApiError(f"{context} failed: [{code}] {msg}")
        raise CloudflareApiError(f"{context} failed: API returned success=false")
    return result


def _build_record_name(record_host: str, zone_name: str) -> str:
    if "." in record_host:
        return record_host
    return f"{record_host}.{zone_name}"


def _iter_records_by_name(
    zone_id: str,
    record_host: str,
    api_token: str,
) -> Iterable[ApiDnsRecord]:
    url = _compose_url(
        f"/zones/{urllib.parse.quote(zone_id, safe='')}"
        f"/dns_records?type=A&name={urllib.parse.quote(record_host)}&per_page=100"
    )
    result = _require_api_result_ok(_safe_request("GET", url, api_token), "List A records")
    for item in result.get("result", []) or []:
        yield ApiDnsRecord(
            record_id=item["id"],
            name=item["name"],
            type=item["type"],
            content=item["content"],
            proxied=bool(item.get("proxied", False)),
            ttl=int(item.get("ttl", 0)),
        )


def _get_zone_id(zone_name: str, api_token: str) -> str:
    encoded_name = urllib.parse.quote(zone_name)
    url = _compose_url(f"/zones?name={encoded_name}&status=active&per_page=50")
    result = _require_api_result_ok(_safe_request("GET", url, api_token), "Get zone")
    results = result.get("result") or []
    matches = [item["id"] for item in results if item.get("name") == zone_name]
    if not matches:
        raise CloudflareApiError(f"Zone not found: {zone_name}")
    if len(matches) > 1:
        raise CloudflareApiError(f"Ambiguous zone name {zone_name}: {len(matches)} matches")
    return matches[0]


def upsert_dns_record(
    *,
    zone_name: str = DEFAULT_ZONE,
    record_host: str = DEFAULT_RECORD_HOST,
    ipv4: str = DEFAULT_IPV4,
    proxied: bool = DEFAULT_PROXIED,
    ttl: int = DEFAULT_TTL,
    dry_run: bool = False,
    api_token: str | None = None,
) -> str:
    token = api_token or os.environ.get("CLOUDFLARE_API_TOKEN")
    if not token:
        raise CloudflareApiError("Missing CLOUDFLARE_API_TOKEN environment variable")
    zone_id = _get_zone_id(zone_name, token)
    fqdn = _build_record_name(record_host, zone_name)
    existing = list(_iter_records_by_name(zone_id, fqdn, token))

    if len(existing) > 1:
        raise CloudflareApiError(f"Multiple A records exist for {fqdn}; refuse to proceed")

    payload = {
        "type": "A",
        "name": fqdn,
        "content": ipv4,
        "proxied": proxied,
        "ttl": ttl,
    }
    target = _mask_target(fqdn)
    if len(existing) == 0:
        action = "created"
        if dry_run:
            print(f"[DRY-RUN] {action}: target={target}")
            return action

        url = _compose_url(f"/zones/{zone_id}/dns_records")
        _require_api_result_ok(_safe_request("POST", url, token, payload), "Create A record")
        print(f"{action}: target={target}")
        return action

    current = existing[0]
    need_update = (
        current.type != "A"
        or current.content != ipv4
        or current.proxied != proxied
        or current.ttl != ttl
    )
    if not need_update:
        action = "unchanged"
        print(f"{action}: target={target}")
        return action

    action = "updated"
    if dry_run:
        print(f"[DRY-RUN] {action}: target={target}")
        return action

    url = _compose_url(f"/zones/{zone_id}/dns_records/{current.record_id}")
    _require_api_result_ok(
        _safe_request("PUT", url, token, payload), "Update A record"
    )
    print(f"{action}: target={target}")
    return action


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upsert one Cloudflare A record for dcmget.v2ex.com.cn"
    )
    parser.add_argument(
        "--zone",
        default=DEFAULT_ZONE,
        help=f"Cloudflare zone name (default: {DEFAULT_ZONE})",
    )
    parser.add_argument(
        "--record",
        default=DEFAULT_RECORD_HOST,
        help=f"record host without zone or full FQDN (default: {DEFAULT_RECORD_HOST})",
    )
    parser.add_argument(
        "--ipv4",
        default=DEFAULT_IPV4,
        help=f"IPv4 target (default: {DEFAULT_IPV4})",
    )
    parser.add_argument(
        "--proxied",
        action="store_true",
        default=DEFAULT_PROXIED,
        help="Set Cloudflare proxy enabled",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print planned action, no API writes",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        upsert_dns_record(
            zone_name=args.zone,
            record_host=args.record,
            ipv4=args.ipv4,
            proxied=args.proxied,
            ttl=DEFAULT_TTL,
            dry_run=args.dry_run,
        )
        return 0
    except CloudflareApiError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
