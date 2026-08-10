use std::collections::HashSet;
use std::io::ErrorKind;
use std::net::{Ipv4Addr, SocketAddr};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, RwLock};
use std::time::Duration;

use anyhow::{Context, anyhow, bail};
use dcmget_dicom::{
    CStoreCommand, CancellationToken, DicomEndpoint, FileStore, LateStoreDecision, LateStorePolicy,
    LateStoreTracker, MoveAttemptResult, MoveStatusClass, ReceiveDisposition, ReceiveRoute,
    StorageScpConfig, StorageScpEvent, StorageScpHandle, StorageScpService, StoreRequest,
    StoreRequestResolveError, StoreRequestResolver, StudyMoveScu,
};
use dcmget_domain::{TaskId, parse_accessions};
use dcmget_state::LegacyConfig;
use serde_json::json;
use sha2::{Digest, Sha256};
use tokio::sync::broadcast;
use tokio::time::{Instant, MissedTickBehavior};

const PROFILE_ID: &str = "cli";
const RECEIVER_READY_TIMEOUT: Duration = Duration::from_secs(2);
const RECEIVER_HEALTH_INTERVAL: Duration = Duration::from_millis(100);
const CANCEL_SETTLE_TIMEOUT: Duration = Duration::from_secs(3);
const MAX_COMPONENT_BYTES: usize = 180;
const COMPONENT_PREFIX_BYTES: usize = 128;

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

    let resolver = ActiveRouteResolver::default();
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

#[derive(Clone, Default)]
struct ActiveRouteResolver {
    active: Arc<RwLock<Option<RouteTarget>>>,
}

impl ActiveRouteResolver {
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
            })?
            .clone()
            .ok_or_else(|| StoreRequestResolveError {
                message: "no C-MOVE receive route is active".to_owned(),
            })?;
        Ok(StoreRequest {
            route: target.route,
            relative_directory: target.relative_directory,
            sop_class_uid: command.sop_class_uid.clone(),
            sop_instance_uid: command.sop_instance_uid.clone(),
            transfer_syntax_uid: transfer_syntax_uid.to_owned(),
        })
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
    store_operations: u64,
    unique_sop_instances: HashSet<String>,
    published: u64,
    existing_skipped: u64,
    conflicts: u64,
    store_failures: u64,
    association_failures: u64,
    received_bytes: u64,
    store_activity_seen: bool,
    active_associations: HashSet<SocketAddr>,
}

impl ReceiveStats {
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
                self.store_operations = self.store_operations.saturating_add(1);
                self.unique_sop_instances
                    .insert(outcome.sop_instance_uid.clone());
                self.received_bytes = self.received_bytes.saturating_add(outcome.file_bytes);
                match outcome.disposition {
                    ReceiveDisposition::Published => {
                        self.published = self.published.saturating_add(1);
                    }
                    ReceiveDisposition::ExistingSkipped => {
                        self.existing_skipped = self.existing_skipped.saturating_add(1);
                    }
                    ReceiveDisposition::ConflictPreserved => {
                        self.conflicts = self.conflicts.saturating_add(1);
                    }
                }
                self.store_activity_seen = true;
                Ok(true)
            }
            StorageScpEvent::StoreFailed { .. } => {
                self.store_failures = self.store_failures.saturating_add(1);
                self.store_activity_seen = true;
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
        let relative_directory = PathBuf::from(safe_accession_component(accession));
        prepare_accession_directory(destination, &relative_directory).await?;
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
        result.locally_received_operations = u64_to_u32(stats.store_operations);
        result.locally_unique_sop_instances = usize_to_u32(stats.unique_sop_instances.len());

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
            &destination.join(relative_directory),
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

async fn prepare_accession_directory(root: &Path, relative: &Path) -> anyhow::Result<()> {
    debug_assert_eq!(relative.components().count(), 1);
    let target = root.join(relative);
    match tokio::fs::symlink_metadata(&target).await {
        Ok(metadata) if metadata.file_type().is_symlink() => {
            bail!(
                "refusing accession destination symlink {}",
                target.display()
            );
        }
        Ok(metadata) if !metadata.is_dir() => {
            bail!(
                "accession destination is not a directory: {}",
                target.display()
            );
        }
        Ok(_) => Ok(()),
        Err(error) if error.kind() == ErrorKind::NotFound => {
            tokio::fs::create_dir(&target).await.with_context(|| {
                format!("failed to create accession directory {}", target.display())
            })?;
            let metadata = tokio::fs::symlink_metadata(&target)
                .await
                .with_context(|| {
                    format!("failed to inspect accession directory {}", target.display())
                })?;
            if metadata.file_type().is_symlink() || !metadata.is_dir() {
                bail!("accession destination changed while it was being created");
            }
            Ok(())
        }
        Err(error) => Err(error)
            .with_context(|| format!("failed to inspect accession directory {}", target.display())),
    }
}

fn safe_accession_component(accession: &str) -> String {
    let mut encoded = String::from("accession-");
    for byte in accession.as_bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_') {
            encoded.push(char::from(*byte));
        } else {
            encoded.push('~');
            encoded.push(hex_digit(byte >> 4));
            encoded.push(hex_digit(byte & 0x0F));
        }
    }
    if encoded.len() <= MAX_COMPONENT_BYTES {
        return encoded;
    }

    let digest = Sha256::digest(accession.as_bytes());
    encoded.truncate(COMPONENT_PREFIX_BYTES);
    encoded.push_str("-sha256-");
    for byte in &digest[..16] {
        encoded.push(hex_digit(byte >> 4));
        encoded.push(hex_digit(byte & 0x0F));
    }
    encoded
}

fn hex_digit(value: u8) -> char {
    char::from(b"0123456789ABCDEF"[usize::from(value)])
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
    if let Some((expected, received)) = result.local_count_gap() {
        reasons.push(format!(
            "PACS reported {expected} completed suboperation(s), but {received} reached local storage"
        ));
    }
    if let Some(expected) = result.counters.completed {
        let unique_received = usize_to_u32(stats.unique_sop_instances.len());
        if unique_received < expected {
            reasons.push(format!(
                "PACS reported {expected} completed suboperation(s), but only {unique_received} unique SOP instance(s) were stored"
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
        json!({
            "type": "accession",
            "accession": accession,
            "success": failure_reasons.is_empty(),
            "no_data": result.counters.completed == Some(0) && stats.store_operations == 0,
            "output_directory": output_directory,
            "move_final_status": result.final_status.map(|status| format!("0x{:04X}", status.code)),
            "move_status_class": result.final_status.map(|status| format!("{:?}", status.class)),
            "pacs_remaining": result.counters.remaining,
            "pacs_completed": result.counters.completed,
            "pacs_failed": result.counters.failed,
            "pacs_warning": result.counters.warning,
            "local_store_operations": stats.store_operations,
            "local_unique_sop_instances": stats.unique_sop_instances.len(),
            "published": stats.published,
            "existing_skipped": stats.existing_skipped,
            "conflicts": stats.conflicts,
            "store_failures": stats.store_failures,
            "storage_association_failures": stats.association_failures,
            "received_bytes": stats.received_bytes,
            "late_store_drain": match drain {
                DrainOutcome::Drained => "drained",
                DrainOutcome::TimedOut => "timed_out",
            },
            "failures": failure_reasons,
        })
    );
}

fn usize_to_u32(value: usize) -> u32 {
    u32::try_from(value).unwrap_or(u32::MAX)
}

fn u64_to_u32(value: u64) -> u32 {
    u32::try_from(value).unwrap_or(u32::MAX)
}

#[cfg(test)]
mod tests {
    use super::*;
    use dcmget_dicom::{MoveCounters, MoveFinalStatus};

    fn route(accession: &str) -> ReceiveRoute {
        ReceiveRoute {
            profile_id: PROFILE_ID.to_owned(),
            task_id: "task-1".to_owned(),
            accession_number: accession.to_owned(),
            destination_root: PathBuf::from("destination"),
        }
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

    #[test]
    fn accession_component_cannot_traverse_or_create_windows_device_names() {
        for accession in [
            "../outside",
            "..\\outside",
            "/absolute",
            "CON",
            "A:B",
            "检查号/一",
        ] {
            let component = safe_accession_component(accession);
            assert!(component.starts_with("accession-"));
            assert_eq!(Path::new(&component).components().count(), 1);
            assert!(!component.contains('/'));
            assert!(!component.contains('\\'));
            assert_ne!(component, ".");
            assert_ne!(component, "..");
        }
    }

    #[test]
    fn long_accession_component_is_bounded_and_collision_resistant() {
        let first = safe_accession_component(&"A".repeat(1_000));
        let second = safe_accession_component(&format!("{}B", "A".repeat(999)));
        assert!(first.len() <= MAX_COMPONENT_BYTES);
        assert!(second.len() <= MAX_COMPONENT_BYTES);
        assert_ne!(first, second);
        assert!(first.contains("-sha256-"));
    }

    #[test]
    fn receive_route_remains_active_for_the_full_lease() {
        let resolver = ActiveRouteResolver::default();
        let expected_route = route("../../A001");
        let lease = resolver
            .activate(RouteTarget {
                route: expected_route.clone(),
                relative_directory: PathBuf::from(safe_accession_component("../../A001")),
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
        let resolver = ActiveRouteResolver::default();
        let lease = resolver
            .activate(RouteTarget {
                route: route("A001"),
                relative_directory: PathBuf::from(safe_accession_component("A001")),
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
    fn missing_pacs_count_and_duplicate_sop_deliveries_are_not_complete() {
        let stats = ReceiveStats::default();
        let missing_count = failure_reasons(&successful_move(None), &stats, DrainOutcome::Drained);
        assert!(
            missing_count
                .iter()
                .any(|reason| reason.contains("omitted the completed count"))
        );

        let mut duplicate_stats = ReceiveStats {
            store_operations: 2,
            ..ReceiveStats::default()
        };
        duplicate_stats
            .unique_sop_instances
            .insert("1.2.3.4".to_owned());
        let duplicate_delivery = failure_reasons(
            &successful_move(Some(2)),
            &duplicate_stats,
            DrainOutcome::Drained,
        );
        assert!(
            duplicate_delivery
                .iter()
                .any(|reason| reason.contains("only 1 unique SOP"))
        );
    }

    #[tokio::test]
    async fn explicit_receiver_shutdown_releases_the_bound_port() {
        let resolver = ActiveRouteResolver::default();
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
