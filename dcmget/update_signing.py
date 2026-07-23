from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


SCHEMA_VERSION = 1
ALGORITHM = "Ed25519"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_ENVELOPE_BYTES = 6 * 1024 * 1024

_ENVELOPE_FIELDS = frozenset(
    {"schema_version", "algorithm", "key_id", "payload", "signature"}
)
_KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class UpdateSigningError(ValueError):
    pass


def load_private_key(
    private_key_pem: bytes,
    *,
    password: bytes | None = None,
) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from a PEM byte string."""
    if not isinstance(private_key_pem, bytes) or not private_key_pem:
        raise UpdateSigningError("更新签名私钥必须是非空 PEM 字节")
    if password is not None and not isinstance(password, bytes):
        raise UpdateSigningError("更新签名私钥密码必须是字节")
    try:
        private_key = serialization.load_pem_private_key(
            private_key_pem,
            password=password,
        )
    except (TypeError, ValueError) as exc:
        raise UpdateSigningError("无法加载更新签名私钥") from exc
    if not isinstance(private_key, Ed25519PrivateKey):
        raise UpdateSigningError("更新签名私钥不是 Ed25519 格式")
    return private_key


def load_public_key(public_key_pem: bytes) -> Ed25519PublicKey:
    """Load an Ed25519 public key from a PEM byte string."""
    if not isinstance(public_key_pem, bytes) or not public_key_pem:
        raise UpdateSigningError("更新签名公钥必须是非空 PEM 字节")
    try:
        public_key = serialization.load_pem_public_key(public_key_pem)
    except (TypeError, ValueError) as exc:
        raise UpdateSigningError("无法加载更新签名公钥") from exc
    if not isinstance(public_key, Ed25519PublicKey):
        raise UpdateSigningError("更新签名公钥不是 Ed25519 格式")
    return public_key


def sign_manifest(
    manifest: bytes,
    *,
    private_key: Ed25519PrivateKey,
    key_id: str,
) -> bytes:
    """Return a compact JSON envelope containing and signing *manifest*."""
    payload = _validate_manifest(manifest)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise UpdateSigningError("更新签名私钥不是 Ed25519 格式")
    normalized_key_id = _validate_key_id(key_id)
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "algorithm": ALGORITHM,
        "key_id": normalized_key_id,
        "payload": _encode_base64(payload),
        "signature": _encode_base64(private_key.sign(payload)),
    }
    encoded = json.dumps(
        envelope,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_ENVELOPE_BYTES:
        raise UpdateSigningError("更新签名信封超过大小限制")
    return encoded


def verify_manifest(
    envelope: bytes,
    trusted_public_keys: Mapping[str, bytes | Ed25519PublicKey],
    *,
    max_manifest_bytes: int = MAX_MANIFEST_BYTES,
) -> bytes:
    """Verify an update envelope and return its exact original manifest bytes."""
    if (
        not isinstance(max_manifest_bytes, int)
        or isinstance(max_manifest_bytes, bool)
        or max_manifest_bytes <= 0
        or max_manifest_bytes > MAX_MANIFEST_BYTES
    ):
        raise UpdateSigningError("manifest 大小限制无效")
    if not isinstance(envelope, bytes) or not envelope:
        raise UpdateSigningError("更新签名信封必须是非空字节")
    if len(envelope) > MAX_ENVELOPE_BYTES:
        raise UpdateSigningError("更新签名信封超过大小限制")
    if not isinstance(trusted_public_keys, Mapping):
        raise UpdateSigningError("受信更新公钥配置无效")

    parsed = _parse_envelope(envelope)
    key_id = _validate_key_id(parsed["key_id"])
    payload = _decode_base64(parsed["payload"], field_name="payload")
    if not payload:
        raise UpdateSigningError("更新 manifest 不能为空")
    if len(payload) > max_manifest_bytes:
        raise UpdateSigningError("更新 manifest 超过大小限制")
    signature = _decode_base64(parsed["signature"], field_name="signature")
    if len(signature) != 64:
        raise UpdateSigningError("更新签名长度无效")

    trusted_key = trusted_public_keys.get(key_id)
    if trusted_key is None:
        raise UpdateSigningError(f"更新签名密钥不受信任：{key_id}")
    if isinstance(trusted_key, bytes):
        public_key = load_public_key(trusted_key)
    elif isinstance(trusted_key, Ed25519PublicKey):
        public_key = trusted_key
    else:
        raise UpdateSigningError(f"受信更新公钥格式无效：{key_id}")

    try:
        public_key.verify(signature, payload)
    except InvalidSignature as exc:
        raise UpdateSigningError("更新 manifest 签名无效") from exc
    return payload


def _validate_manifest(manifest: bytes) -> bytes:
    if not isinstance(manifest, bytes) or not manifest:
        raise UpdateSigningError("更新 manifest 必须是非空字节")
    if len(manifest) > MAX_MANIFEST_BYTES:
        raise UpdateSigningError("更新 manifest 超过大小限制")
    return manifest


def _validate_key_id(key_id: Any) -> str:
    if not isinstance(key_id, str) or not _KEY_ID_PATTERN.fullmatch(key_id):
        raise UpdateSigningError(
            "更新签名 key_id 必须是 1 至 64 位字母、数字、点、下划线或连字符"
        )
    return key_id


def _parse_envelope(envelope: bytes) -> dict[str, Any]:
    try:
        decoded = envelope.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UpdateSigningError("更新签名信封不是有效 UTF-8") from exc
    try:
        parsed = json.loads(
            decoded,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, UpdateSigningError):
            raise
        raise UpdateSigningError("更新签名信封不是有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise UpdateSigningError("更新签名信封必须是 JSON 对象")
    if set(parsed) != _ENVELOPE_FIELDS:
        raise UpdateSigningError("更新签名信封字段不完整或包含未知字段")
    schema_version = parsed["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != SCHEMA_VERSION
    ):
        raise UpdateSigningError("更新签名信封版本不受支持")
    if parsed["algorithm"] != ALGORITHM:
        raise UpdateSigningError("更新签名算法不受支持")
    if not isinstance(parsed["payload"], str) or not isinstance(
        parsed["signature"], str
    ):
        raise UpdateSigningError("更新签名信封的 payload 或 signature 类型无效")
    return parsed


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise UpdateSigningError(f"更新签名信封包含重复字段：{key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise UpdateSigningError(f"更新签名信封包含无效 JSON 常量：{value}")


def _encode_base64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode_base64(value: str, *, field_name: str) -> bytes:
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise UpdateSigningError(f"更新签名信封的 {field_name} 不是有效 Base64") from exc
    if _encode_base64(decoded) != value:
        raise UpdateSigningError(
            f"更新签名信封的 {field_name} 不是规范 Base64"
        )
    return decoded
