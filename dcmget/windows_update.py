from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Mapping, Protocol, Sequence

from .update_signing import (
    MAX_ENVELOPE_BYTES,
    UpdateSigningError,
    verify_manifest,
)
from .update_trust import TRUSTED_UPDATE_PUBLIC_KEYS


PRODUCT = "DcmGet"
PLATFORM = "windows-x64"
SIGNED_MANIFEST_NAME = "UPDATE-MANIFEST.signed.json"
PATCH_MANIFEST_NAME = "PATCH-MANIFEST.json"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024 * 1024
_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_GITHUB_RELEASE_TAG_PATTERN = re.compile(
    r"^(?:v|component-v)(\d+\.\d+\.\d+)$"
)
_SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_GITHUB_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_COMPONENT_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_ALLOWED_GITHUB_HOSTS = {"api.github.com", "github.com"}
STATIC_MIRROR_BASE_URL = "https://dcmget.v2ex.com.cn/updates/"
_STATIC_MIRROR_HOST = "dcmget.v2ex.com.cn"
_STATIC_MIRROR_MANIFEST_URL = (
    f"{STATIC_MIRROR_BASE_URL}stable/{SIGNED_MANIFEST_NAME}"
)
_PROTECTED_COMPONENT_ROOTS = (
    "config",
    "state",
    "downloads",
    "logs",
    "tasks",
    "license",
)


class WindowsUpdateError(RuntimeError):
    pass


class UpdateNetworkError(WindowsUpdateError):
    pass


class UpdateSecurityError(WindowsUpdateError):
    pass


class UpdatePolicy(str, Enum):
    AUTOMATIC = "automatic"
    DISABLED = "disabled"

    @classmethod
    def parse(cls, value: str | "UpdatePolicy") -> "UpdatePolicy":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise WindowsUpdateError(f"不支持的更新策略：{value}") from exc


class UpdatePhase(str, Enum):
    DISABLED = "disabled"
    UNSUPPORTED = "unsupported"
    IDLE = "idle"
    CHECKING = "checking"
    UP_TO_DATE = "up_to_date"
    AVAILABLE = "available"
    DOWNLOADING = "downloading"
    READY = "ready"
    APPLYING = "applying"
    OFFLINE = "offline"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class UpdateAsset:
    name: str
    kind: str
    size: int
    sha256: str
    download_url: str
    signature_status: str = "NOT_APPLICABLE"
    base_version: str | None = None
    preserves_user_data: bool = False
    content_scope: str = ""
    component_files: tuple["ComponentFile", ...] = ()
    base_tree_sha256: str = ""
    target_tree_sha256: str = ""

    @property
    def is_component_patch(self) -> bool:
        return self.kind == "component_patch"

    @property
    def is_full_installer(self) -> bool:
        return self.kind == "full_installer"


@dataclass(frozen=True, slots=True)
class UpdateCandidate:
    version: str
    assets: tuple[UpdateAsset, ...]
    release_url: str = ""

    def preferred_asset(
        self,
        current_version: str,
        *,
        allow_component_patch: bool = True,
    ) -> UpdateAsset:
        matching_patches = [
            asset
            for asset in self.assets
            if allow_component_patch
            and asset.is_component_patch
            and asset.base_version == current_version
        ]
        if matching_patches:
            return matching_patches[0]
        installers = [asset for asset in self.assets if asset.is_full_installer]
        if installers:
            return installers[0]
        raise UpdateSecurityError(
            f"版本 {self.version} 没有适用于 {current_version} 的增量包或完整安装包"
        )


@dataclass(frozen=True, slots=True)
class ComponentFile:
    path: str
    size: int
    sha256: str
    base_size: int | None = None
    base_sha256: str | None = None
    base_missing: bool = False


@dataclass(frozen=True, slots=True)
class ApplyRequest:
    package_path: Path
    current_version: str
    target_version: str
    asset_kind: str
    component_files: tuple[ComponentFile, ...] = ()
    base_tree_sha256: str = ""
    target_tree_sha256: str = ""
    protected_roots: tuple[str, ...] = _PROTECTED_COMPONENT_ROOTS


class ReleaseSource(Protocol):
    def fetch_latest(self) -> UpdateCandidate: ...

    def download_asset(self, asset: UpdateAsset, destination: Path) -> None: ...


class UpdateScheduler(Protocol):
    supports_component_patch: bool

    def schedule(self, request: ApplyRequest) -> None: ...


SignedManifestVerifier = Callable[[bytes], bytes]
AuthenticodeVerifier = Callable[[Path, UpdateAsset], None]
UrlOpen = Callable[..., BinaryIO]


def parse_version(value: str) -> tuple[int, int, int]:
    match = _VERSION_PATTERN.fullmatch(str(value).strip())
    if match is None:
        raise UpdateSecurityError(f"版本号必须采用 X.Y.Z 格式：{value}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_allowed_github_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and (
            hostname in _ALLOWED_GITHUB_HOSTS
            or hostname.endswith(".githubusercontent.com")
        )
    )


def _static_mirror_asset_parts(value: str) -> tuple[str, str] | None:
    try:
        parsed = urllib.parse.urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.netloc != _STATIC_MIRROR_HOST
            or parsed.hostname != _STATIC_MIRROR_HOST
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or "%" in parsed.path
            or "\\" in parsed.path
        ):
            return None
    except ValueError:
        return None
    segments = parsed.path.split("/")
    if len(segments) != 5 or segments[:3] != ["", "updates", "releases"]:
        return None
    version, name = segments[3], segments[4]
    if _VERSION_PATTERN.fullmatch(version) is None:
        return None
    if _SAFE_NAME_PATTERN.fullmatch(name) is None:
        return None
    return version, name


def _is_allowed_static_mirror_asset_url(value: str) -> bool:
    return _static_mirror_asset_parts(value) is not None


def _is_allowed_update_asset_url(value: str) -> bool:
    return _is_allowed_github_url(value) or _is_allowed_static_mirror_asset_url(
        value
    )


def _normalise_asset_kind(value: object) -> str:
    kind = str(value or "").strip().lower()
    if kind in {"component_patch", "component-patch", "patch"}:
        return "component_patch"
    if kind in {"full_installer", "installer"}:
        return "full_installer"
    return ""


def _validated_candidate(
    manifest: Mapping[str, object],
    release_assets: Mapping[str, str],
    *,
    release_url: str,
    allowed_asset_url: Callable[[str], bool] = _is_allowed_github_url,
) -> UpdateCandidate:
    if manifest.get("schema_version") != 1:
        raise UpdateSecurityError("更新清单 schema_version 不受支持")
    if manifest.get("product") != PRODUCT:
        raise UpdateSecurityError("更新清单 product 不是 DcmGet")
    if manifest.get("platform") != PLATFORM:
        raise UpdateSecurityError("更新清单 platform 不是 windows-x64")
    if manifest.get("channel") != "stable":
        raise UpdateSecurityError("更新清单不是 stable 渠道")
    version = str(manifest.get("version", ""))
    parse_version(version)
    records = manifest.get("artifacts")
    if not isinstance(records, list) or not records:
        raise UpdateSecurityError("更新清单缺少 artifacts")

    assets: list[UpdateAsset] = []
    seen_names: set[str] = set()
    for raw_record in records:
        if not isinstance(raw_record, dict):
            raise UpdateSecurityError("更新清单中的 artifact 格式无效")
        kind = _normalise_asset_kind(raw_record.get("kind"))
        if not kind:
            continue
        name = str(raw_record.get("name", ""))
        if _SAFE_NAME_PATTERN.fullmatch(name) is None or name in seen_names:
            raise UpdateSecurityError(f"更新包文件名无效或重复：{name}")
        url = release_assets.get(name, "")
        if not url or not allowed_asset_url(url):
            raise UpdateSecurityError(f"更新包缺少可信 HTTPS 下载地址：{name}")
        try:
            size = int(raw_record.get("size", 0))
        except (TypeError, ValueError) as exc:
            raise UpdateSecurityError(f"更新包大小无效：{name}") from exc
        if not 0 < size <= MAX_ARTIFACT_BYTES:
            raise UpdateSecurityError(f"更新包大小超出允许范围：{name}")
        sha256 = str(raw_record.get("sha256", "")).lower()
        if _SHA256_PATTERN.fullmatch(sha256) is None:
            raise UpdateSecurityError(f"更新包 SHA-256 无效：{name}")

        signature_status = str(
            raw_record.get("signature_status", "NOT_APPLICABLE")
        ).upper()
        base_version = raw_record.get("base_version") or raw_record.get(
            "from_version"
        )
        preserves_user_data = raw_record.get("preserves_user_data") is True
        content_scope = str(raw_record.get("content_scope", "")).lower()
        component_files: tuple[ComponentFile, ...] = ()
        base_tree_sha256 = ""
        target_tree_sha256 = ""
        if kind == "component_patch":
            base_version = str(base_version or "")
            parse_version(base_version)
            if not preserves_user_data or content_scope != "application":
                raise UpdateSecurityError(
                    f"增量包 {name} 必须限定为 application 范围并保留用户数据"
                )
            component_files = _parse_component_files(raw_record.get("files"), name)
            base_tree_sha256 = str(
                raw_record.get("base_tree_sha256", "")
            ).lower()
            target_tree_sha256 = str(
                raw_record.get("target_tree_sha256", "")
            ).lower()
            if (
                _SHA256_PATTERN.fullmatch(base_tree_sha256) is None
                or _SHA256_PATTERN.fullmatch(target_tree_sha256) is None
            ):
                raise UpdateSecurityError(f"增量包 {name} 的应用树指纹无效")
        elif signature_status not in {"SIGNED", "UNSIGNED"}:
            raise UpdateSecurityError(
                f"完整安装包签名状态必须为 SIGNED 或 UNSIGNED：{name}"
            )

        seen_names.add(name)
        assets.append(
            UpdateAsset(
                name=name,
                kind=kind,
                size=size,
                sha256=sha256,
                download_url=url,
                signature_status=signature_status,
                base_version=str(base_version) if base_version else None,
                preserves_user_data=preserves_user_data,
                content_scope=content_scope,
                component_files=component_files,
                base_tree_sha256=base_tree_sha256,
                target_tree_sha256=target_tree_sha256,
            )
        )
    if not assets:
        raise UpdateSecurityError("更新清单没有受支持的 Windows 更新包")
    return UpdateCandidate(
        version=version,
        assets=tuple(assets),
        release_url=release_url,
    )


def _parse_component_files(value: object, asset_name: str) -> tuple[ComponentFile, ...]:
    if not isinstance(value, list) or not value:
        raise UpdateSecurityError(f"增量包 {asset_name} 缺少 files 清单")
    files: list[ComponentFile] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise UpdateSecurityError(f"增量包 {asset_name} 的 files 格式无效")
        path = str(item.get("path", "")).replace("\\", "/")
        canonical = path.casefold()
        if not _is_allowed_component_path(path) or canonical in seen:
            raise UpdateSecurityError(f"增量包文件路径越界或重复：{path}")
        try:
            size = int(item.get("size", -1))
        except (TypeError, ValueError) as exc:
            raise UpdateSecurityError(f"增量文件大小无效：{path}") from exc
        sha256 = str(item.get("sha256", "")).lower()
        if size < 0 or size > MAX_ARTIFACT_BYTES:
            raise UpdateSecurityError(f"增量文件大小超出范围：{path}")
        if _SHA256_PATTERN.fullmatch(sha256) is None:
            raise UpdateSecurityError(f"增量文件 SHA-256 无效：{path}")
        base_missing = item.get("base_missing") is True
        base_size_value = item.get("base_size")
        base_sha256 = str(item.get("base_sha256", "")).lower()
        if base_missing:
            if base_size_value is not None or base_sha256:
                raise UpdateSecurityError(f"新增文件基础状态声明冲突：{path}")
            base_size = None
            base_sha256_value = None
        else:
            try:
                base_size = int(base_size_value)
            except (TypeError, ValueError) as exc:
                raise UpdateSecurityError(f"增量文件缺少基础大小：{path}") from exc
            if base_size < 0 or _SHA256_PATTERN.fullmatch(base_sha256) is None:
                raise UpdateSecurityError(f"增量文件基础指纹无效：{path}")
            base_sha256_value = base_sha256
        seen.add(canonical)
        files.append(
            ComponentFile(
                path=path,
                size=size,
                sha256=sha256,
                base_size=base_size,
                base_sha256=base_sha256_value,
                base_missing=base_missing,
            )
        )
    return tuple(files)


def _is_allowed_component_path(value: str) -> bool:
    path = PurePosixPath(value)
    if (
        not value
        or value.startswith(("/", "\\"))
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or ":" in value
        or any(
            _COMPONENT_SEGMENT_PATTERN.fullmatch(part) is None
            or part.endswith((".", " "))
            for part in path.parts
        )
    ):
        return False
    return value.casefold() == "dcmget.exe" or (
        len(path.parts) > 1 and path.parts[0].casefold() == "_internal"
    )


class StaticMirrorReleaseSource:
    """Read the signed manifest from the fixed HTTPS update mirror."""

    def __init__(
        self,
        *,
        signed_manifest_verifier: SignedManifestVerifier,
        urlopen: UrlOpen = urllib.request.urlopen,
        timeout_seconds: float = 3.0,
    ) -> None:
        self._verifier = signed_manifest_verifier
        self._urlopen = urlopen
        self._timeout = max(float(timeout_seconds), 0.5)

    def fetch_latest(self) -> UpdateCandidate:
        signed_payload = self._read_bytes(
            _STATIC_MIRROR_MANIFEST_URL,
            MAX_ENVELOPE_BYTES,
        )
        try:
            verified_payload = self._verifier(signed_payload)
        except UpdateSecurityError:
            raise
        except Exception as exc:
            raise UpdateSecurityError(f"更新清单签名验证失败：{exc}") from exc
        try:
            manifest = json.loads(verified_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateSecurityError("签名更新清单不是有效 UTF-8 JSON") from exc
        if not isinstance(manifest, dict):
            raise UpdateSecurityError("签名更新清单根节点必须是对象")

        version = str(manifest.get("version", ""))
        parse_version(version)
        asset_urls: dict[str, str] = {}
        records = manifest.get("artifacts")
        if isinstance(records, list):
            for record in records:
                if not isinstance(record, dict):
                    continue
                name = str(record.get("name", ""))
                if _SAFE_NAME_PATTERN.fullmatch(name) is not None:
                    asset_urls[name] = (
                        f"{STATIC_MIRROR_BASE_URL}releases/{version}/{name}"
                    )
        return _validated_candidate(
            manifest,
            asset_urls,
            release_url=f"{STATIC_MIRROR_BASE_URL}releases/{version}/",
            allowed_asset_url=_is_allowed_static_mirror_asset_url,
        )

    def download_asset(self, asset: UpdateAsset, destination: Path) -> None:
        parts = _static_mirror_asset_parts(asset.download_url)
        if parts is None or parts[1] != asset.name:
            raise UpdateSecurityError("拒绝从静态镜像路径边界外下载更新")
        request = self._request(asset.download_url)
        try:
            with self._urlopen(request, timeout=self._timeout) as response:
                self._validate_final_url(response, asset.download_url)
                with destination.open("xb") as output:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
        except FileExistsError:
            raise
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise UpdateNetworkError(f"静态镜像更新包下载失败：{exc}") from exc

    def _read_bytes(self, url: str, limit: int) -> bytes:
        if url != _STATIC_MIRROR_MANIFEST_URL:
            raise UpdateSecurityError("拒绝访问静态镜像清单路径边界外的地址")
        request = self._request(url)
        try:
            with self._urlopen(request, timeout=self._timeout) as response:
                self._validate_final_url(response, url)
                content = response.read(limit + 1)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise UpdateNetworkError(f"静态更新镜像连接失败：{exc}") from exc
        if len(content) > limit:
            raise UpdateSecurityError("静态更新镜像响应超过大小限制")
        return content

    @staticmethod
    def _request(url: str) -> urllib.request.Request:
        return urllib.request.Request(
            url,
            headers={
                "Accept": "application/json, application/octet-stream",
                "User-Agent": "DcmGet-Windows-Updater",
            },
        )

    @staticmethod
    def _validate_final_url(response: object, expected_url: str) -> None:
        geturl = getattr(response, "geturl", None)
        if not callable(geturl):
            raise UpdateSecurityError("静态更新镜像响应缺少最终地址")
        final_url = str(geturl())
        if final_url != expected_url:
            raise UpdateSecurityError("静态更新镜像发生越界重定向")
        if expected_url == _STATIC_MIRROR_MANIFEST_URL:
            return
        if not _is_allowed_static_mirror_asset_url(expected_url):
            raise UpdateSecurityError("静态更新镜像最终地址无效")


class GitHubReleaseSource:
    """Read a stable GitHub Release whose update manifest is PKCS#7 signed."""

    def __init__(
        self,
        *,
        signed_manifest_verifier: SignedManifestVerifier,
        owner: str = "ge2009",
        repository: str = "dcmget",
        urlopen: UrlOpen = urllib.request.urlopen,
        timeout_seconds: float = 15.0,
    ) -> None:
        if (
            _GITHUB_NAME_PATTERN.fullmatch(owner) is None
            or _GITHUB_NAME_PATTERN.fullmatch(repository) is None
        ):
            raise ValueError("GitHub owner/repository 格式无效")
        self._verifier = signed_manifest_verifier
        self._urlopen = urlopen
        self._timeout = max(float(timeout_seconds), 1.0)
        self._api_url = (
            f"https://api.github.com/repos/{owner}/{repository}/releases/latest"
        )

    def fetch_latest(self) -> UpdateCandidate:
        release = self._read_json(self._api_url, MAX_MANIFEST_BYTES)
        if release.get("draft") is True or release.get("prerelease") is True:
            raise UpdateSecurityError("GitHub latest release 不是稳定正式版本")
        release_url = str(release.get("html_url", ""))
        if release_url and not _is_allowed_github_url(release_url):
            raise UpdateSecurityError("GitHub release 地址无效")
        raw_assets = release.get("assets")
        if not isinstance(raw_assets, list):
            raise UpdateSecurityError("GitHub release 缺少 assets")
        asset_urls: dict[str, str] = {}
        for item in raw_assets:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", ""))
            url = str(item.get("browser_download_url", ""))
            if name in asset_urls:
                raise UpdateSecurityError(f"GitHub release 资产重名：{name}")
            if name:
                asset_urls[name] = url
        signed_url = asset_urls.get(SIGNED_MANIFEST_NAME, "")
        if not signed_url or not _is_allowed_github_url(signed_url):
            raise UpdateSecurityError(
                f"GitHub release 缺少 {SIGNED_MANIFEST_NAME}"
            )
        signed_payload = self._read_bytes(signed_url, MAX_MANIFEST_BYTES)
        try:
            verified_payload = self._verifier(signed_payload)
        except WindowsUpdateError:
            raise
        except Exception as exc:
            raise UpdateSecurityError(f"更新清单签名验证失败：{exc}") from exc
        try:
            manifest = json.loads(verified_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateSecurityError("签名更新清单不是有效 UTF-8 JSON") from exc
        if not isinstance(manifest, dict):
            raise UpdateSecurityError("签名更新清单根节点必须是对象")
        candidate = _validated_candidate(
            manifest,
            asset_urls,
            release_url=release_url,
        )
        tag = str(release.get("tag_name", ""))
        tag_match = _GITHUB_RELEASE_TAG_PATTERN.fullmatch(tag)
        if tag_match is None or tag_match.group(1) != candidate.version:
            raise UpdateSecurityError("GitHub release 标签与签名清单版本不一致")
        return candidate

    def download_asset(self, asset: UpdateAsset, destination: Path) -> None:
        if not _is_allowed_github_url(asset.download_url):
            raise UpdateSecurityError("拒绝从非 GitHub HTTPS 地址下载更新")
        request = self._request(asset.download_url)
        try:
            with self._urlopen(request, timeout=self._timeout) as response:
                self._validate_final_url(response)
                with destination.open("xb") as output:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
        except FileExistsError:
            raise
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise UpdateNetworkError(f"更新包下载失败：{exc}") from exc

    def _read_json(self, url: str, limit: int) -> dict[str, object]:
        content = self._read_bytes(url, limit)
        try:
            value = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateSecurityError("GitHub release 响应不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise UpdateSecurityError("GitHub release 响应根节点必须是对象")
        return value

    def _read_bytes(self, url: str, limit: int) -> bytes:
        if not _is_allowed_github_url(url):
            raise UpdateSecurityError("拒绝访问非 GitHub HTTPS 地址")
        request = self._request(url)
        try:
            with self._urlopen(request, timeout=self._timeout) as response:
                self._validate_final_url(response)
                content = response.read(limit + 1)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise UpdateNetworkError(f"更新服务器连接失败：{exc}") from exc
        if len(content) > limit:
            raise UpdateSecurityError("更新服务器响应超过大小限制")
        return content

    @staticmethod
    def _request(url: str) -> urllib.request.Request:
        return urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "DcmGet-Windows-Updater",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    @staticmethod
    def _validate_final_url(response: object) -> None:
        geturl = getattr(response, "geturl", None)
        if callable(geturl) and not _is_allowed_github_url(str(geturl())):
            raise UpdateSecurityError("更新下载被重定向到非 GitHub HTTPS 地址")


class MirrorFirstReleaseSource:
    """Prefer the static mirror and use GitHub only when it is unavailable."""

    def __init__(
        self,
        mirror: ReleaseSource,
        github: ReleaseSource,
    ) -> None:
        self._mirror = mirror
        self._github = github

    def fetch_latest(self) -> UpdateCandidate:
        try:
            return self._mirror.fetch_latest()
        except UpdateNetworkError:
            return self._github.fetch_latest()

    def download_asset(self, asset: UpdateAsset, destination: Path) -> None:
        mirror_parts = _static_mirror_asset_parts(asset.download_url)
        if mirror_parts is not None:
            if mirror_parts[1] != asset.name:
                raise UpdateSecurityError("静态镜像更新包名称与下载地址不一致")
            destination_preexisted = destination.exists() or destination.is_symlink()
            try:
                self._mirror.download_asset(asset, destination)
            except UpdateNetworkError as mirror_error:
                if destination_preexisted:
                    raise UpdateSecurityError(
                        "镜像下载失败后的回退目标已预先存在"
                    ) from mirror_error
                self._remove_failed_download(destination)
                fallback = self._matching_github_asset(
                    mirror_parts[0],
                    asset,
                )
                self._github.download_asset(fallback, destination)
            return
        if _is_allowed_github_url(asset.download_url):
            self._github.download_asset(asset, destination)
            return
        raise UpdateSecurityError("更新包下载地址不属于可信更新源")

    def _matching_github_asset(
        self,
        version: str,
        mirror_asset: UpdateAsset,
    ) -> UpdateAsset:
        candidate = self._github.fetch_latest()
        if candidate.version != version:
            raise UpdateSecurityError("GitHub 兜底版本与镜像版本不一致")
        matching = [
            asset for asset in candidate.assets if asset.name == mirror_asset.name
        ]
        if len(matching) != 1:
            raise UpdateSecurityError("GitHub 兜底缺少唯一的同名更新包")
        github_asset = matching[0]
        if not _is_allowed_github_url(github_asset.download_url):
            raise UpdateSecurityError("GitHub 兜底更新包地址无效")
        # The source URL is the only field allowed to differ. Dataclass
        # equality keeps every signed patch boundary and file hash identical.
        if replace(
            github_asset,
            download_url=mirror_asset.download_url,
        ) != mirror_asset:
            raise UpdateSecurityError("GitHub 兜底更新包与镜像签名清单不一致")
        return github_asset

    @staticmethod
    def _remove_failed_download(destination: Path) -> None:
        if not destination.exists() and not destination.is_symlink():
            return
        if destination.is_symlink() or not destination.is_file():
            raise UpdateSecurityError("镜像下载失败后发现不安全的暂存目标")
        try:
            destination.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise UpdateSecurityError(
                f"无法清理镜像下载残留文件：{exc}"
            ) from exc


class Ed25519SignedManifestVerifier:
    """Verify the single-file Ed25519 envelope against embedded public keys."""

    def __init__(
        self,
        trusted_public_keys: Mapping[str, bytes] = TRUSTED_UPDATE_PUBLIC_KEYS,
    ) -> None:
        self._trusted_public_keys = dict(trusted_public_keys)

    def __call__(self, signed_payload: bytes) -> bytes:
        try:
            return verify_manifest(
                signed_payload,
                self._trusted_public_keys,
                max_manifest_bytes=MAX_MANIFEST_BYTES,
            )
        except UpdateSigningError as exc:
            raise UpdateSecurityError(str(exc)) from exc


class WindowsSignedManifestVerifier:
    """Verify embedded PKCS#7 and bind its signer to the installed executable."""

    _SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$signature = Get-AuthenticodeSignature -LiteralPath $env:DCMGET_CURRENT_EXE
if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
    throw "Installed DcmGet signature is $($signature.Status)"
}
if ($null -eq $signature.SignerCertificate) { throw 'Installed signer is missing' }
$cms = New-Object System.Security.Cryptography.Pkcs.SignedCms
$cms.Decode([IO.File]::ReadAllBytes($env:DCMGET_UPDATE_P7))
$cms.CheckSignature($true)
if ($cms.SignerInfos.Count -ne 1) { throw 'Update manifest must have one signer' }
$signer = $cms.SignerInfos[0].Certificate
if ($null -eq $signer) { throw 'Update manifest signer is missing' }
if ($signer.Thumbprint -ne $signature.SignerCertificate.Thumbprint) {
    throw 'Update manifest signer does not match installed DcmGet'
}
[IO.File]::WriteAllBytes($env:DCMGET_UPDATE_JSON, $cms.ContentInfo.Content)
""".strip()

    def __init__(
        self,
        current_executable: str | Path,
        working_directory: str | Path,
        *,
        platform_name: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._current_executable = Path(current_executable).resolve()
        self._working_directory = Path(working_directory).resolve()
        self._platform = sys.platform if platform_name is None else platform_name
        self._runner = runner

    def __call__(self, signed_payload: bytes) -> bytes:
        if self._platform != "win32":
            raise UpdateSecurityError("PKCS#7 更新验签只允许在 Windows 上执行")
        if not self._current_executable.is_file():
            raise UpdateSecurityError("找不到当前 DcmGet 可执行文件，无法核对签名者")
        self._working_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(
            prefix="manifest-", dir=self._working_directory
        ) as temporary:
            root = Path(temporary)
            signed_path = root / SIGNED_MANIFEST_NAME
            output_path = root / "UPDATE-MANIFEST.json"
            signed_path.write_bytes(signed_payload)
            environment = dict(os.environ)
            environment.update(
                {
                    "DCMGET_CURRENT_EXE": str(self._current_executable),
                    "DCMGET_UPDATE_P7": str(signed_path),
                    "DCMGET_UPDATE_JSON": str(output_path),
                }
            )
            command = [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                self._SCRIPT,
            ]
            try:
                completed = self._runner(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    env=environment,
                )
            except OSError as exc:
                raise UpdateSecurityError(f"无法启动 Windows 更新验签：{exc}") from exc
            if completed.returncode:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise UpdateSecurityError(
                    "更新清单签名无效" + (f"：{detail}" if detail else "")
                )
            if not output_path.is_file():
                raise UpdateSecurityError("PKCS#7 验签未输出更新清单")
            payload = output_path.read_bytes()
        if not payload or len(payload) > MAX_MANIFEST_BYTES:
            raise UpdateSecurityError("PKCS#7 内嵌更新清单大小无效")
        return payload


class WindowsAuthenticodeVerifier:
    """Validate an installer and require the same signer as installed DcmGet."""

    _SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$current = Get-AuthenticodeSignature -LiteralPath $env:DCMGET_CURRENT_EXE
$candidate = Get-AuthenticodeSignature -LiteralPath $env:DCMGET_UPDATE_EXE
if ($current.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
    throw "Installed signature is $($current.Status)"
}
if ($candidate.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
    throw "Update signature is $($candidate.Status)"
}
if ($null -eq $current.SignerCertificate -or $null -eq $candidate.SignerCertificate) {
    throw 'Signer certificate is missing'
}
if ($current.SignerCertificate.Thumbprint -ne $candidate.SignerCertificate.Thumbprint) {
    throw 'Update signer does not match installed DcmGet'
}
""".strip()

    def __init__(
        self,
        current_executable: str | Path,
        *,
        platform_name: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._current_executable = Path(current_executable).resolve()
        self._platform = sys.platform if platform_name is None else platform_name
        self._runner = runner

    def __call__(self, path: Path, asset: UpdateAsset) -> None:
        if not asset.is_full_installer:
            return
        if self._platform != "win32":
            raise UpdateSecurityError("Authenticode 校验只允许在 Windows 上执行")
        environment = dict(os.environ)
        environment.update(
            {
                "DCMGET_CURRENT_EXE": str(self._current_executable),
                "DCMGET_UPDATE_EXE": str(path.resolve()),
            }
        )
        command = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            self._SCRIPT,
        ]
        try:
            completed = self._runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                env=environment,
            )
        except OSError as exc:
            raise UpdateSecurityError(f"无法启动 Authenticode 校验：{exc}") from exc
        if completed.returncode:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise UpdateSecurityError(
                "安装包 Authenticode 校验失败" + (f"：{detail}" if detail else "")
            )


class SecureStagingDirectory:
    """Own updater-only storage; callers may inject a Windows ACL hardener."""

    def __init__(
        self,
        root: str | Path,
        *,
        access_controller: Callable[[Path], None] | None = None,
    ) -> None:
        # Do not call resolve() here: resolving first would hide a caller-
        # supplied symlink/junction before we can reject it.
        self.root = Path(os.path.abspath(Path(root).expanduser()))
        self._access_controller = access_controller

    def prepare(self, version: str) -> Path:
        parse_version(version)
        self._ensure_directory(self.root)
        destination = self.root / version
        self._ensure_directory(destination)
        return destination

    def _ensure_directory(self, path: Path) -> None:
        if path.exists() and (_is_reparse_point(path) or not path.is_dir()):
            raise UpdateSecurityError(f"更新暂存路径类型不安全：{path}")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if _is_reparse_point(path):
            raise UpdateSecurityError(f"更新暂存路径不能是链接或重解析点：{path}")
        try:
            path.chmod(0o700)
        except OSError:
            pass
        if self._access_controller is not None:
            self._access_controller(path)


class WindowsDirectoryAcl:
    """Restrict updater storage to SYSTEM and local Administrators."""

    def __init__(
        self,
        *,
        platform_name: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._platform = sys.platform if platform_name is None else platform_name
        self._runner = runner

    def __call__(self, path: Path) -> None:
        if self._platform != "win32":
            return
        command = [
            "icacls.exe",
            str(path),
            "/inheritance:r",
            "/grant:r",
            "*S-1-5-18:(OI)(CI)F",
            "*S-1-5-32-544:(OI)(CI)F",
        ]
        try:
            completed = self._runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as exc:
            raise UpdateSecurityError(f"无法收紧更新目录权限：{exc}") from exc
        if completed.returncode:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise UpdateSecurityError(
                "无法收紧更新目录权限" + (f"：{detail}" if detail else "")
            )


class WindowsScheduledTaskUpdater:
    """Schedule the signed installer outside the WinSW service process tree."""

    def __init__(
        self,
        staging: SecureStagingDirectory,
        *,
        install_directory: str | Path | None = None,
        platform_name: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._staging = staging
        self._install_directory = (
            None
            if install_directory is None
            else Path(install_directory).expanduser().resolve()
        )
        self._platform = sys.platform if platform_name is None else platform_name
        self._runner = runner
        self._now = now or (lambda: datetime.now(timezone.utc))

    @property
    def supports_component_patch(self) -> bool:
        return self._install_directory is not None

    def can_apply_component(
        self,
        files: Sequence[ComponentFile],
        base_tree_sha256: str,
    ) -> bool:
        if self._install_directory is None:
            return False
        try:
            _validate_installed_base(self._install_directory, files)
            _validate_installed_tree(
                self._install_directory,
                base_tree_sha256,
                "基础",
            )
        except UpdateSecurityError:
            return False
        return True

    def schedule(self, request: ApplyRequest) -> None:
        if request.asset_kind not in {"full_installer", "component_patch"}:
            raise WindowsUpdateError(f"不支持的更新包类型：{request.asset_kind}")
        if self._platform != "win32":
            raise WindowsUpdateError("Windows 更新任务只能在 Windows 上创建")
        package_input = request.package_path
        if package_input.is_symlink() or not package_input.is_file():
            raise UpdateSecurityError("待安装的更新包不存在或类型无效")
        package = package_input.resolve()
        root = self._staging.prepare(request.target_version)
        try:
            package.relative_to(root)
        except ValueError as exc:
            raise UpdateSecurityError("更新包不在受保护的暂存目录中") from exc
        task_id = uuid.uuid4().hex
        task_name = f"DcmGet-ApplyUpdate-{task_id}"
        task_file = root / f"apply-{task_id}.xml"
        log_file = root / f"install-{request.target_version}.log"
        if request.asset_kind == "component_patch":
            if self._install_directory is None:
                raise WindowsUpdateError("组件更新缺少受控安装目录")
            _validate_installed_base(self._install_directory, request.component_files)
            _validate_installed_tree(
                self._install_directory,
                request.base_tree_sha256,
                "基础",
            )
            _validate_component_archive(
                package,
                request.component_files,
                base_version=request.current_version,
                target_version=request.target_version,
                base_tree_sha256=request.base_tree_sha256,
                target_tree_sha256=request.target_tree_sha256,
            )
            payload_root = root / f"payload-{task_id}"
            _extract_component_archive(
                package,
                payload_root,
                request.component_files,
                base_version=request.current_version,
                target_version=request.target_version,
                base_tree_sha256=request.base_tree_sha256,
                target_tree_sha256=request.target_tree_sha256,
            )
            plan_path = root / f"plan-{task_id}.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "files": [
                            {
                                "path": item.path,
                                "size": item.size,
                                "sha256": item.sha256,
                                "base_size": item.base_size,
                                "base_sha256": item.base_sha256,
                                "base_missing": item.base_missing,
                            }
                            for item in request.component_files
                        ],
                        "base_tree_sha256": request.base_tree_sha256,
                        "target_tree_sha256": request.target_tree_sha256,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            script_path = root / f"apply-{task_id}.ps1"
            script_path.write_text(
                self._component_script(
                    payload_directory=payload_root,
                    plan_path=plan_path,
                    install_directory=self._install_directory,
                    working_directory=root / f"work-{task_id}",
                    log_file=log_file,
                ),
                encoding="utf-8-sig",
            )
            command_path = Path("powershell.exe")
            arguments = (
                "-NoProfile -NonInteractive -ExecutionPolicy Bypass "
                f'-File "{script_path}"'
            )
            working_directory = root
        else:
            command_path = package
            arguments = (
                "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- "
                f'/LOG="{log_file}"'
            )
            working_directory = package.parent
        task_file.write_bytes(
            self._task_xml(
                command=command_path,
                arguments=arguments,
                working_directory=working_directory,
                start_at=self._now() + timedelta(seconds=8),
            )
        )
        command = [
            "schtasks.exe",
            "/Create",
            "/TN",
            task_name,
            "/XML",
            str(task_file),
            "/F",
        ]
        try:
            completed = self._runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as exc:
            raise WindowsUpdateError(f"无法创建独立更新任务：{exc}") from exc
        if completed.returncode:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise WindowsUpdateError(
                "创建独立更新任务失败" + (f"：{detail}" if detail else "")
            )

    @staticmethod
    def _task_xml(
        *,
        command: Path,
        arguments: str,
        working_directory: Path,
        start_at: datetime,
    ) -> bytes:
        namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
        ET.register_namespace("", namespace)

        def element(parent: ET.Element, name: str, text: str = "") -> ET.Element:
            child = ET.SubElement(parent, f"{{{namespace}}}{name}")
            if text:
                child.text = text
            return child

        task = ET.Element(f"{{{namespace}}}Task", {"version": "1.4"})
        registration = element(task, "RegistrationInfo")
        element(registration, "Description", "DcmGet signed update")
        triggers = element(task, "Triggers")
        time_trigger = element(triggers, "TimeTrigger")
        start = start_at.astimezone().replace(tzinfo=None)
        element(time_trigger, "StartBoundary", start.isoformat(timespec="seconds"))
        element(
            time_trigger,
            "EndBoundary",
            (start + timedelta(hours=1)).isoformat(timespec="seconds"),
        )
        element(time_trigger, "Enabled", "true")
        principals = element(task, "Principals")
        principal = ET.SubElement(
            principals, f"{{{namespace}}}Principal", {"id": "Author"}
        )
        element(principal, "UserId", "S-1-5-18")
        element(principal, "LogonType", "ServiceAccount")
        element(principal, "RunLevel", "HighestAvailable")
        settings = element(task, "Settings")
        element(settings, "MultipleInstancesPolicy", "IgnoreNew")
        element(settings, "DisallowStartIfOnBatteries", "false")
        element(settings, "StopIfGoingOnBatteries", "false")
        element(settings, "AllowHardTerminate", "true")
        element(settings, "StartWhenAvailable", "true")
        element(settings, "RunOnlyIfNetworkAvailable", "false")
        element(settings, "ExecutionTimeLimit", "PT1H")
        element(settings, "DeleteExpiredTaskAfter", "PT1H")
        actions = ET.SubElement(
            task, f"{{{namespace}}}Actions", {"Context": "Author"}
        )
        execution = element(actions, "Exec")
        element(execution, "Command", str(command))
        element(execution, "Arguments", arguments)
        element(execution, "WorkingDirectory", str(working_directory))
        return ET.tostring(task, encoding="utf-16", xml_declaration=True)

    @staticmethod
    def _component_script(
        *,
        payload_directory: Path,
        plan_path: Path,
        install_directory: Path,
        working_directory: Path,
        log_file: Path,
    ) -> str:
        def quoted(value: Path) -> str:
            return "'" + str(value).replace("'", "''") + "'"

        # The archive has already been path-validated before scheduling. The
        # script repeats root containment checks, backs up every replaced file,
        # and restores the previous application payload if any copy fails.
        return f"""
$ErrorActionPreference = 'Stop'
$installRoot = {quoted(install_directory)}
$workRoot = {quoted(working_directory)}
$payloadRoot = {quoted(payload_directory)}
$planPath = {quoted(plan_path)}
$backupRoot = Join-Path $workRoot 'backup'
$createdList = New-Object System.Collections.Generic.List[string]
$replacedList = New-Object System.Collections.Generic.List[string]
$replacementStarted = $false
New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
Start-Transcript -LiteralPath {quoted(log_file)} -Force | Out-Null
function Add-DcmGetTreeDirectory([string]$directory, [string]$rootPrefix, $records) {{
    foreach ($item in Get-ChildItem -LiteralPath $directory -Force) {{
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {{
            throw "Application tree contains reparse point: $($item.FullName)"
        }}
        if ($item.PSIsContainer) {{
            Add-DcmGetTreeDirectory $item.FullName $rootPrefix $records
        }} else {{
            $relative = $item.FullName.Substring($rootPrefix.Length).Replace('\\', '/')
            $hash = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            $records.Add([PSCustomObject]@{{ Path = $relative; Size = [long]$item.Length; Hash = $hash }})
        }}
    }}
}}
function Get-DcmGetApplicationTreeDigest([string]$root) {{
    $rootFull = [IO.Path]::GetFullPath($root).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $rootItem = Get-Item -LiteralPath $rootFull -Force
    if (-not $rootItem.PSIsContainer -or ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {{
        throw 'Application root is not a safe directory'
    }}
    $rootPrefix = $rootFull + [IO.Path]::DirectorySeparatorChar
    $records = New-Object System.Collections.Generic.List[object]
    $main = Get-Item -LiteralPath (Join-Path $rootFull 'DcmGet.exe') -Force
    if ($main.PSIsContainer -or ($main.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {{
        throw 'DcmGet.exe is not a safe application file'
    }}
    $mainHash = (Get-FileHash -LiteralPath $main.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    $records.Add([PSCustomObject]@{{ Path = 'DcmGet.exe'; Size = [long]$main.Length; Hash = $mainHash }})
    $internal = Join-Path $rootFull '_internal'
    if (Test-Path -LiteralPath $internal) {{
        $internalItem = Get-Item -LiteralPath $internal -Force
        if (-not $internalItem.PSIsContainer -or ($internalItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {{
            throw '_internal is not a safe application directory'
        }}
        Add-DcmGetTreeDirectory $internal $rootPrefix $records
    }}
    $builder = New-Object System.Text.StringBuilder
    $recordByPath = @{{}}
    [string[]]$paths = @($records | ForEach-Object {{ [string]$_.Path }})
    foreach ($record in $records) {{ $recordByPath[[string]$record.Path] = $record }}
    [Array]::Sort($paths, [StringComparer]::Ordinal)
    foreach ($path in $paths) {{
        $record = $recordByPath[$path]
        [void]$builder.Append($path)
        [void]$builder.Append([char]0)
        [void]$builder.Append([string]$record.Size)
        [void]$builder.Append([char]0)
        [void]$builder.Append([string]$record.Hash)
        [void]$builder.Append("`n")
    }}
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {{
        $bytes = [Text.Encoding]::UTF8.GetBytes($builder.ToString())
        return ([BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    }} finally {{
        $algorithm.Dispose()
    }}
}}
$plan = Get-Content -LiteralPath $planPath -Raw -Encoding UTF8 | ConvertFrom-Json
$service = Get-Service -Name 'kayisoft-dcmget' -ErrorAction SilentlyContinue
$restartService = $null -ne $service -and $service.Status -eq [System.ServiceProcess.ServiceControllerStatus]::Running
try {{
    $installFull = [IO.Path]::GetFullPath($installRoot)
    if ($null -ne $service -and $service.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Stopped) {{
        Stop-Service -Name 'kayisoft-dcmget' -Force -ErrorAction Stop
        $service.WaitForStatus([System.ServiceProcess.ServiceControllerStatus]::Stopped, [TimeSpan]::FromSeconds(30))
    }}
    Get-CimInstance -ClassName Win32_Process | Where-Object {{
        $null -ne $_.ExecutablePath -and
        [IO.Path]::GetFullPath($_.ExecutablePath).StartsWith(
            $installFull + [IO.Path]::DirectorySeparatorChar,
            [StringComparison]::OrdinalIgnoreCase
        )
    }} | ForEach-Object {{
        & taskkill.exe /PID ([string]$_.ProcessId) /T /F | Out-Null
        if ($LASTEXITCODE -ne 0) {{ throw "Unable to stop installed process $($_.ProcessId)" }}
    }}
    $baseTreeHash = Get-DcmGetApplicationTreeDigest $installFull
    if ($baseTreeHash -ne ([string]$plan.base_tree_sha256).ToLowerInvariant()) {{
        throw 'Component base application tree mismatch'
    }}
    $payloadRoot = [IO.Path]::GetFullPath($payloadRoot)
    foreach ($entry in $plan.files) {{
        $relative = [string]$entry.path
        $sourcePath = [IO.Path]::GetFullPath((Join-Path $payloadRoot $relative))
        if (-not $sourcePath.StartsWith($payloadRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {{
            throw 'Component source escaped payload root'
        }}
        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {{
            throw "Component source is missing: $relative"
        }}
        $sourceHash = (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($sourceHash -ne ([string]$entry.sha256).ToLowerInvariant()) {{
            throw "Component source hash mismatch: $relative"
        }}
        $destination = [IO.Path]::GetFullPath((Join-Path $installFull $relative))
        if (-not $destination.StartsWith($installFull + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {{
            throw 'Component destination escaped install root'
        }}
        $parent = Split-Path -Parent $destination
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        if ([bool]$entry.base_missing) {{
            if (Test-Path -LiteralPath $destination) {{
                throw "Component base expected file to be absent: $relative"
            }}
        }} else {{
            if (-not (Test-Path -LiteralPath $destination -PathType Leaf)) {{
                throw "Component base file is missing: $relative"
            }}
            if ((Get-Item -LiteralPath $destination).Length -ne [long]$entry.base_size) {{
                throw "Component base size mismatch: $relative"
            }}
            $baseHash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($baseHash -ne ([string]$entry.base_sha256).ToLowerInvariant()) {{
                throw "Component base hash mismatch: $relative"
            }}
        }}
        if (Test-Path -LiteralPath $destination -PathType Leaf) {{
            $backup = Join-Path $backupRoot $relative
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $backup) | Out-Null
            Copy-Item -LiteralPath $destination -Destination $backup -Force
            $replacedList.Add($relative)
        }} else {{
            $createdList.Add($relative)
        }}
        $replacementStarted = $true
        Copy-Item -LiteralPath $sourcePath -Destination $destination -Force
        $destinationHash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($destinationHash -ne ([string]$entry.sha256).ToLowerInvariant()) {{
            throw "Installed component hash mismatch: $relative"
        }}
    }}
    $targetTreeHash = Get-DcmGetApplicationTreeDigest $installFull
    if ($targetTreeHash -ne ([string]$plan.target_tree_sha256).ToLowerInvariant()) {{
        throw 'Component target application tree mismatch'
    }}
}} catch {{
    $originalError = $_
    if ($replacementStarted) {{
        for ($index = $createdList.Count - 1; $index -ge 0; $index--) {{
            $relative = $createdList[$index]
            $destination = Join-Path $installRoot $relative
            Remove-Item -LiteralPath $destination -Force -ErrorAction SilentlyContinue
        }}
        for ($index = $replacedList.Count - 1; $index -ge 0; $index--) {{
            $relative = $replacedList[$index]
            Copy-Item -LiteralPath (Join-Path $backupRoot $relative) -Destination (Join-Path $installRoot $relative) -Force
        }}
        $rollbackTreeHash = Get-DcmGetApplicationTreeDigest $installFull
        if ($rollbackTreeHash -ne ([string]$plan.base_tree_sha256).ToLowerInvariant()) {{
            throw 'Component rollback application tree mismatch'
        }}
    }}
    throw $originalError
}} finally {{
    if ($restartService) {{
        Start-Service -Name 'kayisoft-dcmget' -ErrorAction SilentlyContinue
    }}
    Stop-Transcript -ErrorAction SilentlyContinue | Out-Null
}}
""".strip() + "\n"


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    reparse_attribute = getattr(
        __import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0
    )
    return bool(attributes & reparse_attribute)


class WindowsUpdateService:
    """Non-blocking update state machine for the Windows management center."""

    def __init__(
        self,
        *,
        current_version: str,
        source: ReleaseSource,
        staging: SecureStagingDirectory,
        scheduler: UpdateScheduler | Callable[[ApplyRequest], None],
        authenticode_verifier: AuthenticodeVerifier | None = None,
        policy: str | UpdatePolicy = UpdatePolicy.AUTOMATIC,
        platform_name: str | None = None,
        state_file: str | Path | None = None,
        automatic_interval_seconds: float = 24 * 60 * 60,
        timer_factory: Callable[[float, Callable[[], None]], object] = threading.Timer,
    ) -> None:
        parse_version(current_version)
        self._current_version = current_version
        self._source = source
        self._staging = staging
        self._scheduler = scheduler
        self._authenticode_verifier = authenticode_verifier
        self._platform = sys.platform if platform_name is None else platform_name
        self._state_file = None if state_file is None else Path(state_file)
        self._lock = threading.RLock()
        self._automatic_interval = max(float(automatic_interval_seconds), 1.0)
        self._timer_factory = timer_factory
        self._timer: object | None = None
        self._closed = False
        self._worker: threading.Thread | None = None
        self._candidate: UpdateCandidate | None = None
        self._selected_asset: UpdateAsset | None = None
        self._downloaded_path: Path | None = None
        self._error = ""
        self._checked_at = ""
        self._highest_seen_version = current_version
        persisted_policy = self._load_state()
        self._policy = UpdatePolicy.parse(persisted_policy or policy)
        self._phase = self._initial_phase()

    def status(self) -> dict[str, object]:
        with self._lock:
            candidate = self._candidate
            asset = self._selected_asset
            downloaded = bool(
                self._downloaded_path is not None
                and self._downloaded_path.is_file()
            )
            available = candidate is not None and parse_version(
                candidate.version
            ) > parse_version(self._current_version)
            default_message = {
                UpdatePhase.DISABLED: "自动更新已关闭",
                UpdatePhase.UNSUPPORTED: "自动更新仅支持 Windows 安装版",
                UpdatePhase.IDLE: "等待检查更新",
                UpdatePhase.CHECKING: "正在检查更新",
                UpdatePhase.UP_TO_DATE: "当前已是最新版本",
                UpdatePhase.AVAILABLE: "发现可用更新",
                UpdatePhase.DOWNLOADING: "正在下载更新包",
                UpdatePhase.READY: "更新包已准备完成",
                UpdatePhase.APPLYING: "更新任务已提交",
                UpdatePhase.OFFLINE: "当前无法连接更新服务器",
                UpdatePhase.ERROR: "更新失败",
            }[self._phase]
            message = (
                default_message
                if self._phase is UpdatePhase.OFFLINE
                else self._error or default_message
            )
            return {
                "policy": self._policy.value,
                "phase": self._phase.value,
                "state": self._phase.value,
                "current_version": self._current_version,
                "available_version": candidate.version if candidate else "",
                "latest_version": candidate.version if candidate else "",
                "asset_kind": asset.kind if asset else "",
                "package_kind": (
                    "patch"
                    if asset and asset.is_component_patch
                    else "installer"
                    if asset and asset.is_full_installer
                    else ""
                ),
                "asset_name": asset.name if asset else "",
                "download_size": asset.size if asset else 0,
                "downloaded_path": str(self._downloaded_path or ""),
                "downloaded": downloaded,
                "available": available,
                "error": self._error,
                "message": message,
                "checked_at": self._checked_at,
                "supported": self._platform == "win32",
            }

    def set_policy(self, value: str | UpdatePolicy) -> dict[str, object]:
        policy = UpdatePolicy.parse(value)
        schedule_check = False
        with self._lock:
            previous = self._policy
            self._policy = policy
            if policy is UpdatePolicy.DISABLED:
                self._cancel_timer_locked()
                self._phase = UpdatePhase.DISABLED
                self._error = ""
            else:
                self._phase = self._initial_phase()
                schedule_check = (
                    previous is UpdatePolicy.DISABLED and not self._closed
                )
            self._save_state_locked()
        if schedule_check:
            self.start_automatic_check(delay_seconds=0.0)
        return self.status()

    def check(self, *, wait: bool = False) -> dict[str, object]:
        with self._lock:
            if not self._may_operate_locked():
                return self.status()
            if self._worker is not None and self._worker.is_alive():
                return self.status()
            self._phase = UpdatePhase.CHECKING
            self._error = ""
            if not wait:
                self._start_worker_locked("windows-update-check", self._perform_check)
                return self.status()
        self._perform_check()
        return self.status()

    def _perform_check(self) -> None:
        try:
            candidate = self._source.fetch_latest()
            _validate_service_candidate(candidate)
            if parse_version(candidate.version) < parse_version(
                self._highest_seen_version
            ):
                raise UpdateSecurityError("更新服务器返回了低于已见版本的清单")
            if parse_version(candidate.version) <= parse_version(
                self._current_version
            ):
                with self._lock:
                    if self._policy is UpdatePolicy.DISABLED:
                        return
                    self._candidate = None
                    self._selected_asset = None
                    self._downloaded_path = None
                    self._phase = UpdatePhase.UP_TO_DATE
                    self._checked_at = _utc_now()
                return self.status()
            allow_component_patch = bool(
                getattr(self._scheduler, "supports_component_patch", True)
            )
            asset = candidate.preferred_asset(
                self._current_version,
                allow_component_patch=allow_component_patch,
            )
            if asset.is_component_patch:
                base_validator = getattr(
                    self._scheduler, "can_apply_component", None
                )
                if callable(base_validator) and not base_validator(
                    asset.component_files,
                    asset.base_tree_sha256,
                ):
                    asset = candidate.preferred_asset(
                        self._current_version,
                        allow_component_patch=False,
                    )
            with self._lock:
                if self._policy is UpdatePolicy.DISABLED:
                    return
                self._candidate = candidate
                self._selected_asset = asset
                self._downloaded_path = None
                self._phase = UpdatePhase.AVAILABLE
                self._checked_at = _utc_now()
                self._highest_seen_version = candidate.version
                self._save_state_locked()
        except UpdateNetworkError as exc:
            self._record_failure(UpdatePhase.OFFLINE, exc)
        except Exception as exc:
            self._record_failure(UpdatePhase.ERROR, exc)
        finally:
            self._clear_worker()

    def download(self, *, wait: bool = False) -> dict[str, object]:
        with self._lock:
            if not self._may_operate_locked():
                return self.status()
            if self._worker is not None and self._worker.is_alive():
                return self.status()
            candidate = self._candidate
            asset = self._selected_asset
            if candidate is None or asset is None:
                self._phase = UpdatePhase.ERROR
                self._error = "请先检查更新"
                return self.status()
            self._phase = UpdatePhase.DOWNLOADING
            self._error = ""
            if not wait:
                self._start_worker_locked(
                    "windows-update-download",
                    lambda: self._perform_download(candidate, asset),
                )
                return self.status()
        self._perform_download(candidate, asset)
        return self.status()

    def _perform_download(
        self,
        candidate: UpdateCandidate,
        asset: UpdateAsset,
    ) -> None:
        try:
            target_dir = self._staging.prepare(candidate.version)
            final_path = target_dir / asset.name
            partial_path = target_dir / f"{asset.name}.part"
            for path in (partial_path, final_path):
                if path.exists():
                    if path.is_symlink() or not path.is_file():
                        raise UpdateSecurityError(f"更新暂存文件类型不安全：{path}")
                    path.unlink()
            self._source.download_asset(asset, partial_path)
            _verify_download(partial_path, asset)
            if asset.is_component_patch:
                _validate_component_archive(
                    partial_path,
                    asset.component_files,
                    base_version=asset.base_version,
                    target_version=candidate.version,
                    base_tree_sha256=asset.base_tree_sha256,
                    target_tree_sha256=asset.target_tree_sha256,
                )
            elif self._authenticode_verifier is not None:
                self._authenticode_verifier(partial_path, asset)
            os.replace(partial_path, final_path)
            with self._lock:
                if self._policy is UpdatePolicy.DISABLED:
                    return
                self._downloaded_path = final_path
                self._phase = UpdatePhase.READY
        except UpdateNetworkError as exc:
            self._record_failure(UpdatePhase.OFFLINE, exc)
        except Exception as exc:
            self._record_failure(UpdatePhase.ERROR, exc)
        finally:
            self._clear_worker()

    def apply(self) -> dict[str, object]:
        with self._lock:
            if not self._may_operate_locked():
                return self.status()
            candidate = self._candidate
            asset = self._selected_asset
            downloaded = self._downloaded_path
            if candidate is None or asset is None or downloaded is None:
                self._phase = UpdatePhase.ERROR
                self._error = "更新包尚未准备完成"
                return self.status()
        try:
            _verify_download(downloaded, asset)
            if (
                asset.is_full_installer
                and self._authenticode_verifier is not None
            ):
                self._authenticode_verifier(downloaded, asset)
            request = ApplyRequest(
                package_path=downloaded,
                current_version=self._current_version,
                target_version=candidate.version,
                asset_kind=asset.kind,
                component_files=asset.component_files,
                base_tree_sha256=asset.base_tree_sha256,
                target_tree_sha256=asset.target_tree_sha256,
            )
            schedule = getattr(self._scheduler, "schedule", None)
            if callable(schedule):
                schedule(request)
            elif callable(self._scheduler):
                self._scheduler(request)
            else:
                raise WindowsUpdateError("更新调度器不可用")
            with self._lock:
                self._phase = UpdatePhase.APPLYING
                self._error = ""
        except Exception as exc:
            self._record_failure(UpdatePhase.ERROR, exc)
        return self.status()

    def start_automatic_check(self, delay_seconds: float = 5.0) -> bool:
        with self._lock:
            if not self._may_operate_locked() or self._timer is not None:
                return False
            self._schedule_timer_locked(max(float(delay_seconds), 0.0))
            return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._cancel_timer_locked()

    def _start_worker_locked(
        self,
        name: str,
        target: Callable[[], None],
    ) -> None:
        worker = threading.Thread(target=target, name=name, daemon=True)
        self._worker = worker
        worker.start()

    def _clear_worker(self) -> None:
        with self._lock:
            if self._worker is threading.current_thread():
                self._worker = None

    def _timer_check(self) -> None:
        with self._lock:
            self._timer = None
            if not self._may_operate_locked():
                return
        self.check()
        with self._lock:
            if self._may_operate_locked() and self._timer is None:
                self._schedule_timer_locked(self._automatic_interval)

    def _initial_phase(self) -> UpdatePhase:
        if self._policy_if_available() is UpdatePolicy.DISABLED:
            return UpdatePhase.DISABLED
        if self._platform != "win32":
            return UpdatePhase.UNSUPPORTED
        return UpdatePhase.IDLE

    def _policy_if_available(self) -> UpdatePolicy:
        return getattr(self, "_policy", UpdatePolicy.AUTOMATIC)

    def _may_operate_locked(self) -> bool:
        if self._closed:
            return False
        if self._policy is UpdatePolicy.DISABLED:
            self._phase = UpdatePhase.DISABLED
            return False
        if self._platform != "win32":
            self._phase = UpdatePhase.UNSUPPORTED
            return False
        return True

    def _record_failure(self, phase: UpdatePhase, error: Exception) -> None:
        with self._lock:
            if self._policy is UpdatePolicy.DISABLED:
                return
            self._phase = phase
            self._error = str(error) or error.__class__.__name__
            self._checked_at = _utc_now()

    def _cancel_timer_locked(self) -> None:
        if self._timer is not None:
            cancel = getattr(self._timer, "cancel", None)
            if callable(cancel):
                cancel()
            self._timer = None

    def _schedule_timer_locked(self, delay_seconds: float) -> None:
        timer = self._timer_factory(delay_seconds, self._timer_check)
        if hasattr(timer, "daemon"):
            setattr(timer, "daemon", True)
        start = getattr(timer, "start", None)
        if not callable(start):
            raise WindowsUpdateError("自动更新定时器不可用")
        self._timer = timer
        start()

    def _load_state(self) -> str:
        if self._state_file is None or not self._state_file.is_file():
            return ""
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return ""
            highest = str(raw.get("highest_seen_version", ""))
            if highest and parse_version(highest) >= parse_version(
                self._current_version
            ):
                self._highest_seen_version = highest
            policy = str(raw.get("policy", ""))
            if policy:
                UpdatePolicy.parse(policy)
            return policy
        except (OSError, ValueError, WindowsUpdateError):
            return ""

    def _save_state_locked(self) -> None:
        if self._state_file is None:
            return
        self._state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "policy": self._policy.value,
            "highest_seen_version": self._highest_seen_version,
        }
        temporary = self._state_file.with_name(f".{self._state_file.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._state_file)


def _validate_service_candidate(candidate: UpdateCandidate) -> None:
    parse_version(candidate.version)
    if not candidate.assets:
        raise UpdateSecurityError("更新候选版本不包含更新包")
    for asset in candidate.assets:
        if _SAFE_NAME_PATTERN.fullmatch(asset.name) is None:
            raise UpdateSecurityError(f"更新包文件名无效：{asset.name}")
        if asset.kind not in {"component_patch", "full_installer"}:
            raise UpdateSecurityError(f"更新包类型无效：{asset.kind}")
        if not 0 < asset.size <= MAX_ARTIFACT_BYTES:
            raise UpdateSecurityError(f"更新包大小无效：{asset.name}")
        if _SHA256_PATTERN.fullmatch(asset.sha256) is None:
            raise UpdateSecurityError(f"更新包 SHA-256 无效：{asset.name}")
        mirror_parts = _static_mirror_asset_parts(asset.download_url)
        if not _is_allowed_update_asset_url(asset.download_url):
            raise UpdateSecurityError(f"更新包下载地址无效：{asset.name}")
        if mirror_parts is not None and mirror_parts != (
            candidate.version,
            asset.name,
        ):
            raise UpdateSecurityError(
                f"静态镜像更新包路径与候选版本不一致：{asset.name}"
            )
        if asset.is_component_patch:
            if (
                not asset.base_version
                or not asset.preserves_user_data
                or asset.content_scope != "application"
                or not asset.component_files
                or _SHA256_PATTERN.fullmatch(asset.base_tree_sha256) is None
                or _SHA256_PATTERN.fullmatch(asset.target_tree_sha256) is None
            ):
                raise UpdateSecurityError(f"增量包边界声明无效：{asset.name}")
            parse_version(asset.base_version)
            seen: set[str] = set()
            for item in asset.component_files:
                canonical = item.path.casefold()
                if (
                    not _is_allowed_component_path(item.path)
                    or canonical in seen
                    or item.size < 0
                    or _SHA256_PATTERN.fullmatch(item.sha256) is None
                    or (
                        item.base_missing
                        and (
                            item.base_size is not None
                            or item.base_sha256 is not None
                        )
                    )
                    or (
                        not item.base_missing
                        and (
                            item.base_size is None
                            or item.base_size < 0
                            or item.base_sha256 is None
                            or _SHA256_PATTERN.fullmatch(item.base_sha256)
                            is None
                        )
                    )
                ):
                    raise UpdateSecurityError(
                        f"增量包文件清单无效：{asset.name} / {item.path}"
                    )
                seen.add(canonical)
        elif asset.signature_status not in {"SIGNED", "UNSIGNED"}:
            raise UpdateSecurityError(
                f"完整安装包签名状态无效：{asset.name}"
            )


def _verify_download(path: Path, asset: UpdateAsset) -> None:
    if path.is_symlink() or not path.is_file():
        raise UpdateSecurityError(f"更新包不存在或类型无效：{path}")
    stat_result = path.stat()
    if stat_result.st_size != asset.size:
        raise UpdateSecurityError(
            f"更新包大小不匹配：期望 {asset.size}，实际 {stat_result.st_size}"
        )
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest().lower() != asset.sha256.lower():
        raise UpdateSecurityError(f"更新包 SHA-256 校验失败：{asset.name}")


def _installed_application_tree_digest(install_directory: Path) -> str:
    install_input = Path(install_directory)
    if _is_reparse_point(install_input):
        raise UpdateSecurityError("组件应用目录不能是链接或重解析点")
    install_root = install_input.resolve()
    if not install_root.is_dir():
        raise UpdateSecurityError("组件应用目录不存在")
    records: dict[str, tuple[int, str]] = {}

    def add_file(path: Path, relative: str) -> None:
        if (
            path.is_symlink()
            or _is_reparse_point(path)
            or not path.is_file()
            or not _is_allowed_component_path(relative)
        ):
            raise UpdateSecurityError(f"组件应用树包含不安全文件：{relative}")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        records[relative] = (path.stat().st_size, digest.hexdigest())

    main_executable = install_root / "DcmGet.exe"
    add_file(main_executable, "DcmGet.exe")
    internal = install_root / "_internal"
    if internal.exists():
        if _is_reparse_point(internal) or not internal.is_dir():
            raise UpdateSecurityError("_internal 不是安全目录")

        def visit(directory: Path, relative_parts: tuple[str, ...]) -> None:
            try:
                entries = sorted(os.scandir(directory), key=lambda item: item.name)
            except OSError as exc:
                raise UpdateSecurityError(f"无法读取组件应用树：{directory}") from exc
            for entry in entries:
                path = Path(entry.path)
                parts = (*relative_parts, entry.name)
                relative = PurePosixPath(*parts).as_posix()
                if entry.is_symlink() or _is_reparse_point(path):
                    raise UpdateSecurityError(
                        f"组件应用树包含链接或重解析点：{relative}"
                    )
                if entry.is_dir(follow_symlinks=False):
                    visit(path, parts)
                elif entry.is_file(follow_symlinks=False):
                    add_file(path, relative)
                else:
                    raise UpdateSecurityError(
                        f"组件应用树包含不支持的对象：{relative}"
                    )

        visit(internal, ("_internal",))

    tree = hashlib.sha256()
    for relative in sorted(records):
        size, sha256 = records[relative]
        tree.update(relative.encode("utf-8"))
        tree.update(b"\0")
        tree.update(str(size).encode("ascii"))
        tree.update(b"\0")
        tree.update(sha256.encode("ascii"))
        tree.update(b"\n")
    return tree.hexdigest()


def _validate_installed_tree(
    install_directory: Path,
    expected_sha256: str,
    label: str,
) -> None:
    if _SHA256_PATTERN.fullmatch(expected_sha256) is None:
        raise UpdateSecurityError(f"组件{label}应用树指纹无效")
    actual = _installed_application_tree_digest(install_directory)
    if actual.lower() != expected_sha256.lower():
        raise UpdateSecurityError(f"组件{label}应用树指纹不匹配")


def _validate_installed_base(
    install_directory: Path,
    files: Sequence[ComponentFile],
) -> None:
    install_root = install_directory.resolve()
    if _is_reparse_point(install_directory) or not install_root.is_dir():
        raise UpdateSecurityError("组件更新安装目录不存在或类型不安全")
    for item in files:
        target = install_root.joinpath(*PurePosixPath(item.path).parts)
        try:
            target.resolve(strict=False).relative_to(install_root)
        except ValueError as exc:
            raise UpdateSecurityError(f"组件基础文件越界：{item.path}") from exc
        if target.is_symlink() or _is_reparse_point(target):
            raise UpdateSecurityError(f"组件基础文件不能是链接：{item.path}")
        if item.base_missing:
            if target.exists():
                raise UpdateSecurityError(
                    f"组件基础状态不匹配，文件应不存在：{item.path}"
                )
            continue
        if not target.is_file():
            raise UpdateSecurityError(f"组件基础文件不存在：{item.path}")
        if target.stat().st_size != item.base_size:
            raise UpdateSecurityError(f"组件基础文件大小不匹配：{item.path}")
        digest = hashlib.sha256()
        with target.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest().lower() != str(item.base_sha256).lower():
            raise UpdateSecurityError(f"组件基础文件 SHA-256 不匹配：{item.path}")


def _validate_component_archive(
    path: Path,
    expected_files: Sequence[ComponentFile],
    *,
    base_version: str | None = None,
    target_version: str | None = None,
    base_tree_sha256: str = "",
    target_tree_sha256: str = "",
) -> None:
    if not expected_files:
        raise UpdateSecurityError("增量包缺少已签名的文件清单")
    expected = {item.path.casefold(): item for item in expected_files}
    actual: dict[str, zipfile.ZipInfo] = {}
    embedded_manifest: bytes | None = None
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                raw_name = info.filename.replace("\\", "/")
                archive_path = PurePosixPath(raw_name)
                if info.is_dir():
                    continue
                if raw_name == PATCH_MANIFEST_NAME:
                    if embedded_manifest is not None or info.file_size > MAX_MANIFEST_BYTES:
                        raise UpdateSecurityError("增量包内的 PATCH-MANIFEST.json 无效")
                    with archive.open(info) as source:
                        embedded_manifest = source.read(MAX_MANIFEST_BYTES + 1)
                    if len(embedded_manifest) > MAX_MANIFEST_BYTES:
                        raise UpdateSecurityError("增量包内的 PATCH-MANIFEST.json 过大")
                    continue
                unix_mode = (info.external_attr >> 16) & 0o170000
                if (
                    info.flag_bits & 0x1
                    or unix_mode == 0o120000
                ):
                    raise UpdateSecurityError(f"增量包包含不安全条目：{raw_name}")
                relative = archive_path.as_posix()
                canonical = relative.casefold()
                if (
                    not _is_allowed_component_path(relative)
                    or canonical in actual
                    or canonical not in expected
                ):
                    raise UpdateSecurityError(
                        f"增量包包含越界、重复或额外文件：{relative}"
                    )
                record = expected[canonical]
                if info.file_size != record.size:
                    raise UpdateSecurityError(f"增量文件大小不匹配：{relative}")
                digest = hashlib.sha256()
                with archive.open(info) as source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(block)
                if digest.hexdigest().lower() != record.sha256.lower():
                    raise UpdateSecurityError(f"增量文件 SHA-256 不匹配：{relative}")
                actual[canonical] = info
    except (OSError, zipfile.BadZipFile) as exc:
        raise UpdateSecurityError(f"增量包不是有效 ZIP：{exc}") from exc
    missing = sorted(set(expected) - set(actual))
    if missing:
        raise UpdateSecurityError(f"增量包缺少清单文件：{', '.join(missing)}")
    if embedded_manifest is None:
        raise UpdateSecurityError("增量包缺少 PATCH-MANIFEST.json")
    try:
        patch_manifest = json.loads(embedded_manifest.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateSecurityError("PATCH-MANIFEST.json 不是有效 UTF-8 JSON") from exc
    if not isinstance(patch_manifest, dict):
        raise UpdateSecurityError("PATCH-MANIFEST.json 根节点必须是对象")
    if (
        patch_manifest.get("product") != PRODUCT
        or patch_manifest.get("platform") != PLATFORM
        or patch_manifest.get("removed_paths") != []
        or patch_manifest.get("install_path_allowlist")
        != ["DcmGet.exe", "_internal/**"]
    ):
        raise UpdateSecurityError("PATCH-MANIFEST.json 的产品或安装边界无效")
    if base_version and patch_manifest.get("base_version") != base_version:
        raise UpdateSecurityError("PATCH-MANIFEST.json 的基础版本不匹配")
    if target_version and patch_manifest.get("version") != target_version:
        raise UpdateSecurityError("PATCH-MANIFEST.json 的目标版本不匹配")
    embedded_base_tree = str(
        patch_manifest.get("base_tree_sha256", "")
    ).lower()
    embedded_target_tree = str(
        patch_manifest.get("target_tree_sha256", "")
    ).lower()
    if (
        _SHA256_PATTERN.fullmatch(embedded_base_tree) is None
        or _SHA256_PATTERN.fullmatch(embedded_target_tree) is None
        or embedded_base_tree != base_tree_sha256.lower()
        or embedded_target_tree != target_tree_sha256.lower()
    ):
        raise UpdateSecurityError("PATCH-MANIFEST.json 与签名应用树指纹不一致")
    embedded_files = _parse_component_files(
        patch_manifest.get("files"), PATCH_MANIFEST_NAME
    )
    if embedded_files != tuple(expected_files):
        raise UpdateSecurityError("PATCH-MANIFEST.json 与签名文件清单不一致")


def _extract_component_archive(
    archive_path: Path,
    destination: Path,
    expected_files: Sequence[ComponentFile],
    *,
    base_version: str,
    target_version: str,
    base_tree_sha256: str,
    target_tree_sha256: str,
) -> None:
    _validate_component_archive(
        archive_path,
        expected_files,
        base_version=base_version,
        target_version=target_version,
        base_tree_sha256=base_tree_sha256,
        target_tree_sha256=target_tree_sha256,
    )
    if destination.exists():
        raise UpdateSecurityError(f"增量解压目录已存在：{destination}")
    destination.mkdir(parents=True, mode=0o700)
    expected = {item.path.casefold(): item for item in expected_files}
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            if info.is_dir() or info.filename.replace("\\", "/") == PATCH_MANIFEST_NAME:
                continue
            archive_name = PurePosixPath(info.filename.replace("\\", "/"))
            relative = archive_name
            record = expected[relative.as_posix().casefold()]
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if target.exists() or target.is_symlink():
                raise UpdateSecurityError(f"增量解压目标发生冲突：{record.path}")
            digest = hashlib.sha256()
            with archive.open(info) as source, target.open("xb") as output:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if digest.hexdigest().lower() != record.sha256.lower():
                raise UpdateSecurityError(f"增量解压后校验失败：{record.path}")


def create_windows_update_service(
    state_directory: str | Path,
    install_directory: str | Path,
    current_version: str,
) -> WindowsUpdateService:
    """Build the production updater used by the Windows management service."""

    state_root = Path(state_directory).expanduser().resolve()
    install_root = Path(install_directory).expanduser().resolve()
    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    updater_root = (program_data / "DcmGetUpdater").resolve()
    acl = WindowsDirectoryAcl()
    staging = SecureStagingDirectory(updater_root, access_controller=acl)
    source = StaticMirrorReleaseSource(
        signed_manifest_verifier=Ed25519SignedManifestVerifier(
            TRUSTED_UPDATE_PUBLIC_KEYS
        ),
        timeout_seconds=3.0,
    )
    scheduler = WindowsScheduledTaskUpdater(
        staging,
        install_directory=install_root,
    )
    service = WindowsUpdateService(
        current_version=current_version,
        source=source,
        staging=staging,
        scheduler=scheduler,
        authenticode_verifier=None,
        policy=UpdatePolicy.AUTOMATIC,
        state_file=state_root / "windows-update.json",
    )
    service.start_automatic_check(delay_seconds=8.0)
    return service


__all__ = [
    "ApplyRequest",
    "ComponentFile",
    "Ed25519SignedManifestVerifier",
    "GitHubReleaseSource",
    "MirrorFirstReleaseSource",
    "SecureStagingDirectory",
    "STATIC_MIRROR_BASE_URL",
    "StaticMirrorReleaseSource",
    "UpdateAsset",
    "UpdateCandidate",
    "UpdateNetworkError",
    "UpdatePhase",
    "UpdatePolicy",
    "UpdateSecurityError",
    "TRUSTED_UPDATE_PUBLIC_KEYS",
    "WindowsAuthenticodeVerifier",
    "WindowsDirectoryAcl",
    "WindowsScheduledTaskUpdater",
    "WindowsSignedManifestVerifier",
    "WindowsUpdateError",
    "WindowsUpdateService",
    "create_windows_update_service",
    "parse_version",
]
