use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct ProfileId(String);

impl ProfileId {
    pub fn new(value: impl Into<String>) -> Self {
        Self(value.into())
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProfileStatus {
    #[default]
    Stopped,
    Starting,
    Ready,
    Busy,
    Error,
}

impl ProfileStatus {
    pub const fn label(self) -> &'static str {
        match self {
            Self::Stopped => "未启动",
            Self::Starting => "启动中",
            Self::Ready => "接收就绪",
            Self::Busy => "下载中",
            Self::Error => "需要处理",
        }
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReceiverStatus {
    #[default]
    Offline,
    Starting,
    Listening,
    Receiving,
    Error,
}

impl ReceiverStatus {
    pub const fn label(self) -> &'static str {
        match self {
            Self::Offline => "接收器未启动",
            Self::Starting => "正在启动接收器",
            Self::Listening => "接收器已就绪",
            Self::Receiving => "正在接收影像",
            Self::Error => "接收器异常",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProfileSettings {
    pub pacs_server_ip: String,
    pub pacs_server_port: u16,
    pub calling_ae_title: String,
    pub pacs_ae_title: String,
    pub storage_ae_title: String,
    pub storage_port: u16,
    pub default_destination: String,
    #[serde(default)]
    pub anonymization_enabled: bool,
}

impl Default for ProfileSettings {
    fn default() -> Self {
        Self {
            pacs_server_ip: "127.0.0.1".into(),
            pacs_server_port: 104,
            calling_ae_title: "DCMGET".into(),
            pacs_ae_title: "PACS".into(),
            storage_ae_title: "DCMGET".into(),
            storage_port: 6666,
            default_destination: String::new(),
            anonymization_enabled: false,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProfileSummary {
    pub id: ProfileId,
    pub name: String,
    pub ae_title: String,
    pub port: u16,
    pub status: ProfileStatus,
    pub speed_bytes_per_second: u64,
    pub active_tasks: u32,
    pub settings: ProfileSettings,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskStatus {
    #[default]
    Waiting,
    Running,
    Pausing,
    Paused,
    Cancelling,
    Completed,
    Partial,
    Failed,
    Cancelled,
}

impl TaskStatus {
    pub const fn label(self) -> &'static str {
        match self {
            Self::Waiting => "等待中",
            Self::Running => "下载中",
            Self::Pausing => "正在暂停",
            Self::Paused => "已暂停",
            Self::Cancelling => "正在结束",
            Self::Completed => "已完成",
            Self::Partial => "部分成功",
            Self::Failed => "失败",
            Self::Cancelled => "已取消",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct TaskSummary {
    pub id: String,
    pub title: String,
    pub status: TaskStatus,
    pub completed: u32,
    pub total: u32,
    pub files: u64,
    pub speed_bytes_per_second: u64,
    pub current_accession: Option<String>,
    pub error_summary: Option<String>,
}

impl TaskSummary {
    pub fn progress_percent(&self) -> f32 {
        if self.total == 0 {
            return 0.0;
        }
        let hundredths = (u64::from(self.completed) * 10_000 / u64::from(self.total)).min(10_000);
        let hundredths = u16::try_from(hundredths).unwrap_or(10_000);
        f32::from(hundredths) / 100.0
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LogLevel {
    Error,
    Warning,
    Info,
    Debug,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct LogEntry {
    pub timestamp: String,
    pub level: LogLevel,
    pub source: String,
    pub message: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct WorkspaceSnapshot {
    pub profiles: Vec<ProfileSummary>,
    pub selected_profile_id: Option<ProfileId>,
    pub receiver_status: ReceiverStatus,
    pub aggregate_speed_bytes_per_second: u64,
    pub tasks: Vec<TaskSummary>,
    pub errors: Vec<LogEntry>,
    pub detailed_logs_enabled: bool,
    /// Set only when the application layer could not load or refresh real state.
    pub load_error: Option<String>,
}

impl WorkspaceSnapshot {
    pub fn selected_profile(&self) -> Option<&ProfileSummary> {
        let selected_profile_id = self.selected_profile_id.as_ref()?;
        self.profiles
            .iter()
            .find(|profile| &profile.id == selected_profile_id)
    }

    /// Initial state shown while the application service loads persistent data.
    pub fn loading() -> Self {
        Self {
            profiles: Vec::new(),
            selected_profile_id: None,
            receiver_status: ReceiverStatus::Offline,
            aggregate_speed_bytes_per_second: 0,
            tasks: Vec::new(),
            errors: Vec::new(),
            detailed_logs_enabled: false,
            load_error: None,
        }
    }

    pub fn load_failed(message: impl Into<String>) -> Self {
        Self {
            load_error: Some(message.into()),
            ..Self::loading()
        }
    }

    #[cfg(any(test, feature = "mock-ui"))]
    pub fn technical_gate_sample() -> Self {
        let profile_id = ProfileId::new("profile-ct");
        Self {
            profiles: vec![
                ProfileSummary {
                    id: profile_id.clone(),
                    name: "影像中心 CT".into(),
                    ae_title: "DCMGET".into(),
                    port: 6666,
                    status: ProfileStatus::Busy,
                    speed_bytes_per_second: 27_400_000,
                    active_tasks: 1,
                    settings: ProfileSettings {
                        pacs_server_ip: "192.0.2.10".into(),
                        pacs_server_port: 104,
                        calling_ae_title: "DCMGET".into(),
                        pacs_ae_title: "PACS".into(),
                        storage_ae_title: "DCMGET".into(),
                        storage_port: 6666,
                        default_destination: r"D:\DICOM".into(),
                        anonymization_enabled: false,
                    },
                },
                ProfileSummary {
                    id: ProfileId::new("profile-mr"),
                    name: "磁共振归档".into(),
                    ae_title: "DCMGET_MR".into(),
                    port: 6667,
                    status: ProfileStatus::Ready,
                    speed_bytes_per_second: 0,
                    active_tasks: 0,
                    settings: ProfileSettings {
                        storage_ae_title: "DCMGET_MR".into(),
                        storage_port: 6667,
                        ..ProfileSettings::default()
                    },
                },
                ProfileSummary {
                    id: ProfileId::new("profile-test"),
                    name: "测试 PACS".into(),
                    ae_title: "DCMGET_TEST".into(),
                    port: 6668,
                    status: ProfileStatus::Stopped,
                    speed_bytes_per_second: 0,
                    active_tasks: 0,
                    settings: ProfileSettings {
                        storage_ae_title: "DCMGET_TEST".into(),
                        storage_port: 6668,
                        ..ProfileSettings::default()
                    },
                },
            ],
            selected_profile_id: Some(profile_id),
            receiver_status: ReceiverStatus::Receiving,
            aggregate_speed_bytes_per_second: 27_400_000,
            tasks: vec![TaskSummary {
                id: "task-20260810-01".into(),
                title: "今日 CT 批量下载".into(),
                status: TaskStatus::Running,
                completed: 86,
                total: 240,
                files: 1_842,
                speed_bytes_per_second: 27_400_000,
                current_accession: Some("202601261643".into()),
                error_summary: None,
            }],
            errors: vec![LogEntry {
                timestamp: "10:42:18".into(),
                level: LogLevel::Error,
                source: "接收器".into(),
                message: "1 个对象写入失败，已保留到隔离目录，可在任务结束后重试。".into(),
            }],
            detailed_logs_enabled: false,
            load_error: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn loading_and_failure_states_never_contain_demo_activity() {
        let loading = WorkspaceSnapshot::loading();
        assert!(loading.profiles.is_empty());
        assert!(loading.tasks.is_empty());
        assert_eq!(loading.aggregate_speed_bytes_per_second, 0);
        assert!(loading.load_error.is_none());

        let failure = WorkspaceSnapshot::load_failed("state database unavailable");
        assert!(failure.profiles.is_empty());
        assert!(failure.tasks.is_empty());
        assert_eq!(
            failure.load_error.as_deref(),
            Some("state database unavailable")
        );
    }

    #[test]
    fn progress_is_safe_for_empty_and_overcomplete_tasks() {
        let mut task = WorkspaceSnapshot::technical_gate_sample().tasks.remove(0);
        task.total = 0;
        assert!(task.progress_percent().abs() < f32::EPSILON);

        task.total = 10;
        task.completed = 11;
        assert!((task.progress_percent() - 100.0).abs() < f32::EPSILON);
    }

    #[test]
    fn selected_profile_is_resolved_without_side_effects() {
        let snapshot = WorkspaceSnapshot::technical_gate_sample();
        assert_eq!(snapshot.selected_profile().map(|p| p.port), Some(6666));
    }
}
