from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import posixpath
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path, PurePosixPath


VIEWER_PATH = "/viewer/directory/"
LEGACY_VIEWER_PATH = "/viewer/dicomjson/"
STUDY_INDEX = "VIEWER/.dcmget/index"
LEGACY_STUDY_INDEX = "DCMGET_STUDIES.json"
STUDY_INDEX_ENDPOINT = "/api/studies"
OHIF_PAYLOAD_CHECKSUMS = "DCMGET_PAYLOAD.SHA256"
VIEWER_LOG_NAME = "dcmget-pdi-viewer.log"
MAX_CHECKSUM_MANIFEST_BYTES = 1024 * 1024
MAX_VIEWER_FILES = 4096
MAX_VIEWER_FILE_BYTES = 512 * 1024 * 1024
MAX_VIEWER_TOTAL_BYTES = 512 * 1024 * 1024
MAX_STUDY_INDEX_BYTES = 64 * 1024 * 1024
MAX_STUDY_INDEX_INSTANCES = 100_000
SESSION_TOKEN_BYTES = 32
DEFAULT_IDLE_TIMEOUT_SECONDS = 4 * 60 * 60


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        flags |= getattr(os, "O_NOINHERIT", 0)
        descriptor = os.open(self.baseFilename, flags, 0o600)
        return os.fdopen(
            descriptor,
            self.mode,
            encoding=self.encoding,
            errors=self.errors,
        )

    def doRollover(self) -> None:
        super().doRollover()
        for index in range(self.backupCount + 1):
            path = Path(
                self.baseFilename if index == 0 else f"{self.baseFilename}.{index}"
            )
            try:
                path.chmod(0o600)
            except OSError:
                pass


class PdiRequestHandler(BaseHTTPRequestHandler):
    """Serve one PDI directory without exposing the surrounding filesystem."""

    server_version = "DcmGetPDI/2.6.3"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch(send_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch(send_body=False)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._host_is_allowed():
            self._send_empty(HTTPStatus.MISDIRECTED_REQUEST)
            return
        if not self._cookie_is_valid():
            self._send_empty(HTTPStatus.FORBIDDEN)
            return
        self._request_started()
        try:
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)
        finally:
            self._request_finished()

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST

    def _request_started(self) -> None:
        recorder = getattr(self.server, "request_started", None)
        if callable(recorder):
            recorder()

    def _request_finished(self) -> None:
        recorder = getattr(self.server, "request_finished", None)
        if callable(recorder):
            recorder()

    def log_message(self, format: str, *args: object) -> None:
        if getattr(self.server, "quiet", False):
            return
        message = format % args
        session_token = str(getattr(self.server, "session_token", ""))
        if session_token:
            message = message.replace(session_token, "<redacted>")
        recorder = getattr(self.server, "record_access", None)
        if callable(recorder):
            recorder(message)
        # PyInstaller's Windows ``--windowed`` mode intentionally leaves
        # stderr unset.  HTTP access logging must never be allowed to abort a
        # request before the response status line is written.
        if sys.stderr is None:
            return
        try:
            super().log_message("%s", message)
        except (AttributeError, OSError, ValueError):
            return

    def end_headers(self) -> None:
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self' blob: data:; connect-src 'self' blob:; "
            "img-src 'self' blob: data:; font-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' blob:; "
            "worker-src 'self' blob:; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'none'; form-action 'none'",
        )
        super().end_headers()

    def _dispatch(self, *, send_body: bool) -> None:
        if not self._host_is_allowed():
            self._send_empty(HTTPStatus.MISDIRECTED_REQUEST)
            return
        request_path = urllib.parse.urlsplit(self.path).path
        if request_path.startswith("/open/"):
            if self.command != "GET":
                self._send_empty(HTTPStatus.METHOD_NOT_ALLOWED)
                return
            if not self._path_token_is_valid(request_path, "/open/"):
                self._send_empty(HTTPStatus.NOT_FOUND)
                return
            self._request_started()
            try:
                self._open_session()
            finally:
                self._request_finished()
            return
        if request_path.startswith("/ready/"):
            if self.command != "HEAD":
                self._send_empty(HTTPStatus.METHOD_NOT_ALLOWED)
                return
            if not self._path_token_is_valid(request_path, "/ready/"):
                self._send_empty(HTTPStatus.NOT_FOUND)
                return
            self._request_started()
            try:
                self._send_empty(HTTPStatus.OK)
            finally:
                self._request_finished()
            return
        if not self._cookie_is_valid():
            self._send_empty(HTTPStatus.FORBIDDEN)
            return

        self._request_started()
        try:
            self._serve_authorized(request_path, send_body=send_body)
        finally:
            self._request_finished()

    def _serve_authorized(self, request_path: str, *, send_body: bool) -> None:
        normalized = _normalize_request_path(request_path)
        if normalized is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if normalized == STUDY_INDEX_ENDPOINT:
            payload = bytes(getattr(self.server, "study_index_bytes"))
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if send_body:
                self.wfile.write(payload)
            return
        if normalized.startswith("/api/") or normalized in {
            f"/{LEGACY_STUDY_INDEX}",
            f"/{STUDY_INDEX}",
        }:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if normalized == "/DICOM" or normalized.startswith("/DICOM/"):
            file_path = self._resolve_dicom_path(normalized)
        else:
            file_path = self._resolve_viewer_path(normalized)
        if file_path is None or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        try:
            size = file_path.stat().st_size
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            start, end = self._parse_range(size)
        except ValueError:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        content_type = self._content_type(file_path, request_path)
        ranged = bool(self.headers.get("Range", "").strip())
        self.send_response(HTTPStatus.PARTIAL_CONTENT if ranged else HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        if ranged:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not send_body:
            return

        try:
            with file_path.open("rb") as source:
                source.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _resolve_dicom_path(self, request_path: str) -> Path | None:
        allowlist = getattr(self.server, "dicom_allowlist", frozenset())
        if request_path not in allowlist:
            return None
        return _safe_dicom_path(Path(getattr(self.server, "pdi_root")), request_path)

    def _resolve_viewer_path(self, request_path: str) -> Path | None:
        root = Path(getattr(self.server, "pdi_root")).resolve()
        viewer_root = (root / "VIEWER" / "OHIF").resolve()
        relative = request_path.lstrip("/")
        candidate = viewer_root.joinpath(*relative.split("/"))
        if not candidate.is_file():
            if Path(relative).suffix:
                return None
            candidate = viewer_root / "index.html"
        if _path_contains_symlink(viewer_root, candidate):
            return None

        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(viewer_root)
        except (OSError, ValueError):
            return None
        return resolved

    def _open_session(self) -> None:
        cookie_name = str(getattr(self.server, "cookie_name"))
        session_token = str(getattr(self.server, "session_token"))
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header(
            "Location",
            _viewer_directory_location(
                str(getattr(self.server, "viewer_path", VIEWER_PATH))
            ),
        )
        self.send_header(
            "Set-Cookie",
            f"{cookie_name}={session_token}; Path=/; HttpOnly; SameSite=Strict",
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _path_token_is_valid(self, request_path: str, prefix: str) -> bool:
        candidate = request_path[len(prefix) :]
        if not candidate or "/" in candidate:
            return False
        return secrets.compare_digest(
            candidate,
            str(getattr(self.server, "session_token", "")),
        )

    def _cookie_is_valid(self) -> bool:
        raw_cookie = self.headers.get("Cookie", "")
        if not raw_cookie:
            return False
        cookies = SimpleCookie()
        try:
            cookies.load(raw_cookie)
        except CookieError:
            return False
        morsel = cookies.get(str(getattr(self.server, "cookie_name", "")))
        if morsel is None:
            return False
        return secrets.compare_digest(
            morsel.value,
            str(getattr(self.server, "session_token", "")),
        )

    def _send_empty(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _host_is_allowed(self) -> bool:
        host = self.headers.get("Host", "").strip().lower()
        expected = f"127.0.0.1:{self.server.server_address[1]}"
        return host == expected

    def _parse_range(self, size: int) -> tuple[int, int]:
        header = self.headers.get("Range", "").strip()
        if size <= 0:
            if header:
                raise ValueError("empty file has no byte range")
            return 0, -1
        if not header:
            return 0, size - 1
        if not header.startswith("bytes=") or "," in header:
            raise ValueError("unsupported range")
        first, separator, last = header[6:].partition("-")
        if not separator:
            raise ValueError("invalid range")
        if not first:
            length = int(last)
            if length <= 0:
                raise ValueError("invalid suffix range")
            return max(0, size - length), size - 1
        start = int(first)
        end = int(last) if last else size - 1
        if start < 0 or start >= size or end < start:
            raise ValueError("range outside file")
        return start, min(end, size - 1)

    @staticmethod
    def _content_type(path: Path, request_path: str) -> str:
        if request_path.startswith("/DICOM/"):
            return "application/dicom"
        if request_path == STUDY_INDEX_ENDPOINT:
            return "application/json; charset=utf-8"
        guessed = mimetypes.guess_type(path.name)[0]
        return guessed or "application/octet-stream"


class PdiHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        root: Path,
        host: str,
        port: int,
        *,
        quiet: bool = False,
        logger: logging.Logger | None = None,
        idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
        session_token: str | None = None,
    ):
        if host != "127.0.0.1":
            raise ValueError("PDI 服务只允许绑定 127.0.0.1")
        if not 0 < idle_timeout_seconds < float("inf"):
            raise ValueError("PDI 服务空闲超时必须大于 0")
        self.pdi_root = Path(root).expanduser().resolve()
        self.quiet = quiet
        self.logger = logger
        self.idle_timeout_seconds = float(idle_timeout_seconds)
        self.session_token = _validated_session_token(
            session_token or generate_session_token()
        )
        self.cookie_name = f"dcmget_pdi_{secrets.token_hex(12)}"
        study_index_path = _study_index_path(self.pdi_root)
        self.viewer_path = (
            LEGACY_VIEWER_PATH
            if study_index_path == self.pdi_root / LEGACY_STUDY_INDEX
            else VIEWER_PATH
        )
        self.study_index_bytes, self.dicom_allowlist = _load_study_index(
            self.pdi_root, study_index_path
        )
        self.idle_expired = False
        self._last_activity = time.monotonic()
        self._idle_condition = threading.Condition()
        self._idle_monitor_stop = False
        self._active_requests = 0
        super().__init__((host, port), PdiRequestHandler)

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        with self._idle_condition:
            self.idle_expired = False
            self._idle_monitor_stop = False
            self._last_activity = time.monotonic()
            self._active_requests = 0
        monitor = threading.Thread(
            target=self._wait_for_idle_shutdown,
            name="PdiIdleMonitor",
            daemon=True,
        )
        monitor.start()
        try:
            super().serve_forever(poll_interval=poll_interval)
        finally:
            with self._idle_condition:
                self._idle_monitor_stop = True
                self._idle_condition.notify_all()
            monitor.join(timeout=1)

    def request_started(self) -> None:
        with self._idle_condition:
            self._active_requests += 1
            self._last_activity = time.monotonic()
            self._idle_condition.notify_all()

    def request_finished(self) -> None:
        with self._idle_condition:
            self._active_requests = max(0, self._active_requests - 1)
            self._last_activity = time.monotonic()
            self._idle_condition.notify_all()

    def _wait_for_idle_shutdown(self) -> None:
        while True:
            with self._idle_condition:
                if self._idle_monitor_stop:
                    return
                if self._active_requests:
                    self._idle_condition.wait()
                    continue
                remaining = self.idle_timeout_seconds - (
                    time.monotonic() - self._last_activity
                )
                if remaining > 0:
                    self._idle_condition.wait(timeout=remaining)
                    continue
                self.idle_expired = True
                break
        if self.logger is not None:
            self.logger.info(
                "SESSION IDLE EXIT after %.0f seconds", self.idle_timeout_seconds
            )
        self.shutdown()

    def record_access(self, message: str) -> None:
        if self.logger is not None:
            self.logger.info("HTTP %s", message)

    def handle_error(self, request, client_address) -> None:
        if self.logger is not None:
            self.logger.error(
                "Unhandled request error client=%s",
                client_address[0] if client_address else "unknown",
                exc_info=sys.exc_info(),
            )
        if sys.stderr is not None and not self.quiet:
            try:
                super().handle_error(request, client_address)
            except (AttributeError, OSError, ValueError):
                pass


def validate_pdi_root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve()
    required_directories = (
        root / "DICOM",
        root / "VIEWER",
        root / "VIEWER" / "OHIF",
    )
    required_files = (root / "VIEWER" / "OHIF" / "index.html",)
    missing = [
        str(path.relative_to(root))
        for path in required_directories
        if not path.is_dir() or path.is_symlink()
    ]
    missing.extend(
        str(path.relative_to(root))
        for path in required_files
        if not path.is_file() or path.is_symlink()
    )
    study_index = _study_index_path(root)
    if study_index is None:
        missing.append(STUDY_INDEX)
    if missing:
        raise FileNotFoundError(f"PDI 目录不完整，缺少：{'、'.join(missing)}")
    for path in (*required_directories, *required_files, study_index):
        try:
            path.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise FileNotFoundError(f"PDI 路径超出导出目录：{path.name}") from exc
    viewer_root = root / "VIEWER" / "OHIF"
    checksum_path = viewer_root / OHIF_PAYLOAD_CHECKSUMS
    if checksum_path.is_file():
        verify_viewer_payload(viewer_root)
    elif study_index == root / STUDY_INDEX:
        raise FileNotFoundError(
            f"PDI 离线阅片器缺少资源校验清单：{OHIF_PAYLOAD_CHECKSUMS}"
        )
    return root


def verify_viewer_payload(root: Path) -> None:
    """Validate the bundled viewer without reading any patient DICOM data."""

    checksum_path = root / OHIF_PAYLOAD_CHECKSUMS
    try:
        if checksum_path.is_symlink():
            raise RuntimeError("离线阅片器资源校验清单不能是符号链接")
        if checksum_path.stat().st_size > MAX_CHECKSUM_MANIFEST_BYTES:
            raise RuntimeError("离线阅片器资源校验清单过大")
        lines = checksum_path.read_text(encoding="utf-8").splitlines()
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError("离线阅片器缺少资源校验清单") from exc

    if not lines or len(lines) > MAX_VIEWER_FILES:
        raise RuntimeError("离线阅片器资源校验清单条目数无效")
    expected: dict[str, str] = {}
    for line in lines:
        digest, separator, relative = line.partition("  ")
        candidate = PurePosixPath(relative)
        normalized = candidate.as_posix()
        if (
            not separator
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not relative
            or "\\" in relative
            or candidate.is_absolute()
            or ".." in candidate.parts
            or normalized in expected
        ):
            raise RuntimeError("离线阅片器资源校验清单格式无效")
        expected[normalized] = digest

    asset_paths: dict[str, Path] = {}
    total_bytes = 0
    try:
        for path in root.rglob("*"):
            if path.is_symlink():
                raise RuntimeError("离线阅片器资源包含不允许的符号链接")
            if not path.is_file() or path == checksum_path:
                continue
            relative = path.relative_to(root).as_posix()
            if len(asset_paths) >= MAX_VIEWER_FILES:
                raise RuntimeError("离线阅片器资源文件过多")
            size = path.stat().st_size
            if size > MAX_VIEWER_FILE_BYTES:
                raise RuntimeError(f"离线阅片器资源文件过大：{relative}")
            total_bytes += size
            if total_bytes > MAX_VIEWER_TOTAL_BYTES:
                raise RuntimeError("离线阅片器资源总体积超过 512 MiB 上限")
            asset_paths[relative] = path
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError(f"离线阅片器资源无法读取：{exc}") from exc
    if asset_paths.keys() != expected.keys():
        raise RuntimeError("离线阅片器资源校验失败，文件可能缺失或已损坏")

    try:
        actual = {
            relative: _sha256(asset_paths[relative]) for relative in expected
        }
    except OSError as exc:
        raise RuntimeError(f"离线阅片器资源无法读取：{exc}") from exc
    if actual != expected:
        raise RuntimeError("离线阅片器资源校验失败，文件可能缺失或已损坏")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def generate_session_token() -> str:
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def _validated_session_token(value: str) -> str:
    token = str(value)
    allowed = frozenset(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )
    if not 43 <= len(token) <= 128 or any(
        character not in allowed for character in token
    ):
        raise ValueError("PDI 会话令牌格式无效")
    return token


def _viewer_directory_location(viewer_path: str = VIEWER_PATH) -> str:
    query = urllib.parse.urlencode({"url": STUDY_INDEX_ENDPOINT, "lng": "zh"})
    return f"{viewer_path}?{query}"


def viewer_url(port: int, session_token: str) -> str:
    token = _validated_session_token(session_token)
    return f"http://127.0.0.1:{port}/open/{token}"


def _study_index_path(root: Path) -> Path | None:
    for relative in (STUDY_INDEX, LEGACY_STUDY_INDEX):
        candidate = root / relative
        if not candidate.is_file() or candidate.is_symlink():
            continue
        try:
            candidate.resolve(strict=True).relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        return candidate
    return None


def _normalize_request_path(request_path: str) -> str | None:
    decoded = urllib.parse.unquote(request_path)
    if not decoded.startswith("/") or "\0" in decoded or "\\" in decoded:
        return None
    if ".." in decoded.split("/"):
        return None
    normalized = posixpath.normpath(decoded)
    return normalized if normalized.startswith("/") else None


def _path_contains_symlink(base: Path, candidate: Path) -> bool:
    try:
        relative = candidate.relative_to(base)
    except ValueError:
        return True
    current = base
    if current.is_symlink():
        return True
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _safe_dicom_path(root: Path, request_path: str) -> Path | None:
    normalized = _normalize_request_path(request_path)
    if normalized is None or not normalized.startswith("/DICOM/"):
        return None
    relative = PurePosixPath(normalized.lstrip("/"))
    if any(
        not part or part in {".", ".."} or ":" in part
        for part in relative.parts
    ):
        return None
    dicom_root = root / "DICOM"
    candidate = root.joinpath(*relative.parts)
    if dicom_root.is_symlink() or _path_contains_symlink(root, candidate):
        return None
    try:
        resolved_dicom_root = dicom_root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_dicom_root)
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _load_study_index(
    root: Path,
    index_path: Path | None = None,
) -> tuple[bytes, frozenset[str]]:
    index_path = index_path or _study_index_path(root)
    if index_path is None:
        raise FileNotFoundError(f"PDI 目录不完整，缺少：{STUDY_INDEX}")
    try:
        with index_path.open("rb") as source:
            raw = source.read(MAX_STUDY_INDEX_BYTES + 1)
    except OSError as exc:
        raise RuntimeError(f"PDI 阅片索引无法读取：{exc}") from exc
    if len(raw) > MAX_STUDY_INDEX_BYTES:
        raise RuntimeError("PDI 阅片索引超过 64 MiB 上限")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"PDI 阅片索引格式无效：{exc}") from exc
    allowlist = _validate_study_index(payload, root)
    return bytes(raw), frozenset(allowlist)


def _validate_study_index(payload: object, root: Path) -> set[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("studies"), list):
        raise RuntimeError("PDI 阅片索引必须包含 studies 数组")
    allowlist: set[str] = set()
    instance_count = 0
    for study in payload["studies"]:
        if not isinstance(study, dict) or not isinstance(study.get("series"), list):
            raise RuntimeError("PDI 阅片索引的 study/series 结构无效")
        for series in study["series"]:
            if not isinstance(series, dict) or not isinstance(
                series.get("instances"), list
            ):
                raise RuntimeError("PDI 阅片索引的 series/instances 结构无效")
            for instance in series["instances"]:
                instance_count += 1
                if instance_count > MAX_STUDY_INDEX_INSTANCES:
                    raise RuntimeError("PDI 阅片索引实例数超过上限")
                if (
                    not isinstance(instance, dict)
                    or not isinstance(instance.get("metadata"), dict)
                    or not isinstance(instance.get("url"), str)
                ):
                    raise RuntimeError("PDI 阅片索引的实例结构无效")
                request_path = _validate_dicom_url(instance["url"])
                if request_path not in allowlist:
                    if _safe_dicom_path(root, request_path) is None:
                        raise RuntimeError(
                            f"PDI 阅片索引引用了无效 DICOM 文件：{request_path}"
                        )
                    allowlist.add(request_path)
    return allowlist


def _validate_dicom_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "dicomweb"
        or parsed.netloc
        or parsed.fragment
        or not parsed.path.startswith("/DICOM/")
    ):
        raise RuntimeError("PDI 阅片索引包含无效 DICOM URL")
    normalized = _normalize_request_path(parsed.path)
    if normalized is None or normalized != parsed.path:
        raise RuntimeError("PDI 阅片索引包含不安全的 DICOM 路径")
    if parsed.query:
        try:
            query = urllib.parse.parse_qs(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=True,
            )
        except ValueError as exc:
            raise RuntimeError("PDI 阅片索引包含无效帧参数") from exc
        frames = query.get("frame", [])
        if (
            set(query) != {"frame"}
            or len(frames) != 1
            or not frames[0].isdigit()
            or int(frames[0]) <= 0
        ):
            raise RuntimeError("PDI 阅片索引包含无效帧参数")
    return normalized


def run_server(
    root: str | Path,
    *,
    port: int = 0,
    open_browser: bool = True,
    quiet: bool = False,
    logger: logging.Logger | None = None,
    idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
    session_token: str | None = None,
) -> int:
    pdi_root = validate_pdi_root(root)
    with PdiHttpServer(
        pdi_root,
        "127.0.0.1",
        port,
        quiet=quiet,
        logger=logger,
        idle_timeout_seconds=idle_timeout_seconds,
        session_token=session_token,
    ) as server:
        url = viewer_url(server.server_address[1], server.session_token)
        if logger is not None:
            logger.info(
                "SESSION START pid=%s frozen=%s port=%s",
                os.getpid(),
                bool(getattr(sys, "frozen", False)),
                server.server_address[1],
            )
        if sys.stdout is not None:
            print(f"DcmGet PDI 本地阅片服务：{url}", flush=True)
        if open_browser:
            threading.Timer(0.25, webbrowser.open, args=(url,)).start()
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打开 DcmGet PDI 本地 OHIF 阅片器")
    default_root = (
        Path(sys.executable).resolve().parent
        if bool(getattr(sys, "frozen", False))
        else Path.cwd()
    )
    parser.add_argument("--root", default=str(default_root), help="PDI 根目录")
    parser.add_argument("--port", type=int, default=0, help="本地端口，0 表示自动选择")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--quiet", action="store_true", help="不输出 HTTP 访问日志")
    parser.add_argument(
        "--session-token",
        default="",
        help="高熵本机会话令牌；留空时自动生成",
    )
    parser.add_argument(
        "--idle-timeout",
        type=int,
        default=DEFAULT_IDLE_TIMEOUT_SECONDS,
        help=(
            "无请求后自动退出的秒数"
            f"（默认 {DEFAULT_IDLE_TIMEOUT_SECONDS}，约 4 小时）"
        ),
    )
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("端口必须为 0 到 65535")
    if args.idle_timeout <= 0:
        parser.error("空闲超时必须大于 0 秒")
    logger = _configure_viewer_logger()
    try:
        return run_server(
            args.root,
            port=args.port,
            open_browser=not args.no_browser,
            quiet=args.quiet,
            logger=logger,
            idle_timeout_seconds=args.idle_timeout,
            session_token=args.session_token or None,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        if logger is not None:
            logger.error("Unable to start PDI viewer: %s", exc, exc_info=True)
        if sys.stderr is not None:
            print(f"无法启动 PDI 阅片器：{exc}", file=sys.stderr)
        return 1


def viewer_log_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "DcmGet" / "logs" / VIEWER_LOG_NAME
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "DcmGet"
            / "logs"
            / VIEWER_LOG_NAME
        )
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "dcmget" / "logs" / VIEWER_LOG_NAME


def _configure_viewer_logger() -> logging.Logger | None:
    path = viewer_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        handler = _PrivateRotatingFileHandler(
            path,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
        )
    except OSError:
        return None
    logger = logging.getLogger("dcmget.pdi.viewer")
    for existing in logger.handlers:
        existing.close()
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    return logger


if __name__ == "__main__":
    raise SystemExit(main())
