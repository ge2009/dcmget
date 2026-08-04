from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dcmget import __version__
from dcmget.webview_shell import (
    NativeShellApi,
    WebViewShellError,
    build_shell_command,
    run_webview_shell,
    spawn_webview_shell,
    validate_loopback_url,
    wait_until_ready,
)


class _Response:
    status = 200

    def __init__(self, payload: object | None = None) -> None:
        self.payload = payload or {
            "version": __version__,
            "mode": "manager",
            "csrf_token": "test-token",
        }

    def read(self, _limit: int = -1) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

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
    assert isinstance(calls[0][1][2]["js_api"], NativeShellApi)
    assert calls[1] == ("start", {"gui": "edgechromium", "debug": False})


def test_native_shell_api_opens_only_an_existing_directory_without_shell(tmp_path: Path):
    calls: list[tuple[list[str], dict[str, object]]] = []

    def popen(command: list[str], **kwargs: object) -> object:
        calls.append((command, kwargs))
        return object()

    directory = tmp_path / "SMB results"
    directory.mkdir()
    api = NativeShellApi(platform_name="win32", popen=popen)

    result = api.open_directory(str(directory))

    assert result["path"] == str(directory.resolve())
    assert calls[0][0] == ["explorer.exe", str(directory.resolve())]
    assert calls[0][1]["shell"] is False


def test_native_shell_api_rejects_missing_paths_and_files(tmp_path: Path):
    calls: list[list[str]] = []
    api = NativeShellApi(
        platform_name="win32",
        popen=lambda command, **_kwargs: calls.append(command),
    )
    file_path = tmp_path / "image.dcm"
    file_path.write_bytes(b"DICM")

    with pytest.raises(WebViewShellError, match="不存在"):
        api.open_directory(str(tmp_path / "missing"))
    with pytest.raises(WebViewShellError, match="不是目录"):
        api.open_directory(str(file_path))
    with pytest.raises(WebViewShellError, match="路径无效"):
        api.open_directory("bad\0path")
    assert calls == []


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


@pytest.mark.parametrize(
    "payload",
    [
        b"<html>another service</html>",
        json.dumps(
            {
                "version": "2.9.1",
                "mode": "manager",
                "csrf_token": "old-token",
            }
        ).encode("utf-8"),
        json.dumps(
            {"version": __version__, "mode": "manager", "csrf_token": ""}
        ).encode("utf-8"),
    ],
)
def test_webview_shell_rejects_another_service_or_stale_version(payload: bytes):
    class Response:
        status = 200

        def read(self, _limit: int = -1) -> bytes:
            return payload

        def close(self) -> None:
            return None

    with pytest.raises(WebViewShellError, match="不是当前版本"):
        run_webview_shell(
            "http://127.0.0.1:8786/",
            timeout=0,
            urlopen=lambda *_args, **_kwargs: Response(),
            webview_module=SimpleNamespace(),
        )


def test_webview_shell_accepts_a_large_current_task_bootstrap():
    payload = {
        "version": __version__,
        "mode": "profile",
        "csrf_token": "test-token",
        "task": {"results": [{"message": "x" * 80_000}]},
    }

    assert wait_until_ready(
        "http://127.0.0.1:8786/profiles/1",
        timeout=0,
        urlopen=lambda *_args, **_kwargs: _Response(payload),
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
