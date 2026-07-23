from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

from dcmget.update_signing import (
    MAX_ENVELOPE_BYTES,
    MAX_MANIFEST_BYTES,
    UpdateSigningError,
    load_private_key,
    load_public_key,
    sign_manifest,
    verify_manifest,
)
from dcmget.update_trust import (
    DEFAULT_UPDATE_KEY_ID,
    TRUSTED_UPDATE_PUBLIC_KEYS,
)


@pytest.fixture
def signing_key():
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, private_pem, public_pem


def test_signed_envelope_round_trip_preserves_exact_manifest_bytes(signing_key):
    private_key, private_pem, public_pem = signing_key
    manifest = b'{"version":"3.6.1","notes":"\\u6d4b\\u8bd5"}\r\n'

    envelope = sign_manifest(
        manifest,
        private_key=load_private_key(private_pem),
        key_id="release-2026",
    )
    parsed = json.loads(envelope)

    assert parsed == {
        "algorithm": "Ed25519",
        "key_id": "release-2026",
        "payload": base64.b64encode(manifest).decode("ascii"),
        "schema_version": 1,
        "signature": base64.b64encode(private_key.sign(manifest)).decode("ascii"),
    }
    assert (
        verify_manifest(envelope, {"release-2026": public_pem})
        == manifest
    )
    assert (
        verify_manifest(
            envelope,
            {"release-2026": load_public_key(public_pem)},
        )
        == manifest
    )


def test_load_private_key_supports_encrypted_ed25519_pem(signing_key):
    private_key, _private_pem, public_pem = signing_key
    encrypted = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"test-password"),
    )

    loaded = load_private_key(encrypted, password=b"test-password")
    envelope = sign_manifest(b"manifest", private_key=loaded, key_id="test")

    assert verify_manifest(envelope, {"test": public_pem}) == b"manifest"
    with pytest.raises(UpdateSigningError, match="无法加载"):
        load_private_key(encrypted, password=b"wrong-password")


def test_wrong_key_unknown_key_and_tampered_payload_are_rejected(signing_key):
    private_key, _private_pem, public_pem = signing_key
    other = Ed25519PrivateKey.generate()
    other_public_pem = other.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    envelope = sign_manifest(
        b'{"version":"3.6.1"}',
        private_key=private_key,
        key_id="release",
    )

    with pytest.raises(UpdateSigningError, match="不受信任"):
        verify_manifest(envelope, {"other": public_pem})
    with pytest.raises(UpdateSigningError, match="签名无效"):
        verify_manifest(envelope, {"release": other_public_pem})

    parsed = json.loads(envelope)
    parsed["payload"] = base64.b64encode(b'{"version":"9.9.9"}').decode("ascii")
    tampered = json.dumps(parsed, separators=(",", ":")).encode()
    with pytest.raises(UpdateSigningError, match="签名无效"):
        verify_manifest(tampered, {"release": public_pem})


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda value: value.update(schema_version=2), "版本"),
        (lambda value: value.update(schema_version=True), "版本"),
        (lambda value: value.update(algorithm="ed25519"), "算法"),
        (lambda value: value.update(key_id="../release"), "key_id"),
        (lambda value: value.update(payload=1), "类型"),
        (lambda value: value.update(signature=None), "类型"),
        (lambda value: value.update(extra=True), "字段"),
        (lambda value: value.pop("payload"), "字段"),
    ],
)
def test_envelope_schema_is_strict(signing_key, mutator, message):
    private_key, _private_pem, public_pem = signing_key
    parsed = json.loads(
        sign_manifest(b"manifest", private_key=private_key, key_id="release")
    )
    mutator(parsed)

    with pytest.raises(UpdateSigningError, match=message):
        verify_manifest(
            json.dumps(parsed, separators=(",", ":")).encode(),
            {"release": public_pem},
        )


@pytest.mark.parametrize(
    "envelope",
    [
        b"",
        b"\xff",
        b"[]",
        b'{"schema_version":NaN}',
        b'{"schema_version":1,"schema_version":1}',
    ],
)
def test_invalid_json_and_duplicate_fields_are_rejected(envelope):
    with pytest.raises(UpdateSigningError):
        verify_manifest(envelope, {})


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("payload", "***", "Base64"),
        ("payload", "YQ", "Base64"),
        ("signature", "***", "Base64"),
        ("signature", base64.b64encode(b"short").decode("ascii"), "长度"),
    ],
)
def test_base64_and_signature_length_are_strict(
    signing_key,
    field,
    value,
    message,
):
    private_key, _private_pem, public_pem = signing_key
    parsed = json.loads(
        sign_manifest(b"manifest", private_key=private_key, key_id="release")
    )
    parsed[field] = value

    with pytest.raises(UpdateSigningError, match=message):
        verify_manifest(
            json.dumps(parsed, separators=(",", ":")).encode(),
            {"release": public_pem},
        )


def test_manifest_and_envelope_size_limits_are_enforced(signing_key):
    private_key, _private_pem, public_pem = signing_key

    with pytest.raises(UpdateSigningError, match="大小限制"):
        sign_manifest(
            b"x" * (MAX_MANIFEST_BYTES + 1),
            private_key=private_key,
            key_id="release",
        )
    envelope = sign_manifest(
        b"x" * 32,
        private_key=private_key,
        key_id="release",
    )
    with pytest.raises(UpdateSigningError, match="大小限制"):
        verify_manifest(
            envelope,
            {"release": public_pem},
            max_manifest_bytes=31,
        )
    with pytest.raises(UpdateSigningError, match="大小限制"):
        verify_manifest(b"x" * (MAX_ENVELOPE_BYTES + 1), {})


def test_key_types_and_key_ids_are_validated(signing_key):
    private_key, private_pem, public_pem = signing_key
    envelope = sign_manifest(b"manifest", private_key=private_key, key_id="release")

    with pytest.raises(UpdateSigningError, match="key_id"):
        sign_manifest(b"manifest", private_key=private_key, key_id="")
    with pytest.raises(UpdateSigningError, match="私钥"):
        sign_manifest(b"manifest", private_key=object(), key_id="release")
    with pytest.raises(UpdateSigningError, match="公钥格式"):
        verify_manifest(envelope, {"release": object()})
    with pytest.raises(UpdateSigningError, match="私钥"):
        load_private_key(public_pem)

    rsa_private = generate_private_key(public_exponent=65537, key_size=2048)
    rsa_public_pem = rsa_private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    with pytest.raises(UpdateSigningError, match="不是 Ed25519"):
        load_public_key(rsa_public_pem)
    assert isinstance(load_private_key(private_pem), Ed25519PrivateKey)


def test_embedded_update_trust_anchors_are_valid_ed25519_keys():
    assert DEFAULT_UPDATE_KEY_ID in TRUSTED_UPDATE_PUBLIC_KEYS
    assert TRUSTED_UPDATE_PUBLIC_KEYS
    for public_key_pem in TRUSTED_UPDATE_PUBLIC_KEYS.values():
        assert load_public_key(public_key_pem)
