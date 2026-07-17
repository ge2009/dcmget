from __future__ import annotations

import argparse
from io import BytesIO
import json
import os
import sys
import uuid
from pathlib import Path

from dcmget import __version__
from dcmget.diagnostics import (
    diagnostic_log_path,
    install_diagnostics,
    install_qt_message_handler,
    prepare_macos_qt_plugins,
    record_exception,
)


install_diagnostics(__version__)

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QMessageBox

install_qt_message_handler()

from dcmget.auth_ui import authorize_gui
from dcmget.architecture import ensure_supported_runtime
from dcmget.config import load_config
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
from dcmget.release_notes import load_release_notes
from dcmget.ui import APP_STYLESHEET, DcmGetWindow
from dcmget.runtime import (
    application_state_dir,
    ensure_default_config,
    is_frozen,
    portable_dcmtk_bin,
    resource_root,
)
from dcmget.single_instance import SingleInstance
from dcmget.task_state import TaskCheckpointStore, TaskStateError
from dcmget.windows_portable_runtime import prepare_windows_portable_dcmtk


PROJECT_ROOT = resource_root()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"DcmGet {__version__} 图形界面")
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
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--self-test-report", help=argparse.SUPPRESS)
    parser.add_argument("--ui-self-test", action="store_true", help=argparse.SUPPRESS)
    return parser


def _positive_profile_number(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("实例编号必须是正整数") from exc
    if number < 1 or number > 9999:
        raise argparse.ArgumentTypeError("实例编号必须在 1 到 9999 之间")
    return number


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


def create_application() -> QApplication:
    prepare_macos_qt_plugins()
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("DcmGet")
    app.setApplicationVersion(__version__)
    app.setStyleSheet(APP_STYLESHEET)
    return app


def run_ui_self_test(config_path: str) -> int:
    from tempfile import TemporaryDirectory

    app = create_application()
    with TemporaryDirectory(prefix="dcmget-ui-self-test-") as temporary:
        temporary_root = Path(temporary)
        profile = acquire_instance_profile(
            state_root=temporary_root / "state",
            config_root=temporary_root / "config",
            template_config_path=config_path,
        )
        try:
            app.aboutToQuit.connect(profile.close)
            window = DcmGetWindow(
                profile.config_path,
                PROJECT_ROOT,
                profile.task_state_path,
                offer_task_resume=False,
                enable_multi_task=False,
                profile_number=profile.number,
                instance_label=profile.label,
                settings_name=profile.settings_name,
                log_directory=profile.log_directory,
            )
            window.show()
            app.processEvents()
            if not window.isVisible() or window.centralWidget() is None:
                raise RuntimeError("主窗口未能显示")
            window.close()
            app.processEvents()
        finally:
            profile.close()
    print(f"DcmGet {__version__} UI self-test OK")
    return 0


def resume_authorization_task_id(task_state_path: str | Path) -> str | None:
    """Return only the unfinished task assigned to the selected instance."""

    try:
        checkpoint = TaskCheckpointStore(task_state_path).load()
    except TaskStateError:
        return None
    return checkpoint.task_id if checkpoint is not None else None


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
    self_test_requested = any(
        value in {"--self-test", "--ui-self-test"} for value in arguments
    )
    try:
        ensure_supported_runtime()
        args = build_parser().parse_args(arguments)
        prepare_windows_portable_dcmtk(PROJECT_ROOT)
        if args.self_test:
            if args.self_test_report:
                return run_self_test(args.config, args.self_test_report)
            return run_self_test(args.config)
        if args.ui_self_test:
            return run_ui_self_test(args.config)

        app = create_application()
        migrate_legacy_task_state(args.config)
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
                    {"action": "activate", "profile": args.profile}
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
            {"action": "activate", "profile": profile.number}
        ):
            profile.close()
            profile = None
            activation.close()
            activation = None
            return 0
    except Exception as exc:
        if activation is not None:
            activation.close()
        if profile is not None:
            profile.close()
        _report_startup_failure("DcmGet 启动失败", exc, self_test_requested)
        return 1

    assert profile is not None
    assert activation is not None
    try:
        resume_task_id = resume_authorization_task_id(profile.task_state_path)
        if not authorize_gui(resume_task_id):
            return 1
        window = DcmGetWindow(
            profile.config_path,
            PROJECT_ROOT,
            profile.task_state_path,
            offer_task_resume=True,
            enable_multi_task=False,
            profile_number=profile.number,
            instance_label=profile.label,
            settings_name=profile.settings_name,
            log_directory=profile.log_directory,
        )
        activation.set_activation_handler(
            window.external_activation_requested.emit
        )
        app.aboutToQuit.connect(activation.close)
        app.aboutToQuit.connect(profile.close)
        window.show()
        return app.exec_()
    except Exception as exc:
        _report_startup_failure("DcmGet 主窗口启动失败", exc, False)
        return 1
    finally:
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
    if QApplication.instance() is not None:
        try:
            QMessageBox.critical(None, "DcmGet 启动失败", message)
            return
        except Exception as dialog_error:
            record_exception("无法显示启动失败提示", dialog_error)
    print(message, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
