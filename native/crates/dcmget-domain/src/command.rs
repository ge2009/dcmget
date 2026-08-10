use serde::{Deserialize, Serialize};

use crate::{PdiStatus, ProfileId, TaskId};

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "command", rename_all = "snake_case")]
pub enum AppCommand {
    RegisterProfile {
        profile_id: ProfileId,
    },
    StartProfile {
        profile_id: ProfileId,
    },
    StopProfile {
        profile_id: ProfileId,
    },
    CreateTask {
        task_id: TaskId,
        profile_id: ProfileId,
        accessions: Vec<String>,
    },
    PauseTask {
        task_id: TaskId,
    },
    ResumeTask {
        task_id: TaskId,
    },
    FinishTask {
        task_id: TaskId,
    },
    CancelTask {
        task_id: TaskId,
    },
    ReportAggregateSpeed {
        bytes_per_second: u64,
    },
    ExitApplication,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "event", rename_all = "snake_case")]
pub enum AppEvent {
    ProfileRegistered { profile_id: ProfileId },
    ProfileStarted { profile_id: ProfileId },
    ProfileStopped { profile_id: ProfileId },
    TaskCreated { task_id: TaskId },
    TaskPaused { task_id: TaskId },
    TaskResumed { task_id: TaskId },
    TaskFinished { task_id: TaskId },
    TaskCancelled { task_id: TaskId },
    CommandRejected { message: String },
    ApplicationStopping,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReceiverState {
    Stopped,
    Starting,
    Listening,
    Stopping,
    Faulted,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ReceiverStatus {
    pub profile_id: ProfileId,
    pub state: ReceiverState,
    pub ae_title: String,
    pub port: u16,
    pub active_associations: u16,
    pub message: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct PdiProgress {
    pub task_id: TaskId,
    pub status: PdiStatus,
    pub stage: String,
    pub completed: u64,
    pub total: u64,
    pub message: String,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
pub struct UpdateStatus {
    pub state: String,
    pub current_version: String,
    pub available_version: String,
    pub message: String,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
pub struct LicenseStatus {
    pub state: String,
    pub remaining_trial_tasks: Option<u32>,
    pub message: String,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
pub struct DiagnosticStatus {
    pub state: String,
    pub output_path: String,
    pub message: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LogLevel {
    Debug,
    Info,
    Warning,
    Error,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct LogEntry {
    pub timestamp: String,
    pub level: LogLevel,
    pub source: String,
    pub task_id: Option<TaskId>,
    pub message: String,
}
