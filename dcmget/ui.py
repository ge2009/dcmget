from __future__ import annotations

import html
import os
import re
import socket
import sys
import threading
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
from .task_state import (
    TaskCheckpoint,
    TaskCheckpointStore,
    TaskStateError,
    merge_checkpoint_summary,
)


COLORS = {
    "primary": "#0369A1",
    "primary_hover": "#075985",
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


def pdi_viewer_command(directory: str | Path) -> tuple[str, list[str]] | None:
    """Return a safe detached command for a viewer bundled in one PDI root."""

    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        return None

    standalone_names = (
        ("OPEN_VIEWER.exe", "OPEN_VIEWER")
        if sys.platform == "win32"
        else ("OPEN_VIEWER",)
    )
    for name in standalone_names:
        executable = root / name
        if executable.is_file() and not executable.is_symlink():
            return str(executable), ["--root", str(root), "--quiet"]

    server_script = root / "VIEWER" / "pdi_server.py"
    if (
        server_script.is_file()
        and not server_script.is_symlink()
        and not bool(getattr(sys, "frozen", False))
    ):
        return str(Path(sys.executable).resolve()), [
            str(server_script),
            "--root",
            str(root),
            "--quiet",
        ]

    if sys.platform == "win32":
        batch = root / "OPEN_VIEWER.bat"
        if batch.is_file() and not batch.is_symlink():
            return "cmd.exe", ["/d", "/s", "/c", str(batch)]
        return None

    launcher = root / (
        "OPEN_VIEWER.command" if sys.platform == "darwin" else "OPEN_VIEWER.sh"
    )
    if launcher.is_file() and not launcher.is_symlink():
        return "/bin/sh", [str(launcher)]
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
        self.setMinimumSize(680, 520)
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
        browse = QPushButton("选择目录")
        browse.clicked.connect(self._browse_dcmtk)
        dcmtk_row_layout.addWidget(self.dcmtk_edit, 1)
        dcmtk_row_layout.addWidget(browse)
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
        for widget in (self.calling_ae_edit, self.pacs_ae_edit):
            widget.setMaxLength(16)
        pacs_form.addRow("PACS 地址", self.pacs_host_edit)
        pacs_form.addRow("PACS 端口", self.pacs_port_edit)
        pacs_form.addRow("本机调用 AE", self.calling_ae_edit)
        pacs_form.addRow("PACS AE", self.pacs_ae_edit)

        receiver_card, receiver_form = self._card("DICOM 接收器")
        self.storage_ae_edit = QLineEdit()
        self.storage_ae_edit.setMaxLength(16)
        self.storage_port_edit = self._port_edit()
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
        pdi_heading = QLabel("PDI 便携目录导出")
        pdi_heading.setObjectName("SectionTitle")
        pdi_header.addWidget(pdi_heading)
        pdi_header.addStretch()
        self.pdi_enabled_checkbox = QCheckBox("每批下载完成后自动生成")
        self.pdi_enabled_checkbox.setAccessibleName("自动生成 PDI 便携目录")
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
        self.pdi_output_edit.setPlaceholderText("留空时保存到 DICOM 目录/PDI")
        self.pdi_output_button = QPushButton("选择目录")
        self.pdi_output_button.clicked.connect(self._browse_pdi_output)
        pdi_output_layout.addWidget(self.pdi_output_edit, 1)
        pdi_output_layout.addWidget(self.pdi_output_button)
        pdi_form.addRow("输出根目录", pdi_output_row)

        self.pdi_ohif_checkbox = QCheckBox("生成后可直接阅片（推荐）")
        pdi_form.addRow("DICOM 查看器", self.pdi_ohif_checkbox)
        self.pdi_ohif_hint = QLabel(
            "直接读取目录中的原始 DICOM，不生成 JPG；无需选择索引文件。"
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
            calling_ae_title=self.calling_ae_edit.text().strip(),
            pacs_ae_title=self.pacs_ae_edit.text().strip(),
            storage_ae_title=self.storage_ae_edit.text().strip(),
            storage_port=self._port_value(self.storage_port_edit),
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
            widget.setToolTip(message)
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

    def _browse_dcmtk(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择 DCMTK bin 目录", self.dcmtk_edit.text())
        if selected:
            self.dcmtk_edit.setText(selected)

    def _browse_pdi_output(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "选择 PDI 输出根目录", self.pdi_output_edit.text()
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
    ):
        super().__init__()
        self.config_path = Path(config_path)
        self.project_root = Path(project_root)
        self.config = load_config(self.config_path)
        self.resolver = DcmtkResolver(self.project_root)
        self.task_store = TaskCheckpointStore(task_state_path)
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
        self.settings_store = QSettings("DcmGet", "DcmGet2")
        self._log_panel_expanded = self.settings_store.value(
            "window/log_expanded", False, type=bool
        )
        self._task_form_expanded = self.settings_store.value(
            "window/task_form_expanded", True, type=bool
        )

        self.setWindowTitle(f"DcmGet {__version__} - DICOM 下载工作台")
        self.setMinimumSize(1024, 720)
        self.resize(1180, 820)
        logo = self.project_root / "logo.png"
        if logo.exists():
            self.setWindowIcon(QIcon(str(logo)))
        self._build_ui()
        self._restore_ui_state()
        self.settings_page.set_config(self.config)
        self._reset_pdi_status_card()
        last_destination = self.settings_store.value("task/destination", "", type=str)
        self.destination_edit.setText(last_destination or self.config.dicom_destination_folder)
        self._load_configured_accessions()
        QTimer.singleShot(0, self._refresh_tool_status)
        if offer_task_resume:
            QTimer.singleShot(0, self._offer_task_resume)

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self._build_header())

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_task_page())
        self.settings_page = SettingsPage()
        self.settings_page.saved.connect(self._save_settings)
        self.settings_page.back_requested.connect(self._cancel_settings)
        self.pages.addWidget(self.settings_page)
        root_layout.addWidget(self.pages, 1)
        self.setCentralWidget(root)
        self.setStyleSheet(APP_STYLESHEET)

        QShortcut(QKeySequence("Ctrl+O"), self, activated=self._choose_accession_file)
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self._start_download)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=self._show_settings)

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("Header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(24, 14, 24, 14)
        title_box = QVBoxLayout()
        title = QLabel(f"DcmGet {__version__}")
        title.setObjectName("AppTitle")
        subtitle = QLabel("DICOM 批量下载工作台")
        subtitle.setObjectName("HeaderSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        layout.addLayout(title_box)
        layout.addStretch()
        self.tool_status = QLabel("正在检测 DCMTK…")
        self.tool_status.setObjectName("StatusPill")
        self.tool_status.setProperty("status", "pending")
        layout.addWidget(self.tool_status)
        self.entitlement_status = QLabel()
        self.entitlement_status.setObjectName("StatusPill")
        self._refresh_entitlement_status()
        layout.addWidget(self.entitlement_status)
        self.registration_button = QToolButton()
        self.registration_button.setText("软件注册")
        self.registration_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.registration_button.clicked.connect(self._show_activation)
        layout.addWidget(self.registration_button)
        self.release_notes_button = QToolButton()
        self.release_notes_button.setText("版本说明")
        self.release_notes_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.release_notes_button.clicked.connect(self._show_release_notes)
        layout.addWidget(self.release_notes_button)
        self.diagnostic_log_button = QToolButton()
        self.diagnostic_log_button.setText("诊断日志")
        self.diagnostic_log_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.diagnostic_log_button.setToolTip("打开启动、异常和崩溃诊断日志目录")
        self.diagnostic_log_button.clicked.connect(
            self._open_diagnostic_log_directory
        )
        layout.addWidget(self.diagnostic_log_button)
        self.settings_button = QToolButton()
        self.settings_button.setText("设置")
        self.settings_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.settings_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogDetailedView))
        self.settings_button.setToolTip("连接、接收、匿名与 PDI 设置（Ctrl+,）")
        self.settings_button.clicked.connect(self._show_settings)
        layout.addWidget(self.settings_button)
        return header

    def _build_task_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
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
        grid.addWidget(self.destination_edit, 2, 1)
        self.destination_button = QPushButton("选择目录")
        self.destination_button.clicked.connect(self._choose_destination)
        grid.addWidget(self.destination_button, 2, 2)
        self.open_destination_button = QPushButton("打开目标目录")
        self.open_destination_button.clicked.connect(self._open_destination_directory)
        grid.addWidget(self.open_destination_button, 2, 3)
        grid.setColumnStretch(1, 1)
        input_layout.addWidget(self.task_form_body)
        self._set_task_form_expanded(self._task_form_expanded)
        layout.addWidget(input_card)

        preflight_card = QFrame()
        preflight_card.setObjectName("Card")
        preflight_layout = QVBoxLayout(preflight_card)
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
            label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            self.preflight_labels.append(label)
            self.preflight_grid.addWidget(label, index // 3, index % 3)
        preflight_layout.addLayout(self.preflight_grid)
        layout.addWidget(preflight_card)

        action_row = QHBoxLayout()
        self.progress_label = QLabel("尚未开始")
        self.progress_label.setObjectName("ProgressText")
        action_row.addWidget(self.progress_label)
        action_row.addStretch()
        self.discard_resume_button = QPushButton("放弃续传并新建")
        self.discard_resume_button.setObjectName("DangerButton")
        self.discard_resume_button.setToolTip("保留已下载文件，仅删除未完成任务的恢复记录")
        self.discard_resume_button.clicked.connect(self._discard_resume_task)
        self.discard_resume_button.hide()
        self.retry_button = QPushButton("重试失败项")
        self.retry_button.setEnabled(False)
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
        self.pause_button = QPushButton("暂停")
        self.pause_button.setEnabled(False)
        self.pause_button.setToolTip("当前检查号完成后暂停；继续时从下一项接着下载")
        self.pause_button.clicked.connect(self._toggle_pause)
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("DangerButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_download)
        self.start_button = QPushButton("开始下载")
        self.start_button.setObjectName("PrimaryButton")
        self.start_button.setToolTip("开始下载（Ctrl+Enter）")
        self.start_button.clicked.connect(
            lambda _checked=False: self._start_download()
        )
        action_row.addWidget(self.discard_resume_button)
        action_row.addWidget(self.retry_button)
        action_row.addWidget(self.accept_partial_button)
        action_row.addWidget(self.log_toggle_button)
        action_row.addWidget(self.pause_button)
        action_row.addWidget(self.stop_button)
        action_row.addWidget(self.start_button)
        layout.addLayout(action_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.progress_bar)

        self.pdi_status_card = QFrame()
        self.pdi_status_card.setObjectName("PdiStatusCard")
        pdi_status_layout = QVBoxLayout(self.pdi_status_card)
        pdi_status_layout.setContentsMargins(14, 10, 14, 10)
        pdi_status_layout.setSpacing(8)
        pdi_status_header = QHBoxLayout()
        pdi_title = QLabel("PDI 便携目录")
        pdi_title.setObjectName("SectionTitle")
        pdi_status_header.addWidget(pdi_title)
        self.pdi_status_label = QLabel("下载完成后自动生成")
        self.pdi_status_label.setObjectName("PdiStatusText")
        self.pdi_status_label.setWordWrap(True)
        pdi_status_header.addWidget(self.pdi_status_label, 1)
        self.pdi_view_button = QPushButton("立即阅片")
        self.pdi_view_button.setObjectName("PrimaryButton")
        self.pdi_view_button.setEnabled(False)
        self.pdi_view_button.setToolTip("从当前 PDI 目录启动本地离线阅片器")
        self.pdi_view_button.clicked.connect(self._open_pdi_viewer)
        self.pdi_open_button = QPushButton("打开文件夹")
        self.pdi_open_button.setEnabled(False)
        self.pdi_open_button.clicked.connect(self._open_pdi_directory)
        self.pdi_retry_button = QPushButton("重试 PDI")
        self.pdi_retry_button.setEnabled(False)
        self.pdi_retry_button.clicked.connect(self._retry_pdi)
        pdi_status_header.addWidget(self.pdi_view_button)
        pdi_status_header.addWidget(self.pdi_open_button)
        pdi_status_header.addWidget(self.pdi_retry_button)
        pdi_status_layout.addLayout(pdi_status_header)
        self.pdi_progress_bar = QProgressBar()
        self.pdi_progress_bar.setRange(0, 100)
        self.pdi_progress_bar.setValue(0)
        self.pdi_progress_bar.setTextVisible(False)
        pdi_status_layout.addWidget(self.pdi_progress_bar)
        layout.addWidget(self.pdi_status_card)

        self.large_batch_summary_card = QFrame()
        self.large_batch_summary_card.setObjectName("LargeBatchSummaryCard")
        large_batch_layout = QVBoxLayout(self.large_batch_summary_card)
        large_batch_layout.setContentsMargins(14, 12, 14, 12)
        large_batch_layout.setSpacing(6)
        large_batch_title = QLabel("大批量任务摘要")
        large_batch_title.setObjectName("SectionTitle")
        large_batch_layout.addWidget(large_batch_title)
        self.large_batch_summary_label = QLabel()
        self.large_batch_summary_label.setObjectName("LargeBatchSummaryText")
        self.large_batch_summary_label.setWordWrap(True)
        self.large_batch_summary_label.setAccessibleName("大批量任务总进度")
        large_batch_layout.addWidget(self.large_batch_summary_label)
        self.large_batch_summary_card.hide()
        layout.addWidget(self.large_batch_summary_card)

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
        return page

    def _build_log_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("Card")
        layout = QVBoxLayout(panel)
        header = QHBoxLayout()
        title = QLabel("运行日志")
        title.setObjectName("SectionTitle")
        header.addWidget(title)
        header.addStretch()
        open_result = QPushButton("打开结果")
        open_result.clicked.connect(self._open_selected_result)
        copy_error = QPushButton("复制详情")
        copy_error.clicked.connect(self._copy_selected_detail)
        clear = QPushButton("清空日志")
        clear.clicked.connect(lambda: self.log_edit.clear())
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

    def _show_settings(self) -> None:
        if self._is_busy():
            return
        self.settings_page.set_config(self.config)
        self.pages.setCurrentIndex(1)

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
        self.entitlement_status.setProperty(
            "status", "ok" if text.startswith("已注册") else "warning"
        )
        self.entitlement_status.style().unpolish(self.entitlement_status)
        self.entitlement_status.style().polish(self.entitlement_status)

    def _save_settings(self, config: AppConfig) -> None:
        config.access_numbers_file_path = self.config.access_numbers_file_path
        config.dicom_destination_folder = self.destination_edit.text().strip()
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
        except Exception as exc:
            self.tools = None
            self._set_tool_status("DCMTK 未就绪", "error")
            self.settings_page.set_dcmtk_status(str(exc), False)

    def _set_tool_status(self, text: str, status: str) -> None:
        self.tool_status.setText(text)
        self.tool_status.setProperty("status", status)
        self.tool_status.style().unpolish(self.tool_status)
        self.tool_status.style().polish(self.tool_status)

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
                self._append_log("恢复", str(exc), "warning")
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

    def _sync_task_detail_visibility(self) -> None:
        self.task_table.setVisible(not self._task_table_summary_mode)
        self.large_batch_summary_card.setVisible(self._task_table_summary_mode)
        self.task_splitter.setVisible(
            not self._task_table_summary_mode or self._log_panel_expanded
        )

    def _start_download(
        self,
        override: list[str] | None = None,
        *,
        resume_checkpoint: TaskCheckpoint | None = None,
    ) -> None:
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
                    self._append_log("恢复", str(exc), "warning")
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
        if not check.ok or check.tools is None:
            self._append_log("预检", "启动预检未通过，请修正标红设置", "error")
            QMessageBox.warning(self, "预检未通过", "请查看启动预检和设置页中的错误提示。")
            if any(key != "dicom_destination_folder" for key in check.errors):
                self.pages.setCurrentIndex(1)
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
            label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
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
        self.stop_button.setEnabled(running)
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
        self.retry_button.setEnabled(
            bool(
                not running
                and (
                    download_retryable
                    or (
                        not recovery_pending
                        and not self._accepted_partial_results
                        and self.last_summary
                        and self.last_summary.failed_accessions
                    )
                )
            )
        )
        self.accept_partial_button.setVisible(download_retryable)
        self.accept_partial_button.setEnabled(download_retryable)
        self.accession_edit.setReadOnly(running or recovery_pending)
        self.destination_edit.setReadOnly(running or pdi_pending)
        self.accession_button.setEnabled(not running and not recovery_pending)
        self.destination_button.setEnabled(not running and not pdi_pending)
        self.settings_button.setEnabled(not running)
        self.registration_button.setEnabled(not running)

    def _on_worker_state(self, state: str) -> None:
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
        self._set_pdi_status("准备生成 PDI 便携目录…", "pending")
        self.pdi_progress_bar.setRange(0, 0)
        self.pdi_view_button.setEnabled(False)
        self.pdi_open_button.setEnabled(False)
        self.pdi_retry_button.setEnabled(False)
        self._set_running(True, reset_summary=False, can_pause=False)
        self.progress_label.setText("下载已完成，正在生成 PDI…")

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
            pdi_message = f"PDI 便携目录已生成：{output_directory}"
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
        command = pdi_viewer_command(root) if root is not None else None
        if root is None or command is None:
            QMessageBox.warning(
                self,
                "无法启动阅片器",
                "当前 PDI 目录没有可用的离线阅片启动器，请重试 PDI 导出。\n\n"
                f"诊断日志：{diagnostic_log_directory()}",
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
                        "离线阅片服务正在启动，请稍后再点击“立即阅片”",
                        "info",
                    )
                return
            self._stop_pdi_viewer_process()
        program, arguments = command
        viewer_url = ""
        controls_browser = "--root" in arguments
        if controls_browser:
            port = 0
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = int(listener.getsockname()[1])
            except OSError as exc:
                record_exception("DcmGetWindow._open_pdi_viewer.port", exc)
            arguments = [
                *arguments,
                "--port",
                str(port),
                "--no-browser",
            ]
            if port:
                from .pdi_server import viewer_url as build_viewer_url

                viewer_url = build_viewer_url(port)
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

    def _set_pdi_viewer_url(self, url: str) -> None:
        candidate = QUrl(url)
        port = candidate.port()
        if (
            candidate.scheme() != "http"
            or candidate.host() != "127.0.0.1"
            or port <= 0
        ):
            return
        self._pdi_viewer_url = candidate.toString()
        self._pdi_viewer_probe_url = f"http://127.0.0.1:{port}/api/studies"

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

    def _on_pdi_viewer_error(self, process: QProcess, _error: object) -> None:
        if process is not self._pdi_viewer_process:
            return
        if self._pdi_viewer_ready:
            self._append_log("PDI", f"离线阅片服务异常：{process.errorString()}", "warning")
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
        if self._pdi_source_files or self._pdi_task_id:
            self._start_pdi_export()

    def _on_worker_failed(self, message: str) -> None:
        self._worker_failure_message = message
        self._append_log("应用", message, "error")
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
        if level == "error" and not self._log_panel_expanded:
            self._set_log_panel_expanded(True)
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
        splitter = self.settings_store.value("window/splitter")
        if splitter:
            self.task_splitter.restoreState(splitter)

    def closeEvent(self, event: QCloseEvent) -> None:
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
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus, QTableWidget:focus {{ border: 2px solid #38BDF8; }}
QLineEdit[invalid="true"], QSpinBox[invalid="true"], QComboBox[invalid="true"] {{ border: 2px solid {COLORS['danger']}; background: #FEF2F2; }}
QPushButton, QToolButton {{
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 6px;
    padding: 7px 12px;
}}
QPushButton:hover, QToolButton:hover {{ background: #F1F5F9; border-color: #94A3B8; }}
QPushButton:focus, QToolButton:focus {{ border: 2px solid #38BDF8; }}
QPushButton:disabled, QToolButton:disabled {{ color: #94A3B8; background: #F8FAFC; }}
QPushButton#PrimaryButton {{ background: {COLORS['primary']}; color: white; border-color: {COLORS['primary']}; font-weight: 650; }}
QPushButton#PrimaryButton:hover {{ background: {COLORS['primary_hover']}; }}
QPushButton#DangerButton {{ color: {COLORS['danger']}; border-color: #FCA5A5; }}
QPushButton#DangerButton:hover {{ background: #FEF2F2; }}
QProgressBar {{ border: 0; background: #E2E8F0; border-radius: 4px; min-height: 8px; max-height: 8px; }}
QProgressBar::chunk {{ background: {COLORS['primary']}; border-radius: 4px; }}
QHeaderView::section {{ background: #F1F5F9; color: {COLORS['muted']}; padding: 8px; border: 0; border-bottom: 1px solid {COLORS['border']}; font-weight: 650; }}
QTableWidget {{ gridline-color: #E2E8F0; alternate-background-color: #F8FAFC; }}
QTableWidget::item {{ padding: 6px; }}
QTableWidget::item:selected {{ background: #E0F2FE; color: {COLORS['text']}; }}
QScrollBar:vertical {{ width: 10px; background: transparent; }}
QScrollBar::handle:vertical {{ background: #CBD5E1; border-radius: 5px; min-height: 28px; }}
"""
