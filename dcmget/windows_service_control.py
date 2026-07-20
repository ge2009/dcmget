from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path


LOGGER = logging.getLogger(__name__)

SERVICE_NAME = "kayisoft-dcmget"
SERVICE_WRAPPER_FILENAME = f"{SERVICE_NAME}.exe"
SC_FILENAME = "sc.exe"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
DEFAULT_COMMAND_TIMEOUT_SECONDS = 20.0
DEFAULT_STOP_DELAY_SECONDS = 0.35


class WindowsServiceControlError(RuntimeError):
    """Raised when the installed Windows service cannot be controlled."""


def _decode_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    for encoding in ("utf-8", "gb18030"):
        try:
            return value.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return value.decode("utf-8", errors="replace").strip()


def _status_from_output(output: str) -> str:
    normalized = " ".join(output.casefold().split())
    if any(
        marker in normalized
        for marker in (
            "nonexistent",
            "not installed",
            "does not exist",
            "does not exist as an installed service",
            "未安装",
            "不存在",
        )
    ):
        return "not_installed"
    state_match = re.search(r"(?:state|状态)\s*:\s*([1-7])", normalized)
    if state_match:
        return {
            "1": "stopped",
            "2": "starting",
            "3": "stopping",
            "4": "running",
            "5": "starting",
            "6": "paused",
            "7": "paused",
        }[state_match.group(1)]
    if any(
        marker in normalized
        for marker in ("stop pending", "stop_pending", "stopping", "正在停止")
    ):
        return "stopping"
    if any(
        marker in normalized
        for marker in ("start pending", "start_pending", "starting", "正在启动")
    ):
        return "starting"
    if any(marker in normalized for marker in ("stopped", "inactive", "已停止")):
        return "stopped"
    if any(marker in normalized for marker in ("paused", "已暂停")):
        return "paused"
    if any(marker in normalized for marker in ("started", "running", "active", "运行中")):
        return "running"
    return "unknown"


_STATUS_LABELS = {
    "not_installed": "未安装",
    "stopping": "正在停止",
    "starting": "正在启动",
    "stopped": "已停止",
    "paused": "已暂停",
    "running": "运行中",
    "unknown": "状态未知",
}


class WindowsServiceControl:
    """Control the fixed WinSW wrapper shipped beside the DcmGet executable."""

    def __init__(
        self,
        install_directory: str | Path | None = None,
        *,
        system_root: str | Path | None = None,
        platform_name: str | None = None,
        command_timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        stop_delay_seconds: float = DEFAULT_STOP_DELAY_SECONDS,
    ):
        if install_directory is None:
            if getattr(sys, "frozen", False):
                install_directory = Path(sys.executable).resolve().parent
            else:
                install_directory = Path(__file__).resolve().parents[1]
        self.install_directory = Path(install_directory).expanduser().resolve()
        self.system_root = Path(
            system_root or os.environ.get("SystemRoot", r"C:\Windows")
        ).expanduser()
        self.platform_name = platform_name or sys.platform
        self.command_timeout_seconds = float(command_timeout_seconds)
        self.stop_delay_seconds = float(stop_delay_seconds)
        if self.command_timeout_seconds <= 0:
            raise ValueError("Windows 服务命令超时时间必须大于 0")
        if self.stop_delay_seconds < 0:
            raise ValueError("Windows 服务停止延迟不能小于 0")

    @property
    def wrapper_path(self) -> Path:
        return self.install_directory / SERVICE_WRAPPER_FILENAME

    @property
    def sc_path(self) -> Path:
        return self.system_root / "System32" / SC_FILENAME

    def status(self, _payload: object = None) -> dict[str, object]:
        unsupported = self._unsupported_result("status")
        if unsupported is not None:
            return unsupported
        completed = self._run("query")
        output = self._combined_output(completed)
        status = _status_from_output(output)
        if status == "not_installed":
            return self._not_installed_result("status", output)
        if completed.returncode != 0 and status == "unknown":
            raise WindowsServiceControlError(
                self._command_error("查询", completed.returncode, output)
            )
        return {
            "ok": True,
            "supported": True,
            "service": SERVICE_NAME,
            "action": "status",
            "status": status,
            "status_label": _STATUS_LABELS[status],
            "message": f"DcmGet Windows 服务：{_STATUS_LABELS[status]}",
            "output": output,
        }

    def start(self, _payload: object = None) -> dict[str, object]:
        unsupported = self._unsupported_result("start")
        if unsupported is not None:
            return unsupported
        completed = self._run("start")
        output = self._combined_output(completed)
        if _status_from_output(output) == "not_installed":
            return self._not_installed_result("start", output)
        if completed.returncode != 0:
            raise WindowsServiceControlError(
                self._command_error("启动", completed.returncode, output)
            )
        return {
            "ok": True,
            "supported": True,
            "service": SERVICE_NAME,
            "action": "start",
            "status": "starting",
            "status_label": _STATUS_LABELS["starting"],
            "message": "DcmGet Windows 服务启动命令已提交",
            "output": output,
        }

    def stop(self, _payload: object = None) -> dict[str, object]:
        unsupported = self._unsupported_result("stop")
        if unsupported is not None:
            return unsupported
        sc = self._checked_sc()
        query = self._run("query")
        query_output = self._combined_output(query)
        current_status = _status_from_output(query_output)
        if current_status == "not_installed":
            return self._not_installed_result("stop", query_output)
        if query.returncode != 0 and current_status == "unknown":
            raise WindowsServiceControlError(
                self._command_error("查询", query.returncode, query_output)
            )
        command = [str(sc), "stop", SERVICE_NAME]

        def launch_delayed_stop() -> None:
            if self.stop_delay_seconds:
                time.sleep(self.stop_delay_seconds)
            try:
                # Never wait for Service Control here.  This code normally
                # runs inside the service process being stopped; returning the
                # HTTP response before issuing the control prevents self-wait.
                subprocess.Popen(
                    command,
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                    close_fds=True,
                )
            except OSError:
                LOGGER.exception("无法异步启动 Windows 服务停止命令")

        threading.Thread(
            target=launch_delayed_stop,
            name="dcmget-windows-service-stop",
            daemon=True,
        ).start()
        return {
            "ok": True,
            "supported": True,
            "service": SERVICE_NAME,
            "action": "stop",
            "status": "stopping",
            "status_label": _STATUS_LABELS["stopping"],
            "scheduled": True,
            "delay_seconds": self.stop_delay_seconds,
            "message": "DcmGet Windows 服务停止命令已安排，任务恢复点会保留",
        }

    def handlers(self) -> dict[str, Callable[[object], dict[str, object]]]:
        return {
            "windows-service-status": self.status,
            "windows-service-start": self.start,
            "windows-service-stop": self.stop,
        }

    def _unsupported_result(self, action: str) -> dict[str, object] | None:
        if self.platform_name == "win32" and self.wrapper_path.is_file():
            return None
        message = (
            "当前 Windows 运行方式未安装 kayisoft-dcmget 服务"
            if self.platform_name == "win32"
            else "仅 Windows 安装版支持"
        )
        return {
            "ok": False,
            "supported": False,
            "service": SERVICE_NAME,
            "action": action,
            "status": (
                "not_installed" if self.platform_name == "win32" else "unsupported"
            ),
            "status_label": (
                "未安装" if self.platform_name == "win32" else "不支持"
            ),
            "message": message,
        }

    @staticmethod
    def _not_installed_result(action: str, detail: str) -> dict[str, object]:
        return {
            "ok": False,
            "supported": False,
            "service": SERVICE_NAME,
            "action": action,
            "status": "not_installed",
            "status_label": _STATUS_LABELS["not_installed"],
            "message": "DcmGet Windows 服务未安装",
            "output": detail,
        }

    def _checked_sc(self) -> Path:
        sc = self.sc_path
        if not sc.is_file():
            raise WindowsServiceControlError(
                f"未找到 Windows 服务控制程序：{sc}"
            )
        return sc

    def _run(self, action: str) -> subprocess.CompletedProcess[bytes]:
        sc = self._checked_sc()
        try:
            return subprocess.run(
                [str(sc), action, SERVICE_NAME],
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.command_timeout_seconds,
                check=False,
                creationflags=CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise WindowsServiceControlError(
                f"Windows 服务{self._action_label(action)}超时"
            ) from exc
        except PermissionError as exc:
            raise WindowsServiceControlError(
                f"Windows 服务{self._action_label(action)}失败：请检查服务控制权限"
            ) from exc
        except OSError as exc:
            raise WindowsServiceControlError(
                f"Windows 服务{self._action_label(action)}失败：{exc}"
            ) from exc

    @staticmethod
    def _combined_output(completed: subprocess.CompletedProcess[bytes]) -> str:
        stdout = _decode_output(completed.stdout)
        stderr = _decode_output(completed.stderr)
        return "\n".join(part for part in (stdout, stderr) if part)[:4096]

    @staticmethod
    def _command_error(action: str, returncode: int, output: str) -> str:
        detail = output or "服务控制器未返回详细信息"
        return f"Windows 服务{action}失败（退出码 {returncode}）：{detail}"

    @staticmethod
    def _action_label(action: str) -> str:
        return {"query": "状态查询", "start": "启动", "stop": "停止"}.get(
            action,
            "控制",
        )


def windows_service_operation_handlers(
    install_directory: str | Path | None = None,
) -> Mapping[str, Callable[[object], dict[str, object]]]:
    """Return handlers ready to merge into the Web operation handler map."""

    return WindowsServiceControl(install_directory).handlers()
