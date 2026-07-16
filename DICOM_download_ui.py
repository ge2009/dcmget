from __future__ import annotations

import argparse
from io import BytesIO
import sys
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
from dcmget.config import load_config
from dcmget.core import DcmtkResolver
from dcmget.licensing import PUBLIC_KEY_PEM, trial_status
from dcmget.release_notes import load_release_notes
from dcmget.ui import APP_STYLESHEET, DcmGetWindow
from dcmget.runtime import ensure_default_config, is_frozen, resource_root
from dcmget.task_state import TaskCheckpointStore, TaskStateError


PROJECT_ROOT = resource_root()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DcmGet 2.6 图形界面")
    parser.add_argument(
        "--config",
        default=str(ensure_default_config()),
        help="配置文件路径",
    )
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ui-self-test", action="store_true", help=argparse.SUPPRESS)
    return parser


def run_self_test(config_path: str) -> int:
    from tempfile import TemporaryDirectory

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from pydicom import dcmread
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

    from dcmget.anonymization import DicomAnonymizer
    from dcmget.config import AppConfig
    from dcmget.pdi import PdiExporter

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
    DicomAnonymizer("research", secret=b"dcmget-self-test-key-material-32b").anonymize_dataset(
        dataset
    )
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
                pdi_output / "DCMGET_STUDIES.json",
                pdi_output / "VIEWER" / "OHIF" / "index.html",
                pdi_output / "VIEWER" / "pdi_server.py",
                pdi_output / "OPEN_VIEWER.exe",
                pdi_output / "OPEN_VIEWER.bat",
            )
            missing_pdi = [path.name for path in required_pdi if not path.is_file()]
            if missing_pdi:
                raise RuntimeError(
                    f"PDI 冻结资源自检缺少文件：{'、'.join(missing_pdi)}"
                )
            index_text = (pdi_output / "INDEX.HTM").read_text(encoding="utf-8")
            if "本次导出未能加入 OHIF" in index_text:
                raise RuntimeError("PDI 冻结资源自检未能加入 OHIF")
    print(f"DcmGet {__version__} self-test OK; DCMTK {tools.version}; fork=yes")
    return 0


def validate_frozen_pdi_resources(root: str | Path) -> None:
    if not is_frozen():
        return
    base = Path(root)
    ohif = base / ".runtime" / "ohif" / "ohif-3.12.6"
    required = (
        base / "DcmGetPdiServer.exe",
        base / "dcmget" / "pdi_server.py",
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
    app = create_application()
    window = DcmGetWindow(
        config_path,
        PROJECT_ROOT,
        offer_task_resume=False,
    )
    window.show()
    app.processEvents()
    if not window.isVisible() or window.centralWidget() is None:
        raise RuntimeError("主窗口未能显示")
    window.close()
    app.processEvents()
    print(f"DcmGet {__version__} UI self-test OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    self_test_requested = any(
        value in {"--self-test", "--ui-self-test"} for value in arguments
    )
    try:
        args = build_parser().parse_args(arguments)
        if args.self_test:
            return run_self_test(args.config)
        if args.ui_self_test:
            return run_ui_self_test(args.config)

        app = create_application()
    except Exception as exc:
        _report_startup_failure("DcmGet 启动失败", exc, self_test_requested)
        return 1

    resume_task_id = None
    try:
        checkpoint = TaskCheckpointStore().load()
        if checkpoint is not None:
            resume_task_id = checkpoint.task_id
    except TaskStateError:
        pass

    try:
        if not authorize_gui(resume_task_id):
            return 1
        window = DcmGetWindow(args.config, PROJECT_ROOT)
        window.show()
        return app.exec_()
    except Exception as exc:
        _report_startup_failure("DcmGet 主窗口启动失败", exc, False)
        return 1


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
