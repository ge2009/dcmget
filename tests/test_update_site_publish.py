from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dcmget.update_signing import sign_manifest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PUBLISH_SCRIPT = PROJECT_ROOT / "ops" / "update-site" / "publish_windows_release.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _publisher_fixture(
    tmp_path: Path,
) -> tuple[Path, Ed25519PrivateKey, str]:
    project = tmp_path / "publisher-project"
    script = project / "ops" / "update-site" / PUBLISH_SCRIPT.name
    script.parent.mkdir(parents=True)
    shutil.copy2(PUBLISH_SCRIPT, script)
    script.chmod(0o755)
    package = project / "dcmget"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(PROJECT_ROOT / "dcmget" / "update_signing.py", package)
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    key_id = "publisher-test-key"
    (package / "update_trust.py").write_text(
        f"DEFAULT_UPDATE_KEY_ID = {key_id!r}\n"
        f"TRUSTED_UPDATE_PUBLIC_KEYS = {{{key_id!r}: {public_key!r}}}\n",
        encoding="utf-8",
    )
    return script, private_key, key_id


def _run_publish_with_manifest(
    tmp_path: Path,
    *,
    version: str,
    manifest: dict[str, object],
    assets: dict[str, bytes],
) -> tuple[subprocess.CompletedProcess[str], Path]:
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    for name, content in assets.items():
        (release_dir / name).write_bytes(content)
    publish_script, private_key, key_id = _publisher_fixture(tmp_path)
    manifest.setdefault(
        "manifest_signature",
        {
            "name": "UPDATE-MANIFEST.signed.json",
            "kind": "ed25519_signed_envelope",
            "algorithm": "Ed25519",
            "key_id": key_id,
            "content_encoding": "base64",
        },
    )
    raw_manifest = json.dumps(manifest, sort_keys=True).encode("utf-8")
    (release_dir / "UPDATE-MANIFEST.json").write_bytes(raw_manifest)
    (release_dir / "UPDATE-MANIFEST.signed.json").write_bytes(
        sign_manifest(raw_manifest, private_key=private_key, key_id=key_id)
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "network-called"
    for command in ("ssh", "scp", "rsync"):
        _write_executable(
            fake_bin / command,
            f"#!/bin/sh\ntouch '{marker}'\nexit 99\n",
        )
    environment = dict(os.environ)
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    completed = subprocess.run(
        [str(publish_script), str(release_dir), version],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed, marker


def _patch_only_manifest(
    *,
    version: str,
    patch_name: str,
    patch_content: bytes,
) -> dict[str, object]:
    sha256 = hashlib.sha256
    install_tree = sha256(b"target tree").hexdigest()
    patch_record = {
        "name": patch_name,
        "kind": "component_patch",
        "size": len(patch_content),
        "sha256": sha256(patch_content).hexdigest(),
        "signature_status": "NOT_APPLICABLE",
        "base_version": "3.5.9",
        "preserves_user_data": True,
        "content_scope": "application",
        "layout_version": 1,
        "install_path_allowlist": ["DcmGet.exe", "_internal/**"],
        "base_tree_sha256": sha256(b"base tree").hexdigest(),
        "target_tree_sha256": install_tree,
        "files": [
            {
                "path": "DcmGet.exe",
                "size": 12,
                "sha256": sha256(b"target file").hexdigest(),
                "base_missing": False,
                "base_size": 10,
                "base_sha256": sha256(b"base file").hexdigest(),
            }
        ],
        "removed_paths": [],
    }
    return {
        "schema_version": 1,
        "product": "DcmGet",
        "platform": "windows-x64",
        "channel": "stable",
        "version": version,
        "layout_version": 1,
        "install_tree_sha256": install_tree,
        "component_chain": {
            "schema_version": 1,
            "root_full_releases": [
                {
                    "version": "3.5.0",
                    "install_tree_sha256": sha256(b"full root").hexdigest(),
                }
            ],
        },
        "artifacts": [patch_record],
        "full_installer": None,
        "component_patches": [patch_record],
    }


def test_publish_rejects_signed_manifest_version_mismatch_before_network(
    tmp_path: Path,
) -> None:
    name = "DcmGet-3.6.1-Setup-x64.exe"
    content = b"installer placeholder"
    record = {
        "name": name,
        "kind": "full_installer",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "signature_status": "UNSIGNED",
        "preserves_user_data": True,
        "content_scope": "application",
    }
    manifest = {
        "schema_version": 1,
        "product": "DcmGet",
        "platform": "windows-x64",
        "channel": "stable",
        "version": "3.6.1",
        "layout_version": 1,
        "install_tree_sha256": hashlib.sha256(b"tree").hexdigest(),
        "artifacts": [record],
        "full_installer": record,
        "component_patches": [],
    }

    completed, marker = _run_publish_with_manifest(
        tmp_path,
        version="3.6.0",
        manifest=manifest,
        assets={name: content},
    )

    assert completed.returncode != 0
    assert "version 不匹配" in completed.stderr
    assert not marker.exists()


def test_publish_accepts_patch_only_manifest_before_network(tmp_path: Path) -> None:
    patch_name = "DcmGet-3.6.0-windows-x64-components-from-3.5.9.zip"
    patch_content = b"component patch"
    manifest = _patch_only_manifest(
        version="3.6.0",
        patch_name=patch_name,
        patch_content=patch_content,
    )

    completed, marker = _run_publish_with_manifest(
        tmp_path,
        version="3.6.0",
        manifest=manifest,
        assets={patch_name: patch_content},
    )

    assert completed.returncode != 0
    assert marker.exists(), completed.stderr
    assert "必须且只能包含一个完整安装包" not in completed.stderr


@pytest.mark.parametrize("signature_status", ["SIGNED", "UNSIGNED"])
def test_publish_accepts_full_manifest_before_network(
    tmp_path: Path, signature_status: str,
) -> None:
    name = "DcmGet-3.6.0-Setup-x64.exe"
    content = b"signed full payload"
    record = {
        "name": name,
        "kind": "full_installer",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "signature_status": signature_status,
        "preserves_user_data": True,
        "content_scope": "application",
    }
    manifest = {
        "schema_version": 1,
        "product": "DcmGet",
        "platform": "windows-x64",
        "channel": "stable",
        "version": "3.6.0",
        "layout_version": 1,
        "install_tree_sha256": hashlib.sha256(b"tree").hexdigest(),
        "artifacts": [record],
        "full_installer": record,
        "component_patches": [],
    }

    completed, marker = _run_publish_with_manifest(
        tmp_path,
        version="3.6.0",
        manifest=manifest,
        assets={name: content},
    )

    assert completed.returncode != 0
    assert marker.exists(), completed.stderr


def test_publish_rejects_invalid_patch_metadata_before_network(
    tmp_path: Path,
) -> None:
    patch_name = "DcmGet-3.6.0-windows-x64-components-from-3.5.9.zip"
    patch_content = b"component patch"
    manifest = _patch_only_manifest(
        version="3.6.0",
        patch_name=patch_name,
        patch_content=patch_content,
    )
    patch = manifest["component_patches"][0]  # type: ignore[index]
    patch["files"][0]["path"] = "../config.json"  # type: ignore[index]

    completed, marker = _run_publish_with_manifest(
        tmp_path,
        version="3.6.0",
        manifest=manifest,
        assets={patch_name: patch_content},
    )

    assert completed.returncode != 0
    assert "文件路径无效" in completed.stderr
    assert not marker.exists()


def test_publish_rejects_patch_only_self_anchored_as_full_release(
    tmp_path: Path,
) -> None:
    patch_name = "DcmGet-3.6.0-windows-x64-components-from-3.5.9.zip"
    patch_content = b"component patch"
    manifest = _patch_only_manifest(
        version="3.6.0",
        patch_name=patch_name,
        patch_content=patch_content,
    )
    chain = manifest["component_chain"]  # type: ignore[assignment]
    chain["root_full_releases"][0]["version"] = "3.6.0"  # type: ignore[index]

    completed, marker = _run_publish_with_manifest(
        tmp_path,
        version="3.6.0",
        manifest=manifest,
        assets={patch_name: patch_content},
    )

    assert completed.returncode != 0
    assert "更新链锚点无效" in completed.stderr
    assert not marker.exists()
