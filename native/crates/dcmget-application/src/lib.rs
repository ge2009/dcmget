//! UI-independent `DcmGet` application orchestration.

pub mod archive;
mod process_guard;
mod transfer;

pub use process_guard::{ProcessGuard, ProcessGuardError};

use std::collections::{HashMap, HashSet, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use chrono::{SecondsFormat, Utc};
use dcmget_domain::{
    AccessionResult, AppCommand, AppConfig, AppEvent, LogEntry, LogLevel, Profile, ProfileId,
    ProfileRuntimeStatus, ReceiverState, ReceiverStatus, Task, TaskId, TaskPhase, TaskSummary,
    parse_accessions,
};
use dcmget_state::{LegacyLayout, MigrationReport, MigrationService, StateError, StateRepository};
use thiserror::Error;
use tokio::sync::{broadcast, mpsc, watch};
use tokio::task::JoinHandle;

use crate::transfer::{
    InternalEvent, NativeProfileRuntimeFactory, ProfileRuntimeFactory, ProfileWork, TaskControl,
};

const COMMAND_CAPACITY: usize = 256;
const INTERNAL_EVENT_CAPACITY: usize = 1_024;
const EVENT_CAPACITY: usize = 1_024;
const PROFILE_COMMAND_CAPACITY: usize = 4;
const MAX_PROGRESS_HZ: u64 = 4;
const MAX_UI_LOGS: usize = 500;
const WORKER_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Debug, Error)]
pub enum ApplicationError {
    #[error("application command channel is closed")]
    Closed,
    #[error("profile {0} does not exist")]
    UnknownProfile(ProfileId),
    #[error("task {0} does not exist")]
    UnknownTask(TaskId),
    #[error("profile {0} already has an active task")]
    ProfileBusy(ProfileId),
    #[error("profile {profile_id} conflicts with active profile {other_profile_id} on port {port}")]
    PortConflict {
        profile_id: ProfileId,
        other_profile_id: ProfileId,
        port: u16,
    },
    #[error("invalid profile configuration: {0}")]
    InvalidProfile(String),
    #[error("invalid task: {0}")]
    InvalidTask(String),
    #[error(transparent)]
    ProcessGuard(#[from] ProcessGuardError),
    #[error(transparent)]
    State(#[from] StateError),
}

#[derive(Clone, Debug)]
pub struct BootstrapPaths {
    pub config_root: PathBuf,
    pub state_root: PathBuf,
    pub native_database: PathBuf,
    pub backup_root: PathBuf,
}

pub struct BootstrapResult {
    pub service: ApplicationService,
    pub handle: ApplicationHandle,
    pub migration: MigrationReport,
}

#[derive(Debug, Clone, Default)]
pub struct ApplicationSnapshot {
    pub profiles: Vec<Profile>,
    pub tasks: Vec<TaskSummary>,
    pub receiver_statuses: Vec<ReceiverStatus>,
    pub logs: Vec<LogEntry>,
    pub active_profiles: usize,
    pub active_tasks: usize,
    pub aggregate_speed_bytes_per_second: u64,
    pub startup_error: String,
    pub runtime_error: String,
    pub shutting_down: bool,
}

struct RuntimeProfile {
    profile: Profile,
    commands: Option<mpsc::Sender<ProfileWork>>,
    worker: Option<JoinHandle<()>>,
    active_task: Option<TaskId>,
    receiver_status: ReceiverStatus,
}

struct RuntimeTask {
    task: Task,
    summary: TaskSummary,
    control: Option<TaskControl>,
    pending_progress: Option<AccessionResult>,
}

#[derive(Default)]
struct RuntimeState {
    profiles: HashMap<ProfileId, RuntimeProfile>,
    tasks: HashMap<TaskId, RuntimeTask>,
    logs: VecDeque<LogEntry>,
    progress_dirty: HashSet<TaskId>,
    runtime_error: String,
    manual_speed_bytes_per_second: u64,
}

#[derive(Clone)]
pub struct ApplicationHandle {
    commands: mpsc::Sender<AppCommand>,
    events: broadcast::Sender<AppEvent>,
    snapshot: watch::Receiver<ApplicationSnapshot>,
    shutdown_requested: Arc<AtomicBool>,
}

impl ApplicationHandle {
    pub async fn send(&self, command: AppCommand) -> Result<(), ApplicationError> {
        self.commands
            .send(command)
            .await
            .map_err(|_| ApplicationError::Closed)
    }

    #[must_use]
    pub fn subscribe(&self) -> broadcast::Receiver<AppEvent> {
        self.events.subscribe()
    }

    #[must_use]
    pub fn snapshot(&self) -> ApplicationSnapshot {
        self.snapshot.borrow().clone()
    }

    #[must_use]
    pub fn shutdown_requested(&self) -> bool {
        self.shutdown_requested.load(Ordering::Acquire)
    }
}

pub struct ApplicationService {
    state: RuntimeState,
    repository: Option<StateRepository>,
    runtime_factory: Arc<dyn ProfileRuntimeFactory>,
    commands: mpsc::Receiver<AppCommand>,
    internal_events: mpsc::Receiver<InternalEvent>,
    internal_sender: mpsc::Sender<InternalEvent>,
    events: broadcast::Sender<AppEvent>,
    snapshot: watch::Sender<ApplicationSnapshot>,
    shutdown_requested: Arc<AtomicBool>,
    process_guard: Option<ProcessGuard>,
}

impl ApplicationService {
    /// Open an already-native state database. Production desktop startup should
    /// normally use [`Self::bootstrap`] so legacy sources are backed up and
    /// imported before this database is read.
    pub fn open(
        state_path: impl AsRef<Path>,
    ) -> Result<(Self, ApplicationHandle), ApplicationError> {
        let repository = StateRepository::open(state_path)?;
        Self::from_repository(repository, None, Arc::new(NativeProfileRuntimeFactory))
    }

    /// Acquire the legacy-compatible process lock, back up and read legacy
    /// Profile/task sources, then make the native database the only writer.
    pub fn bootstrap(paths: BootstrapPaths) -> Result<BootstrapResult, ApplicationError> {
        let BootstrapPaths {
            config_root,
            state_root,
            native_database,
            backup_root,
        } = paths;
        let guard = ProcessGuard::acquire(&state_root)?;
        let repository = StateRepository::open(native_database)?;
        let layout = LegacyLayout::discover(config_root, state_root)?;
        let migration =
            MigrationService::new(backup_root).migrate_if_needed(&layout, &repository)?;
        let (service, handle) = Self::from_repository(
            repository,
            Some(guard),
            Arc::new(NativeProfileRuntimeFactory),
        )?;
        Ok(BootstrapResult {
            service,
            handle,
            migration,
        })
    }

    /// Volatile channel retained for small contract tests. It contains no
    /// Profiles and intentionally does not represent production startup.
    #[must_use]
    pub fn channel() -> (Self, ApplicationHandle) {
        Self::build(
            RuntimeState::default(),
            None,
            None,
            Arc::new(NativeProfileRuntimeFactory),
        )
    }

    fn from_repository(
        repository: StateRepository,
        process_guard: Option<ProcessGuard>,
        runtime_factory: Arc<dyn ProfileRuntimeFactory>,
    ) -> Result<(Self, ApplicationHandle), ApplicationError> {
        recover_interrupted_tasks(&repository)?;
        let state = load_runtime_state(&repository)?;
        Ok(Self::build(
            state,
            Some(repository),
            process_guard,
            runtime_factory,
        ))
    }

    fn build(
        state: RuntimeState,
        repository: Option<StateRepository>,
        process_guard: Option<ProcessGuard>,
        runtime_factory: Arc<dyn ProfileRuntimeFactory>,
    ) -> (Self, ApplicationHandle) {
        let (command_tx, command_rx) = mpsc::channel(COMMAND_CAPACITY);
        let (internal_tx, internal_rx) = mpsc::channel(INTERNAL_EVENT_CAPACITY);
        let (event_tx, _) = broadcast::channel(EVENT_CAPACITY);
        let initial_snapshot = snapshot_from_state(&state, false);
        let (snapshot_tx, snapshot_rx) = watch::channel(initial_snapshot);
        let shutdown_requested = Arc::new(AtomicBool::new(false));
        let service = Self {
            state,
            repository,
            runtime_factory,
            commands: command_rx,
            internal_events: internal_rx,
            internal_sender: internal_tx,
            events: event_tx.clone(),
            snapshot: snapshot_tx,
            shutdown_requested: Arc::clone(&shutdown_requested),
            process_guard,
        };
        let handle = ApplicationHandle {
            commands: command_tx,
            events: event_tx,
            snapshot: snapshot_rx,
            shutdown_requested,
        };
        (service, handle)
    }

    pub async fn run(mut self) {
        self.publish_workspace();
        let mut progress_tick =
            tokio::time::interval(Duration::from_millis(1_000 / MAX_PROGRESS_HZ));
        progress_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

        loop {
            tokio::select! {
                _ = progress_tick.tick() => {
                    self.flush_progress();
                    self.reap_finished_workers().await;
                    self.publish_snapshot();
                }
                event = self.internal_events.recv() => {
                    if let Some(event) = event {
                        self.apply_internal(event);
                    }
                }
                command = self.commands.recv() => {
                    let Some(command) = command else {
                        self.shutdown().await;
                        break;
                    };
                    if matches!(command, AppCommand::ExitApplication) {
                        self.shutdown().await;
                        break;
                    }
                    if let Err(error) = self.apply_command(command).await {
                        self.reject(&error);
                    }
                }
            }
        }
        drop(self.process_guard.take());
    }

    async fn apply_command(&mut self, command: AppCommand) -> Result<(), ApplicationError> {
        match command {
            AppCommand::ReloadWorkspace => self.reload_workspace()?,
            AppCommand::RegisterProfile { profile_id } => {
                let profile = Profile {
                    id: profile_id,
                    display_name: "新实例".to_owned(),
                    config: AppConfig::default(),
                    runtime_status: ProfileRuntimeStatus::Stopped,
                    source_config_path: String::new(),
                    created_at: now(),
                    updated_at: now(),
                };
                self.upsert_profile(profile)?;
            }
            AppCommand::UpsertProfile { profile } => self.upsert_profile(*profile)?,
            AppCommand::StartProfile { profile_id } => self.start_profile(&profile_id)?,
            AppCommand::StopProfile { profile_id } => self.stop_profile(&profile_id).await?,
            AppCommand::CreateTask {
                task_id,
                profile_id,
                name,
                accessions,
                destination,
            } => {
                self.preflight_profile_start(&profile_id)?;
                self.create_task(&task_id, &profile_id, &name, &accessions, &destination)?;
                self.start_profile(&profile_id)?;
                self.dispatch_task(&task_id).await?;
            }
            AppCommand::StartTask { task_id } => self.start_task(&task_id).await?,
            AppCommand::PauseTask { task_id } => self.pause_task(&task_id)?,
            AppCommand::ResumeTask { task_id } => self.resume_task(&task_id).await?,
            AppCommand::FinishTask { .. } => {
                return Err(ApplicationError::InvalidTask(
                    "不能手工将任务标记为完成；完成状态只能由下载引擎确认".to_owned(),
                ));
            }
            AppCommand::CancelTask { task_id } => self.cancel_task(&task_id)?,
            AppCommand::DeleteTask { task_id } => self.delete_task(&task_id)?,
            AppCommand::ReportAggregateSpeed { bytes_per_second } => {
                self.state.manual_speed_bytes_per_second = bytes_per_second;
            }
            AppCommand::ExitApplication => unreachable!("handled by run"),
        }
        self.publish_snapshot();
        Ok(())
    }

    fn upsert_profile(&mut self, mut profile: Profile) -> Result<(), ApplicationError> {
        if self
            .state
            .profiles
            .get(&profile.id)
            .is_some_and(|runtime| runtime.worker.is_some() || runtime.active_task.is_some())
        {
            return Err(ApplicationError::ProfileBusy(profile.id));
        }
        if let Some(task) = self.state.tasks.values().find(|task| {
            task.task.profile_id == profile.id
                && !matches!(task.task.phase, TaskPhase::Completed | TaskPhase::Cancelled)
        }) {
            return Err(ApplicationError::InvalidProfile(format!(
                "Profile {} 仍有未完成或可重试任务 {}，请先完成、取消或删除任务后再修改配置",
                profile.id, task.task.id
            )));
        }
        validate_download_config(&profile.config)?;
        profile.runtime_status = ProfileRuntimeStatus::Stopped;
        profile.updated_at = now();
        if profile.created_at.is_empty() {
            profile.created_at.clone_from(&profile.updated_at);
        }
        if let Some(repository) = &self.repository {
            repository.upsert_profile(&profile)?;
        }
        let receiver_status = stopped_receiver_status(&profile);
        self.state.profiles.insert(
            profile.id.clone(),
            RuntimeProfile {
                profile: profile.clone(),
                commands: None,
                worker: None,
                active_task: None,
                receiver_status,
            },
        );
        let _ = self.events.send(AppEvent::ProfileRegistered {
            profile_id: profile.id.clone(),
        });
        let _ = self.events.send(AppEvent::ProfileUpdated { profile });
        Ok(())
    }

    fn start_profile(&mut self, profile_id: &ProfileId) -> Result<(), ApplicationError> {
        self.preflight_profile_start(profile_id)?;
        let profile = self
            .state
            .profiles
            .get(profile_id)
            .ok_or_else(|| ApplicationError::UnknownProfile(profile_id.clone()))?
            .profile
            .clone();
        if self
            .state
            .profiles
            .get(profile_id)
            .is_some_and(|runtime| runtime.worker.is_some())
        {
            return Ok(());
        }
        let (commands, receiver) = mpsc::channel(PROFILE_COMMAND_CAPACITY);
        let worker =
            self.runtime_factory
                .spawn(profile.clone(), receiver, self.internal_sender.clone());
        let runtime = self
            .state
            .profiles
            .get_mut(profile_id)
            .expect("profile checked above");
        runtime.profile.runtime_status = ProfileRuntimeStatus::Starting;
        runtime.receiver_status.state = ReceiverState::Starting;
        "正在启动接收器".clone_into(&mut runtime.receiver_status.message);
        runtime.commands = Some(commands);
        runtime.worker = Some(worker);
        let _ = self.events.send(AppEvent::ReceiverStatusChanged {
            status: runtime.receiver_status.clone(),
        });
        Ok(())
    }

    fn preflight_profile_start(&self, profile_id: &ProfileId) -> Result<(), ApplicationError> {
        let runtime = self
            .state
            .profiles
            .get(profile_id)
            .ok_or_else(|| ApplicationError::UnknownProfile(profile_id.clone()))?;
        validate_download_config(&runtime.profile.config)?;
        if let Some(worker) = &runtime.worker {
            if worker.is_finished()
                || runtime
                    .commands
                    .as_ref()
                    .is_none_or(mpsc::Sender::is_closed)
            {
                return Err(ApplicationError::InvalidProfile(
                    "接收器后台已经退出，请等待状态刷新后重试".to_owned(),
                ));
            }
            return Ok(());
        }
        for (other_id, other) in &self.state.profiles {
            if other_id != profile_id
                && other.worker.is_some()
                && other.profile.config.storage_port == runtime.profile.config.storage_port
            {
                return Err(ApplicationError::PortConflict {
                    profile_id: profile_id.clone(),
                    other_profile_id: other_id.clone(),
                    port: runtime.profile.config.storage_port,
                });
            }
        }
        Ok(())
    }

    async fn stop_profile(&mut self, profile_id: &ProfileId) -> Result<(), ApplicationError> {
        let runtime = self
            .state
            .profiles
            .get_mut(profile_id)
            .ok_or_else(|| ApplicationError::UnknownProfile(profile_id.clone()))?;
        runtime.profile.runtime_status = ProfileRuntimeStatus::Stopping;
        runtime.receiver_status.state = ReceiverState::Stopping;
        "正在停止接收器".clone_into(&mut runtime.receiver_status.message);
        if let Some(task_id) = runtime.active_task.clone()
            && let Some(task) = self.state.tasks.get_mut(&task_id)
        {
            if let Some(control) = &task.control {
                control.stop_profile();
            }
            persist_phase(
                self.repository.as_ref(),
                task,
                TaskPhase::DownloadRetryable,
                "接收器已停止",
            )?;
        }
        if let Some(sender) = &runtime.commands {
            let _ = sender.send(ProfileWork::Stop).await;
        }
        let _ = self.events.send(AppEvent::ReceiverStatusChanged {
            status: runtime.receiver_status.clone(),
        });
        Ok(())
    }

    fn create_task(
        &mut self,
        task_id: &TaskId,
        profile_id: &ProfileId,
        name: &str,
        accessions: &[String],
        destination: &str,
    ) -> Result<(), ApplicationError> {
        let profile = self
            .state
            .profiles
            .get(profile_id)
            .ok_or_else(|| ApplicationError::UnknownProfile(profile_id.clone()))?;
        if profile.active_task.is_some() {
            return Err(ApplicationError::ProfileBusy(profile_id.clone()));
        }
        if self.state.tasks.contains_key(task_id)
            || self
                .repository
                .as_ref()
                .is_some_and(|repository| repository.has_task(task_id).unwrap_or(false))
        {
            return Err(ApplicationError::InvalidTask(format!(
                "任务编号 {task_id} 已存在"
            )));
        }
        let parsed = parse_accessions(&accessions.join("\n"));
        if parsed.values.is_empty() || !parsed.invalid_values.is_empty() {
            return Err(ApplicationError::InvalidTask(
                "至少需要一个有效检查号，且不能包含通配符或控制字符".to_owned(),
            ));
        }
        if name.trim().is_empty() {
            return Err(ApplicationError::InvalidTask("任务名称不能为空".to_owned()));
        }
        if destination.trim().is_empty() {
            return Err(ApplicationError::InvalidTask("目标目录不能为空".to_owned()));
        }
        let new_accessions = parsed
            .values
            .iter()
            .map(String::as_str)
            .collect::<HashSet<_>>();
        let duplicate = self.state.tasks.values().find_map(|other| {
            (other.task.profile_id == *profile_id
                && !other.task.phase.is_terminal()
                && other
                    .task
                    .accessions
                    .iter()
                    .any(|value| new_accessions.contains(value.as_str())))
            .then_some(other.task.id.clone())
        });
        if let Some(other_task_id) = duplicate {
            return Err(ApplicationError::InvalidTask(format!(
                "检查号已存在于未完成任务 {other_task_id}"
            )));
        }
        let mut config = profile.profile.config.clone();
        destination
            .trim()
            .clone_into(&mut config.dicom_destination_folder);
        validate_download_config(&config)?;
        let timestamp = now();
        let task = Task {
            id: task_id.clone(),
            profile_id: profile_id.clone(),
            name: name.trim().to_owned(),
            phase: TaskPhase::Queued,
            config,
            accessions: parsed.values,
            results: Vec::new(),
            partial_results: Vec::new(),
            trial_required: false,
            trial_consumed: false,
            pdi_attempt_id: String::new(),
            current_accession: String::new(),
            speed_bytes_per_second: 0.0,
            error_message: String::new(),
            created_at: timestamp.clone(),
            updated_at: timestamp,
        };
        task.validate_new()
            .map_err(|error| ApplicationError::InvalidTask(error.to_string()))?;
        if let Some(repository) = &self.repository {
            repository.insert_task(&task)?;
        }
        let summary = summary_for_new_task(&task);
        self.state.tasks.insert(
            task_id.clone(),
            RuntimeTask {
                task,
                summary: summary.clone(),
                control: None,
                pending_progress: None,
            },
        );
        let _ = self.events.send(AppEvent::TaskCreated {
            task_id: task_id.clone(),
        });
        let _ = self.events.send(AppEvent::TaskUpdated { summary });
        Ok(())
    }

    async fn start_task(&mut self, task_id: &TaskId) -> Result<(), ApplicationError> {
        let profile_id = self
            .state
            .tasks
            .get(task_id)
            .ok_or_else(|| ApplicationError::UnknownTask(task_id.clone()))?
            .task
            .profile_id
            .clone();
        self.preflight_profile_start(&profile_id)?;
        let retry = self.state.tasks.get(task_id).is_some_and(|task| {
            matches!(
                task.task.phase,
                TaskPhase::DownloadRetryable | TaskPhase::Failed
            )
        });
        if retry && let Some(repository) = &self.repository {
            repository.reset_results_for_retry(task_id)?;
            let refreshed = repository.get_task(task_id)?;
            let summary = repository.get_task_summary(task_id)?;
            let runtime = self
                .state
                .tasks
                .get_mut(task_id)
                .ok_or_else(|| ApplicationError::UnknownTask(task_id.clone()))?;
            runtime.task = refreshed;
            runtime.summary = summary;
            runtime.pending_progress = None;
        }
        self.start_profile(&profile_id)?;
        self.dispatch_task(task_id).await
    }

    async fn dispatch_task(&mut self, task_id: &TaskId) -> Result<(), ApplicationError> {
        let profile_id = self
            .state
            .tasks
            .get(task_id)
            .ok_or_else(|| ApplicationError::UnknownTask(task_id.clone()))?
            .task
            .profile_id
            .clone();
        let runtime = self
            .state
            .profiles
            .get_mut(&profile_id)
            .ok_or_else(|| ApplicationError::UnknownProfile(profile_id.clone()))?;
        if let Some(active) = &runtime.active_task
            && active != task_id
        {
            return Err(ApplicationError::ProfileBusy(profile_id));
        }
        let sender = runtime
            .commands
            .clone()
            .ok_or_else(|| ApplicationError::InvalidProfile("接收器尚未启动".to_owned()))?;
        let task = self
            .state
            .tasks
            .get_mut(task_id)
            .ok_or_else(|| ApplicationError::UnknownTask(task_id.clone()))?;
        if task.control.is_some() {
            return Ok(());
        }
        if !matches!(
            task.task.phase,
            TaskPhase::Queued
                | TaskPhase::Paused
                | TaskPhase::DownloadRetryable
                | TaskPhase::Failed
        ) {
            return Err(ApplicationError::InvalidTask(format!(
                "任务当前状态 {} 不能启动",
                task.task.phase
            )));
        }
        let control = TaskControl::new();
        sender
            .send(ProfileWork::Run {
                task: Box::new(task.task.clone()),
                control: control.clone(),
            })
            .await
            .map_err(|_| ApplicationError::Closed)?;
        task.control = Some(control);
        runtime.active_task = Some(task_id.clone());
        Ok(())
    }

    fn pause_task(&mut self, task_id: &TaskId) -> Result<(), ApplicationError> {
        let task = self
            .state
            .tasks
            .get_mut(task_id)
            .ok_or_else(|| ApplicationError::UnknownTask(task_id.clone()))?;
        if task.task.phase != TaskPhase::Running {
            return Err(ApplicationError::InvalidTask(format!(
                "任务当前状态 {} 不能暂停",
                task.task.phase
            )));
        }
        let control = task
            .control
            .as_ref()
            .ok_or_else(|| ApplicationError::InvalidTask("任务尚未运行".to_owned()))?;
        if control.current() != transfer::ControlState::Running {
            return Err(ApplicationError::InvalidTask(
                "任务控制状态已变化，不能重复暂停".to_owned(),
            ));
        }
        control.pause();
        persist_phase(self.repository.as_ref(), task, TaskPhase::PausePending, "")?;
        let _ = self.events.send(AppEvent::TaskPausePending {
            task_id: task_id.clone(),
        });
        self.emit_task_summary(task_id);
        Ok(())
    }

    async fn resume_task(&mut self, task_id: &TaskId) -> Result<(), ApplicationError> {
        let (phase, control) = self
            .state
            .tasks
            .get(task_id)
            .map(|task| (task.task.phase, task.control.clone()))
            .ok_or_else(|| ApplicationError::UnknownTask(task_id.clone()))?;
        if phase != TaskPhase::Paused {
            return Err(ApplicationError::InvalidTask(format!(
                "任务当前状态 {phase} 不能继续"
            )));
        }
        if let Some(control) = control {
            if control.current() != transfer::ControlState::Paused {
                return Err(ApplicationError::InvalidTask(
                    "任务控制状态已变化，不能继续".to_owned(),
                ));
            }
            control.resume();
            let task = self
                .state
                .tasks
                .get_mut(task_id)
                .ok_or_else(|| ApplicationError::UnknownTask(task_id.clone()))?;
            persist_phase(self.repository.as_ref(), task, TaskPhase::Running, "")?;
            let _ = self.events.send(AppEvent::TaskResumed {
                task_id: task_id.clone(),
            });
            self.emit_task_summary(task_id);
            return Ok(());
        }
        self.start_task(task_id).await
    }

    fn cancel_task(&mut self, task_id: &TaskId) -> Result<(), ApplicationError> {
        let task = self
            .state
            .tasks
            .get_mut(task_id)
            .ok_or_else(|| ApplicationError::UnknownTask(task_id.clone()))?;
        if let Some(control) = &task.control {
            control.cancel();
            persist_phase(self.repository.as_ref(), task, TaskPhase::Cancelling, "")?;
        } else {
            persist_phase(self.repository.as_ref(), task, TaskPhase::Cancelled, "")?;
            if let Some(repository) = &self.repository {
                repository.finalize_cancelled_accessions(task_id)?;
                task.task = repository.get_task(task_id)?;
                task.summary = repository.get_task_summary(task_id)?;
            }
            let _ = self.events.send(AppEvent::TaskCancelled {
                task_id: task_id.clone(),
            });
        }
        self.emit_task_summary(task_id);
        Ok(())
    }

    fn delete_task(&mut self, task_id: &TaskId) -> Result<(), ApplicationError> {
        let task = self
            .state
            .tasks
            .get(task_id)
            .ok_or_else(|| ApplicationError::UnknownTask(task_id.clone()))?;
        if task.control.is_some() || !task.task.phase.can_delete() {
            return Err(ApplicationError::InvalidTask(
                "运行中的任务不能删除".to_owned(),
            ));
        }
        if let Some(repository) = &self.repository {
            repository.delete_task(task_id)?;
        }
        self.state.tasks.remove(task_id);
        let _ = self.events.send(AppEvent::TaskDeleted {
            task_id: task_id.clone(),
        });
        Ok(())
    }

    fn apply_internal(&mut self, event: InternalEvent) {
        if let Err(error) = self.apply_internal_result(event) {
            self.reject(&error);
        }
        self.publish_snapshot();
    }

    fn apply_internal_result(&mut self, event: InternalEvent) -> Result<(), ApplicationError> {
        match event {
            InternalEvent::ReceiverReady {
                profile_id,
                address,
            } => self.receiver_ready(&profile_id, address),
            InternalEvent::ReceiverStopped { profile_id } => {
                self.receiver_stopped(&profile_id);
                Ok(())
            }
            InternalEvent::ReceiverFaulted {
                profile_id,
                message,
            } => self.receiver_faulted(&profile_id, &message),
            InternalEvent::AssociationCount { profile_id, count } => {
                if let Some(profile) = self.state.profiles.get_mut(&profile_id) {
                    profile.receiver_status.active_associations = count;
                }
                Ok(())
            }
            InternalEvent::TaskStarted { task_id } => self.task_started(&task_id),
            InternalEvent::TaskPaused { task_id } => self.task_paused(&task_id),
            InternalEvent::AccessionProgress { task_id, result } => {
                if let Some(task) = self.state.tasks.get_mut(&task_id) {
                    task.summary.current_accession.clone_from(&result.accession);
                    task.summary.speed_bytes_per_second = result.speed_bytes_per_second;
                    task.summary.received_bytes = result.received_bytes;
                    task.pending_progress = Some(result);
                    self.state.progress_dirty.insert(task_id);
                }
                Ok(())
            }
            InternalEvent::AccessionFinished {
                task_id,
                result,
                acknowledgment,
            } => match self.record_result(&task_id, result) {
                Ok(()) => {
                    let _ = acknowledgment.send(Ok(()));
                    Ok(())
                }
                Err(error) => {
                    let _ = acknowledgment.send(Err(error.to_string()));
                    Err(error)
                }
            },
            InternalEvent::TaskFinished {
                task_id,
                phase,
                error,
            } => self.finish_runtime_task(&task_id, phase, &error),
            InternalEvent::Log {
                level,
                source,
                task_id,
                message,
            } => {
                self.append_log(level, source, task_id, message);
                Ok(())
            }
        }
    }

    fn task_started(&mut self, task_id: &TaskId) -> Result<(), ApplicationError> {
        let phase = self
            .state
            .tasks
            .get(task_id)
            .ok_or_else(|| ApplicationError::UnknownTask(task_id.clone()))?
            .task
            .phase;
        if matches!(phase, TaskPhase::Cancelling | TaskPhase::Cancelled) {
            return Ok(());
        }
        if !matches!(
            phase,
            TaskPhase::Queued
                | TaskPhase::Running
                | TaskPhase::Paused
                | TaskPhase::DownloadRetryable
                | TaskPhase::Failed
        ) {
            return Err(ApplicationError::InvalidTask(format!(
                "任务当前状态 {phase} 不能进入运行状态"
            )));
        }
        self.set_task_phase(task_id, TaskPhase::Running, "")?;
        let _ = self.events.send(AppEvent::TaskStarted {
            task_id: task_id.clone(),
        });
        Ok(())
    }

    fn task_paused(&mut self, task_id: &TaskId) -> Result<(), ApplicationError> {
        let phase = self
            .state
            .tasks
            .get(task_id)
            .ok_or_else(|| ApplicationError::UnknownTask(task_id.clone()))?
            .task
            .phase;
        if matches!(phase, TaskPhase::Cancelling | TaskPhase::Cancelled) {
            return Ok(());
        }
        if phase != TaskPhase::PausePending {
            return Err(ApplicationError::InvalidTask(format!(
                "任务当前状态 {phase} 不能确认暂停"
            )));
        }
        self.set_task_phase(task_id, TaskPhase::Paused, "")?;
        let _ = self.events.send(AppEvent::TaskPaused {
            task_id: task_id.clone(),
        });
        Ok(())
    }

    fn receiver_ready(
        &mut self,
        profile_id: &ProfileId,
        address: std::net::SocketAddr,
    ) -> Result<(), ApplicationError> {
        let runtime = self
            .state
            .profiles
            .get_mut(profile_id)
            .ok_or_else(|| ApplicationError::UnknownProfile(profile_id.clone()))?;
        runtime.profile.runtime_status = ProfileRuntimeStatus::Running;
        runtime.receiver_status.state = ReceiverState::Listening;
        runtime.receiver_status.port = address.port();
        runtime.receiver_status.message = format!("接收器已监听 {address}");
        let _ = self.events.send(AppEvent::ProfileStarted {
            profile_id: profile_id.clone(),
        });
        let _ = self.events.send(AppEvent::ReceiverStatusChanged {
            status: runtime.receiver_status.clone(),
        });
        Ok(())
    }

    fn receiver_stopped(&mut self, profile_id: &ProfileId) {
        if let Some(runtime) = self.state.profiles.get_mut(profile_id) {
            runtime.profile.runtime_status = ProfileRuntimeStatus::Stopped;
            runtime.receiver_status = stopped_receiver_status(&runtime.profile);
            runtime.commands = None;
            runtime.active_task = None;
            let _ = self.events.send(AppEvent::ProfileStopped {
                profile_id: profile_id.clone(),
            });
            let _ = self.events.send(AppEvent::ReceiverStatusChanged {
                status: runtime.receiver_status.clone(),
            });
        }
    }

    fn receiver_faulted(
        &mut self,
        profile_id: &ProfileId,
        message: &str,
    ) -> Result<(), ApplicationError> {
        let active_task = self
            .state
            .profiles
            .get(profile_id)
            .and_then(|runtime| runtime.active_task.clone());
        if let Some(runtime) = self.state.profiles.get_mut(profile_id) {
            runtime.profile.runtime_status = ProfileRuntimeStatus::Faulted;
            runtime.receiver_status.state = ReceiverState::Faulted;
            message.clone_into(&mut runtime.receiver_status.message);
            runtime.commands = None;
            runtime.active_task = None;
            let _ = self.events.send(AppEvent::ReceiverStatusChanged {
                status: runtime.receiver_status.clone(),
            });
        }
        if let Some(task_id) = &active_task {
            self.set_task_phase(task_id, TaskPhase::DownloadRetryable, message)?;
            if let Some(task) = self.state.tasks.get_mut(task_id) {
                task.control = None;
            }
        }
        self.append_log(
            LogLevel::Error,
            "receiver".to_owned(),
            active_task,
            message.to_owned(),
        );
        Ok(())
    }

    fn record_result(
        &mut self,
        task_id: &TaskId,
        result: AccessionResult,
    ) -> Result<(), ApplicationError> {
        if let Some(repository) = &self.repository {
            repository.record_result(task_id, &result)?;
        }
        let task = self
            .state
            .tasks
            .get_mut(task_id)
            .ok_or_else(|| ApplicationError::UnknownTask(task_id.clone()))?;
        task.task
            .results
            .retain(|value| value.accession != result.accession);
        task.task.results.push(result.clone());
        task.pending_progress = None;
        task.summary = if let Some(repository) = &self.repository {
            repository.get_task_summary(task_id)?
        } else {
            summary_from_task(&task.task)
        };
        let _ = self.events.send(AppEvent::AccessionUpdated {
            task_id: task_id.clone(),
            result,
        });
        let _ = self.events.send(AppEvent::TaskUpdated {
            summary: task.summary.clone(),
        });
        Ok(())
    }

    fn finish_runtime_task(
        &mut self,
        task_id: &TaskId,
        mut phase: TaskPhase,
        error: &str,
    ) -> Result<(), ApplicationError> {
        let profile_id = self
            .state
            .tasks
            .get(task_id)
            .ok_or_else(|| ApplicationError::UnknownTask(task_id.clone()))?
            .task
            .profile_id
            .clone();
        let mut terminal_error = error.to_owned();
        if phase == TaskPhase::Completed {
            let summary = match &self.repository {
                Some(repository) => repository.get_task_summary(task_id)?,
                None => self
                    .state
                    .tasks
                    .get(task_id)
                    .ok_or_else(|| ApplicationError::UnknownTask(task_id.clone()))?
                    .summary
                    .clone(),
            };
            if summary.pending_count > 0 || summary.partial_count > 0 || summary.failed_count > 0 {
                phase = TaskPhase::DownloadRetryable;
                terminal_error = format!(
                    "任务完成校验未通过：仍有 {} 条待处理、{} 条部分成功、{} 条失败",
                    summary.pending_count, summary.partial_count, summary.failed_count
                );
            }
        }
        self.set_task_phase(task_id, phase, &terminal_error)?;
        if phase == TaskPhase::Cancelled
            && let Some(repository) = &self.repository
        {
            repository.finalize_cancelled_accessions(task_id)?;
            if let Some(task) = self.state.tasks.get_mut(task_id) {
                task.task = repository.get_task(task_id)?;
                task.summary = repository.get_task_summary(task_id)?;
            }
        }
        if let Some(task) = self.state.tasks.get_mut(task_id) {
            task.control = None;
            task.pending_progress = None;
        }
        if let Some(profile) = self.state.profiles.get_mut(&profile_id) {
            profile.active_task = None;
        }
        match phase {
            TaskPhase::Cancelled => {
                let _ = self.events.send(AppEvent::TaskCancelled {
                    task_id: task_id.clone(),
                });
            }
            _ => {
                let _ = self.events.send(AppEvent::TaskFinished {
                    task_id: task_id.clone(),
                });
            }
        }
        Ok(())
    }

    fn set_task_phase(
        &mut self,
        task_id: &TaskId,
        phase: TaskPhase,
        error: &str,
    ) -> Result<(), ApplicationError> {
        let task = self
            .state
            .tasks
            .get_mut(task_id)
            .ok_or_else(|| ApplicationError::UnknownTask(task_id.clone()))?;
        persist_phase(self.repository.as_ref(), task, phase, error)?;
        self.emit_task_summary(task_id);
        Ok(())
    }

    fn emit_task_summary(&self, task_id: &TaskId) {
        if let Some(task) = self.state.tasks.get(task_id) {
            let _ = self.events.send(AppEvent::TaskUpdated {
                summary: task.summary.clone(),
            });
        }
    }

    fn flush_progress(&mut self) {
        let dirty = self.state.progress_dirty.drain().collect::<Vec<_>>();
        for task_id in dirty {
            let Some(task) = self.state.tasks.get_mut(&task_id) else {
                continue;
            };
            let Some(progress) = task.pending_progress.as_ref() else {
                continue;
            };
            if let Some(repository) = &self.repository
                && let Err(error) = repository.record_result(&task_id, progress).and_then(|()| {
                    repository.update_task_runtime(
                        &task_id,
                        &progress.accession,
                        progress.speed_bytes_per_second,
                        "",
                    )
                })
            {
                self.state.runtime_error = error.to_string();
            }
            let _ = self.events.send(AppEvent::AccessionUpdated {
                task_id: task_id.clone(),
                result: progress.clone(),
            });
            let _ = self.events.send(AppEvent::TaskUpdated {
                summary: task.summary.clone(),
            });
        }
    }

    fn reload_workspace(&mut self) -> Result<(), ApplicationError> {
        let Some(repository) = &self.repository else {
            self.publish_workspace();
            return Ok(());
        };
        if self
            .state
            .profiles
            .values()
            .any(|profile| profile.worker.is_some())
        {
            return Err(ApplicationError::InvalidTask(
                "接收器运行期间不能重新加载工作区".to_owned(),
            ));
        }
        self.state = load_runtime_state(repository)?;
        self.publish_workspace();
        Ok(())
    }

    fn publish_workspace(&self) {
        let snapshot =
            snapshot_from_state(&self.state, self.shutdown_requested.load(Ordering::Acquire));
        let _ = self.events.send(AppEvent::WorkspaceLoaded {
            profiles: snapshot.profiles.clone(),
            tasks: snapshot.tasks.clone(),
        });
        self.snapshot.send_replace(snapshot);
    }

    fn publish_snapshot(&self) {
        self.snapshot.send_replace(snapshot_from_state(
            &self.state,
            self.shutdown_requested.load(Ordering::Acquire),
        ));
    }

    fn reject(&mut self, error: &ApplicationError) {
        let message = error.to_string();
        self.state.runtime_error.clone_from(&message);
        self.append_log(
            LogLevel::Error,
            "application".to_owned(),
            None,
            message.clone(),
        );
        let _ = self.events.send(AppEvent::CommandRejected { message });
        self.publish_snapshot();
    }

    fn append_log(
        &mut self,
        level: LogLevel,
        source: String,
        task_id: Option<TaskId>,
        message: String,
    ) {
        let entry = LogEntry {
            timestamp: now(),
            level,
            source,
            task_id,
            message,
        };
        if self.state.logs.len() >= MAX_UI_LOGS {
            self.state.logs.pop_front();
        }
        self.state.logs.push_back(entry.clone());
        let _ = self.events.send(AppEvent::LogAppended { entry });
    }

    async fn reap_finished_workers(&mut self) {
        let finished = self
            .state
            .profiles
            .iter_mut()
            .filter_map(|(profile_id, runtime)| {
                runtime
                    .worker
                    .as_ref()
                    .is_some_and(JoinHandle::is_finished)
                    .then(|| {
                        runtime
                            .worker
                            .take()
                            .map(|worker| (profile_id.clone(), worker))
                    })
                    .flatten()
            })
            .collect::<Vec<_>>();
        for (profile_id, worker) in finished {
            match worker.await {
                Ok(()) => {
                    if let Some(runtime) = self.state.profiles.get_mut(&profile_id) {
                        runtime.commands = None;
                    }
                }
                Err(error) => {
                    let message = format!("Profile {profile_id} 后台线程异常退出：{error}");
                    if let Err(error) = self.receiver_faulted(&profile_id, &message) {
                        self.reject(&error);
                    }
                }
            }
        }
    }

    async fn shutdown(&mut self) {
        self.flush_progress();
        for task in self.state.tasks.values_mut() {
            if let Some(control) = &task.control {
                let phase = if control.current() == transfer::ControlState::Cancelled {
                    TaskPhase::Cancelled
                } else {
                    TaskPhase::DownloadRetryable
                };
                control.stop_profile();
                let _ = persist_phase(
                    self.repository.as_ref(),
                    task,
                    phase,
                    if phase == TaskPhase::Cancelled {
                        ""
                    } else {
                        "应用退出，任务可继续"
                    },
                );
                if phase == TaskPhase::Cancelled
                    && let Some(repository) = &self.repository
                {
                    let _ = repository.finalize_cancelled_accessions(&task.task.id);
                    if let Ok(reloaded) = repository.get_task(&task.task.id) {
                        task.task = reloaded;
                    }
                    if let Ok(summary) = repository.get_task_summary(&task.task.id) {
                        task.summary = summary;
                    }
                }
            }
        }
        for runtime in self.state.profiles.values_mut() {
            runtime.profile.runtime_status = ProfileRuntimeStatus::Stopping;
            runtime.receiver_status.state = ReceiverState::Stopping;
            if let Some(sender) = &runtime.commands {
                let _ = sender.send(ProfileWork::Stop).await;
            }
        }
        let mut workers = self
            .state
            .profiles
            .values_mut()
            .filter_map(|runtime| runtime.worker.take())
            .collect::<Vec<_>>();
        for worker in &mut workers {
            if tokio::time::timeout(WORKER_SHUTDOWN_TIMEOUT, &mut *worker)
                .await
                .is_err()
            {
                worker.abort();
                let _ = worker.await;
            }
        }
        for runtime in self.state.profiles.values_mut() {
            runtime.commands = None;
            runtime.active_task = None;
            runtime.profile.runtime_status = ProfileRuntimeStatus::Stopped;
            runtime.receiver_status = stopped_receiver_status(&runtime.profile);
        }
        self.shutdown_requested.store(true, Ordering::Release);
        self.publish_snapshot();
        let _ = self.events.send(AppEvent::ApplicationStopping);
    }
}

fn recover_interrupted_tasks(repository: &StateRepository) -> Result<(), ApplicationError> {
    for summary in repository.list_task_summaries()? {
        match summary.phase {
            TaskPhase::Running | TaskPhase::PausePending => {
                repository.set_task_phase(&summary.task_id, TaskPhase::DownloadRetryable)?;
                repository.update_task_runtime(
                    &summary.task_id,
                    "",
                    0.0,
                    "上次运行意外中断，可继续任务",
                )?;
            }
            TaskPhase::Cancelling => {
                repository.finalize_cancelled_accessions(&summary.task_id)?;
                repository.set_task_phase(&summary.task_id, TaskPhase::Cancelled)?;
                repository.update_task_runtime(&summary.task_id, "", 0.0, "")?;
            }
            _ => {}
        }
    }
    Ok(())
}

fn load_runtime_state(repository: &StateRepository) -> Result<RuntimeState, ApplicationError> {
    let profiles = repository.list_profiles()?;
    let summaries = repository.list_task_summaries()?;
    let mut state = RuntimeState::default();
    for mut profile in profiles {
        profile.runtime_status = ProfileRuntimeStatus::Stopped;
        let receiver_status = stopped_receiver_status(&profile);
        state.profiles.insert(
            profile.id.clone(),
            RuntimeProfile {
                profile,
                commands: None,
                worker: None,
                active_task: None,
                receiver_status,
            },
        );
    }
    for summary in summaries {
        let task = repository.get_task(&summary.task_id)?;
        state.tasks.insert(
            summary.task_id.clone(),
            RuntimeTask {
                task,
                summary,
                control: None,
                pending_progress: None,
            },
        );
    }
    Ok(state)
}

fn persist_phase(
    repository: Option<&StateRepository>,
    task: &mut RuntimeTask,
    phase: TaskPhase,
    error: &str,
) -> Result<(), ApplicationError> {
    task.task.phase = phase;
    error.clone_into(&mut task.task.error_message);
    task.task.updated_at = now();
    task.summary.phase = phase;
    error.clone_into(&mut task.summary.error_message);
    task.summary.updated_at.clone_from(&task.task.updated_at);
    if let Some(repository) = repository {
        repository.set_task_phase(&task.task.id, phase)?;
        repository.update_task_runtime(
            &task.task.id,
            &task.task.current_accession,
            task.task.speed_bytes_per_second,
            error,
        )?;
        task.summary = repository.get_task_summary(&task.task.id)?;
    }
    Ok(())
}

fn validate_download_config(config: &AppConfig) -> Result<(), ApplicationError> {
    if config.anonymization_enabled {
        return Err(ApplicationError::InvalidProfile(
            "原生预览暂不支持匿名化，请关闭匿名化或使用旧版".to_owned(),
        ));
    }
    if config.pdi_export_enabled {
        return Err(ApplicationError::InvalidProfile(
            "原生预览暂不支持 PDI 导出，请关闭 PDI 后再启动".to_owned(),
        ));
    }
    if config.pacs_server_port == 0 {
        return Err(ApplicationError::InvalidProfile(
            "pacs_server_port: PACS 端口必须在 1 到 65535 之间".to_owned(),
        ));
    }
    if config.storage_port == 0 {
        return Err(ApplicationError::InvalidProfile(
            "storage_port: 接收端口必须在 1 到 65535 之间".to_owned(),
        ));
    }
    let issues = config
        .validate()
        .into_iter()
        .filter(|issue| {
            matches!(
                issue.field.as_str(),
                "dicom_destination_folder"
                    | "pacs_server_ip"
                    | "calling_ae_title"
                    | "pacs_ae_title"
                    | "storage_ae_title"
                    | "auto_retry_attempts"
                    | "auto_retry_backoff_seconds"
            )
        })
        .map(|issue| format!("{}: {}", issue.field, issue.message))
        .collect::<Vec<_>>();
    if issues.is_empty() {
        Ok(())
    } else {
        Err(ApplicationError::InvalidProfile(issues.join("; ")))
    }
}

fn stopped_receiver_status(profile: &Profile) -> ReceiverStatus {
    ReceiverStatus {
        profile_id: profile.id.clone(),
        state: ReceiverState::Stopped,
        ae_title: profile.config.storage_ae_title.clone(),
        port: profile.config.storage_port,
        active_associations: 0,
        message: "接收器未启动".to_owned(),
    }
}

fn snapshot_from_state(state: &RuntimeState, shutting_down: bool) -> ApplicationSnapshot {
    let mut profiles = state
        .profiles
        .values()
        .map(|runtime| runtime.profile.clone())
        .collect::<Vec<_>>();
    profiles.sort_by(|left, right| left.id.cmp(&right.id));
    let mut tasks = state
        .tasks
        .values()
        .map(|runtime| runtime.summary.clone())
        .collect::<Vec<_>>();
    tasks.sort_by(|left, right| right.created_at.cmp(&left.created_at));
    let mut receiver_statuses = state
        .profiles
        .values()
        .map(|runtime| runtime.receiver_status.clone())
        .collect::<Vec<_>>();
    receiver_statuses.sort_by(|left, right| left.profile_id.cmp(&right.profile_id));
    let computed_speed = tasks
        .iter()
        .filter(|task| matches!(task.phase, TaskPhase::Running | TaskPhase::PausePending))
        .map(|task| speed_to_u64(task.speed_bytes_per_second))
        .fold(0_u64, u64::saturating_add);
    ApplicationSnapshot {
        active_profiles: profiles
            .iter()
            .filter(|profile| {
                matches!(
                    profile.runtime_status,
                    ProfileRuntimeStatus::Starting | ProfileRuntimeStatus::Running
                )
            })
            .count(),
        active_tasks: tasks
            .iter()
            .filter(|task| {
                matches!(
                    task.phase,
                    TaskPhase::Running | TaskPhase::PausePending | TaskPhase::Cancelling
                )
            })
            .count(),
        aggregate_speed_bytes_per_second: computed_speed.max(state.manual_speed_bytes_per_second),
        profiles,
        tasks,
        receiver_statuses,
        logs: state.logs.iter().cloned().collect(),
        startup_error: String::new(),
        runtime_error: state.runtime_error.clone(),
        shutting_down,
    }
}

fn summary_for_new_task(task: &Task) -> TaskSummary {
    TaskSummary {
        task_id: task.id.clone(),
        profile_id: task.profile_id.clone(),
        name: task.name.clone(),
        phase: task.phase,
        total_count: task.accessions.len() as u64,
        processed_count: 0,
        pending_count: task.accessions.len() as u64,
        completed_count: 0,
        failed_count: 0,
        file_count: 0,
        received_bytes: 0,
        speed_bytes_per_second: 0.0,
        current_accession: String::new(),
        error_message: String::new(),
        created_at: task.created_at.clone(),
        updated_at: task.updated_at.clone(),
        no_data_count: 0,
        partial_count: 0,
        cancelled_count: 0,
    }
}

fn summary_from_task(task: &Task) -> TaskSummary {
    let mut summary = summary_for_new_task(task);
    summary.phase = task.phase;
    summary.processed_count = task.results.len() as u64;
    summary.pending_count = summary.total_count.saturating_sub(summary.processed_count);
    summary.completed_count = task
        .results
        .iter()
        .filter(|result| {
            matches!(
                result.status,
                dcmget_domain::AccessionStatus::Completed | dcmget_domain::AccessionStatus::NoData
            )
        })
        .count() as u64;
    summary.failed_count = task
        .results
        .iter()
        .filter(|result| {
            matches!(
                result.status,
                dcmget_domain::AccessionStatus::Failed | dcmget_domain::AccessionStatus::Partial
            )
        })
        .count() as u64;
    summary.file_count = task.results.iter().map(|result| result.file_count).sum();
    summary.received_bytes = task
        .results
        .iter()
        .map(|result| result.received_bytes)
        .sum();
    summary
}

fn now() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Micros, true)
}

#[allow(
    clippy::cast_possible_truncation,
    clippy::cast_precision_loss,
    clippy::cast_sign_loss
)]
fn speed_to_u64(value: f64) -> u64 {
    if !value.is_finite() || value <= 0.0 {
        0
    } else if value >= u64::MAX as f64 {
        u64::MAX
    } else {
        value as u64
    }
}

#[cfg(test)]
mod tests {
    use std::net::{Ipv4Addr, SocketAddr};

    use super::*;

    #[derive(Default)]
    struct IdleRuntimeFactory;

    impl ProfileRuntimeFactory for IdleRuntimeFactory {
        fn spawn(
            &self,
            profile: Profile,
            mut commands: mpsc::Receiver<ProfileWork>,
            events: mpsc::Sender<InternalEvent>,
        ) -> JoinHandle<()> {
            tokio::spawn(async move {
                let _ = events
                    .send(InternalEvent::ReceiverReady {
                        profile_id: profile.id.clone(),
                        address: SocketAddr::from((
                            Ipv4Addr::LOCALHOST,
                            profile.config.storage_port,
                        )),
                    })
                    .await;
                while let Some(command) = commands.recv().await {
                    match command {
                        ProfileWork::Run { task, .. } => {
                            let _ = events
                                .send(InternalEvent::TaskStarted {
                                    task_id: task.id.clone(),
                                })
                                .await;
                        }
                        ProfileWork::Stop => break,
                    }
                }
                let _ = events
                    .send(InternalEvent::ReceiverStopped {
                        profile_id: profile.id,
                    })
                    .await;
            })
        }
    }

    #[derive(Default)]
    struct PanicOnTaskRuntimeFactory;

    impl ProfileRuntimeFactory for PanicOnTaskRuntimeFactory {
        fn spawn(
            &self,
            profile: Profile,
            mut commands: mpsc::Receiver<ProfileWork>,
            events: mpsc::Sender<InternalEvent>,
        ) -> JoinHandle<()> {
            tokio::spawn(async move {
                let _ = events
                    .send(InternalEvent::ReceiverReady {
                        profile_id: profile.id,
                        address: SocketAddr::from((
                            Ipv4Addr::LOCALHOST,
                            profile.config.storage_port,
                        )),
                    })
                    .await;
                if let Some(ProfileWork::Run { task, .. }) = commands.recv().await {
                    let _ = events
                        .send(InternalEvent::TaskStarted {
                            task_id: task.id.clone(),
                        })
                        .await;
                    panic!("simulated profile worker failure");
                }
            })
        }
    }

    fn repository() -> (tempfile::TempDir, StateRepository, ProfileId) {
        let temp = tempfile::tempdir().unwrap();
        let repository = StateRepository::open(temp.path().join("state.sqlite3")).unwrap();
        let profile_id = ProfileId::legacy(1).unwrap();
        let port = available_port();
        repository
            .upsert_profile(&Profile {
                id: profile_id.clone(),
                display_name: "实例 1".into(),
                config: AppConfig {
                    storage_port: port,
                    ..AppConfig::default()
                },
                runtime_status: ProfileRuntimeStatus::Stopped,
                source_config_path: String::new(),
                created_at: String::new(),
                updated_at: String::new(),
            })
            .unwrap();
        (temp, repository, profile_id)
    }

    fn available_port() -> u16 {
        let listener = std::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        listener.local_addr().unwrap().port()
    }

    fn open_with_idle_runtime(
        repository: StateRepository,
    ) -> (ApplicationService, ApplicationHandle) {
        ApplicationService::from_repository(repository, None, Arc::new(IdleRuntimeFactory)).unwrap()
    }

    #[tokio::test]
    async fn open_loads_real_profiles_and_task_snapshot() {
        let (temp, repository, profile_id) = repository();
        repository
            .create_task(
                &profile_id,
                "恢复任务",
                AppConfig::default(),
                vec!["A001".into()],
                false,
            )
            .unwrap();
        let (service, handle) = ApplicationService::open(repository.path()).unwrap();
        let worker = tokio::spawn(service.run());
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert_eq!(handle.snapshot().profiles.len(), 1);
        assert_eq!(handle.snapshot().tasks.len(), 1);
        handle.send(AppCommand::ExitApplication).await.unwrap();
        worker.await.unwrap();
        drop(temp);
    }

    #[tokio::test]
    async fn profile_update_persists_and_active_port_conflict_is_rejected() {
        let (_temp, repository, profile_id) = repository();
        let (service, handle) = ApplicationService::open(repository.path()).unwrap();
        let worker = tokio::spawn(service.run());
        let first_port = repository
            .get_profile(&profile_id)
            .unwrap()
            .config
            .storage_port;
        let second_id = ProfileId::legacy(2).unwrap();
        let mut second = Profile {
            id: second_id.clone(),
            display_name: "实例 2".into(),
            config: AppConfig {
                storage_port: available_port(),
                ..AppConfig::default()
            },
            runtime_status: ProfileRuntimeStatus::Stopped,
            source_config_path: String::new(),
            created_at: String::new(),
            updated_at: String::new(),
        };
        handle
            .send(AppCommand::UpsertProfile {
                profile: Box::new(second.clone()),
            })
            .await
            .unwrap();
        handle
            .send(AppCommand::StartProfile {
                profile_id: profile_id.clone(),
            })
            .await
            .unwrap();
        tokio::time::sleep(Duration::from_millis(50)).await;
        second.config.storage_port = first_port;
        handle
            .send(AppCommand::UpsertProfile {
                profile: Box::new(second),
            })
            .await
            .unwrap();
        handle
            .send(AppCommand::StartProfile {
                profile_id: second_id,
            })
            .await
            .unwrap();
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert!(handle.snapshot().runtime_error.contains("conflicts"));
        handle.send(AppCommand::ExitApplication).await.unwrap();
        worker.await.unwrap();
    }

    #[tokio::test]
    async fn profile_update_is_rejected_while_retryable_task_keeps_config_snapshot() {
        let (_temp, repository, profile_id) = repository();
        let original = repository.get_profile(&profile_id).unwrap();
        let task = repository
            .create_task(
                &profile_id,
                "retryable",
                original.config.clone(),
                vec!["A001".into()],
                false,
            )
            .unwrap();
        repository
            .set_task_phase(&task.id, TaskPhase::DownloadRetryable)
            .unwrap();
        let inspect = repository.clone();
        let (service, handle) = open_with_idle_runtime(repository);
        let worker = tokio::spawn(service.run());

        let mut changed = original.clone();
        changed.config.storage_port = available_port();
        handle
            .send(AppCommand::UpsertProfile {
                profile: Box::new(changed),
            })
            .await
            .unwrap();
        tokio::time::timeout(Duration::from_secs(1), async {
            loop {
                if handle.snapshot().runtime_error.contains(task.id.as_str()) {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .expect("profile update rejection should be published");

        assert_eq!(
            inspect
                .get_profile(&profile_id)
                .unwrap()
                .config
                .storage_port,
            original.config.storage_port
        );
        handle.send(AppCommand::ExitApplication).await.unwrap();
        worker.await.unwrap();
    }

    #[tokio::test]
    async fn create_task_preflights_known_profile_port_conflict_before_insert() {
        let (_temp, repository, first_id) = repository();
        let shared_port = repository
            .get_profile(&first_id)
            .unwrap()
            .config
            .storage_port;
        let second_id = ProfileId::legacy(2).unwrap();
        repository
            .upsert_profile(&Profile {
                id: second_id.clone(),
                display_name: "实例 2".into(),
                config: AppConfig {
                    storage_port: shared_port,
                    ..AppConfig::default()
                },
                runtime_status: ProfileRuntimeStatus::Stopped,
                source_config_path: String::new(),
                created_at: String::new(),
                updated_at: String::new(),
            })
            .unwrap();
        let inspect = repository.clone();
        let (service, handle) = open_with_idle_runtime(repository);
        let worker = tokio::spawn(service.run());
        let task_id = TaskId::new("port-conflict-task").unwrap();

        handle
            .send(AppCommand::StartProfile {
                profile_id: first_id,
            })
            .await
            .unwrap();
        handle
            .send(AppCommand::CreateTask {
                task_id: task_id.clone(),
                profile_id: second_id,
                name: "conflict".to_owned(),
                accessions: vec!["A001".to_owned()],
                destination: std::env::temp_dir().to_string_lossy().into_owned(),
            })
            .await
            .unwrap();
        tokio::time::timeout(Duration::from_secs(1), async {
            loop {
                if handle.snapshot().runtime_error.contains("conflicts") {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .expect("known port conflict should be rejected");

        assert!(!inspect.has_task(&task_id).unwrap());
        handle.send(AppCommand::ExitApplication).await.unwrap();
        worker.await.unwrap();
    }

    #[tokio::test]
    async fn exit_waits_for_receiver_shutdown_and_releases_port() {
        let (_temp, repository, profile_id) = repository();
        let (service, handle) = ApplicationService::open(repository.path()).unwrap();
        let worker = tokio::spawn(service.run());
        handle
            .send(AppCommand::StartProfile { profile_id })
            .await
            .unwrap();
        tokio::time::sleep(Duration::from_millis(50)).await;
        let port = handle
            .snapshot()
            .receiver_statuses
            .iter()
            .find(|status| status.state == ReceiverState::Listening)
            .unwrap()
            .port;
        assert!(port > 0);
        handle.send(AppCommand::ExitApplication).await.unwrap();
        worker.await.unwrap();
        let rebound =
            tokio::net::TcpListener::bind(SocketAddr::from((Ipv4Addr::UNSPECIFIED, port)))
                .await
                .unwrap();
        drop(rebound);
        assert!(handle.snapshot().shutting_down);
        assert_eq!(handle.snapshot().active_profiles, 0);
    }

    #[tokio::test]
    async fn retry_keeps_completed_accessions_and_requeues_only_failures() {
        let (_temp, repository, profile_id) = repository();
        let task = repository
            .create_task(
                &profile_id,
                "retry",
                repository.get_profile(&profile_id).unwrap().config,
                vec!["A001".into(), "A002".into()],
                false,
            )
            .unwrap();
        repository
            .record_result(
                &task.id,
                &AccessionResult::blank("A001", dcmget_domain::AccessionStatus::Completed),
            )
            .unwrap();
        repository
            .record_result(
                &task.id,
                &AccessionResult::blank("A002", dcmget_domain::AccessionStatus::Failed),
            )
            .unwrap();
        repository
            .set_task_phase(&task.id, TaskPhase::DownloadRetryable)
            .unwrap();
        let inspect = repository.clone();
        let (service, handle) = open_with_idle_runtime(repository);
        let worker = tokio::spawn(service.run());
        handle
            .send(AppCommand::StartTask {
                task_id: task.id.clone(),
            })
            .await
            .unwrap();
        tokio::time::sleep(Duration::from_millis(30)).await;
        let retried = inspect.get_task(&task.id).unwrap();
        assert_eq!(
            retried
                .results
                .iter()
                .map(|result| result.accession.as_str())
                .collect::<Vec<_>>(),
            ["A001"]
        );
        assert_eq!(
            inspect.next_pending(&task.id).unwrap().as_deref(),
            Some("A002")
        );
        handle.send(AppCommand::ExitApplication).await.unwrap();
        worker.await.unwrap();
    }

    #[tokio::test]
    async fn cancelled_task_stays_cancelled_when_exit_follows_immediately() {
        let (_temp, repository, profile_id) = repository();
        let inspect = repository.clone();
        let (service, handle) = open_with_idle_runtime(repository);
        let worker = tokio::spawn(service.run());
        let task_id = TaskId::new("cancel-exit").unwrap();
        handle
            .send(AppCommand::CreateTask {
                task_id: task_id.clone(),
                profile_id,
                name: "cancel".to_owned(),
                accessions: vec!["A001".to_owned()],
                destination: tempfile::tempdir()
                    .unwrap()
                    .path()
                    .to_string_lossy()
                    .into_owned(),
            })
            .await
            .unwrap();
        tokio::time::sleep(Duration::from_millis(20)).await;
        handle
            .send(AppCommand::CancelTask {
                task_id: task_id.clone(),
            })
            .await
            .unwrap();
        handle.send(AppCommand::ExitApplication).await.unwrap();
        worker.await.unwrap();
        assert_eq!(
            inspect.get_task(&task_id).unwrap().phase,
            TaskPhase::Cancelled
        );
        assert_eq!(
            inspect.get_task_summary(&task_id).unwrap().cancelled_count,
            1
        );
        inspect.delete_task(&task_id).unwrap();
    }

    #[test]
    fn interrupted_cancelling_task_recovers_as_cancelled_not_retryable() {
        let (_temp, repository, profile_id) = repository();
        let task = repository
            .create_task(
                &profile_id,
                "cancelling",
                repository.get_profile(&profile_id).unwrap().config,
                vec!["A001".into(), "A002".into()],
                false,
            )
            .unwrap();
        repository
            .set_task_phase(&task.id, TaskPhase::Cancelling)
            .unwrap();

        let (_service, _handle) = open_with_idle_runtime(repository.clone());

        assert_eq!(
            repository.get_task(&task.id).unwrap().phase,
            TaskPhase::Cancelled
        );
        assert_eq!(
            repository
                .get_task_summary(&task.id)
                .unwrap()
                .cancelled_count,
            2
        );
        assert!(repository.next_pending(&task.id).unwrap().is_none());
    }

    #[tokio::test]
    async fn pause_and_resume_cannot_overwrite_cancelling_phase() {
        let (_temp, repository, profile_id) = repository();
        let task = repository
            .create_task(
                &profile_id,
                "cancelling",
                repository.get_profile(&profile_id).unwrap().config,
                vec!["A001".into()],
                false,
            )
            .unwrap();
        repository
            .set_task_phase(&task.id, TaskPhase::Cancelling)
            .unwrap();
        let state = load_runtime_state(&repository).unwrap();
        let (mut service, _handle) = ApplicationService::build(
            state,
            Some(repository.clone()),
            None,
            Arc::new(IdleRuntimeFactory),
        );
        let control = TaskControl::new();
        control.cancel();
        service.state.tasks.get_mut(&task.id).unwrap().control = Some(control);

        assert!(service.pause_task(&task.id).is_err());
        assert!(service.resume_task(&task.id).await.is_err());
        service
            .apply_internal_result(InternalEvent::TaskStarted {
                task_id: task.id.clone(),
            })
            .unwrap();
        service
            .apply_internal_result(InternalEvent::TaskPaused {
                task_id: task.id.clone(),
            })
            .unwrap();
        assert_eq!(
            repository.get_task(&task.id).unwrap().phase,
            TaskPhase::Cancelling
        );
    }

    #[tokio::test]
    async fn profile_worker_panic_faults_receiver_and_requeues_active_task() {
        let (_temp, repository, profile_id) = repository();
        let inspect = repository.clone();
        let (service, handle) = ApplicationService::from_repository(
            repository,
            None,
            Arc::new(PanicOnTaskRuntimeFactory),
        )
        .unwrap();
        let worker = tokio::spawn(service.run());
        let task_id = TaskId::new("panic-task").unwrap();
        handle
            .send(AppCommand::CreateTask {
                task_id: task_id.clone(),
                profile_id,
                name: "panic".to_owned(),
                accessions: vec!["A001".to_owned()],
                destination: std::env::temp_dir().to_string_lossy().into_owned(),
            })
            .await
            .unwrap();

        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                let snapshot = handle.snapshot();
                let task_retryable = snapshot.tasks.iter().any(|task| {
                    task.task_id == task_id && task.phase == TaskPhase::DownloadRetryable
                });
                let receiver_faulted = snapshot
                    .receiver_statuses
                    .iter()
                    .any(|status| status.state == ReceiverState::Faulted);
                if task_retryable && receiver_faulted {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("worker panic should become a persisted fault");

        assert_eq!(
            inspect.get_task(&task_id).unwrap().phase,
            TaskPhase::DownloadRetryable
        );
        assert!(
            handle
                .snapshot()
                .logs
                .iter()
                .any(|entry| entry.message.contains("后台线程异常退出"))
        );
        handle
            .send(AppCommand::DeleteTask {
                task_id: task_id.clone(),
            })
            .await
            .unwrap();
        tokio::time::timeout(Duration::from_secs(1), async {
            loop {
                if handle
                    .snapshot()
                    .tasks
                    .iter()
                    .all(|task| task.task_id != task_id)
                {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .expect("faulted task control should be cleared for deletion");
        handle.send(AppCommand::ExitApplication).await.unwrap();
        worker.await.unwrap();
    }

    #[tokio::test]
    async fn finish_task_command_cannot_force_unverified_completion() {
        let (_temp, repository, profile_id) = repository();
        let task = repository
            .create_task(
                &profile_id,
                "queued",
                repository.get_profile(&profile_id).unwrap().config,
                vec!["A001".into()],
                false,
            )
            .unwrap();
        let (mut service, _handle) = open_with_idle_runtime(repository.clone());

        let error = service
            .apply_command(AppCommand::FinishTask {
                task_id: task.id.clone(),
            })
            .await
            .unwrap_err();

        assert!(error.to_string().contains("不能手工将任务标记为完成"));
        assert_eq!(
            repository.get_task(&task.id).unwrap().phase,
            TaskPhase::Queued
        );
    }

    #[tokio::test]
    async fn accession_finished_ack_reflects_repository_commit_result() {
        let (_temp, repository, profile_id) = repository();
        let task = repository
            .create_task(
                &profile_id,
                "persist",
                repository.get_profile(&profile_id).unwrap().config,
                vec!["A001".into()],
                false,
            )
            .unwrap();
        let state = load_runtime_state(&repository).unwrap();
        let (mut service, _handle) = ApplicationService::build(
            state,
            Some(repository.clone()),
            None,
            Arc::new(IdleRuntimeFactory),
        );

        let (success, confirmation) = tokio::sync::oneshot::channel();
        service
            .apply_internal_result(InternalEvent::AccessionFinished {
                task_id: task.id.clone(),
                result: AccessionResult::blank("A001", dcmget_domain::AccessionStatus::Completed),
                acknowledgment: success,
            })
            .unwrap();
        assert_eq!(confirmation.await.unwrap(), Ok(()));
        assert_eq!(
            repository
                .get_task_summary(&task.id)
                .unwrap()
                .completed_count,
            1
        );

        let (failure, confirmation) = tokio::sync::oneshot::channel();
        assert!(
            service
                .apply_internal_result(InternalEvent::AccessionFinished {
                    task_id: task.id.clone(),
                    result: AccessionResult::blank(
                        "NOT-IN-TASK",
                        dcmget_domain::AccessionStatus::Completed,
                    ),
                    acknowledgment: failure,
                })
                .is_err()
        );
        assert!(
            confirmation
                .await
                .unwrap()
                .unwrap_err()
                .contains("is not part of task")
        );
    }

    #[test]
    fn completed_phase_requires_repository_to_have_no_pending_or_partial_rows() {
        let (_temp, repository, profile_id) = repository();
        let pending = repository
            .create_task(
                &profile_id,
                "pending",
                repository.get_profile(&profile_id).unwrap().config,
                vec!["A001".into()],
                false,
            )
            .unwrap();
        let partial = repository
            .create_task(
                &profile_id,
                "partial",
                repository.get_profile(&profile_id).unwrap().config,
                vec!["A002".into()],
                false,
            )
            .unwrap();
        repository
            .record_result(
                &partial.id,
                &AccessionResult::blank("A002", dcmget_domain::AccessionStatus::Partial),
            )
            .unwrap();
        let state = load_runtime_state(&repository).unwrap();
        let (mut service, _handle) = ApplicationService::build(
            state,
            Some(repository.clone()),
            None,
            Arc::new(IdleRuntimeFactory),
        );

        service
            .finish_runtime_task(&pending.id, TaskPhase::Completed, "")
            .unwrap();
        service
            .finish_runtime_task(&partial.id, TaskPhase::Completed, "")
            .unwrap();

        for task_id in [&pending.id, &partial.id] {
            let task = repository.get_task(task_id).unwrap();
            assert_eq!(task.phase, TaskPhase::DownloadRetryable);
            assert!(task.error_message.contains("完成校验未通过"));
        }
    }

    #[tokio::test]
    async fn duplicate_detection_remains_linear_for_forty_thousand_accessions() {
        let (_temp, repository, profile_id) = repository();
        let existing = (0..40_000)
            .map(|index| format!("A{index:05}"))
            .collect::<Vec<_>>();
        repository
            .create_task(
                &profile_id,
                "large",
                repository.get_profile(&profile_id).unwrap().config,
                existing.clone(),
                false,
            )
            .unwrap();
        let (service, handle) = open_with_idle_runtime(repository);
        let worker = tokio::spawn(service.run());
        handle
            .send(AppCommand::CreateTask {
                task_id: TaskId::new("large-duplicate").unwrap(),
                profile_id,
                name: "duplicate".to_owned(),
                accessions: existing,
                destination: std::env::temp_dir().to_string_lossy().into_owned(),
            })
            .await
            .unwrap();
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                if handle.snapshot().runtime_error.contains("未完成任务") {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .expect("40k duplicate validation must remain responsive");
        handle.send(AppCommand::ExitApplication).await.unwrap();
        worker.await.unwrap();
    }

    #[tokio::test]
    async fn zero_ports_are_rejected_before_receiver_start() {
        let (_temp, repository, profile_id) = repository();
        let (service, handle) = open_with_idle_runtime(repository);
        let worker = tokio::spawn(service.run());
        let mut profile = handle.snapshot().profiles[0].clone();
        profile.config.storage_port = 0;
        handle
            .send(AppCommand::UpsertProfile {
                profile: Box::new(profile),
            })
            .await
            .unwrap();
        tokio::time::sleep(Duration::from_millis(10)).await;
        assert!(handle.snapshot().runtime_error.contains("storage_port"));
        handle
            .send(AppCommand::StartProfile { profile_id })
            .await
            .unwrap();
        handle.send(AppCommand::ExitApplication).await.unwrap();
        worker.await.unwrap();
    }

    #[test]
    fn unsupported_processing_modes_are_rejected_instead_of_silently_ignored() {
        let config = AppConfig {
            anonymization_enabled: true,
            ..AppConfig::default()
        };
        assert!(
            validate_download_config(&config)
                .unwrap_err()
                .to_string()
                .contains("暂不支持匿名化")
        );

        let config = AppConfig {
            pdi_export_enabled: true,
            ..AppConfig::default()
        };
        assert!(
            validate_download_config(&config)
                .unwrap_err()
                .to_string()
                .contains("暂不支持 PDI")
        );
    }
}
