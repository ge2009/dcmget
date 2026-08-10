use std::collections::hash_map::DefaultHasher;
use std::fs::{self, File, OpenOptions};
use std::hash::{Hash, Hasher};
use std::io::{self, Read, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use sha2::{Digest, Sha256};
use tokio::sync::{mpsc, oneshot};
use tokio::task::JoinHandle;

use crate::cancel::CancellationToken;
use crate::model::{
    QuarantineStoreRequest, ReceiveDisposition, ReceiveOutcome, Sha256Digest, StoreRequest,
};
use crate::part10::{FileMeta, Part10Error, build_part10_header};

pub const C_STORE_SUCCESS: u16 = 0x0000;
pub const C_STORE_FAILURE_CANNOT_UNDERSTAND: u16 = 0xC000;
const COPY_BUFFER_BYTES: usize = 1024 * 1024;
// One acknowledged chunk may be queued while the blocking worker writes the
// previous chunk. This keeps memory bounded and makes disk/SMB throughput apply
// backpressure to DIMSE receive instead of accumulating a complete object.
const FILE_WORKER_QUEUE_DEPTH: usize = 1;
const PUBLICATION_LOCK_SHARDS: usize = 256;
const QUARANTINE_DIRECTORY: &str = "_DcmGetQuarantine";

/// Boundary required from the future dicom-ul association adapter.
///
/// dicom-rs 0.10's promiscuous mode still checks transfer-syntax support. The
/// final adapter must inject this policy into a small pinned dicom-ul patch so
/// storage payloads can be preserved without requiring a pixel decoder.
pub trait TransferSyntaxAcceptancePolicy: Send + Sync {
    fn accepts(&self, abstract_syntax_uid: &str, transfer_syntax_uid: &str) -> bool;
}

#[derive(Debug, Clone, Copy, Default)]
pub struct RawTransferSyntaxPolicy;

impl TransferSyntaxAcceptancePolicy for RawTransferSyntaxPolicy {
    fn accepts(&self, abstract_syntax_uid: &str, transfer_syntax_uid: &str) -> bool {
        valid_uid_shape(abstract_syntax_uid) && valid_uid_shape(transfer_syntax_uid)
    }
}

#[async_trait]
pub trait StorePayloadSink: Send + Sync {
    type Session: StorePayloadSession;

    async fn begin_store(
        &self,
        request: StoreRequest,
        cancellation: CancellationToken,
    ) -> Result<Self::Session, StoreError>;

    async fn begin_quarantine(
        &self,
        request: QuarantineStoreRequest,
        cancellation: CancellationToken,
    ) -> Result<Self::Session, StoreError>;
}

#[async_trait]
pub trait StorePayloadSession: Send {
    async fn write_dataset_chunk(&mut self, chunk: &[u8]) -> Result<(), StoreError>;
    async fn finish(self) -> Result<ReceiveOutcome, StoreError>;
    async fn abort(self) -> Result<(), StoreError>;
}

#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error(transparent)]
    Part10(#[from] Part10Error),
    #[error("operation cancelled")]
    Cancelled,
    #[error("received an empty DICOM dataset")]
    EmptyDataset,
    #[error("destination path must be relative and may not contain '..': {0}")]
    UnsafeRelativePath(PathBuf),
    #[error("destination directory contains a symbolic link or reparse point: {0}")]
    UnsafeDestinationLink(PathBuf),
    #[error("destination path component is not a directory: {0}")]
    DestinationNotDirectory(PathBuf),
    #[error("destination path resolves outside the configured root: {0}")]
    DestinationOutsideRoot(PathBuf),
    #[error("store session is already closed")]
    SessionClosed,
    #[error("file operation failed for {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: io::Error,
    },
    #[error("internal publication lock was poisoned")]
    LockPoisoned,
    #[error("file worker failed: {message}")]
    Worker { message: String },
}

impl StoreError {
    #[must_use]
    pub fn recommended_c_store_status(&self) -> u16 {
        C_STORE_FAILURE_CANNOT_UNDERSTAND
    }

    /// Stable event text which deliberately excludes filesystem paths, SOP
    /// identifiers, and worker details which may contain patient-correlated
    /// metadata. Full errors remain available to the local caller.
    pub(crate) fn redacted_event_message(&self) -> &'static str {
        match self {
            Self::Part10(_) => "invalid DICOM Part 10 metadata",
            Self::Cancelled => "store operation was cancelled",
            Self::EmptyDataset => "received an empty DICOM dataset",
            Self::UnsafeRelativePath(_) => "unsafe routed destination path",
            Self::UnsafeDestinationLink(_) => "destination contains a link or reparse point",
            Self::DestinationNotDirectory(_) => "destination component is not a directory",
            Self::DestinationOutsideRoot(_) => "destination resolves outside its configured root",
            Self::SessionClosed => "store session closed before completion",
            Self::Io { .. } => "filesystem operation failed",
            Self::LockPoisoned => "internal publication lock failed",
            Self::Worker { .. } => "file worker failed",
        }
    }
}

#[derive(Clone)]
pub struct FileStore {
    inner: Arc<FileStoreInner>,
}

struct FileStoreInner {
    sequence: AtomicU64,
    publication_locks: Box<[Mutex<()>]>,
}

impl Default for FileStore {
    fn default() -> Self {
        Self::new()
    }
}

impl FileStore {
    #[must_use]
    pub fn new() -> Self {
        Self {
            inner: Arc::new(FileStoreInner {
                sequence: AtomicU64::new(0),
                publication_locks: (0..PUBLICATION_LOCK_SHARDS)
                    .map(|_| Mutex::new(()))
                    .collect(),
            }),
        }
    }

    fn next_sequence(&self) -> u64 {
        self.inner.sequence.fetch_add(1, Ordering::Relaxed)
    }

    fn publication_shard(target: &Path) -> usize {
        let mut hasher = DefaultHasher::new();
        target.hash(&mut hasher);
        usize::try_from(hasher.finish() % PUBLICATION_LOCK_SHARDS as u64).unwrap_or(0)
    }

    async fn begin_request(
        &self,
        request: FileStoreRequest,
        cancellation: CancellationToken,
    ) -> Result<FileStoreSession, StoreError> {
        if cancellation.is_cancelled() {
            return Err(StoreError::Cancelled);
        }
        validate_relative_path(&request.relative_directory())?;
        let (commands, command_receiver) = mpsc::channel(FILE_WORKER_QUEUE_DEPTH);
        let (ready_sender, ready_receiver) = oneshot::channel();
        let store = self.clone();
        let worker = tokio::task::spawn_blocking(move || {
            let result = FileStoreWorker::new(store, request, cancellation);
            match result {
                Ok(worker) => {
                    if ready_sender.send(Ok(())).is_ok() {
                        worker.run(command_receiver);
                    }
                }
                Err(error) => {
                    let _ = ready_sender.send(Err(error));
                }
            }
        });
        match ready_receiver.await {
            Ok(Ok(())) => Ok(FileStoreSession {
                commands,
                worker: Some(worker),
            }),
            Ok(Err(error)) => {
                join_file_worker(worker).await?;
                Err(error)
            }
            Err(error) => {
                let join_result = join_file_worker(worker).await;
                Err(join_result.err().unwrap_or_else(|| StoreError::Worker {
                    message: format!("startup response channel closed: {error}"),
                }))
            }
        }
    }
}

#[async_trait]
impl StorePayloadSink for FileStore {
    type Session = FileStoreSession;

    async fn begin_store(
        &self,
        request: StoreRequest,
        cancellation: CancellationToken,
    ) -> Result<Self::Session, StoreError> {
        self.begin_request(FileStoreRequest::Routed(request), cancellation)
            .await
    }

    async fn begin_quarantine(
        &self,
        request: QuarantineStoreRequest,
        cancellation: CancellationToken,
    ) -> Result<Self::Session, StoreError> {
        validate_safe_component(&request.target.profile_id)?;
        self.begin_request(FileStoreRequest::Quarantine(request), cancellation)
            .await
    }
}

pub struct FileStoreSession {
    commands: mpsc::Sender<FileWorkerCommand>,
    worker: Option<JoinHandle<()>>,
}

enum FileWorkerCommand {
    Write {
        chunk: Vec<u8>,
        response: oneshot::Sender<Result<(), StoreError>>,
    },
    Finish {
        response: oneshot::Sender<Result<ReceiveOutcome, StoreError>>,
    },
    Abort {
        response: oneshot::Sender<Result<(), StoreError>>,
    },
}

#[derive(Clone)]
enum FileStoreRequest {
    Routed(StoreRequest),
    Quarantine(QuarantineStoreRequest),
}

impl FileStoreRequest {
    fn destination_root(&self) -> &Path {
        match self {
            Self::Routed(request) => &request.route.destination_root,
            Self::Quarantine(request) => &request.target.destination_root,
        }
    }

    fn relative_directory(&self) -> PathBuf {
        match self {
            Self::Routed(request) => request.relative_directory.clone(),
            Self::Quarantine(request) => {
                PathBuf::from(QUARANTINE_DIRECTORY).join(&request.target.profile_id)
            }
        }
    }

    fn sop_class_uid(&self) -> &str {
        match self {
            Self::Routed(request) => &request.sop_class_uid,
            Self::Quarantine(request) => &request.sop_class_uid,
        }
    }

    fn sop_instance_uid(&self) -> &str {
        match self {
            Self::Routed(request) => &request.sop_instance_uid,
            Self::Quarantine(request) => &request.sop_instance_uid,
        }
    }

    fn transfer_syntax_uid(&self) -> &str {
        match self {
            Self::Routed(request) => &request.transfer_syntax_uid,
            Self::Quarantine(request) => &request.transfer_syntax_uid,
        }
    }
}

struct FileStoreWorker {
    store: FileStore,
    request: FileStoreRequest,
    cancellation: CancellationToken,
    part_path: Option<PathBuf>,
    publication_sequence: u64,
    file: Option<File>,
    file_hasher: Option<Sha256>,
    dataset_hasher: Option<Sha256>,
    header_bytes: u64,
    dataset_bytes: u64,
}

type PublishedFile = (ReceiveDisposition, PathBuf, Sha256Digest, u64);

impl FileStoreWorker {
    fn new(
        store: FileStore,
        request: FileStoreRequest,
        cancellation: CancellationToken,
    ) -> Result<Self, StoreError> {
        if cancellation.is_cancelled() {
            return Err(StoreError::Cancelled);
        }
        let meta = FileMeta {
            media_storage_sop_class_uid: request.sop_class_uid().to_owned(),
            media_storage_sop_instance_uid: request.sop_instance_uid().to_owned(),
            transfer_syntax_uid: request.transfer_syntax_uid().to_owned(),
        };
        let header = build_part10_header(&meta)?;
        let relative_directory = request.relative_directory();
        let staging_directory = ensure_safe_destination_directory(
            request.destination_root(),
            &relative_directory.join(".dcmget-staging"),
        )?;
        let (part_path, mut file, publication_sequence) =
            create_unique_part(&staging_directory, store.next_sequence())?;
        if let Err(error) = file.write_all(&header) {
            let _ = fs::remove_file(&part_path);
            return Err(io_error(part_path, error));
        }
        let mut hasher = Sha256::new();
        hasher.update(&header);
        Ok(Self {
            store,
            request,
            cancellation,
            part_path: Some(part_path),
            publication_sequence,
            file: Some(file),
            file_hasher: Some(hasher),
            dataset_hasher: Some(Sha256::new()),
            header_bytes: u64::try_from(header.len()).unwrap_or(u64::MAX),
            dataset_bytes: 0,
        })
    }

    fn run(mut self, mut commands: mpsc::Receiver<FileWorkerCommand>) {
        while let Some(command) = commands.blocking_recv() {
            match command {
                FileWorkerCommand::Write { chunk, response } => {
                    let _ = response.send(self.write_dataset_chunk(&chunk));
                }
                FileWorkerCommand::Finish { response } => {
                    let _ = response.send(self.publish());
                    return;
                }
                FileWorkerCommand::Abort { response } => {
                    let _ = response.send(self.cleanup_part());
                    return;
                }
            }
        }
        let _ = self.cleanup_part();
    }

    fn part_path(&self) -> Result<&Path, StoreError> {
        self.part_path.as_deref().ok_or(StoreError::SessionClosed)
    }

    fn cleanup_part(&mut self) -> Result<(), StoreError> {
        self.file.take();
        if let Some(path) = self.part_path.take() {
            match fs::remove_file(&path) {
                Ok(()) => Ok(()),
                Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
                Err(error) => Err(io_error(path, error)),
            }
        } else {
            Ok(())
        }
    }

    fn publish(mut self) -> Result<ReceiveOutcome, StoreError> {
        if self.cancellation.is_cancelled() {
            self.cleanup_part()?;
            return Err(StoreError::Cancelled);
        }
        if self.dataset_bytes == 0 {
            self.cleanup_part()?;
            return Err(StoreError::EmptyDataset);
        }
        let part_path = self.part_path()?.to_path_buf();
        let mut file = self.file.take().ok_or(StoreError::SessionClosed)?;
        file.flush()
            .map_err(|error| io_error(part_path.clone(), error))?;
        file.sync_all()
            .map_err(|error| io_error(part_path.clone(), error))?;
        drop(file);
        if self.cancellation.is_cancelled() {
            self.cleanup_part()?;
            return Err(StoreError::Cancelled);
        }
        let incoming_digest = Sha256Digest(
            self.file_hasher
                .take()
                .ok_or(StoreError::SessionClosed)?
                .finalize()
                .into(),
        );
        let incoming_dataset_digest = Sha256Digest(
            self.dataset_hasher
                .take()
                .ok_or(StoreError::SessionClosed)?
                .finalize()
                .into(),
        );
        let file_bytes = self.header_bytes.saturating_add(self.dataset_bytes);
        let request = self.request.clone();
        let (disposition, published_path, published_digest, published_bytes) = match &request {
            FileStoreRequest::Routed(request) => self.publish_routed(
                request,
                &part_path,
                incoming_digest,
                incoming_dataset_digest,
                file_bytes,
            )?,
            FileStoreRequest::Quarantine(request) => {
                self.publish_quarantine(request, &part_path, incoming_digest, file_bytes)?
            }
        };
        self.part_path = None;
        Ok(ReceiveOutcome {
            disposition,
            path: published_path,
            sop_instance_uid: self.request.sop_instance_uid().to_owned(),
            sha256: published_digest,
            file_bytes: published_bytes,
            dataset_bytes: self.dataset_bytes,
        })
    }

    fn publish_routed(
        &mut self,
        request: &StoreRequest,
        part_path: &Path,
        incoming_digest: Sha256Digest,
        incoming_dataset_digest: Sha256Digest,
        file_bytes: u64,
    ) -> Result<PublishedFile, StoreError> {
        let target_directory = ensure_safe_destination_directory(
            &request.route.destination_root,
            &request.relative_directory,
        )?;
        let target = target_directory.join(format!("{}.dcm", request.sop_instance_uid));
        let lock_shard = FileStore::publication_shard(&target);
        let store_inner = Arc::clone(&self.store.inner);
        let _guard = store_inner.publication_locks[lock_shard]
            .lock()
            .map_err(|_| StoreError::LockPoisoned)?;
        if self.cancellation.is_cancelled() {
            self.cleanup_part()?;
            return Err(StoreError::Cancelled);
        }

        if target.exists() {
            let existing = digest_part10_file(&target)?;
            if let Some(existing) = existing.filter(|existing| {
                existing.dataset_digest == incoming_dataset_digest
                    && existing.dataset_bytes == self.dataset_bytes
            }) {
                remove_file(part_path)?;
                Ok((
                    ReceiveDisposition::ExistingSkipped,
                    target,
                    existing.file_digest,
                    existing.file_bytes,
                ))
            } else {
                let conflict_directory = ensure_safe_destination_directory(
                    &request.route.destination_root,
                    Path::new("_DcmGetConflicts"),
                )?;
                let conflict = reserve_conflict_path(
                    &conflict_directory,
                    &request.sop_instance_uid,
                    self.store.next_sequence(),
                )?;
                rename(part_path, &conflict)?;
                sync_directory_best_effort(&conflict_directory);
                Ok((
                    ReceiveDisposition::ConflictPreserved,
                    conflict,
                    incoming_digest,
                    file_bytes,
                ))
            }
        } else {
            rename(part_path, &target)?;
            sync_directory_best_effort(&target_directory);
            Ok((
                ReceiveDisposition::Published,
                target,
                incoming_digest,
                file_bytes,
            ))
        }
    }

    fn publish_quarantine(
        &self,
        request: &QuarantineStoreRequest,
        part_path: &Path,
        incoming_digest: Sha256Digest,
        file_bytes: u64,
    ) -> Result<PublishedFile, StoreError> {
        let relative_directory =
            PathBuf::from(QUARANTINE_DIRECTORY).join(&request.target.profile_id);
        let target_directory = ensure_safe_destination_directory(
            &request.target.destination_root,
            &relative_directory,
        )?;
        let target = persist_quarantine(
            part_path,
            &target_directory,
            &request.sop_instance_uid,
            self.publication_sequence,
        )?;
        sync_directory_best_effort(&target_directory);
        Ok((
            ReceiveDisposition::Quarantined,
            target,
            incoming_digest,
            file_bytes,
        ))
    }

    fn write_dataset_chunk(&mut self, chunk: &[u8]) -> Result<(), StoreError> {
        if self.cancellation.is_cancelled() {
            return Err(StoreError::Cancelled);
        }
        let part_path = self.part_path()?.to_path_buf();
        let file = self.file.as_mut().ok_or(StoreError::SessionClosed)?;
        file.write_all(chunk)
            .map_err(|error| io_error(part_path, error))?;
        self.file_hasher
            .as_mut()
            .ok_or(StoreError::SessionClosed)?
            .update(chunk);
        self.dataset_hasher
            .as_mut()
            .ok_or(StoreError::SessionClosed)?
            .update(chunk);
        self.dataset_bytes = self
            .dataset_bytes
            .saturating_add(u64::try_from(chunk.len()).unwrap_or(u64::MAX));
        Ok(())
    }
}

#[async_trait]
impl StorePayloadSession for FileStoreSession {
    async fn write_dataset_chunk(&mut self, chunk: &[u8]) -> Result<(), StoreError> {
        let (response, result) = oneshot::channel();
        self.commands
            .send(FileWorkerCommand::Write {
                chunk: chunk.to_vec(),
                response,
            })
            .await
            .map_err(|_| StoreError::Worker {
                message: "file worker stopped before accepting a dataset chunk".to_owned(),
            })?;
        result.await.map_err(|_| StoreError::Worker {
            message: "file worker stopped before acknowledging a dataset chunk".to_owned(),
        })?
    }

    async fn finish(mut self) -> Result<ReceiveOutcome, StoreError> {
        let (response, result) = oneshot::channel();
        self.commands
            .send(FileWorkerCommand::Finish { response })
            .await
            .map_err(|_| StoreError::Worker {
                message: "file worker stopped before publication".to_owned(),
            })?;
        let result = result.await.map_err(|_| StoreError::Worker {
            message: "file worker stopped without a publication result".to_owned(),
        })?;
        if let Some(worker) = self.worker.take() {
            join_file_worker(worker).await?;
        }
        result
    }

    async fn abort(mut self) -> Result<(), StoreError> {
        let (response, result) = oneshot::channel();
        self.commands
            .send(FileWorkerCommand::Abort { response })
            .await
            .map_err(|_| StoreError::Worker {
                message: "file worker stopped before abort cleanup".to_owned(),
            })?;
        let result = result.await.map_err(|_| StoreError::Worker {
            message: "file worker stopped without an abort result".to_owned(),
        })?;
        if let Some(worker) = self.worker.take() {
            join_file_worker(worker).await?;
        }
        result
    }
}

impl Drop for FileStoreWorker {
    fn drop(&mut self) {
        let _ = self.cleanup_part();
    }
}

async fn join_file_worker(worker: JoinHandle<()>) -> Result<(), StoreError> {
    worker.await.map_err(|error| StoreError::Worker {
        message: error.to_string(),
    })
}

fn validate_relative_path(path: &Path) -> Result<(), StoreError> {
    if path
        .components()
        .any(|component| !matches!(component, Component::Normal(_)))
        && !path.as_os_str().is_empty()
    {
        return Err(StoreError::UnsafeRelativePath(path.to_path_buf()));
    }
    Ok(())
}

fn validate_safe_component(value: &str) -> Result<(), StoreError> {
    let path = Path::new(value);
    let mut components = path.components();
    if value.is_empty()
        || !matches!(components.next(), Some(Component::Normal(_)))
        || components.next().is_some()
    {
        return Err(StoreError::UnsafeRelativePath(path.to_path_buf()));
    }
    Ok(())
}

fn valid_uid_shape(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && !value.starts_with('.')
        && !value.ends_with('.')
        && value.split('.').all(|component| {
            !component.is_empty() && component.bytes().all(|byte| byte.is_ascii_digit())
        })
}

fn create_unique_part(
    directory: &Path,
    first_sequence: u64,
) -> Result<(PathBuf, File, u64), StoreError> {
    for offset in 0..1_000_u64 {
        let path = directory.join(format!(
            ".incoming-{}-{}.part",
            std::process::id(),
            first_sequence.saturating_add(offset)
        ));
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(file) => {
                return Ok((path, file, first_sequence.saturating_add(offset)));
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(io_error(path, error)),
        }
    }
    Err(io_error(
        directory.to_path_buf(),
        io::Error::new(
            io::ErrorKind::AlreadyExists,
            "could not reserve unique staging file",
        ),
    ))
}

fn quarantine_path(directory: &Path, sop_uid: &str, sequence: u64) -> PathBuf {
    directory.join(format!(
        "{sop_uid}-{}-{sequence}.dcm.quarantine",
        std::process::id()
    ))
}

/// Atomically publish on the same volume without replacing any existing file.
/// `TempPath::persist_noclobber` uses a no-replace rename where available and a
/// same-volume hard-link/unlink fallback elsewhere.
fn persist_quarantine(
    source: &Path,
    directory: &Path,
    sop_uid: &str,
    first_sequence: u64,
) -> Result<PathBuf, StoreError> {
    let mut temporary = tempfile::TempPath::try_from_path(source.to_path_buf())
        .map_err(|error| io_error(source.to_path_buf(), error))?;
    for offset in 0..1_000_u64 {
        let target = quarantine_path(directory, sop_uid, first_sequence.saturating_add(offset));
        match temporary.persist_noclobber(&target) {
            Ok(()) => return Ok(target),
            Err(error) if error.error.kind() == io::ErrorKind::AlreadyExists => {
                temporary = error.path;
            }
            Err(error) => {
                return Err(io_error(target, error.error));
            }
        }
    }
    Err(io_error(
        directory.to_path_buf(),
        io::Error::new(
            io::ErrorKind::AlreadyExists,
            "could not reserve unique quarantine file",
        ),
    ))
}

fn reserve_conflict_path(
    directory: &Path,
    sop_uid: &str,
    first_sequence: u64,
) -> Result<PathBuf, StoreError> {
    for offset in 0..1_000_u64 {
        let sequence = first_sequence.saturating_add(offset);
        let conflict = directory.join(format!("{sop_uid}-{}-{sequence}.dcm", std::process::id()));
        let reservation = conflict.with_extension("reserve");
        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&reservation)
        {
            Ok(file) => {
                drop(file);
                if conflict.exists() {
                    let _ = fs::remove_file(&reservation);
                    continue;
                }
                fs::remove_file(&reservation).map_err(|error| io_error(reservation, error))?;
                return Ok(conflict);
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(io_error(reservation, error)),
        }
    }
    Err(io_error(
        directory.to_path_buf(),
        io::Error::new(
            io::ErrorKind::AlreadyExists,
            "could not reserve unique conflict file",
        ),
    ))
}

fn ensure_safe_destination_directory(
    destination_root: &Path,
    relative_directory: &Path,
) -> Result<PathBuf, StoreError> {
    validate_relative_path(relative_directory)?;
    match fs::create_dir_all(destination_root) {
        Ok(()) => {}
        Err(error) => return Err(io_error(destination_root.to_path_buf(), error)),
    }
    validate_destination_directory(destination_root)?;
    let canonical_root = fs::canonicalize(destination_root)
        .map_err(|error| io_error(destination_root.to_path_buf(), error))?;
    let mut directory = destination_root.to_path_buf();
    for component in relative_directory.components() {
        let Component::Normal(component) = component else {
            return Err(StoreError::UnsafeRelativePath(
                relative_directory.to_path_buf(),
            ));
        };
        directory.push(component);
        match fs::create_dir(&directory) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(io_error(directory, error)),
        }
        validate_destination_directory(&directory)?;
        let canonical_directory =
            fs::canonicalize(&directory).map_err(|error| io_error(directory.clone(), error))?;
        if !canonical_directory.starts_with(&canonical_root) {
            return Err(StoreError::DestinationOutsideRoot(directory));
        }
    }
    Ok(directory)
}

fn validate_destination_directory(path: &Path) -> Result<(), StoreError> {
    let metadata =
        fs::symlink_metadata(path).map_err(|error| io_error(path.to_path_buf(), error))?;
    if is_link_or_reparse_point(&metadata) {
        return Err(StoreError::UnsafeDestinationLink(path.to_path_buf()));
    }
    if !metadata.is_dir() {
        return Err(StoreError::DestinationNotDirectory(path.to_path_buf()));
    }
    Ok(())
}

#[cfg(not(windows))]
fn is_link_or_reparse_point(metadata: &fs::Metadata) -> bool {
    metadata.file_type().is_symlink()
}

#[cfg(windows)]
fn is_link_or_reparse_point(metadata: &fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;

    const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0400;
    metadata.file_type().is_symlink()
        || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
}

fn remove_file(path: &Path) -> Result<(), StoreError> {
    fs::remove_file(path).map_err(|error| io_error(path.to_path_buf(), error))
}

fn rename(source: &Path, destination: &Path) -> Result<(), StoreError> {
    fs::rename(source, destination).map_err(|error| StoreError::Io {
        path: destination.to_path_buf(),
        source: error,
    })
}

struct ExistingPart10Digest {
    file_digest: Sha256Digest,
    dataset_digest: Sha256Digest,
    file_bytes: u64,
    dataset_bytes: u64,
}

/// Return file and dataset digests in a single streaming pass.
///
/// Retry downloads produced by DCMTK, pynetdicom and this crate can carry
/// different implementation-identification fields while containing the exact
/// same negotiated dataset. Deduplication therefore compares this digest, but
/// the audit outcome still reports the full-file digest.
fn digest_part10_file(path: &Path) -> Result<Option<ExistingPart10Digest>, StoreError> {
    let mut file = File::open(path).map_err(|error| io_error(path.to_path_buf(), error))?;
    let mut prefix = [0_u8; 144];
    if let Err(error) = file.read_exact(&mut prefix) {
        if error.kind() == io::ErrorKind::UnexpectedEof {
            return Ok(None);
        }
        return Err(io_error(path.to_path_buf(), error));
    }
    if &prefix[128..132] != b"DICM"
        || prefix[132..136] != [0x02, 0x00, 0x00, 0x00]
        || &prefix[136..138] != b"UL"
        || u16::from_le_bytes([prefix[138], prefix[139]]) != 4
    {
        return Ok(None);
    }
    let group_length = u64::from(u32::from_le_bytes(prefix[140..144].try_into().unwrap()));
    let dataset_offset = 144_u64.saturating_add(group_length);
    let file_size = file
        .metadata()
        .map_err(|error| io_error(path.to_path_buf(), error))?
        .len();
    if dataset_offset > file_size {
        return Ok(None);
    }
    let mut file_hasher = Sha256::new();
    file_hasher.update(prefix);
    let mut dataset_hasher = Sha256::new();
    let mut buffer = vec![0_u8; COPY_BUFFER_BYTES];
    let mut remaining_meta = group_length;
    while remaining_meta > 0 {
        let requested = usize::try_from(remaining_meta)
            .unwrap_or(usize::MAX)
            .min(buffer.len());
        let count = file
            .read(&mut buffer[..requested])
            .map_err(|error| io_error(path.to_path_buf(), error))?;
        if count == 0 {
            return Ok(None);
        }
        file_hasher.update(&buffer[..count]);
        remaining_meta = remaining_meta.saturating_sub(u64::try_from(count).unwrap_or(u64::MAX));
    }
    let mut dataset_bytes = 0_u64;
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|error| io_error(path.to_path_buf(), error))?;
        if count == 0 {
            break;
        }
        file_hasher.update(&buffer[..count]);
        dataset_hasher.update(&buffer[..count]);
        dataset_bytes = dataset_bytes.saturating_add(u64::try_from(count).unwrap_or(u64::MAX));
    }
    Ok(Some(ExistingPart10Digest {
        file_digest: Sha256Digest(file_hasher.finalize().into()),
        dataset_digest: Sha256Digest(dataset_hasher.finalize().into()),
        file_bytes: file_size,
        dataset_bytes,
    }))
}

fn io_error(path: PathBuf, source: io::Error) -> StoreError {
    StoreError::Io { path, source }
}

#[cfg(unix)]
fn sync_directory_best_effort(path: &Path) {
    if let Ok(directory) = File::open(path) {
        let _ = directory.sync_all();
    }
}

#[cfg(not(unix))]
fn sync_directory_best_effort(_path: &Path) {}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::time::Duration;

    use tokio::sync::Barrier;

    use super::*;
    use crate::model::{
        DicomEndpoint, MoveRequest, QuarantineStoreRequest, QuarantineTarget, ReceiveRoute,
    };

    fn temporary_root(test: &str) -> PathBuf {
        static TEST_SEQUENCE: AtomicU64 = AtomicU64::new(0);
        let root = std::env::temp_dir().join(format!(
            "dcmget-dicom-{test}-{}-{}",
            std::process::id(),
            TEST_SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        root
    }

    fn request(root: &Path, sop_uid: &str) -> StoreRequest {
        StoreRequest {
            route: ReceiveRoute {
                profile_id: "profile-1".to_owned(),
                task_id: "task-1".to_owned(),
                accession_number: "ACC001".to_owned(),
                destination_root: root.to_path_buf(),
            },
            relative_directory: PathBuf::from("PAT001/ACC001/1.2.3"),
            sop_class_uid: "1.2.840.10008.5.1.4.1.1.2".to_owned(),
            sop_instance_uid: sop_uid.to_owned(),
            transfer_syntax_uid: "1.2.840.10008.1.2.1".to_owned(),
        }
    }

    fn quarantine_request(root: &Path, sop_uid: &str) -> QuarantineStoreRequest {
        QuarantineStoreRequest {
            target: QuarantineTarget {
                profile_id: "profile-1".to_owned(),
                destination_root: root.to_path_buf(),
                active_task_id: None,
            },
            sop_class_uid: "1.2.840.10008.5.1.4.1.1.2".to_owned(),
            sop_instance_uid: sop_uid.to_owned(),
            transfer_syntax_uid: "1.2.840.10008.1.2.1".to_owned(),
        }
    }

    async fn store_payload(
        store: &FileStore,
        request: StoreRequest,
        payload: &[u8],
    ) -> Result<ReceiveOutcome, StoreError> {
        let mut session = store.begin_store(request, CancellationToken::new()).await?;
        for chunk in payload.chunks(3) {
            session.write_dataset_chunk(chunk).await?;
        }
        session.finish().await
    }

    #[test]
    fn raw_policy_accepts_unknown_but_well_formed_transfer_syntax() {
        let policy = RawTransferSyntaxPolicy;
        assert!(policy.accepts("1.2.840.10008.5.1.4.1.1.2", "9.9.9.42"));
        assert!(!policy.accepts("CT Image Storage", "9.9.9.42"));
    }

    #[test]
    fn event_error_text_does_not_expose_destination_paths() {
        let error = StoreError::Io {
            path: PathBuf::from("patient-identifier/accession-identifier/object.part"),
            source: io::Error::other("device details"),
        };
        let message = error.redacted_event_message();
        assert_eq!(message, "filesystem operation failed");
        assert!(!message.contains("patient-identifier"));
        assert!(!message.contains("device details"));
    }

    #[tokio::test]
    async fn streams_part10_to_target_volume_and_atomically_publishes_dcm() {
        let root = temporary_root("publish");
        let store = FileStore::new();
        let payload = b"raw-negotiated-dataset-payload";
        let result = store_payload(&store, request(&root, "1.2.3.4"), payload)
            .await
            .unwrap();
        assert_eq!(result.disposition, ReceiveDisposition::Published);
        assert_eq!(result.path.extension().unwrap(), "dcm");
        let bytes = fs::read(&result.path).unwrap();
        assert_eq!(&bytes[128..132], b"DICM");
        assert!(bytes.ends_with(payload));
        assert_eq!(result.file_bytes, u64::try_from(bytes.len()).unwrap());
        assert_eq!(result.dataset_bytes, u64::try_from(payload.len()).unwrap());
        assert_eq!(result.recommended_c_store_status(), C_STORE_SUCCESS);
        assert!(
            !root
                .join("PAT001/ACC001/1.2.3/.dcmget-staging")
                .read_dir()
                .unwrap()
                .any(|_| true)
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn identical_sop_and_content_is_skipped() {
        let root = temporary_root("duplicate");
        let store = FileStore::new();
        let first = store_payload(&store, request(&root, "1.2.3.5"), b"same")
            .await
            .unwrap();
        let second = store_payload(&store, request(&root, "1.2.3.5"), b"same")
            .await
            .unwrap();
        assert_eq!(first.disposition, ReceiveDisposition::Published);
        assert_eq!(second.disposition, ReceiveDisposition::ExistingSkipped);
        assert_eq!(first.path, second.path);
        assert_eq!(first.sha256, second.sha256);
        assert!(fs::read_dir(root.join("_DcmGetConflicts")).is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn duplicate_compares_dataset_not_part10_implementation_fields() {
        let root = temporary_root("duplicate-meta");
        let store = FileStore::new();
        let first = store_payload(&store, request(&root, "1.2.3.51"), b"same")
            .await
            .unwrap();
        let mut bytes = fs::read(&first.path).unwrap();
        let implementation = bytes
            .windows(b"DCMGET_4_0".len())
            .position(|window| window == b"DCMGET_4_0")
            .expect("implementation version in Part 10 header");
        bytes[implementation..implementation + b"OTHER__4_0".len()].copy_from_slice(b"OTHER__4_0");
        fs::write(&first.path, bytes).unwrap();

        let second = store_payload(&store, request(&root, "1.2.3.51"), b"same")
            .await
            .unwrap();
        assert_eq!(second.disposition, ReceiveDisposition::ExistingSkipped);
        assert_eq!(first.path, second.path);
        fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn conflicting_sop_is_preserved_without_overwriting_target() {
        let root = temporary_root("conflict");
        let store = FileStore::new();
        let first = store_payload(&store, request(&root, "1.2.3.6"), b"first")
            .await
            .unwrap();
        let conflict = store_payload(&store, request(&root, "1.2.3.6"), b"second")
            .await
            .unwrap();
        assert_eq!(conflict.disposition, ReceiveDisposition::ConflictPreserved);
        assert_eq!(
            conflict.recommended_c_store_status(),
            C_STORE_FAILURE_CANNOT_UNDERSTAND
        );
        assert!(conflict.path.starts_with(root.join("_DcmGetConflicts")));
        assert!(fs::read(&first.path).unwrap().ends_with(b"first"));
        assert!(fs::read(&conflict.path).unwrap().ends_with(b"second"));
        fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn quarantine_publication_is_unique_part10_and_never_overwrites() {
        let root = temporary_root("quarantine-unique");
        let store = FileStore::new();
        let mut first = store
            .begin_quarantine(
                quarantine_request(&root, "1.2.3.61"),
                CancellationToken::new(),
            )
            .await
            .unwrap();
        first.write_dataset_chunk(b"first").await.unwrap();
        let first = first.finish().await.unwrap();
        let mut second = store
            .begin_quarantine(
                quarantine_request(&root, "1.2.3.61"),
                CancellationToken::new(),
            )
            .await
            .unwrap();
        second.write_dataset_chunk(b"second").await.unwrap();
        let second = second.finish().await.unwrap();

        assert_eq!(first.disposition, ReceiveDisposition::Quarantined);
        assert_eq!(second.disposition, ReceiveDisposition::Quarantined);
        assert_ne!(first.path, second.path);
        assert_eq!(first.path.extension().unwrap(), "quarantine");
        assert!(
            first
                .path
                .starts_with(root.join("_DcmGetQuarantine/profile-1"))
        );
        assert!(fs::read(&first.path).unwrap().ends_with(b"first"));
        assert!(fs::read(&second.path).unwrap().ends_with(b"second"));
        fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn concurrent_same_sop_never_overwrites() {
        let root = temporary_root("concurrent");
        let store = Arc::new(FileStore::new());
        let barrier = Arc::new(Barrier::new(2));
        let mut workers = Vec::new();
        for payload in [b"first".as_slice(), b"second".as_slice()] {
            let root = root.clone();
            let store = Arc::clone(&store);
            let barrier = Arc::clone(&barrier);
            let payload = payload.to_vec();
            workers.push(tokio::spawn(async move {
                barrier.wait().await;
                store_payload(&store, request(&root, "1.2.3.7"), &payload)
                    .await
                    .unwrap()
            }));
        }
        let mut outcomes = Vec::new();
        for worker in workers {
            outcomes.push(worker.await.unwrap());
        }
        assert_eq!(
            outcomes
                .iter()
                .filter(|outcome| outcome.disposition == ReceiveDisposition::Published)
                .count(),
            1
        );
        assert_eq!(
            outcomes
                .iter()
                .filter(|outcome| outcome.disposition == ReceiveDisposition::ConflictPreserved)
                .count(),
            1
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn cancellation_and_abort_remove_partial_files() {
        let root = temporary_root("cancel");
        let store = FileStore::new();
        let cancellation = CancellationToken::new();
        let mut session = store
            .begin_store(request(&root, "1.2.3.8"), cancellation.clone())
            .await
            .unwrap();
        session.write_dataset_chunk(b"partial").await.unwrap();
        cancellation.cancel();
        assert!(matches!(session.finish().await, Err(StoreError::Cancelled)));
        let staging = root.join("PAT001/ACC001/1.2.3/.dcmget-staging");
        assert!(!staging.read_dir().unwrap().any(|_| true));

        let mut session = store
            .begin_store(request(&root, "1.2.3.9"), CancellationToken::new())
            .await
            .unwrap();
        session.write_dataset_chunk(b"partial").await.unwrap();
        session.abort().await.unwrap();
        assert!(!staging.read_dir().unwrap().any(|_| true));
        fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn dropping_session_closes_worker_and_removes_partial_file() {
        let root = temporary_root("drop-cleanup");
        let store = FileStore::new();
        let mut session = store
            .begin_store(request(&root, "1.2.3.91"), CancellationToken::new())
            .await
            .unwrap();
        session.write_dataset_chunk(b"partial").await.unwrap();
        let staging = root.join("PAT001/ACC001/1.2.3/.dcmget-staging");
        assert!(staging.read_dir().unwrap().any(|_| true));
        drop(session);

        tokio::time::timeout(Duration::from_secs(1), async {
            while staging.read_dir().unwrap().any(|_| true) {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("dropping an association must close and clean its file worker");
        fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn cancelled_and_dropped_quarantine_sessions_remove_partial_files() {
        let root = temporary_root("quarantine-cleanup");
        let store = FileStore::new();
        let cancellation = CancellationToken::new();
        let mut cancelled = store
            .begin_quarantine(quarantine_request(&root, "1.2.3.92"), cancellation.clone())
            .await
            .unwrap();
        cancelled.write_dataset_chunk(b"partial").await.unwrap();
        cancellation.cancel();
        assert!(matches!(
            cancelled.finish().await,
            Err(StoreError::Cancelled)
        ));

        let mut dropped = store
            .begin_quarantine(
                quarantine_request(&root, "1.2.3.93"),
                CancellationToken::new(),
            )
            .await
            .unwrap();
        dropped.write_dataset_chunk(b"partial").await.unwrap();
        let staging = root.join("_DcmGetQuarantine/profile-1/.dcmget-staging");
        drop(dropped);
        tokio::time::timeout(Duration::from_secs(1), async {
            while staging.read_dir().unwrap().any(|_| true) {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("dropping quarantine session must clean its staging file");
        assert!(
            !root
                .join("_DcmGetQuarantine/profile-1")
                .read_dir()
                .unwrap()
                .any(|entry| entry
                    .unwrap()
                    .path()
                    .extension()
                    .is_some_and(|value| value == "quarantine"))
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn unsafe_or_empty_payload_never_publishes() {
        let root = temporary_root("invalid");
        let store = FileStore::new();
        let mut unsafe_request = request(&root, "1.2.3.10");
        unsafe_request.relative_directory = PathBuf::from("../escape");
        assert!(matches!(
            store
                .begin_store(unsafe_request, CancellationToken::new())
                .await,
            Err(StoreError::UnsafeRelativePath(_))
        ));

        let session = store
            .begin_store(request(&root, "1.2.3.11"), CancellationToken::new())
            .await
            .unwrap();
        assert!(matches!(
            session.finish().await,
            Err(StoreError::EmptyDataset)
        ));
        assert!(!root.join("PAT001/ACC001/1.2.3/1.2.3.11.dcm").exists());
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn preexisting_symlink_ancestor_cannot_escape_destination_root() {
        use std::os::unix::fs::symlink;

        let root = temporary_root("symlink-escape");
        let outside = temporary_root("symlink-outside");
        fs::create_dir(root.join("PAT001")).unwrap();
        symlink(&outside, root.join("PAT001/ACC001")).unwrap();

        let result = FileStore::new()
            .begin_store(request(&root, "1.2.3.12"), CancellationToken::new())
            .await;
        assert!(matches!(result, Err(StoreError::UnsafeDestinationLink(_))));
        assert!(!outside.join("1.2.3/.dcmget-staging").exists());
        assert!(!outside.join("1.2.3/1.2.3.12.dcm").exists());

        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(outside).unwrap();
    }

    #[test]
    fn move_request_type_is_independent_from_payload_sink() {
        let request = MoveRequest::study_by_accession(
            "profile",
            "task",
            "ACC001",
            DicomEndpoint {
                host: "pacs.local".to_owned(),
                port: 104,
            },
            "DCMGET",
            "PACS",
            "DCMGET",
        );
        assert_eq!(request.query_keys()[1], ("AccessionNumber", "ACC001"));
    }
}
