use std::collections::HashSet;
use std::net::{Ipv4Addr, SocketAddr};
use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};

use dcmget_dicom::{
    CStoreCommand, CancellationToken, DicomEndpoint, FileStore, LateStoreDecision, LateStorePolicy,
    LateStoreTracker, MoveAttemptResult, MoveStatusClass, QuarantineTarget, ReceiveDisposition,
    ReceiveRoute, StorageScpConfig, StorageScpEvent, StorageScpHandle, StorageScpService,
    StoreRequest, StoreRequestResolveError, StoreRequestResolver, StudyMoveScu,
};
use dcmget_domain::{
    AccessionResult, AccessionStatus, LogLevel, Profile, ProfileId, ResultVerificationStatus, Task,
    TaskId, TaskPhase,
};
use thiserror::Error;
use tokio::sync::{broadcast, mpsc, oneshot, watch};
use tokio::task::JoinHandle;
use tokio::time::MissedTickBehavior;

const RECEIVER_HEALTH_INTERVAL: Duration = Duration::from_millis(100);
const CANCEL_SETTLE_TIMEOUT: Duration = Duration::from_secs(3);
const PROGRESS_INTERVAL: Duration = Duration::from_millis(250);

use crate::archive::{ArchiveBatchResult, archive_received_files, prepare_staging_directory};

#[derive(Debug, Error)]
pub(crate) enum TransferError {
    #[error("invalid task destination: {0}")]
    InvalidDestination(String),
    #[error("file operation failed for {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("Storage SCP failed: {0}")]
    Receiver(String),
    #[error("DICOM receive event stream failed: {0}")]
    EventStream(String),
    #[error("DICOM receive route failed: {0}")]
    Route(String),
    #[error("DICOM archive worker failed: {0}")]
    ArchiveWorker(String),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ControlState {
    Running,
    Paused,
    Cancelled,
    ProfileStopping,
}

#[derive(Clone)]
pub(crate) struct TaskControl {
    state: watch::Sender<ControlState>,
    cancellation: CancellationToken,
}

impl TaskControl {
    pub(crate) fn new() -> Self {
        let (state, _) = watch::channel(ControlState::Running);
        Self {
            state,
            cancellation: CancellationToken::new(),
        }
    }

    pub(crate) fn pause(&self) {
        if self.current() == ControlState::Running {
            self.state.send_replace(ControlState::Paused);
        }
    }

    pub(crate) fn resume(&self) {
        if self.current() == ControlState::Paused {
            self.state.send_replace(ControlState::Running);
        }
    }

    pub(crate) fn cancel(&self) {
        self.state.send_replace(ControlState::Cancelled);
        self.cancellation.cancel();
    }

    pub(crate) fn stop_profile(&self) {
        self.state.send_replace(ControlState::ProfileStopping);
        self.cancellation.cancel();
    }

    pub(crate) fn current(&self) -> ControlState {
        *self.state.borrow()
    }

    async fn wait_until_running(&self) -> ControlState {
        let mut receiver = self.state.subscribe();
        loop {
            let current = *receiver.borrow_and_update();
            if current != ControlState::Paused {
                return current;
            }
            if receiver.changed().await.is_err() {
                return ControlState::Cancelled;
            }
        }
    }
}

pub(crate) enum ProfileWork {
    Run {
        task: Box<Task>,
        control: TaskControl,
    },
    Stop,
}

#[derive(Debug)]
pub(crate) enum InternalEvent {
    ReceiverReady {
        profile_id: ProfileId,
        address: SocketAddr,
    },
    ReceiverStopped {
        profile_id: ProfileId,
    },
    ReceiverFaulted {
        profile_id: ProfileId,
        message: String,
    },
    AssociationCount {
        profile_id: ProfileId,
        count: u16,
    },
    TaskStarted {
        task_id: TaskId,
    },
    TaskPaused {
        task_id: TaskId,
    },
    AccessionProgress {
        task_id: TaskId,
        result: AccessionResult,
    },
    AccessionFinished {
        task_id: TaskId,
        result: AccessionResult,
        acknowledgment: oneshot::Sender<Result<(), String>>,
    },
    TaskFinished {
        task_id: TaskId,
        phase: TaskPhase,
        error: String,
    },
    Log {
        level: LogLevel,
        source: String,
        task_id: Option<TaskId>,
        message: String,
    },
}

pub(crate) trait ProfileRuntimeFactory: Send + Sync {
    fn spawn(
        &self,
        profile: Profile,
        commands: mpsc::Receiver<ProfileWork>,
        events: mpsc::Sender<InternalEvent>,
    ) -> JoinHandle<()>;
}

#[derive(Default)]
pub(crate) struct NativeProfileRuntimeFactory;

impl ProfileRuntimeFactory for NativeProfileRuntimeFactory {
    fn spawn(
        &self,
        profile: Profile,
        commands: mpsc::Receiver<ProfileWork>,
        events: mpsc::Sender<InternalEvent>,
    ) -> JoinHandle<()> {
        tokio::spawn(run_profile(profile, commands, events))
    }
}

async fn run_profile(
    profile: Profile,
    mut commands: mpsc::Receiver<ProfileWork>,
    events: mpsc::Sender<InternalEvent>,
) {
    let profile_id = profile.id.clone();
    let quarantine_root = match prepare_destination(&profile.config.dicom_destination_folder).await
    {
        Ok(destination) => destination,
        Err(error) => {
            let _ = events
                .send(InternalEvent::ReceiverFaulted {
                    profile_id,
                    message: error.to_string(),
                })
                .await;
            return;
        }
    };
    let resolver = ActiveRouteResolver::new(profile_id.to_string(), quarantine_root);
    let mut receiver = match ProfileReceiver::start(&profile, &resolver).await {
        Ok(receiver) => receiver,
        Err(error) => {
            let _ = events
                .send(InternalEvent::ReceiverFaulted {
                    profile_id,
                    message: error.to_string(),
                })
                .await;
            return;
        }
    };
    let _ = events
        .send(InternalEvent::ReceiverReady {
            profile_id: profile_id.clone(),
            address: receiver.local_address(),
        })
        .await;

    while let Some(command) = commands.recv().await {
        match command {
            ProfileWork::Run { task, control } => {
                receiver.discard_idle_events();
                let task_id = task.id.clone();
                let _ = events
                    .send(InternalEvent::TaskStarted {
                        task_id: task_id.clone(),
                    })
                    .await;
                let outcome =
                    run_task(&profile, &task, &control, &resolver, &mut receiver, &events).await;
                let (phase, error, stop_after_task) = match outcome {
                    Ok(outcome) => (outcome.phase, outcome.error, outcome.stop_receiver),
                    Err(error) => (TaskPhase::DownloadRetryable, error.to_string(), true),
                };
                let _ = events
                    .send(InternalEvent::TaskFinished {
                        task_id,
                        phase,
                        error,
                    })
                    .await;
                if stop_after_task {
                    break;
                }
            }
            ProfileWork::Stop => break,
        }
    }

    if let Err(error) = receiver.shutdown().await {
        let _ = events
            .send(InternalEvent::Log {
                level: LogLevel::Error,
                source: "receiver".to_owned(),
                task_id: None,
                message: error.to_string(),
            })
            .await;
    }
    resolver.clear();
    let _ = events
        .send(InternalEvent::ReceiverStopped { profile_id })
        .await;
}

struct ProfileReceiver {
    handle: Option<StorageScpHandle>,
    events: broadcast::Receiver<StorageScpEvent>,
}

impl ProfileReceiver {
    async fn start(
        profile: &Profile,
        resolver: &ActiveRouteResolver,
    ) -> Result<Self, TransferError> {
        let bind_address = SocketAddr::from((Ipv4Addr::UNSPECIFIED, profile.config.storage_port));
        let handle = StorageScpService::start(
            StorageScpConfig::new(bind_address, profile.config.storage_ae_title.clone()),
            FileStore::new(),
            resolver.clone(),
        )
        .await
        .map_err(|error| TransferError::Receiver(error.to_string()))?;
        let events = handle.subscribe();
        Ok(Self {
            handle: Some(handle),
            events,
        })
    }

    fn handle(&self) -> &StorageScpHandle {
        self.handle
            .as_ref()
            .expect("Profile receiver handle must exist while it is running")
    }

    fn local_address(&self) -> SocketAddr {
        self.handle().local_address()
    }

    fn parts(&mut self) -> (&StorageScpHandle, &mut broadcast::Receiver<StorageScpEvent>) {
        let Self { handle, events } = self;
        (
            handle
                .as_ref()
                .expect("Profile receiver handle must exist while it is running"),
            events,
        )
    }

    fn discard_idle_events(&mut self) {
        discard_idle_events(&mut self.events);
    }

    async fn quiesce(&mut self, resolver: &ActiveRouteResolver) -> Result<(), TransferError> {
        let handle = self
            .handle
            .take()
            .expect("Profile receiver handle must exist before shutdown");
        let result = handle
            .shutdown()
            .await
            .map_err(|error| TransferError::Receiver(error.to_string()));
        resolver.clear();
        result
    }

    async fn start_stopped(
        &mut self,
        profile: &Profile,
        resolver: &ActiveRouteResolver,
    ) -> Result<(), TransferError> {
        debug_assert!(self.handle.is_none());
        *self = Self::start(profile, resolver).await?;
        self.discard_idle_events();
        Ok(())
    }

    async fn shutdown(mut self) -> Result<(), TransferError> {
        let Some(handle) = self.handle.take() else {
            return Ok(());
        };
        handle
            .shutdown()
            .await
            .map_err(|error| TransferError::Receiver(error.to_string()))
    }
}

struct TaskOutcome {
    phase: TaskPhase,
    error: String,
    stop_receiver: bool,
}

#[allow(clippy::too_many_arguments, clippy::too_many_lines)]
async fn run_task(
    profile: &Profile,
    task: &Task,
    control: &TaskControl,
    resolver: &ActiveRouteResolver,
    receiver: &mut ProfileReceiver,
    internal_events: &mpsc::Sender<InternalEvent>,
) -> Result<TaskOutcome, TransferError> {
    let destination = prepare_destination(&task.config.dicom_destination_folder).await?;
    let move_scu = StudyMoveScu::default();
    let mut succeeded = 0_u64;
    let mut retryable = 0_u64;
    let mut failed = 0_u64;

    let finished = task
        .results
        .iter()
        .filter(|result| {
            matches!(
                result.status,
                AccessionStatus::Completed | AccessionStatus::NoData
            )
        })
        .map(|result| result.accession.as_str())
        .collect::<HashSet<_>>();
    let pending_accessions = task
        .accessions
        .iter()
        .filter(|accession| !finished.contains(accession.as_str()))
        .collect::<Vec<_>>();
    for (accession_index, accession) in pending_accessions.iter().enumerate() {
        let accession = accession.as_str();
        let has_more_accessions = accession_index + 1 < pending_accessions.len();
        if control.current() == ControlState::Paused {
            let _ = internal_events
                .send(InternalEvent::TaskPaused {
                    task_id: task.id.clone(),
                })
                .await;
        }
        match control.wait_until_running().await {
            ControlState::Paused => unreachable!("wait_until_running never returns paused"),
            ControlState::Cancelled => {
                return Ok(TaskOutcome {
                    phase: TaskPhase::Cancelled,
                    error: String::new(),
                    stop_receiver: true,
                });
            }
            ControlState::ProfileStopping => {
                return Ok(TaskOutcome {
                    phase: TaskPhase::DownloadRetryable,
                    error: "接收器已停止，可稍后继续任务".to_owned(),
                    stop_receiver: true,
                });
            }
            ControlState::Running => {}
        }
        let staging = prepare_staging_directory(&destination, accession)
            .await
            .map_err(|error| TransferError::ArchiveWorker(error.to_string()))?;
        let relative_directory = staging.relative_path().to_path_buf();
        let route = ReceiveRoute {
            profile_id: profile.id.to_string(),
            task_id: task.id.to_string(),
            accession_number: accession.to_owned(),
            destination_root: destination.clone(),
        };
        let route_lease = resolver.activate(RouteTarget {
            route: route.clone(),
            relative_directory: relative_directory.clone(),
            expected_move_originator_ae: task.config.calling_ae_title.trim().to_owned(),
            expected_move_originator_message_id: 1,
        })?;
        let request = dcmget_dicom::MoveRequest::study_by_accession(
            profile.id.to_string(),
            task.id.to_string(),
            accession,
            DicomEndpoint {
                host: task.config.pacs_server_ip.trim().to_owned(),
                port: task.config.pacs_server_port,
            },
            task.config.calling_ae_title.clone(),
            task.config.pacs_ae_title.clone(),
            task.config.storage_ae_title.clone(),
        );
        let started = Instant::now();
        let mut stats = ReceiveStats::default();
        let mut attempt_count = 0_u32;
        let mut move_result = None;
        let max_attempts = u32::from(task.config.auto_retry_attempts).saturating_add(1);

        while attempt_count < max_attempts {
            attempt_count = attempt_count.saturating_add(1);
            let (receiver_handle, receiver_events) = receiver.parts();
            move_result = execute_move_collecting(
                &move_scu,
                &request,
                &route,
                receiver_handle,
                receiver_events,
                control,
                &mut stats,
                internal_events,
                &task.id,
                accession,
                started,
            )
            .await?;
            let Some(result) = move_result.as_ref() else {
                drop(route_lease);
                return Ok(cancelled_or_stopped(control));
            };
            if !should_retry(result, &stats) || attempt_count >= max_attempts {
                break;
            }
            let delay = Duration::from_secs(u64::from(task.config.auto_retry_backoff_seconds));
            tokio::select! {
                () = control.cancellation.cancelled() => {
                    drop(route_lease);
                    return Ok(cancelled_or_stopped(control));
                }
                () = tokio::time::sleep(delay) => {}
            }
        }

        let Some(mut move_result) = move_result else {
            drop(route_lease);
            return Ok(cancelled_or_stopped(control));
        };
        let expect_files = expects_files(&move_result, &stats);
        let expected_completed_operations = move_result.counters.completed.map(u64::from);
        let (receiver_handle, receiver_events) = receiver.parts();
        let Some(drain) = drain_late_stores(
            expect_files,
            expected_completed_operations,
            &route,
            receiver_handle,
            receiver_events,
            control,
            &mut stats,
            internal_events,
            &profile.id,
            &task.id,
        )
        .await?
        else {
            drop(route_lease);
            return Ok(cancelled_or_stopped(control));
        };
        if drain == DrainOutcome::RestartReceiver {
            // The completed count can become trustworthy before a PACS closes
            // its storage association. Stop every old writer and account for
            // events queued during shutdown before publishing out of staging.
            receiver.quiesce(resolver).await?;
            collect_quiesced_events(
                &mut receiver.events,
                &route,
                &mut stats,
                internal_events,
                &task.id,
            )?;
            let _ = internal_events.try_send(InternalEvent::AssociationCount {
                profile_id: profile.id.clone(),
                count: 0,
            });
        }
        move_result.locally_received_operations = u64_to_u32(stats.successful_store_operations);
        move_result.locally_unique_sop_instances = usize_to_u32(stats.unique_sop_instances.len());
        if drain != DrainOutcome::TimedOut {
            let archive = archive_received_files(
                &destination,
                staging.path(),
                accession,
                &task.config.directory_template,
                &stats.archived_files,
            )
            .await
            .map_err(|error| TransferError::ArchiveWorker(error.to_string()))?;
            for message in archive.failures() {
                let _ = internal_events
                    .send(InternalEvent::Log {
                        level: LogLevel::Error,
                        source: "archive".to_owned(),
                        task_id: Some(task.id.clone()),
                        message: message.clone(),
                    })
                    .await;
            }
            stats.apply_archive(archive);
        }
        let result = build_result(
            accession,
            &destination,
            &move_result,
            &stats,
            drain,
            attempt_count,
            started.elapsed(),
        );
        match result.status {
            AccessionStatus::Completed | AccessionStatus::NoData => {
                succeeded = succeeded.saturating_add(1);
            }
            AccessionStatus::Partial => retryable = retryable.saturating_add(1),
            AccessionStatus::Failed => failed = failed.saturating_add(1),
            _ => {}
        }
        if let Err(error) = persist_accession_result(internal_events, &task.id, result).await {
            drop(route_lease);
            return Ok(TaskOutcome {
                phase: TaskPhase::DownloadRetryable,
                error,
                stop_receiver: true,
            });
        }
        if drain == DrainOutcome::TimedOut {
            drop(route_lease);
            return Ok(TaskOutcome {
                phase: TaskPhase::DownloadRetryable,
                error: "等待迟到影像超时，为防止串入下一检查号已暂停任务".to_owned(),
                stop_receiver: true,
            });
        }
        if drain == DrainOutcome::RestartReceiver {
            // Retain the route until every old association has been aborted by
            // receiver shutdown. Clearing it earlier could attribute a delayed
            // C-STORE to the next accession because many PACS reuse message ID 1.
            drop(route_lease);
            if has_more_accessions {
                receiver.start_stopped(profile, resolver).await?;
                let _ = internal_events
                    .send(InternalEvent::Log {
                        level: LogLevel::Info,
                        source: "receiver".to_owned(),
                        task_id: Some(task.id.clone()),
                        message: "PACS 保持接收关联长连接，已安全重启接收器后继续下一检查号"
                            .to_owned(),
                    })
                    .await;
                continue;
            }
            let phase = terminal_task_phase(succeeded, retryable, failed);
            let error = task_outcome_error(retryable, failed);
            return Ok(TaskOutcome {
                phase,
                error,
                stop_receiver: true,
            });
        }
        route_lease.release();

        if control.current() == ControlState::Paused {
            let _ = internal_events
                .send(InternalEvent::TaskPaused {
                    task_id: task.id.clone(),
                })
                .await;
        }
    }

    let phase = terminal_task_phase(succeeded, retryable, failed);
    let error = task_outcome_error(retryable, failed);
    Ok(TaskOutcome {
        phase,
        error,
        stop_receiver: false,
    })
}

async fn persist_accession_result(
    internal_events: &mpsc::Sender<InternalEvent>,
    task_id: &TaskId,
    result: AccessionResult,
) -> Result<(), String> {
    let (acknowledgment, confirmation) = oneshot::channel();
    internal_events
        .send(InternalEvent::AccessionFinished {
            task_id: task_id.clone(),
            result,
            acknowledgment,
        })
        .await
        .map_err(|_| "无法提交检查号结果，应用后台已经停止".to_owned())?;
    confirmation
        .await
        .map_err(|_| "检查号结果持久化确认通道已关闭".to_owned())?
        .map_err(|error| format!("检查号结果持久化失败：{error}"))
}

fn task_outcome_error(retryable: u64, failed: u64) -> String {
    if failed > 0 || retryable > 0 {
        format!("{failed} 个失败，{retryable} 个部分成功，可重试")
    } else {
        String::new()
    }
}

fn terminal_task_phase(succeeded: u64, retryable: u64, failed: u64) -> TaskPhase {
    if failed > 0 && succeeded == 0 && retryable == 0 {
        TaskPhase::Failed
    } else if failed > 0 || retryable > 0 {
        TaskPhase::DownloadRetryable
    } else {
        TaskPhase::Completed
    }
}

fn cancelled_or_stopped(control: &TaskControl) -> TaskOutcome {
    match control.current() {
        ControlState::ProfileStopping => TaskOutcome {
            phase: TaskPhase::DownloadRetryable,
            error: "接收器已停止，可稍后继续任务".to_owned(),
            stop_receiver: true,
        },
        _ => TaskOutcome {
            phase: TaskPhase::Cancelled,
            error: String::new(),
            stop_receiver: true,
        },
    }
}

#[allow(clippy::too_many_arguments)]
async fn execute_move_collecting(
    move_scu: &StudyMoveScu,
    request: &dcmget_dicom::MoveRequest,
    route: &ReceiveRoute,
    receiver: &StorageScpHandle,
    events: &mut broadcast::Receiver<StorageScpEvent>,
    control: &TaskControl,
    stats: &mut ReceiveStats,
    internal_events: &mpsc::Sender<InternalEvent>,
    task_id: &TaskId,
    accession: &str,
    started: Instant,
) -> Result<Option<MoveAttemptResult>, TransferError> {
    let mut move_future = Box::pin(move_scu.execute(request, &control.cancellation));
    let mut health = tokio::time::interval(RECEIVER_HEALTH_INTERVAL);
    health.set_missed_tick_behavior(MissedTickBehavior::Skip);
    let mut last_progress = Instant::now()
        .checked_sub(PROGRESS_INTERVAL)
        .unwrap_or_else(Instant::now);
    loop {
        tokio::select! {
            result = &mut move_future => return Ok(Some(result)),
            () = control.cancellation.cancelled() => {
                let _ = tokio::time::timeout(CANCEL_SETTLE_TIMEOUT, &mut move_future).await;
                return Ok(None);
            }
            event = events.recv() => match event {
                Ok(event) => {
                    let changed = stats.collect(event, route, internal_events, task_id)?;
                    if changed && last_progress.elapsed() >= PROGRESS_INTERVAL {
                        last_progress = Instant::now();
                        let progress = stats.progress(accession, started.elapsed());
                        let _ = internal_events.try_send(InternalEvent::AccessionProgress {
                            task_id: task_id.clone(), result: progress,
                        });
                    }
                }
                Err(broadcast::error::RecvError::Lagged(count)) => {
                    control.cancellation.cancel();
                    let _ = tokio::time::timeout(CANCEL_SETTLE_TIMEOUT, &mut move_future).await;
                    return Err(TransferError::EventStream(format!("lagged by {count} event(s)")));
                }
                Err(broadcast::error::RecvError::Closed) => {
                    control.cancellation.cancel();
                    let _ = tokio::time::timeout(CANCEL_SETTLE_TIMEOUT, &mut move_future).await;
                    return Err(TransferError::EventStream("closed during C-MOVE".to_owned()));
                }
            },
            _ = health.tick() => {
                if !receiver.is_ready() {
                    control.cancellation.cancel();
                    let _ = tokio::time::timeout(CANCEL_SETTLE_TIMEOUT, &mut move_future).await;
                    return Err(TransferError::Receiver("stopped during C-MOVE".to_owned()));
                }
            }
        }
    }
}

#[allow(clippy::too_many_arguments)]
async fn drain_late_stores(
    expect_files: bool,
    expected_completed_operations: Option<u64>,
    route: &ReceiveRoute,
    receiver: &StorageScpHandle,
    events: &mut broadcast::Receiver<StorageScpEvent>,
    control: &TaskControl,
    stats: &mut ReceiveStats,
    internal_events: &mpsc::Sender<InternalEvent>,
    profile_id: &ProfileId,
    task_id: &TaskId,
) -> Result<Option<DrainOutcome>, TransferError> {
    let policy = LateStorePolicy::default();
    let started = Instant::now();
    let mut tracker = LateStoreTracker::new(policy, expect_files);
    if stats.store_activity_seen {
        tracker.observe_completed_store(Duration::ZERO);
    }
    loop {
        if !receiver.is_ready() {
            return Err(TransferError::Receiver(
                "stopped while late C-STOREs were draining".to_owned(),
            ));
        }
        let elapsed = started.elapsed();
        if elapsed >= policy.maximum_wait && !stats.active_associations.is_empty() {
            return Ok(Some(DrainOutcome::TimedOut));
        }
        let delay = match tracker.decision(elapsed, &control.cancellation) {
            LateStoreDecision::Cancelled => return Ok(None),
            LateStoreDecision::TimedOut => return Ok(Some(DrainOutcome::TimedOut)),
            LateStoreDecision::Drained => {
                if let Some(outcome) = completed_drain_outcome(
                    expected_completed_operations,
                    stats.successful_store_operations,
                    stats.active_associations.is_empty(),
                ) {
                    return Ok(Some(outcome));
                }
                policy.poll_interval
            }
            LateStoreDecision::Wait(delay) => delay,
        };
        tokio::select! {
            () = control.cancellation.cancelled() => return Ok(None),
            () = tokio::time::sleep(delay) => {}
            event = events.recv() => match event {
                Ok(event) => {
                    if stats.collect(event, route, internal_events, task_id)? {
                        tracker.observe_completed_store(started.elapsed());
                    }
                    let _ = internal_events.try_send(InternalEvent::AssociationCount {
                        profile_id: profile_id.clone(),
                        count: u16::try_from(stats.active_associations.len()).unwrap_or(u16::MAX),
                    });
                }
                Err(broadcast::error::RecvError::Lagged(count)) => {
                    return Err(TransferError::EventStream(format!("late-store drain lagged by {count} event(s)")));
                }
                Err(broadcast::error::RecvError::Closed) => {
                    return Err(TransferError::EventStream("closed during late-store drain".to_owned()));
                }
            }
        }
    }
}

#[derive(Debug, Clone)]
struct RouteTarget {
    route: ReceiveRoute,
    relative_directory: PathBuf,
    expected_move_originator_ae: String,
    expected_move_originator_message_id: u16,
}

impl RouteTarget {
    fn quarantine_target(&self) -> QuarantineTarget {
        QuarantineTarget {
            profile_id: self.route.profile_id.clone(),
            destination_root: self.route.destination_root.clone(),
            active_task_id: Some(self.route.task_id.clone()),
        }
    }
}

#[derive(Clone)]
struct ActiveRouteResolver {
    active: Arc<RwLock<Option<RouteTarget>>>,
    profile_quarantine: QuarantineTarget,
}

impl ActiveRouteResolver {
    fn new(profile_id: String, destination_root: PathBuf) -> Self {
        Self {
            active: Arc::new(RwLock::new(None)),
            profile_quarantine: QuarantineTarget {
                profile_id,
                destination_root,
                active_task_id: None,
            },
        }
    }

    fn activate(&self, target: RouteTarget) -> Result<RouteLease, TransferError> {
        let mut active = self
            .active
            .write()
            .map_err(|_| TransferError::Route("active route lock was poisoned".to_owned()))?;
        if active.is_some() {
            return Err(TransferError::Route(
                "a receive route is already active for this Profile".to_owned(),
            ));
        }
        *active = Some(target);
        Ok(RouteLease {
            resolver: self.clone(),
        })
    }

    fn clear(&self) {
        let mut active = self
            .active
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        *active = None;
    }
}

impl StoreRequestResolver for ActiveRouteResolver {
    fn resolve(
        &self,
        command: &CStoreCommand,
        transfer_syntax_uid: &str,
    ) -> Result<StoreRequest, StoreRequestResolveError> {
        let target = self
            .active
            .read()
            .map_err(|_| StoreRequestResolveError {
                message: "active receive route lock was poisoned".to_owned(),
                quarantine_target: self.profile_quarantine.clone(),
            })?
            .clone()
            .ok_or_else(|| StoreRequestResolveError {
                message: "no C-MOVE receive route is active".to_owned(),
                quarantine_target: self.profile_quarantine.clone(),
            })?;
        let quarantine_target = target.quarantine_target();
        if command
            .move_originator_ae_title
            .as_deref()
            .is_some_and(|value| value.trim() != target.expected_move_originator_ae)
        {
            return Err(StoreRequestResolveError {
                message: "C-STORE Move Originator AE does not match the active task".to_owned(),
                quarantine_target: quarantine_target.clone(),
            });
        }
        if command
            .move_originator_message_id
            .is_some_and(|value| value != target.expected_move_originator_message_id)
        {
            return Err(StoreRequestResolveError {
                message: "C-STORE Move Originator Message ID does not match the active task"
                    .to_owned(),
                quarantine_target,
            });
        }
        Ok(StoreRequest {
            route: target.route,
            relative_directory: target.relative_directory,
            sop_class_uid: command.sop_class_uid.clone(),
            sop_instance_uid: command.sop_instance_uid.clone(),
            transfer_syntax_uid: transfer_syntax_uid.to_owned(),
        })
    }

    fn quarantine_target(&self) -> QuarantineTarget {
        self.active
            .read()
            .ok()
            .and_then(|active| active.as_ref().map(RouteTarget::quarantine_target))
            .unwrap_or_else(|| self.profile_quarantine.clone())
    }
}

struct RouteLease {
    resolver: ActiveRouteResolver,
}

impl RouteLease {
    fn release(self) {
        self.resolver.clear();
    }
}

#[derive(Debug, Default)]
struct ReceiveStats {
    successful_store_operations: u64,
    unique_sop_instances: HashSet<String>,
    archived_files: HashSet<PathBuf>,
    published: u64,
    existing_skipped: u64,
    conflicts: u64,
    store_failures: u64,
    quarantined: u64,
    quarantined_files: HashSet<PathBuf>,
    association_failures: u64,
    archive_failures: u64,
    received_bytes: u64,
    store_activity_seen: bool,
    active_associations: HashSet<SocketAddr>,
}

impl ReceiveStats {
    fn apply_archive(&mut self, archive: ArchiveBatchResult) {
        let (archived_files, failures, conflicts) = archive.into_parts();
        self.archived_files = archived_files;
        self.conflicts = self.conflicts.saturating_add(conflicts);
        self.archive_failures = usize_to_u64(failures.len());
    }

    fn collect(
        &mut self,
        event: StorageScpEvent,
        expected_route: &ReceiveRoute,
        internal_events: &mpsc::Sender<InternalEvent>,
        task_id: &TaskId,
    ) -> Result<bool, TransferError> {
        match event {
            StorageScpEvent::Ready { .. } | StorageScpEvent::EchoCompleted { .. } => Ok(false),
            StorageScpEvent::AssociationOpened { peer } => {
                self.active_associations.insert(peer);
                Ok(false)
            }
            StorageScpEvent::AssociationClosed { peer } => {
                self.active_associations.remove(&peer);
                Ok(false)
            }
            StorageScpEvent::StoreCompleted {
                request, outcome, ..
            } => {
                if request.route != *expected_route {
                    return Err(TransferError::Route(
                        "received a C-STORE event for another accession".to_owned(),
                    ));
                }
                if outcome.disposition == ReceiveDisposition::Quarantined {
                    return Err(TransferError::Route(
                        "quarantined payload was incorrectly reported as a completed store"
                            .to_owned(),
                    ));
                }
                self.unique_sop_instances
                    .insert(outcome.sop_instance_uid.clone());
                self.archived_files.insert(outcome.path.clone());
                self.received_bytes = self.received_bytes.saturating_add(outcome.file_bytes);
                match outcome.disposition {
                    ReceiveDisposition::Published => {
                        self.successful_store_operations =
                            self.successful_store_operations.saturating_add(1);
                        self.published = self.published.saturating_add(1);
                    }
                    ReceiveDisposition::ExistingSkipped => {
                        self.successful_store_operations =
                            self.successful_store_operations.saturating_add(1);
                        self.existing_skipped = self.existing_skipped.saturating_add(1);
                    }
                    ReceiveDisposition::ConflictPreserved => {
                        self.conflicts = self.conflicts.saturating_add(1);
                    }
                    ReceiveDisposition::Quarantined => unreachable!(
                        "quarantine disposition was rejected before normal store accounting"
                    ),
                }
                self.store_activity_seen = true;
                Ok(true)
            }
            StorageScpEvent::StoreFailed {
                active_task_id,
                message,
                quarantined,
                ..
            } => {
                if let Some(quarantined) = quarantined.as_ref() {
                    if quarantined.profile_id != expected_route.profile_id {
                        return Err(TransferError::Route(
                            "received a quarantine event for another Profile".to_owned(),
                        ));
                    }
                    if quarantined.active_task_id != active_task_id {
                        return Err(TransferError::Route(
                            "quarantine event task correlation was inconsistent".to_owned(),
                        ));
                    }
                }
                if active_task_id.as_deref() != Some(expected_route.task_id.as_str()) {
                    // A Profile-level quarantine is deliberately unassigned.
                    // Do not let an event queued between accessions make the
                    // next task look partial.
                    return Ok(false);
                }
                self.store_failures = self.store_failures.saturating_add(1);
                self.store_activity_seen = true;
                if let Some(quarantined) = quarantined {
                    self.quarantined = self.quarantined.saturating_add(1);
                    self.received_bytes = self
                        .received_bytes
                        .saturating_add(quarantined.payload.file_bytes);
                    self.quarantined_files.insert(quarantined.payload.path);
                }
                let _ = internal_events.try_send(InternalEvent::Log {
                    level: LogLevel::Error,
                    source: "receiver".to_owned(),
                    task_id: Some(task_id.clone()),
                    message,
                });
                Ok(true)
            }
            StorageScpEvent::AssociationFailed { message, .. } => {
                self.association_failures = self.association_failures.saturating_add(1);
                let _ = internal_events.try_send(InternalEvent::Log {
                    level: LogLevel::Error,
                    source: "receiver".to_owned(),
                    task_id: Some(task_id.clone()),
                    message,
                });
                Ok(false)
            }
        }
    }

    fn progress(&self, accession: &str, elapsed: Duration) -> AccessionResult {
        let mut result = AccessionResult::blank(accession, AccessionStatus::Downloading);
        result.file_count = usize_to_u64(self.unique_sop_instances.len());
        result.received_bytes = self.received_bytes;
        result.speed_bytes_per_second = speed(self.received_bytes, elapsed);
        let mut reported_files = self.archived_files.clone();
        reported_files.extend(self.quarantined_files.iter().cloned());
        result.archived_files = sorted_paths(&reported_files);
        result.new_file_count = self.published;
        result.existing_skipped_count = self.existing_skipped;
        result.conflict_preserved_count = self.conflicts;
        result
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
enum DrainOutcome {
    Drained,
    RestartReceiver,
    TimedOut,
}

fn completed_drain_outcome(
    expected_completed_operations: Option<u64>,
    successful_store_operations: u64,
    associations_closed: bool,
) -> Option<DrainOutcome> {
    if associations_closed {
        Some(DrainOutcome::Drained)
    } else if expected_completed_operations == Some(successful_store_operations) {
        Some(DrainOutcome::RestartReceiver)
    } else {
        None
    }
}

fn build_result(
    accession: &str,
    output_directory: &Path,
    move_result: &MoveAttemptResult,
    stats: &ReceiveStats,
    drain: DrainOutcome,
    attempt_count: u32,
    elapsed: Duration,
) -> AccessionResult {
    let unique = usize_to_u64(stats.unique_sop_instances.len());
    let counters_clean = move_result.counters.remaining.unwrap_or(0) == 0
        && move_result.counters.failed.unwrap_or(0) == 0
        && move_result.counters.warning.unwrap_or(0) == 0;
    let receiver_clean = stats.store_failures == 0
        && stats.association_failures == 0
        && stats.archive_failures == 0
        && stats.conflicts == 0
        && drain != DrainOutcome::TimedOut;
    let move_clean = move_result.association_failure.is_none()
        && move_result
            .final_status
            .is_some_and(|status| status.class == MoveStatusClass::Success)
        && counters_clean;
    let no_data =
        move_result.counters.completed == Some(0) && unique == 0 && move_clean && receiver_clean;
    let local_count_matches = move_result
        .counters
        .completed
        .is_some_and(|expected| u64::from(expected) == stats.successful_store_operations);
    let clean_success = move_clean && local_count_matches && receiver_clean;
    let result_status = if no_data {
        AccessionStatus::NoData
    } else if unique > 0 && clean_success {
        AccessionStatus::Completed
    } else if unique > 0 || stats.store_failures > 0 {
        AccessionStatus::Partial
    } else {
        AccessionStatus::Failed
    };
    let expected = move_result.counters.completed.map(u64::from);
    let verification_status = match expected {
        Some(expected) if expected == stats.successful_store_operations => {
            ResultVerificationStatus::Matched
        }
        Some(_) => ResultVerificationStatus::Mismatch,
        None => ResultVerificationStatus::Unverifiable,
    };
    let mut messages = Vec::new();
    if let Some(failure) = &move_result.association_failure {
        messages.push(format!("C-MOVE 连接失败：{failure:?}"));
    }
    if let Some(final_status) = move_result.final_status
        && final_status.class != MoveStatusClass::Success
    {
        messages.push(format!("PACS 最终状态 0x{:04X}", final_status.code));
    }
    if let Some(expected) = expected
        && expected != stats.successful_store_operations
    {
        messages.push(format!(
            "PACS 报告 {expected} 个完成子操作，本机成功处理 {} 个 C-STORE（保留 {unique} 个唯一对象）",
            stats.successful_store_operations
        ));
    }
    append_receive_failure_messages(&mut messages, stats);
    if move_result.counters.remaining.unwrap_or(0) > 0 {
        messages.push("PACS 最终响应仍有未完成子操作".to_owned());
    }
    if move_result.counters.failed.unwrap_or(0) > 0 {
        messages.push(format!(
            "PACS 报告 {} 个失败子操作",
            move_result.counters.failed.unwrap_or(0)
        ));
    }
    if move_result.counters.warning.unwrap_or(0) > 0 {
        messages.push(format!(
            "PACS 报告 {} 个警告子操作",
            move_result.counters.warning.unwrap_or(0)
        ));
    }
    if drain == DrainOutcome::TimedOut {
        messages.push("等待迟到影像超时".to_owned());
    }
    let mut result = AccessionResult::blank(accession, result_status);
    result.file_count = unique;
    result.duration_seconds = elapsed.as_secs_f64();
    result.message = messages.join("；");
    result.output_directory = output_directory.to_string_lossy().into_owned();
    result.received_bytes = stats.received_bytes;
    result.speed_bytes_per_second = speed(stats.received_bytes, elapsed);
    let mut reported_files = stats.archived_files.clone();
    reported_files.extend(stats.quarantined_files.iter().cloned());
    result.archived_files = sorted_paths(&reported_files);
    result.new_file_count = stats.published;
    result.existing_skipped_count = stats.existing_skipped;
    result.conflict_preserved_count = stats.conflicts;
    result.verification_status = verification_status;
    result.verification_message.clone_from(&result.message);
    result.local_verified_files = unique;
    result.pacs_completed_suboperations = move_result.counters.completed.map(i64::from);
    result.pacs_expected_suboperations = move_result.counters.completed.map(i64::from);
    result.move_dimse_status = move_result.final_status.map(|value| i64::from(value.code));
    result.attempt_count = attempt_count.max(1);
    result.transient_failure =
        move_result.association_failure.is_some() || stats.store_failures > 0;
    result
}

fn append_receive_failure_messages(messages: &mut Vec<String>, stats: &ReceiveStats) {
    if stats.quarantined > 0 {
        messages.push(format!(
            "{} 个对象因归属或元数据核验失败，已保留在待核验隔离目录",
            stats.quarantined
        ));
    }
    let unpreserved_failures = stats.store_failures.saturating_sub(stats.quarantined);
    if unpreserved_failures > 0 {
        messages.push(format!(
            "{unpreserved_failures} 个对象写入失败且未能隔离保存"
        ));
    }
    if stats.association_failures > 0 {
        messages.push(format!("{} 个接收关联失败", stats.association_failures));
    }
    if stats.archive_failures > 0 {
        messages.push(format!(
            "{} 个对象归档失败，原文件已保留在暂存目录，可重试",
            stats.archive_failures
        ));
    }
}

fn should_retry(result: &MoveAttemptResult, stats: &ReceiveStats) -> bool {
    stats.store_failures > 0
        || stats.unique_sop_instances.is_empty()
            && (result.association_failure.is_some()
                || result
                    .final_status
                    .is_none_or(|status| status.class != MoveStatusClass::Success))
}

fn discard_idle_events(events: &mut broadcast::Receiver<StorageScpEvent>) {
    while let Ok(_) | Err(broadcast::error::TryRecvError::Lagged(_)) = events.try_recv() {}
}

fn collect_quiesced_events(
    events: &mut broadcast::Receiver<StorageScpEvent>,
    route: &ReceiveRoute,
    stats: &mut ReceiveStats,
    internal_events: &mpsc::Sender<InternalEvent>,
    task_id: &TaskId,
) -> Result<(), TransferError> {
    loop {
        match events.try_recv() {
            Ok(event) => {
                stats.collect(event, route, internal_events, task_id)?;
            }
            Err(broadcast::error::TryRecvError::Empty | broadcast::error::TryRecvError::Closed) => {
                return Ok(());
            }
            Err(broadcast::error::TryRecvError::Lagged(count)) => {
                return Err(TransferError::EventStream(format!(
                    "receiver shutdown event stream lagged by {count} event(s)"
                )));
            }
        }
    }
}

fn expects_files(result: &MoveAttemptResult, stats: &ReceiveStats) -> bool {
    stats.store_activity_seen
        || result
            .counters
            .completed
            .is_some_and(|completed| completed > 0)
        || result.final_status.is_some_and(|status| {
            matches!(
                status.class,
                MoveStatusClass::Success | MoveStatusClass::Warning
            )
        })
}

async fn prepare_destination(destination: &str) -> Result<PathBuf, TransferError> {
    let path = PathBuf::from(destination.trim());
    if path.as_os_str().is_empty() {
        return Err(TransferError::InvalidDestination(
            "目标目录不能为空".to_owned(),
        ));
    }
    tokio::fs::create_dir_all(&path)
        .await
        .map_err(|source| TransferError::Io {
            path: path.clone(),
            source,
        })?;
    tokio::fs::canonicalize(&path)
        .await
        .map_err(|source| TransferError::Io { path, source })
}

fn sorted_paths(paths: &HashSet<PathBuf>) -> Vec<String> {
    let mut values = paths
        .iter()
        .map(|path| path.to_string_lossy().into_owned())
        .collect::<Vec<_>>();
    values.sort();
    values
}

#[allow(clippy::cast_precision_loss)]
fn speed(bytes: u64, elapsed: Duration) -> f64 {
    if elapsed.is_zero() {
        0.0
    } else {
        bytes as f64 / elapsed.as_secs_f64()
    }
}

fn usize_to_u32(value: usize) -> u32 {
    u32::try_from(value).unwrap_or(u32::MAX)
}

fn usize_to_u64(value: usize) -> u64 {
    u64::try_from(value).unwrap_or(u64::MAX)
}

fn u64_to_u32(value: u64) -> u32 {
    u32::try_from(value).unwrap_or(u32::MAX)
}

#[cfg(test)]
mod tests {
    use super::*;
    use dcmget_dicom::{
        MoveCounters, MoveFinalStatus, QuarantineOutcome, ReceiveOutcome, Sha256Digest,
    };

    #[tokio::test]
    async fn pause_does_not_cancel_but_stop_does() {
        let control = TaskControl::new();
        control.pause();
        assert_eq!(control.current(), ControlState::Paused);
        assert!(!control.cancellation.is_cancelled());
        control.resume();
        assert_eq!(control.current(), ControlState::Running);
        control.stop_profile();
        assert_eq!(control.current(), ControlState::ProfileStopping);
        assert!(control.cancellation.is_cancelled());
    }

    fn successful_move(completed: u32) -> MoveAttemptResult {
        MoveAttemptResult {
            final_status: Some(MoveFinalStatus::from_code(0x0000)),
            counters: MoveCounters {
                completed: Some(completed),
                ..MoveCounters::default()
            },
            pending_responses: 1,
            locally_received_operations: completed,
            locally_unique_sop_instances: completed,
            association_failure: None,
            cancel_requested: false,
        }
    }

    #[test]
    fn pacs_warning_or_local_count_gap_is_never_reported_complete() {
        let mut stats = ReceiveStats {
            successful_store_operations: 1,
            published: 1,
            ..ReceiveStats::default()
        };
        stats.unique_sop_instances.insert("1.2.3".to_owned());
        stats.archived_files.insert(PathBuf::from("one.dcm"));
        let mut move_result = successful_move(2);
        let result = build_result(
            "A001",
            Path::new("destination"),
            &move_result,
            &stats,
            DrainOutcome::Drained,
            1,
            Duration::from_secs(1),
        );
        assert_eq!(result.status, AccessionStatus::Partial);

        move_result.counters.completed = Some(1);
        move_result.counters.warning = Some(1);
        let result = build_result(
            "A001",
            Path::new("destination"),
            &move_result,
            &stats,
            DrainOutcome::Drained,
            1,
            Duration::from_secs(1),
        );
        assert_eq!(result.status, AccessionStatus::Partial);
        assert!(result.message.contains("警告子操作"));
    }

    #[test]
    fn duplicate_successful_stores_match_pacs_completed_but_keep_one_unique_file() {
        let mut stats = ReceiveStats {
            successful_store_operations: 2,
            published: 1,
            existing_skipped: 1,
            ..ReceiveStats::default()
        };
        stats.unique_sop_instances.insert("1.2.3".to_owned());
        stats.archived_files.insert(PathBuf::from("one.dcm"));

        let result = build_result(
            "A001",
            Path::new("destination"),
            &successful_move(2),
            &stats,
            DrainOutcome::RestartReceiver,
            1,
            Duration::from_secs(1),
        );

        assert_eq!(result.status, AccessionStatus::Completed);
        assert_eq!(result.file_count, 1);
        assert_eq!(result.local_verified_files, 1);
        assert_eq!(
            result.verification_status,
            ResultVerificationStatus::Matched
        );
        assert_eq!(result.new_file_count, 1);
        assert_eq!(result.existing_skipped_count, 1);
    }

    #[test]
    fn completed_count_with_open_association_requires_receiver_restart() {
        assert_eq!(
            completed_drain_outcome(Some(2), 2, false),
            Some(DrainOutcome::RestartReceiver)
        );
        assert_eq!(completed_drain_outcome(Some(2), 1, false), None);
        assert_eq!(
            completed_drain_outcome(Some(2), 1, true),
            Some(DrainOutcome::Drained)
        );
    }

    #[test]
    fn queued_store_events_are_accounted_after_receiver_quiescence() {
        let route = ReceiveRoute {
            profile_id: "profile-1".to_owned(),
            task_id: "task-1".to_owned(),
            accession_number: "A001".to_owned(),
            destination_root: PathBuf::from("destination"),
        };
        let peer = "127.0.0.1:1000".parse().unwrap();
        let path = PathBuf::from("destination/.dcmget-staging/accession-A001/1.2.3.4.dcm");
        let (sender, mut events) = broadcast::channel(8);
        let request = StoreRequest {
            route: route.clone(),
            relative_directory: PathBuf::from(".dcmget-staging/accession-A001"),
            sop_class_uid: "1.2.840.10008.5.1.4.1.1.2".to_owned(),
            sop_instance_uid: "1.2.3.4".to_owned(),
            transfer_syntax_uid: "1.2.840.10008.1.2.1".to_owned(),
        };
        sender
            .send(StorageScpEvent::StoreCompleted {
                peer,
                request: Box::new(request),
                outcome: ReceiveOutcome {
                    disposition: ReceiveDisposition::Published,
                    path: path.clone(),
                    sop_instance_uid: "1.2.3.4".to_owned(),
                    sha256: Sha256Digest([0; 32]),
                    file_bytes: 512,
                    dataset_bytes: 384,
                },
            })
            .unwrap();
        sender
            .send(StorageScpEvent::AssociationClosed { peer })
            .unwrap();
        drop(sender);
        let (internal_events, _receiver) = mpsc::channel(8);
        let task_id = TaskId::new("task-1").unwrap();
        let mut stats = ReceiveStats::default();
        stats.active_associations.insert(peer);

        collect_quiesced_events(&mut events, &route, &mut stats, &internal_events, &task_id)
            .unwrap();

        assert_eq!(stats.successful_store_operations, 1);
        assert_eq!(
            stats.unique_sop_instances,
            HashSet::from(["1.2.3.4".to_owned()])
        );
        assert_eq!(stats.archived_files, HashSet::from([path]));
        assert!(stats.active_associations.is_empty());
    }

    #[test]
    fn idle_receiver_events_are_discarded_before_a_task() {
        let (sender, mut receiver) = broadcast::channel(8);
        sender
            .send(StorageScpEvent::AssociationFailed {
                peer: "127.0.0.1:1000".parse().unwrap(),
                message: "idle failure".to_owned(),
            })
            .unwrap();
        discard_idle_events(&mut receiver);
        assert!(matches!(
            receiver.try_recv(),
            Err(broadcast::error::TryRecvError::Empty)
        ));
    }

    #[test]
    fn no_active_route_uses_the_explicit_profile_quarantine_root() {
        let quarantine_root = PathBuf::from("profile-dicom-volume");
        let resolver = ActiveRouteResolver::new("profile-1".to_owned(), quarantine_root.clone());
        let command = CStoreCommand {
            message_id: 1,
            sop_class_uid: "1.2.840.10008.5.1.4.1.1.2".to_owned(),
            sop_instance_uid: "1.2.3.4".to_owned(),
            move_originator_ae_title: None,
            move_originator_message_id: None,
        };

        let error = resolver
            .resolve(&command, "1.2.840.10008.1.2.1")
            .unwrap_err();
        assert_eq!(
            error.quarantine_target,
            QuarantineTarget {
                profile_id: "profile-1".to_owned(),
                destination_root: quarantine_root,
                active_task_id: None,
            }
        );
    }

    #[test]
    fn unassigned_quarantine_is_not_charged_to_the_next_task() {
        let route = ReceiveRoute {
            profile_id: "profile-1".to_owned(),
            task_id: "next-task".to_owned(),
            accession_number: "NEXT".to_owned(),
            destination_root: PathBuf::from("destination"),
        };
        let event = StorageScpEvent::StoreFailed {
            peer: "127.0.0.1:12345".parse().unwrap(),
            sop_instance_uid: Some("1.2.3.4".to_owned()),
            active_task_id: None,
            message: "no active route".to_owned(),
            quarantined: Some(QuarantineOutcome {
                profile_id: "profile-1".to_owned(),
                active_task_id: None,
                reason: "no active route".to_owned(),
                payload: ReceiveOutcome {
                    disposition: ReceiveDisposition::Quarantined,
                    path: PathBuf::from(
                        "destination/_DcmGetQuarantine/profile-1/unassigned.dcm.quarantine",
                    ),
                    sop_instance_uid: "1.2.3.4".to_owned(),
                    sha256: Sha256Digest([0; 32]),
                    file_bytes: 512,
                    dataset_bytes: 256,
                },
            }),
        };
        let (internal_events, _receiver) = mpsc::channel(1);
        let task_id = TaskId::new("next-task").unwrap();
        let mut stats = ReceiveStats::default();

        assert!(
            !stats
                .collect(event, &route, &internal_events, &task_id)
                .unwrap()
        );
        assert_eq!(stats.store_failures, 0);
        assert_eq!(stats.quarantined, 0);
        assert!(stats.quarantined_files.is_empty());
    }

    #[test]
    fn partial_result_keeps_the_task_retryable() {
        assert_eq!(terminal_task_phase(0, 1, 0), TaskPhase::DownloadRetryable);
        assert_eq!(terminal_task_phase(1, 1, 0), TaskPhase::DownloadRetryable);
        assert_eq!(terminal_task_phase(0, 0, 1), TaskPhase::Failed);
        assert_eq!(terminal_task_phase(1, 0, 0), TaskPhase::Completed);
    }

    #[test]
    fn quarantined_only_result_is_partial_retryable_and_keeps_the_review_path() {
        let quarantine_path =
            PathBuf::from("destination/_DcmGetQuarantine/profile-1/1.2.3.4-1-1.dcm.quarantine");
        let stats = ReceiveStats {
            store_failures: 1,
            quarantined: 1,
            quarantined_files: HashSet::from([quarantine_path.clone()]),
            received_bytes: 512,
            store_activity_seen: true,
            ..ReceiveStats::default()
        };
        let move_result = successful_move(1);
        let result = build_result(
            "A001",
            Path::new("destination"),
            &move_result,
            &stats,
            DrainOutcome::Drained,
            1,
            Duration::from_secs(1),
        );

        assert_eq!(result.status, AccessionStatus::Partial);
        assert!(result.transient_failure);
        assert_eq!(result.file_count, 0);
        assert_eq!(
            result.archived_files,
            vec![quarantine_path.to_string_lossy().into_owned()]
        );
        assert!(result.message.contains("待核验隔离目录"));
        assert!(should_retry(&move_result, &stats));
    }

    #[tokio::test]
    async fn accession_result_waits_for_persistence_confirmation() {
        let (events, mut receiver) = mpsc::channel(1);
        let task_id = TaskId::new("persistence-ack").unwrap();
        let expected_task_id = task_id.clone();
        let worker = tokio::spawn(async move {
            persist_accession_result(
                &events,
                &task_id,
                AccessionResult::blank("A001", AccessionStatus::Completed),
            )
            .await
        });

        let InternalEvent::AccessionFinished {
            task_id,
            acknowledgment,
            ..
        } = receiver.recv().await.unwrap()
        else {
            panic!("expected completed accession event");
        };
        assert_eq!(task_id, expected_task_id);
        assert!(!worker.is_finished());
        acknowledgment.send(Ok(())).unwrap();
        assert!(worker.await.unwrap().is_ok());
    }
}
