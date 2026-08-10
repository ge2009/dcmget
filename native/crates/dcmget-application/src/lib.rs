//! UI-independent `DcmGet` application orchestration.

mod process_guard;

pub use process_guard::{ProcessGuard, ProcessGuardError};

use std::{
    collections::HashMap,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

use dcmget_domain::{AppCommand, AppEvent, ProfileId, TaskId};
use parking_lot::RwLock;
use thiserror::Error;
use tokio::sync::{broadcast, mpsc, watch};

const COMMAND_CAPACITY: usize = 256;
const EVENT_CAPACITY: usize = 1_024;
const MAX_PROGRESS_HZ: u64 = 4;

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
}

#[derive(Debug, Clone, Default)]
pub struct ApplicationSnapshot {
    pub active_profiles: usize,
    pub active_tasks: usize,
    pub aggregate_speed_bytes_per_second: u64,
    pub shutting_down: bool,
}

#[derive(Debug, Clone)]
struct RuntimeTask {
    profile_id: ProfileId,
    paused: bool,
}

#[derive(Debug, Default)]
struct RuntimeState {
    profiles: HashMap<ProfileId, bool>,
    tasks: HashMap<TaskId, RuntimeTask>,
    aggregate_speed_bytes_per_second: u64,
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
    state: Arc<RwLock<RuntimeState>>,
    commands: mpsc::Receiver<AppCommand>,
    events: broadcast::Sender<AppEvent>,
    snapshot: watch::Sender<ApplicationSnapshot>,
    shutdown_requested: Arc<AtomicBool>,
}

impl ApplicationService {
    #[must_use]
    pub fn channel() -> (Self, ApplicationHandle) {
        let (command_tx, command_rx) = mpsc::channel(COMMAND_CAPACITY);
        let (event_tx, _) = broadcast::channel(EVENT_CAPACITY);
        let (snapshot_tx, snapshot_rx) = watch::channel(ApplicationSnapshot::default());
        let shutdown_requested = Arc::new(AtomicBool::new(false));
        let service = Self {
            state: Arc::new(RwLock::new(RuntimeState::default())),
            commands: command_rx,
            events: event_tx.clone(),
            snapshot: snapshot_tx,
            shutdown_requested: Arc::clone(&shutdown_requested),
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
        let mut progress_tick =
            tokio::time::interval(Duration::from_millis(1_000 / MAX_PROGRESS_HZ));
        progress_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let mut progress_dirty = false;

        loop {
            tokio::select! {
                _ = progress_tick.tick(), if progress_dirty => {
                    self.publish_snapshot();
                    progress_dirty = false;
                }
                command = self.commands.recv() => {
                    let Some(command) = command else { break };
                    let should_exit = matches!(command, AppCommand::ExitApplication);
                    progress_dirty |= self.apply(command);
                    if should_exit {
                        self.shutdown_requested.store(true, Ordering::Release);
                        self.publish_snapshot();
                        break;
                    }
                }
            }
        }
    }

    fn apply(&self, command: AppCommand) -> bool {
        let mut state = self.state.write();
        match command {
            AppCommand::RegisterProfile { profile_id } => {
                state.profiles.entry(profile_id.clone()).or_insert(false);
                let _ = self.events.send(AppEvent::ProfileRegistered { profile_id });
            }
            AppCommand::StartProfile { profile_id } => {
                if let Some(running) = state.profiles.get_mut(&profile_id) {
                    *running = true;
                    let _ = self.events.send(AppEvent::ProfileStarted { profile_id });
                } else {
                    let _ = self.events.send(AppEvent::CommandRejected {
                        message: ApplicationError::UnknownProfile(profile_id).to_string(),
                    });
                }
            }
            AppCommand::StopProfile { profile_id } => {
                if let Some(running) = state.profiles.get_mut(&profile_id) {
                    *running = false;
                    let _ = self.events.send(AppEvent::ProfileStopped { profile_id });
                }
            }
            AppCommand::CreateTask {
                task_id,
                profile_id,
                ..
            } => {
                if !state.profiles.contains_key(&profile_id) {
                    let _ = self.events.send(AppEvent::CommandRejected {
                        message: ApplicationError::UnknownProfile(profile_id).to_string(),
                    });
                } else if state
                    .tasks
                    .values()
                    .any(|task| task.profile_id == profile_id)
                {
                    let _ = self.events.send(AppEvent::CommandRejected {
                        message: ApplicationError::ProfileBusy(profile_id).to_string(),
                    });
                } else {
                    state.tasks.insert(
                        task_id.clone(),
                        RuntimeTask {
                            profile_id,
                            paused: false,
                        },
                    );
                    let _ = self.events.send(AppEvent::TaskCreated { task_id });
                }
            }
            AppCommand::PauseTask { task_id } => {
                if let Some(task) = state.tasks.get_mut(&task_id) {
                    task.paused = true;
                    let _ = self.events.send(AppEvent::TaskPaused { task_id });
                }
            }
            AppCommand::ResumeTask { task_id } => {
                if let Some(task) = state.tasks.get_mut(&task_id) {
                    task.paused = false;
                    let _ = self.events.send(AppEvent::TaskResumed { task_id });
                }
            }
            AppCommand::FinishTask { task_id } => {
                state.tasks.remove(&task_id);
                let _ = self.events.send(AppEvent::TaskFinished { task_id });
            }
            AppCommand::CancelTask { task_id } => {
                state.tasks.remove(&task_id);
                let _ = self.events.send(AppEvent::TaskCancelled { task_id });
            }
            AppCommand::ReportAggregateSpeed { bytes_per_second } => {
                state.aggregate_speed_bytes_per_second = bytes_per_second;
            }
            AppCommand::ExitApplication => {
                state.tasks.clear();
                for running in state.profiles.values_mut() {
                    *running = false;
                }
                let _ = self.events.send(AppEvent::ApplicationStopping);
            }
        }
        true
    }

    fn publish_snapshot(&self) {
        let state = self.state.read();
        self.snapshot.send_replace(ApplicationSnapshot {
            active_profiles: state.profiles.values().filter(|running| **running).count(),
            active_tasks: state.tasks.len(),
            aggregate_speed_bytes_per_second: state.aggregate_speed_bytes_per_second,
            shutting_down: self.shutdown_requested.load(Ordering::Acquire),
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn profile_allows_only_one_task_and_exit_clears_runtime() {
        let (service, handle) = ApplicationService::channel();
        let worker = tokio::spawn(service.run());
        let profile_id = ProfileId::new("profile-1").unwrap();
        let task_id = TaskId::new("task-1").unwrap();
        handle
            .send(AppCommand::RegisterProfile {
                profile_id: profile_id.clone(),
            })
            .await
            .unwrap();
        handle
            .send(AppCommand::StartProfile {
                profile_id: profile_id.clone(),
            })
            .await
            .unwrap();
        handle
            .send(AppCommand::CreateTask {
                task_id,
                profile_id,
                accessions: vec!["A1".into()],
            })
            .await
            .unwrap();
        handle.send(AppCommand::ExitApplication).await.unwrap();
        worker.await.unwrap();
        let snapshot = handle.snapshot();
        assert!(snapshot.shutting_down);
        assert_eq!(snapshot.active_profiles, 0);
        assert_eq!(snapshot.active_tasks, 0);
    }

    #[tokio::test]
    async fn progress_snapshot_is_coalesced() {
        let (service, handle) = ApplicationService::channel();
        let worker = tokio::spawn(service.run());
        for bytes_per_second in 1..=50 {
            handle
                .send(AppCommand::ReportAggregateSpeed { bytes_per_second })
                .await
                .unwrap();
        }
        tokio::time::sleep(Duration::from_millis(300)).await;
        assert_eq!(handle.snapshot().aggregate_speed_bytes_per_second, 50);
        handle.send(AppCommand::ExitApplication).await.unwrap();
        worker.await.unwrap();
    }

    #[tokio::test]
    async fn task_for_an_unknown_profile_is_rejected() {
        let (service, handle) = ApplicationService::channel();
        let mut events = handle.subscribe();
        let worker = tokio::spawn(service.run());
        handle
            .send(AppCommand::CreateTask {
                task_id: TaskId::new("task-orphan").unwrap(),
                profile_id: ProfileId::new("missing-profile").unwrap(),
                accessions: vec!["A1".into()],
            })
            .await
            .unwrap();
        assert!(matches!(
            events.recv().await.unwrap(),
            AppEvent::CommandRejected { .. }
        ));
        assert_eq!(handle.snapshot().active_tasks, 0);
        handle.send(AppCommand::ExitApplication).await.unwrap();
        worker.await.unwrap();
    }

    #[tokio::test]
    async fn cancellation_has_a_distinct_event() {
        let (service, handle) = ApplicationService::channel();
        let mut events = handle.subscribe();
        let worker = tokio::spawn(service.run());
        let profile_id = ProfileId::new("profile-cancel").unwrap();
        let task_id = TaskId::new("task-cancel").unwrap();
        handle
            .send(AppCommand::RegisterProfile {
                profile_id: profile_id.clone(),
            })
            .await
            .unwrap();
        assert!(matches!(
            events.recv().await.unwrap(),
            AppEvent::ProfileRegistered { .. }
        ));
        handle
            .send(AppCommand::CreateTask {
                task_id: task_id.clone(),
                profile_id,
                accessions: vec!["A1".into()],
            })
            .await
            .unwrap();
        assert!(matches!(
            events.recv().await.unwrap(),
            AppEvent::TaskCreated { .. }
        ));
        handle
            .send(AppCommand::CancelTask {
                task_id: task_id.clone(),
            })
            .await
            .unwrap();
        assert_eq!(
            events.recv().await.unwrap(),
            AppEvent::TaskCancelled { task_id }
        );
        handle.send(AppCommand::ExitApplication).await.unwrap();
        worker.await.unwrap();
    }
}
