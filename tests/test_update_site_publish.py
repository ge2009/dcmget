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
