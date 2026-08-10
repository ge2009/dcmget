//! Stable, UI-independent contracts for the native `DcmGet` application.
//!
//! GPUI, the CLI, persistence, and the DICOM engine communicate exclusively
//! through these types. Framework types must not leak into business state.

mod command;
mod config;
mod pdi;
mod profile;
mod task;

pub use command::{
    AppCommand, AppEvent, DiagnosticStatus, LicenseStatus, LogEntry, LogLevel, PdiProgress,
    ReceiverState, ReceiverStatus, UpdateStatus,
};
pub use config::{
    ANONYMIZATION_PROFILES, AccessionParseResult, AppConfig, CURRENT_CONFIG_VERSION,
    DEFAULT_DIRECTORY_TEMPLATE, ValidationIssue, parse_accessions, validate_ae_title,
};
pub use pdi::{PdiResult, PdiStatus};
pub use profile::{Profile, ProfileId, ProfileRuntimeStatus};
pub use task::{
    AccessionResult, AccessionStatus, DomainError, ResultVerificationStatus, Task, TaskId,
    TaskPhase, TaskSummary,
};
