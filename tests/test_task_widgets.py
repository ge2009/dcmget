from __future__ import annotations

from dataclasses import dataclass

from PyQt5.QtCore import QRect, Qt
from PyQt5.QtGui import QPainter, QPixmap
from PyQt5.QtWidgets import QApplication, QLabel, QStyleOptionViewItem

from dcmget.task_manager import TaskSummary
from dcmget.task_widgets import (
    TASK_LIST_ITEM_HEIGHT,
    TASK_WORKSPACE_COMPACT_WIDTH,
    TaskListModel,
    TaskSidebar,
    TaskSummaryDelegate,
    TaskWorkspace,
    format_task_transfer_rate,
)


@dataclass(frozen=True, slots=True)
class Summary:
    task_id: str
    name: str = ""
    phase: str = "queued"
    total_count: int = 0
    processed_count: int = 0
    pending_count: int = 0
    completed_count: int = 0
    failed_count: int = 0
    file_count: int = 0
    received_bytes: int = 0
    speed_bytes_per_second: float = 0.0
    queue_position: int | None = None
    current_accession: str = ""
    error_message: str = ""
    created_at: str = "2026-07-17T09:32:00"
    updated_at: str = "2026-07-17T09:32:00"


def test_task_model_exposes_aggregate_roles_and_never_displays_patient_name(qtbot):
    patient_name = "患者张三"
    summary = Summary(
        task_id="a" * 32,
        name=patient_name,
        phase="downloading",
        total_count=40000,
        processed_count=1234,
        file_count=5678,
        speed_bytes_per_second=1.5 * 1024 * 1024,
        queue_position=2,
        current_accession="A001",
    )
    model = TaskListModel([summary])
    index = model.index(0, 0)

    assert model.rowCount() == 1
    assert index.data(TaskListModel.TaskIdRole) == "a" * 32
    assert index.data(TaskListModel.PhaseRole) == "downloading"
    assert index.data(TaskListModel.StatusTextRole) == "并发下载中"
    assert index.data(TaskListModel.TotalCountRole) == 40000
    assert index.data(TaskListModel.ProcessedCountRole) == 1234
    assert index.data(TaskListModel.FileCountRole) == 5678
    assert index.data(TaskListModel.SpeedRole) == 1.5 * 1024 * 1024
    assert index.data(TaskListModel.QueuePositionRole) == 2
    assert index.data(TaskListModel.ProgressPercentRole) == 3
    assert "40,000 个检查号" in index.data(TaskListModel.TitleRole)
    assert "1.5 MB/s" in index.data(TaskListModel.DetailRole)
    assert not index.data(Qt.DecorationRole).isNull()

    safe_roles = (
        Qt.DisplayRole,
        Qt.ToolTipRole,
        TaskListModel.TitleRole,
        TaskListModel.DetailRole,
        getattr(Qt, "AccessibleTextRole", Qt.DisplayRole),
    )
    for role in safe_roles:
        assert patient_name not in str(index.data(role) or "")


def test_40000_accession_task_is_one_summary_row_not_accession_widgets(qtbot):
    model = TaskListModel(
        [
            Summary(
                task_id="large",
                total_count=40000,
                processed_count=39999,
                file_count=120000,
            )
        ]
    )

    assert model.rowCount() == 1
    assert "39,999/40,000" in model.index(0, 0).data(TaskListModel.DetailRole)
    assert model.index(0, 0).data(Qt.SizeHintRole).height() == TASK_LIST_ITEM_HEIGHT


def test_task_model_upsert_remove_and_status_alias(qtbot):
    class StatusOnly:
        task_id = "legacy"
        status = "failed"
        accession_count = 3
        processed_count = 3
        file_count = 1
        speed_bytes_per_second = 0
        queue_position = None
        created_at = ""

    model = TaskListModel([StatusOnly()])
    index = model.index(0, 0)
    assert index.data(TaskListModel.StatusTextRole) == "失败"
    assert index.data(TaskListModel.TotalCountRole) == 3

    model.upsert_task(
        Summary(
            task_id="legacy",
            phase="completed",
            total_count=3,
            processed_count=3,
            file_count=10,
        )
    )
    assert model.rowCount() == 1
    assert model.index(0, 0).data(TaskListModel.StatusTextRole) == "完成"
    assert model.remove_task("legacy")
    assert model.rowCount() == 0
    assert not model.remove_task("missing")


def test_task_model_accepts_task_manager_summary_contract(qtbot):
    summary = TaskSummary(
        task_id="manager-task",
        name="must-not-be-rendered",
        phase="running",
        total_count=8,
        processed_count=3,
        pending_count=5,
        completed_count=3,
        failed_count=0,
        file_count=24,
        received_bytes=1024,
        speed_bytes_per_second=2048,
        queue_position=None,
        current_accession="A004",
        error_message="",
        created_at="2026-07-17T10:00:00+08:00",
        updated_at="2026-07-17T10:01:00+08:00",
    )
    model = TaskListModel([summary])
    index = model.index(0, 0)

    assert index.data(TaskListModel.StatusTextRole) == "并发下载中"
    assert index.data(TaskListModel.ProcessedCountRole) == 3
    assert "must-not-be-rendered" not in index.data(Qt.DisplayRole)


def test_task_delegate_paints_offscreen_without_patient_text(qtbot):
    model = TaskListModel(
        [
            Summary(
                task_id="paint",
                name="患者李四",
                phase="partial",
                total_count=20,
                processed_count=10,
                file_count=30,
            )
        ]
    )
    delegate = TaskSummaryDelegate()
    pixmap = QPixmap(320, TASK_LIST_ITEM_HEIGHT)
    pixmap.fill(Qt.white)
    painter = QPainter(pixmap)
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, pixmap.width(), pixmap.height())
    option.font = QApplication.font()
    option.fontMetrics = QApplication.fontMetrics()

    delegate.paint(painter, option, model.index(0, 0))
    painter.end()

    assert not pixmap.isNull()
    assert delegate.sizeHint(option, model.index(0, 0)).height() == TASK_LIST_ITEM_HEIGHT


def test_sidebar_summary_selection_and_new_task_signal(qtbot):
    sidebar = TaskSidebar()
    qtbot.addWidget(sidebar)
    sidebar.resize(300, 540)
    sidebar.show()
    tasks = [
        Summary("running", phase="downloading", total_count=10),
        Summary("queued", phase="queued", total_count=20, queue_position=1),
        Summary("failed", phase="failed", total_count=2, processed_count=2),
    ]
    sidebar.set_tasks(tasks)
    sidebar.set_concurrency_limit(2)

    assert sidebar.list_stack.currentWidget() is sidebar.list_view
    assert sidebar.model.rowCount() == 3
    assert sidebar.list_view.horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
    assert "共 3 个任务" in sidebar.summary_label.text()
    assert "并发 1/2" in sidebar.summary_label.text()
    assert "可用槽位 1" in sidebar.summary_label.text()
    assert "等待槽位 1" in sidebar.summary_label.text()
    assert "需处理 1" in sidebar.summary_label.text()

    with qtbot.waitSignal(sidebar.task_selected) as selected:
        assert sidebar.select_task("queued")
    assert selected.args == ["queued"]

    with qtbot.waitSignal(sidebar.new_task_requested):
        qtbot.mouseClick(sidebar.new_task_button, Qt.LeftButton)

    sidebar.set_tasks([])
    assert sidebar.list_stack.currentWidget() is sidebar.empty_label
    assert sidebar.summary_label.text() == "暂无任务"


def test_workspace_wide_master_detail_and_compact_full_page_navigation(qtbot):
    detail = QLabel("任务详情")
    workspace = TaskWorkspace(detail)
    qtbot.addWidget(workspace)
    workspace.set_tasks(
        [Summary("one", phase="downloading", total_count=40000)]
    )

    workspace.resize(1180, 720)
    workspace.show()
    QApplication.processEvents()
    assert not workspace.is_compact
    assert workspace.sidebar.isVisible()
    assert workspace.detail_shell.isVisible()
    assert workspace.compact_toolbar.isHidden()
    assert workspace.splitter.sizes()[0] <= 320

    with qtbot.waitSignal(workspace.task_selected) as selected:
        assert workspace.select_task("one")
    assert selected.args == ["one"]
    assert workspace.selected_task_id == "one"

    workspace.resize(TASK_WORKSPACE_COMPACT_WIDTH - 1, 600)
    QApplication.processEvents()
    assert workspace.is_compact
    assert workspace.sidebar.isHidden()
    assert workspace.detail_shell.isVisible()
    assert workspace.compact_toolbar.isVisible()
    assert workspace.back_to_tasks_button.text() == "任务列表 (1)"

    qtbot.mouseClick(workspace.back_to_tasks_button, Qt.LeftButton)
    assert workspace.sidebar.isVisible()
    assert workspace.detail_shell.isHidden()

    item_rect = workspace.sidebar.list_view.visualRect(
        workspace.sidebar.model.index(0, 0)
    )
    qtbot.mouseClick(
        workspace.sidebar.list_view.viewport(),
        Qt.LeftButton,
        pos=item_rect.center(),
    )
    QApplication.processEvents()
    assert workspace.sidebar.isHidden()
    assert workspace.detail_shell.isVisible()


def test_compact_new_task_switches_to_detail_and_emits_signal(qtbot):
    workspace = TaskWorkspace(QLabel("新建任务编辑器"))
    qtbot.addWidget(workspace)
    workspace.resize(683, 480)
    workspace.show()
    QApplication.processEvents()
    assert workspace.is_compact
    assert workspace.sidebar.isVisible()

    with qtbot.waitSignal(workspace.new_task_requested):
        qtbot.mouseClick(workspace.sidebar.new_task_button, Qt.LeftButton)

    assert workspace.selected_task_id == ""
    assert workspace.sidebar.isHidden()
    assert workspace.detail_shell.isVisible()
    assert workspace.minimumSizeHint().width() <= 683
    assert workspace.minimumSizeHint().height() <= 480


def test_new_task_editor_is_not_replaced_when_background_tasks_refresh(qtbot):
    workspace = TaskWorkspace(QLabel("新建任务编辑器"))
    qtbot.addWidget(workspace)
    task = Summary("running", phase="downloading", total_count=2)
    workspace.set_tasks([task])
    assert workspace.select_task(task.task_id)
    QApplication.processEvents()

    workspace._request_new_task()
    QApplication.processEvents()
    selected_after_refresh: list[str] = []
    workspace.task_selected.connect(selected_after_refresh.append)

    workspace.set_tasks([task])
    QApplication.processEvents()

    assert workspace.selected_task_id == ""
    assert workspace.sidebar.current_task_id() == ""
    assert selected_after_refresh == []


def test_sidebar_reports_concurrent_download_slots_separately_from_pdi(qtbot):
    sidebar = TaskSidebar()
    qtbot.addWidget(sidebar)
    sidebar.set_concurrency_limit(3)
    sidebar.set_tasks(
        [
            Summary("run-a", phase="running"),
            Summary("run-b", phase="downloading"),
            Summary("queued", phase="queued", queue_position=1),
            Summary("pdi", phase="pdi_running"),
        ]
    )

    assert "并发 2/3" in sidebar.summary_label.text()
    assert "可用槽位 1" in sidebar.summary_label.text()
    assert "等待槽位 1" in sidebar.summary_label.text()
    assert "PDI 1" in sidebar.summary_label.text()
    queued = sidebar.model.index_for_task_id("queued")
    assert queued.data(TaskListModel.StatusTextRole) == "等待并发槽"


def test_transfer_rate_formatting_matches_task_summary_units():
    assert format_task_transfer_rate(0) == "—"
    assert format_task_transfer_rate(512) == "512 B/s"
    assert format_task_transfer_rate(1536) == "1.5 KB/s"
    assert format_task_transfer_rate(2.5 * 1024 * 1024) == "2.5 MB/s"
