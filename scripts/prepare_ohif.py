#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import uuid
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_ROOT = ROOT / "packaging" / "ohif"
MANIFEST_PATH = MANIFEST_ROOT / "ohif-3.12.6.json"
OFFLINE_CONFIG = MANIFEST_ROOT / "app-config.js"
DISABLE_SERVICE_WORKER = MANIFEST_ROOT / "init-service-worker.js"
DEFAULT_RUNTIME_ROOT = ROOT / ".runtime" / "ohif"
PAYLOAD_CHECKSUMS = "DCMGET_PAYLOAD.SHA256"
PROVENANCE_FILE = "DCMGET_OHIF_PAYLOAD.json"
ALLOWED_HTTPS_HOSTS = {"registry.npmjs.org"}


class PreparationError(RuntimeError):
    pass


class _PinnedHttpsRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, hostname: str, port: int):
        super().__init__()
        self.hostname = hostname
        self.port = port

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(urllib.parse.urljoin(req.full_url, newurl))
        port = target.port or 443
        if target.scheme != "https" or target.hostname != self.hostname or port != self.port:
            raise PreparationError("OHIF 下载重定向到非固定 HTTPS 来源")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


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
        OFFLINE_CONFIG,
        DISABLE_SERVICE_WORKER,
        ROOT / "logo.png",
        MANIFEST_ROOT / "LICENSE-OHIF.txt",
        MANIFEST_ROOT / "THIRD_PARTY-OHIF.md",
    ):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreparationError(f"无法读取 OHIF 清单 {path}: {exc}") from exc

    required = {
        "product",
        "package_name",
        "version",
        "asset_name",
        "asset_url",
        "asset_size",
        "sha256",
        "archive_root",
        "license",
        "source_url",
    }
    missing = sorted(required - manifest.keys())
    if missing:
        raise PreparationError(f"OHIF 清单缺少字段：{', '.join(missing)}")
    if manifest["package_name"] != "@ohif/app":
        raise PreparationError("OHIF 清单包名不是 @ohif/app")
    version = _safe_component(str(manifest["version"]), "版本")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise PreparationError(f"OHIF 清单中的版本不是 X.Y.Z：{version}")
    _safe_component(str(manifest["asset_name"]), "资源文件名")
    _safe_archive_path(str(manifest["archive_root"]))
    try:
        if int(manifest["asset_size"]) <= 0:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise PreparationError("OHIF 清单中的资源大小无效") from exc
    checksum = str(manifest["sha256"]).lower()
    if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
        raise PreparationError("OHIF 清单中的 SHA-256 无效")
    return manifest


def _safe_component(value: str, label: str) -> str:
    if not value or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise PreparationError(f"OHIF 清单中的{label}不安全：{value}")
    return value


def _asset_valid(path: Path, manifest: dict[str, Any]) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == int(manifest["asset_size"])
        and file_sha256(path) == str(manifest["sha256"]).lower()
    )


def _validated_asset_url(value: object) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(str(value))
    if parsed.scheme == "file":
        if parsed.hostname not in {None, "", "localhost"}:
            raise PreparationError("OHIF 本地测试文件不能使用远程主机")
        return parsed
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HTTPS_HOSTS:
        raise PreparationError("OHIF 下载地址只允许固定的 npm 官方 HTTPS 主机")
    return parsed


def acquire_asset(
    manifest: dict[str, Any],
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    *,
    offline: bool = False,
    force: bool = False,
) -> Path:
    cache = runtime_root / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / _safe_component(str(manifest["asset_name"]), "资源文件名")
    if not force and _asset_valid(destination, manifest):
        return destination
    if offline:
        raise PreparationError(f"离线模式下没有有效的 OHIF 缓存：{destination}")

    requested_url = _validated_asset_url(manifest["asset_url"])
    partial = cache / f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.part"
    asset_url = str(manifest["asset_url"])
    request = urllib.request.Request(asset_url, headers={"User-Agent": "DcmGet-OHIF-Preparer/1"})
    opener = (
        urllib.request.build_opener(
            _PinnedHttpsRedirectHandler(requested_url.hostname or "", requested_url.port or 443)
        )
        if requested_url.scheme == "https"
        else urllib.request.build_opener()
    )
    expected_size = int(manifest["asset_size"])
    try:
        with opener.open(request, timeout=60) as response, partial.open("wb") as output:
            final_url = urllib.parse.urlsplit(response.geturl())
            if requested_url.scheme == "https" and (
                final_url.scheme != "https"
                or final_url.hostname != requested_url.hostname
                or (final_url.port or 443) != (requested_url.port or 443)
            ):
                raise PreparationError("OHIF 下载发生到非固定 HTTPS 主机的重定向")
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) != expected_size:
                raise PreparationError("OHIF 下载响应大小与清单不一致")
            total = 0
            while True:
                block = response.read(min(1024 * 1024, expected_size + 1 - total))
                if not block:
                    break
                total += len(block)
                if total > expected_size:
                    raise PreparationError("OHIF 下载内容超过清单声明的大小")
                output.write(block)
    except PreparationError:
        partial.unlink(missing_ok=True)
        raise
    except (OSError, ValueError) as exc:
        partial.unlink(missing_ok=True)
        raise PreparationError(f"下载 OHIF 失败：{exc}") from exc
    if not _asset_valid(partial, manifest):
        actual = file_sha256(partial) if partial.is_file() else "missing"
        partial.unlink(missing_ok=True)
        raise PreparationError(f"OHIF 校验失败，实际 SHA-256：{actual}")
    partial.replace(destination)
    return destination


def payload_path(
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    manifest: dict[str, Any] | None = None,
) -> Path:
    selected = manifest or load_manifest()
    version = _safe_component(str(selected["version"]), "版本")
    return runtime_root / f"ohif-{version}"


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
        candidate = PurePosixPath(relative)
        if (
            not separator
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or not relative
            or candidate.is_absolute()
            or ".." in candidate.parts
            or candidate.as_posix() in expected
        ):
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
    try:
        recorded = json.loads((path / PROVENANCE_FILE).read_text(encoding="utf-8"))
        return (
            (path / "index.html").is_file()
            and (path / "app-config.js").read_bytes() == OFFLINE_CONFIG.read_bytes()
            and (path / "init-service-worker.js").read_bytes()
            == DISABLE_SERVICE_WORKER.read_bytes()
            and all(
                not (path / name).exists()
                for name in (
                    "sw.js",
                    "google.js",
                    "oidc-client.min.js",
                    "silent-refresh.html",
                )
            )
            and (path / "LICENSE-OHIF.txt").is_file()
            and (path / "THIRD_PARTY-OHIF.md").is_file()
            and recorded.get("package_name") == manifest["package_name"]
            and recorded.get("version") == manifest["version"]
            and recorded.get("source_sha256") == manifest["sha256"]
            and recorded.get("preparation_inputs_sha256")
            == preparation_inputs_sha256()
            and payload_checksums_valid(path)
        )
    except (OSError, json.JSONDecodeError, KeyError):
        return False


def _safe_archive_path(name: str) -> PurePosixPath:
    if "\\" in name:
        raise PreparationError(f"OHIF 归档包含不安全路径：{name}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise PreparationError(f"OHIF 归档包含不安全路径：{name}")
    return path


def _extract_dist(archive: Path, destination: Path, archive_root: str) -> None:
    root = _safe_archive_path(archive_root)
    found_file = False
    member_count = 0
    extracted_size = 0
    try:
        bundle = tarfile.open(archive, mode="r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise PreparationError(f"无法打开 OHIF npm 归档：{exc}") from exc
    with bundle:
        for member in bundle:
            member_count += 1
            extracted_size += max(0, member.size)
            if member_count > 20_000 or extracted_size > 1024 * 1024 * 1024:
                raise PreparationError("OHIF 归档成员数量或解压体积超过安全上限")
            member_path = _safe_archive_path(member.name)
            if member_path.parts[: len(root.parts)] != root.parts:
                continue
            relative_parts = member_path.parts[len(root.parts) :]
            if not relative_parts:
                continue
            target = destination.joinpath(*relative_parts)
            try:
                target.resolve().relative_to(destination.resolve())
            except ValueError as exc:
                raise PreparationError(
                    f"OHIF 归档路径超出目标目录：{member.name}"
                ) from exc
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise PreparationError(f"OHIF dist 包含不支持的链接或设备：{member.name}")
            source = bundle.extractfile(member)
            if source is None:
                raise PreparationError(f"无法读取 OHIF 归档成员：{member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            found_file = True
    if not found_file or not (destination / "index.html").is_file():
        raise PreparationError("OHIF npm 归档中没有完整的 package/dist")


def prepare_payload(
    archive: Path,
    manifest: dict[str, Any],
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    *,
    force: bool = False,
) -> Path:
    if not _asset_valid(archive, manifest):
        raise PreparationError("OHIF npm 归档未通过固定大小和 SHA-256 校验")
    destination = payload_path(runtime_root, manifest)
    if not force and payload_is_current(destination, manifest):
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".ohif-prepare-", dir=destination.parent) as raw:
        workspace = Path(raw)
        staged = workspace / destination.name
        staged.mkdir()
        _extract_dist(archive, staged, str(manifest["archive_root"]))
        shutil.copy2(OFFLINE_CONFIG, staged / "app-config.js")
        shutil.copy2(DISABLE_SERVICE_WORKER, staged / "init-service-worker.js")
        for unused_online_file in (
            "sw.js",
            "google.js",
            "oidc-client.min.js",
            "silent-refresh.html",
        ):
            (staged / unused_online_file).unlink(missing_ok=True)
        (staged / "assets").mkdir(exist_ok=True)
        shutil.copy2(ROOT / "logo.png", staged / "assets" / "dcmget-logo.png")
        shutil.copy2(MANIFEST_ROOT / "LICENSE-OHIF.txt", staged / "LICENSE-OHIF.txt")
        shutil.copy2(
            MANIFEST_ROOT / "THIRD_PARTY-OHIF.md", staged / "THIRD_PARTY-OHIF.md"
        )
        (staged / PROVENANCE_FILE).write_text(
            json.dumps(
                {
                    "product": manifest["product"],
                    "package_name": manifest["package_name"],
                    "version": manifest["version"],
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
            raise PreparationError("OHIF 暂存结果未通过逐文件 SHA-256 校验")

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
        raise PreparationError("OHIF 发布后的完整性检查失败")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(
        description="下载、校验并安全解包固定版本的 OHIF Viewer 离线资源"
    )
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    try:
        manifest = load_manifest()
        archive = acquire_asset(
            manifest, args.runtime_root, offline=args.offline, force=args.force
        )
        print(prepare_payload(archive, manifest, args.runtime_root, force=args.force))
        return 0
    except (PreparationError, OSError, tarfile.TarError) as exc:
        print(f"OHIF 准备失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
