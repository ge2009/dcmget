from __future__ import annotations

import argparse
import ctypes
import errno
from io import BytesIO
import ipaddress
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from pathlib import Path

from dcmget import __version__
from dcmget.diagnostics import (
    diagnostic_log_path,
    install_diagnostics,
    record_exception,
)


install_diagnostics(__version__)

from dcmget.app_service import DcmGetAppService
from dcmget.architecture import ensure_supported_runtime
from dcmget.config import AppConfig, load_config, save_config
from dcmget.core import DcmtkResolver
from dcmget.instance_profile import (
    InstanceProfile,
    ProfileInUseError,
    acquire_instance_profile,
    instance_activation_path,
    migrate_legacy_checkpoint_to_profile,
    migrate_task_catalog_to_profiles,
)
from dcmget.licensing import PUBLIC_KEY_PEM, trial_status
from dcmget.management_server import run_windows_management_server
from dcmget.profile_manager import ProfileManager
from dcmget.profile_web_operations import ProfileWebOperations
from dcmget.release_notes import load_release_notes
from dcmget.runtime import (
    application_state_dir,
    ensure_default_config,
    is_frozen,
    portable_dcmtk_bin,
    resource_root,
)
from dcmget.single_instance import SingleInstance
from dcmget.task_state import TaskCheckpointStore
from dcmget.windows_portable_runtime import prepare_windows_portable_dcmtk
from dcmget.windows_service_control import windows_service_operation_handlers
from dcmget.web_security import DirectoryRoot, discover_local_hosts
from dcmget.web_server import DcmGetWebServer
from dcmget.webview_shell import run_webview_shell, spawn_webview_shell


PROJECT_ROOT = resource_root()
LOGGER = logging.getLogger("dcmget.diagnostics")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"DcmGet {__version__} 局域网 Web 工作台")
    parser.add_argument(
        "--config",
        default=str(ensure_default_config()),
        help="实例配置初始化模板路径",
    )
    parser.add_argument(
        "--profile",
        type=_positive_profile_number,
        help="指定实例编号（正整数）；未指定时自动选择空闲实例",
    )
    parser.add_argument(
        "--open-profile-web",
        action="store_true",
        help="启动或唤醒指定实例，并在服务就绪后打开 Web 页面",
    )
    parser.add_argument(
        "--no-open-browser",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--windows-management",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--native-shell-url", help=argparse.SUPPRESS)
    parser.add_argument("--profile-name", help="启动前保存 Profile 显示名")
    parser.add_argument("--pacs-server-ip", help="启动前保存 PACS 地址")
    parser.add_argument("--pacs-server-port", type=int, help="启动前保存 PACS 端口")
    parser.add_argument("--calling-ae-title", help="启动前保存本机调用 AE")
    parser.add_argument("--pacs-ae-title", help="启动前保存 PACS AE")
    parser.add_argument("--storage-ae-title", help="启动前保存接收 AE")
    parser.add_argument("--storage-port", type=int, help="启动前保存 DICOM 接收端口")
    parser.add_argument("--web-port", type=int, help="启动前保存 Web 端口")
    parser.add_argument(
        "--dicom-destination-folder",
        help="启动前保存 DICOM 目标目录",
    )
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--self-test-report", help=argparse.SUPPRESS)
    parser.add_argument(
        "--web-self-test",
        "--ui-self-test",
        dest="web_self_test",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def _positive_profile_number(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("实例编号必须是正整数") from exc
    if number < 1 or number > 9999:
        raise argparse.ArgumentTypeError("实例编号必须在 1 到 9999 之间")
    return number


def _profile_updates(args: argparse.Namespace) -> dict[str, object]:
    fields = {
        "display_name": "profile_name",
        "pacs_server_ip": "pacs_server_ip",
        "pacs_server_port": "pacs_server_port",
        "calling_ae_title": "calling_ae_title",
        "pacs_ae_title": "pacs_ae_title",
        "storage_ae_title": "storage_ae_title",
        "storage_port": "storage_port",
        "web_port": "web_port",
        "dicom_destination_folder": "dicom_destination_folder",
    }
    return {
        config_field: value
        for config_field, argument_name in fields.items()
        if (value := getattr(args, argument_name, None)) is not None
    }


def run_self_test(config_path: str, report_path: str | None = None) -> int:
    from tempfile import TemporaryDirectory

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from pydicom import dcmread
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

    from dcmget.anonymization import DicomAnonymizer
    from dcmget.config import AppConfig
    from dcmget.pdi import PdiExporter, STUDY_INDEX

    load_config(config_path)
    validate_web_resources(PROJECT_ROOT)
    trial_status()
    if f"## {__version__}" not in load_release_notes(PROJECT_ROOT):
        raise RuntimeError("版本说明文件缺失或与程序版本不一致")
    public_key = serialization.load_pem_public_key(PUBLIC_KEY_PEM)
    if not isinstance(public_key, Ed25519PublicKey):
        raise RuntimeError("授权公钥类型错误")
    validate_frozen_pdi_resources(PROJECT_ROOT)

    file_meta = FileMetaDataset()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.SOPInstanceUID = "1.2.826.0.1.3680043.10.999.1"
    dataset.PatientID = "SELF-TEST-PATIENT"
    dataset.AccessionNumber = "SELF-TEST"
    DicomAnonymizer(
        "research", secret=b"dcmget-self-test-key-material-32b"
    ).anonymize_dataset(dataset)
    if not str(dataset.PatientID).startswith("ANON-"):
        raise RuntimeError("pydicom 匿名处理自检失败")
    buffer = BytesIO()
    dataset.save_as(buffer)
    buffer.seek(0)
    if str(dcmread(buffer).SOPInstanceUID) != str(dataset.SOPInstanceUID):
        raise RuntimeError("pydicom 读写自检失败")

    tools = DcmtkResolver(PROJECT_ROOT).resolve()
    if tools.version != "3.7.0" or not tools.supports_fork:
        raise RuntimeError(
            f"DCMTK 自检失败：version={tools.version}, fork={tools.supports_fork}"
        )
    if is_frozen():
        with TemporaryDirectory(prefix="dcmget-pdi-self-test-") as temporary:
            root = Path(temporary)
            source = root / "source.dcm"
            instance_uid = generate_uid()
            pdi_meta = FileMetaDataset()
            pdi_meta.MediaStorageSOPClassUID = CTImageStorage
            pdi_meta.MediaStorageSOPInstanceUID = instance_uid
            pdi_meta.TransferSyntaxUID = ExplicitVRLittleEndian
            pdi_dataset = FileDataset(
                None, {}, file_meta=pdi_meta, preamble=b"\0" * 128
            )
            pdi_dataset.SOPClassUID = CTImageStorage
            pdi_dataset.SOPInstanceUID = instance_uid
            pdi_dataset.StudyInstanceUID = generate_uid()
            pdi_dataset.SeriesInstanceUID = generate_uid()
            pdi_dataset.PatientName = "DcmGet^SelfTest"
            pdi_dataset.PatientID = "SELFTEST"
            pdi_dataset.AccessionNumber = "PDI-SELF-TEST"
            pdi_dataset.Modality = "CT"
            pdi_dataset.StudyDate = "20260716"
            pdi_dataset.StudyTime = "120000"
            pdi_dataset.StudyID = "SELFTEST"
            pdi_dataset.SeriesNumber = 1
            pdi_dataset.InstanceNumber = 1
            pdi_dataset.Rows = 1
            pdi_dataset.Columns = 1
            pdi_dataset.SamplesPerPixel = 1
            pdi_dataset.PhotometricInterpretation = "MONOCHROME2"
            pdi_dataset.BitsAllocated = 8
            pdi_dataset.BitsStored = 8
            pdi_dataset.HighBit = 7
            pdi_dataset.PixelRepresentation = 0
            pdi_dataset.PixelData = b"\0"
            pdi_dataset.save_as(source, enforce_file_format=True)
            pdi_config = AppConfig(
                dicom_destination_folder=str(root / "dicom"),
                pdi_export_enabled=True,
                pdi_institution_name="DcmGet Self Test",
                pdi_output_folder=str(root / "pdi"),
                pdi_include_ohif_viewer=True,
            )
            pdi_result = PdiExporter(
                pdi_config, tools, project_root=PROJECT_ROOT
            ).export([source])
            if not pdi_result.output_directory:
                raise RuntimeError(f"PDI 冻结资源自检失败：{pdi_result.message}")
            pdi_output = Path(pdi_result.output_directory)
            required_pdi = (
                pdi_output / "DICOMDIR",
                pdi_output / STUDY_INDEX,
                pdi_output / "VIEWER" / "OHIF" / "index.html",
                pdi_output / "VIEWER" / "pdi_server.py",
                pdi_output / "VIEWER" / "architecture.py",
                pdi_output / "OPEN_VIEWER.exe",
                pdi_output / "OPEN_VIEWER.bat",
            )
            missing_pdi = [path.name for path in required_pdi if not path.is_file()]
            if missing_pdi:
                raise RuntimeError(
                    f"PDI 冻结资源自检缺少文件：{'、'.join(missing_pdi)}"
                )
            index_text = (pdi_output / "INDEX.HTM").read_text(encoding="utf-8")
            if "本次导出未能加入离线阅片器" in index_text:
                raise RuntimeError("PDI 冻结资源自检未能加入离线阅片器")
    if report_path:
        _write_self_test_report(
            report_path,
            {
                "resource_root": str(PROJECT_ROOT.resolve()),
                "storescp": str(tools.storescp.resolve()),
                "portable_dcmtk_bin": (
                    str(portable_dcmtk_bin()) if portable_dcmtk_bin() else None
                ),
            },
        )
    print(f"DcmGet {__version__} self-test OK; DCMTK {tools.version}; fork=yes")
    return 0


def _write_self_test_report(path: str | Path, report: dict[str, object]) -> None:
    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(
        f".{output.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def validate_frozen_pdi_resources(root: str | Path) -> None:
    if not is_frozen():
        return
    base = Path(root)
    ohif = base / ".runtime" / "ohif" / "ohif-3.12.6"
    required = (
        base / "DcmGetPdiServer.exe",
        base / "dcmget" / "pdi_server.py",
        base / "dcmget" / "architecture.py",
        ohif / "index.html",
        ohif / "app-config.js",
        ohif / "init-service-worker.js",
        ohif / "LICENSE-OHIF.txt",
        ohif / "THIRD_PARTY-OHIF.md",
        ohif / "DCMGET_OHIF_PAYLOAD.json",
        ohif / "DCMGET_PAYLOAD.SHA256",
    )
    missing = [str(path.relative_to(base)) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"PDI 离线资源缺失：{'、'.join(missing)}")
    prohibited = [
        name
        for name in ("sw.js", "google.js", "oidc-client.min.js", "silent-refresh.html")
        if (ohif / name).exists()
    ]
    if prohibited:
        raise RuntimeError(f"PDI 离线资源包含在线入口：{'、'.join(prohibited)}")
    for name in ("app-config.js", "init-service-worker.js"):
        content = (ohif / name).read_text(encoding="utf-8")
        if "http://" in content or "https://" in content:
            raise RuntimeError(f"PDI 离线资源包含外部地址：{name}")


def validate_web_resources(root: str | Path = PROJECT_ROOT) -> Path:
    static_root = Path(root) / "dcmget" / "webui"
    required = tuple(
        static_root / name
        for name in ("index.html", "app.css", "app.js", "theme.js")
    )
    missing = [path.name for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"Web 离线资源缺失：{'、'.join(missing)}")
    for path in required:
        content = path.read_text(encoding="utf-8")
        if "http://" in content or "https://" in content:
            raise RuntimeError(f"Web 离线资源包含外部地址：{path.name}")
    return static_root


def _available_port(host: str, preferred: int, *, excluded: set[int] | None = None) -> int:
    excluded = set(excluded or ())
    candidates = (
        *range(preferred, min(65536, preferred + 1000)),
        *range(1024, min(preferred, 2024)),
    )
    for port in candidates:
        if port in excluded:
            continue
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind((host, port))
            except OSError as exc:
                address_in_use = exc.errno in {
                    errno.EADDRINUSE,
                    getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE),
                    10048,
                } or getattr(exc, "winerror", None) == 10048
                if not address_in_use and host not in {"0.0.0.0", "::"}:
                    raise RuntimeError(f"无法绑定 Web 监听地址 {host}：{exc}") from exc
                continue
        return port
    raise RuntimeError(f"Web 端口 {preferred} 附近没有可用端口")


def _profile_web_config(profile: InstanceProfile) -> AppConfig:
    manager = ProfileManager(
        config_root=profile.config_path.parents[2],
        state_root=profile.state_directory.parents[1],
    )
    manager.validate_profile_ports(profile.number, check_system_ports=True)
    config = load_config(profile.config_path)
    web_errors = {
        field: message
        for field, message in config.validate().items()
        if field in {"web_bind_address", "web_port", "web_session_timeout_minutes"}
    }
    if web_errors:
        raise RuntimeError("；".join(web_errors.values()))
    save_config(profile.config_path, config)
    return config


def _open_ui_when_ready(
    url: str,
    *,
    timeout: float = 15.0,
    poll_interval: float = 0.1,
    opener=None,
    urlopen=None,
) -> bool:
    """Open the local UI only after the profile Web service answers HTTP."""

    open_url = opener or (
        spawn_webview_shell
        if sys.platform == "win32"
        else lambda target: webbrowser.open(target, new=1)
    )
    probe = urlopen or urllib.request.urlopen
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() <= deadline:
        try:
            response = probe(url, timeout=min(0.5, max(0.05, timeout)))
            close = getattr(response, "close", None)
            if callable(close):
                close()
        except urllib.error.HTTPError:
            # An HTTP response proves that the intended service is listening.
            pass
        except (OSError, urllib.error.URLError, TimeoutError):
            if time.monotonic() >= deadline:
                break
            time.sleep(max(0.0, poll_interval))
            continue
        try:
            result = open_url(url)
            return bool(result is None or result)
        except Exception as exc:
            record_exception("无法打开 DcmGet 工作台", exc)
            return False
    LOGGER.error("Web service did not become ready before UI open: %s", url)
    return False


def _schedule_ui_open(url: str) -> None:
    opener = threading.Thread(
        target=_open_ui_when_ready,
        args=(url,),
        name="dcmget-ui-opener",
        daemon=True,
    )
    opener.start()


def _activation_requests_ui(payload: dict[str, object]) -> bool:
    return str(payload.get("action", "activate")) != "ensure-running"


def _lan_hosts() -> tuple[str, ...]:
    hosts = set(discover_local_hosts())
    try:
        import psutil

        for addresses in psutil.net_if_addrs().values():
            for address in addresses:
                if address.family in {socket.AF_INET, socket.AF_INET6}:
                    hosts.add(str(address.address).split("%", 1)[0])
    except Exception:
        pass
    return tuple(sorted(host for host in hosts if host))


def _lan_url(config: AppConfig) -> str:
    if config.web_bind_address not in {"0.0.0.0", "::"}:
        host = config.web_bind_address
    else:
        candidates: list[str] = []
        for value in _lan_hosts():
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if address.version == 4 and not address.is_loopback and not address.is_link_local:
                candidates.append(value)
        host = candidates[0] if candidates else "127.0.0.1"
    return f"http://{host}:{config.web_port}/"


def _directory_roots(config: AppConfig) -> tuple[DirectoryRoot, ...]:
    candidates: list[tuple[str, str, Path]] = []
    home = Path.home()
    if home.is_dir():
        candidates.append(("home", "用户目录", home))
    if sys.platform == "win32":
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = Path(f"{letter}:\\")
            if drive.is_dir():
                candidates.append((f"drive-{letter.lower()}", f"磁盘 {letter}:", drive))
    else:
        for root_id, label, path in (
            ("volumes", "外接磁盘", Path("/Volumes")),
            ("mnt", "挂载目录", Path("/mnt")),
            ("media", "可移动介质", Path("/media")),
        ):
            if path.is_dir():
                candidates.append((root_id, label, path))
    for root_id, label, raw in (
        ("destination", "当前 DICOM 目录", config.dicom_destination_folder),
        ("pdi", "当前 PDI 目录", config.pdi_output_folder),
    ):
        if not str(raw).strip():
            continue
        path = Path(raw).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
            candidates.append((root_id, label, path))
        except OSError:
            continue
    roots: list[DirectoryRoot] = []
    seen: set[Path] = set()
    for root_id, label, path in candidates:
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            continue
        if resolved in seen or resolved.is_symlink():
            continue
        seen.add(resolved)
        roots.append(DirectoryRoot(root_id, label, resolved))
    return tuple(roots)


def _open_host_path(path: str | Path) -> dict[str, object]:
    selected = Path(path).expanduser().resolve(strict=True)
    if sys.platform == "win32":
        os.startfile(str(selected))  # type: ignore[attr-defined]
    else:
        import subprocess

        command = ["open", str(selected)] if sys.platform == "darwin" else ["xdg-open", str(selected)]
        subprocess.Popen(
            command,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    return {"ok": True, "message": f"已在主机打开：{selected}", "path": str(selected)}


def _operation_handlers(
    profile: InstanceProfile,
    service: DcmGetAppService,
) -> dict[str, object]:
    def current_config(_payload: object = None) -> AppConfig:
        return load_config(profile.config_path)

    def open_destination(_payload: object = None) -> dict[str, object]:
        return _open_host_path(current_config().dicom_destination_folder)

    def open_logs(_payload: object = None) -> dict[str, object]:
        profile.log_directory.mkdir(parents=True, exist_ok=True)
        return _open_host_path(profile.log_directory)

    def open_data(_payload: object = None) -> dict[str, object]:
        return _open_host_path(profile.state_directory)

    def open_pdi(_payload: object = None) -> dict[str, object]:
        snapshot = service.snapshot()
        pdi = snapshot.get("pdi")
        output = pdi.get("output_directory", "") if isinstance(pdi, dict) else ""
        if not output:
            raise RuntimeError("当前任务没有可打开的 PDI 目录")
        return _open_host_path(str(output))

    def acceptance_report(_payload: object = None) -> dict[str, object]:
        snapshot = service.snapshot()
        task = snapshot.get("task")
        task_id = str(task.get("id", "")) if isinstance(task, dict) else ""
        if not task_id:
            raise RuntimeError("当前没有可打开的验收报告")
        report = (
            Path(current_config().dicom_destination_folder)
            / "_DcmGetReports"
            / f"task-{task_id[:8]}"
            / f"dcmget-acceptance-{task_id}.html"
        )
        return _open_host_path(report)

    def profile_backup(_payload: object = None) -> dict[str, object]:
        from datetime import datetime
        from dcmget.profile_backup import create_profile_backup

        output = profile.state_directory / "backups" / (
            f"dcmget-profiles-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        )
        result = create_profile_backup(output, config_root=profile.config_path.parents[2])
        return {"ok": True, "message": "Profile 备份已生成", "path": str(result.path)}

    def support_bundle(_payload: object = None) -> dict[str, object]:
        from datetime import datetime
        from dcmget.support_bundle import create_support_bundle

        output = profile.state_directory / "support" / (
            f"dcmget-support-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        )
        result = create_support_bundle(
            output,
            current_config(),
            project_root=PROJECT_ROOT,
            diagnostic_directory=profile.log_directory,
        )
        return {"ok": True, "message": "脱敏支持包已生成", "path": str(result.path)}

    profile_operations = ProfileWebOperations(
        manager=ProfileManager(
            config_root=profile.config_path.parents[2],
            state_root=profile.state_directory.parents[1],
        ),
        project_root=PROJECT_ROOT,
    )
    return {
        "open-destination": open_destination,
        "open-log-directory": open_logs,
        "open-data-directory": open_data,
        "open-pdi": open_pdi,
        "acceptance-report": acceptance_report,
        "profile-backup": profile_backup,
        "support-bundle": support_bundle,
        **windows_service_operation_handlers(),
        **profile_operations.handlers(),
    }


def run_web_self_test(config_path: str) -> int:
    from tempfile import TemporaryDirectory

    validate_web_resources(PROJECT_ROOT)
    with TemporaryDirectory(prefix="dcmget-web-self-test-") as temporary:
        root = Path(temporary)
        config = load_config(config_path)
        config.web_bind_address = "127.0.0.1"
        config.web_port = _available_port("127.0.0.1", 18787)
        config.dicom_destination_folder = str(root / "dicom")
        saved_config = root / "config.json"
        save_config(saved_config, config)
        service = DcmGetAppService(
            task_store=TaskCheckpointStore(root / "active-task.sqlite3"),
            project_root=PROJECT_ROOT,
            profile_name="自检",
            fallback_log_directory=root / "logs",
        )
        server = DcmGetWebServer(
            service,
            state_directory=root / "web-state",
            host=config.web_bind_address,
            port=config.web_port,
            trusted_hosts=("127.0.0.1", "localhost"),
            static_root=PROJECT_ROOT / "dcmget" / "webui",
            directory_roots={"self-test": root},
            config_path=saved_config,
            project_root=PROJECT_ROOT,
            profile_metadata={"name": "自检", "number": 1},
            session_ttl_seconds=300,
            nicegui_enabled=True,
        )
        try:
            url = server.start_background(timeout=10)
            with urllib.request.urlopen(url, timeout=5) as response:
                html = response.read().decode("utf-8")
            if response.status != 200 or "DcmGet" not in html:
                raise RuntimeError("Web 首页自检失败")
            with urllib.request.urlopen(url + "api/bootstrap", timeout=5) as response:
                bootstrap = json.loads(response.read().decode("utf-8"))
            if not bootstrap.get("insecure_http", bootstrap.get("web", {}).get("insecure_http")):
                raise RuntimeError("Web HTTP 安全提示自检失败")
        finally:
            server.stop(timeout=10)
    print(f"DcmGet {__version__} Web self-test OK")
    return 0


def run_ui_self_test(config_path: str) -> int:
    """Compatibility alias retained for existing deployment automation."""

    return run_web_self_test(config_path)


def migrate_legacy_task_state(config_template_path: str | Path) -> None:
    """Copy unfinished pre-2.9 tasks into persistent instance slots once."""

    state_root = application_state_dir()
    migration_options = {
        "state_root": state_root,
        "template_config_path": config_template_path,
    }
    migrate_task_catalog_to_profiles(
        state_root / "tasks.sqlite3",
        **migration_options,
    )
    migrate_legacy_checkpoint_to_profile(
        state_root / "active-task.sqlite3",
        **migration_options,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    profile: InstanceProfile | None = None
    activation: SingleInstance | None = None
    server: DcmGetWebServer | None = None
    service: DcmGetAppService | None = None
    self_test_requested = any(
        value in {"--self-test", "--web-self-test", "--ui-self-test"}
        for value in arguments
    )
    try:
        ensure_supported_runtime()
        args = build_parser().parse_args(arguments)
        if args.native_shell_url:
            return run_webview_shell(args.native_shell_url)
        if args.windows_management:
            return run_windows_management_server(
                project_root=PROJECT_ROOT,
                static_root=validate_web_resources(PROJECT_ROOT),
                trusted_hosts=_lan_hosts(),
            )
        prepare_windows_portable_dcmtk(PROJECT_ROOT)
        if args.self_test:
            if args.self_test_report:
                return run_self_test(args.config, args.self_test_report)
            return run_self_test(args.config)
        if args.web_self_test:
            return run_web_self_test(args.config)

        migrate_legacy_task_state(args.config)
        updates = _profile_updates(args)
        if (updates or args.open_profile_web) and args.profile is None:
            raise RuntimeError(
                "启动前修改配置或打开 Profile Web 页面时，必须指定 --profile N"
            )
        if updates:
            ProfileManager().update_profile(args.profile, **updates)
        if args.no_open_browser:
            activation_action = "ensure-running"
        elif args.open_profile_web:
            activation_action = "open-profile-web"
        else:
            activation_action = "activate"
        try:
            profile = acquire_instance_profile(
                args.profile,
                template_config_path=args.config,
            )
        except ProfileInUseError as busy_error:
            if args.profile is None:
                raise
            notifier = SingleInstance(instance_activation_path(args.profile))
            try:
                if notifier.notify_existing(
                    {"action": activation_action, "profile": args.profile}
                ):
                    return 0
            finally:
                notifier.close()
            try:
                profile = acquire_instance_profile(
                    args.profile,
                    template_config_path=args.config,
                )
            except ProfileInUseError:
                raise busy_error
        activation = SingleInstance(profile.activation_path)
        if not activation.start(
            {"action": activation_action, "profile": profile.number}
        ):
            profile.close()
            profile = None
            activation.close()
            activation = None
            return 0
        config = _profile_web_config(profile)
        task_store = TaskCheckpointStore(profile.task_state_path)
        service = DcmGetAppService(
            task_store=task_store,
            project_root=PROJECT_ROOT,
            profile_name=profile.label,
            fallback_log_directory=profile.log_directory,
        )
        resolver = DcmtkResolver(PROJECT_ROOT)

        def tools_provider(selected: AppConfig):
            return resolver.resolve(selected.dcmtk_bin_dir)

        lan_url = _lan_url(config)
        server = DcmGetWebServer(
            service,
            state_directory=profile.state_directory,
            host=config.web_bind_address,
            port=config.web_port,
            trusted_hosts=_lan_hosts(),
            static_root=validate_web_resources(PROJECT_ROOT),
            directory_roots=_directory_roots(config),
            config_path=profile.config_path,
            project_root=PROJECT_ROOT,
            profile_metadata={
                "id": profile.number,
                "number": profile.number,
                "name": profile.label,
                "data_dir": str(profile.state_directory),
                "lan_url": lan_url,
            },
            session_ttl_seconds=config.web_session_timeout_minutes * 60,
            tools_provider=tools_provider,
            operation_handlers=_operation_handlers(profile, service),
            nicegui_enabled=True,
        )
        activation.set_activation_handler(
            lambda payload: (
                _schedule_ui_open(server.url)
                if _activation_requests_ui(payload)
                else None
            )
        )
        checkpoint = task_store.load(include_archived_files=False)
        if checkpoint is not None:
            try:
                service.resume_task(tools_provider(checkpoint.config))
            except Exception as exc:
                record_exception("未完成任务自动恢复失败", exc)

        if not args.no_open_browser and (
            args.open_profile_web or config.web_open_browser
        ):
            _schedule_ui_open(server.url)
    except Exception as exc:
        if server is not None:
            try:
                server.stop(timeout=10)
            except Exception as stop_error:
                record_exception("Web 服务启动清理失败", stop_error)
        if activation is not None:
            activation.close()
        if profile is not None:
            profile.close()
        _report_startup_failure("DcmGet 启动失败", exc, self_test_requested)
        return 1

    assert profile is not None
    assert activation is not None
    assert server is not None
    try:
        LOGGER.info(
            "DcmGet Web ready local=%s lan=%s profile=%s",
            server.url,
            _lan_url(load_config(profile.config_path)),
            profile.number,
        )
        server.run()
        return 0
    except Exception as exc:
        _report_startup_failure("DcmGet Web 服务启动失败", exc, False)
        return 1
    finally:
        try:
            server.stop(timeout=15)
        except Exception as exc:
            record_exception("DcmGet Web 服务停止失败", exc)
        activation.close()
        profile.close()


def _report_startup_failure(
    context: str,
    exc: BaseException,
    self_test: bool,
) -> None:
    record_exception(context, exc)
    message = f"{context}：{exc}\n\n诊断日志：{diagnostic_log_path()}"
    if self_test:
        print(message, file=sys.stderr)
        return
    if sys.platform == "win32" and not self_test:
        try:
            ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
                None,
                message,
                "DcmGet 启动失败",
                0x10,
            )
            return
        except Exception as native_error:
            record_exception("无法显示 Windows 启动失败提示", native_error)
    print(message, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
