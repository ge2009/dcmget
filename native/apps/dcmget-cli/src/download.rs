use std::collections::HashSet;
use std::net::{Ipv4Addr, SocketAddr};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, RwLock};
use std::time::Duration;

use anyhow::{Context, anyhow, bail};
use dcmget_application::archive::{
    ArchiveBatchResult, archive_received_files, prepare_staging_directory,
};
use dcmget_dicom::{
    CStoreCommand, CancellationToken, DicomEndpoint, FileStore, LateStoreDecision, LateStorePolicy,
    LateStoreTracker, MoveAttemptResult, MoveStatusClass, QuarantineTarget, ReceiveDisposition,
    ReceiveRoute, StorageScpConfig, StorageScpEvent, StorageScpHandle, StorageScpService,
    StoreRequest, StoreRequestResolveError, StoreRequestResolver, StudyMoveScu,
};
use dcmget_domain::{TaskId, parse_accessions};
use dcmget_state::LegacyConfig;
use serde_json::json;
use tokio::sync::broadcast;
use tokio::time::{Instant, MissedTickBehavior};

const PROFILE_ID: &str = "cli";
const RECEIVER_READY_TIMEOUT: Duration = Duration::from_secs(2);
const RECEIVER_HEALTH_INTERVAL: Duration = Duration::from_millis(100);
const CANCEL_SETTLE_TIMEOUT: Duration = Duration::from_secs(3);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DownloadOutcome {
    Success,
    Failed,
    Interrupted,
}

/// Execute the unlicensed, direct-to-destination CLI path.
///
/// Setup and input failures are returned to the caller (exit 1). Once the
/// receiver has been confirmed ready, operational failures are represented by
/// `DownloadOutcome::Failed` (exit 2), so the receiver can always be shut down
/// before control returns to `main`.
pub async fn run(
    config_path: &Path,
    accessions_path: &Path,
    destination_override: Option<PathBuf>,
) -> anyhow::Result<DownloadOutcome> {
    let cancellation = CancellationToken::new();
    let interrupted = Arc::new(AtomicBool::new(false));
    let signal_cancellation = cancellation.clone();
    let signal_interrupted = Arc::clone(&interrupted);
    let signal_task = tokio::spawn(async move {
        match tokio::signal::ctrl_c().await {
            Ok(()) => {
                signal_interrupted.store(true, Ordering::Release);
                signal_cancellation.cancel();
            }
            Err(error) => {
                eprintln!("error: failed to install Ctrl-C handler: {error}");
                signal_cancellation.cancel();
            }
        }
    });

    let result = run_inner(
        config_path,
        accessions_path,
        destination_override,
        &cancellation,
        &interrupted,
    )
    .await;
    signal_task.abort();
    if interrupted.load(Ordering::Acquire) {
        Ok(DownloadOutcome::Interrupted)
    } else {
        result
    }
}

#[allow(clippy::too_many_lines)]
async fn run_inner(
    config_path: &Path,
    accessions_path: &Path,
    destination_override: Option<PathBuf>,
    cancellation: &CancellationToken,
    interrupted: &AtomicBool,
) -> anyhow::Result<DownloadOutcome> {
    let config_source = tokio::fs::read_to_string(config_path)
        .await
        .with_context(|| format!("failed to read configuration {}", config_path.display()))?;
    let config = LegacyConfig::from_json(&config_source).context("invalid configuration JSON")?;
    let issues = download_validation_issues(&config, destination_override.is_some());
    if !issues.is_empty() {
        bail!("invalid download configuration: {}", issues.join("; "));
    }

    let accession_source = tokio::fs::read_to_string(accessions_path)
        .await
        .with_context(|| {
            format!(
                "failed to read accession input {}",
                accessions_path.display()
            )
        })?;
    let accession_source = strip_utf8_bom(&accession_source);
    let parsed = parse_accessions(accession_source);
    if !parsed.invalid_values.is_empty() {
        bail!(
            "accession input contains {} invalid value(s)",
            parsed.invalid_values.len()
        );
    }
    if parsed.values.is_empty() {
        bail!("accession input does not contain any usable values");
    }
    if parsed.blank_count > 0 || parsed.duplicate_count > 0 {
        eprintln!(
            "accession input normalized: {} blank line(s), {} duplicate(s) ignored",
            parsed.blank_count, parsed.duplicate_count
        );
    }

    let destination = destination_override
        .unwrap_or_else(|| PathBuf::from(config.dicom_destination_folder.trim()));
    if destination.as_os_str().is_empty() {
        bail!("download destination must not be empty");
    }
    tokio::fs::create_dir_all(&destination)
        .await
        .with_context(|| format!("failed to create destination {}", destination.display()))?;
    let destination = tokio::fs::canonicalize(&destination)
        .await
        .with_context(|| format!("failed to resolve destination {}", destination.display()))?;

    if cancellation.is_cancelled() {
        return Ok(cancelled_outcome(interrupted));
    }

    let resolver = ActiveRouteResolver::new(PROFILE_ID, destination.clone());
    let bind_address = SocketAddr::from((Ipv4Addr::UNSPECIFIED, config.storage_port));
    let receiver = StorageScpService::start(
        StorageScpConfig::new(bind_address, config.storage_ae_title.clone()),
        FileStore::new(),
        resolver.clone(),
    )
    .await
    .with_context(|| format!("failed to start Storage SCP on {bind_address}"))?;
    let mut events = receiver.subscribe();

    match confirm_receiver_ready(&receiver, &mut events, cancellation).await {
        Ok(ReceiverReady::Ready) => {}
        Ok(ReceiverReady::Cancelled) => {
            let shutdown = receiver.shutdown().await;
            if let Err(error) = shutdown {
                eprintln!("error: Storage SCP shutdown after cancellation failed: {error}");
            }
            return Ok(cancelled_outcome(interrupted));
        }
        Err(error) => {
            let shutdown = receiver.shutdown().await;
            if let Err(shutdown_error) = shutdown {
                return Err(error.context(format!(
                    "Storage SCP also failed to shut down: {shutdown_error}"
                )));
            }
            return Err(error);
        }
    }

    eprintln!("Storage SCP ready on {}", receiver.local_address());
    let run_result = run_moves(
        &config,
        &parsed.values,
        &destination,
        &resolver,
        &receiver,
        &mut events,
        cancellation,
    )
    .await;
    let shutdown_result = receiver.shutdown().await;
    // Cancellation, an event-integrity error, or a drain timeout deliberately
    // retains the current route until all receiver associations have stopped.
    resolver.clear();

    if interrupted.load(Ordering::Acquire) {
        if let Err(error) = shutdown_result {
            eprintln!("error: Storage SCP shutdown after Ctrl-C failed: {error}");
        }
        return Ok(DownloadOutcome::Interrupted);
    }
    if let Err(error) = shutdown_result {
        return Err(anyhow!("Storage SCP shutdown failed: {error}"));
    }

    match run_result {
        Ok(RunProgress::Cancelled) => Ok(DownloadOutcome::Failed),
        Ok(RunProgress::Finished(summary)) => {
            println!(
                "{}",
                json!({
                    "type": "summary",
                    "requested": summary.requested,
                    "attempted": summary.attempted,
                    "succeeded": summary.succeeded,
                    "failed": summary.failed,
                    "stopped_early": summary.stopped_early,
                })
            );
            if summary.failed == 0
                && summary.attempted == summary.requested
                && !summary.stopped_early
            {
                Ok(DownloadOutcome::Success)
            } else {
                Ok(DownloadOutcome::Failed)
            }
        }
        Err(error) => {
            eprintln!("error: native download failed: {error:#}");
            Ok(DownloadOutcome::Failed)
        }
    }
}

fn cancelled_outcome(interrupted: &AtomicBool) -> DownloadOutcome {
    if interrupted.load(Ordering::Acquire) {
        DownloadOutcome::Interrupted
    } else {
        DownloadOutcome::Failed
    }
}

fn download_validation_issues(
    config: &LegacyConfig,
    destination_is_overridden: bool,
) -> Vec<String> {
    config
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
                    | "directory_template"
            ) && !(destination_is_overridden && issue.field == "dicom_destination_folder")
        })
        .map(|issue| format!("{}: {}", issue.field, issue.message))
        .collect()
}

fn strip_utf8_bom(source: &str) -> &str {
    source.strip_prefix('\u{feff}').unwrap_or(source)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ReceiverReady {
    Ready,
    Cancelled,
}

async fn confirm_receiver_ready(
    receiver: &StorageScpHandle,
    events: &mut broadcast::Receiver<StorageScpEvent>,
    cancellation: &CancellationToken,
) -> anyhow::Result<ReceiverReady> {
    let expected_address = receiver.local_address();
    let confirmation = tokio::time::timeout(RECEIVER_READY_TIMEOUT, async {
        loop {
            tokio::select! {
                () = cancellation.cancelled() => return Ok(ReceiverReady::Cancelled),
                event = events.recv() => match event {
                    Ok(StorageScpEvent::Ready { address }) if address == expected_address => {
                        return Ok(ReceiverReady::Ready);
                    }
                    Ok(StorageScpEvent::Ready { address }) => {
                        bail!(
                            "Storage SCP announced unexpected address {address}; expected {expected_address}"
                        );
                    }
                    Ok(_) => {}
                    Err(broadcast::error::RecvError::Lagged(count)) => {
                        bail!("Storage SCP ready event stream lagged by {count} event(s)");
                    }
                    Err(broadcast::error::RecvError::Closed) => {
                        bail!("Storage SCP ready event stream closed");
                    }
                }
            }
        }
    })
    .await
    .map_err(|_| anyhow!("timed out waiting for Storage SCP readiness"))??;
    if confirmation == ReceiverReady::Ready && !receiver.is_ready() {
        bail!("Storage SCP stopped immediately after announcing readiness");
    }
    Ok(confirmation)
}

#[derive(Debug, Clone)]
struct RouteTarget {
    route: ReceiveRoute,
    relative_directory: PathBuf,
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
    fn new(profile_id: impl Into<String>, destination_root: PathBuf) -> Self {
        Self {
            active: Arc::new(RwLock::new(None)),
            profile_quarantine: QuarantineTarget {
                profile_id: profile_id.into(),
                destination_root,
                active_task_id: None,
            },
        }
    }

    fn activate(&self, target: RouteTarget) -> anyhow::Result<RouteLease> {
        let mut active = self
            .active
            .write()
            .map_err(|_| anyhow!("active receive route lock was poisoned"))?;
        if active.is_some() {
            bail!("a receive route is already active for this CLI Profile");
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
    /// Release a normally drained route. Dropping without calling this method
    /// intentionally retains the route until the receiver has shut down.
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

    fn collect_store_outcome(
        &mut self,
        disposition: ReceiveDisposition,
        sop_instance_uid: &str,
        file_bytes: u64,
    ) {
        self.unique_sop_instances
            .insert(sop_instance_uid.to_owned());
        self.received_bytes = self.received_bytes.saturating_add(file_bytes);
        match disposition {
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
            ReceiveDisposition::Quarantined => {
                self.store_failures = self.store_failures.saturating_add(1);
                self.quarantined = self.quarantined.saturating_add(1);
            }
        }
        self.store_activity_seen = true;
    }

    fn collect(
        &mut self,
        event: StorageScpEvent,
        expected_route: &ReceiveRoute,
    ) -> anyhow::Result<bool> {
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
                    bail!("received a C-STORE event for a route other than the active accession");
                }
                if outcome.disposition == ReceiveDisposition::Quarantined {
                    bail!("quarantined payload was incorrectly reported as a completed store");
                }
                self.archived_files.insert(outcome.path.clone());
                self.collect_store_outcome(
                    outcome.disposition,
                    &outcome.sop_instance_uid,
                    outcome.file_bytes,
                );
                Ok(true)
            }
            StorageScpEvent::StoreFailed {
                active_task_id,
                quarantined,
                ..
            } => {
                if let Some(quarantined) = quarantined.as_ref() {
                    if quarantined.profile_id != expected_route.profile_id {
                        bail!("received a quarantine event for another CLI Profile");
                    }
                    if quarantined.active_task_id != active_task_id {
                        bail!("quarantine event task correlation was inconsistent");
                    }
                }
                if active_task_id.as_deref() != Some(expected_route.task_id.as_str()) {
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
                Ok(true)
            }
            StorageScpEvent::AssociationFailed { .. } => {
                self.association_failures = self.association_failures.saturating_add(1);
                Ok(false)
            }
        }
    }
}

#[derive(Debug, Clone, Copy, Default)]
struct RunSummary {
    requested: usize,
    attempted: usize,
    succeeded: usize,
    failed: usize,
    stopped_early: bool,
}

#[derive(Debug, Clone, Copy)]
enum RunProgress {
    Finished(RunSummary),
    Cancelled,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DrainOutcome {
    Drained,
    TimedOut,
}

#[allow(clippy::too_many_arguments, clippy::too_many_lines)]
async fn run_moves(
    config: &LegacyConfig,
    accessions: &[String],
    destination: &Path,
    resolver: &ActiveRouteResolver,
    receiver: &StorageScpHandle,
    events: &mut broadcast::Receiver<StorageScpEvent>,
    cancellation: &CancellationToken,
) -> anyhow::Result<RunProgress> {
    let task_id = TaskId::generate();
    let move_scu = StudyMoveScu::default();
    let mut summary = RunSummary {
        requested: accessions.len(),
        ..RunSummary::default()
    };

    for accession in accessions {
        if cancellation.is_cancelled() {
            return Ok(RunProgress::Cancelled);
        }
        let staging = prepare_staging_directory(destination, accession)
            .await
            .with_context(|| format!("failed to prepare staging for accession {accession}"))?;
        let relative_directory = staging.relative_path().to_path_buf();
        let route = ReceiveRoute {
            profile_id: PROFILE_ID.to_owned(),
            task_id: task_id.to_string(),
            accession_number: accession.clone(),
            destination_root: destination.to_path_buf(),
        };
        let route_lease = resolver.activate(RouteTarget {
            route: route.clone(),
            relative_directory: relative_directory.clone(),
        })?;
        let request = dcmget_dicom::MoveRequest::study_by_accession(
            PROFILE_ID,
            task_id.to_string(),
            accession,
            DicomEndpoint {
                host: config.pacs_server_ip.trim().to_owned(),
                port: config.pacs_server_port,
            },
            config.calling_ae_title.clone(),
            config.pacs_ae_title.clone(),
            config.storage_ae_title.clone(),
        );
        let mut stats = ReceiveStats::default();
        let Some(mut result) = execute_move_collecting(
            &move_scu,
            &request,
            &route,
            receiver,
            events,
            cancellation,
            &mut stats,
        )
        .await?
        else {
            drop(route_lease);
            return Ok(RunProgress::Cancelled);
        };

        let expect_files = expects_files(&result, &stats);
        let Some(drain) = drain_late_stores(
            expect_files,
            &route,
            receiver,
            events,
            cancellation,
            &mut stats,
        )
        .await?
        else {
            drop(route_lease);
            return Ok(RunProgress::Cancelled);
        };
        result.locally_received_operations = u64_to_u32(stats.successful_store_operations);
        result.locally_unique_sop_instances = usize_to_u32(stats.unique_sop_instances.len());
        if drain == DrainOutcome::Drained {
            let archive = archive_received_files(
                destination,
                staging.path(),
                accession,
                &config.directory_template,
                &stats.archived_files,
            )
            .await
            .with_context(|| format!("failed to archive accession {accession}"))?;
            for failure in archive.failures() {
                eprintln!("error: {failure}");
            }
            stats.apply_archive(archive);
        }

        let failure_reasons = failure_reasons(&result, &stats, drain);
        let failed = !failure_reasons.is_empty();
        summary.attempted = summary.attempted.saturating_add(1);
        if failed {
            summary.failed = summary.failed.saturating_add(1);
        } else {
            summary.succeeded = summary.succeeded.saturating_add(1);
        }
        print_accession_report(
            accession,
            destination,
            &result,
            &stats,
            drain,
            &failure_reasons,
        );
        if drain == DrainOutcome::TimedOut {
            // Switching the active route after an incomplete drain could send
            // an old accession into the next accession's directory.
            drop(route_lease);
            summary.stopped_early = summary.attempted < summary.requested;
            break;
        }
        route_lease.release();
    }

    Ok(RunProgress::Finished(summary))
}

#[allow(clippy::too_many_arguments)]
async fn execute_move_collecting(
    move_scu: &StudyMoveScu,
    request: &dcmget_dicom::MoveRequest,
    route: &ReceiveRoute,
    receiver: &StorageScpHandle,
    events: &mut broadcast::Receiver<StorageScpEvent>,
    cancellation: &CancellationToken,
    stats: &mut ReceiveStats,
) -> anyhow::Result<Option<MoveAttemptResult>> {
    let mut move_future = Box::pin(move_scu.execute(request, cancellation));
    let mut health = tokio::time::interval(RECEIVER_HEALTH_INTERVAL);
    health.set_missed_tick_behavior(MissedTickBehavior::Skip);
    loop {
        tokio::select! {
            result = &mut move_future => return Ok(Some(result)),
            () = cancellation.cancelled() => {
                let _ = tokio::time::timeout(CANCEL_SETTLE_TIMEOUT, &mut move_future).await;
                return Ok(None);
            }
            event = events.recv() => match event {
                Ok(event) => {
                    stats.collect(event, route)?;
                }
                Err(broadcast::error::RecvError::Lagged(count)) => {
                    cancellation.cancel();
                    let _ = tokio::time::timeout(CANCEL_SETTLE_TIMEOUT, &mut move_future).await;
                    bail!("Storage SCP event collection lagged by {count} event(s)");
                }
                Err(broadcast::error::RecvError::Closed) => {
                    cancellation.cancel();
                    let _ = tokio::time::timeout(CANCEL_SETTLE_TIMEOUT, &mut move_future).await;
                    bail!("Storage SCP event stream closed during C-MOVE");
                }
            },
            _ = health.tick() => {
                if !receiver.is_ready() {
                    cancellation.cancel();
                    let _ = tokio::time::timeout(CANCEL_SETTLE_TIMEOUT, &mut move_future).await;
                    bail!("Storage SCP stopped while C-MOVE was active");
                }
            }
        }
    }
}

#[allow(clippy::too_many_arguments)]
async fn drain_late_stores(
    expect_files: bool,
    route: &ReceiveRoute,
    receiver: &StorageScpHandle,
    events: &mut broadcast::Receiver<StorageScpEvent>,
    cancellation: &CancellationToken,
    stats: &mut ReceiveStats,
) -> anyhow::Result<Option<DrainOutcome>> {
    let policy = LateStorePolicy::default();
    let started = Instant::now();
    let mut tracker = LateStoreTracker::new(policy, expect_files);
    if stats.store_activity_seen {
        // Stores completed before the final C-MOVE response still require a
        // quiet period after that final response before the route is released.
        tracker.observe_completed_store(Duration::ZERO);
    }

    loop {
        if !receiver.is_ready() {
            bail!("Storage SCP stopped while late C-STOREs were draining");
        }
        let elapsed = started.elapsed();
        if elapsed >= policy.maximum_wait && !stats.active_associations.is_empty() {
            return Ok(Some(DrainOutcome::TimedOut));
        }
        let delay = match tracker.decision(elapsed, cancellation) {
            LateStoreDecision::Cancelled => return Ok(None),
            LateStoreDecision::TimedOut => return Ok(Some(DrainOutcome::TimedOut)),
            LateStoreDecision::Drained if stats.active_associations.is_empty() => {
                return Ok(Some(DrainOutcome::Drained));
            }
            LateStoreDecision::Drained => policy.poll_interval,
            LateStoreDecision::Wait(delay) => delay,
        };

        tokio::select! {
            () = cancellation.cancelled() => return Ok(None),
            () = tokio::time::sleep(delay) => {}
            event = events.recv() => match event {
                Ok(event) => {
                    if stats.collect(event, route)? {
                        tracker.observe_completed_store(started.elapsed());
                    }
                }
                Err(broadcast::error::RecvError::Lagged(count)) => {
                    bail!("Storage SCP event collection lagged by {count} event(s) during drain");
                }
                Err(broadcast::error::RecvError::Closed) => {
                    bail!("Storage SCP event stream closed during late-store drain");
                }
            }
        }
    }
}

fn expects_files(result: &MoveAttemptResult, stats: &ReceiveStats) -> bool {
    if stats.store_activity_seen {
        return true;
    }
    match result.counters.completed {
        Some(completed) => completed > 0,
        None => result.final_status.is_some_and(|status| {
            matches!(
                status.class,
                MoveStatusClass::Success | MoveStatusClass::Warning
            )
        }),
    }
}

fn failure_reasons(
    result: &MoveAttemptResult,
    stats: &ReceiveStats,
    drain: DrainOutcome,
) -> Vec<String> {
    let mut reasons = Vec::new();
    if let Some(failure) = &result.association_failure {
        reasons.push(format!("C-MOVE association failure: {failure:?}"));
    }
    match result.final_status {
        None => reasons.push("PACS did not return a final C-MOVE status".to_owned()),
        Some(final_status) if final_status.class != MoveStatusClass::Success => {
            reasons.push(format!(
                "PACS final C-MOVE status 0x{:04X} was {:?}",
                final_status.code, final_status.class
            ));
        }
        Some(_) => {}
    }
    if result.counters.remaining.unwrap_or(0) > 0 {
        reasons.push("PACS reported remaining suboperations in the final response".to_owned());
    }
    if result.counters.failed.unwrap_or(0) > 0 {
        reasons.push("PACS reported failed C-MOVE suboperations".to_owned());
    }
    if result.counters.warning.unwrap_or(0) > 0 {
        reasons.push("PACS reported warning C-MOVE suboperations".to_owned());
    }
    if result.counters.completed.is_none()
        && result
            .final_status
            .is_some_and(|status| status.class == MoveStatusClass::Success)
    {
        reasons.push(
            "successful C-MOVE could not be verified because PACS omitted the completed count"
                .to_owned(),
        );
    }
    if let Some(expected) = result.counters.completed {
        let successful_store_operations = stats.successful_store_operations;
        if u64::from(expected) != successful_store_operations {
            reasons.push(format!(
                "PACS reported {expected} completed suboperation(s), but {successful_store_operations} successful C-STORE operation(s) reached local storage"
            ));
        }
    }
    if stats.store_failures > 0 {
        reasons.push(format!(
            "{} C-STORE operation(s) failed locally",
            stats.store_failures
        ));
    }
    if stats.association_failures > 0 {
        reasons.push(format!(
            "{} storage association(s) failed",
            stats.association_failures
        ));
    }
    if stats.archive_failures > 0 {
        reasons.push(format!(
            "{} object(s) failed directory-template publication and remain in staging",
            stats.archive_failures
        ));
    }
    if stats.conflicts > 0 {
        reasons.push(format!(
            "{} conflicting SOP instance(s) were preserved outside the accession directory",
            stats.conflicts
        ));
    }
    if drain == DrainOutcome::TimedOut {
        reasons.push(
            "late C-STORE drain timed out; subsequent accessions were not started".to_owned(),
        );
    }
    reasons
}

fn print_accession_report(
    accession: &str,
    output_directory: &Path,
    result: &MoveAttemptResult,
    stats: &ReceiveStats,
    drain: DrainOutcome,
    failure_reasons: &[String],
) {
    println!(
        "{}",
        accession_report_value(
            accession,
            output_directory,
            result,
            stats,
            drain,
            failure_reasons,
        )
    );
}

fn accession_report_value(
    accession: &str,
    output_directory: &Path,
    result: &MoveAttemptResult,
    stats: &ReceiveStats,
    drain: DrainOutcome,
    failure_reasons: &[String],
) -> serde_json::Value {
    let mut archived_files = stats.archived_files.iter().collect::<Vec<_>>();
    archived_files.sort();
    let mut quarantined_files = stats.quarantined_files.iter().collect::<Vec<_>>();
    quarantined_files.sort();
    json!({
        "type": "accession",
        "accession": accession,
        "success": failure_reasons.is_empty(),
        "no_data": result.counters.completed == Some(0)
            && stats.successful_store_operations == 0
            && stats.unique_sop_instances.is_empty(),
        "output_directory": output_directory,
        "move_final_status": result.final_status.map(|status| format!("0x{:04X}", status.code)),
        "move_status_class": result.final_status.map(|status| format!("{:?}", status.class)),
        "pacs_remaining": result.counters.remaining,
        "pacs_completed": result.counters.completed,
        "pacs_failed": result.counters.failed,
        "pacs_warning": result.counters.warning,
        "local_store_operations": stats.successful_store_operations,
        "local_unique_sop_instances": stats.unique_sop_instances.len(),
        "published": stats.published,
        "existing_skipped": stats.existing_skipped,
        "conflicts": stats.conflicts,
        "archive_failures": stats.archive_failures,
        "archived_files": archived_files,
        "store_failures": stats.store_failures,
        "quarantined": stats.quarantined,
        "quarantined_files": quarantined_files,
        "storage_association_failures": stats.association_failures,
        "received_bytes": stats.received_bytes,
        "late_store_drain": match drain {
            DrainOutcome::Drained => "drained",
            DrainOutcome::TimedOut => "timed_out",
        },
        "failures": failure_reasons,
    })
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
    use std::fs;

    use dcmget_dicom::{
        MoveCounters, MoveFinalStatus, QuarantineOutcome, ReceiveOutcome, Sha256Digest,
    };
    use dicom_core::{DataElement, PrimitiveValue, VR};
    use dicom_dictionary_std::tags;
    use dicom_object::{FileMetaTableBuilder, InMemDicomObject};
    use sha2::{Digest, Sha256};

    fn route(accession: &str) -> ReceiveRoute {
        ReceiveRoute {
            profile_id: PROFILE_ID.to_owned(),
            task_id: "task-1".to_owned(),
            accession_number: accession.to_owned(),
            destination_root: PathBuf::from("destination"),
        }
    }

    fn resolver() -> ActiveRouteResolver {
        ActiveRouteResolver::new(PROFILE_ID, PathBuf::from("destination"))
    }

    fn command() -> CStoreCommand {
        CStoreCommand {
            message_id: 7,
            sop_class_uid: "1.2.840.10008.5.1.4.1.1.2".to_owned(),
            sop_instance_uid: "1.2.3.4".to_owned(),
            move_originator_ae_title: Some("DCMGET".to_owned()),
            move_originator_message_id: Some(1),
        }
    }

    fn successful_move(completed: Option<u32>) -> MoveAttemptResult {
        MoveAttemptResult {
            final_status: Some(MoveFinalStatus::from_code(0x0000)),
            counters: MoveCounters {
                completed,
                ..MoveCounters::default()
            },
            pending_responses: 1,
            locally_received_operations: completed.unwrap_or(0),
            locally_unique_sop_instances: completed.unwrap_or(0),
            association_failure: None,
            cancel_requested: false,
        }
    }

    fn write_test_dicom(
        path: &Path,
        patient_id: &str,
        study_instance_uid: &str,
        sop_instance_uid: &str,
    ) {
        const SOP_CLASS_UID: &str = "1.2.840.10008.5.1.4.1.1.7";
        let mut object = InMemDicomObject::new_empty();
        object.put(DataElement::new(
            tags::SPECIFIC_CHARACTER_SET,
            VR::CS,
            PrimitiveValue::from("ISO_IR 192"),
        ));
        object.put(DataElement::new(
            tags::SOP_CLASS_UID,
            VR::UI,
            PrimitiveValue::from(SOP_CLASS_UID),
        ));
        object.put(DataElement::new(
            tags::SOP_INSTANCE_UID,
            VR::UI,
            PrimitiveValue::from(sop_instance_uid),
        ));
        object.put(DataElement::new(
            tags::PATIENT_ID,
            VR::LO,
            PrimitiveValue::from(patient_id),
        ));
        object.put(DataElement::new(
            tags::ACCESSION_NUMBER,
            VR::SH,
            PrimitiveValue::from("DATASET-ACCESSION"),
        ));
        object.put(DataElement::new(
            tags::STUDY_INSTANCE_UID,
            VR::UI,
            PrimitiveValue::from(study_instance_uid),
        ));
        let file = object
            .with_meta(
                FileMetaTableBuilder::new()
                    .transfer_syntax("1.2.840.10008.1.2.1")
                    .media_storage_sop_class_uid(SOP_CLASS_UID)
                    .media_storage_sop_instance_uid(sop_instance_uid),
            )
            .unwrap();
        file.write_to_file(path).unwrap();
    }

    fn received_stats(source: PathBuf, sop_instance_uid: &str) -> ReceiveStats {
        ReceiveStats {
            successful_store_operations: 1,
            unique_sop_instances: HashSet::from([sop_instance_uid.to_owned()]),
            archived_files: HashSet::from([source]),
            published: 1,
            store_activity_seen: true,
            ..ReceiveStats::default()
        }
    }

    fn sha256(path: &Path) -> [u8; 32] {
        Sha256::digest(fs::read(path).unwrap()).into()
    }

    #[tokio::test]
    async fn cli_default_template_publishes_from_hidden_same_volume_staging() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path().canonicalize().unwrap();
        let staging = prepare_staging_directory(&root, "A001").await.unwrap();
        assert!(
            staging
                .relative_path()
                .starts_with(Path::new(".dcmget-staging"))
        );
        assert!(staging.path().starts_with(&root));
        let source = staging.path().join("1.2.3.4.dcm");
        write_test_dicom(&source, "P001", "1.2.3", "1.2.3.4");
        let mut stats = received_stats(source.clone(), "1.2.3.4");

        let archive = archive_received_files(
            &root,
            staging.path(),
            "A001",
            &LegacyConfig::default().directory_template,
            &stats.archived_files,
        )
        .await
        .unwrap();
        stats.apply_archive(archive);

        let target = root.join("P001/A001/1.2.3/1.2.3.4.dcm");
        assert_eq!(stats.archive_failures, 0);
        assert_eq!(stats.archived_files, HashSet::from([target.clone()]));
        assert!(target.is_file());
        assert!(!source.exists());
    }

    #[tokio::test]
    async fn cli_custom_template_uses_requested_accession() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path().canonicalize().unwrap();
        let staging = prepare_staging_directory(&root, "REQUESTED").await.unwrap();
        let source = staging.path().join("1.2.4.5.dcm");
        write_test_dicom(&source, "P002", "1.2.4", "1.2.4.5");

        let archive = archive_received_files(
            &root,
            staging.path(),
            "REQUESTED",
            "study-{StudyInstanceUID}/{AccessionNumber}/{PatientID}",
            &HashSet::from([source]),
        )
        .await
        .unwrap();
        let (files, failures, conflicts) = archive.into_parts();

        let target = root.join("study-1.2.4/REQUESTED/P002/1.2.4.5.dcm");
        assert!(failures.is_empty());
        assert_eq!(conflicts, 0);
        assert_eq!(files, HashSet::from([target.clone()]));
        assert!(target.is_file());
        assert!(!root.join("study-1.2.4/DATASET-ACCESSION").exists());
    }

    #[tokio::test]
    async fn cli_rejects_unsafe_template_and_archive_defense_cannot_escape_root() {
        let config = LegacyConfig {
            directory_template: "../../{PatientID}".to_owned(),
            ..LegacyConfig::default()
        };
        assert!(
            download_validation_issues(&config, false)
                .iter()
                .any(|issue| issue.starts_with("directory_template:"))
        );

        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path().canonicalize().unwrap();
        let staging = prepare_staging_directory(&root, "A003").await.unwrap();
        let source = staging.path().join("1.2.5.6.dcm");
        write_test_dicom(&source, "../PATIENT", "1.2.5", "1.2.5.6");
        let archive = archive_received_files(
            &root,
            staging.path(),
            "A003",
            "../../{PatientID}/{AccessionNumber}",
            &HashSet::from([source]),
        )
        .await
        .unwrap();
        let (files, failures, _) = archive.into_parts();

        assert!(failures.is_empty());
        assert_eq!(files.len(), 1);
        assert!(files.iter().all(|path| path.starts_with(&root)));
    }

    #[tokio::test]
    async fn cli_metadata_parse_failure_keeps_staging_and_fails_accession() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path().canonicalize().unwrap();
        let staging = prepare_staging_directory(&root, "A004").await.unwrap();
        let source = staging.path().join("broken.dcm");
        fs::write(&source, b"not a DICOM object").unwrap();
        let mut stats = received_stats(source.clone(), "1.2.6.7");

        let archive = archive_received_files(
            &root,
            staging.path(),
            "A004",
            &LegacyConfig::default().directory_template,
            &stats.archived_files,
        )
        .await
        .unwrap();
        assert_eq!(archive.failures().len(), 1);
        stats.apply_archive(archive);
        let failures = failure_reasons(&successful_move(Some(1)), &stats, DrainOutcome::Drained);

        assert!(source.is_file());
        assert_eq!(stats.archive_failures, 1);
        assert!(
            failures
                .iter()
                .any(|failure| failure.contains("remain in staging"))
        );
    }

    #[tokio::test]
    async fn cli_template_publication_preserves_dicom_sha256() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path().canonicalize().unwrap();
        let staging = prepare_staging_directory(&root, "A005").await.unwrap();
        let source = staging.path().join("1.2.7.8.dcm");
        write_test_dicom(&source, "P005", "1.2.7", "1.2.7.8");
        let source_sha = sha256(&source);

        let archive = archive_received_files(
            &root,
            staging.path(),
            "A005",
            &LegacyConfig::default().directory_template,
            &HashSet::from([source]),
        )
        .await
        .unwrap();
        let (files, failures, _) = archive.into_parts();
        let target = files.into_iter().next().unwrap();

        assert!(failures.is_empty());
        assert_eq!(sha256(&target), source_sha);
    }

    #[test]
    fn receive_route_remains_active_for_the_full_lease() {
        let resolver = resolver();
        let expected_route = route("../../A001");
        let lease = resolver
            .activate(RouteTarget {
                route: expected_route.clone(),
                relative_directory: PathBuf::from(".dcmget-staging/accession-A001"),
            })
            .expect("route should activate");
        let request = resolver
            .resolve(&command(), "1.2.840.10008.1.2.1")
            .expect("route must remain resolvable during the lease");
        assert_eq!(request.route, expected_route);
        assert_eq!(request.sop_class_uid, command().sop_class_uid);
        assert_eq!(request.sop_instance_uid, command().sop_instance_uid);
        lease.release();
        assert!(resolver.resolve(&command(), "1.2.840.10008.1.2.1").is_err());
    }

    #[test]
    fn abandoned_route_is_retained_until_receiver_shutdown_cleanup() {
        let resolver = resolver();
        let lease = resolver
            .activate(RouteTarget {
                route: route("A001"),
                relative_directory: PathBuf::from(".dcmget-staging/accession-A001"),
            })
            .expect("route should activate");
        drop(lease);
        assert!(
            resolver.resolve(&command(), "1.2.840.10008.1.2.1").is_ok(),
            "cancellation and drain timeout must retain the old route"
        );
        resolver.clear();
        assert!(resolver.resolve(&command(), "1.2.840.10008.1.2.1").is_err());
    }

    #[test]
    fn no_active_route_uses_the_explicit_cli_destination_for_quarantine() {
        let quarantine_root = PathBuf::from("explicit-cli-volume");
        let resolver = ActiveRouteResolver::new(PROFILE_ID, quarantine_root.clone());
        let error = resolver
            .resolve(&command(), "1.2.840.10008.1.2.1")
            .unwrap_err();
        assert_eq!(
            error.quarantine_target,
            QuarantineTarget {
                profile_id: PROFILE_ID.to_owned(),
                destination_root: quarantine_root,
                active_task_id: None,
            }
        );
    }

    #[test]
    fn unassigned_quarantine_is_not_charged_to_the_next_cli_accession() {
        let expected_route = route("NEXT");
        let quarantine_path =
            PathBuf::from("destination/_DcmGetQuarantine/cli/unassigned.dcm.quarantine");
        let event = StorageScpEvent::StoreFailed {
            peer: "127.0.0.1:12345".parse().unwrap(),
            sop_instance_uid: Some("1.2.3.4".to_owned()),
            active_task_id: None,
            message: "no active route".to_owned(),
            quarantined: Some(QuarantineOutcome {
                profile_id: PROFILE_ID.to_owned(),
                active_task_id: None,
                reason: "no active route".to_owned(),
                payload: ReceiveOutcome {
                    disposition: ReceiveDisposition::Quarantined,
                    path: quarantine_path,
                    sop_instance_uid: "1.2.3.4".to_owned(),
                    sha256: Sha256Digest([0; 32]),
                    file_bytes: 512,
                    dataset_bytes: 256,
                },
            }),
        };
        let mut stats = ReceiveStats::default();

        assert!(!stats.collect(event, &expected_route).unwrap());
        assert_eq!(stats.store_failures, 0);
        assert_eq!(stats.quarantined, 0);
        assert!(stats.quarantined_files.is_empty());
    }

    #[test]
    fn irrelevant_pdi_settings_do_not_block_the_download_only_cli() {
        let mut config = LegacyConfig {
            pdi_export_enabled: true,
            pdi_institution_name: String::new(),
            ..LegacyConfig::default()
        };
        assert!(download_validation_issues(&config, false).is_empty());
        config.pacs_server_ip.clear();
        assert_eq!(download_validation_issues(&config, false).len(), 1);
    }

    #[test]
    fn confirmed_no_data_is_complete_but_gaps_and_drain_timeouts_fail() {
        let stats = ReceiveStats::default();
        let no_data = failure_reasons(&successful_move(Some(0)), &stats, DrainOutcome::Drained);
        assert!(no_data.is_empty());

        let mut missing_local_files = successful_move(Some(2));
        missing_local_files.locally_received_operations = 0;
        missing_local_files.locally_unique_sop_instances = 0;
        let gap = failure_reasons(&missing_local_files, &stats, DrainOutcome::TimedOut);
        assert!(gap.iter().any(|reason| reason.contains("local storage")));
        assert!(gap.iter().any(|reason| reason.contains("drain timed out")));
    }

    #[test]
    fn accession_input_utf8_bom_is_removed_before_domain_parsing() {
        let parsed = parse_accessions(strip_utf8_bom("\u{feff}A001\nA002\n"));
        assert_eq!(parsed.values, ["A001", "A002"]);
    }

    #[test]
    fn missing_pacs_count_is_not_complete() {
        let stats = ReceiveStats::default();
        let missing_count = failure_reasons(&successful_move(None), &stats, DrainOutcome::Drained);
        assert!(
            missing_count
                .iter()
                .any(|reason| reason.contains("omitted the completed count"))
        );
    }

    #[test]
    fn duplicate_sop_deliveries_complete_and_report_operations_separately_from_unique_files() {
        let mut duplicate_stats = ReceiveStats::default();
        duplicate_stats.collect_store_outcome(ReceiveDisposition::Published, "1.2.3.4", 1024);
        duplicate_stats.collect_store_outcome(ReceiveDisposition::ExistingSkipped, "1.2.3.4", 1024);
        let duplicate_delivery = failure_reasons(
            &successful_move(Some(2)),
            &duplicate_stats,
            DrainOutcome::Drained,
        );
        assert!(duplicate_delivery.is_empty());

        let report = accession_report_value(
            "A001",
            Path::new("destination/accession-A001"),
            &successful_move(Some(2)),
            &duplicate_stats,
            DrainOutcome::Drained,
            &duplicate_delivery,
        );
        assert_eq!(report["success"], true);
        assert_eq!(report["local_store_operations"], 2);
        assert_eq!(report["local_unique_sop_instances"], 1);
        assert_eq!(report["published"], 1);
        assert_eq!(report["existing_skipped"], 1);
    }

    #[test]
    fn extra_successful_store_operations_are_not_silently_accepted() {
        let stats = ReceiveStats {
            successful_store_operations: 2,
            ..ReceiveStats::default()
        };
        let failures = failure_reasons(&successful_move(Some(1)), &stats, DrainOutcome::Drained);
        assert!(
            failures
                .iter()
                .any(|reason| reason.contains("2 successful C-STORE"))
        );
    }

    #[test]
    fn quarantined_store_is_failed_and_reported_without_counting_local_success() {
        let expected_route = route("A001");
        let quarantine_path =
            PathBuf::from("destination/_DcmGetQuarantine/cli/1.2.3.4-1-1.dcm.quarantine");
        let mut stats = ReceiveStats::default();
        assert!(
            stats
                .collect(
                    StorageScpEvent::StoreFailed {
                        peer: "127.0.0.1:12345".parse().unwrap(),
                        sop_instance_uid: Some("1.2.3.4".to_owned()),
                        active_task_id: Some(expected_route.task_id.clone()),
                        message: "cannot attribute received C-STORE".to_owned(),
                        quarantined: Some(QuarantineOutcome {
                            profile_id: PROFILE_ID.to_owned(),
                            active_task_id: Some(expected_route.task_id.clone()),
                            reason: "cannot attribute received C-STORE".to_owned(),
                            payload: ReceiveOutcome {
                                disposition: ReceiveDisposition::Quarantined,
                                path: quarantine_path.clone(),
                                sop_instance_uid: "1.2.3.4".to_owned(),
                                sha256: Sha256Digest([0; 32]),
                                file_bytes: 512,
                                dataset_bytes: 256,
                            },
                        }),
                    },
                    &expected_route,
                )
                .unwrap()
        );
        assert_eq!(stats.successful_store_operations, 0);
        assert_eq!(stats.store_failures, 1);
        assert_eq!(stats.quarantined, 1);
        assert!(stats.quarantined_files.contains(&quarantine_path));

        let failures = failure_reasons(&successful_move(Some(1)), &stats, DrainOutcome::Drained);
        let report = accession_report_value(
            "A001",
            Path::new("destination/accession-A001"),
            &successful_move(Some(1)),
            &stats,
            DrainOutcome::Drained,
            &failures,
        );
        assert_eq!(report["success"], false);
        assert_eq!(report["local_store_operations"], 0);
        assert_eq!(report["quarantined"], 1);
        assert_eq!(
            report["quarantined_files"][0],
            quarantine_path.to_string_lossy().as_ref()
        );
    }

    #[tokio::test]
    async fn explicit_receiver_shutdown_releases_the_bound_port() {
        let resolver = resolver();
        let receiver = StorageScpService::start(
            StorageScpConfig::new(SocketAddr::from((Ipv4Addr::LOCALHOST, 0)), "DCMGET"),
            FileStore::new(),
            resolver,
        )
        .await
        .expect("receiver should bind");
        let address = receiver.local_address();
        receiver.shutdown().await.expect("shutdown should drain");
        let rebound = tokio::net::TcpListener::bind(address)
            .await
            .expect("shutdown must release the listening port");
        drop(rebound);
    }
}
