//! Presentation contracts and the only GPUI component boundary used by `DcmGet` pages.
//!
//! The types in this crate deliberately contain no filesystem, database, or network access.
//! A desktop page receives an immutable [`WorkspaceSnapshot`] and emits [`DesktopCommand`] values
//! through a [`CommandSink`]. The application layer owns all side effects.

mod command;
mod model;

#[cfg(feature = "gpui")]
mod gpui_adapter;

#[cfg(any(test, feature = "mock-ui"))]
pub use command::RecordingCommandSink;
pub use command::{CommandSink, DesktopCommand, SnapshotSource, WorkspaceBackend};
pub use model::{
    LogEntry, LogLevel, ProfileId, ProfileSettings, ProfileStatus, ProfileSummary, ReceiverStatus,
    TaskStatus, TaskSummary, WorkspaceSnapshot,
};

#[cfg(feature = "gpui")]
pub use gpui_adapter::{
    ActionButton, ActionButtonKind, MetricCard, Panel, SectionHeading, StatusPill,
    initialize_light_theme,
};
