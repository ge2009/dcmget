from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dcmget import windows_service_control as service_control
from dcmget.windows_service_control import (
    CREATE_NO_WINDOW,
    SERVICE_NAME,
    SERVICE_WRAPPER_FILENAME,
    WindowsServiceControl,
    WindowsServiceControlError,
    windows_service_operation_handlers,
)


def _wrapper(directory: Path) -> Path:
    path = directory / SERVICE_WRAPPER_FILENAME
    path.write_bytes(b"stub")
    return path


def _installed(directory: Path) -> tuple[Path, Path, Path]:
    wrapper = _wrapper(directory)
    system_root = directory / "Windows"
    sc = system_root / "System32" / "sc.exe"
    sc.parent.mkdir(parents=True)
    sc.write_bytes(b"stub")
    return wrapper, system_root, sc


def test_status_uses_system_sc_and_hidden_array_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _wrapper_path, system_root, sc = _installed(tmp_path)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, b"Started\r\n", b"")

    monkeypatch.setattr(service_control.subprocess, "run", fake_run)
    control = WindowsServiceControl(
        tmp_path,
        system_root=system_root,
        platform_name="win32",
    )

    result = control.status()

    assert result["status"] == "running"
    assert result["status_label"] == "运行中"
    assert calls == [
        (
            [str(sc), "query", SERVICE_NAME],
            {
                "shell": False,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "timeout": 20.0,
                "check": False,
                "creationflags": CREATE_NO_WINDOW,
            },
        )
    ]


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (b"Stopped", "stopped"),
        (b"Start Pending", "starting"),
        (b"Stop Pending", "stopping"),
        (b"Paused", "paused"),
        (b"NonExistent", "not_installed"),
        (b"STATE : 4", "running"),
        ("状态 : 1".encode(), "stopped"),
    ],
)
def test_status_parses_service_control_states_even_when_exit_is_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: bytes,
    expected: str,
):
    _wrapper_path, system_root, _sc = _installed(tmp_path)
    monkeypatch.setattr(
        service_control.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, output, b""),
    )

    assert WindowsServiceControl(
        tmp_path,
        system_root=system_root,
        platform_name="win32",
    ).status()["status"] == expected


def test_start_reports_explicit_chinese_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _wrapper_path, system_root, _sc = _installed(tmp_path)
    monkeypatch.setattr(
        service_control.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            5,
            b"",
            "拒绝访问".encode("gb18030"),
        ),
    )

    with pytest.raises(
        WindowsServiceControlError,
        match=r"Windows 服务启动失败（退出码 5）：拒绝访问",
    ):
        WindowsServiceControl(
            tmp_path,
            system_root=system_root,
            platform_name="win32",
        ).start()


def test_missing_wrapper_is_reported_as_unavailable_for_portable_windows(tmp_path: Path):
    control = WindowsServiceControl(tmp_path, platform_name="win32")

    for method in (control.status, control.start, control.stop):
        result = method()
        assert result == {
            "ok": False,
            "supported": False,
            "service": "kayisoft-dcmget",
            "action": method.__name__,
            "status": "not_installed",
            "status_label": "未安装",
            "message": "当前 Windows 运行方式未安装 kayisoft-dcmget 服务",
        }


def test_missing_installed_service_is_reported_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _wrapper_path, system_root, _sc = _installed(tmp_path)
    monkeypatch.setattr(
        service_control.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            1060,
            b"The specified service does not exist as an installed service.",
            b"",
        ),
    )

    result = WindowsServiceControl(
        tmp_path,
        system_root=system_root,
        platform_name="win32",
    ).status()

    assert result["ok"] is False
    assert result["supported"] is False
    assert result["status"] == "not_installed"


def test_non_windows_returns_supported_false_without_running_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        service_control.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("非 Windows 不应启动服务包装器"),
    )
    control = WindowsServiceControl(tmp_path, platform_name="darwin")

    for method in (control.status, control.start, control.stop):
        assert method() == {
            "ok": False,
            "supported": False,
            "service": "kayisoft-dcmget",
            "action": method.__name__,
            "status": "unsupported",
            "status_label": "不支持",
            "message": "仅 Windows 安装版支持",
        }


def test_stop_is_delayed_and_never_waits_for_service_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _wrapper_path, system_root, sc = _installed(tmp_path)
    events: list[object] = []

    class ImmediateThread:
        def __init__(self, *, target, name: str, daemon: bool):
            events.append(("thread", name, daemon))
            self.target = target

        def start(self) -> None:
            events.append("thread-start")
            self.target()

    class ProcessWithoutWait:
        def wait(self):
            pytest.fail("异步停止不能等待服务控制命令返回")

        def communicate(self):
            pytest.fail("异步停止不能等待服务控制命令返回")

    def fake_popen(command: list[str], **kwargs: object):
        events.append(("popen", command, kwargs))
        return ProcessWithoutWait()

    monkeypatch.setattr(service_control.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        service_control.time,
        "sleep",
        lambda seconds: events.append(("sleep", seconds)),
    )
    monkeypatch.setattr(service_control.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        service_control.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            b"STATE : 4 RUNNING",
            b"",
        ),
    )

    result = WindowsServiceControl(
        tmp_path,
        system_root=system_root,
        platform_name="win32",
        stop_delay_seconds=0.5,
    ).stop()

    assert result["scheduled"] is True
    assert result["status"] == "stopping"
    assert events[:3] == [
        ("thread", "dcmget-windows-service-stop", True),
        "thread-start",
        ("sleep", 0.5),
    ]
    popen = events[3]
    assert isinstance(popen, tuple)
    assert popen[0] == "popen"
    assert popen[1] == [str(sc), "stop", SERVICE_NAME]
    assert popen[2] == {
        "shell": False,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "creationflags": CREATE_NO_WINDOW,
        "close_fds": True,
    }


def test_handlers_are_payload_compatible_and_json_safe(tmp_path: Path):
    handlers = windows_service_operation_handlers(tmp_path)

    assert set(handlers) == {
        "windows-service-status",
        "windows-service-start",
        "windows-service-stop",
    }
    result = handlers["windows-service-status"]({"ignored": True})
    assert result["supported"] is False
    assert all(isinstance(key, str) for key in result)
