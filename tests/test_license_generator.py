from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tools import dcmget_license_generator as generator


def test_generator_script_can_run_directly_from_source_tree():
    script = Path(__file__).resolve().parents[1] / "tools" / "dcmget_license_generator.py"

    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0
    assert "DcmGet" in result.stdout


def test_generator_rejects_private_key_that_client_cannot_verify(tmp_path, capsys):
    private_key = Ed25519PrivateKey.generate()
    private_path = tmp_path / "wrong-private.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    result = generator.main(
        [
            "ABCDEF-123456-7890AB-CDEF12",
            "--customer",
            "测试医院",
            "--private-key",
            str(private_path),
            "--raw",
        ]
    )

    assert result == 1
    assert "签名无效" in capsys.readouterr().err
