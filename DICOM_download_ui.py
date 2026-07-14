from __future__ import annotations

import argparse
from io import BytesIO
import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from dcmget import __version__
from dcmget.auth_ui import authorize_gui
from dcmget.config import load_config
from dcmget.core import DcmtkResolver
from dcmget.licensing import PUBLIC_KEY_PEM, trial_status
from dcmget.ui import APP_STYLESHEET, DcmGetWindow
from dcmget.runtime import ensure_default_config, resource_root


PROJECT_ROOT = resource_root()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DcmGet 2.1 图形界面")
    parser.add_argument(
        "--config",
        default=str(ensure_default_config()),
        help="配置文件路径",
    )
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    return parser


def run_self_test(config_path: str) -> int:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from pydicom import dcmread
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian

    load_config(config_path)
    trial_status()
    public_key = serialization.load_pem_public_key(PUBLIC_KEY_PEM)
    if not isinstance(public_key, Ed25519PublicKey):
        raise RuntimeError("授权公钥类型错误")

    file_meta = FileMetaDataset()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.SOPInstanceUID = "1.2.826.0.1.3680043.10.999.1"
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return run_self_test(args.config)
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("DcmGet")
    app.setApplicationVersion(__version__)
    app.setStyleSheet(APP_STYLESHEET)
    if not authorize_gui():
        return 1
    window = DcmGetWindow(args.config, PROJECT_ROOT)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
