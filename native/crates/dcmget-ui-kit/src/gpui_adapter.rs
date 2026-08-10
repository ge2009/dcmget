use std::rc::Rc;

use gpui::{
    AnyElement, App, ClickEvent, ElementId, IntoElement, ParentElement, RenderOnce, SharedString,
    Styled, Window, div, px, rgb,
};
use gpui_component::{
    ActiveTheme, Disableable, Icon, IconName, StyledExt, Theme, ThemeMode,
    button::{Button, ButtonVariant, ButtonVariants},
    h_flex, v_flex,
};

use crate::{ProfileStatus, ReceiverStatus, TaskStatus};

/// Initializes gpui-component and forces `DcmGet`'s deliberately light clinical-workstation theme.
pub fn initialize_light_theme(cx: &mut App) {
    gpui_component::init(cx);
    Theme::change(ThemeMode::Light, None, cx);

    let theme = Theme::global_mut(cx);
    theme.font_family = if cfg!(target_os = "windows") {
        "Microsoft YaHei UI".into()
    } else {
        ".SystemUIFont".into()
    };
    theme.font_size = px(17.0);
    theme.radius = px(9.0);
    theme.radius_lg = px(14.0);
    theme.primary = rgb(0x000b_6f7c).into();
    theme.primary_foreground = rgb(0x00ff_ffff).into();
    theme.background = rgb(0x00f3_f6f7).into();
    theme.popover = rgb(0x00ff_ffff).into();
    theme.border = rgb(0x00d9_e1e4).into();
    theme.muted = rgb(0x00eb_f0f2).into();
    theme.muted_foreground = rgb(0x005c_6b70).into();
    theme.danger = rgb(0x00c7_383b).into();
    theme.warning = rgb(0x00c1_7112).into();
    theme.success = rgb(0x001f_875b).into();
}

type ClickHandler = Rc<dyn Fn(&ClickEvent, &mut Window, &mut App)>;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ActionButtonKind {
    Primary,
    Secondary,
    Danger,
    Ghost,
}

#[derive(IntoElement)]
#[must_use]
pub struct ActionButton {
    id: ElementId,
    label: SharedString,
    icon: IconName,
    kind: ActionButtonKind,
    disabled: bool,
    on_click: Option<ClickHandler>,
}

impl ActionButton {
    pub fn new(id: impl Into<ElementId>, label: impl Into<SharedString>, icon: IconName) -> Self {
        Self {
            id: id.into(),
            label: label.into(),
            icon,
            kind: ActionButtonKind::Secondary,
            disabled: false,
            on_click: None,
        }
    }

    pub fn kind(mut self, kind: ActionButtonKind) -> Self {
        self.kind = kind;
        self
    }

    pub fn disabled(mut self, disabled: bool) -> Self {
        self.disabled = disabled;
        self
    }

    pub fn on_click(
        mut self,
        handler: impl Fn(&ClickEvent, &mut Window, &mut App) + 'static,
    ) -> Self {
        self.on_click = Some(Rc::new(handler));
        self
    }
}

impl RenderOnce for ActionButton {
    fn render(self, _: &mut Window, _: &mut App) -> impl IntoElement {
        let mut button = Button::new(self.id)
            .label(self.label)
            .icon(self.icon)
            .disabled(self.disabled);

        button = match self.kind {
            ActionButtonKind::Primary => button.primary(),
            ActionButtonKind::Secondary => button.outline(),
            ActionButtonKind::Danger => button.with_variant(ButtonVariant::Danger),
            ActionButtonKind::Ghost => button.with_variant(ButtonVariant::Ghost),
        };

        if let Some(handler) = self.on_click {
            button = button.on_click(move |event, window, cx| handler(event, window, cx));
        }
        button
    }
}

#[derive(IntoElement)]
#[must_use]
pub struct Panel {
    children: Vec<AnyElement>,
    padding: f32,
}

impl Panel {
    pub fn new() -> Self {
        Self {
            children: Vec::new(),
            padding: 20.0,
        }
    }

    pub fn compact(mut self) -> Self {
        self.padding = 14.0;
        self
    }

    pub fn child(mut self, child: impl IntoElement) -> Self {
        self.children.push(child.into_any_element());
        self
    }

    pub fn children(mut self, children: impl IntoIterator<Item = impl IntoElement>) -> Self {
        self.children
            .extend(children.into_iter().map(IntoElement::into_any_element));
        self
    }
}

impl Default for Panel {
    fn default() -> Self {
        Self::new()
    }
}

impl RenderOnce for Panel {
    fn render(self, _: &mut Window, cx: &mut App) -> impl IntoElement {
        v_flex()
            .w_full()
            .gap_3()
            .p(px(self.padding))
            .rounded(cx.theme().radius_lg)
            .border_1()
            .border_color(cx.theme().border)
            .bg(cx.theme().popover)
            .children(self.children)
    }
}

#[derive(IntoElement)]
pub struct SectionHeading {
    eyebrow: SharedString,
    title: SharedString,
    description: SharedString,
}

impl SectionHeading {
    pub fn new(
        eyebrow: impl Into<SharedString>,
        title: impl Into<SharedString>,
        description: impl Into<SharedString>,
    ) -> Self {
        Self {
            eyebrow: eyebrow.into(),
            title: title.into(),
            description: description.into(),
        }
    }
}

impl RenderOnce for SectionHeading {
    fn render(self, _: &mut Window, cx: &mut App) -> impl IntoElement {
        v_flex()
            .gap_1()
            .child(
                div()
                    .text_xs()
                    .font_semibold()
                    .text_color(cx.theme().primary)
                    .child(self.eyebrow),
            )
            .child(div().text_2xl().font_semibold().child(self.title))
            .child(
                div()
                    .text_sm()
                    .text_color(cx.theme().muted_foreground)
                    .child(self.description),
            )
    }
}

#[derive(IntoElement)]
pub struct MetricCard {
    label: SharedString,
    value: SharedString,
    detail: SharedString,
    icon: IconName,
}

impl MetricCard {
    pub fn new(
        label: impl Into<SharedString>,
        value: impl Into<SharedString>,
        detail: impl Into<SharedString>,
        icon: IconName,
    ) -> Self {
        Self {
            label: label.into(),
            value: value.into(),
            detail: detail.into(),
            icon,
        }
    }
}

impl RenderOnce for MetricCard {
    fn render(self, _: &mut Window, cx: &mut App) -> impl IntoElement {
        v_flex()
            .flex_1()
            .min_w(px(170.0))
            .gap_2()
            .p_4()
            .rounded(cx.theme().radius_lg)
            .border_1()
            .border_color(cx.theme().border)
            .bg(cx.theme().popover)
            .child(
                h_flex()
                    .items_center()
                    .justify_between()
                    .child(
                        div()
                            .text_sm()
                            .text_color(cx.theme().muted_foreground)
                            .child(self.label),
                    )
                    .child(Icon::new(self.icon).size_5().text_color(cx.theme().primary)),
            )
            .child(div().text_2xl().font_semibold().child(self.value))
            .child(
                div()
                    .text_xs()
                    .text_color(cx.theme().muted_foreground)
                    .child(self.detail),
            )
    }
}

#[derive(IntoElement)]
pub struct StatusPill {
    label: SharedString,
    tone: StatusTone,
}

#[derive(Clone, Copy)]
enum StatusTone {
    Neutral,
    Primary,
    Success,
    Warning,
    Danger,
}

impl StatusPill {
    pub fn receiver(status: ReceiverStatus) -> Self {
        let tone = match status {
            ReceiverStatus::Offline => StatusTone::Neutral,
            ReceiverStatus::Starting => StatusTone::Warning,
            ReceiverStatus::Listening | ReceiverStatus::Receiving => StatusTone::Success,
            ReceiverStatus::Error => StatusTone::Danger,
        };
        Self {
            label: status.label().into(),
            tone,
        }
    }

    pub fn profile(status: ProfileStatus) -> Self {
        let tone = match status {
            ProfileStatus::Stopped => StatusTone::Neutral,
            ProfileStatus::Starting => StatusTone::Warning,
            ProfileStatus::Ready => StatusTone::Success,
            ProfileStatus::Busy => StatusTone::Primary,
            ProfileStatus::Error => StatusTone::Danger,
        };
        Self {
            label: status.label().into(),
            tone,
        }
    }

    pub fn task(status: TaskStatus) -> Self {
        let tone = match status {
            TaskStatus::Waiting | TaskStatus::Cancelled => StatusTone::Neutral,
            TaskStatus::Running => StatusTone::Primary,
            TaskStatus::Pausing
            | TaskStatus::Paused
            | TaskStatus::Cancelling
            | TaskStatus::Partial => StatusTone::Warning,
            TaskStatus::Completed => StatusTone::Success,
            TaskStatus::Failed => StatusTone::Danger,
        };
        Self {
            label: status.label().into(),
            tone,
        }
    }
}

impl RenderOnce for StatusPill {
    fn render(self, _: &mut Window, cx: &mut App) -> impl IntoElement {
        let color = match self.tone {
            StatusTone::Neutral => cx.theme().muted_foreground,
            StatusTone::Primary => cx.theme().primary,
            StatusTone::Success => cx.theme().success,
            StatusTone::Warning => cx.theme().warning,
            StatusTone::Danger => cx.theme().danger,
        };
        h_flex()
            .gap_2()
            .items_center()
            .px_3()
            .py_1()
            .rounded_full()
            .bg(color.opacity(0.10))
            .text_color(color)
            .text_xs()
            .font_semibold()
            .child(div().size(px(7.0)).rounded_full().bg(color))
            .child(self.label)
    }
}
