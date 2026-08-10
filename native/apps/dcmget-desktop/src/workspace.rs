use std::{
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

use dcmget_ui_kit::{
    ActionButton, ActionButtonKind, DesktopCommand, LogLevel, MetricCard, Panel, ProfileId,
    ProfileSummary, SectionHeading, StatusPill, TaskStatus, WorkspaceBackend, WorkspaceSnapshot,
    initialize_light_theme,
};
use gpui::{
    AnyElement, App, AppContext, Application, Context, Entity, FocusHandle, Focusable,
    InteractiveElement, IntoElement, ParentElement, Render, SharedString,
    StatefulInteractiveElement, Styled, Subscription, Timer, Window, WindowBounds, WindowOptions,
    div, prelude::FluentBuilder, px, relative, size,
};
use gpui_component::{
    ActiveTheme, Icon, IconName, Root, StyledExt, WindowExt,
    checkbox::Checkbox,
    dialog::DialogButtonProps,
    h_flex,
    input::{Input, InputEvent, InputState},
    progress::Progress,
    v_flex,
};
use gpui_component_assets::Assets;

pub fn run(backend: Arc<dyn WorkspaceBackend>) {
    let app = Application::new().with_assets(Assets);
    app.run(move |cx| {
        initialize_light_theme(cx);

        let shutdown_backend = Arc::clone(&backend);
        cx.on_app_quit(move |_| {
            let shutdown_backend = Arc::clone(&shutdown_backend);
            async move {
                let _ = shutdown_backend.shutdown();
            }
        })
        .detach();
        cx.on_window_closed(|cx| {
            if cx.windows().is_empty() {
                cx.quit();
            }
        })
        .detach();

        let options = WindowOptions {
            window_bounds: Some(WindowBounds::centered(size(px(1280.0), px(820.0)), cx)),
            titlebar: Some(gpui::TitlebarOptions {
                title: Some("DcmGet DICOM 影像下载".into()),
                ..Default::default()
            }),
            ..Default::default()
        };

        cx.spawn(async move |cx| {
            let backend = Arc::clone(&backend);
            cx.open_window(options, |window, cx| {
                let workspace = cx.new(|cx| Workbench::new(window, cx, backend));
                cx.new(|cx| Root::new(workspace, window, cx))
            })?;
            Ok::<_, anyhow::Error>(())
        })
        .detach();
    });
}

struct Workbench {
    snapshot: WorkspaceSnapshot,
    backend: Arc<dyn WorkspaceBackend>,
    accession_input: Entity<InputState>,
    destination_input: Entity<InputState>,
    focus_handle: FocusHandle,
    _subscriptions: Vec<Subscription>,
}

impl Workbench {
    fn new(
        window: &mut Window,
        cx: &mut Context<Self>,
        backend: Arc<dyn WorkspaceBackend>,
    ) -> Self {
        let mut snapshot = backend
            .snapshot()
            .unwrap_or_else(WorkspaceSnapshot::load_failed);
        if snapshot.selected_profile_id.is_none() {
            snapshot.selected_profile_id =
                snapshot.profiles.first().map(|profile| profile.id.clone());
        }
        let default_destination = snapshot
            .selected_profile()
            .map_or("", |profile| profile.settings.default_destination.as_str())
            .to_owned();
        let accession_input = cx.new(|cx| {
            InputState::new(window, cx)
                .placeholder("粘贴检查号，每行一个")
                .multi_line(true)
        });
        let destination_input = cx.new(|cx| {
            InputState::new(window, cx)
                .placeholder("选择影像保存目录，例如 D:\\DICOM")
                .default_value(default_destination)
        });
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

        let mut workbench = Self {
            snapshot,
            backend,
            accession_input,
            destination_input,
            focus_handle: cx.focus_handle(),
            _subscriptions: subscriptions,
        };
        workbench.schedule_refresh(cx);
        workbench
    }

    fn submit(&self, command: DesktopCommand) {
        self.backend.submit(command);
    }

    fn schedule_refresh(&mut self, cx: &mut Context<Self>) {
        let backend = Arc::clone(&self.backend);
        cx.spawn(async move |this, cx| {
            Timer::after(Duration::from_millis(250)).await;
            let snapshot = backend
                .snapshot()
                .unwrap_or_else(WorkspaceSnapshot::load_failed);
            if let Some(this) = this.upgrade() {
                this.update(cx, |this, cx| {
                    this.apply_snapshot(snapshot);
                    this.schedule_refresh(cx);
                    cx.notify();
                })
                .ok();
            }
        })
        .detach();
    }

    fn apply_snapshot(&mut self, mut snapshot: WorkspaceSnapshot) {
        let selected = self.snapshot.selected_profile_id.as_ref();
        snapshot.selected_profile_id = selected
            .and_then(|selected| {
                snapshot
                    .profiles
                    .iter()
                    .any(|profile| &profile.id == selected)
                    .then(|| selected.clone())
            })
            .or_else(|| snapshot.profiles.first().map(|profile| profile.id.clone()));
        self.snapshot = snapshot;
    }

    fn select_profile(
        &mut self,
        profile_id: ProfileId,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) {
        let destination_is_empty = self.destination_input.read(cx).value().trim().is_empty();
        let default_destination = self
            .snapshot
            .profiles
            .iter()
            .find(|profile| profile.id == profile_id)
            .map(|profile| profile.settings.default_destination.clone());
        self.snapshot.selected_profile_id = Some(profile_id.clone());
        self.submit(DesktopCommand::SelectProfile(profile_id));
        if destination_is_empty && let Some(default_destination) = default_destination {
            self.destination_input.update(cx, |input, cx| {
                input.set_value(default_destination, window, cx);
            });
        }
        cx.notify();
    }

    fn create_task(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        let accessions = self.accession_input.read(cx).value().trim().to_owned();
        let destination = self.destination_input.read(cx).value().trim().to_owned();
        if accessions.is_empty() || destination.is_empty() {
            window.push_notification("请先填写检查号和目标目录。", cx);
            return;
        }

        let Some(profile_id) = self.snapshot.selected_profile_id.clone() else {
            window.push_notification("请先配置并选择一个 Profile。", cx);
            return;
        };
        self.submit(DesktopCommand::CreateTask {
            profile_id,
            accessions,
            destination,
        });
        window.push_notification("任务已提交，正在预检。", cx);
    }

    fn show_settings(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        self.submit(DesktopCommand::OpenSettings);
        let selected = self.snapshot.selected_profile().cloned();
        if let Some(profile) = &selected
            && matches!(
                profile.status,
                dcmget_ui_kit::ProfileStatus::Starting
                    | dcmget_ui_kit::ProfileStatus::Ready
                    | dcmget_ui_kit::ProfileStatus::Busy
            )
        {
            let backend = Arc::clone(&self.backend);
            let profile_id = profile.id.clone();
            window.open_dialog(cx, move |dialog, _, _| {
                let backend = Arc::clone(&backend);
                let profile_id = profile_id.clone();
                dialog
                    .title("先停止 Profile？")
                    .child("运行中的接收器不能修改 PACS、AE 或端口。停止后可再次打开设置。")
                    .confirm()
                    .button_props(
                        DialogButtonProps::default()
                            .ok_text("停止 Profile")
                            .cancel_text("取消"),
                    )
                    .on_ok(move |_, window, cx| {
                        backend.submit(DesktopCommand::StopProfile(profile_id.clone()));
                        window.push_notification("正在停止 Profile，停止后可修改设置。", cx);
                        true
                    })
            });
            return;
        }
        self.show_profile_editor(selected.as_ref(), window, cx);
    }

    fn show_new_profile(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        self.show_profile_editor(None, window, cx);
    }

    #[allow(clippy::too_many_lines)]
    fn show_profile_editor(
        &mut self,
        profile: Option<&ProfileSummary>,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) {
        let creating = profile.is_none();
        let settings = profile.map_or_else(dcmget_ui_kit::ProfileSettings::default, |profile| {
            profile.settings.clone()
        });
        let anonymization_enabled = Arc::new(AtomicBool::new(settings.anonymization_enabled));
        let initial_display_name =
            profile.map_or_else(|| "新 Profile".to_owned(), |profile| profile.name.clone());
        let display_name = cx.new(|cx| {
            InputState::new(window, cx)
                .placeholder("例如：CT 下载任务")
                .default_value(initial_display_name.clone())
        });
        let pacs_ip = cx.new(|cx| {
            InputState::new(window, cx)
                .placeholder("PACS IP 或主机名")
                .default_value(if creating {
                    String::new()
                } else {
                    settings.pacs_server_ip
                })
        });
        let pacs_port = cx.new(|cx| {
            InputState::new(window, cx)
                .placeholder("PACS 端口")
                .default_value(settings.pacs_server_port.to_string())
        });
        let calling_ae = cx.new(|cx| {
            InputState::new(window, cx)
                .placeholder("本机调用 AE")
                .default_value(settings.calling_ae_title)
        });
        let pacs_ae = cx.new(|cx| {
            InputState::new(window, cx)
                .placeholder("PACS AE")
                .default_value(if creating {
                    String::new()
                } else {
                    settings.pacs_ae_title
                })
        });
        let storage_ae = cx.new(|cx| {
            InputState::new(window, cx)
                .placeholder("接收 AE")
                .default_value(settings.storage_ae_title)
        });
        let storage_port = cx.new(|cx| {
            InputState::new(window, cx)
                .placeholder("SCP 接收端口")
                .default_value(settings.storage_port.to_string())
        });
        let destination = cx.new(|cx| {
            InputState::new(window, cx)
                .placeholder("默认影像保存目录")
                .default_value(settings.default_destination)
        });
        let backend = Arc::clone(&self.backend);
        let profile_id = profile.map(|profile| profile.id.clone());
        window.open_sheet(cx, move |sheet, _, _| {
            let anonymization_setting = Arc::clone(&anonymization_enabled);
            sheet
                .size(px(540.0))
                .title(if creating {
                    "新建 Profile"
                } else {
                    "Profile 设置"
                })
                .child(
                    v_flex()
                        .gap_4()
                        .child(div().text_sm().text_color(gpui::rgb(0x5c_6b_70)).child(
                            "保存前会由后台校验 AE、端口和目录；运行中的 Profile 需先停止。",
                        ))
                        .child(settings_input("Profile 名称", &display_name))
                        .child(settings_input("PACS IP / 主机", &pacs_ip))
                        .child(settings_input("PACS 端口", &pacs_port))
                        .child(settings_input("本机调用 AE", &calling_ae))
                        .child(settings_input("PACS AE", &pacs_ae))
                        .child(settings_input("接收 AE", &storage_ae))
                        .child(settings_input("SCP 接收端口", &storage_port))
                        .child(settings_input("默认目标目录", &destination))
                        .child(
                            v_flex()
                                .gap_2()
                                .child(
                                    Checkbox::new("anonymization-enabled")
                                        .label("启用匿名化")
                                        .checked(anonymization_setting.load(Ordering::Relaxed))
                                        .on_click({
                                            let anonymization_setting =
                                                Arc::clone(&anonymization_setting);
                                            move |checked, _, _| {
                                                anonymization_setting
                                                    .store(*checked, Ordering::Relaxed);
                                            }
                                        }),
                                )
                                .child(
                                    div()
                                        .text_sm()
                                        .text_color(gpui::rgb(0xb4_53_09))
                                        .child(
                                            "原生 4.0 预览暂不支持匿名化。旧版 Profile 若已启用，请取消勾选后保存；程序不会静默跳过匿名化。",
                                        ),
                                ),
                        ),
                )
                .footer(
                    h_flex()
                        .justify_end()
                        .gap_2()
                        .child(
                            ActionButton::new("close-settings", "取消", IconName::CircleX)
                                .kind(ActionButtonKind::Ghost)
                                .on_click(|_, window, cx| window.close_sheet(cx)),
                        )
                        .child(
                            ActionButton::new("save-settings", "保存设置", IconName::Check)
                                .kind(ActionButtonKind::Primary)
                                .on_click({
                                    let pacs_ip = pacs_ip.clone();
                                    let display_name = display_name.clone();
                                    let pacs_port = pacs_port.clone();
                                    let calling_ae = calling_ae.clone();
                                    let pacs_ae = pacs_ae.clone();
                                    let storage_ae = storage_ae.clone();
                                    let storage_port = storage_port.clone();
                                    let destination = destination.clone();
                                    let profile_id = profile_id.clone();
                                    let backend = Arc::clone(&backend);
                                    let anonymization_setting =
                                        Arc::clone(&anonymization_setting);
                                    move |_, window, cx| {
                                        let Ok(pacs_port_value) =
                                            pacs_port.read(cx).value().parse()
                                        else {
                                            window.push_notification(
                                                "PACS 端口必须是 1–65535 的整数。",
                                                cx,
                                            );
                                            return;
                                        };
                                        let Ok(storage_port_value) =
                                            storage_port.read(cx).value().parse()
                                        else {
                                            window.push_notification(
                                                "SCP 接收端口必须是 1–65535 的整数。",
                                                cx,
                                            );
                                            return;
                                        };
                                        let display_name_value =
                                            display_name.read(cx).value().trim().to_owned();
                                        let pacs_ip_value =
                                            pacs_ip.read(cx).value().trim().to_owned();
                                        let calling_ae_value =
                                            calling_ae.read(cx).value().trim().to_owned();
                                        let pacs_ae_value =
                                            pacs_ae.read(cx).value().trim().to_owned();
                                        let storage_ae_value =
                                            storage_ae.read(cx).value().trim().to_owned();
                                        let destination_value =
                                            destination.read(cx).value().trim().to_owned();
                                        if display_name_value.is_empty()
                                            || pacs_ip_value.is_empty()
                                            || calling_ae_value.is_empty()
                                            || pacs_ae_value.is_empty()
                                            || storage_ae_value.is_empty()
                                            || destination_value.is_empty()
                                        {
                                            window.push_notification(
                                                "请填写所有 Profile、PACS、AE 和目标目录字段。",
                                                cx,
                                            );
                                            return;
                                        }
                                        let anonymization_enabled =
                                            anonymization_setting.load(Ordering::Relaxed);
                                        if anonymization_enabled {
                                            window.push_notification(
                                                "原生 4.0 预览暂不支持匿名化。请取消勾选后再保存，或继续使用旧版。",
                                                cx,
                                            );
                                            return;
                                        }
                                        let command = match &profile_id {
                                            None => DesktopCommand::CreateProfile {
                                                display_name: display_name_value,
                                                pacs_server_ip: pacs_ip_value,
                                                pacs_server_port: pacs_port_value,
                                                calling_ae_title: calling_ae_value,
                                                pacs_ae_title: pacs_ae_value,
                                                storage_ae_title: storage_ae_value,
                                                storage_port: storage_port_value,
                                                default_destination: destination_value,
                                                anonymization_enabled,
                                            },
                                            Some(profile_id) => DesktopCommand::SaveProfile {
                                                profile_id: profile_id.clone(),
                                                display_name: display_name_value,
                                                pacs_server_ip: pacs_ip_value,
                                                pacs_server_port: pacs_port_value,
                                                calling_ae_title: calling_ae_value,
                                                pacs_ae_title: pacs_ae_value,
                                                storage_ae_title: storage_ae_value,
                                                storage_port: storage_port_value,
                                                default_destination: destination_value,
                                                anonymization_enabled,
                                            },
                                        };
                                        backend.submit(command);
                                        window.push_notification(
                                            if creating {
                                                "Profile 已提交，后台正在校验并创建。"
                                            } else {
                                                "设置已提交，后台正在校验并保存。"
                                            },
                                            cx,
                                        );
                                        window.close_sheet(cx);
                                    }
                                }),
                        ),
                )
        });
    }

    #[allow(clippy::too_many_lines)]
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
                                h_flex()
                                    .items_center()
                                    .gap_2()
                                    .child(
                                        div()
                                            .text_xs()
                                            .child(self.snapshot.profiles.len().to_string()),
                                    )
                                    .child(
                                        ActionButton::new("new-profile", "新建", IconName::Plus)
                                            .kind(ActionButtonKind::Ghost)
                                            .on_click(cx.listener(|this, _, window, cx| {
                                                this.show_new_profile(window, cx);
                                            })),
                                    ),
                            ),
                    )
                    .when(self.snapshot.profiles.is_empty(), |profiles| {
                        profiles.child(
                            v_flex()
                                .gap_2()
                                .p_3()
                                .rounded_lg()
                                .border_1()
                                .border_color(cx.theme().border)
                                .child(div().font_semibold().child("还没有 Profile"))
                                .child(
                                    div()
                                        .text_xs()
                                        .text_color(cx.theme().muted_foreground)
                                        .child("新建并填写 PACS、AE 和接收端口后即可开始。"),
                                ),
                        )
                    })
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
        let is_selected = self.snapshot.selected_profile_id.as_ref() == Some(&profile.id);
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
            .on_click(cx.listener(move |this, _, window, cx| {
                this.select_profile(profile_id.clone(), window, cx);
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
        let profile_control = self.snapshot.selected_profile().map(|profile| {
            profile_action(
                profile.status,
                profile.id.clone(),
                Arc::clone(&self.backend),
            )
        });
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
                    .when_some(profile_control, ParentElement::child)
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
        let open_destination_profile = self
            .snapshot
            .selected_profile()
            .filter(|profile| !profile.settings.default_destination.trim().is_empty())
            .map(|profile| profile.id.clone());
        let can_open_destination = open_destination_profile.is_some();
        let backend = Arc::clone(&self.backend);
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
                        h_flex()
                            .items_center()
                            .gap_2()
                            .child(
                                div()
                                    .text_sm()
                                    .text_color(cx.theme().muted_foreground)
                                    .child(format!("已识别 {count} 条")),
                            )
                            .child(
                                ActionButton::new(
                                    "open-destination",
                                    "打开目标目录",
                                    IconName::FolderOpen,
                                )
                                .kind(ActionButtonKind::Ghost)
                                .disabled(!can_open_destination)
                                .on_click(move |_, _, _| {
                                    if let Some(profile_id) = &open_destination_profile {
                                        backend.submit(DesktopCommand::OpenDestination(
                                            profile_id.clone(),
                                        ));
                                    }
                                }),
                            ),
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
                                .disabled(
                                    self.snapshot.selected_profile_id.is_none()
                                        || self.snapshot.load_error.is_some(),
                                )
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
                        .child(task_action(task.status, task.id, Arc::clone(&self.backend))),
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
                        h_flex()
                            .items_center()
                            .gap_3()
                            .child(
                                Checkbox::new("detailed-logs")
                                    .label("显示详细日志")
                                    .checked(detailed)
                                    .on_click({
                                        let backend = Arc::clone(&self.backend);
                                        move |checked, _, _| {
                                            backend.submit(DesktopCommand::ToggleDetailedLogs(
                                                *checked,
                                            ));
                                        }
                                    }),
                            )
                            .child(
                                ActionButton::new(
                                    "open-log-directory",
                                    "日志目录",
                                    IconName::FolderOpen,
                                )
                                .kind(ActionButtonKind::Ghost)
                                .on_click({
                                    let backend = Arc::clone(&self.backend);
                                    move |_, _, _| {
                                        backend.submit(DesktopCommand::OpenLogDirectory);
                                    }
                                }),
                            ),
                    ),
            )
            .children(self.snapshot.errors.iter().map(|entry| {
                let tone = match entry.level {
                    LogLevel::Error => cx.theme().danger,
                    LogLevel::Warning => cx.theme().warning,
                    LogLevel::Info => cx.theme().primary,
                    LogLevel::Debug => cx.theme().muted_foreground,
                };
                h_flex()
                    .w_full()
                    .items_start()
                    .gap_3()
                    .p_3()
                    .rounded_lg()
                    .bg(tone.opacity(0.07))
                    .border_l_2()
                    .border_color(tone)
                    .child(Icon::new(IconName::TriangleAlert).size_5().text_color(tone))
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
                            .when_some(self.snapshot.load_error.clone(), |content, message| {
                                content.child(load_error_panel(
                                    message,
                                    Arc::clone(&self.backend),
                                    cx,
                                ))
                            })
                            .child(self.render_new_task(cx))
                            .child(self.render_metrics())
                            .child(self.render_task_summary(cx))
                            .child(self.render_error_log(cx))
                            .child(div().h(px(8.0))),
                    ),
            )
    }
}

fn settings_input(label: &'static str, state: &Entity<InputState>) -> impl IntoElement {
    v_flex()
        .gap_1()
        .child(div().text_sm().font_semibold().child(label))
        .child(Input::new(state))
}

fn load_error_panel(
    message: String,
    backend: Arc<dyn WorkspaceBackend>,
    cx: &mut Context<Workbench>,
) -> impl IntoElement {
    let log_backend = Arc::clone(&backend);
    Panel::new().child(
        h_flex()
            .items_start()
            .gap_3()
            .child(
                Icon::new(IconName::TriangleAlert)
                    .size_5()
                    .text_color(cx.theme().danger),
            )
            .child(
                v_flex()
                    .gap_1()
                    .child(div().font_semibold().child("DcmGet 后台未就绪"))
                    .child(
                        div()
                            .text_sm()
                            .text_color(cx.theme().muted_foreground)
                            .child(message),
                    )
                    .child(
                        div()
                            .text_xs()
                            .text_color(cx.theme().muted_foreground)
                            .child("请检查配置/状态目录权限和诊断日志，修复后重新启动。"),
                    )
                    .child(
                        h_flex()
                            .gap_2()
                            .child(
                                ActionButton::new("reload-workspace", "重新加载", IconName::Redo2)
                                    .kind(ActionButtonKind::Secondary)
                                    .on_click(move |_, _, _| {
                                        backend.submit(DesktopCommand::ReloadWorkspace);
                                    }),
                            )
                            .child(
                                ActionButton::new(
                                    "open-startup-log-directory",
                                    "日志目录",
                                    IconName::FolderOpen,
                                )
                                .kind(ActionButtonKind::Ghost)
                                .on_click(move |_, _, _| {
                                    log_backend.submit(DesktopCommand::OpenLogDirectory);
                                }),
                            ),
                    ),
            ),
    )
}

fn profile_action(
    status: dcmget_ui_kit::ProfileStatus,
    profile_id: ProfileId,
    backend: Arc<dyn WorkspaceBackend>,
) -> impl IntoElement {
    let (label, icon, command) = match status {
        dcmget_ui_kit::ProfileStatus::Stopped | dcmget_ui_kit::ProfileStatus::Error => (
            "启动 Profile",
            IconName::ArrowRight,
            DesktopCommand::StartProfile(profile_id),
        ),
        dcmget_ui_kit::ProfileStatus::Starting => (
            "停止启动",
            IconName::CircleX,
            DesktopCommand::StopProfile(profile_id),
        ),
        dcmget_ui_kit::ProfileStatus::Ready | dcmget_ui_kit::ProfileStatus::Busy => (
            "停止 Profile",
            IconName::CircleX,
            DesktopCommand::StopProfile(profile_id),
        ),
    };
    ActionButton::new("profile-action", label, icon)
        .kind(ActionButtonKind::Secondary)
        .on_click(move |_, _, _| backend.submit(command.clone()))
}

fn task_action(
    status: TaskStatus,
    task_id: String,
    backend: Arc<dyn WorkspaceBackend>,
) -> impl IntoElement {
    let (label, icon, command, disabled) = match status {
        TaskStatus::Running => (
            "暂停",
            IconName::Minus,
            DesktopCommand::PauseTask(task_id),
            false,
        ),
        TaskStatus::Pausing => (
            "正在暂停",
            IconName::Minus,
            DesktopCommand::CancelTask(task_id),
            true,
        ),
        TaskStatus::Paused => (
            "继续",
            IconName::ArrowRight,
            DesktopCommand::ResumeTask(task_id),
            false,
        ),
        TaskStatus::Cancelling => (
            "正在结束",
            IconName::CircleX,
            DesktopCommand::CancelTask(task_id),
            true,
        ),
        TaskStatus::Failed | TaskStatus::Partial => (
            "重试",
            IconName::Redo2,
            DesktopCommand::StartTask(task_id),
            false,
        ),
        TaskStatus::Completed | TaskStatus::Cancelled => (
            "删除任务",
            IconName::Delete,
            DesktopCommand::DeleteTask(task_id),
            false,
        ),
        TaskStatus::Waiting => (
            "结束任务",
            IconName::CircleX,
            DesktopCommand::CancelTask(task_id),
            false,
        ),
    };
    ActionButton::new("task-action", label, icon)
        .kind(ActionButtonKind::Ghost)
        .disabled(disabled)
        .on_click(move |_, _, _| backend.submit(command.clone()))
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
