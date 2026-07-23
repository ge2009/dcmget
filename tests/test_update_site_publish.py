from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PUBLISH_SCRIPT = PROJECT_ROOT / "ops" / "update-site" / "publish_windows_release.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


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
    (release_dir / "UPDATE-MANIFEST.json.p7").write_bytes(b"fake-pkcs7")
    signed_manifest = tmp_path / "signed-manifest.json"
    raw_manifest = json.dumps(manifest, sort_keys=True).encode("utf-8")
    signed_manifest.write_bytes(raw_manifest)
    (release_dir / "UPDATE-MANIFEST.json").write_bytes(raw_manifest)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "openssl",
        """#!/bin/sh
set -eu
output=''
while [ "$#" -gt 0 ]; do
    if [ "$1" = '-out' ]; then
        output=$2
        shift 2
    else
        shift
    fi
done
cp "$FAKE_SIGNED_MANIFEST" "$output"
""",
    )
    marker = tmp_path / "network-called"
    for command in ("ssh", "scp", "rsync"):
        _write_executable(
            fake_bin / command,
            f"#!/bin/sh\ntouch '{marker}'\nexit 99\n",
        )
    environment = dict(os.environ)
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    environment["FAKE_SIGNED_MANIFEST"] = str(signed_manifest)
    completed = subprocess.run(
        [str(PUBLISH_SCRIPT), str(release_dir), version],
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
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    installer = release_dir / "DcmGet-3.6.1-Setup-x64.exe"
    installer.write_bytes(b"signed installer placeholder")
    (release_dir / "UPDATE-MANIFEST.json.p7").write_bytes(b"fake-pkcs7")
    signed_manifest = tmp_path / "signed-manifest.json"
    signed_manifest.write_text(
        json.dumps(
            {
                "product": "DcmGet",
                "platform": "windows-x64",
                "channel": "stable",
                "version": "3.6.1",
                "artifacts": [
                    {
                        "name": installer.name,
                        "kind": "full_installer",
                        "size": installer.stat().st_size,
                        "sha256": hashlib.sha256(installer.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "openssl",
        """#!/bin/sh
set -eu
output=''
while [ "$#" -gt 0 ]; do
    if [ "$1" = '-out' ]; then
        output=$2
        shift 2
    else
        shift
    fi
done
cp "$FAKE_SIGNED_MANIFEST" "$output"
""",
    )
    marker = tmp_path / "network-called"
    for command in ("ssh", "scp", "rsync"):
        _write_executable(
            fake_bin / command,
            f"#!/bin/sh\ntouch '{marker}'\nexit 99\n",
        )

    environment = dict(os.environ)
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    environment["FAKE_SIGNED_MANIFEST"] = str(signed_manifest)
    completed = subprocess.run(
        [str(PUBLISH_SCRIPT), str(release_dir), "3.6.0"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
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


def test_publish_still_accepts_signed_full_manifest_before_network(
    tmp_path: Path,
) -> None:
    name = "DcmGet-3.6.0-Setup-x64.exe"
    content = b"signed full payload"
    record = {
        "name": name,
        "kind": "full_installer",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "signature_status": "SIGNED",
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
