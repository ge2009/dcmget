from __future__ import annotations

import argparse
from io import BytesIO
import sys

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
from dcmget.runtime import ensure_default_config, resource_root
from dcmget.task_state import TaskCheckpointStore, TaskStateError


PROJECT_ROOT = resource_root()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DcmGet 2.5 图形界面")
    parser.add_argument(
        "--config",
        default=str(ensure_default_config()),
        help="配置文件路径",
    )
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ui-self-test", action="store_true", help=argparse.SUPPRESS)
    return parser


def run_self_test(config_path: str) -> int:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from pydicom import dcmread
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian

    from dcmget.anonymization import DicomAnonymizer

    load_config(config_path)
    trial_status()
    if f"## {__version__}" not in load_release_notes(PROJECT_ROOT):
        raise RuntimeError("版本说明文件缺失或与程序版本不一致")
    public_key = serialization.load_pem_public_key(PUBLIC_KEY_PEM)
    if not isinstance(public_key, Ed25519PublicKey):
        raise RuntimeError("授权公钥类型错误")

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
    print(f"DcmGet {__version__} self-test OK; DCMTK {tools.version}; fork=yes")
    return 0


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
