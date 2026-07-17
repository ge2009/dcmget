from __future__ import annotations

import html
import re
import socket
import sys
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from PyQt5.QtCore import (
    QObject,
    QProcess,
    QSettings,
    Qt,
    QThread,
    QTimer,
    QUrl,
    pyqtSignal,
    pyqtSlot,
)
from PyQt5.QtGui import (
    QColor,
    QCloseEvent,
    QDesktopServices,
    QIcon,
    QIntValidator,
    QKeySequence,
    QPixmap,
)
from PyQt5.QtNetwork import (
    QNetworkAccessManager,
    QNetworkProxy,
    QNetworkReply,
    QNetworkRequest,
)
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QShortcut,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QFileDialog,
    QHeaderView,
    QAbstractItemView,
)

from . import __version__
from .auth_ui import activate_gui, entitlement_text, prepare_download_entitlement
from .config import (
    ANONYMIZATION_PROFILE_OPTIONS,
    DEFAULT_ANONYMIZATION_PROFILE,
    AppConfig,
    DIRECTORY_TEMPLATES,
    load_config,
    parse_accessions,
    save_config,
)
from .core import (
    AccessionResult,
    AccessionStatus,
    BatchSummary,
    DcmtkResolver,
    DownloadRunner,
    PreflightResult,
    ToolPaths,
    log_directory,
    preflight,
)
from .diagnostics import diagnostic_log_directory, record_exception
from .licensing import consume_trial, trial_task_consumed
from .release_notes import load_release_notes
from .runtime import is_frozen, resource_root
from .task_state import (
    TaskCheckpoint,
    TaskCheckpointStore,
    TaskStateError,
    merge_checkpoint_summary,
)
from .task_manager import DELETABLE_TASK_PHASES, TaskSummary, shared_receiver_config
from .task_controller import TaskExecutionController
from .task_widgets import TaskWorkspace


COLORS = {
    "primary": "#0369A1",
    "primary_hover": "#075985",
    "focus_on_primary": "#FBBF24",
    "success": "#047857",
    "warning": "#B45309",
    "danger": "#B91C1C",
    "background": "#F8FAFC",
    "surface": "#FFFFFF",
    "text": "#0F172A",
    "muted": "#475569",
    "border": "#CBD5E1",
}

TASK_TABLE_DETAIL_LIMIT = 200
PDI_VIEWER_PROBE_INTERVAL_MS = 250
PDI_VIEWER_START_TIMEOUT_MS = 30_000
PDI_LAST_OPEN_DIRECTORY_KEY = "pdi/last_open_directory"
PDI_OHIF_VERSION = "3.12.6"
WINDOW_MINIMUM_WIDTH = 800
WINDOW_MINIMUM_HEIGHT = 520


def pdi_viewer_command(directory: str | Path) -> tuple[str, list[str]] | None:
    """Return the trusted local server command for one complete PDI root."""

    root_candidate = Path(directory).expanduser()
    if root_candidate.is_symlink():
        return None
    root = root_candidate.resolve()
    dicom_root = root / "DICOM"
    if (
        not root.is_dir()
        or not dicom_root.is_dir()
        or dicom_root.is_symlink()
    ):
        return None
    study_indexes = (
        root / "VIEWER" / ".dcmget" / "index",
        root / "DCMGET_STUDIES.json",
    )
    if not any(path.is_file() and not path.is_symlink() for path in study_indexes):
        return None

    viewer_candidate = (
        resource_root()
        / ".runtime"
        / "ohif"
        / f"ohif-{PDI_OHIF_VERSION}"
    )
    if viewer_candidate.is_symlink():
        return None
    viewer_root = viewer_candidate.resolve()
    required_viewer_files = (
        viewer_root / "index.html",
        viewer_root / "DCMGET_PAYLOAD.SHA256",
        viewer_root / "DCMGET_OHIF_PAYLOAD.json",
    )
    if not all(
        path.is_file() and not path.is_symlink() for path in required_viewer_files
    ):
        return None

    base_arguments = [
        "--root",
        str(root),
        "--viewer-root",
        str(viewer_root),
        "--quiet",
    ]
    if is_frozen():
        trusted_executable = resource_root() / "DcmGetPdiServer.exe"
        if trusted_executable.is_file() and not trusted_executable.is_symlink():
            return str(trusted_executable), base_arguments
    else:
        trusted_script = Path(__file__).with_name("pdi_server.py")
        if trusted_script.is_file() and not trusted_script.is_symlink():
            return str(Path(sys.executable).resolve()), [
                str(trusted_script),
                *base_arguments,
            ]
    return None


def format_transfer_rate(bytes_per_second: float) -> str:
    if bytes_per_second <= 0:
        return "—"
    value = float(bytes_per_second)
    units = ("B/s", "KB/s", "MB/s", "GB/s", "TB/s")
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    return f"{value:.0f} {unit}" if unit == "B/s" else f"{value:.1f} {unit}"


class ReleaseNotesDialog(QDialog):
    def __init__(self, notes: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"DcmGet {__version__} 版本说明")
        screen = parent.screen() if parent is not None else QApplication.primaryScreen()
        if screen is None:
            self.setMinimumSize(480, 360)
            self.resize(680, 520)
        else:
            available = screen.availableGeometry()
            usable_width = max(1, available.width() - 48)
            usable_height = max(1, available.height() - 48)
            self.setMinimumSize(
                min(480, usable_width),
                min(360, usable_height),
            )
            self.resize(
                min(680, usable_width),
                min(520, usable_height),
            )
        layout = QVBoxLayout(self)
        title = QLabel(f"DcmGet {__version__} 版本说明")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        browser = QTextBrowser()
        browser.setMarkdown(notes)
        browser.setAccessibleName("版本更新内容")
        layout.addWidget(browser, 1)
        buttons = QDialogButtonBox()
        close_button = buttons.addButton("关闭", QDialogButtonBox.RejectRole)
        close_button.clicked.connect(self.close)
        layout.addWidget(buttons)


class AccessionTextEdit(QPlainTextEdit):
    file_dropped = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setTabChangesFocus(True)
        self.setPlaceholderText("每行一个检查号；也可以拖入 TXT 文件")
        self.setMinimumHeight(92)
        self.setAccessibleName("检查号输入")

    def dragEnterEvent(self, event):  # type: ignore[override]
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile() and urls[0].toLocalFile().lower().endswith(".txt"):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):  # type: ignore[override]
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile():
            self.file_dropped.emit(urls[0].toLocalFile())
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class SettingsPage(QWidget):
    saved = pyqtSignal(object)
    back_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._config = AppConfig()
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 24)
        outer.setSpacing(16)

        title_row = QHBoxLayout()
        back = QToolButton()
        back.setIcon(self.style().standardIcon(QStyle.SP_ArrowBack))
        back.setToolTip("返回任务主页")
        back.clicked.connect(self.back_requested)
        title = QLabel("连接、接收、匿名与 PDI 设置")
        title.setObjectName("PageTitle")
        title_row.addWidget(back)
        title_row.addWidget(title)
        title_row.addStretch()
        outer.addLayout(title_row)

        self.error_label = QLabel()
        self.error_label.setObjectName("ErrorText")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        outer.addWidget(self.error_label)

        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setSpacing(16)

        dcmtk_card, dcmtk_form = self._card("DCMTK 工具")
        dcmtk_row = QWidget()
        dcmtk_row_layout = QHBoxLayout(dcmtk_row)
        dcmtk_row_layout.setContentsMargins(0, 0, 0, 0)
        self.dcmtk_edit = QLineEdit()
        self.dcmtk_edit.setPlaceholderText("留空时自动检测系统 PATH 或 .runtime")
        self.dcmtk_browse_button = QPushButton("选择目录")
        self.dcmtk_browse_button.clicked.connect(self._browse_dcmtk)
        dcmtk_row_layout.addWidget(self.dcmtk_edit, 1)
        dcmtk_row_layout.addWidget(self.dcmtk_browse_button)
        dcmtk_form.addRow("bin 目录", dcmtk_row)
        self.dcmtk_status_label = QLabel("工具状态")
        self.dcmtk_hint = QLabel()
        self.dcmtk_hint.setObjectName("FieldHint")
        dcmtk_form.addRow(self.dcmtk_status_label, self.dcmtk_hint)
        self.dcmtk_status_label.hide()
        self.dcmtk_hint.hide()

        pacs_card, pacs_form = self._card("PACS 连接")
        self.pacs_host_edit = QLineEdit()
        self.pacs_port_edit = self._port_edit()
        self.calling_ae_edit = QLineEdit()
        self.pacs_ae_edit = QLineEdit()
        pacs_form.addRow("PACS 地址", self.pacs_host_edit)
        pacs_form.addRow("PACS 端口", self.pacs_port_edit)
        pacs_form.addRow("本机调用 AE", self.calling_ae_edit)
        pacs_form.addRow("PACS AE", self.pacs_ae_edit)

        receiver_card, receiver_form = self._card("DICOM 接收器")
        self.storage_ae_edit = QLineEdit()
        self.storage_port_edit = self._port_edit()
        self.max_concurrent_moves_spin = QSpinBox()
        self.max_concurrent_moves_spin.setRange(1, 8)
        self.max_concurrent_moves_spin.setSuffix(" 个")
        self.max_concurrent_moves_spin.setAccessibleName("最大并发下载数")
        concurrency_tooltip = (
            "同时运行的 C-MOVE 数量；默认 2，过高可能增加 PACS 和网络压力"
        )
        self.max_concurrent_moves_spin.setToolTip(concurrency_tooltip)
        self.max_concurrent_moves_spin.setProperty(
            "unlockedToolTip", concurrency_tooltip
        )
        self.directory_template_combo = QComboBox()
        self.directory_template_combo.setEditable(True)
        self.directory_template_combo.addItems(DIRECTORY_TEMPLATES)
        self.directory_template_combo.setToolTip(
            "可组合 {PatientID}、{AccessionNumber}、{StudyInstanceUID}"
        )
        self.log_size_spin = QSpinBox()
        self.log_size_spin.setRange(1, 4096)
        self.log_size_spin.setSuffix(" MB")
        receiver_form.addRow("接收 AE", self.storage_ae_edit)
        receiver_form.addRow("监听端口", self.storage_port_edit)
        receiver_form.addRow("并发下载数", self.max_concurrent_moves_spin)
        concurrency_hint = QLabel(
            "相同接收 AE 与端口复用一个并发 SCP；改用不同端口会自动启动多个 SCP。"
            "默认同时下载 2 个检查号，其余任务显示为“等待并发槽”。"
        )
        concurrency_hint.setObjectName("FieldHint")
        concurrency_hint.setWordWrap(True)
        receiver_form.addRow("", concurrency_hint)
        receiver_form.addRow("目录模板", self.directory_template_combo)
        directory_hint = QLabel(
            "可编辑组合：{PatientID}、{AccessionNumber}、{StudyInstanceUID}"
        )
        directory_hint.setObjectName("FieldHint")
        directory_hint.setWordWrap(True)
        receiver_form.addRow("", directory_hint)
        receiver_form.addRow("单个日志上限", self.log_size_spin)

        anonymization_card, anonymization_form = self._card("下载后匿名处理")
        self.anonymization_enabled_checkbox = QCheckBox(
            "归档前自动处理 DICOM 元数据"
        )
        self.anonymization_profile_combo = QComboBox()
        self.anonymization_profile_combo.setAccessibleName("匿名方案")
        for profile_id, label, _description in ANONYMIZATION_PROFILE_OPTIONS:
            self.anonymization_profile_combo.addItem(label, profile_id)
        self.anonymization_profile_hint = QLabel()
        self.anonymization_profile_hint.setObjectName("FieldHint")
        self.anonymization_profile_hint.setWordWrap(True)
        self.anonymization_warning = QLabel(
            "归档成功后不保留原始副本。仅处理 DICOM 元数据，不会清除像素中烧录的文字、人脸特征，"
            "研究/严格方案会拒绝已标记的烧录或可识别视觉特征，以及 PDF、SR、图形标注、缩略图和叠加层。"
        )
        self.anonymization_warning.setObjectName("WarningText")
        self.anonymization_warning.setWordWrap(True)
        anonymization_form.addRow("启用匿名", self.anonymization_enabled_checkbox)
        anonymization_form.addRow("匿名方案", self.anonymization_profile_combo)
        anonymization_form.addRow("", self.anonymization_profile_hint)
        anonymization_form.addRow("", self.anonymization_warning)
        self.anonymization_enabled_checkbox.toggled.connect(
            self._update_anonymization_state
        )
        self.anonymization_profile_combo.currentIndexChanged.connect(
            self._update_anonymization_state
        )

        pdi_card = QFrame()
        pdi_card.setObjectName("Card")
        pdi_layout = QVBoxLayout(pdi_card)
        pdi_header = QHBoxLayout()
        pdi_heading = QLabel("PDI 便携阅片目录")
        pdi_heading.setObjectName("SectionTitle")
        pdi_header.addWidget(pdi_heading)
        pdi_header.addStretch()
        self.pdi_enabled_checkbox = QCheckBox("每批下载完成后自动生成")
        self.pdi_enabled_checkbox.setAccessibleName("自动生成 PDI 便携阅片目录")
        pdi_header.addWidget(self.pdi_enabled_checkbox)
        self.pdi_card_toggle = QToolButton()
        self.pdi_card_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.pdi_card_toggle.setAccessibleName("展开或收起 PDI 设置")
        self.pdi_card_toggle.clicked.connect(self._toggle_pdi_card)
        pdi_header.addWidget(self.pdi_card_toggle)
        pdi_layout.addLayout(pdi_header)

        self.pdi_card_body = QWidget()
        pdi_form = QFormLayout(self.pdi_card_body)
        pdi_form.setContentsMargins(0, 4, 0, 0)
        pdi_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        pdi_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        pdi_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        pdi_form.setHorizontalSpacing(20)
        pdi_form.setVerticalSpacing(12)
        self.pdi_institution_edit = QLineEdit()
        self.pdi_institution_edit.setPlaceholderText("显示在 PDI 首页和说明文件中")
        pdi_form.addRow("机构名称", self.pdi_institution_edit)

        pdi_output_row = QWidget()
        pdi_output_layout = QHBoxLayout(pdi_output_row)
        pdi_output_layout.setContentsMargins(0, 0, 0, 0)
        self.pdi_output_edit = QLineEdit()
        self.pdi_output_edit.setPlaceholderText("留空时使用 DICOM 保存目录/PDI")
        self.pdi_output_button = QPushButton("选择目录")
        self.pdi_output_button.clicked.connect(self._browse_pdi_output)
        pdi_output_layout.addWidget(self.pdi_output_edit, 1)
        pdi_output_layout.addWidget(self.pdi_output_button)
        pdi_form.addRow("保存位置", pdi_output_row)
        self.pdi_output_hint = QLabel(
            "每批会在此位置自动新建 DCMGET_PDI_… 子目录。"
        )
        self.pdi_output_hint.setObjectName("FieldHint")
        self.pdi_output_hint.setWordWrap(True)
        pdi_form.addRow("", self.pdi_output_hint)

        self.pdi_ohif_checkbox = QCheckBox("在目录内附带离线阅片器（推荐）")
        pdi_form.addRow("DICOM 查看器", self.pdi_ohif_checkbox)
        self.pdi_ohif_hint = QLabel(
            "直接读取目录中的原始 DICOM，不生成 JPG；"
            "无需选择 JSON、DICOMDIR 或逐个影像文件。"
        )
        self.pdi_ohif_hint.setObjectName("FieldHint")
        self.pdi_ohif_hint.setWordWrap(True)
        pdi_form.addRow("", self.pdi_ohif_hint)
        self.pdi_privacy_warning = QLabel(
            "PDI 会复制本批归档文件并可加入离线阅片器。未启用匿名时，"
            "导出目录可能包含患者隐私信息，请按医疗数据管理要求保管和传递。"
        )
        self.pdi_privacy_warning.setObjectName("WarningText")
        self.pdi_privacy_warning.setWordWrap(True)
        pdi_form.addRow("", self.pdi_privacy_warning)
        pdi_layout.addWidget(self.pdi_card_body)
        self._pdi_card_expanded = False
        self.pdi_enabled_checkbox.toggled.connect(self._update_pdi_state)

        content_layout.addWidget(dcmtk_card)
        content_layout.addWidget(pacs_card)
        content_layout.addWidget(receiver_card)
        content_layout.addWidget(anonymization_card)
        content_layout.addWidget(pdi_card)
        content_layout.addStretch()
        self.settings_scroll.setWidget(content)
        outer.addWidget(self.settings_scroll, 1)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.back_requested)
        save = QPushButton("保存设置")
        save.setObjectName("PrimaryButton")
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        outer.addLayout(buttons)

        self._field_widgets = {
            "dcmtk_bin_dir": self.dcmtk_edit,
            "pacs_server_ip": self.pacs_host_edit,
            "pacs_server_port": self.pacs_port_edit,
            "calling_ae_title": self.calling_ae_edit,
            "pacs_ae_title": self.pacs_ae_edit,
            "storage_ae_title": self.storage_ae_edit,
            "storage_port": self.storage_port_edit,
            "max_concurrent_moves": self.max_concurrent_moves_spin,
            "directory_template": self.directory_template_combo,
            "anonymization_profile": self.anonymization_profile_combo,
            "pdi_institution_name": self.pdi_institution_edit,
            "pdi_output_folder": self.pdi_output_edit,
            "max_log_file_size_bytes": self.log_size_spin,
        }

    def _card(self, title: str) -> tuple[QFrame, QFormLayout]:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        heading = QLabel(title)
        heading.setObjectName("SectionTitle")
        layout.addWidget(heading)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(12)
        layout.addLayout(form)
        return card, form

    @staticmethod
    def _port_edit() -> QLineEdit:
        edit = QLineEdit()
        edit.setValidator(QIntValidator(1, 65535, edit))
        edit.setMaxLength(5)
        edit.setInputMethodHints(Qt.ImhDigitsOnly)
        return edit

    @staticmethod
    def _port_value(edit: QLineEdit) -> int:
        try:
            return int(edit.text().strip())
        except ValueError:
            return 0

    def set_config(self, config: AppConfig) -> None:
        self._config = config
        self.dcmtk_edit.setText(config.dcmtk_bin_dir)
        self.pacs_host_edit.setText(config.pacs_server_ip)
        self.pacs_port_edit.setText(str(config.pacs_server_port))
        self.calling_ae_edit.setText(config.calling_ae_title)
        self.pacs_ae_edit.setText(config.pacs_ae_title)
        self.storage_ae_edit.setText(config.storage_ae_title)
        self.storage_port_edit.setText(str(config.storage_port))
        self.max_concurrent_moves_spin.setValue(config.max_concurrent_moves)
        self.directory_template_combo.setCurrentText(config.directory_template)
        self.anonymization_enabled_checkbox.setChecked(config.anonymization_enabled)
        profile_index = self.anonymization_profile_combo.findData(
            config.anonymization_profile
        )
        if profile_index < 0:
            profile_index = self.anonymization_profile_combo.findData(
                DEFAULT_ANONYMIZATION_PROFILE
            )
        self.anonymization_profile_combo.setCurrentIndex(profile_index)
        self._update_anonymization_state()
        self.pdi_enabled_checkbox.setChecked(config.pdi_export_enabled)
        self.pdi_institution_edit.setText(config.pdi_institution_name)
        self.pdi_output_edit.setText(config.pdi_output_folder)
        self.pdi_ohif_checkbox.setChecked(config.pdi_include_ohif_viewer)
        self._set_pdi_card_expanded(config.pdi_export_enabled)
        self._update_pdi_state()
        self.log_size_spin.setValue(max(1, config.max_log_file_size_bytes // (1024 * 1024)))
        self.apply_errors({})

    def config(self) -> AppConfig:
        values = self._config.to_dict()
        values.update(
            dcmtk_bin_dir=self.dcmtk_edit.text().strip(),
            pacs_server_ip=self.pacs_host_edit.text().strip(),
            pacs_server_port=self._port_value(self.pacs_port_edit),
            calling_ae_title=self.calling_ae_edit.text().strip(" "),
            pacs_ae_title=self.pacs_ae_edit.text().strip(" "),
            storage_ae_title=self.storage_ae_edit.text().strip(" "),
            storage_port=self._port_value(self.storage_port_edit),
            max_concurrent_moves=self.max_concurrent_moves_spin.value(),
            directory_template=self.directory_template_combo.currentText().strip(),
            anonymization_enabled=self.anonymization_enabled_checkbox.isChecked(),
            anonymization_profile=str(
                self.anonymization_profile_combo.currentData()
                or DEFAULT_ANONYMIZATION_PROFILE
            ),
            pdi_export_enabled=self.pdi_enabled_checkbox.isChecked(),
            pdi_institution_name=self.pdi_institution_edit.text().strip(),
            pdi_output_folder=self.pdi_output_edit.text().strip(),
            pdi_include_ohif_viewer=self.pdi_ohif_checkbox.isChecked(),
            max_log_file_size_bytes=self.log_size_spin.value() * 1024 * 1024,
        )
        return AppConfig.from_dict(values)

    def _update_anonymization_state(self, _value=None) -> None:
        enabled = self.anonymization_enabled_checkbox.isChecked()
        self.anonymization_profile_combo.setEnabled(enabled)
        profile_id = str(
            self.anonymization_profile_combo.currentData()
            or DEFAULT_ANONYMIZATION_PROFILE
        )
        descriptions = {
            item_id: description
            for item_id, _label, description in ANONYMIZATION_PROFILE_OPTIONS
        }
        self.anonymization_profile_hint.setText(
            "元数据处理：" + descriptions.get(profile_id, "请选择匿名方案")
        )

    def _update_pdi_state(self, _value=None) -> None:
        enabled = self.pdi_enabled_checkbox.isChecked()
        if enabled and not self._pdi_card_expanded:
            self._set_pdi_card_expanded(True)
        for widget in (
            self.pdi_institution_edit,
            self.pdi_output_edit,
            self.pdi_output_button,
            self.pdi_ohif_checkbox,
        ):
            widget.setEnabled(enabled)
        self.pdi_ohif_hint.setEnabled(enabled)

    def _set_pdi_card_expanded(self, expanded: bool) -> None:
        self._pdi_card_expanded = expanded
        self.pdi_card_body.setVisible(expanded)
        self.pdi_card_toggle.setArrowType(
            Qt.DownArrow if expanded else Qt.RightArrow
        )
        self.pdi_card_toggle.setText("收起" if expanded else "展开")

    def _toggle_pdi_card(self) -> None:
        self._set_pdi_card_expanded(not self._pdi_card_expanded)

    def apply_errors(self, errors: dict[str, str]) -> None:
        for field, widget in self._field_widgets.items():
            message = errors.get(field, "")
            widget.setProperty("invalid", bool(message))
            unlocked_tooltip = str(widget.property("unlockedToolTip") or "")
            widget.setToolTip(message or unlocked_tooltip)
            widget.setAccessibleDescription(message)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        messages = list(dict.fromkeys(errors.values()))
        self.error_label.setText("\n".join(messages))
        self.error_label.setVisible(bool(messages))
        if errors:
            first_invalid = next(
                (widget for field, widget in self._field_widgets.items() if field in errors),
                None,
            )
            if first_invalid is not None:
                self.settings_scroll.ensureWidgetVisible(first_invalid, 0, 24)
                first_invalid.setFocus(Qt.OtherFocusReason)

    def set_dcmtk_status(self, text: str, ok: bool) -> None:
        self.dcmtk_hint.setText("" if ok else text)
        self.dcmtk_hint.setProperty("status", "ok" if ok else "error")
        self.dcmtk_hint.style().unpolish(self.dcmtk_hint)
        self.dcmtk_hint.style().polish(self.dcmtk_hint)
        self.dcmtk_status_label.setVisible(not ok)
        self.dcmtk_hint.setVisible(not ok)

    def set_shared_settings_locked(self, locked: bool) -> None:
        message = "有未结束任务时由应用全局锁定" if locked else ""
        for widget in (
            self.dcmtk_edit,
            self.dcmtk_browse_button,
            self.max_concurrent_moves_spin,
        ):
            widget.setEnabled(not locked)
            unlocked_tooltip = str(widget.property("unlockedToolTip") or "")
            widget.setToolTip(message if locked else unlocked_tooltip)

    def _browse_dcmtk(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择 DCMTK bin 目录", self.dcmtk_edit.text())
        if selected:
            self.dcmtk_edit.setText(selected)

    def _browse_pdi_output(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "选择 PDI 保存位置", self.pdi_output_edit.text()
        )
        if selected:
            self.pdi_output_edit.setText(selected)

    def _save(self) -> None:
        config = self.config()
        errors = config.validate()
        self.apply_errors(errors)
        if errors:
            return
        self.saved.emit(config)


class DownloadWorker(QObject):
    log = pyqtSignal(str, str, str)
    state = pyqtSignal(str)
    progress = pyqtSignal(int, int, object)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)
    trial_consumed = pyqtSignal(str)

    def __init__(
        self,
        config: AppConfig,
        tools: ToolPaths,
        accessions: list[str],
        consume_trial_on_ready: bool = False,
        task_store: TaskCheckpointStore | None = None,
        task_id: str = "",
    ):
        super().__init__()
        self.config = config
        self.tools = tools
        self.accessions = accessions
        self.consume_trial_on_ready = consume_trial_on_ready
        self.task_store = task_store
        self.task_id = task_id
        self.runner: DownloadRunner | None = None
        self.cancel_requested = False
        self.pause_requested = False
        self._control_lock = threading.Lock()

    @pyqtSlot()
    def run(self) -> None:
        try:
            runner = DownloadRunner(
                self.config,
                self.tools,
                log_callback=self.log.emit,
                state_callback=self.state.emit,
                progress_callback=self._report_progress,
                ready_callback=(
                    self._consume_trial if self.consume_trial_on_ready else None
                ),
                process_callback=self._record_process,
            )
            with self._control_lock:
                self.runner = runner
                if self.cancel_requested:
                    runner.request_cancel()
                elif self.pause_requested:
                    runner.request_pause()
            self.finished.emit(runner.run(self.accessions))
        except Exception as exc:  # keep worker failures visible in the UI
            record_exception("DownloadWorker.run", exc)
            self.failed.emit(str(exc))

    def request_cancel(self) -> None:
        with self._control_lock:
            self.cancel_requested = True
            if self.runner:
                self.runner.request_cancel()

    def request_pause(self) -> None:
        with self._control_lock:
            self.pause_requested = True
            if self.runner:
                self.runner.request_pause()

    def request_resume(self) -> None:
        with self._control_lock:
            self.pause_requested = False
            if self.runner:
                self.runner.request_resume()

    def _consume_trial(self) -> None:
        trial = consume_trial(task_id=self.task_id or None)
        self.trial_consumed.emit(f"本次使用免费试用，剩余 {trial.remaining} 次")

    def _report_progress(
        self, index: int, total: int, result: AccessionResult
    ) -> None:
        if self.task_store is not None and self.task_id:
            result = self.task_store.record_result(self.task_id, result)
        self.progress.emit(index, total, result)

    def _record_process(
        self,
        kind: str,
        pid: int,
        executable: str,
        active: bool,
    ) -> None:
        if self.task_store is not None and self.task_id:
            self.task_store.record_process(
                self.task_id,
                kind,
                pid,
                executable,
                active=active,
            )


class PdiWorker(QObject):
    """Run the optional PDI exporter without blocking the Qt event loop."""

    log = pyqtSignal(str, str, str)
    progress = pyqtSignal(object, int, int, str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        config: AppConfig,
        tools: ToolPaths,
        files: list[str],
        project_root: Path,
        task_store: TaskCheckpointStore | None = None,
        task_id: str = "",
        recovery_id: str = "",
        reuse_published: bool = False,
    ):
        super().__init__()
        self.config = config
        self.tools = tools
        self.files = files
        self.project_root = project_root
        self.task_store = task_store
        self.task_id = task_id
        self.recovery_id = recovery_id
        self.reuse_published = reuse_published
        self.exporter = None
        self.cancel_requested = False
        self._control_lock = threading.Lock()

    @pyqtSlot()
    def run(self) -> None:
        try:
            # Keep the optional export subsystem out of the normal UI import path.
            from .pdi import PdiExporter

            exporter = PdiExporter(
                self.config,
                self.tools,
                project_root=self.project_root,
                log_callback=self.log.emit,
                progress_callback=self.progress.emit,
                process_callback=self._record_process,
                recovery_id=self.recovery_id,
                reuse_published=self.reuse_published,
            )
            with self._control_lock:
                self.exporter = exporter
                if self.cancel_requested:
                    exporter.request_cancel()
            self.finished.emit(exporter.export(self.files))
        except Exception as exc:  # surface exporter failures in the main UI
            record_exception("PdiWorker.run", exc)
            self.failed.emit(str(exc))

    def request_cancel(self) -> None:
        with self._control_lock:
            if self.cancel_requested:
                return
            self.cancel_requested = True
            if self.exporter is not None:
                self.exporter.request_cancel()

    def _record_process(
        self,
        kind: str,
        pid: int,
        executable: str,
        active: bool,
    ) -> None:
        if self.task_store is not None and self.task_id:
            self.task_store.record_process(
                self.task_id,
                kind,
                pid,
                executable,
                active=active,
            )


class DcmGetWindow(QMainWindow):
    def __init__(
        self,
        config_path: str | Path,
        project_root: str | Path,
        task_state_path: str | Path | None = None,
        *,
        offer_task_resume: bool = True,
        enable_multi_task: bool | None = None,
    ):
        super().__init__()
        self.config_path = Path(config_path)
        self.project_root = Path(project_root)
        self.config = load_config(self.config_path)
        self.resolver = DcmtkResolver(self.project_root)
        self.task_store = TaskCheckpointStore(task_state_path)
        self.multi_task_enabled = (
            offer_task_resume if enable_multi_task is None else enable_multi_task
        )
        self.task_controller: TaskExecutionController | None = None
        self._task_catalog_path = (
            Path(task_state_path).expanduser().with_name("tasks.sqlite3")
            if task_state_path is not None
            else None
        )
        self._selected_task_id = ""
        self._loaded_multi_task_id = ""
        self._multi_task_editor_active = True
        self._log_events: list[tuple[str, str, str, str]] = []
        self._last_pdi_results: dict[str, object] = {}
        self.tools: ToolPaths | None = None
        self.worker: DownloadWorker | None = None
        self.worker_thread: QThread | None = None
        self.pdi_worker: PdiWorker | None = None
        self.pdi_thread: QThread | None = None
        self._pdi_viewer_process: QProcess | None = None
        self._pdi_viewer_root: Path | None = None
        self._pdi_viewer_url = ""
        self._pdi_viewer_probe_url = ""
        self._pdi_viewer_ready = False
        self._pdi_viewer_open_when_ready = False
        self._pdi_viewer_button_states = (False, False)
        self._pdi_viewer_network = QNetworkAccessManager(self)
        self._pdi_viewer_network.setProxy(QNetworkProxy(QNetworkProxy.NoProxy))
        self._pdi_viewer_probe_reply: QNetworkReply | None = None
        self._pdi_viewer_probe_timer = QTimer(self)
        self._pdi_viewer_probe_timer.setInterval(PDI_VIEWER_PROBE_INTERVAL_MS)
        self._pdi_viewer_probe_timer.timeout.connect(self._probe_pdi_viewer_ready)
        self._pdi_viewer_timeout_timer = QTimer(self)
        self._pdi_viewer_timeout_timer.setSingleShot(True)
        self._pdi_viewer_timeout_timer.timeout.connect(
            self._on_pdi_viewer_start_timeout
        )
        self._worker_failure_message: str | None = None
        self._pending_pdi_completion: tuple[str, bool, bool] | None = None
        self.last_summary: BatchSummary | None = None
        self.last_pdi_result = None
        self._pdi_source_files: list[str] = []
        self._pdi_task_id = ""
        self._pdi_reuse_published = False
        self._accepted_partial_results = False
        self.release_notes_dialog: ReleaseNotesDialog | None = None
        self._closing_after_cancel = False
        self._pause_requested = False
        self.current_accessions: list[str] = []
        self._hidden_accession_count = 0
        self.invalid_accessions: tuple[str, ...] = ()
        self._active_accessions: list[str] = []
        self._active_task_id = ""
        self._resume_checkpoint: TaskCheckpoint | None = None
        self._prior_results: list[AccessionResult] = []
        self._display_total = 0
        self._progress_offset = 0
        self.row_by_accession: dict[str, int] = {}
        self._task_table_summary_mode = False
        self._summary_results: dict[str, AccessionResult] = {}
        self._summary_processed = 0
        self._summary_files = 0
        self._summary_status_counts: dict[AccessionStatus, int] = {}
        self._workspace_task_summaries: dict[str, TaskSummary] = {}
        self._compact_action_layout: bool | None = None
        self.settings_store = QSettings("DcmGet", "DcmGet2")
        self._log_panel_expanded = self.settings_store.value(
            "window/log_expanded", False, type=bool
        )
        self._show_detailed_logs = self.settings_store.value(
            "window/log_detailed", False, type=bool
        )
        self._task_form_expanded = self.settings_store.value(
            "window/task_form_expanded", True, type=bool
        )

        self.setWindowTitle(f"DcmGet {__version__} - DICOM 下载工作台")
        self.setMinimumSize(WINDOW_MINIMUM_WIDTH, WINDOW_MINIMUM_HEIGHT)
        self.resize(1180, 820)
        logo = self.project_root / "logo.png"
        if logo.exists():
            self.setWindowIcon(QIcon(str(logo)))
        self._build_ui()
        self.task_workspace.set_concurrency_limit(self.config.max_concurrent_moves)
        self._restore_ui_state()
        self.settings_page.set_config(self.config)
        self._reset_pdi_status_card()
        last_destination = self.settings_store.value("task/destination", "", type=str)
        self.destination_edit.setText(last_destination or self.config.dicom_destination_folder)
        self._sync_quick_pdi_controls_from_config()
        self._load_configured_accessions()
        QTimer.singleShot(0, self._refresh_tool_status)
        if offer_task_resume and not self.multi_task_enabled:
            QTimer.singleShot(0, self._offer_task_resume)

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.header = self._build_header()
        root_layout.addWidget(self.header)

        self.pages = QStackedWidget()
        self.task_detail_page = self._build_task_page()
        self.task_workspace = TaskWorkspace(self.task_detail_page)
        self.task_workspace.new_task_requested.connect(
            self._show_new_task_editor
        )
        self.task_workspace.task_selected.connect(
            self._on_workspace_task_selected
        )
        self.task_workspace.delete_task_requested.connect(
            self._delete_multi_task
        )
        self.task_workspace.compact_mode_changed.connect(
            lambda _compact: self._update_responsive_layouts(force=True)
        )
        self.task_workspace.splitter.splitterMoved.connect(
            lambda _position, _index: self._update_responsive_layouts(force=True)
        )
        self.task_page = self.task_workspace
        self.pages.addWidget(self.task_page)
        self.settings_page = SettingsPage()
        self.settings_page.saved.connect(self._save_settings)
        self.settings_page.back_requested.connect(self._cancel_settings)
        self.pages.addWidget(self.settings_page)
        root_layout.addWidget(self.pages, 1)
        self.setCentralWidget(root)
        self.task_workspace.show_detail()
        self.setStyleSheet(APP_STYLESHEET)
        for widget in (
            self.app_title,
            self.app_subtitle,
            self.tool_status,
            self.entitlement_status,
            self.registration_button,
            self.release_notes_button,
            self.diagnostic_log_button,
            self.settings_button,
        ):
            widget.setMinimumWidth(widget.sizeHint().width())
        self._refresh_entitlement_status()

        self.open_accession_shortcut = QShortcut(QKeySequence("Ctrl+O"), self)
        self.open_accession_shortcut.activated.connect(
            self._choose_accession_file_from_shortcut
        )
        self.start_download_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        self.start_download_shortcut.activated.connect(
            self._start_download_from_shortcut
        )
        self.settings_shortcut = QShortcut(QKeySequence("Ctrl+,"), self)
        self.settings_shortcut.activated.connect(self._show_settings_from_shortcut)

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("Header")
        layout = QVBoxLayout(header)
        layout.setContentsMargins(24, 12, 24, 12)
        layout.setSpacing(8)

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self.app_logo = QLabel()
        self.app_logo.setAccessibleName("DcmGet 标志")
        self.app_logo.setFixedSize(32, 32)
        self.app_logo.setAlignment(Qt.AlignCenter)
        logo_path = self.project_root / "logo.png"
        logo_pixmap = QPixmap(str(logo_path)) if logo_path.is_file() else QPixmap()
        if logo_pixmap.isNull():
            self.app_logo.hide()
        else:
            self.app_logo.setPixmap(
                logo_pixmap.scaled(
                    32,
                    32,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            )
        status_row.addWidget(self.app_logo, 0, Qt.AlignVCenter)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self.app_title = QLabel(f"DcmGet {__version__}")
        self.app_title.setObjectName("AppTitle")
        self.app_subtitle = QLabel("DICOM 批量下载工作台")
        self.app_subtitle.setObjectName("HeaderSubtitle")
        title_box.addWidget(self.app_title)
        title_box.addWidget(self.app_subtitle)
        status_row.addLayout(title_box)
        status_row.addStretch()
        self.tool_status = QLabel("正在检测 DCMTK…")
        self.tool_status.setObjectName("StatusPill")
        self.tool_status.setProperty("status", "pending")
        self.tool_status.setAccessibleName("DCMTK 工具状态")
        self.tool_status.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        status_row.addWidget(self.tool_status)
        self.entitlement_status = QLabel()
        self.entitlement_status.setObjectName("StatusPill")
        self.entitlement_status.setAccessibleName("软件授权状态")
        self.entitlement_status.setSizePolicy(
            QSizePolicy.Minimum, QSizePolicy.Preferred
        )
        self._refresh_entitlement_status()
        status_row.addWidget(self.entitlement_status)
        layout.addLayout(status_row)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        action_row.addStretch()
        self.registration_button = QToolButton()
        self.registration_button.setText("软件注册")
        self.registration_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.registration_button.clicked.connect(self._show_activation)
        action_row.addWidget(self.registration_button)
        self.release_notes_button = QToolButton()
        self.release_notes_button.setText("版本说明")
        self.release_notes_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.release_notes_button.clicked.connect(self._show_release_notes)
        action_row.addWidget(self.release_notes_button)
        self.diagnostic_log_button = QToolButton()
        self.diagnostic_log_button.setText("诊断日志")
        self.diagnostic_log_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.diagnostic_log_button.setToolTip("打开启动、异常和崩溃诊断日志目录")
        self.diagnostic_log_button.clicked.connect(
            self._open_diagnostic_log_directory
        )
        action_row.addWidget(self.diagnostic_log_button)
        self.settings_button = QToolButton()
        self.settings_button.setText("设置")
        self.settings_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.settings_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogDetailedView))
        self.settings_button.setToolTip("连接、接收、匿名与 PDI 设置（Ctrl+,）")
        self.settings_button.clicked.connect(self._show_settings)
        action_row.addWidget(self.settings_button)
        layout.addLayout(action_row)
        return header

    def _build_task_page(self) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        self.task_scroll = QScrollArea()
        self.task_scroll.setWidgetResizable(True)
        self.task_scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        content.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(14)

        input_card = QFrame()
        input_card.setObjectName("Card")
        input_layout = QVBoxLayout(input_card)
        input_header = QHBoxLayout()
        section = QLabel("新建下载任务")
        section.setObjectName("SectionTitle")
        input_header.addWidget(section)
        self.task_form_summary = QLabel()
        self.task_form_summary.setObjectName("FieldHint")
        self.task_form_summary.setSizePolicy(
            QSizePolicy.Ignored, QSizePolicy.Preferred
        )
        input_header.addWidget(self.task_form_summary, 1)
        input_header.addStretch()
        self.task_form_toggle_button = QToolButton()
        self.task_form_toggle_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.task_form_toggle_button.setAccessibleName("展开或收起新建任务")
        self.task_form_toggle_button.clicked.connect(self._toggle_task_form)
        input_header.addWidget(self.task_form_toggle_button)
        input_layout.addLayout(input_header)

        self.task_form_body = QWidget()
        grid = QGridLayout(self.task_form_body)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(10)
        grid.addWidget(QLabel("检查号"), 0, 0, Qt.AlignTop)
        self.accession_edit = AccessionTextEdit()
        self.accession_edit.textChanged.connect(self._update_accession_preview)
        self.accession_edit.file_dropped.connect(self._load_accession_file)
        grid.addWidget(self.accession_edit, 0, 1, 1, 2)
        self.accession_button = QPushButton("选择 TXT")
        self.accession_button.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))
        self.accession_button.clicked.connect(self._choose_accession_file)
        self.accession_summary = QLabel("有效 0 · 空行 0 · 重复 0")
        self.accession_summary.setObjectName("FieldHint")
        helper_row = QHBoxLayout()
        helper_row.addWidget(self.accession_button)
        helper_row.addWidget(self.accession_summary)
        helper_row.addStretch()
        grid.addLayout(helper_row, 1, 1, 1, 2)

        grid.addWidget(QLabel("保存目录"), 2, 0)
        self.destination_edit = QLineEdit()
        self.destination_edit.setAccessibleName("DICOM 保存目录")
        self.destination_edit.textChanged.connect(self._update_task_form_summary)
        self.destination_edit.textChanged.connect(
            lambda _text: self._set_destination_error("")
        )
        grid.addWidget(self.destination_edit, 2, 1)
        self.destination_button = QPushButton("选择目录")
        self.destination_button.clicked.connect(self._choose_destination)
        grid.addWidget(self.destination_button, 2, 2)
        self.open_destination_button = QPushButton("打开目标目录")
        self.open_destination_button.clicked.connect(self._open_destination_directory)
        grid.addWidget(self.open_destination_button, 2, 3)
        self.destination_error_label = QLabel()
        self.destination_error_label.setObjectName("InlineErrorText")
        self.destination_error_label.setWordWrap(True)
        self.destination_error_label.hide()
        grid.addWidget(self.destination_error_label, 3, 1, 1, 3)

        self.quick_pdi_checkbox = QCheckBox("下载完成后生成 PDI 便携目录")
        self.quick_pdi_checkbox.setAccessibleName("下载完成后生成 PDI 便携目录")
        self.quick_pdi_checkbox.toggled.connect(self._on_quick_pdi_toggled)
        grid.addWidget(self.quick_pdi_checkbox, 4, 1, 1, 3)
        self.quick_pdi_output_label = QLabel()
        self.quick_pdi_output_label.setObjectName("FieldHint")
        self.quick_pdi_output_label.setWordWrap(True)
        self.quick_pdi_output_label.setSizePolicy(
            QSizePolicy.Ignored, QSizePolicy.Preferred
        )
        self.quick_pdi_output_label.setAccessibleName("PDI 保存目录")
        grid.addWidget(self.quick_pdi_output_label, 5, 1, 1, 2)
        self.quick_pdi_output_button = QPushButton("选择 PDI 保存目录")
        self.quick_pdi_output_button.clicked.connect(
            self._choose_quick_pdi_output
        )
        grid.addWidget(self.quick_pdi_output_button, 5, 3)
        self.destination_edit.textChanged.connect(self._update_quick_pdi_summary)
        grid.setColumnStretch(1, 1)
        input_layout.addWidget(self.task_form_body)
        self._set_task_form_expanded(self._task_form_expanded)
        layout.addWidget(input_card)

        self.preflight_card = QFrame()
        self.preflight_card.setObjectName("Card")
        preflight_layout = QVBoxLayout(self.preflight_card)
        preflight_layout.setSpacing(8)
        preflight_title = QLabel("启动预检")
        preflight_title.setObjectName("SectionTitle")
        preflight_layout.addWidget(preflight_title)
        self.preflight_grid = QGridLayout()
        self.preflight_grid.setHorizontalSpacing(8)
        self.preflight_grid.setVerticalSpacing(8)
        self.preflight_labels: list[QLabel] = []
        for index, text in enumerate(
            ("DCMTK 待检测", "保存目录待检测", "端口待检测", "PACS 待检测")
        ):
            label = QLabel(text)
            label.setObjectName("CheckPill")
            label.setProperty("status", "pending")
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            self.preflight_labels.append(label)
            self.preflight_grid.addWidget(label, index // 3, index % 3)
        preflight_layout.addLayout(self.preflight_grid)
        layout.addWidget(self.preflight_card)

        self.task_action_layout = QGridLayout()
        self.task_action_layout.setHorizontalSpacing(8)
        self.task_action_layout.setVerticalSpacing(8)
        self.progress_label = QLabel("尚未开始")
        self.progress_label.setObjectName("ProgressText")
        self.progress_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.discard_resume_button = QPushButton("放弃续传并新建")
        self.discard_resume_button.setObjectName("DangerButton")
        self.discard_resume_button.setToolTip("保留已下载文件，仅删除未完成任务的恢复记录")
        self.discard_resume_button.clicked.connect(self._discard_resume_task)
        self.discard_resume_button.hide()
        self.retry_button = QPushButton("重试失败项")
        self.retry_button.setEnabled(False)
        self.retry_button.hide()
        self.retry_button.clicked.connect(self._retry_failed)
        self.accept_partial_button = QPushButton("接受当前结果")
        self.accept_partial_button.setToolTip(
            "不再重试失败项，保留已下载文件并继续生成 PDI（如已启用）"
        )
        self.accept_partial_button.clicked.connect(self._accept_partial_results)
        self.accept_partial_button.hide()
        self.log_toggle_button = QPushButton(
            "收起日志" if self._log_panel_expanded else "展开日志"
        )
        self.log_toggle_button.clicked.connect(self._toggle_log_panel)
        self.open_existing_pdi_button = QPushButton("打开已有 PDI 目录")
        self.open_existing_pdi_button.setToolTip(
            "选择一个完整的 PDI 根目录并使用内置离线阅片服务打开"
        )
        self.open_existing_pdi_button.clicked.connect(
            self._choose_existing_pdi_directory
        )
        self.pause_button = QPushButton("暂停")
        self.pause_button.setEnabled(False)
        self.pause_button.hide()
        self.pause_button.setToolTip("当前检查号完成后暂停；继续时从下一项接着下载")
        self.pause_button.clicked.connect(self._toggle_pause)
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("DangerButton")
        self.stop_button.setEnabled(False)
        self.stop_button.hide()
        self.stop_button.clicked.connect(self._stop_download)
        self.delete_task_button = QPushButton("删除任务")
        self.delete_task_button.setObjectName("DangerButton")
        self.delete_task_button.setToolTip(
            "仅删除任务记录，不删除 DICOM、PDI、日志或隔离文件"
        )
        self.delete_task_button.hide()
        self.delete_task_button.clicked.connect(
            lambda _checked=False: self._delete_multi_task(self._selected_task_id)
        )
        self.start_button = QPushButton("开始下载")
        self.start_button.setObjectName("PrimaryButton")
        self.start_button.setToolTip("开始下载（Ctrl+Enter）")
        self.start_button.clicked.connect(
            lambda _checked=False: self._start_download()
        )
        self._task_action_buttons = (
            self.discard_resume_button,
            self.retry_button,
            self.accept_partial_button,
            self.open_existing_pdi_button,
            self.log_toggle_button,
            self.pause_button,
            self.stop_button,
            self.delete_task_button,
            self.start_button,
        )
        layout.addLayout(self.task_action_layout)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.progress_bar)
        self.task_config_summary_label = QLabel()
        self.task_config_summary_label.setObjectName("FieldHint")
        self.task_config_summary_label.setWordWrap(True)
        self.task_config_summary_label.setAccessibleName("任务连接配置快照")
        self.task_config_summary_label.hide()
        layout.addWidget(self.task_config_summary_label)

        self.large_batch_summary_card = QFrame()
        self.large_batch_summary_card.setObjectName("LargeBatchSummaryCard")
        large_batch_layout = QVBoxLayout(self.large_batch_summary_card)
        large_batch_layout.setContentsMargins(14, 12, 14, 12)
        large_batch_layout.setSpacing(6)
        large_batch_header = QHBoxLayout()
        large_batch_title = QLabel("大批量任务摘要")
        large_batch_title.setObjectName("SectionTitle")
        large_batch_header.addWidget(large_batch_title)
        large_batch_header.addStretch()
        self.copy_failed_button = QPushButton("复制失败检查号")
        self.copy_failed_button.setAccessibleName("复制失败检查号")
        self.copy_failed_button.clicked.connect(self._copy_failed_accessions)
        self.copy_failed_button.hide()
        large_batch_header.addWidget(self.copy_failed_button)
        large_batch_layout.addLayout(large_batch_header)
        self.large_batch_summary_label = QLabel()
        self.large_batch_summary_label.setObjectName("LargeBatchSummaryText")
        self.large_batch_summary_label.setWordWrap(True)
        self.large_batch_summary_label.setSizePolicy(
            QSizePolicy.Ignored, QSizePolicy.Preferred
        )
        self.large_batch_summary_label.setAccessibleName("大批量任务总进度")
        large_batch_layout.addWidget(self.large_batch_summary_label)
        self.large_batch_summary_card.hide()
        layout.addWidget(self.large_batch_summary_card)

        self.pdi_status_card = QFrame()
        self.pdi_status_card.setObjectName("PdiStatusCard")
        pdi_status_layout = QVBoxLayout(self.pdi_status_card)
        pdi_status_layout.setContentsMargins(14, 10, 14, 10)
        pdi_status_layout.setSpacing(8)
        self.pdi_status_header = QGridLayout()
        self.pdi_status_header.setHorizontalSpacing(8)
        self.pdi_status_header.setVerticalSpacing(8)
        self.pdi_status_title = QLabel("PDI 便携阅片目录")
        self.pdi_status_title.setObjectName("SectionTitle")
        self.pdi_status_label = QLabel("下载完成后自动生成")
        self.pdi_status_label.setObjectName("PdiStatusText")
        self.pdi_status_label.setWordWrap(True)
        self.pdi_status_label.setSizePolicy(
            QSizePolicy.Ignored, QSizePolicy.Preferred
        )
        self.pdi_view_button = QPushButton("打开影像")
        self.pdi_view_button.setObjectName("PrimaryButton")
        self.pdi_view_button.setEnabled(False)
        self.pdi_view_button.setToolTip("从当前导出目录启动离线阅片器并打开影像")
        self.pdi_view_button.clicked.connect(self._open_pdi_viewer)
        self.pdi_open_button = QPushButton("打开导出目录")
        self.pdi_open_button.setEnabled(False)
        self.pdi_open_button.setToolTip("打开本批 PDI 导出目录")
        self.pdi_open_button.clicked.connect(self._open_pdi_directory)
        self.pdi_retry_button = QPushButton("重试 PDI")
        self.pdi_retry_button.setEnabled(False)
        self.pdi_retry_button.clicked.connect(self._retry_pdi)
        self._pdi_action_buttons = (
            self.pdi_view_button,
            self.pdi_open_button,
            self.pdi_retry_button,
        )
        pdi_status_layout.addLayout(self.pdi_status_header)
        self.pdi_progress_bar = QProgressBar()
        self.pdi_progress_bar.setRange(0, 100)
        self.pdi_progress_bar.setValue(0)
        self.pdi_progress_bar.setTextVisible(False)
        pdi_status_layout.addWidget(self.pdi_progress_bar)
        layout.addWidget(self.pdi_status_card)
        self._update_responsive_layouts(force=True)

        self.task_splitter = QSplitter(Qt.Vertical)
        self.task_table = QTableWidget(0, 6)
        self.task_table.setHorizontalHeaderLabels(
            ["检查号", "状态", "文件数", "速度", "耗时", "详情"]
        )
        self.task_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.task_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.task_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.task_table.setAlternatingRowColors(True)
        self.task_table.verticalHeader().setVisible(False)
        self.task_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.task_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.task_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.task_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.task_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.task_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.task_table.doubleClicked.connect(self._open_selected_result)
        self.task_splitter.addWidget(self.task_table)
        self.log_panel = self._build_log_panel()
        self.task_splitter.addWidget(self.log_panel)
        self.log_panel.setVisible(self._log_panel_expanded)
        self.task_splitter.setSizes([360, 180])
        layout.addWidget(self.task_splitter, 1)
        self.task_scroll.setWidget(content)
        page_layout.addWidget(self.task_scroll)
        return page

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if hasattr(self, "task_action_layout"):
            self._update_responsive_layouts()

    def _update_responsive_layouts(self, *, force: bool = False) -> None:
        detail_width = (
            self.task_scroll.viewport().width()
            if hasattr(self, "task_scroll")
            else self.width()
        )
        compact = detail_width < 900
        if not force and compact == self._compact_action_layout:
            return
        self._compact_action_layout = compact

        task_widgets = (self.progress_label, *self._task_action_buttons)
        for widget in task_widgets:
            self.task_action_layout.removeWidget(widget)
        for column in range(len(self._task_action_buttons) + 2):
            self.task_action_layout.setColumnStretch(column, 0)
        self.task_action_layout.setColumnStretch(0, 1)
        self.progress_label.setWordWrap(compact)
        if compact:
            self.task_action_layout.addWidget(
                self.progress_label,
                0,
                0,
                1,
                len(self._task_action_buttons) + 1,
            )
            for column, button in enumerate(self._task_action_buttons, start=1):
                self.task_action_layout.addWidget(button, 1, column)
        else:
            self.task_action_layout.addWidget(self.progress_label, 0, 0)
            for column, button in enumerate(self._task_action_buttons, start=1):
                self.task_action_layout.addWidget(button, 0, column)

        pdi_widgets = (
            self.pdi_status_title,
            self.pdi_status_label,
            *self._pdi_action_buttons,
        )
        for widget in pdi_widgets:
            self.pdi_status_header.removeWidget(widget)
        for column in range(len(self._pdi_action_buttons) + 2):
            self.pdi_status_header.setColumnStretch(column, 0)
        if compact:
            self.pdi_status_header.addWidget(self.pdi_status_title, 0, 0)
            self.pdi_status_header.addWidget(
                self.pdi_status_label,
                0,
                1,
                1,
                len(self._pdi_action_buttons) + 1,
            )
            self.pdi_status_header.setColumnStretch(1, 1)
            for column, button in enumerate(self._pdi_action_buttons, start=2):
                self.pdi_status_header.addWidget(button, 1, column)
        else:
            self.pdi_status_header.addWidget(self.pdi_status_title, 0, 0)
            self.pdi_status_header.addWidget(self.pdi_status_label, 0, 1)
            self.pdi_status_header.setColumnStretch(1, 1)
            for column, button in enumerate(self._pdi_action_buttons, start=2):
                self.pdi_status_header.addWidget(button, 0, column)

    def _build_log_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("Card")
        layout = QVBoxLayout(panel)
        header = QHBoxLayout()
        title = QLabel("运行日志")
        title.setObjectName("SectionTitle")
        header.addWidget(title)
        header.addStretch()
        self.log_scope_combo = QComboBox()
        self.log_scope_combo.setAccessibleName("日志范围")
        self.log_scope_combo.addItem("本任务", "task")
        self.log_scope_combo.addItem("全部", "all")
        self.log_scope_combo.currentIndexChanged.connect(
            self._refresh_multi_log_view
        )
        header.addWidget(self.log_scope_combo)
        self.log_detail_checkbox = QCheckBox("显示详细日志")
        self.log_detail_checkbox.setChecked(self._show_detailed_logs)
        self.log_detail_checkbox.setToolTip(
            "默认仅显示错误；开启后显示调试、信息、成功和警告。"
            "磁盘日志始终保留完整内容。"
        )
        self.log_detail_checkbox.toggled.connect(
            self._on_log_detail_toggled
        )
        header.addWidget(self.log_detail_checkbox)
        open_result = QPushButton("打开结果")
        open_result.clicked.connect(self._open_selected_result)
        copy_error = QPushButton("复制详情")
        copy_error.clicked.connect(self._copy_selected_detail)
        clear = QPushButton("清空显示")
        clear.setToolTip("只清空界面缓存，不删除磁盘日志")
        clear.clicked.connect(self._clear_logs)
        open_logs = QPushButton("日志目录")
        open_logs.clicked.connect(self._open_log_directory)
        for button in (open_result, copy_error, clear, open_logs):
            header.addWidget(button)
        layout.addLayout(header)
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.document().setMaximumBlockCount(5000)
        self.log_edit.setAccessibleName("运行日志")
        layout.addWidget(self.log_edit)
        return panel

    def _is_busy(self) -> bool:
        return any(
            item is not None
            for item in (
                self.worker,
                self.worker_thread,
                self.pdi_worker,
                self.pdi_thread,
            )
        )

    @staticmethod
    def _workspace_timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _workspace_task_id(self) -> str:
        if self._active_task_id:
            return self._active_task_id
        if self._pdi_task_id:
            return self._pdi_task_id
        if self._resume_checkpoint is not None:
            return self._resume_checkpoint.task_id
        return self.task_workspace.selected_task_id

    def _publish_workspace_summary(
        self,
        summary: TaskSummary,
        *,
        select: bool = False,
    ) -> None:
        self._workspace_task_summaries[summary.task_id] = summary
        self.task_workspace.upsert_task(summary)
        if select:
            self.task_workspace.select_task(summary.task_id)

    def _workspace_summary_from_checkpoint(
        self,
        checkpoint: TaskCheckpoint,
        *,
        phase: str | None = None,
    ) -> TaskSummary:
        results = list(checkpoint.results)
        partials = list(checkpoint.partial_results.values())
        completed_statuses = {
            AccessionStatus.COMPLETED,
            AccessionStatus.NO_DATA,
        }
        failed_statuses = {
            AccessionStatus.FAILED,
            AccessionStatus.PARTIAL,
        }
        total = len(checkpoint.accessions)
        processed = len(results)
        current = self._workspace_task_summaries.get(checkpoint.task_id)
        now = self._workspace_timestamp()
        return TaskSummary(
            task_id=checkpoint.task_id,
            name=f"任务 {checkpoint.task_id[:8]}",
            phase=phase or checkpoint.phase,
            total_count=total,
            processed_count=processed,
            pending_count=max(0, total - processed),
            completed_count=sum(
                result.status in completed_statuses for result in results
            ),
            failed_count=sum(result.status in failed_statuses for result in results),
            file_count=sum(result.file_count for result in [*results, *partials]),
            received_bytes=sum(
                result.received_bytes for result in [*results, *partials]
            ),
            speed_bytes_per_second=(
                current.speed_bytes_per_second if current is not None else 0.0
            ),
            queue_position=None,
            current_accession=(current.current_accession if current is not None else ""),
            error_message=(current.error_message if current is not None else ""),
            created_at=checkpoint.created_at,
            updated_at=now,
        )

    def _workspace_summary_from_batch(
        self,
        task_id: str,
        batch: BatchSummary,
        *,
        phase: str,
    ) -> TaskSummary:
        current = self._workspace_task_summaries.get(task_id)
        now = self._workspace_timestamp()
        results = list(batch.results)
        completed_statuses = {
            AccessionStatus.COMPLETED,
            AccessionStatus.NO_DATA,
        }
        failed_statuses = {
            AccessionStatus.FAILED,
            AccessionStatus.PARTIAL,
        }
        processed = sum(
            result.status
            in {
                *completed_statuses,
                *failed_statuses,
            }
            for result in results
        )
        total = current.total_count if current is not None else len(results)
        return TaskSummary(
            task_id=task_id,
            name=(current.name if current is not None else f"任务 {task_id[:8]}"),
            phase=phase,
            total_count=total,
            processed_count=processed,
            pending_count=max(0, total - processed),
            completed_count=sum(
                result.status in completed_statuses for result in results
            ),
            failed_count=sum(result.status in failed_statuses for result in results),
            file_count=sum(result.file_count for result in results),
            received_bytes=sum(result.received_bytes for result in results),
            speed_bytes_per_second=0.0,
            queue_position=None,
            current_accession="",
            error_message=(current.error_message if current is not None else ""),
            created_at=(current.created_at if current is not None else now),
            updated_at=now,
        )

    def _update_workspace_phase(
        self,
        phase: str,
        *,
        error_message: str | None = None,
    ) -> None:
        task_id = self._workspace_task_id()
        current = self._workspace_task_summaries.get(task_id)
        if current is None:
            return
        self._publish_workspace_summary(
            replace(
                current,
                phase=phase,
                error_message=(
                    current.error_message
                    if error_message is None
                    else error_message
                ),
                updated_at=self._workspace_timestamp(),
            )
        )

    def _show_new_task_editor(self) -> None:
        self.pages.setCurrentWidget(self.task_page)
        self.task_workspace.show_detail()
        if self.multi_task_enabled:
            self.task_workspace.clear_task_selection()
            self._selected_task_id = ""
            self._loaded_multi_task_id = ""
            self._multi_task_editor_active = True
            self._active_task_id = ""
            self._resume_checkpoint = None
            self._pdi_task_id = ""
            self.accession_edit.setReadOnly(False)
            self.accession_edit.clear()
            self.accession_edit.setPlaceholderText("每行一个检查号")
            self.destination_edit.setReadOnly(False)
            self.destination_edit.setText(self.config.dicom_destination_folder)
            self._populate_waiting_rows([])
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_label.setText("新建下载任务")
            self.task_config_summary_label.clear()
            self.task_config_summary_label.hide()
            self.stop_button.hide()
            self.pause_button.hide()
            self.retry_button.hide()
            self.delete_task_button.hide()
            self.start_button.show()
            self.start_button.setEnabled(True)
            concurrent = sum(
                summary.phase
                in {"preflight", "starting_receiver", "running", "pause_pending", "cancelling"}
                for summary in self._workspace_task_summaries.values()
            )
            waiting = any(
                summary.phase == "queued"
                for summary in self._workspace_task_summaries.values()
            )
            if concurrent == 0 and not waiting:
                start_text = "创建并开始"
            elif concurrent < self.config.max_concurrent_moves and not waiting:
                start_text = "创建并并发开始"
            else:
                start_text = "创建并等待并发槽"
            self.start_button.setText(start_text)
            self._refresh_multi_log_view()
        self._set_task_form_expanded(True)
        self.task_scroll.ensureWidgetVisible(self.accession_edit, 0, 24)
        self.accession_edit.setFocus(Qt.OtherFocusReason)

    def _on_workspace_task_selected(self, task_id: str) -> None:
        self.pages.setCurrentWidget(self.task_page)
        self.task_workspace.show_detail()
        if self.multi_task_enabled and task_id:
            self._selected_task_id = task_id
            self._multi_task_editor_active = False
            self._refresh_multi_log_view()
            self._load_multi_task_detail(task_id)

    def _show_settings(self) -> None:
        if self._is_busy():
            return
        shared_locked = bool(
            self.multi_task_enabled
            and self.task_controller is not None
            and any(
                item.phase not in {"completed", "failed", "cancelled"}
                for item in self.task_controller.list_tasks()
            )
        )
        self.settings_page.set_config(self.config)
        self.settings_page.set_shared_settings_locked(shared_locked)
        self.pages.setCurrentIndex(1)

    def _choose_accession_file_from_shortcut(self) -> None:
        if (
            self.pages.currentWidget() is self.task_page
            and self.accession_button.isEnabled()
        ):
            self._choose_accession_file()

    def _start_download_from_shortcut(self) -> None:
        if (
            self.pages.currentWidget() is self.task_page
            and self.start_button.isEnabled()
        ):
            self._start_download()

    def _show_settings_from_shortcut(self) -> None:
        if (
            self.pages.currentWidget() is self.task_page
            and self.settings_button.isEnabled()
        ):
            self._show_settings()

    def _cancel_settings(self) -> None:
        self.settings_page.set_config(self.config)
        self.pages.setCurrentIndex(0)

    def _show_activation(self) -> None:
        if self._is_busy():
            return
        if activate_gui(self):
            self._refresh_entitlement_status()
            self._append_log("授权", "软件注册成功", "success")

    def _show_release_notes(self) -> None:
        if self.release_notes_dialog is None:
            self.release_notes_dialog = ReleaseNotesDialog(
                load_release_notes(self.project_root), self
            )
        self.release_notes_dialog.show()
        self.release_notes_dialog.raise_()
        self.release_notes_dialog.activateWindow()

    def _refresh_entitlement_status(self) -> None:
        text = entitlement_text()
        self.entitlement_status.setText(text)
        self.entitlement_status.setToolTip(text)
        self.entitlement_status.setAccessibleDescription(text)
        self.entitlement_status.setProperty(
            "status", "ok" if text.startswith("已注册") else "warning"
        )
        self.entitlement_status.style().unpolish(self.entitlement_status)
        self.entitlement_status.style().polish(self.entitlement_status)
        self.entitlement_status.setMinimumWidth(
            self.entitlement_status.sizeHint().width()
        )
        self.entitlement_status.updateGeometry()

    def _save_settings(self, config: AppConfig) -> None:
        config.access_numbers_file_path = self.config.access_numbers_file_path
        config.dicom_destination_folder = self.destination_edit.text().strip()
        if self.multi_task_enabled and self.task_controller is not None:
            runtime_changed = (
                shared_receiver_config(config) != shared_receiver_config(self.config)
                or config.max_concurrent_moves
                != self.config.max_concurrent_moves
            )
            if runtime_changed:
                unfinished = [
                    item
                    for item in self.task_controller.list_tasks()
                    if item.phase not in {"completed", "failed", "cancelled"}
                ]
                if unfinished:
                    QMessageBox.warning(
                        self,
                        "运行参数已锁定",
                        "仍有未结束任务，暂时不能修改并发数或 DCMTK 路径。PACS、AE 和接收端口可按新任务单独保存。",
                    )
                    return
                if not self.task_controller.shutdown():
                    QMessageBox.warning(
                        self,
                        "设置未保存",
                        "后台调度器尚未完全停止，请稍后重试。",
                    )
                    return
                self.task_controller = None
        recovery_task_id = (
            self._resume_checkpoint.task_id
            if self._resume_checkpoint is not None
            else self._pdi_task_id
        )
        if recovery_task_id:
            lease_was_held = self.task_store.lease_held
            if not lease_was_held and not self.task_store.try_acquire_lease():
                QMessageBox.warning(
                    self,
                    "设置未保存",
                    "另一个 DcmGet 实例正在使用恢复任务，无法修改其设置。",
                )
                return
            try:
                self.task_store.update_config(recovery_task_id, config)
            except TaskStateError as exc:
                QMessageBox.warning(self, "设置未保存", str(exc))
                return
            finally:
                if not lease_was_held:
                    self.task_store.release_lease()
            if self._resume_checkpoint is not None:
                self._resume_checkpoint.config = AppConfig.from_dict(config.to_dict())
        self.config = config
        save_config(self.config_path, self.config)
        self.task_workspace.set_concurrency_limit(self.config.max_concurrent_moves)
        self._sync_quick_pdi_controls_from_config()
        self.pages.setCurrentIndex(0)
        self.pdi_status_card.setVisible(self.config.pdi_export_enabled)
        previous_status = str(
            getattr(getattr(self.last_pdi_result, "status", ""), "value", "")
        )
        if (
            self.config.pdi_export_enabled
            and (self._pdi_source_files or self._pdi_task_id)
            and previous_status != "完成"
        ):
            self._set_pdi_status("设置已更新，可以重试 PDI 导出", "warning")
            self.pdi_retry_button.setEnabled(True)
            previous_output = str(
                getattr(self.last_pdi_result, "output_directory", "") or ""
            )
            self.pdi_open_button.setEnabled(
                bool(previous_output and Path(previous_output).is_dir())
            )
            self.pdi_view_button.setEnabled(
                bool(previous_output and pdi_viewer_command(previous_output))
            )
        self._refresh_tool_status()
        self._append_log("应用", "设置已保存", "success")

    def _refresh_tool_status(self) -> None:
        try:
            self.tools = self.resolver.resolve(self.config.dcmtk_bin_dir)
            text = f"DCMTK {self.tools.version} 已就绪"
            self._set_tool_status(text, "ok")
            self.settings_page.set_dcmtk_status(text, True)
            if self.multi_task_enabled:
                self._ensure_task_controller()
        except Exception as exc:
            self.tools = None
            self._set_tool_status("DCMTK 未就绪", "error")
            self.settings_page.set_dcmtk_status(str(exc), False)

    def _ensure_task_controller(self) -> bool:
        if not self.multi_task_enabled:
            return False
        if self.task_controller is not None:
            return True
        if self.tools is None:
            return False
        try:
            controller = TaskExecutionController(
                self.config,
                self.tools,
                catalog_path=self._task_catalog_path,
                legacy_path=self.task_store.path,
                before_task_move=lambda task_id: consume_trial(task_id=task_id),
                project_root=self.project_root,
                parent=self,
            )
        except Exception as exc:
            self._append_log("多任务", str(exc), "error")
            return False
        controller.task_updated.connect(self._on_multi_task_updated)
        controller.tasks_updated.connect(self._on_multi_tasks_updated)
        controller.progress.connect(self._on_multi_progress)
        controller.log.connect(self._on_multi_log)
        controller.scheduler_error.connect(self._on_multi_scheduler_error)
        controller.pdi_progress.connect(self._on_multi_pdi_progress)
        controller.pdi_finished.connect(self._on_multi_pdi_finished)
        self.task_controller = controller
        for message in controller.startup_messages:
            self._append_log("恢复", message, "warning")
        initial_tasks = controller.list_tasks()
        self._on_multi_tasks_updated(initial_tasks)
        if initial_tasks and not self._selected_task_id:
            self._selected_task_id = initial_tasks[0].task_id
            self._multi_task_editor_active = False
            self.task_workspace.select_task(self._selected_task_id)
        controller.start()
        return True

    def _on_multi_tasks_updated(self, summaries: object) -> None:
        if not self.multi_task_enabled:
            return
        values = list(summaries) if isinstance(summaries, (list, tuple)) else []
        # Worker-thread snapshots can arrive out of order.  Reload the current
        # catalog view on the UI thread so an older signal cannot temporarily
        # remove a task that was just created in another concurrent slot.
        if self.task_controller is not None:
            try:
                values = self.task_controller.list_tasks()
            except (OSError, RuntimeError, TaskStateError):
                pass
        self._workspace_task_summaries = {
            summary.task_id: summary for summary in values
        }
        self.task_workspace.set_tasks(values)
        if self._selected_task_id:
            self.task_workspace.select_task(self._selected_task_id)

    def _on_multi_task_updated(self, summary: TaskSummary) -> None:
        previous = self._workspace_task_summaries.get(summary.task_id)
        if previous is None and self.task_controller is not None:
            try:
                summary = self.task_controller.catalog.get_summary(summary.task_id)
            except TaskStateError:
                # A queued worker/PDI signal can arrive after the user deletes
                # the terminal task.  Never recreate a UI-only ghost record.
                self.task_workspace.remove_task(summary.task_id)
                return
        elif previous is not None and summary.updated_at < previous.updated_at:
            # Signals from the download, live-progress and PDI threads can be
            # delivered out of order.  Keep the newest persisted task view.
            return
        self._workspace_task_summaries[summary.task_id] = summary
        self.task_workspace.upsert_task(summary)
        if summary.task_id != self._selected_task_id:
            return
        self._render_multi_summary(summary)
        if self._loaded_multi_task_id != summary.task_id:
            self._load_multi_task_detail(summary.task_id)
            return
        if summary.total_count > TASK_TABLE_DETAIL_LIMIT:
            self._apply_large_multi_task_summary(summary)
            if previous is None or previous.phase != summary.phase:
                self._render_multi_pdi_result(summary)
            return
        if (
            previous is None
            or previous.processed_count != summary.processed_count
            or previous.phase != summary.phase
        ):
            self._load_multi_task_detail(summary.task_id)

    def _apply_large_multi_task_summary(self, summary: TaskSummary) -> None:
        """Refresh a hidden-detail task from its exact catalog summary."""

        self._display_total = summary.total_count
        self._hidden_accession_count = summary.total_count
        self._task_table_summary_mode = True
        self._summary_results = {}
        self._summary_processed = summary.processed_count
        self._summary_files = summary.file_count
        self._summary_status_counts = {
            AccessionStatus.COMPLETED: summary.completed_only_count,
            AccessionStatus.NO_DATA: summary.no_data_count,
            AccessionStatus.PARTIAL: summary.partial_count,
            AccessionStatus.FAILED: summary.failed_only_count,
            AccessionStatus.CANCELLED: summary.cancelled_count,
        }
        self._update_large_batch_summary_label(summary.total_count)
        self._sync_task_detail_visibility()

    def _load_multi_task_detail(self, task_id: str) -> None:
        controller = self.task_controller
        if controller is None or not task_id:
            return
        try:
            detail = controller.manager.get_task_detail(
                task_id,
                accession_limit=TASK_TABLE_DETAIL_LIMIT + 1,
            )
        except TaskStateError as exc:
            self._append_log("多任务", str(exc), "error")
            return
        summary = detail.summary
        self._selected_task_id = task_id
        self._loaded_multi_task_id = task_id
        self._display_total = summary.total_count
        self._active_task_id = ""
        self.destination_edit.setText(detail.config.dicom_destination_folder)
        self.task_config_summary_label.setText(
            f"PACS：{detail.config.pacs_ae_title} @ "
            f"{detail.config.pacs_server_ip}:{detail.config.pacs_server_port} · "
            f"调用 AE：{detail.config.calling_ae_title} · "
            f"接收：{detail.config.storage_ae_title}:{detail.config.storage_port}"
        )
        self.task_config_summary_label.setToolTip(
            "这是创建任务时保存的连接配置；之后修改设置不会覆盖本任务。"
        )
        self.task_config_summary_label.show()
        self.destination_edit.setReadOnly(True)
        self.accession_edit.setReadOnly(True)
        previous = self.quick_pdi_checkbox.blockSignals(True)
        self.quick_pdi_checkbox.setChecked(detail.config.pdi_export_enabled)
        self.quick_pdi_checkbox.blockSignals(previous)
        self.quick_pdi_checkbox.setEnabled(False)
        self.quick_pdi_output_button.setEnabled(False)
        self._set_task_form_expanded(False)

        if summary.total_count <= TASK_TABLE_DETAIL_LIMIT:
            self._hidden_accession_count = 0
            blocked = self.accession_edit.blockSignals(True)
            self.accession_edit.setPlainText("\n".join(detail.accessions))
            self.accession_edit.blockSignals(blocked)
            self.current_accessions = list(detail.accessions)
            self._populate_waiting_rows(detail.accessions)
            for result in [*detail.results, *detail.partial_results.values()]:
                self._set_result_row(result)
        else:
            blocked = self.accession_edit.blockSignals(True)
            self.accession_edit.clear()
            self.accession_edit.setPlaceholderText(
                f"任务包含 {summary.total_count:,} 个检查号，明细已隐藏"
            )
            self.accession_edit.blockSignals(blocked)
            self.current_accessions = []
            self._hidden_accession_count = summary.total_count
            self._task_table_summary_mode = True
            self.task_table.setRowCount(0)
            self.row_by_accession.clear()
            self._apply_large_multi_task_summary(summary)
        self._render_multi_summary(summary)
        self.last_pdi_result = self._last_pdi_results.get(task_id)
        if self.last_pdi_result is None:
            try:
                self.last_pdi_result = controller.load_pdi_result(task_id)
            except TaskStateError as exc:
                self._append_log("PDI", f"无法恢复 PDI 结果：{exc}", "error")
            if self.last_pdi_result is not None:
                self._last_pdi_results[task_id] = self.last_pdi_result
        self._render_multi_pdi_result(summary)

    def _render_multi_summary(self, summary: TaskSummary) -> None:
        self.progress_bar.setRange(0, max(1, summary.total_count))
        self.progress_bar.setValue(summary.processed_count)
        speed = format_transfer_rate(summary.speed_bytes_per_second)
        speed_text = f" · {speed}" if speed != "—" else ""
        current = (
            f" · {summary.current_accession}" if summary.current_accession else ""
        )
        self.progress_label.setText(
            f"{summary.processed_count:,}/{summary.total_count:,}{current}"
            f" · {summary.file_count:,} 个文件{speed_text}"
        )
        phase = summary.phase
        running = phase in {"queued", "running", "pause_pending", "cancelling"}
        stoppable = running or phase in {"paused", "pdi_pending", "pdi_running"}
        self.start_button.hide()
        self.stop_button.setVisible(stoppable)
        self.stop_button.setEnabled(phase not in {"cancelling"})
        self.pause_button.setVisible(phase in {"queued", "running", "pause_pending", "paused"})
        self.pause_button.setEnabled(phase not in {"pause_pending"})
        self.pause_button.setText("继续下载" if phase == "paused" else "暂停")
        retryable = phase in {"failed", "cancelled", "download_retryable"}
        self.retry_button.setVisible(retryable)
        self.retry_button.setEnabled(retryable)
        self.retry_button.setText("重试失败项" if phase == "download_retryable" else "恢复任务")
        deletable = phase in DELETABLE_TASK_PHASES
        self.delete_task_button.setVisible(deletable)
        self.delete_task_button.setEnabled(deletable)
        self.accept_partial_button.hide()
        self.discard_resume_button.hide()
        self.open_existing_pdi_button.setVisible(True)
        self.settings_button.setEnabled(True)
        if phase == "pause_pending":
            self.progress_label.setText(
                self.progress_label.text() + " · 当前检查号完成后暂停"
            )
        elif phase == "queued":
            queue_text = (
                f" · 队列第 {summary.queue_position} 位"
                if summary.queue_position
                else ""
            )
            self.progress_label.setText(
                f"等待可用并发槽{queue_text} · "
                f"已处理 {summary.processed_count:,}/{summary.total_count:,}"
            )
        elif phase == "pdi_pending":
            self.progress_label.setText("下载完成，PDI 正在排队")
        elif phase == "pdi_running":
            self.progress_label.setText("下载完成，正在生成 PDI")
        elif phase == "pdi_retryable":
            self.progress_label.setText("下载完成，PDI 可重试")
        elif phase == "completed":
            self.progress_label.setText(
                f"任务完成 · {summary.file_count:,} 个文件"
            )
        elif phase == "failed" and summary.error_message:
            self.progress_label.setText(f"任务失败：{summary.error_message}")

    def _on_multi_progress(
        self,
        task_id: str,
        result: AccessionResult,
    ) -> None:
        if task_id != self._selected_task_id:
            return
        if self._task_table_summary_mode:
            self._record_large_batch_result(
                result,
                total=self._display_total,
            )
        else:
            self._set_result_row(result)
        speed = format_transfer_rate(result.speed_bytes_per_second)
        speed_text = f" · {speed}" if speed != "—" else ""
        self.progress_label.setText(
            f"{result.accession} · {result.status.value} · "
            f"{result.file_count} 个文件{speed_text}"
        )

    def _on_multi_scheduler_error(self, task_id: str, message: str) -> None:
        self._on_multi_log(task_id, "调度器", message, "error")
        if task_id and task_id == self._selected_task_id:
            self.progress_label.setText(f"任务中断：{message}")

    def _on_multi_pdi_progress(
        self,
        task_id: str,
        stage: object,
        current: int,
        total: int,
        message: str,
    ) -> None:
        if task_id != self._selected_task_id:
            return
        self.pdi_status_card.show()
        self._set_pdi_status(message, "generating")
        self.pdi_progress_bar.setRange(0, max(1, total))
        self.pdi_progress_bar.setValue(current)

    def _on_multi_pdi_finished(self, task_id: str, result: object) -> None:
        self._last_pdi_results[task_id] = result
        if task_id != self._selected_task_id:
            return
        self.last_pdi_result = result
        status = str(getattr(getattr(result, "status", ""), "value", ""))
        message = str(getattr(result, "message", "") or status)
        self._set_pdi_status(
            message,
            "ok" if status == "完成" else "warning" if status == "部分成功" else "error",
        )
        self._render_multi_pdi_result(
            self._workspace_task_summaries.get(task_id)
        )

    def _render_multi_pdi_result(self, summary: TaskSummary | None) -> None:
        result = self._last_pdi_results.get(self._selected_task_id)
        output = str(getattr(result, "output_directory", "") or "")
        output_exists = bool(output and Path(output).is_dir())
        pdi_phase = summary.phase if summary is not None else ""
        enabled = bool(result) or pdi_phase.startswith("pdi_")
        self.pdi_status_card.setVisible(enabled)
        if result is not None and pdi_phase not in {"pdi_pending", "pdi_running"}:
            status = str(getattr(getattr(result, "status", ""), "value", ""))
            message = str(getattr(result, "message", "") or status)
            display_state = (
                "ok"
                if status == "完成"
                else "warning" if status == "部分成功" else "error"
            )
            self._set_pdi_status(message, display_state)
        self.pdi_open_button.setEnabled(output_exists)
        self.pdi_view_button.setEnabled(
            bool(output_exists and pdi_viewer_command(output))
        )
        self.pdi_retry_button.setEnabled(pdi_phase == "pdi_retryable")

    def _set_tool_status(self, text: str, status: str) -> None:
        self.tool_status.setText(text)
        self.tool_status.setToolTip(text)
        self.tool_status.setAccessibleDescription(text)
        self.tool_status.setProperty("status", status)
        self.tool_status.style().unpolish(self.tool_status)
        self.tool_status.style().polish(self.tool_status)
        self.tool_status.setMinimumWidth(self.tool_status.sizeHint().width())
        self.tool_status.updateGeometry()

    def _load_configured_accessions(self) -> None:
        path = Path(self.config.access_numbers_file_path).expanduser()
        if not path.is_absolute():
            path = self.project_root / path
        if path.exists():
            try:
                self.accession_edit.setPlainText(path.read_text(encoding="utf-8-sig"))
            except OSError:
                pass

    def _offer_task_resume(self) -> None:
        if self._is_busy():
            return
        if not self.task_store.path.is_file():
            return
        if not self.task_store.try_acquire_lease():
            self._append_log(
                "恢复",
                "上次任务正在另一个 DcmGet 实例中运行，本窗口不会修改其恢复点",
                "warning",
            )
            return
        try:
            checkpoint = self.task_store.load(include_archived_files=False)
            if checkpoint is not None:
                for message in self.task_store.cleanup_recorded_processes(
                    checkpoint.task_id
                ):
                    self._append_log("恢复", message, "warning")
        except TaskStateError as exc:
            self.task_store.release_lease()
            self._append_log("恢复", str(exc), "error")
            QMessageBox.warning(
                self,
                "无法读取上次任务",
                f"{exc}\n\n程序仍可正常使用；开始新任务时会建立新的恢复点。",
            )
            return
        if checkpoint is None:
            self.task_store.release_lease()
            return
        self._publish_workspace_summary(
            self._workspace_summary_from_checkpoint(
                checkpoint,
                phase=(
                    "interrupted"
                    if checkpoint.phase == "downloading"
                    else checkpoint.phase
                ),
            ),
            select=True,
        )
        pending = checkpoint.pending_accessions
        retryable_results = [
            result
            for result in checkpoint.results
            if result.status in {AccessionStatus.FAILED, AccessionStatus.PARTIAL}
        ]
        if (
            checkpoint.phase == "downloading"
            and not pending
            and retryable_results
        ):
            try:
                self.task_store.set_phase(checkpoint.task_id, "download_retryable")
                checkpoint.phase = "download_retryable"
            except TaskStateError as exc:
                self.task_store.release_lease()
                self._append_log("恢复", str(exc), "error")
                return
        pdi_phases = {"pdi_pending", "pdi_running", "pdi_retryable"}
        pdi_pending = checkpoint.phase in pdi_phases
        archived_count = sum(result.file_count for result in checkpoint.results)
        if (
            checkpoint.phase == "downloading"
            and not pending
            and checkpoint.config.pdi_export_enabled
            and archived_count
        ):
            pdi_pending = True
        if pdi_pending:
            if checkpoint.phase == "downloading":
                try:
                    self.task_store.set_phase(checkpoint.task_id, "pdi_pending")
                    checkpoint.phase = "pdi_pending"
                except TaskStateError as exc:
                    self.task_store.release_lease()
                    self._append_log("恢复", str(exc), "error")
                    return
            self._offer_pdi_resume(checkpoint, archived_count)
            return
        if checkpoint.phase == "download_retryable":
            retryable = [
                result.accession
                for result in checkpoint.results
                if result.status
                in {AccessionStatus.FAILED, AccessionStatus.PARTIAL}
            ]
            if not retryable:
                self._append_log("恢复", "失败重试恢复点中没有失败项", "error")
                self._hold_download_resume(
                    checkpoint, "恢复点需要人工处理；可明确放弃后新建任务"
                )
                return
            answer = QMessageBox.question(
                self,
                "重试上次失败项",
                (
                    f"检测到上次任务有 {len(retryable):,} 个失败或部分成功的检查号。\n\n"
                    "继续后只重试这些检查号，已经完成和无数据的项目不会重复请求；"
                    "已收到的 DICOM 文件会保留并去重。\n\n"
                    "选择“否”会保留恢复记录，之后仍可继续。"
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if answer != QMessageBox.Yes:
                self._hold_download_resume(
                    checkpoint,
                    f"已保留 {len(retryable):,} 个失败项，点击“重试失败项”继续",
                )
                return
            self._apply_checkpoint_config(checkpoint)
            self._start_download(resume_checkpoint=checkpoint)
            return
        if not pending:
            try:
                self.task_store.clear(checkpoint.task_id)
            except TaskStateError as exc:
                self._append_log("恢复", str(exc), "error")
            self.task_store.release_lease()
            return

        answer = QMessageBox.question(
            self,
            "继续上次任务",
            (
                f"检测到一个未完成任务：共 {len(checkpoint.accessions):,} 个检查号，"
                f"已处理 {len(checkpoint.results):,} 个，剩余 {len(pending):,} 个。\n\n"
                f"保存目录：{checkpoint.config.dicom_destination_folder}\n\n"
                "继续后不会重新请求已处理项；意外退出时正在下载的检查号会重新请求一次。"
                "选择“否”会保留恢复记录，之后仍可继续。"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            self._hold_download_resume(
                checkpoint,
                f"已保留未完成任务，剩余 {len(pending):,} 个检查号",
            )
            return

        self._apply_checkpoint_config(checkpoint)
        self._append_log(
            "恢复",
            f"准备继续上次任务：剩余 {len(pending):,}/{len(checkpoint.accessions):,}",
            "info",
        )
        self._start_download(resume_checkpoint=checkpoint)

    def _apply_checkpoint_config(self, checkpoint: TaskCheckpoint) -> None:
        self.config = AppConfig.from_dict(checkpoint.config.to_dict())
        self.settings_page.set_config(self.config)
        self.destination_edit.setText(self.config.dicom_destination_folder)
        self._sync_quick_pdi_controls_from_config()
        self._display_total = len(checkpoint.accessions)
        if len(checkpoint.accessions) <= TASK_TABLE_DETAIL_LIMIT:
            self._hidden_accession_count = 0
            self.accession_edit.setPlaceholderText("每行一个检查号")
            self.accession_edit.setPlainText("\n".join(checkpoint.accessions))
        else:
            previous = self.accession_edit.blockSignals(True)
            self.accession_edit.clear()
            self.accession_edit.setPlaceholderText(
                f"恢复任务包含 {len(checkpoint.accessions):,} 个检查号，明细已隐藏"
            )
            self.accession_edit.blockSignals(previous)
            self.current_accessions = []
            self._hidden_accession_count = len(checkpoint.accessions)
            self.invalid_accessions = ()
            self.accession_summary.setText(
                f"恢复任务 {len(checkpoint.accessions):,} 条 · 明细已隐藏"
            )
            self._update_task_form_summary()
            self._populate_waiting_rows(checkpoint.accessions)
            self._reset_large_batch_summary(
                len(checkpoint.accessions),
                [*checkpoint.results, *checkpoint.partial_results.values()],
            )

    def _hold_download_resume(
        self, checkpoint: TaskCheckpoint, message: str
    ) -> None:
        self._apply_checkpoint_config(checkpoint)
        self._resume_checkpoint = checkpoint
        self._publish_workspace_summary(
            self._workspace_summary_from_checkpoint(
                checkpoint,
                phase=(
                    "interrupted"
                    if checkpoint.phase == "downloading"
                    else checkpoint.phase
                ),
            ),
            select=True,
        )
        self.last_summary = BatchSummary(list(checkpoint.results))
        self.progress_label.setText(message)
        self.task_store.release_lease()
        self._set_running(False)

    def _offer_pdi_resume(
        self,
        checkpoint: TaskCheckpoint,
        archived_count: int,
    ) -> None:
        self._accepted_partial_results = any(
            result.status in {AccessionStatus.FAILED, AccessionStatus.PARTIAL}
            for result in checkpoint.results
        )
        answer = QMessageBox.question(
            self,
            "继续上次 PDI 导出",
            (
                f"上次任务的 {len(checkpoint.accessions):,} 个检查号已经下载完成，"
                f"还有 {archived_count:,} 个 DICOM 文件等待生成或重试 PDI。\n\n"
                "继续时不会重新下载；选择“否”会保留恢复记录，之后仍可继续。"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            self._apply_checkpoint_config(checkpoint)
            self.last_summary = BatchSummary(list(checkpoint.results))
            self._pdi_task_id = checkpoint.task_id
            self._pdi_reuse_published = checkpoint.phase == "pdi_running"
            self._pdi_source_files = []
            self._set_pdi_status("PDI 恢复记录已保留，可稍后重试", "warning")
            self.pdi_status_card.show()
            self.pdi_retry_button.setEnabled(bool(archived_count))
            self.task_store.release_lease()
            self._set_running(False)
            return

        self._apply_checkpoint_config(checkpoint)
        self.last_summary = BatchSummary(list(checkpoint.results))
        self._display_total = len(checkpoint.accessions)
        self._populate_waiting_rows(checkpoint.accessions)
        if self._task_table_summary_mode:
            self._reset_large_batch_summary(
                self._display_total,
                checkpoint.results,
            )
        else:
            for result in checkpoint.results:
                self._set_result_row(result)
        self.progress_bar.setRange(0, max(1, self._display_total))
        self.progress_bar.setValue(self._display_total)
        self._pdi_task_id = checkpoint.task_id
        self._pdi_reuse_published = checkpoint.phase == "pdi_running"
        try:
            archived_files = self.task_store.load_archived_files(checkpoint.task_id)
        except TaskStateError as exc:
            self.task_store.release_lease()
            self._show_pdi_skipped(str(exc))
            return
        self._pdi_source_files = archived_files
        self._append_log("恢复", "下载已完成，正在继续 PDI 导出", "info")
        self._start_pdi_export(archived_files)

    def _cleanup_pdi_partial(self, checkpoint: TaskCheckpoint) -> bool:
        if not checkpoint.pdi_attempt_id:
            return True
        try:
            from .pdi import cleanup_interrupted_pdi

            removed = cleanup_interrupted_pdi(
                checkpoint.config,
                checkpoint.pdi_attempt_id,
            )
        except (OSError, ValueError) as exc:
            self._append_log("PDI", str(exc), "error")
            QMessageBox.warning(
                self,
                "无法放弃 PDI 恢复",
                f"{exc}\n\n恢复记录已保留，请关闭占用文件的程序后重试。",
            )
            return False
        for path in removed:
            self._append_log("PDI", f"已删除中断的暂存目录：{path}", "warning")
        return True

    def _choose_accession_file(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "选择检查号文件",
            self.config.access_numbers_file_path,
            "文本文件 (*.txt);;所有文件 (*)",
        )
        if selected:
            self._load_accession_file(selected)

    def _load_accession_file(self, path: str) -> None:
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
        except OSError as exc:
            QMessageBox.critical(self, "无法读取文件", str(exc))
            return
        self.config.access_numbers_file_path = path
        self.accession_edit.setPlainText(text)
        self._append_log("应用", f"已载入检查号文件：{path}", "info")

    def _choose_destination(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "选择 DICOM 保存目录", self.destination_edit.text()
        )
        if selected:
            self.destination_edit.setText(selected)

    def _sync_quick_pdi_controls_from_config(self) -> None:
        previous = self.quick_pdi_checkbox.blockSignals(True)
        self.quick_pdi_checkbox.setChecked(self.config.pdi_export_enabled)
        self.quick_pdi_checkbox.blockSignals(previous)
        self._update_quick_pdi_summary()

    def _update_quick_pdi_summary(self, _value=None) -> None:
        configured = self.config.pdi_output_folder.strip()
        destination = self.destination_edit.text().strip()
        if configured:
            path_text = configured
            summary = f"PDI 保存位置：{configured}"
        elif destination:
            path_text = str(Path(destination).expanduser() / "PDI")
            summary = f"PDI 保存位置：{path_text}（默认）"
        else:
            path_text = "DICOM 保存目录下的 PDI（默认）"
            summary = f"PDI 保存位置：{path_text}"
        self.quick_pdi_output_label.setText(summary)
        self.quick_pdi_output_label.setToolTip(path_text)
        self.quick_pdi_output_label.setAccessibleDescription(path_text)
        recovery_pending = bool(self._resume_checkpoint or self._pdi_task_id)
        self.quick_pdi_output_button.setEnabled(
            self.quick_pdi_checkbox.isChecked()
            and not self._is_busy()
            and not recovery_pending
        )

    def _on_quick_pdi_toggled(self, enabled: bool) -> None:
        if self._is_busy() or self._resume_checkpoint or self._pdi_task_id:
            self._sync_quick_pdi_controls_from_config()
            return
        self.config.pdi_export_enabled = enabled
        save_config(self.config_path, self.config)
        self.settings_page.set_config(self.config)
        self.pdi_status_card.setVisible(enabled)
        self._update_quick_pdi_summary()
        self._append_log(
            "应用",
            "已启用下载完成后生成 PDI" if enabled else "已关闭下载完成后生成 PDI",
            "info",
        )

    def _choose_quick_pdi_output(self) -> None:
        initial = (
            self.config.pdi_output_folder.strip()
            or self.destination_edit.text().strip()
            or str(Path.home())
        )
        selected = QFileDialog.getExistingDirectory(
            self, "选择 PDI 保存目录", initial
        )
        if not selected:
            return
        self.config.pdi_output_folder = selected
        save_config(self.config_path, self.config)
        self.settings_page.set_config(self.config)
        self._update_quick_pdi_summary()
        self._append_log("应用", f"PDI 保存目录：{selected}", "info")

    def _open_destination_directory(self) -> None:
        directory = self.destination_edit.text().strip()
        if not directory:
            QMessageBox.warning(self, "无法打开目录", "请先选择已存在的目标目录。")
            return
        path = Path(directory).expanduser()
        if path.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))
            return
        QMessageBox.warning(self, "无法打开目录", "请先选择已存在的目标目录。")

    def _set_destination_error(self, message: str, *, focus: bool = False) -> None:
        self.destination_edit.setProperty("invalid", bool(message))
        self.destination_edit.setToolTip(message)
        self.destination_edit.setAccessibleDescription(message)
        self.destination_error_label.setText(message)
        self.destination_error_label.setVisible(bool(message))
        self.destination_edit.style().unpolish(self.destination_edit)
        self.destination_edit.style().polish(self.destination_edit)
        if message and focus:
            self._set_task_form_expanded(True)
            self.task_scroll.ensureWidgetVisible(self.destination_edit, 0, 24)
            self.destination_edit.setFocus(Qt.OtherFocusReason)

    def _update_accession_preview(self) -> None:
        parsed = parse_accessions(self.accession_edit.toPlainText())
        self.current_accessions = parsed.values
        self._hidden_accession_count = 0
        self.invalid_accessions = parsed.invalid_values
        self.accession_summary.setText(
            f"有效 {len(parsed.values)} · 空行 {parsed.blank_count} · "
            f"重复 {parsed.duplicate_count} · 无效 {len(parsed.invalid_values)}"
        )
        self._update_task_form_summary()
        if self._is_busy():
            return
        self._populate_waiting_rows(parsed.values)

    def _populate_waiting_rows(self, accessions: list[str]) -> None:
        self._task_table_summary_mode = len(accessions) > TASK_TABLE_DETAIL_LIMIT
        self.row_by_accession = {}
        self.task_table.setUpdatesEnabled(False)
        try:
            self.task_table.clearContents()
            if self._task_table_summary_mode:
                self.task_table.setRowCount(0)
                self._reset_large_batch_summary(len(accessions))
            else:
                self.task_table.setRowCount(len(accessions))
                for row, accession in enumerate(accessions):
                    self.row_by_accession[accession] = row
                    self.task_table.setItem(row, 0, QTableWidgetItem(accession))
                    self._set_result_row(
                        AccessionResult(
                            accession=accession,
                            status=AccessionStatus.WAITING,
                        ),
                        row,
                    )
        finally:
            self.task_table.setUpdatesEnabled(True)
        self._sync_task_detail_visibility()

    def _reset_large_batch_summary(
        self,
        total: int,
        results: list[AccessionResult] | None = None,
    ) -> None:
        self._summary_results = {}
        self._summary_processed = 0
        self._summary_files = 0
        self._summary_status_counts = {}
        for result in results or []:
            self._record_large_batch_result(result, update_label=False)
        self._update_large_batch_summary_label(total)

    def _record_large_batch_result(
        self,
        result: AccessionResult,
        *,
        update_label: bool = True,
        total: int | None = None,
    ) -> None:
        if result.status in {AccessionStatus.WAITING, AccessionStatus.DOWNLOADING}:
            if update_label:
                self._update_large_batch_summary_label(
                    total or self._display_total or len(self.current_accessions),
                    result,
                )
            return
        previous = self._summary_results.get(result.accession)
        if previous is not None:
            self._summary_files -= previous.file_count
            self._summary_status_counts[previous.status] = max(
                0,
                self._summary_status_counts.get(previous.status, 1) - 1,
            )
        else:
            self._summary_processed += 1
        self._summary_results[result.accession] = AccessionResult(
            accession=result.accession,
            status=result.status,
            file_count=result.file_count,
        )
        self._summary_files += result.file_count
        self._summary_status_counts[result.status] = (
            self._summary_status_counts.get(result.status, 0) + 1
        )
        if update_label:
            self._update_large_batch_summary_label(
                total or self._display_total or len(self.current_accessions),
                result,
            )

    def _update_large_batch_summary_label(
        self,
        total: int,
        current: AccessionResult | None = None,
    ) -> None:
        status = self._summary_status_counts
        lines = [
            (
                f"共 {total:,} 个检查号；超过 {TASK_TABLE_DETAIL_LIMIT} 条，"
                "为保持界面流畅，已隐藏逐项列表。"
            ),
            (
                f"已处理 {self._summary_processed:,}/{total:,} · "
                f"完成 {status.get(AccessionStatus.COMPLETED, 0):,} · "
                f"无数据 {status.get(AccessionStatus.NO_DATA, 0):,} · "
                f"部分成功 {status.get(AccessionStatus.PARTIAL, 0):,} · "
                f"失败 {status.get(AccessionStatus.FAILED, 0):,} · "
                f"已取消 {status.get(AccessionStatus.CANCELLED, 0):,} · "
                f"文件 {self._summary_files:,}"
            ),
        ]
        if current is not None:
            lines.append(
                f"当前：{current.accession} · {current.status.value} · "
                f"{current.file_count:,} 个文件"
            )
        self.large_batch_summary_label.setText("\n".join(lines))
        failed_count = status.get(AccessionStatus.FAILED, 0) + status.get(
            AccessionStatus.PARTIAL, 0
        )
        self.copy_failed_button.setVisible(bool(failed_count))
        self.copy_failed_button.setEnabled(bool(failed_count))
        self.copy_failed_button.setToolTip(
            f"复制 {failed_count:,} 个失败或部分成功的检查号"
            if failed_count
            else ""
        )

    def _failed_accessions_for_copy(self) -> list[str]:
        if (
            self.multi_task_enabled
            and self.task_controller is not None
            and self._selected_task_id
            and self._task_table_summary_mode
        ):
            try:
                return self.task_controller.manager.list_failed_accessions(
                    self._selected_task_id
                )
            except TaskStateError as exc:
                self._append_log("多任务", str(exc), "error")
                return []
        return [
            result.accession
            for result in self._summary_results.values()
            if result.status in {AccessionStatus.FAILED, AccessionStatus.PARTIAL}
        ]

    def _copy_failed_accessions(self) -> None:
        accessions = self._failed_accessions_for_copy()
        if not accessions:
            return
        QApplication.clipboard().setText("\n".join(accessions))
        self._append_log(
            "应用",
            f"已复制 {len(accessions):,} 个失败或部分成功的检查号",
            "info",
        )

    def _sync_task_detail_visibility(self) -> None:
        self.task_table.setVisible(not self._task_table_summary_mode)
        self.large_batch_summary_card.setVisible(self._task_table_summary_mode)
        self.task_splitter.setVisible(
            not self._task_table_summary_mode or self._log_panel_expanded
        )

    def _create_multi_task(self, override: list[str] | None = None) -> None:
        if self.invalid_accessions and override is None:
            examples = "、".join(self.invalid_accessions[:3])
            QMessageBox.warning(
                self,
                "检查号包含危险字符",
                "检查号不能包含 DICOM 通配符 *、?、反斜杠或控制字符。"
                f"\n\n请修正：{examples}",
            )
            return
        accessions = list(
            override if override is not None else self.current_accessions
        )
        if not accessions:
            self._append_log("应用", "请先导入至少一个检查号", "error")
            QMessageBox.warning(
                self,
                "没有检查号",
                "请选择 TXT 文件或粘贴检查号后再创建任务。",
            )
            return

        task_config = AppConfig.from_dict(self.config.to_dict())
        task_config.access_numbers_file_path = (
            task_config.access_numbers_file_path or "access.txt"
        )
        task_config.dicom_destination_folder = self.destination_edit.text().strip()
        task_config.pdi_export_enabled = self.quick_pdi_checkbox.isChecked()
        check = preflight(task_config, self.resolver, check_port=False)
        self._show_preflight(check)
        self.settings_page.apply_errors(check.errors)
        destination_error = check.errors.get("dicom_destination_folder", "")
        self._set_destination_error(destination_error)
        if not check.ok or check.tools is None:
            self._append_log("预检", "新任务静态预检未通过", "error")
            QMessageBox.warning(
                self,
                "预检未通过",
                "请查看启动预检和设置页中的错误提示。",
            )
            return

        entitled, use_trial, entitlement_message = prepare_download_entitlement(
            self
        )
        if not entitled:
            if entitlement_message:
                self._append_log("授权", entitlement_message, "error")
                QMessageBox.warning(self, "无法创建任务", entitlement_message)
            return
        if entitlement_message == "已完成软件注册":
            self._refresh_entitlement_status()

        self.tools = check.tools
        if not self._ensure_task_controller() or self.task_controller is None:
            QMessageBox.warning(
                self,
                "多任务服务未就绪",
                "无法启动任务调度器，请查看运行日志。",
            )
            return
        try:
            summary = self.task_controller.create_task(
                task_config,
                accessions,
                trial_required=use_trial,
            )
        except TaskStateError as exc:
            self._append_log("多任务", str(exc), "error")
            QMessageBox.warning(self, "无法创建任务", str(exc))
            return

        self.config.access_numbers_file_path = task_config.access_numbers_file_path
        self.config.dicom_destination_folder = task_config.dicom_destination_folder
        save_config(self.config_path, self.config)
        self._selected_task_id = summary.task_id
        self._multi_task_editor_active = False
        self.task_workspace.select_task(summary.task_id)
        self._set_task_form_expanded(False)
        self._append_log(
            "多任务",
            f"已创建任务 {summary.task_id[:8]}，共 {summary.total_count:,} 个检查号",
            "success",
        )
        if self._loaded_multi_task_id != summary.task_id:
            self._load_multi_task_detail(summary.task_id)

    def _start_download(
        self,
        override: list[str] | None = None,
        *,
        resume_checkpoint: TaskCheckpoint | None = None,
    ) -> None:
        if self.multi_task_enabled and resume_checkpoint is None:
            self._create_multi_task(override)
            return
        if self._is_busy():
            return
        if (
            resume_checkpoint is None
            and override is None
            and self._resume_checkpoint is None
            and self.invalid_accessions
        ):
            examples = "、".join(self.invalid_accessions[:3])
            QMessageBox.warning(
                self,
                "检查号包含危险字符",
                (
                    "检查号不能包含 DICOM 通配符 *、?、反斜杠或控制字符。"
                    f"\n\n请修正：{examples}"
                ),
            )
            return
        if self._pdi_task_id:
            QMessageBox.warning(
                self,
                "PDI 任务待处理",
                "请先重试或放弃当前 PDI 恢复任务，再开始新的下载。",
            )
            return
        if resume_checkpoint is None and override is None:
            resume_checkpoint = self._resume_checkpoint
        if resume_checkpoint is not None:
            continuing_existing = bool(
                self._resume_checkpoint is not None
                and self._resume_checkpoint.task_id == resume_checkpoint.task_id
            )
            requested_destination = self.destination_edit.text().strip()
            if not self.task_store.try_acquire_lease():
                QMessageBox.warning(
                    self,
                    "任务正在运行",
                    "该恢复任务正在另一个 DcmGet 实例中运行。",
                )
                return
            if resume_checkpoint.phase == "download_retryable":
                try:
                    resume_checkpoint = self.task_store.prepare_download_retry(
                        resume_checkpoint.task_id,
                        include_archived_files=False,
                    )
                except TaskStateError as exc:
                    self.task_store.release_lease()
                    QMessageBox.warning(self, "无法准备失败项重试", str(exc))
                    return
            self._resume_checkpoint = resume_checkpoint
            accessions = resume_checkpoint.pending_accessions
            display_accessions = resume_checkpoint.accessions
            self.config = AppConfig.from_dict(resume_checkpoint.config.to_dict())
            if continuing_existing and requested_destination:
                self.config.dicom_destination_folder = requested_destination
            self.destination_edit.setText(self.config.dicom_destination_folder)
            self._sync_quick_pdi_controls_from_config()
            try:
                self.task_store.update_config(
                    resume_checkpoint.task_id,
                    self.config,
                )
                resume_checkpoint.config = AppConfig.from_dict(self.config.to_dict())
            except TaskStateError as exc:
                self.task_store.release_lease()
                QMessageBox.warning(self, "无法更新续传设置", str(exc))
                self._set_running(False)
                return
        else:
            accessions = list(
                override if override is not None else self.current_accessions
            )
            display_accessions = list(accessions)
        if not accessions:
            if resume_checkpoint is not None:
                try:
                    self.task_store.clear(resume_checkpoint.task_id)
                except TaskStateError as exc:
                    self._append_log("恢复", str(exc), "error")
                self._resume_checkpoint = None
                self.task_store.release_lease()
                self._set_running(False)
                return
            self._append_log("应用", "请先导入至少一个检查号", "error")
            QMessageBox.warning(self, "没有检查号", "请选择 TXT 文件或粘贴检查号后再开始。")
            return

        self.config.access_numbers_file_path = self.config.access_numbers_file_path or "access.txt"
        self.config.dicom_destination_folder = self.destination_edit.text().strip()
        check = preflight(self.config, self.resolver)
        self._show_preflight(check)
        self.settings_page.apply_errors(check.errors)
        destination_error = check.errors.get("dicom_destination_folder", "")
        self._set_destination_error(destination_error)
        if not check.ok or check.tools is None:
            self._append_log("预检", "启动预检未通过，请修正标红设置", "error")
            QMessageBox.warning(self, "预检未通过", "请查看启动预检和设置页中的错误提示。")
            has_settings_error = any(
                key != "dicom_destination_folder" for key in check.errors
            )
            if has_settings_error:
                self.pages.setCurrentIndex(1)
            elif destination_error:
                self._set_destination_error(destination_error, focus=True)
            if resume_checkpoint is not None:
                self.task_store.release_lease()
                self._set_running(False)
            return

        already_counted_trial = bool(
            resume_checkpoint
            and trial_task_consumed(resume_checkpoint.task_id)
        )
        if already_counted_trial:
            entitled, use_trial, entitlement_message = (
                True,
                True,
                "继续已计次的免费试用任务",
            )
        else:
            entitled, use_trial, entitlement_message = prepare_download_entitlement(self)
        if not entitled:
            if entitlement_message:
                self._append_log("授权", entitlement_message, "error")
                QMessageBox.warning(self, "无法开始下载", entitlement_message)
            if resume_checkpoint is not None:
                self.task_store.release_lease()
                self._set_running(False)
            return
        if entitlement_message == "已完成软件注册":
            self._refresh_entitlement_status()
            self._append_log("授权", entitlement_message, "success")

        if resume_checkpoint is None:
            if not self.task_store.try_acquire_lease():
                QMessageBox.warning(
                    self,
                    "已有任务正在运行",
                    "另一个 DcmGet 实例正在下载，本窗口不能启动新任务。",
                )
                return
            save_config(self.config_path, self.config)
            try:
                checkpoint = self.task_store.start(
                    self.config,
                    accessions,
                    trial_required=use_trial,
                )
            except TaskStateError as exc:
                self._append_log("恢复", str(exc), "error")
                QMessageBox.critical(
                    self,
                    "无法建立任务恢复点",
                    f"{exc}\n\n为避免意外退出后丢失进度，本次任务没有启动。",
                )
                self.task_store.release_lease()
                return
        else:
            checkpoint = resume_checkpoint
        self.tools = check.tools
        self._active_accessions = accessions
        self._active_task_id = checkpoint.task_id
        self._resume_checkpoint = checkpoint if resume_checkpoint is not None else None
        self._prior_results = checkpoint.results
        self._publish_workspace_summary(
            self._workspace_summary_from_checkpoint(
                checkpoint,
                phase="starting_receiver",
            ),
            select=True,
        )
        self._display_total = len(display_accessions)
        self._progress_offset = len(self._prior_results)
        self._pdi_source_files = []
        self.last_pdi_result = None
        self._accepted_partial_results = False
        self._reset_pdi_status_card()
        self._populate_waiting_rows(display_accessions)
        if self._task_table_summary_mode:
            self._reset_large_batch_summary(
                self._display_total,
                self._prior_results,
            )
        else:
            for result in self._prior_results:
                self._set_result_row(result)
        self.progress_bar.setRange(0, self._display_total)
        self.progress_bar.setValue(self._progress_offset)
        action = "继续下载" if resume_checkpoint is not None else "准备下载"
        self.progress_label.setText(
            f"{action} {self._progress_offset}/{self._display_total}"
        )
        self._set_running(True)
        self._worker_failure_message = None

        thread = QThread(self)
        worker = DownloadWorker(
            self.config,
            check.tools,
            list(self._active_accessions),
            consume_trial_on_ready=use_trial,
            task_store=self.task_store,
            task_id=checkpoint.task_id,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.log.connect(self._append_log)
        worker.state.connect(self._on_worker_state)
        worker.progress.connect(self._on_worker_progress)
        worker.finished.connect(self._on_worker_finished)
        worker.failed.connect(self._on_worker_failed)
        worker.trial_consumed.connect(self._on_trial_consumed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._on_worker_thread_finished)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self.worker_thread = thread
        self.worker = worker
        thread.start()

    def _on_trial_consumed(self, message: str) -> None:
        self._refresh_entitlement_status()
        self._append_log("授权", message, "info")

    def _show_preflight(self, check: PreflightResult) -> None:
        while len(self.preflight_labels) < len(check.checks):
            label = QLabel()
            label.setObjectName("CheckPill")
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            index = len(self.preflight_labels)
            self.preflight_labels.append(label)
            self.preflight_grid.addWidget(label, index // 3, index % 3)
        for index, label in enumerate(self.preflight_labels):
            label.setVisible(index < len(check.checks))
        for label, (name, ok, message) in zip(self.preflight_labels, check.checks):
            label.setText(f"{name}：{message}")
            label.setProperty("status", "ok" if ok else "error")
            label.setToolTip(message)
            label.style().unpolish(label)
            label.style().polish(label)

    def _set_running(
        self,
        running: bool,
        *,
        reset_summary: bool = True,
        can_pause: bool = True,
    ) -> None:
        if running and reset_summary:
            self.last_summary = None
        resume_pending = not running and self._resume_checkpoint is not None
        download_retryable = bool(
            resume_pending
            and self._resume_checkpoint is not None
            and self._resume_checkpoint.phase == "download_retryable"
        )
        pdi_pending = not running and bool(self._pdi_task_id)
        recovery_pending = resume_pending or pdi_pending
        self.preflight_card.setVisible(not running and not recovery_pending)
        self.pdi_status_card.setVisible(
            self.config.pdi_export_enabled and (not running or not can_pause)
        )
        self.start_button.setEnabled(not running and not pdi_pending)
        self.start_button.setText(
            "重试失败项"
            if download_retryable
            else "继续未完成任务"
            if resume_pending
            else "PDI 待重试"
            if pdi_pending
            else "开始下载"
        )
        self.stop_button.setVisible(running)
        self.stop_button.setEnabled(running)
        self.pause_button.setVisible(running)
        self.pause_button.setEnabled(running and can_pause)
        if running:
            self._set_task_form_expanded(False)
        if not running:
            self._pause_requested = False
            self.pause_button.setText("暂停")
        self.discard_resume_button.setText(
            "放弃 PDI 恢复并新建"
            if pdi_pending
            else "放弃失败项并新建"
            if download_retryable
            else "放弃续传并新建"
        )
        self.discard_resume_button.setVisible(recovery_pending)
        self.discard_resume_button.setEnabled(recovery_pending)
        retry_available = bool(
            not running
            and not recovery_pending
            and not self._accepted_partial_results
            and self.last_summary
            and self.last_summary.failed_accessions
        )
        self.retry_button.setVisible(retry_available)
        self.retry_button.setEnabled(retry_available)
        self.accept_partial_button.setVisible(download_retryable)
        self.accept_partial_button.setEnabled(download_retryable)
        can_open_existing_pdi = not running and not recovery_pending
        self.open_existing_pdi_button.setVisible(can_open_existing_pdi)
        self.open_existing_pdi_button.setEnabled(can_open_existing_pdi)
        self.accession_edit.setReadOnly(running or recovery_pending)
        self.destination_edit.setReadOnly(running or pdi_pending)
        self.accession_button.setEnabled(not running and not recovery_pending)
        self.destination_button.setEnabled(not running and not pdi_pending)
        self.quick_pdi_checkbox.setEnabled(not running and not recovery_pending)
        self.quick_pdi_output_button.setEnabled(
            not running
            and not recovery_pending
            and self.quick_pdi_checkbox.isChecked()
        )
        self.settings_button.setEnabled(not running)
        self.registration_button.setEnabled(not running)

    def _on_worker_state(self, state: str) -> None:
        workspace_phase = {
            "partial": "download_retryable",
        }.get(state, state)
        self._update_workspace_phase(workspace_phase)
        labels = {
            "starting_receiver": "正在启动 DICOM 接收器…",
            "downloading": "接收器已就绪，正在下载…",
            "pause_pending": "当前检查号完成后暂停…",
            "paused": "任务已暂停，接收器保持监听",
            "stopping": "正在停止后台进程…",
            "completed": "任务已完成",
            "partial": "任务完成，部分检查号失败",
            "cancelled": "任务已取消",
        }
        self.progress_label.setText(labels.get(state, state))
        if state == "pause_pending" and self._pause_requested:
            self.pause_button.setText("取消暂停")
        elif state == "paused" and self._pause_requested:
            self.pause_button.setText("继续下载")
        if state in {"starting_receiver", "stopping"}:
            self.progress_bar.setRange(0, 0)
        elif state == "downloading" and self.progress_bar.maximum() == 0:
            self.progress_bar.setRange(0, 100)

    def _on_worker_progress(self, index: int, total: int, result: AccessionResult) -> None:
        display_total = self._display_total or total
        display_index = self._progress_offset + index
        self.progress_bar.setRange(0, display_total)
        completed = self._progress_offset + (
            index - 1 if result.status == AccessionStatus.DOWNLOADING else index
        )
        self.progress_bar.setValue(completed)
        speed = format_transfer_rate(result.speed_bytes_per_second)
        speed_text = f" · {speed}" if speed != "—" else ""
        pause_text = " · 当前项完成后暂停" if self._pause_requested else ""
        self.progress_label.setText(
            f"{display_index}/{display_total} · {result.accession} · {result.status.value} · "
            f"{result.file_count} 个文件{speed_text}{pause_text}"
        )
        task_id = self._active_task_id
        workspace_summary = self._workspace_task_summaries.get(task_id)
        if workspace_summary is not None:
            final_statuses = {
                AccessionStatus.COMPLETED,
                AccessionStatus.NO_DATA,
                AccessionStatus.PARTIAL,
                AccessionStatus.FAILED,
            }
            is_final = result.status in final_statuses
            processed_count = (
                max(workspace_summary.processed_count, completed)
                if is_final
                else workspace_summary.processed_count
            )
            completed_count = workspace_summary.completed_count + int(
                is_final
                and result.status
                in {AccessionStatus.COMPLETED, AccessionStatus.NO_DATA}
            )
            failed_count = workspace_summary.failed_count + int(
                is_final
                and result.status
                in {AccessionStatus.PARTIAL, AccessionStatus.FAILED}
            )
            self._publish_workspace_summary(
                replace(
                    workspace_summary,
                    phase="downloading",
                    total_count=display_total,
                    processed_count=processed_count,
                    pending_count=max(0, display_total - processed_count),
                    completed_count=completed_count,
                    failed_count=failed_count,
                    file_count=(
                        workspace_summary.file_count + result.file_count
                        if is_final
                        else workspace_summary.file_count
                    ),
                    received_bytes=(
                        workspace_summary.received_bytes + result.received_bytes
                        if is_final
                        else workspace_summary.received_bytes
                    ),
                    speed_bytes_per_second=result.speed_bytes_per_second,
                    current_accession=result.accession,
                    updated_at=self._workspace_timestamp(),
                )
            )
        if self._task_table_summary_mode:
            self._record_large_batch_result(
                result,
                total=display_total,
            )
        else:
            self._set_result_row(result)

    def _set_result_row(self, result: AccessionResult, row: int | None = None) -> None:
        if self._task_table_summary_mode:
            return
        target_row = self.row_by_accession.get(result.accession) if row is None else row
        if target_row is None:
            return
        status_item = QTableWidgetItem(result.status.value)
        color = {
            AccessionStatus.COMPLETED: COLORS["success"],
            AccessionStatus.NO_DATA: COLORS["warning"],
            AccessionStatus.PARTIAL: COLORS["warning"],
            AccessionStatus.FAILED: COLORS["danger"],
            AccessionStatus.CANCELLED: COLORS["muted"],
            AccessionStatus.DOWNLOADING: COLORS["primary"],
        }.get(result.status, COLORS["muted"])
        status_item.setForeground(QColor(color))
        icon = {
            AccessionStatus.WAITING: QStyle.SP_FileDialogInfoView,
            AccessionStatus.DOWNLOADING: QStyle.SP_BrowserReload,
            AccessionStatus.COMPLETED: QStyle.SP_DialogApplyButton,
            AccessionStatus.NO_DATA: QStyle.SP_MessageBoxWarning,
            AccessionStatus.PARTIAL: QStyle.SP_MessageBoxWarning,
            AccessionStatus.FAILED: QStyle.SP_MessageBoxCritical,
            AccessionStatus.CANCELLED: QStyle.SP_DialogCancelButton,
        }.get(result.status)
        if icon is not None:
            status_item.setIcon(self.style().standardIcon(icon))
        status_item.setData(Qt.UserRole, result.output_directory)
        self.task_table.setItem(target_row, 1, status_item)
        self.task_table.setItem(target_row, 2, QTableWidgetItem(str(result.file_count)))
        self.task_table.setItem(
            target_row,
            3,
            QTableWidgetItem(format_transfer_rate(result.speed_bytes_per_second)),
        )
        duration = f"{result.duration_seconds:.1f}s" if result.duration_seconds else "—"
        self.task_table.setItem(target_row, 4, QTableWidgetItem(duration))
        self.task_table.setItem(target_row, 5, QTableWidgetItem(result.message or "等待处理"))

    def _on_worker_finished(self, summary: BatchSummary) -> None:
        task_id = self._active_task_id
        checkpoint: TaskCheckpoint | None = None
        active_checkpoint: TaskCheckpoint | None = None
        pdi_has_files = any(result.archived_files for result in summary.results)
        try:
            checkpoint = self.task_store.load(
                include_archived_files=not self._task_table_summary_mode
            )
            if checkpoint is not None and checkpoint.task_id == task_id:
                active_checkpoint = checkpoint
                pdi_has_files = pdi_has_files or any(
                    result.file_count > 0
                    for result in [
                        *checkpoint.results,
                        *checkpoint.partial_results.values(),
                    ]
                )
                summary = merge_checkpoint_summary(checkpoint, summary)
                if not summary.cancelled:
                    if summary.exit_code == 2:
                        self.task_store.set_phase(task_id, "download_retryable")
                        active_checkpoint.phase = "download_retryable"
                    elif self.config.pdi_export_enabled and pdi_has_files:
                        self.task_store.set_phase(task_id, "pdi_pending")
                        active_checkpoint.phase = "pdi_pending"
                    else:
                        self.task_store.clear(task_id)
        except TaskStateError as exc:
            self._append_log("恢复", str(exc), "error")
        self.last_summary = summary
        if self._task_table_summary_mode:
            self._reset_large_batch_summary(
                self._display_total or len(summary.results),
                (
                    [
                        *active_checkpoint.results,
                        *active_checkpoint.partial_results.values(),
                    ]
                    if active_checkpoint is not None
                    else [
                        result
                        for result in summary.results
                        if result.status != AccessionStatus.CANCELLED
                    ]
                ),
            )
        download_retryable = bool(not summary.cancelled and summary.exit_code == 2)
        self._resume_checkpoint = (
            active_checkpoint if summary.cancelled or download_retryable else None
        )
        self._prior_results = []
        self._active_task_id = ""
        pdi_should_run = bool(
            self.config.pdi_export_enabled
            and not summary.cancelled
            and summary.exit_code == 0
            and pdi_has_files
        )
        workspace_phase = (
            "cancelled"
            if summary.cancelled
            else "download_retryable"
            if download_retryable
            else "pdi_pending"
            if pdi_should_run
            else "completed"
        )
        self._publish_workspace_summary(
            self._workspace_summary_from_batch(
                task_id,
                summary,
                phase=workspace_phase,
            )
        )
        keep_lease_for_pdi = pdi_should_run and active_checkpoint is not None
        self._pdi_task_id = task_id if keep_lease_for_pdi else ""
        if not keep_lease_for_pdi:
            self.task_store.release_lease()
        if self.worker_thread is None:
            self._finish_worker(set_idle=False)
            self._complete_worker_finished()

    def _complete_worker_finished(self) -> None:
        summary = self.last_summary
        if summary is None:
            self._set_running(False)
            return
        if self._closing_after_cancel:
            self._set_running(False)
            return
        if (
            self.config.pdi_export_enabled
            and not summary.cancelled
            and summary.exit_code == 0
        ):
            if summary.archived_files:
                self._start_pdi_export(summary.archived_files)
                return
            if self._pdi_task_id:
                self._start_pdi_export()
                return
            pdi_notice = "本批没有可导出的 DICOM 文件，已跳过 PDI。"
            self._show_pdi_skipped(pdi_notice)
            self._set_running(False)
            self._show_download_completion(pdi_notice)
            return
        self._set_running(False)
        self._show_download_completion()

    def _show_download_completion(
        self, pdi_message: str = "", *, pdi_problem: bool = False
    ) -> None:
        summary = self.last_summary
        if summary is None:
            return
        if summary.cancelled:
            title, message = "任务已取消", "已停止下载，已收到的 DICOM 文件已保留。"
        elif summary.exit_code == 2 and not self._accepted_partial_results:
            title = "任务部分完成"
            message = f"有 {len(summary.failed_accessions)} 个检查号失败，可点击“重试失败项”。"
        elif summary.exit_code == 2:
            title = "已接受当前结果"
            message = "失败项已不再作为必须续传任务，已收到的 DICOM 文件已保留。"
        else:
            title = "任务部分完成" if pdi_problem else "下载完成"
            message = "所有检查号均已处理完成。"
        if pdi_message:
            message += "\n\n" + pdi_message
        QMessageBox.information(self, title, message)

    def _start_pdi_export(self, files: list[str] | None = None) -> None:
        if self._is_busy():
            return
        source_files = list(files if files is not None else self._pdi_source_files)
        self._pending_pdi_completion = None
        reuse_published = self._pdi_reuse_published
        self._pdi_reuse_published = False
        pdi_attempt_id = ""
        if self._pdi_task_id:
            if not self.task_store.try_acquire_lease():
                self._show_pdi_skipped("该 PDI 恢复任务正在另一个 DcmGet 实例中运行。")
                return
            try:
                if not source_files:
                    source_files = self.task_store.load_archived_files(
                        self._pdi_task_id
                    )
                if not source_files:
                    self.task_store.release_lease()
                    self._show_pdi_skipped("没有可重试的 DICOM 文件。")
                    return
                pdi_attempt_id, reuse_published = self.task_store.begin_pdi_attempt(
                    self._pdi_task_id,
                    reuse_existing=reuse_published,
                )
            except TaskStateError as exc:
                self.task_store.release_lease()
                self._show_pdi_skipped(str(exc))
                return
        elif not source_files:
            self._show_pdi_skipped("没有可重试的 DICOM 文件。")
            return
        self._pdi_source_files = source_files
        if self.tools is None:
            try:
                self.tools = self.resolver.resolve(self.config.dcmtk_bin_dir)
            except Exception as exc:
                self._on_pdi_failed(str(exc))
                return
        self.last_pdi_result = None
        self.pdi_status_card.show()
        self._set_pdi_status("准备生成 PDI 便携阅片目录…", "pending")
        self.pdi_progress_bar.setRange(0, 0)
        self.pdi_view_button.setEnabled(False)
        self.pdi_open_button.setEnabled(False)
        self.pdi_retry_button.setEnabled(False)
        self._set_running(True, reset_summary=False, can_pause=False)
        self.progress_label.setText("下载已完成，正在生成 PDI…")
        self._update_workspace_phase("pdi_running")

        thread = QThread(self)
        worker = PdiWorker(
            self.config,
            self.tools,
            source_files,
            self.project_root,
            self.task_store if self._pdi_task_id else None,
            self._pdi_task_id,
            pdi_attempt_id,
            reuse_published,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.log.connect(self._append_log)
        worker.progress.connect(self._on_pdi_progress)
        worker.finished.connect(self._on_pdi_finished)
        worker.failed.connect(self._on_pdi_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._on_pdi_thread_finished)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self.pdi_thread = thread
        self.pdi_worker = worker
        thread.start()

    def _on_pdi_progress(
        self,
        stage: object,
        current: int,
        total: int,
        message: str,
    ) -> None:
        stage_text = str(getattr(stage, "value", stage))
        detail = f"{stage_text}：{message}" if message else stage_text
        self._set_pdi_status(detail, "generating")
        if total > 0:
            self.pdi_progress_bar.setRange(0, total)
            self.pdi_progress_bar.setValue(max(0, min(current, total)))
        else:
            self.pdi_progress_bar.setRange(0, 0)

    def _on_pdi_finished(self, result: object) -> None:
        self.last_pdi_result = result
        status_text = str(getattr(getattr(result, "status", ""), "value", ""))
        message = str(getattr(result, "message", "") or status_text or "PDI 导出已结束")
        warnings = list(getattr(result, "warnings", []) or [])
        output_directory = str(getattr(result, "output_directory", "") or "")
        if status_text == "完成" and output_directory:
            message = f"PDI 便携阅片目录已生成：{Path(output_directory).name}"
        if warnings:
            message += f" 共 {len(warnings)} 条警告，详见运行日志。"

        ui_status = {
            "完成": "ok",
            "部分成功": "warning",
            "失败": "error",
            "已取消": "warning",
        }.get(status_text, "warning")
        self._set_pdi_status(message, ui_status)
        self.progress_label.setText(
            {
                "完成": "下载与 PDI 导出已完成",
                "部分成功": "下载已完成，PDI 部分成功",
                "失败": "下载已完成，PDI 导出失败",
                "已取消": "下载已完成，PDI 导出已取消",
            }.get(status_text, "PDI 导出已结束")
        )
        self._update_workspace_phase(
            "completed"
            if status_text == "完成"
            else "cancelled"
            if status_text == "已取消"
            else "pdi_retryable"
        )
        self.pdi_progress_bar.setRange(0, 1)
        self.pdi_progress_bar.setValue(1 if status_text in {"完成", "部分成功"} else 0)
        output_exists = bool(output_directory and Path(output_directory).exists())
        self.pdi_open_button.setEnabled(output_exists)
        self.pdi_view_button.setEnabled(
            bool(output_exists and pdi_viewer_command(output_directory))
        )
        self.pdi_retry_button.setEnabled(False)
        self._save_pdi_checkpoint_status(completed=status_text == "完成")

        if status_text == "完成":
            pdi_message = f"PDI 便携阅片目录已生成：{output_directory}"
        elif status_text == "部分成功":
            pdi_message = f"PDI 已生成，但离线阅片器有部分未完成。\n{message}"
        elif status_text == "已取消":
            pdi_message = "PDI 生成已取消，下载的 DICOM 文件仍已保留。"
        else:
            pdi_message = f"PDI 导出未完成：{message}"
        self._pending_pdi_completion = (
            pdi_message,
            status_text != "完成",
            bool(self._pdi_source_files) and status_text != "完成",
        )
        self._finish_pdi_worker()

    def _on_pdi_failed(self, message: str) -> None:
        self._append_log("PDI", message, "error")
        self._update_workspace_phase("pdi_retryable", error_message=message)
        self.progress_label.setText("下载已完成，PDI 导出失败")
        self._set_pdi_status(f"PDI 导出失败：{message}", "error")
        self.pdi_progress_bar.setRange(0, 1)
        self.pdi_progress_bar.setValue(0)
        self.pdi_view_button.setEnabled(False)
        self.pdi_open_button.setEnabled(False)
        self.pdi_retry_button.setEnabled(False)
        self._save_pdi_checkpoint_status(completed=False)
        self._pending_pdi_completion = (
            f"PDI 导出失败：{message}",
            True,
            bool(self._pdi_source_files),
        )
        self._finish_pdi_worker()

    def _save_pdi_checkpoint_status(self, *, completed: bool) -> None:
        task_id = self._pdi_task_id
        if not task_id:
            if completed:
                self._pdi_source_files = []
            return
        try:
            if completed:
                self.task_store.clear(task_id)
            else:
                self.task_store.set_phase(task_id, "pdi_retryable")
        except TaskStateError as exc:
            self._append_log("恢复", str(exc), "error")
        finally:
            self.task_store.release_lease()
        if completed:
            self._pdi_task_id = ""
            self._pdi_source_files = []

    def _finish_pdi_worker(self) -> None:
        if self.pdi_thread is None:
            self.pdi_worker = None
            self._set_running(False)
            self._complete_pdi_worker()

    def _on_pdi_thread_finished(self) -> None:
        if self.sender() is not self.pdi_thread:
            return
        self.pdi_worker = None
        self.pdi_thread = None
        self._set_running(False)
        self._complete_pdi_worker()

    def _complete_pdi_worker(self) -> None:
        completion = self._pending_pdi_completion
        self._pending_pdi_completion = None
        if completion is None:
            return
        message, problem, retry_enabled = completion
        self.pdi_retry_button.setEnabled(retry_enabled)
        if self._closing_after_cancel:
            return
        self._show_download_completion(message, pdi_problem=problem)

    def _set_pdi_status(self, text: str, status: str) -> None:
        self.pdi_status_label.setText(text)
        self.pdi_status_label.setProperty("status", status)
        self.pdi_status_label.style().unpolish(self.pdi_status_label)
        self.pdi_status_label.style().polish(self.pdi_status_label)

    def _show_pdi_skipped(self, message: str) -> None:
        self.pdi_status_card.setVisible(self.config.pdi_export_enabled)
        self._set_pdi_status(message, "warning")
        self.pdi_progress_bar.setRange(0, 1)
        self.pdi_progress_bar.setValue(0)
        self.pdi_view_button.setEnabled(False)
        self.pdi_open_button.setEnabled(False)
        self.pdi_retry_button.setEnabled(False)

    def _reset_pdi_status_card(self) -> None:
        self.pdi_status_card.setVisible(self.config.pdi_export_enabled)
        self._set_pdi_status("下载完成后自动生成", "pending")
        self.pdi_progress_bar.setRange(0, 100)
        self.pdi_progress_bar.setValue(0)
        self.pdi_view_button.setEnabled(False)
        self.pdi_open_button.setEnabled(False)
        self.pdi_retry_button.setEnabled(False)

    def _pdi_output_directory(self) -> Path | None:
        directory = str(
            getattr(self.last_pdi_result, "output_directory", "") or ""
        )
        if not directory:
            return None
        path = Path(directory).expanduser()
        return path.resolve() if path.is_dir() else None

    def _open_pdi_viewer(self) -> None:
        root = self._pdi_output_directory()
        if root is None:
            QMessageBox.warning(
                self,
                "无法启动阅片器",
                "当前 PDI 目录没有可用的离线阅片启动器，请重试 PDI 导出。\n\n"
                f"诊断日志：{diagnostic_log_directory()}",
            )
            return
        self._launch_pdi_viewer(root)

    def _choose_existing_pdi_directory(self) -> None:
        initial_directory = self.settings_store.value(
            PDI_LAST_OPEN_DIRECTORY_KEY,
            "",
            type=str,
        ).strip()
        if not initial_directory:
            initial_directory = (
                self.config.pdi_output_folder.strip()
                or self.destination_edit.text().strip()
            )
        selected = QFileDialog.getExistingDirectory(
            self,
            "打开已有 PDI 目录",
            initial_directory,
        )
        if not selected:
            return
        root = Path(selected).expanduser().resolve()
        if pdi_viewer_command(root) is None:
            QMessageBox.warning(
                self,
                "无法打开 PDI 目录",
                "所选目录不是可用的 PDI 根目录，或程序内置阅片资源不完整。"
                "请选择包含 DICOM 数据和有效阅片索引的完整 PDI 根目录。",
            )
            return
        self.settings_store.setValue(PDI_LAST_OPEN_DIRECTORY_KEY, str(root))
        self.settings_store.sync()
        self._launch_pdi_viewer(root)

    def _launch_pdi_viewer(self, root: Path) -> None:
        root = root.expanduser().resolve()
        command = pdi_viewer_command(root)
        if command is None:
            QMessageBox.warning(
                self,
                "无法打开 PDI 目录",
                "程序内置离线阅片资源不可用，或 PDI 目录不完整，请检查后重试。",
            )
            return
        if self._pdi_viewer_process is not None:
            if (
                self._pdi_viewer_process.state() != QProcess.NotRunning
                and self._pdi_viewer_root == root
            ):
                self._capture_pdi_viewer_url()
                if self._pdi_viewer_ready and self._pdi_viewer_url:
                    QDesktopServices.openUrl(QUrl(self._pdi_viewer_url))
                else:
                    self._append_log(
                        "PDI",
                        "离线阅片服务正在启动，请稍后再点击“打开影像”",
                        "info",
                    )
                return
            self._stop_pdi_viewer_process()
        program, arguments = command
        viewer_url = ""
        controls_browser = "--root" in arguments
        if controls_browser:
            from .pdi_server import generate_session_token

            session_token = generate_session_token()
            port = 0
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = int(listener.getsockname()[1])
            except OSError as exc:
                record_exception("DcmGetWindow._open_pdi_viewer.port", exc)
            arguments = [
                *arguments,
                "--session-token",
                session_token,
                "--port",
                str(port),
                "--no-browser",
            ]
            if port:
                from .pdi_server import viewer_url as build_viewer_url

                viewer_url = build_viewer_url(port, session_token)
        process = QProcess(self)
        process.setWorkingDirectory(str(root))
        process.readyReadStandardOutput.connect(
            lambda current=process: self._capture_pdi_viewer_url(current)
        )
        process.finished.connect(
            lambda exit_code, exit_status, current=process: self._on_pdi_viewer_finished(
                current, exit_code, exit_status
            )
        )
        process.errorOccurred.connect(
            lambda error, current=process: self._on_pdi_viewer_error(
                current, error
            )
        )
        self._pdi_viewer_process = process
        self._pdi_viewer_root = root
        self._pdi_viewer_url = ""
        self._pdi_viewer_probe_url = ""
        self._pdi_viewer_ready = False
        self._pdi_viewer_open_when_ready = controls_browser
        self._set_pdi_viewer_starting_feedback()
        if viewer_url:
            self._set_pdi_viewer_url(viewer_url)
        try:
            process.start(program, arguments)
        except (OSError, TypeError) as exc:
            record_exception("DcmGetWindow._open_pdi_viewer", exc)
            self._fail_pdi_viewer_start("离线阅片服务无法启动。")
            return
        if process is not self._pdi_viewer_process:
            return
        self._pdi_viewer_probe_timer.start()
        self._pdi_viewer_timeout_timer.start(PDI_VIEWER_START_TIMEOUT_MS)
        self._probe_pdi_viewer_ready()
        self._capture_pdi_viewer_url()

    def _set_pdi_viewer_starting_feedback(self) -> None:
        self._pdi_viewer_button_states = (
            self.pdi_view_button.isEnabled(),
            self.open_existing_pdi_button.isEnabled(),
        )
        self.pdi_view_button.setText("正在启动…")
        self.pdi_view_button.setEnabled(False)
        self.open_existing_pdi_button.setText("阅片器启动中…")
        self.open_existing_pdi_button.setEnabled(False)
        if not self._is_busy():
            self.progress_label.setText("正在启动本地离线阅片服务…")
            if self.pdi_status_card.isVisible():
                self._set_pdi_status("正在启动本地离线阅片服务…", "generating")

    def _restore_pdi_viewer_feedback(self, message: str, status: str) -> None:
        pdi_enabled, existing_enabled = self._pdi_viewer_button_states
        actions_available = not self._is_busy()
        self.pdi_view_button.setText("打开影像")
        self.pdi_view_button.setEnabled(pdi_enabled and actions_available)
        self.open_existing_pdi_button.setText("打开已有 PDI 目录")
        self.open_existing_pdi_button.setEnabled(
            existing_enabled and actions_available
        )
        if actions_available:
            self.progress_label.setText(message)
            if self.pdi_status_card.isVisible():
                self._set_pdi_status(message, status)

    def _set_pdi_viewer_url(self, url: str) -> None:
        candidate = QUrl(url)
        port = candidate.port()
        if (
            candidate.scheme() != "http"
            or candidate.host() != "127.0.0.1"
            or port <= 0
        ):
            return
        path = candidate.path()
        token_prefix = "/open/"
        if path.startswith(token_prefix):
            session_token = path[len(token_prefix) :]
            if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", session_token):
                return
            probe_url = (
                f"http://127.0.0.1:{port}/ready/{session_token}"
            )
        elif path in {"/viewer/directory/", "/viewer/dicomjson/"}:
            probe_url = f"http://127.0.0.1:{port}/api/studies"
        else:
            return
        self._pdi_viewer_url = candidate.toString()
        self._pdi_viewer_probe_url = probe_url

    def _capture_pdi_viewer_url(self, process: QProcess | None = None) -> None:
        current = process or self._pdi_viewer_process
        if current is None or current is not self._pdi_viewer_process:
            return
        try:
            output = bytes(current.readAllStandardOutput()).decode(
                "utf-8", errors="replace"
            )
        except (AttributeError, TypeError):
            return
        match = re.search(r"http://127\.0\.0\.1:\d+/[^\s]+", output)
        if match:
            self._set_pdi_viewer_url(match.group(0))
            self._probe_pdi_viewer_ready()

    def _probe_pdi_viewer_ready(self) -> None:
        if (
            self._pdi_viewer_process is None
            or self._pdi_viewer_ready
            or not self._pdi_viewer_probe_url
            or self._pdi_viewer_probe_reply is not None
        ):
            return
        reply = self._pdi_viewer_network.head(
            QNetworkRequest(QUrl(self._pdi_viewer_probe_url))
        )
        self._pdi_viewer_probe_reply = reply
        reply.finished.connect(
            lambda current=reply: self._on_pdi_viewer_probe_finished(current)
        )

    def _on_pdi_viewer_probe_finished(self, reply: QNetworkReply) -> None:
        if reply is not self._pdi_viewer_probe_reply:
            reply.deleteLater()
            return
        self._pdi_viewer_probe_reply = None
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        reply.deleteLater()
        if self._pdi_viewer_process is None or self._pdi_viewer_ready:
            return
        if int(status or 0) != 200:
            return
        self._pdi_viewer_ready = True
        self._pdi_viewer_probe_timer.stop()
        self._pdi_viewer_timeout_timer.stop()
        self._append_log("PDI", "离线阅片服务已就绪", "success")
        self._restore_pdi_viewer_feedback(
            "离线阅片服务已就绪，正在打开影像",
            "ok",
        )
        if self._pdi_viewer_open_when_ready and self._pdi_viewer_url:
            self._pdi_viewer_open_when_ready = False
            QDesktopServices.openUrl(QUrl(self._pdi_viewer_url))

    def _on_pdi_viewer_finished(
        self,
        process: QProcess,
        exit_code: int,
        _exit_status: object,
    ) -> None:
        if process is not self._pdi_viewer_process:
            return
        self._capture_pdi_viewer_url(process)
        if not self._pdi_viewer_ready:
            self._fail_pdi_viewer_start(
                f"离线阅片服务在就绪前退出（退出码 {exit_code}）。"
            )
            return
        self._append_log("PDI", "离线阅片服务已退出", "info")
        self._stop_pdi_viewer_process()
        self._restore_pdi_viewer_feedback(
            "离线阅片服务已退出，可重新打开",
            "warning",
        )

    def _on_pdi_viewer_error(self, process: QProcess, _error: object) -> None:
        if process is not self._pdi_viewer_process:
            return
        if self._pdi_viewer_ready:
            self._append_log("PDI", f"离线阅片服务异常：{process.errorString()}", "error")
            return
        self._fail_pdi_viewer_start(
            f"离线阅片服务启动失败：{process.errorString()}"
        )

    def _on_pdi_viewer_start_timeout(self) -> None:
        if self._pdi_viewer_process is None or self._pdi_viewer_ready:
            return
        self._fail_pdi_viewer_start(
            f"离线阅片服务在 {PDI_VIEWER_START_TIMEOUT_MS // 1000} 秒内未就绪。"
        )

    def _fail_pdi_viewer_start(self, message: str) -> None:
        if self._pdi_viewer_process is None:
            return
        self._stop_pdi_viewer_process()
        self._restore_pdi_viewer_feedback(
            "离线阅片服务启动失败，可重试",
            "error",
        )
        QMessageBox.critical(
            self,
            "阅片器启动失败",
            f"{message}\n\n请查看诊断日志：{diagnostic_log_directory()}",
        )

    def _cancel_pdi_viewer_probe(self) -> None:
        self._pdi_viewer_probe_timer.stop()
        self._pdi_viewer_timeout_timer.stop()
        reply = self._pdi_viewer_probe_reply
        self._pdi_viewer_probe_reply = None
        if reply is not None:
            reply.abort()
            reply.deleteLater()

    def _stop_pdi_viewer_process(self) -> None:
        process = self._pdi_viewer_process
        self._pdi_viewer_process = None
        self._pdi_viewer_root = None
        self._pdi_viewer_url = ""
        self._pdi_viewer_probe_url = ""
        self._pdi_viewer_ready = False
        self._pdi_viewer_open_when_ready = False
        self._cancel_pdi_viewer_probe()
        if process is None:
            return
        if process.state() != QProcess.NotRunning:
            process.terminate()
            if not process.waitForFinished(1000):
                process.kill()
                process.waitForFinished(1000)
        process.deleteLater()

    def _open_pdi_directory(self) -> None:
        path = self._pdi_output_directory()
        if path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _retry_pdi(self) -> None:
        if (
            self.multi_task_enabled
            and self.task_controller is not None
            and self._selected_task_id
        ):
            try:
                self.task_controller.retry_pdi(self._selected_task_id)
            except TaskStateError as exc:
                QMessageBox.warning(self, "无法重试 PDI", str(exc))
            return
        if self._pdi_source_files or self._pdi_task_id:
            self._start_pdi_export()

    def _on_worker_failed(self, message: str) -> None:
        self._worker_failure_message = message
        self._append_log("应用", message, "error")
        self._update_workspace_phase("failed", error_message=message)
        try:
            checkpoint = self.task_store.load(include_archived_files=False)
            if checkpoint is not None and checkpoint.task_id == self._active_task_id:
                self._resume_checkpoint = checkpoint
        except TaskStateError as exc:
            self._append_log("恢复", str(exc), "error")
        self.progress_label.setText("任务中断，可点击开始或在下次启动时继续")
        self.task_store.release_lease()
        if self.worker_thread is None:
            self._finish_worker(set_idle=False)
            self._complete_worker_failed(message)

    def _complete_worker_failed(self, message: str) -> None:
        self._set_running(False)
        if self._closing_after_cancel:
            return
        QMessageBox.critical(
            self,
            "下载中断",
            f"{message}\n\n已完成项已保存；重新启动 DcmGet 后可继续剩余任务。",
        )

    def _delete_multi_task(self, task_id: str) -> None:
        controller = self.task_controller
        if not self.multi_task_enabled or controller is None or not task_id:
            return
        try:
            tasks_before = controller.list_tasks()
            summary = next(item for item in tasks_before if item.task_id == task_id)
        except (StopIteration, TaskStateError):
            QMessageBox.warning(self, "无法删除任务", "任务记录不存在或已经被删除。")
            return
        if summary.phase not in DELETABLE_TASK_PHASES:
            QMessageBox.warning(
                self,
                "无法删除任务",
                "运行、排队、暂停、取消中或正在生成 PDI 的任务不能删除。",
            )
            return
        answer = QMessageBox.question(
            self,
            "删除任务记录",
            (
                "确定从任务列表删除这个任务吗？\n\n"
                "仅删除任务记录和进度，不会删除已下载的 DICOM、PDI、"
                "日志或隔离文件。\n\n此操作无法撤销。"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        ordered_ids = [item.task_id for item in tasks_before]
        deleted_row = ordered_ids.index(task_id)
        try:
            controller.delete_task(task_id)
        except TaskStateError as exc:
            QMessageBox.warning(self, "无法删除任务", str(exc))
            return
        self._workspace_task_summaries.pop(task_id, None)
        self._last_pdi_results.pop(task_id, None)
        self.task_workspace.remove_task(task_id)
        remaining = controller.list_tasks()
        if remaining:
            adjacent = remaining[min(deleted_row, len(remaining) - 1)]
            self._on_multi_tasks_updated(remaining)
            self.task_workspace.select_task(adjacent.task_id)
            self._on_workspace_task_selected(adjacent.task_id)
            return
        self._on_multi_tasks_updated([])
        self._show_new_task_editor()

    def _on_worker_thread_finished(self) -> None:
        if self.sender() is not self.worker_thread:
            return
        failure_message = self._worker_failure_message
        self._worker_failure_message = None
        self._finish_worker(set_idle=False)
        if failure_message is not None:
            self._complete_worker_failed(failure_message)
        elif self.last_summary is not None:
            self._complete_worker_finished()
        else:
            self._set_running(False)

    def _finish_worker(self, *, set_idle: bool = True) -> None:
        self.worker = None
        self.worker_thread = None
        self.progress_bar.setRange(
            0,
            max(1, self._display_total or len(self._active_accessions)),
        )
        if set_idle:
            self._set_running(False)

    def _stop_download(self) -> None:
        if (
            self.multi_task_enabled
            and self.task_controller is not None
            and self._selected_task_id
        ):
            try:
                summary = self.task_controller.manager.get_task_detail(
                    self._selected_task_id,
                    accession_limit=1,
                ).summary
            except TaskStateError as exc:
                QMessageBox.warning(self, "无法停止任务", str(exc))
                return
            answer = QMessageBox.question(
                self,
                "停止任务",
                "确定停止这个任务吗？已收到的文件和恢复点都会保留。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
            try:
                if summary.phase in {"pdi_pending", "pdi_running"}:
                    self.task_controller.cancel_pdi(self._selected_task_id)
                else:
                    self.task_controller.cancel_task(self._selected_task_id)
            except TaskStateError as exc:
                QMessageBox.warning(self, "无法停止任务", str(exc))
            return
        if self.pdi_worker is not None:
            answer = QMessageBox.question(
                self,
                "取消 PDI 生成",
                "确定取消当前 PDI 导出吗？已下载的 DICOM 文件不会删除。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer == QMessageBox.Yes:
                self.stop_button.setEnabled(False)
                self.progress_label.setText("正在取消 PDI 生成…")
                self._set_pdi_status("正在取消…", "warning")
                self.pdi_worker.request_cancel()
            return
        if not self.worker:
            return
        answer = QMessageBox.question(
            self,
            "停止下载",
            "确定停止当前任务吗？已收到的文件会保留。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.stop_button.setEnabled(False)
            self.pause_button.setEnabled(False)
            self.progress_label.setText("正在停止…")
            self.worker.request_cancel()

    def _toggle_pause(self) -> None:
        if (
            self.multi_task_enabled
            and self.task_controller is not None
            and self._selected_task_id
        ):
            try:
                summary = self.task_controller.manager.get_task_detail(
                    self._selected_task_id,
                    accession_limit=1,
                ).summary
                if summary.phase == "paused":
                    self.task_controller.resume_task(self._selected_task_id)
                else:
                    self.task_controller.pause_task(self._selected_task_id)
            except TaskStateError as exc:
                QMessageBox.warning(self, "无法更新任务", str(exc))
            return
        if not self.worker:
            return
        if self._pause_requested:
            self._pause_requested = False
            self.pause_button.setText("暂停")
            self.progress_label.setText("正在继续下载…")
            self.worker.request_resume()
        else:
            self._pause_requested = True
            self.pause_button.setText("取消暂停")
            self.progress_label.setText("当前检查号完成后暂停…")
            self.worker.request_pause()

    def _discard_resume_task(self) -> None:
        checkpoint = self._resume_checkpoint
        task_id = checkpoint.task_id if checkpoint is not None else self._pdi_task_id
        if self._is_busy() or not task_id:
            return
        answer = QMessageBox.question(
            self,
            "放弃恢复任务",
            "确定放弃恢复记录并新建任务吗？已经下载的 DICOM 和 PDI 文件不会删除。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            if not self.task_store.lease_held and not self.task_store.try_acquire_lease():
                QMessageBox.warning(self, "任务正在运行", "另一个 DcmGet 实例正在使用该任务。")
                return
            stored = self.task_store.load_required()
            if stored.task_id != task_id:
                raise TaskStateError("活动任务已改变，拒绝放弃旧任务")
            if not self._cleanup_pdi_partial(stored):
                return
            self.task_store.clear(task_id)
        except TaskStateError as exc:
            QMessageBox.warning(self, "无法放弃任务", str(exc))
            return
        finally:
            self.task_store.release_lease()
        self._workspace_task_summaries.pop(task_id, None)
        self.task_workspace.remove_task(task_id)
        self._resume_checkpoint = None
        self._pdi_task_id = ""
        self._pdi_reuse_published = False
        self._pdi_source_files = []
        self.last_summary = None
        self._accepted_partial_results = False
        self.pdi_retry_button.setEnabled(False)
        self._active_task_id = ""
        self._hidden_accession_count = 0
        self.accession_edit.setPlaceholderText("每行一个检查号")
        self.progress_label.setText("已放弃续传记录，可以新建任务")
        self._set_running(False)
        self._set_task_form_expanded(True)

    def _retry_failed(self) -> None:
        if (
            self.multi_task_enabled
            and self.task_controller is not None
            and self._selected_task_id
        ):
            try:
                self.task_controller.retry_task(self._selected_task_id)
            except TaskStateError as exc:
                QMessageBox.warning(self, "无法重试任务", str(exc))
            return
        if self._accepted_partial_results:
            return
        if (
            self._resume_checkpoint is not None
            and self._resume_checkpoint.phase == "download_retryable"
        ):
            self._start_download(resume_checkpoint=self._resume_checkpoint)
            return
        if self.last_summary:
            failed = self.last_summary.failed_accessions
            if failed:
                self._start_download(failed)

    def _accept_partial_results(self) -> None:
        checkpoint = self._resume_checkpoint
        if (
            self._is_busy()
            or checkpoint is None
            or checkpoint.phase != "download_retryable"
        ):
            return
        failed_count = sum(
            result.status in {AccessionStatus.FAILED, AccessionStatus.PARTIAL}
            for result in checkpoint.results
        )
        answer = QMessageBox.question(
            self,
            "接受当前下载结果",
            (
                f"确定不再重试这 {failed_count:,} 个失败或部分成功的检查号吗？\n\n"
                "已收到的 DICOM 文件会保留；如果已启用 PDI，将直接使用现有文件继续导出。"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        if not self.task_store.try_acquire_lease():
            QMessageBox.warning(self, "任务正在运行", "另一个 DcmGet 实例正在使用该任务。")
            return
        try:
            stored = self.task_store.load_required(include_archived_files=False)
            if (
                stored.task_id != checkpoint.task_id
                or stored.phase != "download_retryable"
            ):
                raise TaskStateError("活动任务已改变，拒绝接受旧任务结果")
            source_files = (
                self.task_store.load_archived_files(stored.task_id)
                if stored.config.pdi_export_enabled
                else []
            )
            if source_files:
                self.task_store.set_phase(stored.task_id, "pdi_pending")
                stored.phase = "pdi_pending"
            else:
                self.task_store.clear(stored.task_id)
        except TaskStateError as exc:
            self.task_store.release_lease()
            QMessageBox.warning(self, "无法接受当前结果", str(exc))
            return

        self.config = AppConfig.from_dict(stored.config.to_dict())
        self._resume_checkpoint = None
        self._accepted_partial_results = True
        if source_files:
            self._pdi_task_id = stored.task_id
            self._pdi_source_files = source_files
            self._append_log("恢复", "已接受当前下载结果，继续生成 PDI", "warning")
            self._start_pdi_export(source_files)
            return

        self.task_store.release_lease()
        self._set_running(False)
        self.progress_label.setText("已接受当前结果，恢复记录已结束")
        QMessageBox.information(
            self,
            "已接受当前结果",
            "现有 DICOM 文件已保留；失败项不再作为必须续传任务。",
        )

    def _append_log(self, source: str, message: str, level: str) -> None:
        self._record_log_event("", source, message, level)

    def _record_log_event(
        self,
        task_id: str,
        source: str,
        message: str,
        level: str,
    ) -> None:
        self._log_events.append((task_id, source, message, level))
        if len(self._log_events) > 5000:
            del self._log_events[: len(self._log_events) - 5000]
        visible = self._log_event_is_visible(task_id, level)
        if level == "error" and visible and not self._log_panel_expanded:
            self._set_log_panel_expanded(True)
        if not visible:
            return
        display_source = f"{source} · {task_id[:8]}" if task_id else source
        self._render_log_event(display_source, message, level)

    def _render_log_event(
        self,
        source: str,
        message: str,
        level: str,
    ) -> None:
        colors = {
            "debug": COLORS["muted"],
            "info": COLORS["text"],
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["danger"],
        }
        safe_source = html.escape(source)
        safe_message = html.escape(message)
        color = colors.get(level, COLORS["text"])
        self.log_edit.append(
            f'<span style="color:{COLORS["muted"]}">[{safe_source}]</span> '
            f'<span style="color:{color}">{safe_message}</span>'
        )
        scrollbar = self.log_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _log_event_is_visible(self, task_id: str, level: str) -> bool:
        if not self._show_detailed_logs and level != "error":
            return False
        if not self.multi_task_enabled:
            return True
        show_all = self.log_scope_combo.currentData() == "all"
        return show_all or not task_id or task_id == self._selected_task_id

    def _on_multi_log(
        self,
        task_id: str,
        source: str,
        message: str,
        level: str,
    ) -> None:
        self._record_log_event(task_id, source, message, level)

    def _refresh_multi_log_view(self, _index: int = 0) -> None:
        if not hasattr(self, "log_edit"):
            return
        self.log_edit.clear()
        for task_id, source, message, level in self._log_events:
            if not self._log_event_is_visible(task_id, level):
                continue
            display_source = f"{source} · {task_id[:8]}" if task_id else source
            self._render_log_event(display_source, message, level)

    def _on_log_detail_toggled(self, checked: bool) -> None:
        self._show_detailed_logs = bool(checked)
        self.settings_store.setValue(
            "window/log_detailed", self._show_detailed_logs
        )
        self._refresh_multi_log_view()

    def _clear_logs(self) -> None:
        self._log_events.clear()
        self.log_edit.clear()

    def _selected_result_directory(self) -> str:
        row = self.task_table.currentRow()
        if row < 0:
            return ""
        item = self.task_table.item(row, 1)
        return str(item.data(Qt.UserRole) or "") if item else ""

    def _open_selected_result(self) -> None:
        directory = self._selected_result_directory() or self.destination_edit.text()
        path = Path(directory).expanduser()
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def _copy_selected_detail(self) -> None:
        row = self.task_table.currentRow()
        if row >= 0:
            values = [self.task_table.item(row, column) for column in range(self.task_table.columnCount())]
            QApplication.clipboard().setText(" | ".join(item.text() if item else "" for item in values))

    def _open_log_directory(self) -> None:
        config = AppConfig.from_dict(self.config.to_dict())
        config.dicom_destination_folder = self.destination_edit.text().strip()
        path = log_directory(config)
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def _open_diagnostic_log_directory(self) -> None:
        path = diagnostic_log_directory()
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def _toggle_log_panel(self) -> None:
        self._set_log_panel_expanded(not self._log_panel_expanded)

    def _set_log_panel_expanded(self, expanded: bool) -> None:
        self._log_panel_expanded = expanded
        self.log_panel.setVisible(expanded)
        self._sync_task_detail_visibility()
        self.log_toggle_button.setText(
            "收起日志" if expanded else "展开日志"
        )

    def _update_task_form_summary(self) -> None:
        destination = self.destination_edit.text().strip()
        display_destination = destination or "未选择保存目录"
        if len(display_destination) > 48:
            display_destination = "…" + display_destination[-47:]
        count = self._hidden_accession_count or len(self.current_accessions)
        self.task_form_summary.setText(
            f"{count} 个检查号 · 保存到 {display_destination}"
        )
        self.task_form_summary.setToolTip(
            f"{count} 个检查号\n保存到：{destination or '未选择保存目录'}"
        )

    def _set_task_form_expanded(self, expanded: bool) -> None:
        self._task_form_expanded = expanded
        self.task_form_body.setVisible(expanded)
        self._update_task_form_summary()
        self.task_form_summary.setVisible(not expanded)
        self.task_form_toggle_button.setArrowType(
            Qt.DownArrow if expanded else Qt.RightArrow
        )
        self.task_form_toggle_button.setText("收起" if expanded else "展开")
        self.task_form_toggle_button.setToolTip(
            "收起检查号和保存目录" if expanded else "展开新建任务输入"
        )

    def _toggle_task_form(self) -> None:
        self._set_task_form_expanded(not self._task_form_expanded)

    def _restore_ui_state(self) -> None:
        geometry = self.settings_store.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        self._clamp_to_available_screen()
        splitter = self.settings_store.value("window/splitter")
        if splitter:
            self.task_splitter.restoreState(splitter)

    def _clamp_to_available_screen(self) -> None:
        geometry = self.geometry()
        screen = QApplication.screenAt(geometry.center())
        if screen is None:
            screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        if available.width() <= 0 or available.height() <= 0:
            return

        minimum_width = min(WINDOW_MINIMUM_WIDTH, available.width())
        minimum_height = min(WINDOW_MINIMUM_HEIGHT, available.height())
        self.setMinimumSize(minimum_width, minimum_height)
        width = min(max(geometry.width(), minimum_width), available.width())
        height = min(max(geometry.height(), minimum_height), available.height())
        rightmost = available.x() + available.width() - width
        bottommost = available.y() + available.height() - height
        x = max(available.x(), min(geometry.x(), rightmost))
        y = max(available.y(), min(geometry.y(), bottommost))
        self.setGeometry(x, y, width, height)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.multi_task_enabled and self.task_controller is not None:
            summaries = self.task_controller.list_tasks()
            active = sum(
                item.phase in {"running", "pause_pending", "cancelling"}
                for item in summaries
            )
            queued = sum(item.phase == "queued" for item in summaries)
            paused = sum(item.phase == "paused" for item in summaries)
            pdi = sum(
                item.phase in {"pdi_pending", "pdi_running"}
                for item in summaries
            )
            if active or queued or paused or pdi:
                answer = QMessageBox.question(
                    self,
                    "退出 DcmGet",
                    (
                        f"当前有活动任务 {active} 个、排队任务 {queued} 个、"
                        f"暂停任务 {paused} 个、PDI 任务 {pdi} 个。\n\n"
                        "退出会停止当前后台进程，但会保留全部恢复点；"
                        "下次启动将自动继续。"
                    ),
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    event.ignore()
                    return
            if not self.task_controller.shutdown():
                self._append_log(
                    "多任务",
                    "后台进程尚未完全停止，暂不退出",
                    "error",
                )
                event.ignore()
                return
            self.task_controller = None
        if self._is_busy():
            if self._closing_after_cancel:
                event.ignore()
                return
            pdi_running = self.pdi_worker is not None
            answer = QMessageBox.question(
                self,
                "退出 DcmGet",
                (
                    "PDI 便携目录仍在生成。取消导出并退出吗？"
                    if pdi_running
                    else "下载仍在进行。停止任务并退出吗？"
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self._closing_after_cancel = True
            self.pause_button.setEnabled(False)
            active_worker = self.pdi_worker or self.worker
            if active_worker is not None:
                active_worker.request_cancel()
            active_thread = self.pdi_thread or self.worker_thread
            if active_thread:
                active_thread.finished.connect(self.close)
            self.progress_label.setText("正在停止后台进程并退出…")
            event.ignore()
            return
        self.settings_store.setValue("window/geometry", self.saveGeometry())
        self.settings_store.setValue("window/splitter", self.task_splitter.saveState())
        self.settings_store.setValue("window/log_expanded", self._log_panel_expanded)
        self.settings_store.setValue(
            "window/log_detailed", self._show_detailed_logs
        )
        self.settings_store.setValue(
            "window/task_form_expanded", self._task_form_expanded
        )
        self.settings_store.setValue("task/destination", self.destination_edit.text().strip())
        self.settings_store.sync()
        self._stop_pdi_viewer_process()
        event.accept()


APP_STYLESHEET = f"""
QWidget {{
    color: {COLORS['text']};
    font-size: 13px;
}}
QMainWindow, QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget {{
    background: {COLORS['background']};
}}
QFrame#Header {{
    background: {COLORS['surface']};
    border-bottom: 1px solid {COLORS['border']};
}}
QFrame#Card {{
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
}}
QFrame#PdiStatusCard {{
    background: #F0F9FF;
    border: 1px solid #BAE6FD;
    border-radius: 8px;
}}
QFrame#LargeBatchSummaryCard {{
    background: #F0F9FF;
    border: 1px solid #BAE6FD;
    border-radius: 8px;
}}
QLabel#AppTitle {{ font-size: 20px; font-weight: 700; color: {COLORS['text']}; }}
QLabel#HeaderSubtitle, QLabel#FieldHint {{ color: {COLORS['muted']}; }}
QLabel#PageTitle {{ font-size: 20px; font-weight: 700; }}
QLabel#SectionTitle {{ font-size: 15px; font-weight: 650; }}
QLabel#ProgressText {{ color: {COLORS['muted']}; font-weight: 600; }}
QLabel#PdiStatusText {{ color: {COLORS['muted']}; }}
QLabel#LargeBatchSummaryText {{ color: {COLORS['muted']}; }}
QLabel#PdiStatusText[status="generating"] {{ color: {COLORS['primary']}; font-weight: 600; }}
QLabel#PdiStatusText[status="ok"] {{ color: {COLORS['success']}; font-weight: 600; }}
QLabel#PdiStatusText[status="warning"] {{ color: {COLORS['warning']}; font-weight: 600; }}
QLabel#PdiStatusText[status="error"] {{ color: {COLORS['danger']}; font-weight: 600; }}
QLabel#ErrorText {{ color: {COLORS['danger']}; background: #FEF2F2; border: 1px solid #FECACA; padding: 9px; border-radius: 6px; }}
QLabel#InlineErrorText {{ color: {COLORS['danger']}; }}
QLabel#WarningText {{ color: {COLORS['warning']}; background: #FFF7ED; border: 1px solid #FED7AA; padding: 9px; border-radius: 6px; }}
QLabel#StatusPill, QLabel#CheckPill {{ padding: 6px 10px; border-radius: 12px; background: #F1F5F9; color: {COLORS['muted']}; }}
QLabel#StatusPill[status="ok"], QLabel#CheckPill[status="ok"], QLabel#FieldHint[status="ok"] {{ background: #ECFDF5; color: {COLORS['success']}; }}
QLabel#StatusPill[status="warning"] {{ background: #FFF7ED; color: {COLORS['warning']}; }}
QLabel#StatusPill[status="error"], QLabel#CheckPill[status="error"], QLabel#FieldHint[status="error"] {{ background: #FEF2F2; color: {COLORS['danger']}; }}
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox, QTableWidget {{
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 6px;
    selection-background-color: #BAE6FD;
    selection-color: {COLORS['text']};
}}
QLineEdit, QSpinBox, QComboBox {{ padding: 7px 9px; min-height: 20px; }}
QPlainTextEdit, QTextEdit {{ padding: 8px; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus, QTableWidget:focus {{ border: 2px solid {COLORS['primary']}; }}
QLineEdit[invalid="true"], QSpinBox[invalid="true"], QComboBox[invalid="true"] {{ border: 2px solid {COLORS['danger']}; background: #FEF2F2; }}
QPushButton, QToolButton {{
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 6px;
    padding: 7px 12px;
}}
QPushButton:hover, QToolButton:hover {{ background: #F1F5F9; border-color: #94A3B8; }}
QPushButton:focus, QToolButton:focus {{ border: 2px solid {COLORS['primary']}; }}
QPushButton:disabled, QToolButton:disabled {{ color: #94A3B8; background: #F8FAFC; }}
QPushButton#PrimaryButton {{ background: {COLORS['primary']}; color: white; border-color: {COLORS['primary']}; font-weight: 650; }}
QPushButton#PrimaryButton:hover {{ background: {COLORS['primary_hover']}; }}
QPushButton#PrimaryButton:focus {{ border: 2px solid {COLORS['focus_on_primary']}; }}
QPushButton#PrimaryButton:disabled {{ color: #94A3B8; border-color: {COLORS['border']}; background: #F8FAFC; }}
QPushButton#DangerButton {{ color: {COLORS['danger']}; border-color: #FCA5A5; }}
QPushButton#DangerButton:hover {{ background: #FEF2F2; }}
QPushButton#DangerButton:focus {{ border: 2px solid {COLORS['primary']}; }}
QPushButton#DangerButton:disabled {{ color: #94A3B8; border-color: {COLORS['border']}; background: #F8FAFC; }}
QProgressBar {{ border: 0; background: #E2E8F0; border-radius: 4px; min-height: 8px; max-height: 8px; }}
QProgressBar::chunk {{ background: {COLORS['primary']}; border-radius: 4px; }}
QHeaderView::section {{ background: #F1F5F9; color: {COLORS['muted']}; padding: 8px; border: 0; border-bottom: 1px solid {COLORS['border']}; font-weight: 650; }}
QTableWidget {{ gridline-color: #E2E8F0; alternate-background-color: #F8FAFC; }}
QTableWidget::item {{ padding: 6px; }}
QTableWidget::item:selected {{ background: #E0F2FE; color: {COLORS['text']}; }}
QScrollBar:vertical {{ width: 10px; background: transparent; }}
QScrollBar::handle:vertical {{ background: #CBD5E1; border-radius: 5px; min-height: 28px; }}
"""
