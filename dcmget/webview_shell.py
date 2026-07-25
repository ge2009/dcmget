from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from . import __version__

LOGGER = logging.getLogger("dcmget.diagnostics")
MAX_BOOTSTRAP_BYTES = 2 * 1024 * 1024


class WebViewShellError(RuntimeError):
    pass


def validate_loopback_url(value: str) -> str:
    url = str(value or "").strip()
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise WebViewShellError("WebView 地址无效") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or not 1 <= port <= 65535
    ):
        raise WebViewShellError("WebView 只允许打开本机 HTTP 工作台")
    return url


def wait_until_ready(
    url: str,
    *,
    timeout: float = 20.0,
    poll_interval: float = 0.1,
    urlopen: Callable[..., Any] | None = None,
) -> bool:
    """Wait for the same-version DcmGet bootstrap API, not just an open port."""

    target = validate_loopback_url(url)
    parsed = urlsplit(target)
    bootstrap_url = urlunsplit(
        (parsed.scheme, parsed.netloc, "/api/bootstrap", "", "")
    )
    probe = urlopen or urllib.request.urlopen
    deadline = time.monotonic() + max(0.0, float(timeout))
    attempted = False
    while not attempted or time.monotonic() <= deadline:
        attempted = True
        try:
            response = probe(
                bootstrap_url,
                timeout=min(0.5, max(0.05, timeout)),
            )
            try:
                status = int(getattr(response, "status", 200))
                raw = response.read(MAX_BOOTSTRAP_BYTES + 1)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            if status == 200 and len(raw) <= MAX_BOOTSTRAP_BYTES:
                payload = json.loads(raw.decode("utf-8"))
                if (
                    isinstance(payload, dict)
                    and payload.get("version") == __version__
                    and payload.get("mode") in {"manager", "profile"}
                    and isinstance(payload.get("csrf_token"), str)
                    and bool(payload["csrf_token"])
                ):
                    return True
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            ValueError,
            OSError,
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
        ):
            pass
        if time.monotonic() <= deadline:
            time.sleep(max(0.0, poll_interval))
    return False


def run_webview_shell(
    url: str,
    *,
    timeout: float = 20.0,
    urlopen: Callable[..., Any] | None = None,
    webview_module: Any | None = None,
) -> int:
    target = validate_loopback_url(url)
    if not wait_until_ready(target, timeout=timeout, urlopen=urlopen):
        raise WebViewShellError(
            "DcmGet 后台服务尚未就绪，或目标端口不是当前版本的 DcmGet；"
            "请确认 kayisoft-dcmget 服务已启动"
        )
    if webview_module is None:
        try:
            import webview as webview_module
        except ImportError as exc:
            raise WebViewShellError("缺少 Windows WebView 组件，请重新安装 DcmGet") from exc
    try:
        LOGGER.info(
            "Starting WebView2 shell url=%s",
            target,
        )
        webview_module.create_window(
            "DcmGet DICOM 影像下载",
            target,
            width=1440,
            height=900,
            min_size=(1024, 720),
            background_color="#f3f7f9",
            text_select=True,
        )
        webview_module.start(gui="edgechromium", debug=False)
    except Exception as exc:
        LOGGER.exception("WebView2 shell startup failed")
        raise WebViewShellError(
            "无法启动 Windows WebView2，请修复或安装 Microsoft Edge WebView2 Runtime"
        ) from exc
    return 0


def build_shell_command(
    url: str,
    *,
    executable: str | Path | None = None,
    frozen: bool | None = None,
    entrypoint: str | Path | None = None,
) -> list[str]:
    target = validate_loopback_url(url)
    program = Path(executable or sys.executable).expanduser().resolve()
    packaged = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    if packaged:
        return [str(program), "--native-shell-url", target]
    script = Path(entrypoint or Path(__file__).resolve().parents[1] / "DICOM_download_ui.py")
    return [str(program), str(script.resolve()), "--native-shell-url", target]


def spawn_webview_shell(
    url: str,
    *,
    executable: str | Path | None = None,
    frozen: bool | None = None,
    entrypoint: str | Path | None = None,
    popen: Callable[..., Any] | None = None,
) -> int:
    command = build_shell_command(
        url,
        executable=executable,
        frozen=frozen,
        entrypoint=entrypoint,
    )
    process = (popen or subprocess.Popen)(
        command,
        shell=False,
        close_fds=(sys.platform != "win32"),
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if sys.platform == "win32"
            else 0
        ),
    )
    return int(process.pid)
