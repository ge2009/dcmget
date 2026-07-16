from __future__ import annotations

import argparse
import mimetypes
import posixpath
import sys
import threading
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


VIEWER_PATH = "/viewer/dicomjson/"
STUDY_INDEX = "DCMGET_STUDIES.json"


class PdiRequestHandler(BaseHTTPRequestHandler):
    """Serve one PDI directory without exposing the surrounding filesystem."""

    server_version = "DcmGetPDI/2.6"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._serve(send_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._serve(send_body=False)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST

    def log_message(self, format: str, *args: object) -> None:
        if getattr(self.server, "quiet", False):
            return
        super().log_message(format, *args)

    def end_headers(self) -> None:
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
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

    def _serve(self, *, send_body: bool) -> None:
        if not self._host_is_allowed():
            self.send_error(HTTPStatus.MISDIRECTED_REQUEST)
            return
        request_path = urllib.parse.urlsplit(self.path).path
        file_path = self._resolve_request_path(request_path)
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

    def _resolve_request_path(self, request_path: str) -> Path | None:
        root = Path(getattr(self.server, "pdi_root")).resolve()
        viewer_root = root / "VIEWER" / "OHIF"
        decoded = urllib.parse.unquote(request_path)
        if ".." in decoded.replace("\\", "/").split("/"):
            return None
        normalized = posixpath.normpath(decoded)
        if "\0" in decoded or normalized.startswith("../"):
            return None

        if normalized == f"/{STUDY_INDEX}":
            candidate = root / STUDY_INDEX
        elif normalized.startswith("/DICOM/"):
            candidate = root.joinpath(*normalized.lstrip("/").split("/"))
        else:
            relative = normalized.lstrip("/")
            candidate = viewer_root / relative
            if not candidate.is_file():
                if Path(relative).suffix:
                    return None
                candidate = viewer_root / "index.html"

        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            return None
        return resolved

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
        if path.name == STUDY_INDEX:
            return "application/json; charset=utf-8"
        guessed = mimetypes.guess_type(path.name)[0]
        return guessed or "application/octet-stream"


class PdiHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, root: Path, host: str, port: int, *, quiet: bool = False):
        if host != "127.0.0.1":
            raise ValueError("PDI 服务只允许绑定 127.0.0.1")
        self.pdi_root = root
        self.quiet = quiet
        super().__init__((host, port), PdiRequestHandler)


def validate_pdi_root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve()
    required_directories = (
        root / "DICOM",
        root / "VIEWER",
        root / "VIEWER" / "OHIF",
    )
    required_files = (
        root / STUDY_INDEX,
        root / "VIEWER" / "OHIF" / "index.html",
    )
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
    if missing:
        raise FileNotFoundError(f"PDI 目录不完整，缺少：{'、'.join(missing)}")
    for path in (*required_directories, *required_files):
        try:
            path.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise FileNotFoundError(f"PDI 路径超出导出目录：{path.name}") from exc
    return root


def viewer_url(port: int) -> str:
    query = urllib.parse.urlencode({"url": f"/{STUDY_INDEX}", "lng": "zh"})
    return f"http://127.0.0.1:{port}{VIEWER_PATH}?{query}"


def run_server(
    root: str | Path,
    *,
    port: int = 0,
    open_browser: bool = True,
    quiet: bool = False,
) -> int:
    pdi_root = validate_pdi_root(root)
    with PdiHttpServer(pdi_root, "127.0.0.1", port, quiet=quiet) as server:
        url = viewer_url(server.server_address[1])
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
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("端口必须为 0 到 65535")
    try:
        return run_server(
            args.root,
            port=args.port,
            open_browser=not args.no_browser,
            quiet=args.quiet,
        )
    except (OSError, ValueError) as exc:
        print(f"无法启动 PDI 阅片器：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
