//! Compatibility verifier for the existing `DcmGet` Ed25519 update envelope.

use std::{collections::HashMap, hash::BuildHasher};

use base64::{Engine, engine::general_purpose::STANDARD};
use ed25519_dalek::{Signature, Verifier, VerifyingKey, pkcs8::DecodePublicKey};
use serde::Deserialize;
use thiserror::Error;

pub const SCHEMA_VERSION: u8 = 1;
pub const ALGORITHM: &str = "Ed25519";
pub const MAX_MANIFEST_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_ENVELOPE_BYTES: usize = 6 * 1024 * 1024;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum VerificationError {
    #[error("update envelope is empty or too large")]
    EnvelopeSize,
    #[error("update envelope is not valid UTF-8 JSON: {0}")]
    InvalidJson(String),
    #[error("update envelope schema is not supported")]
    Schema,
    #[error("update envelope algorithm is not supported")]
    Algorithm,
    #[error("update key id is invalid")]
    KeyId,
    #[error("update key is not trusted: {0}")]
    UntrustedKey(String),
    #[error("update envelope contains invalid canonical base64")]
    Base64,
    #[error("update manifest is empty or too large")]
    ManifestSize,
    #[error("update signature length is invalid")]
    SignatureLength,
    #[error("trusted update public key is invalid")]
    PublicKey,
    #[error("update manifest signature is invalid")]
    Signature,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Envelope {
    schema_version: u8,
    algorithm: String,
    key_id: String,
    payload: String,
    signature: String,
}

/// Verify the signed envelope and return the exact manifest bytes.
///
/// This intentionally mirrors `dcmget.update_signing.verify_manifest`, so a
/// native client can consume the current stable feed without a flag day.
pub fn verify_envelope<S: BuildHasher>(
    envelope: &[u8],
    trusted_public_keys: &HashMap<String, Vec<u8>, S>,
) -> Result<Vec<u8>, VerificationError> {
    if envelope.is_empty() || envelope.len() > MAX_ENVELOPE_BYTES {
        return Err(VerificationError::EnvelopeSize);
    }
    let parsed: Envelope = serde_json::from_slice(envelope)
        .map_err(|error| VerificationError::InvalidJson(error.to_string()))?;
    if parsed.schema_version != SCHEMA_VERSION {
        return Err(VerificationError::Schema);
    }
    if parsed.algorithm != ALGORITHM {
        return Err(VerificationError::Algorithm);
    }
    if !valid_key_id(&parsed.key_id) {
        return Err(VerificationError::KeyId);
    }
    let trusted = trusted_public_keys
        .get(&parsed.key_id)
        .ok_or_else(|| VerificationError::UntrustedKey(parsed.key_id.clone()))?;
    let payload = decode_canonical_base64(&parsed.payload)?;
    if payload.is_empty() || payload.len() > MAX_MANIFEST_BYTES {
        return Err(VerificationError::ManifestSize);
    }
    let signature_bytes = decode_canonical_base64(&parsed.signature)?;
    let signature =
        Signature::from_slice(&signature_bytes).map_err(|_| VerificationError::SignatureLength)?;
    let pem = std::str::from_utf8(trusted).map_err(|_| VerificationError::PublicKey)?;
    let key = VerifyingKey::from_public_key_pem(pem).map_err(|_| VerificationError::PublicKey)?;
    key.verify(&payload, &signature)
        .map_err(|_| VerificationError::Signature)?;
    Ok(payload)
}

fn decode_canonical_base64(value: &str) -> Result<Vec<u8>, VerificationError> {
    let decoded = STANDARD
        .decode(value.as_bytes())
        .map_err(|_| VerificationError::Base64)?;
    if STANDARD.encode(&decoded) != value {
        return Err(VerificationError::Base64);
    }
    Ok(decoded)
}

fn valid_key_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 64
        && bytes[0].is_ascii_alphanumeric()
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(*byte, b'.' | b'_' | b'-'))
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::{
        Signer, SigningKey,
        pkcs8::{EncodePublicKey, spki::der::pem::LineEnding},
    };
    use rand::rngs::OsRng;
    use serde_json::json;

    fn signed_envelope(payload: &[u8]) -> (Vec<u8>, HashMap<String, Vec<u8>>) {
        let signing_key = SigningKey::generate(&mut OsRng);
        let public_pem = signing_key
            .verifying_key()
            .to_public_key_pem(LineEnding::default())
            .unwrap();
        let signature = signing_key.sign(payload);
        let envelope = serde_json::to_vec(&json!({
            "schema_version": 1,
            "algorithm": "Ed25519",
            "key_id": "release-2026",
            "payload": STANDARD.encode(payload),
            "signature": STANDARD.encode(signature.to_bytes()),
        }))
        .unwrap();
        (
            envelope,
            HashMap::from([("release-2026".into(), public_pem.into_bytes())]),
        )
    }

    #[test]
    fn preserves_exact_signed_manifest_bytes() {
        let payload = b"{\"version\":\"4.0.0\"}\r\n";
        let (envelope, trusted) = signed_envelope(payload);
        assert_eq!(verify_envelope(&envelope, &trusted).unwrap(), payload);
    }

    #[test]
    fn rejects_tampering_unknown_fields_and_duplicate_fields() {
        let (envelope, trusted) = signed_envelope(b"manifest");
        let mut parsed: serde_json::Value = serde_json::from_slice(&envelope).unwrap();
        parsed["payload"] = serde_json::Value::String(STANDARD.encode(b"tampered"));
        assert_eq!(
            verify_envelope(&serde_json::to_vec(&parsed).unwrap(), &trusted),
            Err(VerificationError::Signature)
        );

        let unknown = br#"{"schema_version":1,"algorithm":"Ed25519","key_id":"k","payload":"YQ==","signature":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==","extra":true}"#;
        assert!(matches!(
            verify_envelope(unknown, &trusted),
            Err(VerificationError::InvalidJson(_))
        ));

        let duplicate = br#"{"schema_version":1,"schema_version":1,"algorithm":"Ed25519","key_id":"k","payload":"YQ==","signature":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="}"#;
        assert!(matches!(
            verify_envelope(duplicate, &trusted),
            Err(VerificationError::InvalidJson(_))
        ));
    }

    #[test]
    fn rejects_non_canonical_base64_and_oversize_envelopes() {
        let (envelope, trusted) = signed_envelope(b"manifest");
        let mut parsed: serde_json::Value = serde_json::from_slice(&envelope).unwrap();
        parsed["payload"] = serde_json::Value::String("bWFuaWZlc3Q".into());
        assert_eq!(
            verify_envelope(&serde_json::to_vec(&parsed).unwrap(), &trusted),
            Err(VerificationError::Base64)
        );
        assert_eq!(
            verify_envelope(&vec![b'x'; MAX_ENVELOPE_BYTES + 1], &trusted),
            Err(VerificationError::EnvelopeSize)
        );
    }
}
