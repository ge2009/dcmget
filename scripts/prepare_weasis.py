#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_ROOT = ROOT / "packaging" / "weasis"
DEFAULT_RUNTIME_ROOT = ROOT / ".runtime" / "weasis"
SUPPORTED_PLATFORM = "windows-x86_64"
PAYLOAD_CHECKSUMS = "DCMGET_PAYLOAD.SHA256"


class PreparationError(RuntimeError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def preparation_inputs_sha256() -> str:
    digest = hashlib.sha256()
    for path in (
        Path(__file__).resolve(),
        MANIFEST_ROOT / "LICENSE-Weasis.txt",
        MANIFEST_ROOT / "THIRD_PARTY-Weasis.md",
    ):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_manifest(platform: str = SUPPORTED_PLATFORM) -> dict[str, Any]:
    path = MANIFEST_ROOT / f"{platform}.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreparationError(f"无法读取 Weasis 清单 {path}: {exc}") from exc

    required = {
        "product",
        "version",
        "platform",
        "asset_name",
        "asset_url",
        "asset_size",
        "sha256",
        "license",
        "source_url",
    }
    missing = sorted(required - manifest.keys())
    if missing:
        raise PreparationError(f"Weasis 清单缺少字段：{', '.join(missing)}")
    if manifest["platform"] != platform:
        raise PreparationError("Weasis 清单平台与请求平台不一致")
    checksum = str(manifest["sha256"]).lower()
    if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
        raise PreparationError("Weasis 清单中的 SHA-256 无效")
    return manifest


def _asset_valid(path: Path, manifest: dict[str, Any]) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == int(manifest["asset_size"])
        and file_sha256(path) == str(manifest["sha256"]).lower()
    )


def acquire_asset(
    manifest: dict[str, Any],
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    *,
    offline: bool = False,
    force: bool = False,
) -> Path:
    cache = runtime_root / str(manifest["platform"]) / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / str(manifest["asset_name"])
    if not force and _asset_valid(destination, manifest):
        return destination
    if offline:
        raise PreparationError(f"离线模式下没有有效的 Weasis 缓存：{destination}")

    partial = destination.with_suffix(destination.suffix + ".part")
    partial.unlink(missing_ok=True)
    request = urllib.request.Request(
        str(manifest["asset_url"]), headers={"User-Agent": "DcmGet-Weasis-Preparer/1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
    except (OSError, ValueError) as exc:
        partial.unlink(missing_ok=True)
        raise PreparationError(f"下载 Weasis 失败：{exc}") from exc
    if not _asset_valid(partial, manifest):
        actual = file_sha256(partial) if partial.is_file() else "missing"
        partial.unlink(missing_ok=True)
        raise PreparationError(f"Weasis 校验失败，实际 SHA-256：{actual}")
    partial.replace(destination)
    return destination


def payload_path(
    runtime_root: Path = DEFAULT_RUNTIME_ROOT, platform: str = SUPPORTED_PLATFORM
) -> Path:
    return runtime_root / platform / "Weasis"


def _payload_files(path: Path) -> list[Path]:
    return sorted(
        candidate
        for candidate in path.rglob("*")
        if candidate.is_file() and candidate.name != PAYLOAD_CHECKSUMS
    )


def write_payload_checksums(path: Path) -> None:
    lines = [
        f"{file_sha256(candidate)}  {candidate.relative_to(path).as_posix()}"
        for candidate in _payload_files(path)
    ]
    (path / PAYLOAD_CHECKSUMS).write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def payload_checksums_valid(path: Path) -> bool:
    checksum_path = path / PAYLOAD_CHECKSUMS
    try:
        lines = checksum_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    expected: dict[str, str] = {}
    for line in lines:
        digest, separator, relative = line.partition("  ")
        if (
            not separator
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or not relative
        ):
            return False
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            return False
        expected[candidate.as_posix()] = digest
    try:
        actual = {
            candidate.relative_to(path).as_posix(): file_sha256(candidate)
            for candidate in _payload_files(path)
        }
    except OSError:
        return False
    return bool(actual) and actual == expected


def payload_is_current(path: Path, manifest: dict[str, Any]) -> bool:
    provenance = path / "DCMGET_WEASIS_PAYLOAD.json"
    try:
        recorded = json.loads(provenance.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    try:
        return (
            (path / "Weasis.exe").is_file()
            and (path / "Weasis.exe").stat().st_size > 0
            and (path / "app").is_dir()
            and (path / "runtime").is_dir()
            and (path / "LICENSE-Weasis.txt").is_file()
            and (path / "THIRD_PARTY-Weasis.md").is_file()
            and recorded.get("version") == manifest["version"]
            and recorded.get("source_sha256") == manifest["sha256"]
            and recorded.get("preparation_inputs_sha256")
            == preparation_inputs_sha256()
            and payload_checksums_valid(path)
        )
    except OSError:
        return False


def prepare_windows_payload(
    msi: Path,
    manifest: dict[str, Any],
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    *,
    force: bool = False,
) -> Path:
    if os.name != "nt":
        raise PreparationError(
            "Windows Weasis app-image 只能在 Windows 上提取；本系统请使用 --download-only 缓存资源"
        )
    if not _asset_valid(msi, manifest):
        raise PreparationError("Weasis MSI 未通过固定大小和 SHA-256 校验")
    destination = payload_path(runtime_root, str(manifest["platform"]))
    if not force and payload_is_current(destination, manifest):
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".weasis-prepare-", dir=destination.parent) as raw:
        workspace = Path(raw)
        administrative_image = workspace / "admin"
        administrative_image.mkdir()
        command = [
            "msiexec.exe",
            "/a",
            str(msi.resolve()),
            "/qn",
            "/norestart",
            f"TARGETDIR={administrative_image.resolve()}",
        ]
        completed = subprocess.run(command, check=False)
        if completed.returncode not in {0, 3010}:
            raise PreparationError(
                f"Weasis MSI 管理提取失败，msiexec 返回 {completed.returncode}"
            )

        candidates = sorted(
            (
                exe.parent
                for exe in administrative_image.rglob("Weasis.exe")
                if (exe.parent / "app").is_dir() and (exe.parent / "runtime").is_dir()
            ),
            key=lambda value: (len(value.parts), str(value).lower()),
        )
        if not candidates:
            raise PreparationError("Weasis MSI 中未找到完整的便携 app-image")

        staged = workspace / "Weasis"
        shutil.copytree(candidates[0], staged)
        shutil.copy2(
            MANIFEST_ROOT / "LICENSE-Weasis.txt", staged / "LICENSE-Weasis.txt"
        )
        shutil.copy2(
            MANIFEST_ROOT / "THIRD_PARTY-Weasis.md",
            staged / "THIRD_PARTY-Weasis.md",
        )
        (staged / "DCMGET_WEASIS_PAYLOAD.json").write_text(
            json.dumps(
                {
                    "product": manifest["product"],
                    "version": manifest["version"],
                    "platform": manifest["platform"],
                    "source_asset": manifest["asset_name"],
                    "source_sha256": manifest["sha256"],
                    "source_url": manifest["asset_url"],
                    "preparation_inputs_sha256": preparation_inputs_sha256(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        write_payload_checksums(staged)
        if not payload_is_current(staged, manifest):
            raise PreparationError("Weasis app-image 暂存结果未通过逐文件 SHA-256 校验")

        previous = workspace / "previous"
        if destination.exists():
            destination.replace(previous)
        try:
            staged.replace(destination)
        except OSError:
            if previous.exists() and not destination.exists():
                previous.replace(destination)
            raise
    if not payload_is_current(destination, manifest):
        raise PreparationError("Weasis app-image 发布后的完整性检查失败")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(
        description="下载并校验固定版本的 Windows Weasis 便携查看器资源"
    )
    parser.add_argument("--platform", default=SUPPORTED_PLATFORM, choices=[SUPPORTED_PLATFORM])
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    try:
        manifest = load_manifest(args.platform)
        asset = acquire_asset(
            manifest, args.runtime_root, offline=args.offline, force=args.force
        )
        if args.download_only:
            print(f"已校验并缓存 Weasis 安装资源：{asset}")
            return 0
        destination = payload_path(args.runtime_root, args.platform)
        if not args.force and payload_is_current(destination, manifest):
            print(destination)
            return 0
        print(prepare_windows_payload(asset, manifest, args.runtime_root, force=args.force))
        return 0
    except (PreparationError, OSError, subprocess.SubprocessError) as exc:
        print(f"Weasis 准备失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
