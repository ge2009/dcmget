use std::sync::Arc;

use dcmget_ui_kit::{
    ActionButton, ActionButtonKind, CommandSink, DesktopCommand, MetricCard, Panel, ProfileId,
    ProfileSummary, RecordingCommandSink, SectionHeading, StatusPill, TaskStatus,
    WorkspaceSnapshot, initialize_light_theme,
};
use gpui::{
    AnyElement, App, AppContext, Application, Context, Entity, FocusHandle, Focusable,
    InteractiveElement, IntoElement, ParentElement, Render, SharedString,
    StatefulInteractiveElement, Styled, Subscription, Window, WindowBounds, WindowOptions, div,
    prelude::FluentBuilder, px, relative, size,
};
use gpui_component::{
    ActiveTheme, Icon, IconName, Root, StyledExt, WindowExt,
    checkbox::Checkbox,
    h_flex,
    input::{Input, InputEvent, InputState},
    progress::Progress,
    v_flex,
};
use gpui_component_assets::Assets;

pub fn run() {
    let app = Application::new().with_assets(Assets);
    app.run(move |cx| {
        initialize_light_theme(cx);

        let options = WindowOptions {
            window_bounds: Some(WindowBounds::centered(size(px(1280.0), px(820.0)), cx)),
            titlebar: Some(gpui::TitlebarOptions {
                title: Some("DcmGet DICOM 影像下载".into()),
                ..Default::default()
            }),
            ..Default::default()
        };

        cx.spawn(async move |cx| {
            cx.open_window(options, |window, cx| {
                let sink: Arc<dyn CommandSink> = Arc::new(RecordingCommandSink::default());
                let workspace = cx.new(|cx| Workbench::new(window, cx, sink));
                cx.new(|cx| Root::new(workspace, window, cx))
            })?;
            Ok::<_, anyhow::Error>(())
        })
        .detach();
    });
}

struct Workbench {
    snapshot: WorkspaceSnapshot,
    sink: Arc<dyn CommandSink>,
    accession_input: Entity<InputState>,
    destination_input: Entity<InputState>,
    focus_handle: FocusHandle,
    _subscriptions: Vec<Subscription>,
}

impl Workbench {
    fn new(window: &mut Window, cx: &mut Context<Self>, sink: Arc<dyn CommandSink>) -> Self {
        let accession_input = cx.new(|cx| {
            InputState::new(window, cx)
                .placeholder("粘贴检查号，每行一个")
                .multi_line(true)
        });
        let destination_input = cx
            .new(|cx| InputState::new(window, cx).placeholder("选择影像保存目录，例如 D:\\DICOM"));
        let subscriptions = vec![
            cx.subscribe_in(&accession_input, window, |_, _, event, _, cx| {
                if matches!(event, InputEvent::Change) {
                    cx.notify();
                }
            }),
            cx.subscribe_in(&destination_input, window, |_, _, event, _, cx| {
                if matches!(event, InputEvent::Change) {
                    cx.notify();
                }
            }),
        ];

        Self {
            snapshot: WorkspaceSnapshot::technical_gate_sample(),
            sink,
            accession_input,
            destination_input,
            focus_handle: cx.focus_handle(),
            _subscriptions: subscriptions,
        }
    }

    fn submit(&self, command: DesktopCommand) {
        self.sink.submit(command);
    }

    fn select_profile(&mut self, profile_id: ProfileId, cx: &mut Context<Self>) {
        self.snapshot.selected_profile_id = profile_id.clone();
        self.submit(DesktopCommand::SelectProfile(profile_id));
        cx.notify();
    }

    fn create_task(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        let accessions = self.accession_input.read(cx).value().trim().to_owned();
        let destination = self.destination_input.read(cx).value().trim().to_owned();
        if accessions.is_empty() || destination.is_empty() {
            window.push_notification("请先填写检查号和目标目录。", cx);
            return;
        }

        self.submit(DesktopCommand::CreateTask {
            profile_id: self.snapshot.selected_profile_id.clone(),
            accessions,
            destination,
        });
        window.push_notification("任务命令已提交。技术门禁壳不会直接访问磁盘或 PACS。", cx);
    }

    fn show_settings(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        self.submit(DesktopCommand::OpenSettings);
        let selected = self.snapshot.selected_profile().cloned();
        window.open_sheet(cx, move |sheet, _, _| {
            sheet
                .size(px(460.0))
                .title("Profile 设置")
                .child(
                    v_flex()
                        .gap_4()
                        .child(
                            div().text_sm().text_color(gpui::rgb(0x5c_6b_70)).child(
                                "业务字段只展示只读快照；保存操作由 ApplicationService 接管。",
                            ),
                        )
                        .child(setting_row(
                            "Profile",
                            selected
                                .as_ref()
                                .map_or_else(|| "—".into(), |p| p.name.clone()),
                        ))
                        .child(setting_row(
                            "接收 AE",
                            selected
                                .as_ref()
                                .map_or_else(|| "—".into(), |p| p.ae_title.clone()),
                        ))
                        .child(setting_row(
                            "SCP 端口",
                            selected
                                .as_ref()
                                .map_or_else(|| "—".into(), |p| p.port.to_string()),
                        )),
                )
                .footer(
                    h_flex().justify_end().child(
                        ActionButton::new("close-settings", "完成", IconName::Check)
                            .kind(ActionButtonKind::Primary)
                            .on_click(|_, window, cx| window.close_sheet(cx)),
                    ),
                )
        });
    }

    fn render_sidebar(&self, cx: &mut Context<Self>) -> impl IntoElement {
        v_flex()
            .w(px(264.0))
            .h_full()
            .flex_shrink_0()
            .border_r_1()
            .border_color(cx.theme().border)
            .bg(cx.theme().popover)
            .child(
                v_flex()
                    .gap_1()
                    .p_5()
                    .border_b_1()
                    .border_color(cx.theme().border)
                    .child(
                        h_flex()
                            .gap_3()
                            .items_center()
                            .child(
                                div()
                                    .size_10()
                                    .rounded_lg()
                                    .bg(cx.theme().primary)
                                    .text_color(cx.theme().primary_foreground)
                                    .flex()
                                    .items_center()
                                    .justify_center()
                                    .child(Icon::new(IconName::ArrowDown).size_5()),
                            )
                            .child(
                                v_flex()
                                    .gap_0()
                                    .child(div().font_semibold().child("DcmGet"))
                                    .child(
                                        div()
                                            .text_xs()
                                            .text_color(cx.theme().muted_foreground)
                                            .child("影像接收工作台"),
                                    ),
                            ),
                    ),
            )
            .child(
                v_flex()
                    .flex_1()
                    .gap_2()
                    .p_3()
                    .child(
                        h_flex()
                            .px_2()
                            .py_2()
                            .items_center()
                            .justify_between()
                            .child(
                                div()
                                    .text_xs()
                                    .font_semibold()
                                    .text_color(cx.theme().muted_foreground)
                                    .child("PROFILE"),
                            )
                            .child(
                                div()
                                    .text_xs()
                                    .child(self.snapshot.profiles.len().to_string()),
                            ),
                    )
                    .children(self.snapshot.profiles.iter().cloned().enumerate().map(
                        |(profile_index, profile)| {
                            self.render_profile_item(profile_index, profile, cx)
                        },
                    )),
            )
            .child(
                v_flex()
                    .p_4()
                    .border_t_1()
                    .border_color(cx.theme().border)
                    .child(
                        ActionButton::new("settings", "设置", IconName::Settings2)
                            .kind(ActionButtonKind::Ghost)
                            .on_click(cx.listener(|this, _, window, cx| {
                                this.show_settings(window, cx);
                            })),
                    ),
            )
    }

    fn render_profile_item(
        &self,
        profile_index: usize,
        profile: ProfileSummary,
        cx: &mut Context<Self>,
    ) -> AnyElement {
        let profile_id = profile.id.clone();
        let is_selected = self.snapshot.selected_profile_id == profile.id;
        div()
            .id(("profile", profile_index))
            .cursor_pointer()
            .rounded_lg()
            .px_3()
            .py_3()
            .when(is_selected, |element| {
                element
                    .bg(cx.theme().primary.opacity(0.09))
                    .border_1()
                    .border_color(cx.theme().primary.opacity(0.25))
            })
            .when(!is_selected, |element| {
                element.hover(|style| style.bg(cx.theme().muted.opacity(0.7)))
            })
            .on_click(cx.listener(move |this, _, _, cx| {
                this.select_profile(profile_id.clone(), cx);
            }))
            .child(
                v_flex()
                    .gap_2()
                    .child(
                        h_flex()
                            .items_center()
                            .justify_between()
                            .child(div().font_semibold().child(profile.name))
                            .child(StatusPill::profile(profile.status)),
                    )
                    .child(
                        div()
                            .text_xs()
                            .text_color(cx.theme().muted_foreground)
                            .child(format!("{} · 端口 {}", profile.ae_title, profile.port)),
                    ),
            )
            .into_any_element()
    }

    fn render_header(&self, cx: &mut Context<Self>) -> impl IntoElement {
        h_flex()
            .w_full()
            .items_center()
            .justify_between()
            .px_6()
            .py_4()
            .border_b_1()
            .border_color(cx.theme().border)
            .bg(cx.theme().popover)
            .child(
                v_flex()
                    .gap_1()
                    .child(
                        div()
                            .text_xs()
                            .font_semibold()
                            .text_color(cx.theme().primary)
                            .child("DICOM OPERATIONS"),
                    )
                    .child(div().text_xl().font_semibold().child("下载工作台")),
            )
            .child(
                h_flex()
                    .items_center()
                    .gap_5()
                    .child(
                        v_flex()
                            .items_end()
                            .gap_0()
                            .child(
                                div()
                                    .text_xs()
                                    .text_color(cx.theme().muted_foreground)
                                    .child("所有 Profile 总速度"),
                            )
                            .child(div().font_semibold().child(format_speed(
                                self.snapshot.aggregate_speed_bytes_per_second,
                            ))),
                    )
                    .child(StatusPill::receiver(self.snapshot.receiver_status)),
            )
    }

    fn render_new_task(&self, cx: &mut Context<Self>) -> impl IntoElement {
        let count = self
            .accession_input
            .read(cx)
            .value()
            .lines()
            .filter(|line| !line.trim().is_empty())
            .count();
        Panel::new()
            .child(
                h_flex()
                    .items_start()
                    .justify_between()
                    .child(SectionHeading::new(
                        "新建任务",
                        "从检查号开始",
                        "每行一个检查号；后台会负责去重、预检和恢复点。",
                    ))
                    .child(
                        div()
                            .text_sm()
                            .text_color(cx.theme().muted_foreground)
                            .child(format!("已识别 {count} 条")),
                    ),
            )
            .child(
                h_flex()
                    .w_full()
                    .gap_3()
                    .child(
                        div()
                            .flex_1()
                            .min_w(px(280.0))
                            .child(Input::new(&self.accession_input).h(px(88.0))),
                    )
                    .child(
                        v_flex()
                            .w(relative(0.46))
                            .gap_3()
                            .child(Input::new(&self.destination_input))
                            .child(
                                ActionButton::new(
                                    "create-task",
                                    "预检并开始",
                                    IconName::ArrowRight,
                                )
                                .kind(ActionButtonKind::Primary)
                                .on_click(cx.listener(
                                    |this, _, window, cx| {
                                        this.create_task(window, cx);
                                    },
                                )),
                            ),
                    ),
            )
    }

    fn render_metrics(&self) -> impl IntoElement {
        let task = self.snapshot.tasks.first();
        h_flex()
            .w_full()
            .gap_3()
            .flex_wrap()
            .child(MetricCard::new(
                "当前检查号",
                task.and_then(|task| task.current_accession.clone())
                    .unwrap_or_else(|| "—".into()),
                "当前正在处理的检查",
                IconName::Search,
            ))
            .child(MetricCard::new(
                "接收文件",
                task.map_or_else(|| "0".into(), |task| task.files.to_string()),
                "本任务唯一 DICOM 文件",
                IconName::GalleryVerticalEnd,
            ))
            .child(MetricCard::new(
                "实时速度",
                format_speed(task.map_or(0, |task| task.speed_bytes_per_second)),
                "最近 10 秒平滑速度",
                IconName::ChartPie,
            ))
            .child(MetricCard::new(
                "需要处理",
                self.snapshot.errors.len().to_string(),
                "默认只显示错误日志",
                IconName::TriangleAlert,
            ))
    }

    fn render_task_summary(&self, cx: &mut Context<Self>) -> impl IntoElement {
        let task = self.snapshot.tasks.first().cloned();
        Panel::new().child(if let Some(task) = task {
            v_flex()
                .gap_4()
                .child(
                    h_flex()
                        .items_center()
                        .justify_between()
                        .child(
                            v_flex()
                                .gap_1()
                                .child(div().font_semibold().child(task.title.clone()))
                                .child(
                                    div()
                                        .text_sm()
                                        .text_color(cx.theme().muted_foreground)
                                        .child(format!(
                                            "已处理 {} / {} · {} 个文件",
                                            task.completed, task.total, task.files
                                        )),
                                ),
                        )
                        .child(StatusPill::task(task.status)),
                )
                .child(Progress::new().value(task.progress_percent()).h(px(7.0)))
                .child(
                    h_flex()
                        .justify_between()
                        .child(
                            div()
                                .text_xs()
                                .text_color(cx.theme().muted_foreground)
                                .child(format!("{:.0}%", task.progress_percent())),
                        )
                        .child(task_action(task.status, task.id, Arc::clone(&self.sink))),
                )
        } else {
            v_flex()
                .items_center()
                .gap_2()
                .py_6()
                .child(Icon::new(IconName::Inbox).size_6())
                .child("还没有任务")
        })
    }

    fn render_error_log(&self, cx: &mut Context<Self>) -> impl IntoElement {
        let detailed = self.snapshot.detailed_logs_enabled;
        Panel::new()
            .child(
                h_flex()
                    .items_center()
                    .justify_between()
                    .child(SectionHeading::new(
                        "运行日志",
                        "需要关注",
                        "默认仅显示错误；详细日志按需开启。",
                    ))
                    .child(
                        Checkbox::new("detailed-logs")
                            .label("显示详细日志")
                            .checked(detailed)
                            .on_click({
                                let sink = Arc::clone(&self.sink);
                                move |checked, _, _| {
                                    sink.submit(DesktopCommand::ToggleDetailedLogs(*checked));
                                }
                            }),
                    ),
            )
            .children(self.snapshot.errors.iter().map(|entry| {
                h_flex()
                    .w_full()
                    .items_start()
                    .gap_3()
                    .p_3()
                    .rounded_lg()
                    .bg(cx.theme().danger.opacity(0.07))
                    .border_l_2()
                    .border_color(cx.theme().danger)
                    .child(
                        Icon::new(IconName::TriangleAlert)
                            .size_5()
                            .text_color(cx.theme().danger),
                    )
                    .child(
                        v_flex()
                            .gap_1()
                            .flex_1()
                            .child(
                                h_flex()
                                    .gap_2()
                                    .text_xs()
                                    .text_color(cx.theme().muted_foreground)
                                    .child(entry.timestamp.clone())
                                    .child("·")
                                    .child(entry.source.clone()),
                            )
                            .child(div().text_sm().child(entry.message.clone())),
                    )
            }))
    }
}

impl Focusable for Workbench {
    fn focus_handle(&self, _: &App) -> FocusHandle {
        self.focus_handle.clone()
    }
}

impl Render for Workbench {
    fn render(&mut self, _: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        h_flex()
            .id("dcmget-workbench")
            .track_focus(&self.focus_handle)
            .size_full()
            .min_w(px(960.0))
            .bg(cx.theme().background)
            .text_color(cx.theme().foreground)
            .child(self.render_sidebar(cx))
            .child(
                v_flex()
                    .flex_1()
                    .h_full()
                    .overflow_hidden()
                    .child(self.render_header(cx))
                    .child(
                        v_flex()
                            .id("content-scroll")
                            .flex_1()
                            .overflow_y_scroll()
                            .gap_4()
                            .p_5()
                            .child(self.render_new_task(cx))
                            .child(self.render_metrics())
                            .child(self.render_task_summary(cx))
                            .child(self.render_error_log(cx))
                            .child(div().h(px(8.0))),
                    ),
            )
    }
}

fn setting_row(label: &'static str, value: String) -> impl IntoElement {
    h_flex()
        .items_center()
        .justify_between()
        .py_3()
        .border_b_1()
        .border_color(gpui::rgb(0xd9_e1_e4))
        .child(div().text_sm().child(label))
        .child(div().font_semibold().child(value))
}

fn task_action(
    status: TaskStatus,
    task_id: String,
    sink: Arc<dyn CommandSink>,
) -> impl IntoElement {
    let (label, icon, command) = match status {
        TaskStatus::Running => ("暂停", IconName::Minus, DesktopCommand::PauseTask(task_id)),
        TaskStatus::Paused => (
            "继续",
            IconName::ArrowRight,
            DesktopCommand::ResumeTask(task_id),
        ),
        _ => (
            "结束任务",
            IconName::CircleX,
            DesktopCommand::CancelTask(task_id),
        ),
    };
    ActionButton::new("task-action", label, icon)
        .kind(ActionButtonKind::Ghost)
        .on_click(move |_, _, _| sink.submit(command.clone()))
}

fn format_speed(bytes_per_second: u64) -> SharedString {
    if bytes_per_second >= 1_000_000_000 {
        format_scaled_speed(bytes_per_second, 1_000_000_000, "GB/s").into()
    } else if bytes_per_second >= 1_000_000 {
        format_scaled_speed(bytes_per_second, 1_000_000, "MB/s").into()
    } else if bytes_per_second >= 1_000 {
        format_scaled_speed(bytes_per_second, 1_000, "KB/s").into()
    } else {
        format!("{bytes_per_second} B/s").into()
    }
}

fn format_scaled_speed(value: u64, unit: u64, suffix: &str) -> String {
    let whole = value / unit;
    let tenth = value % unit * 10 / unit;
    format!("{whole}.{tenth} {suffix}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn speed_uses_readable_units() {
        assert_eq!(format_speed(0).as_ref(), "0 B/s");
        assert_eq!(format_speed(27_400_000).as_ref(), "27.4 MB/s");
    }
}
