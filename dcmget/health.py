from __future__ import annotations

import os
import platform
import shutil
import socket
import struct
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import psutil

from . import __version__
from .architecture import ArchitectureError, ensure_supported_runtime
from .config import AppConfig
from .core import DcmtkResolver, ToolPaths


HEALTH_SCHEMA_VERSION = 1
DEFAULT_MINIMUM_FREE_BYTES = 1024 * 1024 * 1024
_RELEVANT_PROCESS_NAMES = {
    "dcmget",
    "dcmget.exe",
    "dcmgetpdiserver",
    "dcmgetpdiserver.exe",
    "movescu",
    "movescu.exe",
    "storescp",
    "storescp.exe",
    "dcmmkdir",
    "dcmmkdir.exe",
    "dcmdump",
    "dcmdump.exe",
}


@dataclass(frozen=True, slots=True)
class HealthCheck:
    check_id: str
    status: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.check_id,
            "status": self.status,
            "summary": self.summary,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class HealthReport:
    generated_at: str
    checks: tuple[HealthCheck, ...]
    schema_version: int = HEALTH_SCHEMA_VERSION

    @property
    def status(self) -> str:
        statuses = {check.status for check in self.checks}
        if "error" in statuses:
            return "error"
        if "warning" in statuses:
            return "warning"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "dcmget-health",
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "status": self.status,
            "checks": [check.to_dict() for check in self.checks],
        }


def run_health_check(
    config: AppConfig,
    *,
    project_root: str | Path | None = None,
    resolver: DcmtkResolver | None = None,
    minimum_free_bytes: int = DEFAULT_MINIMUM_FREE_BYTES,
    process_iter: Callable[..., Iterable[psutil.Process]] | None = None,
    check_pacs: bool = False,
    pacs_timeout_seconds: float = 3.0,
    connection_factory: Callable[..., Any] | None = None,
    now: datetime | None = None,
) -> HealthReport:
    """Run workstation checks, optionally including PACS TCP reachability."""

    checks = [
        _runtime_check(),
        _dcmtk_check(config, resolver or DcmtkResolver(project_root)),
        _destination_check(config, max(0, int(minimum_free_bytes))),
        _receiver_port_check(config.storage_port),
    ]
    if check_pacs:
        checks.append(
            _pacs_connectivity_check(
                config.pacs_server_ip,
                config.pacs_server_port,
                timeout_seconds=pacs_timeout_seconds,
                connection_factory=connection_factory or socket.create_connection,
            )
        )
    checks.append(_process_check(process_iter or psutil.process_iter))
    generated = now or datetime.now(timezone.utc)
    return HealthReport(generated.isoformat(), tuple(checks))


def _runtime_check() -> HealthCheck:
    bits = struct.calcsize("P") * 8
    details = {
        "app_version": __version__,
        "python_version": platform.python_version(),
        "pointer_bits": bits,
        "platform": sys.platform,
        "machine": platform.machine(),
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    try:
        ensure_supported_runtime()
    except (ArchitectureError, OSError) as exc:
        return HealthCheck("runtime", "error", str(exc), details)
    return HealthCheck(
        "runtime",
        "ok",
        f"DcmGet {__version__}，{bits} 位运行时",
        details,
    )


def _dcmtk_check(config: AppConfig, resolver: DcmtkResolver) -> HealthCheck:
    try:
        tools = resolver.resolve(config.dcmtk_bin_dir)
    except Exception as exc:
        return HealthCheck(
            "dcmtk",
            "error",
            f"DCMTK 不可用：{exc}",
            {"configured": bool(config.dcmtk_bin_dir.strip()), "tools": {}},
        )

    tool_paths = _tool_paths(tools)
    required = {"movescu", "storescp"}
    if config.pdi_export_enabled:
        required.update(("dcmmkdir", "dcmdump"))
    missing = sorted(name for name in required if not tool_paths.get(name))
    details = {
        "version": tools.version,
        "bin_directory": str(tools.bin_dir),
        "required": sorted(required),
        "missing": missing,
        "tools": tool_paths,
    }
    if missing:
        return HealthCheck(
            "dcmtk",
            "error",
            f"缺少 DCMTK 工具：{'、'.join(missing)}",
            details,
        )
    return HealthCheck(
        "dcmtk",
        "ok",
        f"DCMTK {tools.version} 已就绪",
        details,
    )


def _tool_paths(tools: ToolPaths) -> dict[str, str | None]:
    values = {
        "movescu": tools.movescu,
        "storescp": tools.storescp,
        "dcmmkdir": tools.dcmmkdir,
        "dcmdump": tools.dcmdump,
    }
    return {
        name: str(path) if path is not None and Path(path).is_file() else None
        for name, path in values.items()
    }


def _destination_check(config: AppConfig, minimum_free_bytes: int) -> HealthCheck:
    destination = Path(config.dicom_destination_folder).expanduser()
    details: dict[str, Any] = {
        "path": str(destination),
        "writable": False,
        "minimum_free_bytes": minimum_free_bytes,
    }
    probe: Path | None = None
    try:
        destination.mkdir(parents=True, exist_ok=True)
        descriptor, probe_name = tempfile.mkstemp(
            prefix=".dcmget-health-", dir=destination
        )
        os.close(descriptor)
        probe = Path(probe_name)
        usage = shutil.disk_usage(destination)
        details.update(
            {
                "writable": True,
                "disk_total_bytes": usage.total,
                "disk_used_bytes": usage.used,
                "disk_free_bytes": usage.free,
            }
        )
    except OSError as exc:
        details["error"] = str(exc)
        return HealthCheck(
            "destination",
            "error",
            f"目标目录不可写：{exc}",
            details,
        )
    finally:
        if probe is not None:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass

    if usage.free < minimum_free_bytes:
        return HealthCheck(
            "destination",
            "warning",
            f"目标目录可写，但剩余空间不足 {minimum_free_bytes} 字节",
            details,
        )
    return HealthCheck("destination", "ok", "目标目录可写且磁盘空间可用", details)


def _receiver_port_check(port: int, host: str = "0.0.0.0") -> HealthCheck:
    details: dict[str, Any] = {"host": host, "port": port, "available": False}
    if not 1 <= int(port) <= 65535:
        return HealthCheck("receiver_port", "error", "接收端口无效", details)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if os.name == "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind((host, int(port)))
    except OSError as exc:
        details["error"] = str(exc)
        return HealthCheck(
            "receiver_port",
            "error",
            f"接收端口 {port} 已占用或不可绑定",
            details,
        )
    details["available"] = True
    return HealthCheck("receiver_port", "ok", f"接收端口 {port} 可用", details)


def _pacs_connectivity_check(
    host: str,
    port: int,
    *,
    timeout_seconds: float,
    connection_factory: Callable[..., Any],
) -> HealthCheck:
    details: dict[str, Any] = {
        "host": str(host),
        "port": int(port),
        "timeout_seconds": float(timeout_seconds),
        "tcp_reachable": False,
        "scope": "tcp_only",
    }
    if not str(host).strip() or not 1 <= int(port) <= 65535:
        return HealthCheck("pacs_tcp", "error", "PACS 地址或端口无效", details)
    try:
        connection = connection_factory(
            (str(host).strip(), int(port)),
            timeout=max(0.1, float(timeout_seconds)),
        )
        close = getattr(connection, "close", None)
        if callable(close):
            close()
    except OSError as exc:
        details["error"] = str(exc)
        return HealthCheck(
            "pacs_tcp",
            "error",
            f"无法连接 PACS {host}:{port}",
            details,
        )
    details["tcp_reachable"] = True
    return HealthCheck(
        "pacs_tcp",
        "ok",
        f"PACS {host}:{port} TCP 端口可达",
        details,
    )


def _process_check(
    process_iter: Callable[..., Iterable[psutil.Process]],
) -> HealthCheck:
    processes: list[dict[str, Any]] = []
    failures = 0
    try:
        values = process_iter(["pid", "name", "status"])
    except (OSError, psutil.Error) as exc:
        return HealthCheck(
            "processes",
            "warning",
            f"无法读取进程摘要：{exc}",
            {"processes": [], "read_failures": 1},
        )

    for process in values:
        try:
            info = process.info
            name = str(info.get("name") or "")
            if (
                int(info.get("pid") or -1) != os.getpid()
                and name.lower() not in _RELEVANT_PROCESS_NAMES
            ):
                continue
            processes.append(
                {
                    "pid": int(info.get("pid") or 0),
                    "name": name,
                    "status": str(info.get("status") or "unknown"),
                    "current": int(info.get("pid") or -1) == os.getpid(),
                }
            )
        except (KeyError, TypeError, ValueError, psutil.Error):
            failures += 1
    processes.sort(key=lambda item: (str(item["name"]).lower(), int(item["pid"])))
    return HealthCheck(
        "processes",
        "warning" if failures else "ok",
        f"发现 {len(processes)} 个 DcmGet/DCMTK 相关进程",
        {"processes": processes, "read_failures": failures},
    )
