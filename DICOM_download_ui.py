from __future__ import annotations

import argparse
import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from dcmget.ui import DcmGetWindow
from dcmget.runtime import ensure_default_config, resource_root


PROJECT_ROOT = resource_root()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DcmGet 2.0 图形界面")
    parser.add_argument(
        "--config",
        default=str(ensure_default_config()),
        help="配置文件路径",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("DcmGet")
    app.setApplicationVersion("2.0.0")
    window = DcmGetWindow(args.config, PROJECT_ROOT)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
