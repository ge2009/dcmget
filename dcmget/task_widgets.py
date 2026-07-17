from __future__ import annotations

from datetime import datetime
from typing import Iterable, Protocol, Sequence

from PyQt5.QtCore import QAbstractListModel, QModelIndex, QRect, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPainter
from PyQt5.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)


TASK_WIDGET_COLORS = {
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

TASK_WORKSPACE_COMPACT_WIDTH = 960
TASK_LIST_ITEM_HEIGHT = 82


class TaskSummaryLike(Protocol):
    """UI-facing subset of :class:`dcmget.task_manager.TaskSummary`."""

    task_id: str
    phase: str
    total_count: int
    processed_count: int
    file_count: int
    speed_bytes_per_second: float
    queue_position: int | None
    created_at: str


_PHASE_PRESENTATION: dict[str, tuple[str, str, QStyle.StandardPixmap]] = {
    "queued": ("等待并发槽", "muted", QStyle.SP_FileDialogInfoView),
    "waiting": ("等待并发槽", "muted", QStyle.SP_FileDialogInfoView),
    "preflight": ("预检中", "primary", QStyle.SP_BrowserReload),
    "starting_receiver": ("启动接收器", "primary", QStyle.SP_BrowserReload),
    "running": ("并发下载中", "primary", QStyle.SP_ArrowDown),
    "downloading": ("并发下载中", "primary", QStyle.SP_ArrowDown),
    "pause_pending": ("等待暂停", "warning", QStyle.SP_MessageBoxWarning),
    "paused": ("已暂停", "warning", QStyle.SP_MediaPause),
    "stopping": ("停止中", "warning", QStyle.SP_BrowserStop),
    "cancelling": ("取消中", "warning", QStyle.SP_BrowserStop),
    "download_retryable": ("待重试", "warning", QStyle.SP_BrowserReload),
    "pdi_pending": ("等待 PDI", "muted", QStyle.SP_FileDialogInfoView),
    "pdi_running": ("生成 PDI", "primary", QStyle.SP_BrowserReload),
    "pdi_retryable": ("PDI 待重试", "warning", QStyle.SP_BrowserReload),
    "completed": ("完成", "success", QStyle.SP_DialogApplyButton),
    "partial": ("部分成功", "warning", QStyle.SP_MessageBoxWarning),
    "failed": ("失败", "danger", QStyle.SP_MessageBoxCritical),
    "cancelled": ("已取消", "muted", QStyle.SP_DialogCancelButton),
    "interrupted": ("待恢复", "warning", QStyle.SP_MessageBoxWarning),
    "recovery_required": ("待恢复", "warning", QStyle.SP_MessageBoxWarning),
}


def _summary_value(summary: object, *names: str, default: object = None) -> object:
    for name in names:
        if hasattr(summary, name):
            return getattr(summary, name)
    return default


def _summary_text(summary: object, *names: str, default: str = "") -> str:
    value = _summary_value(summary, *names, default=default)
    if hasattr(value, "value"):
        value = getattr(value, "value")
    return str(value or default).strip()


def _summary_int(summary: object, *names: str, default: int = 0) -> int:
    try:
        return max(0, int(_summary_value(summary, *names, default=default)))
    except (TypeError, ValueError):
        return max(0, default)


def _summary_float(summary: object, *names: str, default: float = 0.0) -> float:
    try:
        return max(0.0, float(_summary_value(summary, *names, default=default)))
    except (TypeError, ValueError):
        return max(0.0, default)


def _summary_phase(summary: object) -> str:
    return _summary_text(summary, "phase", "status", default="queued").lower()


def _status_presentation(
    summary: object,
) -> tuple[str, str, QStyle.StandardPixmap]:
    phase = _summary_phase(summary)
    return _PHASE_PRESENTATION.get(
        phase,
        (phase or "未知状态", "muted", QStyle.SP_FileDialogInfoView),
    )


def _created_label(summary: object) -> str:
    raw = _summary_value(summary, "created_at", default="")
    if isinstance(raw, datetime):
        value = raw
    else:
        text = str(raw or "").strip()
        if not text:
            return ""
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text[:16]
    if value.tzinfo is not None:
        value = value.astimezone()
    return value.strftime("%m-%d %H:%M")


def format_task_transfer_rate(bytes_per_second: float) -> str:
    value = max(0.0, float(bytes_per_second or 0.0))
    if value <= 0:
        return "—"
    units = ("B/s", "KB/s", "MB/s", "GB/s", "TB/s")
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{value:.0f} {units[unit_index]}"
    return f"{value:.1f} {units[unit_index]}"


class TaskListModel(QAbstractListModel):
    """Aggregate-only task model; it never exposes patient-facing text."""

    TaskIdRole = Qt.UserRole + 1
    PhaseRole = Qt.UserRole + 2
    StatusTextRole = Qt.UserRole + 3
    StatusColorRole = Qt.UserRole + 4
    TotalCountRole = Qt.UserRole + 5
    ProcessedCountRole = Qt.UserRole + 6
    FileCountRole = Qt.UserRole + 7
    SpeedRole = Qt.UserRole + 8
    QueuePositionRole = Qt.UserRole + 9
    ProgressPercentRole = Qt.UserRole + 10
    TitleRole = Qt.UserRole + 11
    DetailRole = Qt.UserRole + 12

    tasks_changed = pyqtSignal()

    def __init__(
        self,
        tasks: Sequence[TaskSummaryLike] | None = None,
        parent: object | None = None,
    ) -> None:
        super().__init__(parent)
        self._tasks: list[TaskSummaryLike] = list(tasks or ())

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._tasks)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):  # noqa: N802
        if not index.isValid() or not 0 <= index.row() < len(self._tasks):
            return None
        task = self._tasks[index.row()]
        task_id = _summary_text(task, "task_id")
        phase = _summary_phase(task)
        status_text, color_name, icon_name = _status_presentation(task)
        total = _summary_int(task, "total_count", "accession_count")
        processed = min(total, _summary_int(task, "processed_count")) if total else 0
        files = _summary_int(task, "file_count")
        speed = _summary_float(task, "speed_bytes_per_second")
        raw_queue_position = _summary_value(task, "queue_position", default=None)
        try:
            queue_position = (
                max(1, int(raw_queue_position))
                if raw_queue_position is not None
                else None
            )
        except (TypeError, ValueError):
            queue_position = None
        percent = int(round(processed * 100 / total)) if total else 0
        created = _created_label(task)
        short_id = task_id[-6:] if task_id else "未保存"
        title_prefix = created or f"任务 {short_id}"
        title = f"{title_prefix} · {total:,} 个检查号"
        queue_text = f"队列第 {queue_position} 位 · " if queue_position else ""
        speed_text = format_task_transfer_rate(speed)
        detail = (
            f"{queue_text}{processed:,}/{total:,} · "
            f"文件 {files:,} · {speed_text}"
        )

        if role == Qt.DisplayRole:
            return f"{title}\n{status_text} · {detail}"
        if role == Qt.ToolTipRole:
            return (
                f"任务：{task_id or '未保存'}\n"
                f"状态：{status_text}\n"
                f"进度：{processed:,}/{total:,}\n"
                f"文件：{files:,}\n速度：{speed_text}"
            )
        if role == Qt.DecorationRole:
            return QApplication.style().standardIcon(icon_name)
        if role == Qt.SizeHintRole:
            return QSize(260, TASK_LIST_ITEM_HEIGHT)
        if role == self.TaskIdRole:
            return task_id
        if role == self.PhaseRole:
            return phase
        if role == self.StatusTextRole:
            return status_text
        if role == self.StatusColorRole:
            return TASK_WIDGET_COLORS[color_name]
        if role == self.TotalCountRole:
            return total
        if role == self.ProcessedCountRole:
            return processed
        if role == self.FileCountRole:
            return files
        if role == self.SpeedRole:
            return speed
        if role == self.QueuePositionRole:
            return queue_position
        if role == self.ProgressPercentRole:
            return max(0, min(percent, 100))
        if role == self.TitleRole:
            return title
        if role == self.DetailRole:
            return detail
        accessible_role = getattr(Qt, "AccessibleTextRole", -1)
        if role == accessible_role:
            return f"{title}，{status_text}，{detail}"
        return None

    def roleNames(self) -> dict[int, bytes]:  # noqa: N802
        roles = super().roleNames()
        roles.update(
            {
                self.TaskIdRole: b"taskId",
                self.PhaseRole: b"phase",
                self.StatusTextRole: b"statusText",
                self.StatusColorRole: b"statusColor",
                self.TotalCountRole: b"totalCount",
                self.ProcessedCountRole: b"processedCount",
                self.FileCountRole: b"fileCount",
                self.SpeedRole: b"speedBytesPerSecond",
                self.QueuePositionRole: b"queuePosition",
                self.ProgressPercentRole: b"progressPercent",
                self.TitleRole: b"title",
                self.DetailRole: b"detail",
            }
        )
        return roles

    def task_at(self, row: int) -> TaskSummaryLike | None:
        return self._tasks[row] if 0 <= row < len(self._tasks) else None

    def task_id_at(self, row: int) -> str:
        task = self.task_at(row)
        return _summary_text(task, "task_id") if task is not None else ""

    def index_for_task_id(self, task_id: str) -> QModelIndex:
        for row, task in enumerate(self._tasks):
            if _summary_text(task, "task_id") == task_id:
                return self.index(row, 0)
        return QModelIndex()

    def tasks(self) -> tuple[TaskSummaryLike, ...]:
        return tuple(self._tasks)

    def set_tasks(self, tasks: Iterable[TaskSummaryLike]) -> None:
        self.beginResetModel()
        self._tasks = list(tasks)
        self.endResetModel()
        self.tasks_changed.emit()

    def upsert_task(self, task: TaskSummaryLike) -> None:
        task_id = _summary_text(task, "task_id")
        existing = self.index_for_task_id(task_id) if task_id else QModelIndex()
        if existing.isValid():
            self._tasks[existing.row()] = task
            self.dataChanged.emit(existing, existing, [])
        else:
            row = len(self._tasks)
            self.beginInsertRows(QModelIndex(), row, row)
            self._tasks.append(task)
            self.endInsertRows()
        self.tasks_changed.emit()

    def remove_task(self, task_id: str) -> bool:
        index = self.index_for_task_id(task_id)
        if not index.isValid():
            return False
        row = index.row()
        self.beginRemoveRows(QModelIndex(), row, row)
        self._tasks.pop(row)
        self.endRemoveRows()
        self.tasks_changed.emit()
        return True


class TaskSummaryDelegate(QStyledItemDelegate):
    """Paint one privacy-safe, aggregate task summary row."""

    def sizeHint(  # noqa: N802
        self,
        _option: QStyleOptionViewItem,
        _index: QModelIndex,
    ) -> QSize:
        return QSize(260, TASK_LIST_ITEM_HEIGHT)

    def paint(  # noqa: N802
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> None:
        painter.save()
        rect = option.rect.adjusted(2, 2, -2, -2)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        background = "#E0F2FE" if selected else "#F8FAFC" if hovered else "#FFFFFF"
        painter.setPen(QColor(TASK_WIDGET_COLORS["border"]))
        painter.setBrush(QColor(background))
        painter.drawRoundedRect(rect, 7, 7)
        if selected:
            painter.fillRect(
                QRect(rect.left(), rect.top() + 7, 4, max(1, rect.height() - 14)),
                QColor(TASK_WIDGET_COLORS["primary"]),
            )

        icon_rect = QRect(rect.left() + 12, rect.top() + 13, 20, 20)
        icon = index.data(Qt.DecorationRole)
        if icon is not None:
            icon.paint(painter, icon_rect, Qt.AlignCenter)

        status_text = str(index.data(TaskListModel.StatusTextRole) or "")
        status_color = QColor(
            str(index.data(TaskListModel.StatusColorRole) or TASK_WIDGET_COLORS["muted"])
        )
        title_font = QFont(option.font)
        title_font.setBold(True)
        status_font = QFont(option.font)
        status_font.setBold(True)
        status_metrics = option.fontMetrics
        status_width = status_metrics.horizontalAdvance(status_text) + 2
        status_rect = QRect(
            rect.right() - status_width - 12,
            rect.top() + 12,
            status_width,
            22,
        )
        painter.setFont(status_font)
        painter.setPen(status_color)
        painter.drawText(status_rect, Qt.AlignRight | Qt.AlignVCenter, status_text)

        title_left = icon_rect.right() + 9
        title_right = status_rect.left() - 8
        title_rect = QRect(
            title_left,
            rect.top() + 10,
            max(1, title_right - title_left),
            24,
        )
        painter.setFont(title_font)
        painter.setPen(QColor(TASK_WIDGET_COLORS["text"]))
        title = option.fontMetrics.elidedText(
            str(index.data(TaskListModel.TitleRole) or ""),
            Qt.ElideRight,
            title_rect.width(),
        )
        painter.drawText(title_rect, Qt.AlignLeft | Qt.AlignVCenter, title)

        detail_rect = QRect(
            rect.left() + 13,
            rect.top() + 38,
            max(1, rect.width() - 26),
            20,
        )
        painter.setFont(option.font)
        painter.setPen(QColor(TASK_WIDGET_COLORS["muted"]))
        detail = option.fontMetrics.elidedText(
            str(index.data(TaskListModel.DetailRole) or ""),
            Qt.ElideRight,
            detail_rect.width(),
        )
        painter.drawText(detail_rect, Qt.AlignLeft | Qt.AlignVCenter, detail)

        track = QRect(rect.left() + 13, rect.bottom() - 11, max(1, rect.width() - 26), 4)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#E2E8F0"))
        painter.drawRoundedRect(track, 2, 2)
        percent = int(index.data(TaskListModel.ProgressPercentRole) or 0)
        if percent > 0:
            progress = QRect(
                track.left(),
                track.top(),
                max(2, int(track.width() * min(percent, 100) / 100)),
                track.height(),
            )
            painter.setBrush(QColor(TASK_WIDGET_COLORS["primary"]))
            painter.drawRoundedRect(progress, 2, 2)
        painter.restore()


class TaskSidebar(QFrame):
    new_task_requested = pyqtSignal()
    task_selected = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TaskSidebar")
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 14, 12, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("任务")
        title.setObjectName("TaskSidebarTitle")
        header.addWidget(title)
        header.addStretch()
        self.new_task_button = QPushButton("新建任务")
        self.new_task_button.setObjectName("TaskPrimaryButton")
        self.new_task_button.setAccessibleName("新建下载任务")
        self.new_task_button.setIcon(
            self.style().standardIcon(QStyle.SP_FileDialogNewFolder)
        )
        self.new_task_button.clicked.connect(self.new_task_requested)
        header.addWidget(self.new_task_button)
        layout.addLayout(header)

        self.summary_label = QLabel("暂无任务")
        self.summary_label.setObjectName("TaskSidebarSummary")
        self.summary_label.setWordWrap(True)
        self.summary_label.setAccessibleName("任务状态摘要")
        layout.addWidget(self.summary_label)

        self.model = TaskListModel(parent=self)
        self._concurrency_limit = 2
        self.list_view = QListView()
        self.list_view.setObjectName("TaskListView")
        self.list_view.setAccessibleName("下载任务列表")
        self.list_view.setModel(self.model)
        self.list_view.setItemDelegate(TaskSummaryDelegate(self.list_view))
        self.list_view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list_view.setMouseTracking(True)
        self.list_view.setSpacing(2)
        self.list_view.selectionModel().currentChanged.connect(
            self._on_current_changed
        )

        self.empty_label = QLabel("还没有下载任务\n点击“新建任务”开始")
        self.empty_label.setObjectName("TaskEmptyState")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setAccessibleName("暂无下载任务")

        self.list_stack = QStackedWidget()
        self.list_stack.addWidget(self.empty_label)
        self.list_stack.addWidget(self.list_view)
        layout.addWidget(self.list_stack, 1)

        self.model.tasks_changed.connect(self._refresh_summary)
        self._refresh_summary()

    def set_tasks(self, tasks: Iterable[TaskSummaryLike]) -> None:
        selected = self.current_task_id()
        self.model.set_tasks(tasks)
        if selected:
            index = self.model.index_for_task_id(selected)
            if index.isValid():
                self.list_view.setCurrentIndex(index)

    def clear_current_task(self) -> None:
        self.list_view.clearSelection()
        self.list_view.setCurrentIndex(QModelIndex())

    def set_concurrency_limit(self, limit: int) -> None:
        self._concurrency_limit = max(1, min(8, int(limit)))
        self._refresh_summary()

    def upsert_task(self, task: TaskSummaryLike) -> None:
        self.model.upsert_task(task)

    def remove_task(self, task_id: str) -> bool:
        return self.model.remove_task(task_id)

    def current_task_id(self) -> str:
        index = self.list_view.currentIndex()
        return str(index.data(TaskListModel.TaskIdRole) or "") if index.isValid() else ""

    def select_task(self, task_id: str) -> bool:
        index = self.model.index_for_task_id(task_id)
        if not index.isValid():
            return False
        self.list_view.setCurrentIndex(index)
        self.list_view.scrollTo(index)
        return True

    def _on_current_changed(
        self,
        current: QModelIndex,
        _previous: QModelIndex,
    ) -> None:
        if current.isValid():
            self.task_selected.emit(
                str(current.data(TaskListModel.TaskIdRole) or "")
            )

    def _refresh_summary(self) -> None:
        tasks = self.model.tasks()
        total = len(tasks)
        active_download_phases = {
            "preflight",
            "starting_receiver",
            "running",
            "downloading",
            "pause_pending",
            "stopping",
            "cancelling",
        }
        queued_download_phases = {"queued", "waiting"}
        pdi_phases = {"pdi_pending", "pdi_running"}
        attention_phases = {
            "paused",
            "download_retryable",
            "pdi_retryable",
            "partial",
            "failed",
            "interrupted",
            "recovery_required",
        }
        active = sum(
            _summary_phase(task) in active_download_phases for task in tasks
        )
        queued = sum(
            _summary_phase(task) in queued_download_phases for task in tasks
        )
        pdi = sum(_summary_phase(task) in pdi_phases for task in tasks)
        attention = sum(_summary_phase(task) in attention_phases for task in tasks)
        if not total:
            text = "暂无任务"
        else:
            parts = [f"共 {total:,} 个任务"]
            if active:
                parts.append(f"并发 {active:,}/{self._concurrency_limit:,}")
            available = max(0, self._concurrency_limit - active)
            parts.append(f"可用槽位 {available:,}")
            if queued:
                parts.append(f"等待槽位 {queued:,}")
            if pdi:
                parts.append(f"PDI {pdi:,}")
            if attention:
                parts.append(f"需处理 {attention:,}")
            text = " · ".join(parts)
        self.summary_label.setText(text)
        self.list_stack.setCurrentWidget(self.list_view if total else self.empty_label)


class TaskWorkspace(QWidget):
    """Responsive master-detail shell for the multi-task workbench."""

    new_task_requested = pyqtSignal()
    task_selected = pyqtSignal(str)
    compact_mode_changed = pyqtSignal(bool)

    def __init__(
        self,
        detail_widget: QWidget | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._compact = False
        self._compact_showing_list = True
        self._selected_task_id = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        outer.addWidget(self.splitter)

        self.sidebar = TaskSidebar()
        self.sidebar.new_task_requested.connect(self._request_new_task)
        self.sidebar.task_selected.connect(self._on_task_selected)
        self.sidebar.list_view.clicked.connect(self._on_task_clicked)
        self.splitter.addWidget(self.sidebar)

        self.detail_shell = QFrame()
        self.detail_shell.setObjectName("TaskDetailShell")
        detail_layout = QVBoxLayout(self.detail_shell)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(0)

        self.compact_toolbar = QFrame()
        self.compact_toolbar.setObjectName("TaskCompactToolbar")
        compact_layout = QHBoxLayout(self.compact_toolbar)
        compact_layout.setContentsMargins(12, 8, 12, 8)
        compact_layout.setSpacing(8)
        self.back_to_tasks_button = QPushButton("任务列表")
        self.back_to_tasks_button.setAccessibleName("返回任务列表")
        self.back_to_tasks_button.setIcon(
            self.style().standardIcon(QStyle.SP_ArrowBack)
        )
        self.back_to_tasks_button.clicked.connect(self.show_task_list)
        compact_layout.addWidget(self.back_to_tasks_button)
        compact_layout.addStretch()
        self.compact_new_task_button = QPushButton("新建任务")
        self.compact_new_task_button.setObjectName("TaskPrimaryButton")
        self.compact_new_task_button.setAccessibleName("新建下载任务")
        self.compact_new_task_button.setIcon(
            self.style().standardIcon(QStyle.SP_FileDialogNewFolder)
        )
        self.compact_new_task_button.clicked.connect(self._request_new_task)
        compact_layout.addWidget(self.compact_new_task_button)
        detail_layout.addWidget(self.compact_toolbar)

        self.detail_content = QFrame()
        self.detail_content.setObjectName("TaskDetailContent")
        self.detail_content_layout = QVBoxLayout(self.detail_content)
        self.detail_content_layout.setContentsMargins(0, 0, 0, 0)
        self.detail_content_layout.setSpacing(0)
        detail_layout.addWidget(self.detail_content, 1)
        self.splitter.addWidget(self.detail_shell)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)

        self._detail_widget: QWidget | None = None
        self.set_detail_widget(detail_widget or self._default_detail_placeholder())
        self.sidebar.model.tasks_changed.connect(self._update_compact_task_count)
        self.setStyleSheet(_TASK_WIDGET_STYLESHEET)
        self._apply_responsive_mode(self.width() < TASK_WORKSPACE_COMPACT_WIDTH)

    @property
    def is_compact(self) -> bool:
        return self._compact

    @property
    def selected_task_id(self) -> str:
        return self._selected_task_id

    def set_detail_widget(self, widget: QWidget) -> None:
        if widget is self._detail_widget:
            return
        if self._detail_widget is not None:
            self.detail_content_layout.removeWidget(self._detail_widget)
            self._detail_widget.setParent(None)
        self._detail_widget = widget
        self.detail_content_layout.addWidget(widget)

    def set_tasks(self, tasks: Iterable[TaskSummaryLike]) -> None:
        self.sidebar.set_tasks(tasks)
        if self._selected_task_id:
            if not self.sidebar.select_task(self._selected_task_id):
                self._selected_task_id = ""
                if self._compact:
                    self.show_task_list()

    def set_concurrency_limit(self, limit: int) -> None:
        self.sidebar.set_concurrency_limit(limit)

    def clear_task_selection(self) -> None:
        self._selected_task_id = ""
        self.sidebar.clear_current_task()

    def upsert_task(self, task: TaskSummaryLike) -> None:
        self.sidebar.upsert_task(task)

    def remove_task(self, task_id: str) -> bool:
        removed = self.sidebar.remove_task(task_id)
        if removed and task_id == self._selected_task_id:
            self._selected_task_id = ""
            if self._compact:
                self.show_task_list()
        return removed

    def select_task(self, task_id: str) -> bool:
        selected = self.sidebar.select_task(task_id)
        if selected:
            self._selected_task_id = task_id
            self._compact_showing_list = False
            if self._compact:
                self.show_detail()
        return selected

    def show_task_list(self) -> None:
        if not self._compact:
            return
        self._compact_showing_list = True
        self.sidebar.show()
        self.detail_shell.hide()
        self.sidebar.setFocus(Qt.OtherFocusReason)

    def show_detail(self) -> None:
        if not self._compact:
            return
        self._compact_showing_list = False
        self.sidebar.hide()
        self.detail_shell.show()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._apply_responsive_mode(self.width() < TASK_WORKSPACE_COMPACT_WIDTH)

    def _apply_responsive_mode(self, compact: bool) -> None:
        changed = compact != self._compact
        self._compact = compact
        self.compact_toolbar.setVisible(compact)
        if compact:
            self.sidebar.setMinimumWidth(0)
            self.sidebar.setMaximumWidth(16_777_215)
            if not self._compact_showing_list:
                self.show_detail()
            else:
                self.show_task_list()
        else:
            self.sidebar.setMinimumWidth(240)
            self.sidebar.setMaximumWidth(320)
            self.sidebar.show()
            self.detail_shell.show()
            available = max(1, self.width())
            self.splitter.setSizes([280, max(1, available - 280)])
        if changed:
            self.compact_mode_changed.emit(compact)

    def _request_new_task(self) -> None:
        self.clear_task_selection()
        self._compact_showing_list = False
        self.new_task_requested.emit()
        if self._compact:
            self.show_detail()

    def _on_task_selected(self, task_id: str) -> None:
        self._selected_task_id = task_id
        self._compact_showing_list = False
        self.task_selected.emit(task_id)
        if self._compact:
            self.show_detail()

    def _on_task_clicked(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        task_id = str(index.data(TaskListModel.TaskIdRole) or "")
        if task_id and task_id != self._selected_task_id:
            self._on_task_selected(task_id)
            return
        self._compact_showing_list = False
        if self._compact:
            self.show_detail()

    def _update_compact_task_count(self) -> None:
        count = self.sidebar.model.rowCount()
        self.back_to_tasks_button.setText(f"任务列表 ({count:,})")

    @staticmethod
    def _default_detail_placeholder() -> QWidget:
        placeholder = QLabel("选择一个任务查看详情，或新建下载任务")
        placeholder.setObjectName("TaskDetailPlaceholder")
        placeholder.setAlignment(Qt.AlignCenter)
        placeholder.setWordWrap(True)
        placeholder.setAccessibleName("未选择任务")
        return placeholder

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(1180, 720)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(680, 480)


_TASK_WIDGET_STYLESHEET = f"""
QFrame#TaskSidebar {{
    background: {TASK_WIDGET_COLORS['surface']};
    border-right: 1px solid {TASK_WIDGET_COLORS['border']};
}}
QLabel#TaskSidebarTitle {{
    color: {TASK_WIDGET_COLORS['text']};
    font-size: 16px;
    font-weight: 700;
}}
QLabel#TaskSidebarSummary {{ color: {TASK_WIDGET_COLORS['muted']}; }}
QLabel#TaskEmptyState, QLabel#TaskDetailPlaceholder {{
    color: {TASK_WIDGET_COLORS['muted']};
    background: {TASK_WIDGET_COLORS['background']};
}}
QListView#TaskListView {{
    background: {TASK_WIDGET_COLORS['surface']};
    border: 0;
    outline: 0;
}}
QFrame#TaskDetailShell, QFrame#TaskDetailContent {{
    background: {TASK_WIDGET_COLORS['background']};
}}
QFrame#TaskCompactToolbar {{
    background: {TASK_WIDGET_COLORS['surface']};
    border-bottom: 1px solid {TASK_WIDGET_COLORS['border']};
}}
QPushButton#TaskPrimaryButton {{
    color: white;
    background: {TASK_WIDGET_COLORS['primary']};
    border: 1px solid {TASK_WIDGET_COLORS['primary']};
    border-radius: 6px;
    padding: 7px 12px;
    font-weight: 650;
}}
QPushButton#TaskPrimaryButton:hover {{
    background: {TASK_WIDGET_COLORS['primary_hover']};
}}
QPushButton#TaskPrimaryButton:focus {{ border: 2px solid #FBBF24; }}
"""


__all__ = [
    "TASK_LIST_ITEM_HEIGHT",
    "TASK_WIDGET_COLORS",
    "TASK_WORKSPACE_COMPACT_WIDTH",
    "TaskListModel",
    "TaskSidebar",
    "TaskSummaryDelegate",
    "TaskSummaryLike",
    "TaskWorkspace",
    "format_task_transfer_rate",
]
