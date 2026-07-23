"""Pinned trust anchors for DcmGet application updates.

Only public keys belong in this module. The matching private keys must remain in
the release environment and must never be included in source or release files.
"""

from __future__ import annotations


DEFAULT_UPDATE_KEY_ID = "dcmget-update-2026-01"

TRUSTED_UPDATE_PUBLIC_KEYS: dict[str, bytes] = {
    DEFAULT_UPDATE_KEY_ID: b"""-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAZzhGP7ySxUyi4nilY1rfx5WOVna+A0WxwKn7fBqeAo4=
-----END PUBLIC KEY-----
""",
}
