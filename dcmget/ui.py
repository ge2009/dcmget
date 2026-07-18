from __future__ import annotations

import html
import re
import socket
import sys
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock
from PyQt5.QtCore import (
    QObject,
    QProcess,
    QSettings,
    QStandardPaths,
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
    QTextCursor,
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
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QProgressDialog,
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
    QInputDialog,
)

from . import __version__
from .auth_ui import activate_gui, entitlement_text, prepare_download_entitlement
from .accession_import import (
    AccessionImportError,
    AccessionImportResult,
    ColumnSelectionError,
    import_accession_file,
)
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
from .instance_shortcut import (
    InstanceShortcutError,
    ShortcutExistsError,
    create_instance_shortcut,
    default_instance_shortcut_name,
)
from .licensing import consume_trial, trial_task_consumed
from .release_notes import load_release_notes
from .runtime import is_frozen, resource_root
from .task_state import (
    TaskCheckpoint,
    TaskCheckpointStore,
    TaskStateError,
    merge_checkpoint_summary,
)
from .task_ledger import TaskLedger, TaskLedgerError


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
PDI_TASK_SETTING_FIELDS = (
    "pdi_export_enabled",
    "pdi_institution_name",
    "pdi_output_folder",
    "pdi_include_ohif_viewer",
    "pdi_volume_size_bytes",
)
PDI_VIEWER_PROBE_INTERVAL_MS = 250
PDI_VIEWER_START_TIMEOUT_MS = 30_000
PDI_LAST_OPEN_DIRECTORY_KEY = "pdi/last_open_directory"
ACCESSION_IMPORT_PATH_KEY = "accession_import/path"
ACCESSION_IMPORT_COLUMN_NAME_KEY = "accession_import/column_name"
ACCESSION_IMPORT_COLUMN_INDEX_KEY = "accession_import/column_index"
PDI_OHIF_VERSION = "3.12.6"
WINDOW_MINIMUM_WIDTH = 800
WINDOW_MINIMUM_HEIGHT = 520
DELETABLE_TASK_PHASES = frozenset(
    {"cancelled", "completed", "failed", "download_retryable", "pdi_retryable"}
)


@dataclass(frozen=True, slots=True)
class TaskSummary:
    """Lightweight compatibility view used by the single-task UI state."""

    task_id: str
    name: str
    phase: str
    total_count: int
    processed_count: int
    pending_count: int
    completed_count: int
    failed_count: int
    file_count: int
    received_bytes: int
    speed_bytes_per_second: float
    queue_position: int | None
    current_accession: str
    error_message: str
    created_at: str
    updated_at: str
    no_data_count: int = 0
    partial_count: int = 0
    cancelled_count: int = 0

    @property
    def completed_only_count(self) -> int:
        return max(0, self.completed_count - self.no_data_count)

    @property
    def failed_only_count(self) -> int:
        return max(0, self.failed_count - self.partial_count)


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
    large_text_pasted = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.hidden_accessions: list[str] = []
        self.setAcceptDrops(True)
        self.setTabChangesFocus(True)
        self.setPlaceholderText("每行一个检查号；也可以拖入 TXT、CSV 或 XLSX 文件")
        self.setMinimumHeight(92)
        self.setAccessibleName("检查号输入")

    def dragEnterEvent(self, event):  # type: ignore[override]
        urls = event.mimeData().urls()
        if (
            urls
            and urls[0].isLocalFile()
            and Path(urls[0].toLocalFile()).suffix.lower()
            in {".txt", ".csv", ".xlsx"}
        ):
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

    def insertFromMimeData(self, source):  # type: ignore[override]
        if source.hasText():
            pasted_text = source.text()
            if self.hidden_accessions and not self.toPlainText():
                prospective_text = "\n".join(
                    [*self.hidden_accessions, pasted_text]
                )
            else:
                cursor = self.textCursor()
                prefix_cursor = QTextCursor(self.document())
                prefix_cursor.setPosition(0)
                prefix_cursor.setPosition(
                    cursor.selectionStart(),
                    QTextCursor.KeepAnchor,
                )
                suffix_cursor = QTextCursor(self.document())
                suffix_cursor.setPosition(cursor.selectionEnd())
                suffix_cursor.setPosition(
                    self.document().characterCount() - 1,
                    QTextCursor.KeepAnchor,
                )
                prefix = prefix_cursor.selectedText().replace("\u2029", "\n")
                suffix = suffix_cursor.selectedText().replace("\u2029", "\n")
                prospective_text = prefix + pasted_text + suffix
            if (
                len(parse_accessions(prospective_text).values)
                > TASK_TABLE_DETAIL_LIMIT
            ):
                self.large_text_pasted.emit(prospective_text)
                return
        super().insertFromMimeData(source)


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
        self.receiver_form = receiver_form
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
        self.concurrency_hint = QLabel(
            "相同接收 AE 与端口复用一个并发 SCP；改用不同端口会自动启动多个 SCP。"
            "默认同时下载 2 个检查号，其余任务显示为“等待并发槽”。"
        )
        self.concurrency_hint.setObjectName("FieldHint")
        self.concurrency_hint.setWordWrap(True)
        receiver_form.addRow("", self.concurrency_hint)
        receiver_form.addRow("目录模板", self.directory_template_combo)
        directory_hint = QLabel(
            "可编辑组合：{PatientID}、{AccessionNumber}、{StudyInstanceUID}"
        )
        directory_hint.setObjectName("FieldHint")
        directory_hint.setWordWrap(True)
        receiver_form.addRow("", directory_hint)
        receiver_form.addRow("单个日志上限", self.log_size_spin)

        reliability_card, reliability_form = self._card("可靠性与安全保护")
        self.minimum_free_space_spin = QDoubleSpinBox()
        self.minimum_free_space_spin.setRange(0, 4096)
        self.minimum_free_space_spin.setDecimals(1)
        self.minimum_free_space_spin.setSingleStep(1)
        self.minimum_free_space_spin.setSuffix(" GB")
        self.minimum_free_space_spin.setSpecialValueText("关闭保护")
        self.minimum_free_space_spin.setToolTip(
            "低于该剩余空间时不再启动新的检查号，并安全结束当前接收进程"
        )
        self.auto_retry_attempts_spin = QSpinBox()
        self.auto_retry_attempts_spin.setRange(0, 10)
        self.auto_retry_attempts_spin.setSuffix(" 次")
        self.auto_retry_backoff_spin = QSpinBox()
        self.auto_retry_backoff_spin.setRange(0, 300)
        self.auto_retry_backoff_spin.setSuffix(" 秒")
        self.circuit_breaker_spin = QSpinBox()
        self.circuit_breaker_spin.setRange(2, 100)
        self.circuit_breaker_spin.setSuffix(" 个")
        reliability_form.addRow("磁盘保留空间", self.minimum_free_space_spin)
        reliability_form.addRow("瞬时故障重试", self.auto_retry_attempts_spin)
        reliability_form.addRow("重试基础等待", self.auto_retry_backoff_spin)
        reliability_form.addRow("连续失败暂停", self.circuit_breaker_spin)
        reliability_hint = QLabel(
            "自动重试只用于网络、关联等瞬时故障；连续失败达到阈值后安全暂停，"
            "避免 PACS 或网络异常时批量空跑。"
        )
        reliability_hint.setObjectName("FieldHint")
        reliability_hint.setWordWrap(True)
        reliability_form.addRow("", reliability_hint)

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
        self.pdi_volume_size_spin = QDoubleSpinBox()
        self.pdi_volume_size_spin.setRange(0, 4096)
        self.pdi_volume_size_spin.setDecimals(1)
        self.pdi_volume_size_spin.setSingleStep(1)
        self.pdi_volume_size_spin.setSuffix(" GB")
        self.pdi_volume_size_spin.setSpecialValueText("不分卷")
        self.pdi_volume_size_spin.setToolTip(
            "按完整 Study 自动分卷；同一检查绝不会拆到两个介质目录"
        )
        pdi_form.addRow("单卷容量", self.pdi_volume_size_spin)
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
        content_layout.addWidget(reliability_card)
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
            "minimum_free_space_bytes": self.minimum_free_space_spin,
            "auto_retry_attempts": self.auto_retry_attempts_spin,
            "auto_retry_backoff_seconds": self.auto_retry_backoff_spin,
            "circuit_breaker_failures": self.circuit_breaker_spin,
            "directory_template": self.directory_template_combo,
            "anonymization_profile": self.anonymization_profile_combo,
            "pdi_institution_name": self.pdi_institution_edit,
            "pdi_output_folder": self.pdi_output_edit,
            "pdi_volume_size_bytes": self.pdi_volume_size_spin,
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
        self.minimum_free_space_spin.setValue(
            config.minimum_free_space_bytes / 1024**3
        )
        self.auto_retry_attempts_spin.setValue(config.auto_retry_attempts)
        self.auto_retry_backoff_spin.setValue(config.auto_retry_backoff_seconds)
        self.circuit_breaker_spin.setValue(config.circuit_breaker_failures)
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
        self.pdi_volume_size_spin.setValue(config.pdi_volume_size_bytes / 1024**3)
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
            minimum_free_space_bytes=round(
                self.minimum_free_space_spin.value() * 1024**3
            ),
            auto_retry_attempts=self.auto_retry_attempts_spin.value(),
            auto_retry_backoff_seconds=self.auto_retry_backoff_spin.value(),
            circuit_breaker_failures=self.circuit_breaker_spin.value(),
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
            pdi_volume_size_bytes=round(
                self.pdi_volume_size_spin.value() * 1024**3
            ),
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
            self.pdi_volume_size_spin,
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

    def set_multi_task_mode(self, enabled: bool) -> None:
        label = self.receiver_form.labelForField(
            self.max_concurrent_moves_spin
        )
        self.max_concurrent_moves_spin.setVisible(enabled)
        if label is not None:
            label.setVisible(enabled)
        self.concurrency_hint.setText(
            (
                "相同接收 AE 与端口复用一个并发 SCP；改用不同端口会自动启动多个 SCP。"
                "默认同时下载 2 个检查号，其余任务显示为“等待并发槽”。"
                if enabled
                else "多开实例需使用不同的接收 AE/端口，每个窗口独立保存到自己的目标目录。"
            )
        )

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
    log_directory_ready = pyqtSignal(str, bool)
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
        log_directory: str | Path | None = None,
        fallback_log_directory: str | Path | None = None,
        task_ledger: TaskLedger | None = None,
    ):
        super().__init__()
        self.config = config
        self.tools = tools
        self.accessions = accessions
        self.consume_trial_on_ready = consume_trial_on_ready
        self.task_store = task_store
        self.task_id = task_id
        self.log_directory = (
            Path(log_directory).expanduser() if log_directory is not None else None
        )
        self.fallback_log_directory = (
            Path(fallback_log_directory).expanduser()
            if fallback_log_directory is not None
            else None
        )
        self.task_ledger = task_ledger
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
                audit_callback=(
                    self._record_audit if self.task_ledger is not None else None
                ),
                log_file_name=(
                    f"task-{self.task_id}.log" if self.task_id else "dcmget.log"
                ),
                log_directory=self.log_directory,
                fallback_log_directory=self.fallback_log_directory,
            )
            active_log_directory = getattr(
                runner, "active_log_directory", self.log_directory
            )
            if active_log_directory is not None:
                self.log_directory_ready.emit(
                    str(active_log_directory),
                    bool(getattr(runner, "used_log_fallback", False)),
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

    def _record_audit(
        self,
        result: AccessionResult,
        observations: tuple[object, ...],
    ) -> None:
        if self.task_ledger is None or not self.task_id:
            return
        if not self.config.anonymization_enabled:
            anonymization_status = "not_requested"
        elif result.archived_files:
            anonymization_status = "completed"
        elif result.status == AccessionStatus.NO_DATA:
            anonymization_status = "no_data"
        else:
            anonymization_status = "failed"
        self.task_ledger.record_runner_result(
            self.task_id,
            result,
            observed_instances=observations,
            anonymization_status=anonymization_status,
        )

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
            from .pdi import PdiExporter, PdiVolumeExporter

            exporter_type = (
                PdiVolumeExporter
                if self.config.pdi_volume_size_bytes > 0
                else PdiExporter
            )
            exporter = exporter_type(
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


class PdiVerifyWorker(QObject):
    progress = pyqtSignal(str, int, int, str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, root: str | Path):
        super().__init__()
        self.root = Path(root).expanduser()
        self._cancel = threading.Event()
        self._verifier = None

    @pyqtSlot()
    def run(self) -> None:
        try:
            from .pdi_verify import (
                PdiVerifier,
                pdi_delivery_report_output_directory,
                write_pdi_delivery_reports,
            )

            roots = self._verification_roots()
            completed: list[tuple[object, object]] = []
            cancelled = False
            for index, root in enumerate(roots, start=1):
                if self._cancel.is_set():
                    cancelled = True
                    break

                def on_progress(item, *, _index=index, _root=root) -> None:
                    prefix = f"第 {_index}/{len(roots)} 卷 · " if len(roots) > 1 else ""
                    self.progress.emit(
                        str(_root),
                        int(getattr(item, "current", 0)),
                        int(getattr(item, "total", 0)),
                        prefix + str(getattr(item, "message", "")),
                    )

                verifier = PdiVerifier(
                    root,
                    progress_callback=on_progress,
                    cancel_event=self._cancel,
                )
                self._verifier = verifier
                result = verifier.verify()
                if self._cancel.is_set() or str(
                    getattr(getattr(result, "status", ""), "value", "")
                ) == "cancelled":
                    cancelled = True
                    break
                report_directory = pdi_delivery_report_output_directory(
                    self.root,
                    root,
                    len(roots),
                )
                reports = write_pdi_delivery_reports(
                    result,
                    report_directory,
                )
                completed.append((result, reports))
            self.finished.emit(
                {"cancelled": cancelled or self._cancel.is_set(), "items": completed}
            )
        except Exception as exc:
            record_exception("PdiVerifyWorker.run", exc)
            self.failed.emit(str(exc))
        finally:
            self._verifier = None

    def request_cancel(self) -> None:
        self._cancel.set()
        verifier = self._verifier
        if verifier is not None:
            verifier.cancel()

    def _verification_roots(self) -> list[Path]:
        from .pdi_verify import discover_pdi_verification_roots

        return list(discover_pdi_verification_roots(self.root))


class DcmGetWindow(QMainWindow):
    external_activation_requested = pyqtSignal(object)

    def __init__(
        self,
        config_path: str | Path,
        project_root: str | Path,
        task_state_path: str | Path | None = None,
        *,
        offer_task_resume: bool = True,
        enable_multi_task: bool | None = None,
        profile_number: int | None = None,
        instance_label: str = "",
        settings_name: str = "DcmGet2",
        log_directory: str | Path | None = None,
        profile_lock: FileLock | None = None,
    ):
        super().__init__()
        self.config_path = Path(config_path)
        self.project_root = Path(project_root)
        self.profile_number = int(profile_number) if profile_number is not None else None
        self.profile_lock = profile_lock
        self.instance_label = instance_label.strip()
        self.settings_name = settings_name.strip() or "DcmGet2"
        self.log_directory = (
            Path(log_directory).expanduser() if log_directory is not None else None
        )
        self.instance_log_directory = self.log_directory
        self._active_task_log_directory: Path | None = None
        self._task_log_used_fallback = False
        self.config = load_config(self.config_path)
        self.resolver = DcmtkResolver(self.project_root)
        self.task_store = TaskCheckpointStore(task_state_path)
        self.task_ledger_path = self.task_store.path.with_name("task-ledger.sqlite3")
        self.task_ledger: TaskLedger | None = None
        self._task_ledger_error = ""
        self._last_acceptance_report: Path | None = None
        # Window-internal multi-task scheduling was retired. Each profile owns
        # one receiver/task process and users open another profile for another
        # independent task. Keep the argument only for source compatibility.
        self.multi_task_enabled = False
        self.task_controller = None
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
        self.pdi_verify_worker: PdiVerifyWorker | None = None
        self.pdi_verify_thread: QThread | None = None
        self.pdi_verify_dialog: QProgressDialog | None = None
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
        self._result_stats: dict[str, tuple[int, int, int, int]] = {}
        self._result_new_file_count = 0
        self._result_existing_skipped_count = 0
        self._result_conflict_preserved_count = 0
        self._result_failed_count = 0
        self._result_destination_directory: Path | None = None
        self._summary_processed = 0
        self._summary_files = 0
        self._summary_new_file_count = 0
        self._summary_existing_skipped_count = 0
        self._summary_conflict_preserved_count = 0
        self._summary_status_counts: dict[AccessionStatus, int] = {}
        self._workspace_task_summaries: dict[str, TaskSummary] = {}
        self._compact_action_layout: bool | None = None
        self._ui_running = False
        self._preflight_has_result = False
        self._preflight_has_error = False
        self._active_task_config: AppConfig | None = None
        self._quick_pdi_enabled = bool(self.config.pdi_export_enabled)
        self._quick_pdi_output_folder = self.config.pdi_output_folder.strip()
        self.settings_store = QSettings("DcmGet", self.settings_name)
        self._log_panel_expanded = self.settings_store.value(
            "window/log_expanded", False, type=bool
        )
        self._show_detailed_logs = self.settings_store.value(
            "window/log_detailed", False, type=bool
        )
        self._task_form_expanded = self.settings_store.value(
            "window/task_form_expanded", True, type=bool
        )
        self.external_activation_requested.connect(
            self._activate_from_external_launch
        )

        instance_title = f" - {self.instance_label}" if self.instance_label else ""
        self.setWindowTitle(
            f"DcmGet {__version__}{instance_title} - DICOM 下载工作台"
        )
        self.setMinimumSize(WINDOW_MINIMUM_WIDTH, WINDOW_MINIMUM_HEIGHT)
        self.resize(1180, 820)
        logo = self.project_root / "logo.png"
        if logo.exists():
            self.setWindowIcon(QIcon(str(logo)))
        self._build_ui()
        if self.task_workspace is not None:
            self.task_workspace.set_concurrency_limit(
                self.config.max_concurrent_moves
            )
        self._restore_ui_state()
        self.settings_page.set_config(self.config)
        self._reset_pdi_status_card()
        last_destination = self.settings_store.value("task/destination", "", type=str)
        self.destination_edit.setText(last_destination or self.config.dicom_destination_folder)
        self._sync_quick_pdi_controls_from_config()
        self._load_configured_accessions()
        self._render_task_phase()
        QTimer.singleShot(0, self._refresh_tool_status)
        QTimer.singleShot(0, self._focus_initial_task_control)
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
        self.task_workspace = None
        self.task_page = self.task_detail_page
        self.pages.addWidget(self.task_page)
        self.settings_page = SettingsPage()
        self.settings_page.set_multi_task_mode(self.multi_task_enabled)
        self.settings_page.saved.connect(self._save_settings)
        self.settings_page.back_requested.connect(self._cancel_settings)
        self.pages.addWidget(self.settings_page)
        root_layout.addWidget(self.pages, 1)
        self.setCentralWidget(root)
        if self.task_workspace is not None:
            self.task_workspace.show_detail()
        self.setStyleSheet(APP_STYLESHEET)
        for widget in (
            self.tool_status,
            self.entitlement_status,
            self.registration_button,
            self.release_notes_button,
            self.diagnostic_log_button,
            self.maintenance_button,
            self.instance_shortcut_button,
            self.settings_button,
        ):
            widget.setMinimumWidth(widget.sizeHint().width())
        self._update_header_responsive_layout(force=True)
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
        layout = QHBoxLayout(header)
        layout.setContentsMargins(24, 10, 24, 10)
        layout.setSpacing(8)

        self.app_logo = QLabel(header)
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
        layout.addWidget(self.app_logo, 0, Qt.AlignVCenter)

        self.compact_logo = QLabel(header)
        self.compact_logo.setAccessibleName("DcmGet 标志")
        self.compact_logo.setFixedSize(24, 24)
        self.compact_logo.setAlignment(Qt.AlignCenter)
        if logo_pixmap.isNull():
            self.compact_logo.hide()
        else:
            self.compact_logo.setPixmap(
                logo_pixmap.scaled(
                    24,
                    24,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            )
            self.compact_logo.hide()
        layout.addWidget(self.compact_logo, 0, Qt.AlignVCenter)

        self.app_title = QLabel(f"DcmGet {__version__}")
        self.app_title.setObjectName("AppTitle")
        self.app_title.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        layout.addWidget(self.app_title, 0, Qt.AlignVCenter)

        self.app_subtitle = QLabel()
        self.app_subtitle.setObjectName("HeaderChannel")
        self.app_subtitle.setAccessibleName("当前下载通道")
        self.app_subtitle.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        layout.addWidget(self.app_subtitle, 0, Qt.AlignVCenter)
        layout.addStretch(1)

        # Keep the original status widgets as a stable compatibility surface for
        # diagnostics and integrations.  The visible header presents their
        # combined state through one compact system indicator.
        self.tool_status = QLabel("正在检测 DCMTK…", header)
        self.tool_status.setObjectName("StatusPill")
        self.tool_status.setProperty("status", "pending")
        self.tool_status.setAccessibleName("DCMTK 工具状态")
        self.tool_status.hide()
        self.entitlement_status = QLabel(header)
        self.entitlement_status.setObjectName("StatusPill")
        self.entitlement_status.setAccessibleName("软件授权状态")
        self.entitlement_status.hide()
        self._tool_status_detail = "正在检测 DCMTK…"
        self._tool_status_level = "pending"

        self.system_status = QLabel("系统检测中")
        self.system_status.setObjectName("StatusPill")
        self.system_status.setProperty("status", "pending")
        self.system_status.setAccessibleName("系统状态")
        self.system_status.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        layout.addWidget(self.system_status, 0, Qt.AlignVCenter)

        self.header_settings_button = QToolButton(header)
        self.header_settings_button.setText("设置")
        self.header_settings_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.header_settings_button.setIcon(
            self.style().standardIcon(QStyle.SP_FileDialogDetailedView)
        )
        self.header_settings_button.setToolTip(
            "连接、接收、匿名与 PDI 设置（Ctrl+,）"
        )
        self.header_settings_button.clicked.connect(self._show_settings)
        layout.addWidget(self.header_settings_button, 0, Qt.AlignVCenter)

        self.more_button = QToolButton(header)
        self.more_button.setText("更多")
        self.more_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.more_button.setArrowType(Qt.DownArrow)
        self.more_button.setPopupMode(QToolButton.InstantPopup)
        self.more_button.setAccessibleName("更多功能")
        self.more_menu = QMenu(self.more_button)
        self.license_status_action = self.more_menu.addAction("授权状态")
        self.license_status_action.setEnabled(False)
        self.registration_action = self.more_menu.addAction(
            "软件注册", self._show_activation
        )
        self.more_menu.addSeparator()
        self.release_notes_action = self.more_menu.addAction(
            "版本说明", self._show_release_notes
        )
        self.diagnostic_log_action = self.more_menu.addAction(
            "诊断日志", self._open_diagnostic_log_directory
        )

        self.registration_button = QToolButton(header)
        self.registration_button.setText("软件注册")
        self.registration_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.registration_button.clicked.connect(self._show_activation)
        self.registration_button.setFocusPolicy(Qt.NoFocus)
        self.registration_button.hide()
        self.release_notes_button = QToolButton(header)
        self.release_notes_button.setText("版本说明")
        self.release_notes_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.release_notes_button.clicked.connect(self._show_release_notes)
        self.release_notes_button.setFocusPolicy(Qt.NoFocus)
        self.release_notes_button.hide()
        self.diagnostic_log_button = QToolButton(header)
        self.diagnostic_log_button.setText("诊断日志")
        self.diagnostic_log_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.diagnostic_log_button.setToolTip("打开启动、异常和崩溃诊断日志目录")
        self.diagnostic_log_button.clicked.connect(
            self._open_diagnostic_log_directory
        )
        self.diagnostic_log_button.setFocusPolicy(Qt.NoFocus)
        self.diagnostic_log_button.hide()
        self.maintenance_button = QToolButton(header)
        self.maintenance_button.setText("运维工具")
        self.maintenance_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.maintenance_button.setPopupMode(QToolButton.InstantPopup)
        self.maintenance_menu = QMenu("运维工具", self.more_menu)
        self.maintenance_menu.addAction("运行健康检查", self._run_health_check)
        self.maintenance_menu.addAction("生成脱敏支持包", self._create_support_bundle)
        self.maintenance_menu.addSeparator()
        self.maintenance_menu.addAction("验证 PDI/U盘", self._verify_existing_pdi)
        self.maintenance_menu.addSeparator()
        self.maintenance_menu.addAction("管理 Profile", self._manage_profiles)
        self.maintenance_menu.addAction(
            "备份全部 Profile 配置与显示名", self._backup_profiles
        )
        self.maintenance_menu.addAction(
            "恢复 Profile 配置与显示名", self._restore_profiles
        )
        self.maintenance_button.setMenu(self.maintenance_menu)
        self.maintenance_button.setFocusPolicy(Qt.NoFocus)
        self.maintenance_button.hide()
        self.more_menu.addMenu(self.maintenance_menu)

        self.instance_shortcut_button = QToolButton(header)
        self.instance_shortcut_button.setText("实例快捷方式")
        self.instance_shortcut_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.instance_shortcut_button.setToolTip(
            "在桌面创建固定打开当前实例的快捷方式"
        )
        self.instance_shortcut_button.setAccessibleName("创建当前实例桌面快捷方式")
        self.instance_shortcut_button.clicked.connect(
            self._create_current_instance_shortcut
        )
        self.instance_shortcut_button.setFocusPolicy(Qt.NoFocus)
        self.instance_shortcut_button.hide()
        self.instance_shortcut_action = self.more_menu.addAction(
            "创建实例快捷方式", self._create_current_instance_shortcut
        )
        self.instance_shortcut_action.setVisible(self.profile_number is not None)

        # Retained for source compatibility with existing integrations.  The
        # visible settings control is header_settings_button.
        self.settings_button = QToolButton(header)
        self.settings_button.setText("设置")
        self.settings_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.settings_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogDetailedView))
        self.settings_button.setToolTip("连接、接收、匿名与 PDI 设置（Ctrl+,）")
        self.settings_button.clicked.connect(self._show_settings)
        self.settings_button.setFocusPolicy(Qt.NoFocus)
        self.settings_button.hide()

        self.more_button.setMenu(self.more_menu)
        layout.addWidget(self.more_button, 0, Qt.AlignVCenter)
        self._refresh_entitlement_status()
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
        layout.setAlignment(Qt.AlignTop)

        self.input_card = QFrame()
        self.input_card.setObjectName("Card")
        self.input_card.setSizePolicy(
            QSizePolicy.Preferred, QSizePolicy.Maximum
        )
        input_layout = QVBoxLayout(self.input_card)
        input_header = QHBoxLayout()
        self.task_section_title = QLabel("新建下载任务")
        self.task_section_title.setObjectName("SectionTitle")
        input_header.addWidget(self.task_section_title)
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
        self.accession_edit.setMaximumHeight(120)
        self.accession_edit.textChanged.connect(self._update_accession_preview)
        self.accession_edit.file_dropped.connect(self._load_accession_file)
        self.accession_edit.large_text_pasted.connect(self._apply_large_accession_text)
        grid.addWidget(self.accession_edit, 0, 1, 1, 2)
        self.accession_button = QPushButton("导入 TXT/CSV/Excel")
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
        self.destination_error_label = QLabel()
        self.destination_error_label.setObjectName("InlineErrorText")
        self.destination_error_label.setWordWrap(True)
        self.destination_error_label.hide()
        grid.addWidget(self.destination_error_label, 3, 1, 1, 3)

        self.quick_pdi_checkbox = QCheckBox("本次任务生成 PDI 便携阅片包")
        self.quick_pdi_checkbox.setAccessibleName("下载完成后生成 PDI 便携目录")
        self.quick_pdi_checkbox.setSizePolicy(
            QSizePolicy.Maximum, QSizePolicy.Preferred
        )
        self.quick_pdi_checkbox.setToolTip(
            "仅影响本次下载任务；后续任务默认值请在设置中修改"
        )
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
        layout.addWidget(self.input_card)

        self.recovery_card = QFrame()
        self.recovery_card.setObjectName("RecoveryCard")
        recovery_layout = QVBoxLayout(self.recovery_card)
        recovery_layout.setContentsMargins(16, 14, 16, 14)
        recovery_layout.setSpacing(8)
        self.recovery_title_label = QLabel("发现可继续的任务")
        self.recovery_title_label.setObjectName("SectionTitle")
        recovery_layout.addWidget(self.recovery_title_label)
        self.recovery_summary_label = QLabel()
        self.recovery_summary_label.setObjectName("ProgressText")
        self.recovery_summary_label.setWordWrap(True)
        recovery_layout.addWidget(self.recovery_summary_label)
        self.recovery_detail_label = QLabel()
        self.recovery_detail_label.setObjectName("FieldHint")
        self.recovery_detail_label.setWordWrap(True)
        recovery_layout.addWidget(self.recovery_detail_label)
        self.recovery_action_layout = QHBoxLayout()
        self.recovery_action_layout.addStretch()
        self.recovery_continue_button = QPushButton("继续任务")
        self.recovery_continue_button.setObjectName("PrimaryButton")
        self.recovery_continue_button.clicked.connect(
            self._continue_recovery_task
        )
        self.recovery_action_layout.addWidget(self.recovery_continue_button)
        recovery_layout.addLayout(self.recovery_action_layout)
        self.recovery_card.hide()
        layout.addWidget(self.recovery_card)

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
        self.recovery_action_layout.insertWidget(
            1, self.discard_resume_button
        )
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

        self.task_result_summary = QLabel(
            "结果：新增 0 · 已存在跳过 0 · 冲突保留 0 · 失败 0"
        )
        self.task_result_summary.setObjectName("ProgressText")
        self.task_result_summary.setWordWrap(True)
        self.task_result_summary.setAccessibleName("任务结果统计")
        layout.addWidget(self.task_result_summary)

        self.result_shortcuts_widget = QWidget()
        result_shortcuts = QHBoxLayout(self.result_shortcuts_widget)
        result_shortcuts.setContentsMargins(0, 0, 0, 0)
        result_shortcuts.setSpacing(8)
        result_shortcuts.addWidget(QLabel("结果与日志"))
        self.open_destination_button = QPushButton("打开影像目录")
        self.open_destination_button.clicked.connect(
            self._open_destination_directory
        )
        self.open_task_log_button = QPushButton("打开任务日志")
        self.open_task_log_button.clicked.connect(self._open_task_log_directory)
        self.open_acceptance_report_button = QPushButton("打开验收报告")
        self.open_acceptance_report_button.setEnabled(False)
        self.open_acceptance_report_button.clicked.connect(
            self._open_acceptance_report
        )
        self.open_conflict_button = QPushButton("打开冲突目录")
        self.open_conflict_button.clicked.connect(self._open_conflict_directory)
        for button in (
            self.open_destination_button,
            self.open_task_log_button,
            self.open_acceptance_report_button,
            self.open_conflict_button,
        ):
            result_shortcuts.addWidget(button)
        result_shortcuts.addStretch()
        layout.addWidget(self.result_shortcuts_widget)
        self.destination_edit.textChanged.connect(
            self._on_destination_path_changed
        )
        self._update_result_shortcut_state()

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
        if hasattr(self, "header_settings_button"):
            self._update_header_responsive_layout()
        if hasattr(self, "task_action_layout"):
            self._update_responsive_layouts()

    def _update_header_responsive_layout(self, *, force: bool = False) -> None:
        compact = self.width() < 760
        if not force and compact == getattr(self, "_header_compact", None):
            return
        self._header_compact = compact

        logo_available = bool(
            self.app_logo.pixmap() is not None
            and not self.app_logo.pixmap().isNull()
        )
        self.app_logo.setVisible(logo_available and not compact)
        self.compact_logo.setVisible(logo_available and compact)
        self.app_title.setText("DcmGet" if compact else f"DcmGet {__version__}")

        channel_name = self.instance_label or "默认"
        full_channel = f"下载通道：{channel_name}"
        channel_width = 120 if compact else 240
        channel_prefix = "通道：" if compact else "下载通道："
        channel_text = self.app_subtitle.fontMetrics().elidedText(
            channel_prefix + channel_name,
            Qt.ElideRight,
            channel_width,
        )
        self.app_subtitle.setText(channel_text)
        self.app_subtitle.setToolTip(full_channel)
        self.app_subtitle.setMaximumWidth(channel_width)
        self._refresh_compact_system_status()

    def _focus_initial_task_control(self) -> None:
        if (
            self.pages.currentWidget() is self.task_page
            and self.start_button.isVisible()
            and self.start_button.isEnabled()
        ):
            self.start_button.setFocus(Qt.OtherFocusReason)
        elif (
            self.pages.currentWidget() is self.task_page
            and self.accession_edit.isVisible()
            and not self.accession_edit.isReadOnly()
        ):
            self.accession_edit.setFocus(Qt.OtherFocusReason)

    def _refresh_compact_system_status(self) -> None:
        if not hasattr(self, "system_status"):
            return
        entitlement = self.entitlement_status.text() or "授权状态未知"
        registered = entitlement.startswith("已注册")
        level = getattr(self, "_tool_status_level", "pending")
        compact = bool(getattr(self, "_header_compact", False))
        if level == "error":
            text = "异常" if compact else "系统异常"
            display_level = "error"
        elif level == "ok" and not registered:
            text = "正常 · 试用" if compact else "系统正常 · 试用"
            display_level = "warning"
        elif level == "ok":
            text = "正常" if compact else "系统正常"
            display_level = "ok"
        else:
            text = "检测中" if compact else "系统检测中"
            display_level = "pending"
        detail = getattr(self, "_tool_status_detail", self.tool_status.text())
        tooltip = f"{detail}\n{entitlement}"
        self.system_status.setText(text)
        self.system_status.setToolTip(tooltip)
        self.system_status.setAccessibleDescription(tooltip)
        self.system_status.setProperty("status", display_level)
        self.system_status.style().unpolish(self.system_status)
        self.system_status.style().polish(self.system_status)
        self.system_status.updateGeometry()

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
        self.log_scope_combo.setVisible(self.multi_task_enabled)
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
        open_logs = QPushButton("任务日志")
        open_logs.setToolTip("打开当前任务的完整磁盘日志目录")
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
                self.pdi_verify_worker,
                self.pdi_verify_thread,
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
        return (
            self.task_workspace.selected_task_id
            if self.task_workspace is not None
            else ""
        )

    def _publish_workspace_summary(
        self,
        summary: TaskSummary,
        *,
        select: bool = False,
    ) -> None:
        self._workspace_task_summaries[summary.task_id] = summary
        if self.task_workspace is None:
            return
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
            error_message=(
                checkpoint.interrupted_reason
                or (current.error_message if current is not None else "")
            ),
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
            error_message=(
                batch.interrupted_reason
                or (current.error_message if current is not None else "")
            ),
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
        if self.task_workspace is not None:
            self.task_workspace.show_detail()
        if self.multi_task_enabled and self.task_workspace is not None:
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
        if self.task_workspace is not None:
            self.task_workspace.show_detail()
        if self.multi_task_enabled and self.task_workspace is not None and task_id:
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
        settings_config = AppConfig.from_dict(self.config.to_dict())
        if self._pdi_task_id and self._active_task_config is not None:
            for field_name in PDI_TASK_SETTING_FIELDS:
                setattr(
                    settings_config,
                    field_name,
                    getattr(self._active_task_config, field_name),
                )
        self.settings_page.set_config(settings_config)
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

    def _create_current_instance_shortcut(self) -> None:
        if self.profile_number is None:
            return
        default_name = default_instance_shortcut_name(
            self.config.storage_port,
            self.config.storage_ae_title,
        )
        name, accepted = QInputDialog.getText(
            self,
            "创建实例快捷方式",
            (
                f"为 {self.instance_label or f'实例 {self.profile_number}'} 创建桌面快捷方式。\n"
                f"它会固定使用启动参数 --profile {self.profile_number}，下次直接打开本实例。\n\n"
                "快捷方式名称："
            ),
            QLineEdit.Normal,
            default_name,
        )
        if not accepted:
            return
        desktop_text = QStandardPaths.writableLocation(QStandardPaths.DesktopLocation)
        desktop = Path(desktop_text) if desktop_text else Path.home() / "Desktop"
        try:
            shortcut = create_instance_shortcut(
                self.profile_number,
                name,
                desktop,
                project_root=self.project_root,
            )
        except ShortcutExistsError as exc:
            overwrite = QMessageBox.question(
                self,
                "快捷方式已存在",
                f"桌面上已存在同名快捷方式：\n{exc.path}\n\n是否替换？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if overwrite != QMessageBox.Yes:
                return
            try:
                shortcut = create_instance_shortcut(
                    self.profile_number,
                    name,
                    desktop,
                    project_root=self.project_root,
                    overwrite=True,
                )
            except InstanceShortcutError as retry_exc:
                QMessageBox.critical(self, "创建快捷方式失败", str(retry_exc))
                return
        except InstanceShortcutError as exc:
            QMessageBox.critical(self, "创建快捷方式失败", str(exc))
            return
        QMessageBox.information(
            self,
            "快捷方式已创建",
            (
                f"已创建：\n{shortcut}\n\n"
                f"此快捷方式固定打开实例 {self.profile_number}。"
                "如果移动便携版程序文件，需重新创建快捷方式。"
            ),
        )

    @pyqtSlot(object)
    def _activate_from_external_launch(self, _payload: object = None) -> None:
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        QApplication.alert(self, 1200)

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
        if hasattr(self, "license_status_action"):
            self.license_status_action.setText(f"授权：{text}")
            self.registration_action.setText(
                "管理软件授权" if text.startswith("已注册") else "输入注册码"
            )
        self._refresh_compact_system_status()

    def _save_settings(self, config: AppConfig) -> None:
        config.access_numbers_file_path = self.config.access_numbers_file_path
        config.dicom_destination_folder = self.destination_edit.text().strip()
        if self._pdi_task_id:
            lease_was_held = self.task_store.lease_held
            lease_acquired = lease_was_held or self.task_store.try_acquire_lease()
            if lease_acquired:
                try:
                    checkpoint = self.task_store.load_required(
                        include_archived_files=False
                    )
                    if checkpoint.task_id != self._pdi_task_id:
                        raise TaskStateError("PDI 恢复任务已改变")
                    task_config = AppConfig.from_dict(
                        checkpoint.config.to_dict()
                    )
                    for field_name in PDI_TASK_SETTING_FIELDS:
                        setattr(task_config, field_name, getattr(config, field_name))
                    self.task_store.update_config(
                        self._pdi_task_id,
                        task_config,
                    )
                    self._active_task_config = task_config
                except TaskStateError as exc:
                    QMessageBox.warning(
                        self,
                        "PDI 任务设置未更新",
                        f"{exc}\n\nProfile 默认设置仍会保存。",
                    )
                finally:
                    if not lease_was_held:
                        self.task_store.release_lease()
            else:
                QMessageBox.warning(
                    self,
                    "PDI 任务正在使用",
                    "另一个 DcmGet 实例正在使用该恢复任务；"
                    "本次只保存 Profile 默认设置。",
                )
        self.config = config
        save_config(self.config_path, self.config)
        if self.task_workspace is not None:
            self.task_workspace.set_concurrency_limit(
                self.config.max_concurrent_moves
            )
        self._sync_quick_pdi_controls_from_config(self._effective_task_config())
        self.pages.setCurrentIndex(0)
        self.pdi_status_card.setVisible(
            self._effective_task_config().pdi_export_enabled
        )
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
        self._render_task_phase()

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
        return False

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
        self.header_settings_button.setEnabled(True)
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
        self._tool_status_detail = text
        self._tool_status_level = status
        self.tool_status.setText(text)
        self.tool_status.setToolTip(text)
        self.tool_status.setAccessibleDescription(text)
        self.tool_status.setProperty("status", status)
        self.tool_status.style().unpolish(self.tool_status)
        self.tool_status.style().polish(self.tool_status)
        self.tool_status.setMinimumWidth(self.tool_status.sizeHint().width())
        self.tool_status.updateGeometry()
        self._refresh_compact_system_status()

    def _load_configured_accessions(self) -> None:
        path = Path(self.config.access_numbers_file_path).expanduser()
        if not path.is_absolute():
            path = self.project_root / path
        if not path.exists():
            return
        has_saved_column, saved_column = self._saved_accession_column(path)
        try:
            result = import_accession_file(
                path,
                column=saved_column if has_saved_column else None,
            )
        except ColumnSelectionError as exc:
            self._show_configured_accession_import_issue(
                path,
                f"{exc}。请点击“导入 TXT/CSV/Excel”并明确选择检查号列。",
            )
            return
        except AccessionImportError as exc:
            self._show_configured_accession_import_issue(path, str(exc))
            return
        if not has_saved_column and len(result.available_columns) > 1:
            self._show_configured_accession_import_issue(
                path,
                "表格包含多列，不能安全判断检查号列。"
                "请点击“导入 TXT/CSV/Excel”并明确选择。",
            )
            return
        self._apply_accession_import(str(path), result)

    @staticmethod
    def _canonical_accession_path(path: str | Path) -> str:
        return str(Path(path).expanduser().resolve(strict=False))

    def _saved_accession_column(
        self,
        path: str | Path,
    ) -> tuple[bool, str | int | None]:
        stored_path = self.settings_store.value(
            ACCESSION_IMPORT_PATH_KEY,
            "",
            type=str,
        )
        if (
            not stored_path
            or self._canonical_accession_path(stored_path)
            != self._canonical_accession_path(path)
        ):
            return False, None
        column_name = self.settings_store.value(
            ACCESSION_IMPORT_COLUMN_NAME_KEY,
            "",
            type=str,
        ).strip()
        if column_name:
            return True, column_name
        raw_index = self.settings_store.value(
            ACCESSION_IMPORT_COLUMN_INDEX_KEY,
            "",
        )
        try:
            return True, int(raw_index)
        except (TypeError, ValueError):
            return False, None

    def _remember_accession_column(
        self,
        path: str | Path,
        result: AccessionImportResult,
    ) -> None:
        self.settings_store.setValue(
            ACCESSION_IMPORT_PATH_KEY,
            self._canonical_accession_path(path),
        )
        if result.selected_column is None:
            self.settings_store.remove(ACCESSION_IMPORT_COLUMN_NAME_KEY)
            self.settings_store.remove(ACCESSION_IMPORT_COLUMN_INDEX_KEY)
        else:
            self.settings_store.setValue(
                ACCESSION_IMPORT_COLUMN_NAME_KEY,
                result.selected_column.name,
            )
            self.settings_store.setValue(
                ACCESSION_IMPORT_COLUMN_INDEX_KEY,
                result.selected_column.index,
            )
        self.settings_store.sync()

    def _show_configured_accession_import_issue(
        self,
        path: Path,
        message: str,
    ) -> None:
        self.accession_edit.setPlaceholderText(
            "未自动载入检查号文件；请重新导入并确认文件格式或检查号列"
        )
        self.accession_summary.setText("检查号文件需要确认，尚未载入")
        self._append_log(
            "应用",
            f"未自动载入检查号文件 {path}：{message}",
            "warning",
        )

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
        try:
            self._ensure_task_ledger_batch(checkpoint)
        except TaskLedgerError as exc:
            self.task_store.release_lease()
            self._append_log("验收", str(exc), "error")
            QMessageBox.warning(
                self,
                "无法恢复任务台账",
                f"{exc}\n\n恢复记录已保留，修复台账目录权限后可再次启动。",
            )
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
            if not retryable and not pending:
                self._append_log("恢复", "恢复点中没有未处理或失败项", "error")
                self._hold_download_resume(
                    checkpoint, "恢复点需要人工处理；可明确放弃后新建任务"
                )
                return
            safety_reason = checkpoint.interrupted_reason
            self._hold_download_resume(
                checkpoint,
                (
                    f"任务因安全保护暂停，尚有 {len(pending):,} 项待处理"
                    if safety_reason
                    else f"已保留 {len(retryable):,} 个失败项，可继续重试"
                ),
            )
            return
        if not pending:
            try:
                self.task_store.clear(checkpoint.task_id)
            except TaskStateError as exc:
                self._append_log("恢复", str(exc), "error")
            self.task_store.release_lease()
            return

        self._hold_download_resume(
            checkpoint,
            f"已保留未完成任务，剩余 {len(pending):,} 个检查号",
        )

    def _apply_checkpoint_config(self, checkpoint: TaskCheckpoint) -> None:
        self._active_task_config = AppConfig.from_dict(checkpoint.config.to_dict())
        task_config = self._active_task_config
        self._result_destination_directory = Path(
            task_config.dicom_destination_folder
        ).expanduser()
        self._active_task_log_directory = self._task_log_directory_for_config(
            task_config
        )
        self._task_log_used_fallback = False
        self.settings_page.set_config(task_config)
        self.destination_edit.setText(task_config.dicom_destination_folder)
        self._sync_quick_pdi_controls_from_config()
        self._display_total = len(checkpoint.accessions)
        if len(checkpoint.accessions) <= TASK_TABLE_DETAIL_LIMIT:
            self._hidden_accession_count = 0
            self.accession_edit.setPlaceholderText("每行一个检查号")
            self.accession_edit.setPlainText("\n".join(checkpoint.accessions))
            for result in [
                *checkpoint.results,
                *checkpoint.partial_results.values(),
            ]:
                self._set_result_row(result)
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
        self._update_task_result_summary(
            [*checkpoint.results, *checkpoint.partial_results.values()]
        )
        self._update_result_shortcut_state()

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
        self.last_summary = BatchSummary(
            list(checkpoint.results),
            interrupted_reason=checkpoint.interrupted_reason,
        )
        self.progress_label.setText(message)
        self.task_store.release_lease()
        self._set_running(False)
        self._show_recovery_card(checkpoint)

    def _show_recovery_card(self, checkpoint: TaskCheckpoint) -> None:
        self._set_task_form_expanded(False)
        pending_count = len(checkpoint.pending_accessions)
        failed_count = sum(
            result.status in {AccessionStatus.FAILED, AccessionStatus.PARTIAL}
            for result in checkpoint.results
        )
        file_count = sum(
            result.file_count
            for result in [
                *checkpoint.results,
                *checkpoint.partial_results.values(),
            ]
        )
        processed_count = len(checkpoint.results)
        total_count = len(checkpoint.accessions)
        is_pdi = checkpoint.phase in {
            "pdi_pending",
            "pdi_running",
            "pdi_retryable",
        }
        safety_paused = bool(checkpoint.interrupted_reason)
        self.recovery_title_label.setText(
            "PDI 阅片包待继续"
            if is_pdi
            else "任务已安全暂停"
            if safety_paused
            else "发现可继续的下载任务"
        )
        self.recovery_summary_label.setText(
            f"已处理 {processed_count:,}/{total_count:,} · "
            f"剩余 {pending_count:,} · 文件 {file_count:,} · "
            f"失败或部分成功 {failed_count:,}"
        )
        detail_parts = []
        if checkpoint.interrupted_reason:
            detail_parts.append(checkpoint.interrupted_reason)
        detail_parts.append(
            f"保存目录：{checkpoint.config.dicom_destination_folder or '未设置'}"
        )
        if is_pdi:
            detail_parts.append("继续后只生成阅片包，不会重新下载影像。")
        else:
            detail_parts.append("继续后只处理剩余或失败项，已完成项不会重复请求。")
        self.recovery_detail_label.setText("\n".join(detail_parts))
        self.recovery_continue_button.setText(
            "继续生成阅片包"
            if is_pdi
            else "问题已修复，继续"
            if safety_paused
            else "只重试失败项"
            if checkpoint.phase == "download_retryable" and not pending_count
            else "继续任务"
        )
        self.recovery_continue_button.setEnabled(True)
        self.recovery_card.show()

    def _continue_recovery_task(self) -> None:
        if self._is_busy():
            return
        if self._resume_checkpoint is not None:
            checkpoint = self._resume_checkpoint
            self._append_log(
                "恢复",
                f"准备继续任务：剩余 {len(checkpoint.pending_accessions):,}/"
                f"{len(checkpoint.accessions):,}",
                "info",
            )
            self._start_download(resume_checkpoint=checkpoint)
            return
        if self._pdi_task_id:
            self._append_log("恢复", "准备继续生成 PDI 阅片包", "info")
            self._start_pdi_export()

    def _offer_pdi_resume(
        self,
        checkpoint: TaskCheckpoint,
        archived_count: int,
    ) -> None:
        self._accepted_partial_results = any(
            result.status in {AccessionStatus.FAILED, AccessionStatus.PARTIAL}
            for result in checkpoint.results
        )
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
        self._pdi_source_files = []
        self._set_pdi_status("PDI 恢复记录已保留，可继续生成", "warning")
        self.pdi_retry_button.setEnabled(bool(archived_count))
        self.task_store.release_lease()
        self._set_running(False)
        self._show_recovery_card(checkpoint)

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
            "检查号文件 (*.txt *.csv *.xlsx);;文本文件 (*.txt);;"
            "CSV 文件 (*.csv);;Excel 文件 (*.xlsx)",
        )
        if selected:
            self._load_accession_file(selected)

    def _load_accession_file(self, path: str) -> None:
        try:
            result = import_accession_file(path)
        except ColumnSelectionError as exc:
            selected = self._choose_accession_column(exc.columns)
            if selected is None:
                return
            try:
                result = import_accession_file(path, column=selected)
            except AccessionImportError as retry_exc:
                QMessageBox.critical(self, "无法导入检查号", str(retry_exc))
                return
        except AccessionImportError as exc:
            QMessageBox.critical(self, "无法导入检查号", str(exc))
            return

        if len(result.available_columns) > 1 and result.selected_column is not None:
            selected = self._choose_accession_column(
                result.available_columns,
                current=result.selected_column.index,
            )
            if selected is None:
                return
            if selected != result.selected_column.index:
                try:
                    result = import_accession_file(path, column=selected)
                except AccessionImportError as exc:
                    QMessageBox.critical(self, "无法导入检查号", str(exc))
                    return
        self._apply_accession_import(path, result)

    def _choose_accession_column(
        self,
        columns: tuple[object, ...],
        *,
        current: int = 0,
    ) -> int | None:
        if not columns:
            QMessageBox.warning(
                self,
                "未找到检查号列",
                "表格没有可选择的列，请确认第一行是列名。",
            )
            return None
        labels = [
            f"{getattr(column, 'name', '') or '未命名列'}（第 {getattr(column, 'index', 0) + 1} 列）"
            for column in columns
        ]
        current_row = next(
            (
                index
                for index, column in enumerate(columns)
                if getattr(column, "index", -1) == current
            ),
            0,
        )
        choice, accepted = QInputDialog.getItem(
            self,
            "选择检查号列",
            "请选择包含检查号（Accession Number）的列：",
            labels,
            current_row,
            False,
        )
        if not accepted:
            return None
        return int(getattr(columns[labels.index(choice)], "index", 0))

    def _apply_accession_import(
        self,
        path: str,
        result: AccessionImportResult,
    ) -> None:
        values = list(result.values)
        if len(values) <= TASK_TABLE_DETAIL_LIMIT:
            self.accession_edit.hidden_accessions = []
            self.accession_edit.setPlainText("\n".join(values))
        else:
            previous = self.accession_edit.blockSignals(True)
            self.accession_edit.clear()
            self.accession_edit.setPlaceholderText(
                f"已导入 {len(values):,} 个检查号；为保持界面流畅，明细已隐藏"
            )
            self.accession_edit.blockSignals(previous)
            self.accession_edit.hidden_accessions = list(values)
            self.current_accessions = values
            self._hidden_accession_count = len(values)
            self.invalid_accessions = result.invalid_values
            self._populate_waiting_rows(values)
            self._update_task_form_summary()
        self.invalid_accessions = result.invalid_values
        self.accession_summary.setText(
            f"有效 {result.valid_count:,} · 空行 {result.blank_count:,} · "
            f"重复 {result.duplicate_count:,} · 无效 {result.invalid_count:,}"
        )
        self._render_task_phase()
        self.config.access_numbers_file_path = path
        self._remember_accession_column(path, result)
        column = (
            f"，列：{result.selected_column.name}"
            if result.selected_column is not None
            else ""
        )
        self._append_log(
            "应用",
            f"已导入检查号文件：{path}{column}，有效 {result.valid_count:,} 条",
            "info",
        )

    def _choose_destination(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "选择 DICOM 保存目录", self.destination_edit.text()
        )
        if selected:
            self.destination_edit.setText(selected)

    def _sync_quick_pdi_controls_from_config(
        self, source: AppConfig | None = None
    ) -> None:
        source = source or self._effective_task_config()
        self._quick_pdi_enabled = bool(source.pdi_export_enabled)
        self._quick_pdi_output_folder = source.pdi_output_folder.strip()
        previous = self.quick_pdi_checkbox.blockSignals(True)
        self.quick_pdi_checkbox.setChecked(self._quick_pdi_enabled)
        self.quick_pdi_checkbox.blockSignals(previous)
        self._update_quick_pdi_summary()

    def _update_quick_pdi_summary(self, _value=None) -> None:
        configured = self._quick_pdi_output_folder
        destination = self.destination_edit.text().strip()
        if configured:
            path_text = configured
            summary = f"本次保存位置：{configured}"
        elif destination:
            path_text = str(Path(destination).expanduser() / "PDI")
            summary = f"本次保存位置：{path_text}（随影像目录）"
        else:
            path_text = "DICOM 保存目录下的 PDI（默认）"
            summary = f"本次保存位置：{path_text}"
        self.quick_pdi_output_label.setText(summary)
        self.quick_pdi_output_label.setToolTip(path_text)
        self.quick_pdi_output_label.setAccessibleDescription(path_text)
        enabled = self.quick_pdi_checkbox.isChecked()
        self.quick_pdi_output_label.setVisible(enabled)
        self.quick_pdi_output_button.setVisible(enabled)
        recovery_pending = bool(self._resume_checkpoint or self._pdi_task_id)
        self.quick_pdi_output_button.setEnabled(
            enabled
            and not self._is_busy()
            and not recovery_pending
        )

    def _on_quick_pdi_toggled(self, enabled: bool) -> None:
        if self._is_busy() or self._resume_checkpoint or self._pdi_task_id:
            self._sync_quick_pdi_controls_from_config()
            return
        self._quick_pdi_enabled = enabled
        self._update_quick_pdi_summary()
        self._append_log(
            "应用",
            (
                "本次任务将生成 PDI 便携阅片包"
                if enabled
                else "本次任务不生成 PDI 便携阅片包"
            ),
            "info",
        )

    def _choose_quick_pdi_output(self) -> None:
        initial = (
            self._quick_pdi_output_folder
            or self.destination_edit.text().strip()
            or str(Path.home())
        )
        selected = QFileDialog.getExistingDirectory(
            self, "选择 PDI 保存目录", initial
        )
        if not selected:
            return
        self._quick_pdi_output_folder = selected
        self._update_quick_pdi_summary()
        self._append_log("应用", f"本次 PDI 保存目录：{selected}", "info")

    def _open_destination_directory(self) -> None:
        path = self._destination_directory()
        if path is None:
            QMessageBox.warning(self, "无法打开影像目录", "请先选择已存在的保存目录。")
            return
        if path.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))
            return
        QMessageBox.warning(self, "无法打开影像目录", "请先选择已存在的保存目录。")

    def _on_destination_path_changed(self, _text: str = "") -> None:
        if (
            not self._is_busy()
            and not self._result_stats
            and self.last_summary is None
            and self._resume_checkpoint is None
        ):
            self._active_task_log_directory = None
            self._task_log_used_fallback = False
        self._update_result_shortcut_state()

    def _destination_directory(self) -> Path | None:
        if self._result_destination_directory is not None:
            return self._result_destination_directory
        value = self.destination_edit.text().strip()
        return Path(value).expanduser() if value else None

    def _task_log_directory_for_config(self, config: AppConfig) -> Path:
        if config.anonymization_enabled and self.instance_log_directory is not None:
            return self.instance_log_directory
        return log_directory(config)

    def _get_task_ledger(self) -> TaskLedger:
        if self.task_ledger is not None:
            return self.task_ledger
        try:
            self.task_ledger = TaskLedger(self.task_ledger_path)
        except TaskLedgerError as exc:
            self._task_ledger_error = str(exc)
            raise
        self._task_ledger_error = ""
        return self.task_ledger

    def _ensure_task_ledger_batch(self, checkpoint: TaskCheckpoint) -> None:
        ledger = self._get_task_ledger()
        try:
            ledger.load_batch(checkpoint.task_id)
            return
        except TaskLedgerError as exc:
            if "不存在当前批次" not in str(exc):
                raise
        profile_name = self.instance_label or (
            f"Profile {self.profile_number}" if self.profile_number is not None else ""
        )
        ledger.create_batch(
            checkpoint.accessions,
            batch_id=checkpoint.task_id,
            profile_name=profile_name,
            anonymization_requested=checkpoint.config.anonymization_enabled,
            pdi_requested=checkpoint.config.pdi_export_enabled,
        )

    def _acceptance_report_directory(self, task_id: str) -> Path:
        destination = self._destination_directory()
        if destination is None:
            destination = self.task_ledger_path.parent
        return destination / "_DcmGetReports" / f"task-{task_id[:8]}"

    def _export_acceptance_report(self, task_id: str) -> None:
        if not task_id:
            return
        try:
            paths = self._get_task_ledger().export_reports(
                task_id,
                self._acceptance_report_directory(task_id),
            )
        except (OSError, TaskLedgerError) as exc:
            self._append_log("验收", f"验收报告生成失败：{exc}", "error")
            return
        self._last_acceptance_report = paths.html_path
        self._append_log(
            "验收",
            f"已生成脱敏验收报告：{paths.html_path}",
            "success",
        )
        self._update_result_shortcut_state()

    def _complete_task_ledger(
        self,
        task_id: str,
        status: object,
        *,
        pdi_result: object | None = None,
    ) -> None:
        if not task_id:
            return
        try:
            ledger = self._get_task_ledger()
            if pdi_result is not None:
                ledger.record_pdi_result(
                    task_id,
                    getattr(pdi_result, "status", "unknown"),
                    output_directory=str(
                        getattr(pdi_result, "output_directory", "") or ""
                    ),
                    message=str(getattr(pdi_result, "message", "") or ""),
                )
            ledger.complete_batch(task_id, status)
        except TaskLedgerError as exc:
            self._append_log("验收", f"任务台账更新失败：{exc}", "error")
        self._export_acceptance_report(task_id)

    def _expected_task_log_directory(self) -> Path | None:
        task_config = self._effective_task_config()
        if task_config.anonymization_enabled:
            return self._task_log_directory_for_config(task_config)
        destination = self._destination_directory()
        return destination / "_DcmGetLogs" if destination is not None else None

    def _conflict_directory(self) -> Path | None:
        destination = self._destination_directory()
        return destination / "_DcmGetConflicts" if destination is not None else None

    def _update_result_shortcut_state(self) -> None:
        if not hasattr(self, "open_destination_button"):
            return
        destination = self._destination_directory()
        destination_exists = bool(destination and destination.is_dir())
        self.open_destination_button.setEnabled(destination_exists)
        self.open_destination_button.setToolTip(
            f"打开本任务的 DICOM 影像根目录：{destination}"
            if destination_exists
            else "请先选择已存在的保存目录"
        )

        task_log = self._active_task_log_directory or self._expected_task_log_directory()
        can_open_log = bool(
            task_log
            and (
                task_log.is_dir()
                or destination_exists
                or self._task_log_used_fallback
            )
        )
        self.open_task_log_button.setEnabled(can_open_log)
        if self._task_log_used_fallback and task_log is not None:
            self.open_task_log_button.setText("打开本地日志")
            self.open_task_log_button.setToolTip(
                f"目标日志目录不可写，任务日志已回退到：{task_log}"
            )
        elif can_open_log:
            self.open_task_log_button.setText("打开任务日志")
            self.open_task_log_button.setToolTip(
                f"打开当前任务的下载与接收日志：{task_log}"
            )
        else:
            self.open_task_log_button.setText("打开任务日志")
            self.open_task_log_button.setToolTip("开始任务后可打开下载与接收日志")

        report = self._last_acceptance_report
        report_exists = bool(report and report.is_file())
        self.open_acceptance_report_button.setEnabled(report_exists)
        self.open_acceptance_report_button.setToolTip(
            f"打开本批脱敏验收报告：{report}"
            if report_exists
            else "任务结束后自动生成验收报告"
        )

        conflict = self._conflict_directory()
        conflict_exists = bool(conflict and conflict.is_dir())
        self.open_conflict_button.setEnabled(conflict_exists)
        self.open_conflict_button.setToolTip(
            f"打开已保留的新收冲突文件：{conflict}"
            if conflict_exists
            else "当前保存目录没有需要人工核对的冲突文件"
        )

    def _on_task_log_directory_ready(self, directory: str, fallback: bool) -> None:
        self._active_task_log_directory = Path(directory).expanduser()
        self._task_log_used_fallback = fallback
        self._update_result_shortcut_state()

    def _open_task_log_directory(self) -> None:
        path = self._active_task_log_directory or self._expected_task_log_directory()
        if path is None:
            QMessageBox.warning(
                self,
                "无法打开任务日志",
                "请先选择 DICOM 保存目录并开始任务。",
            )
            return
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            fallback = self.instance_log_directory
            if fallback is None:
                QMessageBox.warning(
                    self,
                    "无法打开任务日志",
                    f"任务日志目录不可写：{path}\n\n{exc}",
                )
                return
            try:
                fallback.mkdir(parents=True, exist_ok=True)
            except OSError as fallback_exc:
                QMessageBox.warning(
                    self,
                    "无法打开任务日志",
                    f"任务日志目录和实例回退目录均不可写：\n{path}\n{fallback}\n\n"
                    f"{fallback_exc}",
                )
                return
            self._active_task_log_directory = fallback
            self._task_log_used_fallback = True
            self._append_log(
                "应用",
                f"任务日志目录不可写：{path}（{exc}）；"
                f"已回退到实例日志目录：{fallback}",
                "warning",
            )
            path = fallback
            self._update_result_shortcut_state()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def _open_acceptance_report(self) -> None:
        path = self._last_acceptance_report
        if path is None or not path.is_file():
            QMessageBox.warning(self, "无法打开验收报告", "当前任务尚未生成验收报告。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def _open_conflict_directory(self) -> None:
        path = self._conflict_directory()
        if path is None or not path.is_dir():
            QMessageBox.information(
                self,
                "没有冲突文件",
                "当前保存目录没有需要人工核对的冲突文件。",
            )
            self._update_result_shortcut_state()
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

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
        if len(parsed.values) > TASK_TABLE_DETAIL_LIMIT:
            self._apply_large_accession_text(self.accession_edit.toPlainText())
            return
        self.accession_edit.hidden_accessions = []
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
        self._render_task_phase()

    def _apply_large_accession_text(self, text: str) -> None:
        parsed = parse_accessions(text)
        self.current_accessions = parsed.values
        self.invalid_accessions = parsed.invalid_values
        previous = self.accession_edit.blockSignals(True)
        if len(parsed.values) > TASK_TABLE_DETAIL_LIMIT:
            self._hidden_accession_count = len(parsed.values)
            self.accession_edit.hidden_accessions = list(parsed.values)
            self.accession_edit.clear()
            self.accession_edit.setPlaceholderText(
                f"已粘贴 {len(parsed.values):,} 个检查号；为保持界面流畅，明细已隐藏"
            )
        else:
            self._hidden_accession_count = 0
            self.accession_edit.hidden_accessions = []
            self.accession_edit.setPlainText("\n".join(parsed.values))
        self.accession_edit.blockSignals(previous)
        self.accession_summary.setText(
            f"有效 {len(parsed.values):,} · 空行 {parsed.blank_count:,} · "
            f"重复 {parsed.duplicate_count:,} · 无效 {len(parsed.invalid_values):,}"
        )
        self._update_task_form_summary()
        if not self._is_busy():
            self._populate_waiting_rows(parsed.values)
            self._render_task_phase()

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
        carryover_results: list[AccessionResult] | None = None,
    ) -> None:
        self._summary_results = {}
        self._summary_processed = 0
        self._summary_files = 0
        self._summary_new_file_count = 0
        self._summary_existing_skipped_count = 0
        self._summary_conflict_preserved_count = 0
        self._summary_status_counts = {}
        for result in results or []:
            self._record_large_batch_result(result, update_label=False)
        for result in carryover_results or []:
            self._record_large_batch_carryover(result)
        self._update_large_batch_summary_label(total)

    def _record_large_batch_carryover(self, result: AccessionResult) -> None:
        if result.accession in self._summary_results:
            return
        carryover = replace(result, status=AccessionStatus.WAITING)
        self._summary_results[result.accession] = carryover
        self._summary_files += carryover.file_count
        self._summary_new_file_count += carryover.new_file_count
        self._summary_existing_skipped_count += carryover.existing_skipped_count
        self._summary_conflict_preserved_count += (
            carryover.conflict_preserved_count
        )

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
            self._summary_new_file_count -= previous.new_file_count
            self._summary_existing_skipped_count -= (
                previous.existing_skipped_count
            )
            self._summary_conflict_preserved_count -= (
                previous.conflict_preserved_count
            )
            if previous.status in {
                AccessionStatus.WAITING,
                AccessionStatus.DOWNLOADING,
            }:
                self._summary_processed += 1
            else:
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
            new_file_count=result.new_file_count,
            existing_skipped_count=result.existing_skipped_count,
            conflict_preserved_count=result.conflict_preserved_count,
        )
        self._summary_files += result.file_count
        self._summary_new_file_count += result.new_file_count
        self._summary_existing_skipped_count += result.existing_skipped_count
        self._summary_conflict_preserved_count += result.conflict_preserved_count
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
            (
                f"新增 {self._summary_new_file_count:,} · "
                f"已存在跳过 {self._summary_existing_skipped_count:,} · "
                f"冲突保留 {self._summary_conflict_preserved_count:,}"
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

    def _update_task_result_summary(
        self,
        results: list[AccessionResult] | None = None,
    ) -> None:
        if results is not None:
            self._result_stats = {}
            self._result_new_file_count = 0
            self._result_existing_skipped_count = 0
            self._result_conflict_preserved_count = 0
            self._result_failed_count = 0
            for result in results:
                self._record_task_result_stats(result)
        self.task_result_summary.setText(
            f"结果：新增 {self._result_new_file_count:,} · "
            f"已存在跳过 {self._result_existing_skipped_count:,} · "
            f"冲突保留 {self._result_conflict_preserved_count:,} · "
            f"失败 {self._result_failed_count:,}"
        )

    def _record_task_result_stats(self, result: AccessionResult) -> None:
        if result.status in {AccessionStatus.WAITING, AccessionStatus.DOWNLOADING}:
            return
        previous = self._result_stats.get(result.accession)
        if previous is not None:
            previous_new, previous_skipped, previous_conflict, previous_failed = (
                previous
            )
            self._result_new_file_count -= previous_new
            self._result_existing_skipped_count -= previous_skipped
            self._result_conflict_preserved_count -= previous_conflict
            self._result_failed_count -= previous_failed
        failed = int(
            result.status in {AccessionStatus.FAILED, AccessionStatus.PARTIAL}
        )
        current = (
            result.new_file_count,
            result.existing_skipped_count,
            result.conflict_preserved_count,
            failed,
        )
        self._result_stats[result.accession] = current
        self._result_new_file_count += current[0]
        self._result_existing_skipped_count += current[1]
        self._result_conflict_preserved_count += current[2]
        self._result_failed_count += current[3]

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

    def _sync_task_detail_visibility(
        self, *, detail_visible: bool | None = None
    ) -> None:
        if detail_visible is None:
            detail_visible = bool(
                self._ui_running
                or self._resume_checkpoint
                or self._pdi_task_id
                or self.last_summary is not None
                or self.last_pdi_result is not None
            ) and self._display_total > 0
        self.task_table.setVisible(
            detail_visible and not self._task_table_summary_mode
        )
        self.large_batch_summary_card.setVisible(
            detail_visible and self._task_table_summary_mode
        )
        self.task_splitter.setVisible(
            (detail_visible and not self._task_table_summary_mode)
            or self._log_panel_expanded
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
            task_config = AppConfig.from_dict(resume_checkpoint.config.to_dict())
            if continuing_existing and requested_destination:
                task_config.dicom_destination_folder = requested_destination
            self._active_task_config = task_config
            self.destination_edit.setText(task_config.dicom_destination_folder)
            self._sync_quick_pdi_controls_from_config()
            try:
                self.task_store.update_config(
                    resume_checkpoint.task_id,
                    task_config,
                )
                resume_checkpoint.config = AppConfig.from_dict(task_config.to_dict())
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
            task_config = AppConfig.from_dict(self.config.to_dict())
            task_config.pdi_export_enabled = self._quick_pdi_enabled
            task_config.pdi_output_folder = self._quick_pdi_output_folder
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

        task_config.access_numbers_file_path = (
            task_config.access_numbers_file_path or "access.txt"
        )
        task_config.dicom_destination_folder = self.destination_edit.text().strip()
        check = preflight(task_config, self.resolver)
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

        checkpoint_was_created = resume_checkpoint is None
        if resume_checkpoint is None:
            if not self.task_store.try_acquire_lease():
                QMessageBox.warning(
                    self,
                    "已有任务正在运行",
                    "另一个 DcmGet 实例正在下载，本窗口不能启动新任务。",
                )
                return
            self.config.access_numbers_file_path = task_config.access_numbers_file_path
            self.config.dicom_destination_folder = task_config.dicom_destination_folder
            save_config(self.config_path, self.config)
            try:
                checkpoint = self.task_store.start(
                    task_config,
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
        try:
            self._ensure_task_ledger_batch(checkpoint)
        except TaskLedgerError as exc:
            self._append_log("验收", str(exc), "error")
            if checkpoint_was_created:
                try:
                    self.task_store.clear(checkpoint.task_id)
                except TaskStateError:
                    pass
            self.task_store.release_lease()
            QMessageBox.critical(
                self,
                "无法建立任务台账",
                f"{exc}\n\n为避免任务完成后无法追溯，本次下载没有启动。",
            )
            return
        self.tools = check.tools
        self._active_task_config = task_config
        self._active_accessions = accessions
        self._active_task_id = checkpoint.task_id
        self._resume_checkpoint = checkpoint if resume_checkpoint is not None else None
        self._prior_results = checkpoint.results
        partial_results = list(checkpoint.partial_results.values())
        resume_display_results = [*self._prior_results, *partial_results]
        self._result_destination_directory = Path(
            task_config.dicom_destination_folder
        ).expanduser()
        self._update_task_result_summary(resume_display_results)
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
        self._last_acceptance_report = None
        self._accepted_partial_results = False
        self._reset_pdi_status_card()
        self._populate_waiting_rows(display_accessions)
        if self._task_table_summary_mode:
            self._reset_large_batch_summary(
                self._display_total,
                self._prior_results,
                carryover_results=partial_results,
            )
        else:
            for result in self._prior_results:
                self._set_result_row(result)
            for result in partial_results:
                retained_message = result.message or "已保留上次收到的文件"
                self._set_result_row(
                    replace(
                        result,
                        status=AccessionStatus.WAITING,
                        message=f"待重试；{retained_message}",
                    )
                )
        self.progress_bar.setRange(0, self._display_total)
        self.progress_bar.setValue(self._progress_offset)
        action = "继续下载" if resume_checkpoint is not None else "准备下载"
        self.progress_label.setText(
            f"{action} {self._progress_offset}/{self._display_total}"
        )
        self._set_running(True)
        self._worker_failure_message = None
        task_log_directory = self._task_log_directory_for_config(task_config)
        self._on_task_log_directory_ready(str(task_log_directory), False)

        thread = QThread(self)
        worker = DownloadWorker(
            task_config,
            check.tools,
            list(self._active_accessions),
            consume_trial_on_ready=use_trial,
            task_store=self.task_store,
            task_id=checkpoint.task_id,
            log_directory=task_log_directory,
            fallback_log_directory=self.instance_log_directory,
            task_ledger=self.task_ledger,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.log.connect(self._append_log)
        worker.log_directory_ready.connect(self._on_task_log_directory_ready)
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
        self._preflight_has_result = True
        self._preflight_has_error = not check.ok
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
        self._render_task_phase()

    def _effective_task_config(self) -> AppConfig:
        return self._active_task_config or self.config

    def _render_task_phase(self) -> None:
        """Render the task page from one small, explicit presentation state."""

        if not hasattr(self, "preflight_card"):
            return
        recovery_pending = bool(self._resume_checkpoint or self._pdi_task_id)
        completed = self.last_summary is not None or self.last_pdi_result is not None
        phase = (
            "running"
            if self._ui_running
            else "recovery"
            if recovery_pending
            else "completed"
            if completed
            else "idle"
        )
        runtime_visible = phase in {"running", "recovery", "completed"}
        result_visible = phase in {"recovery", "completed"}
        detail_visible = runtime_visible and self._display_total > 0

        self.task_section_title.setText(
            "当前任务" if phase in {"running", "recovery"} else "新建下载任务"
        )

        self.preflight_card.setVisible(
            not self._ui_running
            and not recovery_pending
            and self._preflight_has_result
            and (phase == "idle" or self._preflight_has_error)
        )
        progress_visible = phase in {"running", "completed"}
        self.progress_label.setVisible(progress_visible)
        self.progress_bar.setVisible(progress_visible)
        self.task_result_summary.setVisible(progress_visible)
        self._update_result_shortcut_state()
        visible_result_buttons = []
        for button in (
            self.open_destination_button,
            self.open_task_log_button,
            self.open_acceptance_report_button,
            self.open_conflict_button,
        ):
            visible = result_visible and button.isEnabled()
            button.setVisible(visible)
            visible_result_buttons.append(visible)
        self.result_shortcuts_widget.setVisible(any(visible_result_buttons))

        self.start_button.setVisible(phase in {"idle", "completed"})
        if phase in {"idle", "completed"}:
            self.start_button.setEnabled(bool(self.current_accessions))
        self.recovery_card.setVisible(phase == "recovery")
        self.input_card.setVisible(True)
        if phase == "running":
            self._set_task_form_expanded(False)

        pdi_active = bool(
            self.pdi_worker
            or self.pdi_thread
            or self._pdi_task_id
            or self.last_pdi_result is not None
        )
        self.pdi_status_card.setVisible(
            runtime_visible and pdi_active
        )
        self.pdi_retry_button.setVisible(phase != "recovery")
        self._sync_task_detail_visibility(detail_visible=detail_visible)

    def _set_running(
        self,
        running: bool,
        *,
        reset_summary: bool = True,
        can_pause: bool = True,
    ) -> None:
        self._ui_running = running
        if running and reset_summary:
            self.last_summary = None
        resume_pending = not running and self._resume_checkpoint is not None
        download_retryable = bool(
            resume_pending
            and self._resume_checkpoint is not None
            and self._resume_checkpoint.phase == "download_retryable"
        )
        safety_paused = bool(
            download_retryable
            and self._resume_checkpoint is not None
            and self._resume_checkpoint.interrupted_reason
        )
        pdi_pending = not running and bool(self._pdi_task_id)
        recovery_pending = resume_pending or pdi_pending
        self.start_button.setEnabled(not running and not pdi_pending)
        self.start_button.setText(
            "继续安全暂停任务"
            if safety_paused
            else "重试失败项"
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
            else "放弃安全暂停任务并新建"
            if safety_paused
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
        self.accept_partial_button.setVisible(download_retryable and not safety_paused)
        self.accept_partial_button.setEnabled(download_retryable and not safety_paused)
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
        self.header_settings_button.setEnabled(not running)
        self.registration_button.setEnabled(not running)
        self.registration_action.setEnabled(not running)
        self.maintenance_button.setEnabled(not running)
        self.maintenance_menu.menuAction().setEnabled(not running)
        self.more_button.setEnabled(True)
        self._render_task_phase()

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
            "safety_paused": "任务已安全暂停，正在保存恢复点…",
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
        if result.status not in {AccessionStatus.WAITING, AccessionStatus.DOWNLOADING}:
            self._record_task_result_stats(result)
            self._update_task_result_summary()
        self._update_result_shortcut_state()

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
        default_detail = {
            AccessionStatus.WAITING: "等待处理",
            AccessionStatus.DOWNLOADING: "正在接收 DICOM 文件",
            AccessionStatus.COMPLETED: "接收完成",
            AccessionStatus.NO_DATA: "PACS 未返回数据",
            AccessionStatus.PARTIAL: "部分文件已保留",
            AccessionStatus.FAILED: "下载失败",
            AccessionStatus.CANCELLED: "任务已取消",
        }.get(result.status, "")
        self.task_table.setItem(
            target_row,
            5,
            QTableWidgetItem(result.message or default_detail),
        )

    def _on_worker_finished(self, summary: BatchSummary) -> None:
        self._update_result_shortcut_state()
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
                        self.task_store.set_phase(
                            task_id,
                            "download_retryable",
                            interrupted_reason=summary.interrupted_reason,
                        )
                        active_checkpoint.phase = "download_retryable"
                        active_checkpoint.interrupted_reason = (
                            summary.interrupted_reason
                        )
                    elif self._effective_task_config().pdi_export_enabled and pdi_has_files:
                        self.task_store.set_phase(task_id, "pdi_pending")
                        active_checkpoint.phase = "pdi_pending"
                    else:
                        self.task_store.clear(task_id)
        except TaskStateError as exc:
            self._append_log("恢复", str(exc), "error")
        self.last_summary = summary
        self._update_task_result_summary(summary.results)
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
            self._effective_task_config().pdi_export_enabled
            and not summary.cancelled
            and summary.exit_code == 0
            and pdi_has_files
        )
        if not pdi_should_run:
            if summary.cancelled:
                self._export_acceptance_report(task_id)
            elif download_retryable:
                self._export_acceptance_report(task_id)
            else:
                self._complete_task_ledger(task_id, "completed")
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
            self._effective_task_config().pdi_export_enabled
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
        elif summary.interrupted_reason:
            pending_count = (
                len(self._resume_checkpoint.pending_accessions)
                if self._resume_checkpoint is not None
                else sum(
                    result.status == AccessionStatus.CANCELLED
                    for result in summary.results
                )
            )
            title = "任务已安全暂停"
            message = (
                f"{summary.interrupted_reason}\n\n"
                f"尚有 {pending_count:,} 个检查号待处理。修复问题后点击“继续安全暂停任务”。"
            )
        elif summary.exit_code == 2 and not self._accepted_partial_results:
            title = "任务部分完成"
            message = f"有 {len(summary.failed_accessions)} 个检查号失败，可点击“重试失败项”。"
        elif summary.exit_code == 2:
            title = "已接受当前结果"
            message = "失败项已不再作为必须续传任务，已收到的 DICOM 文件已保留。"
        else:
            title = "任务部分完成" if pdi_problem else "下载完成"
            message = "所有检查号均已处理完成。"
        result_details = (
            f"\n\n结果：新增 {summary.new_file_count:,}；"
            f"已存在跳过 {summary.existing_skipped_count:,}；"
            f"冲突保留 {summary.conflict_preserved_count:,}"
        )
        if summary.failed_accessions or not summary.interrupted_reason:
            result_details += f"；失败 {len(summary.failed_accessions):,}"
        message += result_details + "。"
        if pdi_message:
            message += "\n\n" + pdi_message
        self.progress_label.setText(title)
        self.progress_label.setToolTip(message)
        self._append_log(
            "应用",
            message.replace("\n\n", "；"),
            "warning" if pdi_problem or summary.exit_code else "success",
        )
        if self._resume_checkpoint is not None:
            self._show_recovery_card(self._resume_checkpoint)
        elif self._pdi_task_id:
            try:
                checkpoint = self.task_store.load(include_archived_files=False)
            except TaskStateError as exc:
                self._append_log("恢复", str(exc), "error")
            else:
                if checkpoint is not None and checkpoint.task_id == self._pdi_task_id:
                    self._show_recovery_card(checkpoint)
        elif not self._pdi_task_id:
            self._prepare_next_task_draft()
        self._render_task_phase()

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
                self.tools = self.resolver.resolve(
                    self._effective_task_config().dcmtk_bin_dir
                )
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
            self._effective_task_config(),
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
        volume_count = max(1, int(getattr(result, "volume_count", 1) or 1))
        if status_text == "完成" and output_directory:
            message = (
                f"PDI 已生成 {volume_count} 卷：{Path(output_directory).name}"
                if volume_count > 1
                else f"PDI 便携阅片目录已生成：{Path(output_directory).name}"
            )
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
        self.pdi_view_button.setEnabled(self._pdi_viewer_directory() is not None)
        self.pdi_view_button.setText(
            "打开第 1 卷影像" if volume_count > 1 else "打开影像"
        )
        self.pdi_retry_button.setEnabled(False)
        ledger_task_id = self._pdi_task_id
        if status_text in {"完成", "部分成功", "失败", "已取消"}:
            ledger_status = {
                "完成": "completed",
                "部分成功": "partial",
                "失败": "failed",
                "已取消": "cancelled",
            }[status_text]
            self._complete_task_ledger(
                ledger_task_id,
                ledger_status,
                pdi_result=result,
            )
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
        if self._pdi_task_id:
            try:
                ledger = self._get_task_ledger()
                ledger.record_pdi_result(
                    self._pdi_task_id,
                    "failed",
                    message=message,
                )
                ledger.complete_batch(self._pdi_task_id, "failed")
            except TaskLedgerError as exc:
                self._append_log("验收", f"任务台账更新失败：{exc}", "error")
            self._export_acceptance_report(self._pdi_task_id)
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
        self.pdi_status_card.setVisible(
            self._effective_task_config().pdi_export_enabled
        )
        self._set_pdi_status(message, "warning")
        self.pdi_progress_bar.setRange(0, 1)
        self.pdi_progress_bar.setValue(0)
        self.pdi_view_button.setEnabled(False)
        self.pdi_open_button.setEnabled(False)
        self.pdi_retry_button.setEnabled(False)

    def _reset_pdi_status_card(self) -> None:
        self.pdi_status_card.setVisible(False)
        self._set_pdi_status("下载完成后自动生成", "pending")
        self.pdi_progress_bar.setRange(0, 100)
        self.pdi_progress_bar.setValue(0)
        self.pdi_view_button.setEnabled(False)
        self.pdi_view_button.setText("打开影像")
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

    def _pdi_viewer_directory(self) -> Path | None:
        directories = list(
            getattr(self.last_pdi_result, "output_directories", []) or []
        )
        candidates = [*directories]
        root = self._pdi_output_directory()
        if root is not None:
            candidates.append(str(root))
        for value in candidates:
            path = Path(value).expanduser()
            if path.is_dir() and pdi_viewer_command(path):
                return path.resolve()
        return None

    def _open_pdi_viewer(self) -> None:
        root = self._pdi_viewer_directory()
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
        self._update_result_shortcut_state()
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
        self._export_acceptance_report(self._active_task_id)
        self.task_store.release_lease()
        if self.worker_thread is None:
            self._finish_worker(set_idle=False)
            self._complete_worker_failed(message)

    def _complete_worker_failed(self, message: str) -> None:
        self._set_running(False)
        if self._resume_checkpoint is not None:
            self._show_recovery_card(self._resume_checkpoint)
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
        if self.task_workspace is not None:
            self.task_workspace.remove_task(task_id)
        self._resume_checkpoint = None
        self._pdi_task_id = ""
        self._pdi_reuse_published = False
        self._pdi_source_files = []
        self.last_summary = None
        self._update_task_result_summary([])
        self._result_destination_directory = None
        self._active_task_log_directory = None
        self._task_log_used_fallback = False
        self._accepted_partial_results = False
        self.pdi_retry_button.setEnabled(False)
        self._active_task_id = ""
        self._active_task_config = None
        self._prepare_next_task_draft()
        self.progress_label.setText("已放弃续传记录，可以新建任务")
        self._set_running(False)
        self._set_task_form_expanded(True)
        self._update_result_shortcut_state()

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
        if checkpoint.interrupted_reason or checkpoint.pending_accessions:
            QMessageBox.warning(
                self,
                "任务仍有待处理项",
                "安全暂停任务不能直接接受当前结果；请修复问题后继续任务。",
            )
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

        self._active_task_config = AppConfig.from_dict(stored.config.to_dict())
        self._resume_checkpoint = None
        self._accepted_partial_results = True
        if source_files:
            self._pdi_task_id = stored.task_id
            self._pdi_source_files = source_files
            self._append_log("恢复", "已接受当前下载结果，继续生成 PDI", "warning")
            self._start_pdi_export(source_files)
            return

        self._complete_task_ledger(stored.task_id, "accepted_partial")
        self.task_store.release_lease()
        self._set_running(False)
        self.progress_label.setText("已接受当前结果，恢复记录已结束")
        self.progress_label.setToolTip(
            "现有 DICOM 文件已保留；失败项不再作为必须续传任务。"
        )
        self._prepare_next_task_draft()
        self._render_task_phase()

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
        self._open_task_log_directory()

    def _open_diagnostic_log_directory(self) -> None:
        path = diagnostic_log_directory()
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def _operation_config(self) -> AppConfig:
        values = self.config.to_dict()
        destination = self.destination_edit.text().strip()
        if destination:
            values["dicom_destination_folder"] = destination
        return AppConfig.from_dict(values)

    def _run_health_check(self) -> None:
        try:
            from .health import run_health_check

            report = run_health_check(
                self._operation_config(),
                project_root=self.project_root,
                minimum_free_bytes=self.config.minimum_free_space_bytes,
                check_pacs=True,
            )
        except Exception as exc:
            QMessageBox.critical(self, "健康检查失败", str(exc))
            return
        labels = {"ok": "通过", "warning": "警告", "error": "失败"}
        lines = [
            f"[{labels.get(item.status, item.status)}] {item.summary}"
            for item in report.checks
        ]
        message = "\n".join(lines)
        if report.status == "error":
            QMessageBox.warning(self, "健康检查发现问题", message)
        else:
            QMessageBox.information(self, "健康检查完成", message)

    def _default_export_directory(self) -> str:
        downloads = QStandardPaths.writableLocation(QStandardPaths.DownloadLocation)
        return downloads or str(Path.home())

    def _create_support_bundle(self) -> None:
        default_path = Path(self._default_export_directory()) / (
            f"DcmGet-support-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        )
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "保存脱敏支持包",
            str(default_path),
            "ZIP 压缩包 (*.zip)",
        )
        if not selected:
            return
        try:
            from .support_bundle import create_support_bundle

            result = create_support_bundle(
                selected,
                self._operation_config(),
                project_root=self.project_root,
                diagnostic_directory=diagnostic_log_directory(),
            )
        except Exception as exc:
            QMessageBox.critical(self, "支持包生成失败", str(exc))
            return
        QMessageBox.information(
            self,
            "支持包已生成",
            f"已生成默认脱敏的诊断支持包：\n{result.path}\n\n"
            "支持包不包含 DICOM、注册码、试用状态或检查号文件。",
        )

    def _backup_profiles(self) -> None:
        default_path = Path(self._default_export_directory()) / (
            f"DcmGet-profiles-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        )
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "备份全部 Profile 配置",
            str(default_path),
            "DcmGet Profile 备份 (*.zip)",
        )
        if not selected:
            return
        try:
            from .profile_backup import create_profile_backup

            result = create_profile_backup(selected)
        except Exception as exc:
            QMessageBox.critical(self, "Profile 备份失败", str(exc))
            return
        QMessageBox.information(
            self,
            "Profile 备份完成",
            f"已备份 {len(result.profile_numbers)} 个 Profile：\n{result.path}\n\n"
            "备份不包含授权和试用信息。",
        )

    def _manage_profiles(self) -> None:
        try:
            from .profile_dialog import ProfileManagerDialog

            dialog = ProfileManagerDialog(
                self,
                project_root=self.project_root,
                current_profile_number=self.profile_number,
            )
            dialog.exec_()
        except Exception as exc:
            record_exception("DcmGetWindow._manage_profiles", exc)
            QMessageBox.critical(self, "Profile 管理失败", str(exc))

    def _restore_profiles(self) -> None:
        if self._is_busy() or self._resume_checkpoint is not None or self._pdi_task_id:
            QMessageBox.information(
                self,
                "当前任务尚未结束",
                "请先完成或明确放弃当前下载/PDI 恢复任务，再恢复 Profile。",
            )
            return
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "选择 Profile 备份",
            self._default_export_directory(),
            "DcmGet Profile 备份 (*.zip)",
        )
        if not selected:
            return
        answer = QMessageBox.question(
            self,
            "恢复 Profile 配置与显示名",
            "恢复会替换备份中对应的 Profile 配置与显示名；当前内容会先自动备份。\n\n"
            "继续恢复吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            from .profile_backup import restore_profile_backup

            result = restore_profile_backup(
                selected,
                owned_profile_lock=self.profile_lock,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Profile 恢复失败", str(exc))
            return
        if (
            self.profile_number is not None
            and self.profile_number in result.profile_numbers
        ):
            try:
                self.config = load_config(self.config_path)
            except Exception as exc:
                QMessageBox.critical(
                    self,
                    "Profile 已恢复但无法重新载入",
                    f"配置已经写入磁盘，但当前窗口无法重新载入：{exc}\n\n"
                    "请立即关闭并重新启动 DcmGet。",
                )
                return
            self.settings_page.set_config(self.config)
            self.destination_edit.setText(self.config.dicom_destination_folder)
            self._sync_quick_pdi_controls_from_config()
            self._refresh_tool_status()
            try:
                from .profile_manager import read_profile_display_name

                self.instance_label = read_profile_display_name(
                    self.config_path,
                    self.profile_number,
                )
                instance_title = (
                    f" - {self.instance_label}" if self.instance_label else ""
                )
                self.setWindowTitle(
                    f"DcmGet {__version__}{instance_title} - DICOM 下载工作台"
                )
                self._update_header_responsive_layout(force=True)
            except Exception as exc:
                self._append_log(
                    "Profile",
                    f"配置已重新载入，但显示名刷新失败：{exc}",
                    "warning",
                )
        QMessageBox.information(
            self,
            "Profile 恢复完成",
            f"已恢复 Profile：{'、'.join(map(str, result.profile_numbers))}\n\n"
            "请重新启动 DcmGet，使恢复的配置完全生效。"
            + (
                f"\n恢复前快照：{result.previous_backup}"
                if result.previous_backup
                else ""
            ),
        )

    def _verify_existing_pdi(self) -> None:
        if self._is_busy():
            QMessageBox.information(self, "后台任务运行中", "请等待当前后台任务结束后再验证 PDI。")
            return
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择要验证的 PDI 或分卷根目录",
            self.config.pdi_output_folder or self.destination_edit.text(),
        )
        if not selected:
            return
        dialog = QProgressDialog("正在准备 PDI 完整性验证…", "取消验证", 0, 0, self)
        dialog.setWindowTitle("验证 PDI/U盘")
        dialog.setWindowModality(Qt.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        thread = QThread(self)
        worker = PdiVerifyWorker(selected)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_pdi_verify_progress)
        worker.finished.connect(self._on_pdi_verify_finished)
        worker.failed.connect(self._on_pdi_verify_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        dialog.canceled.connect(self._cancel_pdi_verification)
        thread.finished.connect(self._on_pdi_verify_thread_finished)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self.pdi_verify_dialog = dialog
        self.pdi_verify_thread = thread
        self.pdi_verify_worker = worker
        dialog.show()
        thread.start()

    def _cancel_pdi_verification(self) -> None:
        worker = self.pdi_verify_worker
        if worker is not None:
            worker.request_cancel()

    def _on_pdi_verify_progress(
        self,
        _root: str,
        current: int,
        total: int,
        message: str,
    ) -> None:
        dialog = self.pdi_verify_dialog
        if dialog is None:
            return
        dialog.setLabelText(message or "正在验证 PDI…")
        if total > 0:
            dialog.setRange(0, total)
            dialog.setValue(max(0, min(current, total)))
        else:
            dialog.setRange(0, 0)

    def _on_pdi_verify_finished(self, completed: object) -> None:
        if isinstance(completed, dict):
            items = list(completed.get("items") or [])
            cancelled = bool(completed.get("cancelled"))
        else:
            items = list(completed or [])
            cancelled = False
        cancelled = cancelled or any(
            str(getattr(getattr(result, "status", ""), "value", "")) == "cancelled"
            for result, _reports in items
        )
        dialog = self.pdi_verify_dialog
        if dialog is not None:
            dialog.close()
        if cancelled or not items:
            QMessageBox.information(self, "PDI 验证已取消", "验证已取消，没有修改 PDI 文件。")
            return
        failed = sum(
            str(getattr(getattr(result, "status", ""), "value", "")) == "failed"
            for result, _reports in items
        )
        warnings = sum(
            str(getattr(getattr(result, "status", ""), "value", "")) == "warning"
            for result, _reports in items
        )
        report_paths = [str(reports.html_path) for _result, reports in items]
        message = (
            f"已验证 {len(items)} 个 PDI 卷：失败 {failed}，警告 {warnings}。\n\n"
            f"验收报告：\n" + "\n".join(report_paths)
        )
        if failed:
            QMessageBox.warning(self, "PDI 验证未通过", message)
        else:
            QMessageBox.information(self, "PDI 验证完成", message)
        if report_paths:
            QDesktopServices.openUrl(QUrl.fromLocalFile(report_paths[0]))

    def _on_pdi_verify_failed(self, message: str) -> None:
        if self.pdi_verify_dialog is not None:
            self.pdi_verify_dialog.close()
        QMessageBox.critical(self, "PDI 验证失败", message)

    def _on_pdi_verify_thread_finished(self) -> None:
        if self.sender() is not self.pdi_verify_thread:
            return
        self.pdi_verify_worker = None
        self.pdi_verify_thread = None
        self.pdi_verify_dialog = None

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

    def _prepare_next_task_draft(self) -> None:
        """Reset editable inputs without erasing the completed result view."""

        self._active_task_config = None
        previous = self.accession_edit.blockSignals(True)
        self.accession_edit.clear()
        self.accession_edit.hidden_accessions = []
        self.accession_edit.setPlaceholderText(
            "每行一个检查号；也可以拖入 TXT、CSV 或 XLSX 文件"
        )
        self.accession_edit.blockSignals(previous)
        self.current_accessions = []
        self.invalid_accessions = ()
        self._hidden_accession_count = 0
        self.accession_summary.setText("有效 0 · 空行 0 · 重复 0 · 无效 0")
        self.settings_page.set_config(self.config)
        self._sync_quick_pdi_controls_from_config(self.config)
        self._set_task_form_expanded(False)

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
            pdi_verifying = self.pdi_verify_worker is not None
            answer = QMessageBox.question(
                self,
                "退出 DcmGet",
                (
                    "PDI 完整性仍在验证。取消验证并退出吗？"
                    if pdi_verifying
                    else "PDI 便携目录仍在生成。取消导出并退出吗？"
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
            active_worker = self.pdi_verify_worker or self.pdi_worker or self.worker
            if active_worker is not None:
                active_worker.request_cancel()
            active_thread = (
                self.pdi_verify_thread or self.pdi_thread or self.worker_thread
            )
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
QFrame#RecoveryCard {{
    background: #EFF6FF;
    border: 1px solid #93C5FD;
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
QLabel#HeaderSubtitle, QLabel#HeaderChannel, QLabel#FieldHint {{ color: {COLORS['muted']}; }}
QLabel#HeaderChannel {{ padding: 4px 8px; background: #F1F5F9; border-radius: 10px; }}
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
