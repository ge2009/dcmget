from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from dcmget.webview_shell import (
    WebViewShellError,
    build_shell_command,
    run_webview_shell,
    spawn_webview_shell,
    validate_loopback_url,
)


class _Response:
    def close(self) -> None:
        return None


def test_webview_shell_rejects_non_loopback_urls():
    with pytest.raises(WebViewShellError, match="只允许"):
        validate_loopback_url("http://192.168.1.50:8786/")
    with pytest.raises(WebViewShellError, match="只允许"):
        validate_loopback_url("https://127.0.0.1:8786/")


def test_webview_shell_waits_for_http_then_uses_edgechromium():
    attempts = 0
    calls: list[tuple[str, object]] = []

    def urlopen(_url: str, **_kwargs: object) -> _Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("starting")
        return _Response()

    fake = SimpleNamespace(
        create_window=lambda title, url, **kwargs: calls.append(
            ("window", (title, url, kwargs))
        ),
        start=lambda **kwargs: calls.append(("start", kwargs)),
    )

    assert run_webview_shell(
        "http://127.0.0.1:8786/",
        timeout=1,
        urlopen=urlopen,
        webview_module=fake,
    ) == 0
    assert attempts == 2
    assert calls[0][0] == "window"
    assert calls[1] == ("start", {"gui": "edgechromium", "debug": False})


def test_webview_shell_reports_missing_runtime_without_browser_fallback():
    fake = SimpleNamespace(
        create_window=lambda *_args, **_kwargs: None,
        start=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("runtime missing")),
    )
    with pytest.raises(WebViewShellError, match="WebView2"):
        run_webview_shell(
            "http://127.0.0.1:8786/",
            urlopen=lambda *_args, **_kwargs: _Response(),
            webview_module=fake,
        )


def test_webview_shell_builds_frozen_and_source_commands(tmp_path: Path):
    executable = tmp_path / "DcmGet.exe"
    entrypoint = tmp_path / "DICOM_download_ui.py"
    frozen = build_shell_command(
        "http://127.0.0.1:8786/",
        executable=executable,
        frozen=True,
    )
    source = build_shell_command(
        "http://127.0.0.1:8787/?profile=2",
        executable=executable,
        frozen=False,
        entrypoint=entrypoint,
    )
    assert frozen == [
        str(executable.resolve()),
        "--native-shell-url",
        "http://127.0.0.1:8786/",
    ]
    assert source == [
        str(executable.resolve()),
        str(entrypoint.resolve()),
        "--native-shell-url",
        "http://127.0.0.1:8787/?profile=2",
    ]


def test_spawn_webview_shell_uses_argument_array(tmp_path: Path):
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Process:
        pid = 1234

    def popen(command: list[str], **kwargs: object) -> Process:
        calls.append((command, kwargs))
        return Process()

    assert spawn_webview_shell(
        "http://127.0.0.1:8786/",
        executable=tmp_path / "DcmGet.exe",
        frozen=True,
        popen=popen,
    ) == 1234
    assert calls[0][0][-2:] == ["--native-shell-url", "http://127.0.0.1:8786/"]
    assert calls[0][1]["shell"] is False
