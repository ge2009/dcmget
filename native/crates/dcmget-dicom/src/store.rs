use std::collections::hash_map::DefaultHasher;
use std::fs::{self, File, OpenOptions};
use std::hash::{Hash, Hasher};
use std::io::{self, Read, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use sha2::{Digest, Sha256};

use crate::cancel::CancellationToken;
use crate::model::{ReceiveDisposition, ReceiveOutcome, Sha256Digest, StoreRequest};
use crate::part10::{FileMeta, Part10Error, build_part10_header};

pub const C_STORE_SUCCESS: u16 = 0x0000;
pub const C_STORE_FAILURE_CANNOT_UNDERSTAND: u16 = 0xC000;
const COPY_BUFFER_BYTES: usize = 1024 * 1024;
const PUBLICATION_LOCK_SHARDS: usize = 256;

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

pub trait StorePayloadSink: Send + Sync {
    type Session: StorePayloadSession;

    fn begin_store(
        &self,
        request: StoreRequest,
        cancellation: CancellationToken,
    ) -> Result<Self::Session, StoreError>;
}

pub trait StorePayloadSession {
    fn write_dataset_chunk(&mut self, chunk: &[u8]) -> Result<(), StoreError>;
    fn finish(self) -> Result<ReceiveOutcome, StoreError>;
    fn abort(self) -> Result<(), StoreError>;
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
}

impl StoreError {
    #[must_use]
    pub fn recommended_c_store_status(&self) -> u16 {
        C_STORE_FAILURE_CANNOT_UNDERSTAND
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
}

impl StorePayloadSink for FileStore {
    type Session = FileStoreSession;

    fn begin_store(
        &self,
        request: StoreRequest,
        cancellation: CancellationToken,
    ) -> Result<Self::Session, StoreError> {
        if cancellation.is_cancelled() {
            return Err(StoreError::Cancelled);
        }
        validate_relative_path(&request.relative_directory)?;
        let meta = FileMeta {
            media_storage_sop_class_uid: request.sop_class_uid.clone(),
            media_storage_sop_instance_uid: request.sop_instance_uid.clone(),
            transfer_syntax_uid: request.transfer_syntax_uid.clone(),
        };
        let header = build_part10_header(&meta)?;
        let staging_directory = ensure_safe_destination_directory(
            &request.route.destination_root,
            &request.relative_directory.join(".dcmget-staging"),
        )?;
        let (part_path, mut file) = create_unique_part(&staging_directory, self.next_sequence())?;
        if let Err(error) = file.write_all(&header) {
            let _ = fs::remove_file(&part_path);
            return Err(io_error(part_path, error));
        }
        let mut hasher = Sha256::new();
        hasher.update(&header);
        Ok(FileStoreSession {
            store: self.clone(),
            request,
            cancellation,
            part_path: Some(part_path),
            file: Some(file),
            file_hasher: Some(hasher),
            dataset_hasher: Some(Sha256::new()),
            header_bytes: u64::try_from(header.len()).unwrap_or(u64::MAX),
            dataset_bytes: 0,
        })
    }
}

pub struct FileStoreSession {
    store: FileStore,
    request: StoreRequest,
    cancellation: CancellationToken,
    part_path: Option<PathBuf>,
    file: Option<File>,
    file_hasher: Option<Sha256>,
    dataset_hasher: Option<Sha256>,
    header_bytes: u64,
    dataset_bytes: u64,
}

impl FileStoreSession {
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
        let target_directory = ensure_safe_destination_directory(
            &self.request.route.destination_root,
            &self.request.relative_directory,
        )?;
        let target = target_directory.join(format!("{}.dcm", self.request.sop_instance_uid));
        let lock_shard = FileStore::publication_shard(&target);
        let store_inner = Arc::clone(&self.store.inner);
        let _guard = store_inner.publication_locks[lock_shard]
            .lock()
            .map_err(|_| StoreError::LockPoisoned)?;
        if self.cancellation.is_cancelled() {
            self.cleanup_part()?;
            return Err(StoreError::Cancelled);
        }

        let (disposition, published_path, published_digest, published_bytes) = if target.exists() {
            let existing = digest_part10_file(&target)?;
            if let Some(existing) = existing.filter(|existing| {
                existing.dataset_digest == incoming_dataset_digest
                    && existing.dataset_bytes == self.dataset_bytes
            }) {
                remove_file(&part_path)?;
                (
                    ReceiveDisposition::ExistingSkipped,
                    target,
                    existing.file_digest,
                    existing.file_bytes,
                )
            } else {
                let conflict_directory = ensure_safe_destination_directory(
                    &self.request.route.destination_root,
                    Path::new("_DcmGetConflicts"),
                )?;
                let conflict = reserve_conflict_path(
                    &conflict_directory,
                    &self.request.sop_instance_uid,
                    self.store.next_sequence(),
                )?;
                rename(&part_path, &conflict)?;
                sync_directory_best_effort(&conflict_directory);
                (
                    ReceiveDisposition::ConflictPreserved,
                    conflict,
                    incoming_digest,
                    file_bytes,
                )
            }
        } else {
            rename(&part_path, &target)?;
            sync_directory_best_effort(&target_directory);
            (
                ReceiveDisposition::Published,
                target,
                incoming_digest,
                file_bytes,
            )
        };
        self.part_path = None;
        Ok(ReceiveOutcome {
            disposition,
            path: published_path,
            sop_instance_uid: self.request.sop_instance_uid.clone(),
            sha256: published_digest,
            file_bytes: published_bytes,
            dataset_bytes: self.dataset_bytes,
        })
    }
}

impl StorePayloadSession for FileStoreSession {
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

    fn finish(self) -> Result<ReceiveOutcome, StoreError> {
        self.publish()
    }

    fn abort(mut self) -> Result<(), StoreError> {
        self.cleanup_part()
    }
}

impl Drop for FileStoreSession {
    fn drop(&mut self) {
        let _ = self.cleanup_part();
    }
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
) -> Result<(PathBuf, File), StoreError> {
    for offset in 0..1_000_u64 {
        let path = directory.join(format!(
            ".incoming-{}-{}.part",
            std::process::id(),
            first_sequence.saturating_add(offset)
        ));
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(file) => {
                return Ok((path, file));
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
    use std::sync::{Arc, Barrier};
    use std::thread;

    use super::*;
    use crate::model::{DicomEndpoint, MoveRequest, ReceiveRoute};

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

    fn store_payload(
        store: &FileStore,
        request: StoreRequest,
        payload: &[u8],
    ) -> Result<ReceiveOutcome, StoreError> {
        let mut session = store.begin_store(request, CancellationToken::new())?;
        for chunk in payload.chunks(3) {
            session.write_dataset_chunk(chunk)?;
        }
        session.finish()
    }

    #[test]
    fn raw_policy_accepts_unknown_but_well_formed_transfer_syntax() {
        let policy = RawTransferSyntaxPolicy;
        assert!(policy.accepts("1.2.840.10008.5.1.4.1.1.2", "9.9.9.42"));
        assert!(!policy.accepts("CT Image Storage", "9.9.9.42"));
    }

    #[test]
    fn streams_part10_to_target_volume_and_atomically_publishes_dcm() {
        let root = temporary_root("publish");
        let store = FileStore::new();
        let payload = b"raw-negotiated-dataset-payload";
        let result = store_payload(&store, request(&root, "1.2.3.4"), payload).unwrap();
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

    #[test]
    fn identical_sop_and_content_is_skipped() {
        let root = temporary_root("duplicate");
        let store = FileStore::new();
        let first = store_payload(&store, request(&root, "1.2.3.5"), b"same").unwrap();
        let second = store_payload(&store, request(&root, "1.2.3.5"), b"same").unwrap();
        assert_eq!(first.disposition, ReceiveDisposition::Published);
        assert_eq!(second.disposition, ReceiveDisposition::ExistingSkipped);
        assert_eq!(first.path, second.path);
        assert_eq!(first.sha256, second.sha256);
        assert!(fs::read_dir(root.join("_DcmGetConflicts")).is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn duplicate_compares_dataset_not_part10_implementation_fields() {
        let root = temporary_root("duplicate-meta");
        let store = FileStore::new();
        let first = store_payload(&store, request(&root, "1.2.3.51"), b"same").unwrap();
        let mut bytes = fs::read(&first.path).unwrap();
        let implementation = bytes
            .windows(b"DCMGET_4_0".len())
            .position(|window| window == b"DCMGET_4_0")
            .expect("implementation version in Part 10 header");
        bytes[implementation..implementation + b"OTHER__4_0".len()].copy_from_slice(b"OTHER__4_0");
        fs::write(&first.path, bytes).unwrap();

        let second = store_payload(&store, request(&root, "1.2.3.51"), b"same").unwrap();
        assert_eq!(second.disposition, ReceiveDisposition::ExistingSkipped);
        assert_eq!(first.path, second.path);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn conflicting_sop_is_preserved_without_overwriting_target() {
        let root = temporary_root("conflict");
        let store = FileStore::new();
        let first = store_payload(&store, request(&root, "1.2.3.6"), b"first").unwrap();
        let conflict = store_payload(&store, request(&root, "1.2.3.6"), b"second").unwrap();
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

    #[test]
    fn concurrent_same_sop_never_overwrites() {
        let root = temporary_root("concurrent");
        let store = Arc::new(FileStore::new());
        let barrier = Arc::new(Barrier::new(2));
        let mut workers = Vec::new();
        for payload in [b"first".as_slice(), b"second".as_slice()] {
            let root = root.clone();
            let store = Arc::clone(&store);
            let barrier = Arc::clone(&barrier);
            let payload = payload.to_vec();
            workers.push(thread::spawn(move || {
                barrier.wait();
                store_payload(&store, request(&root, "1.2.3.7"), &payload).unwrap()
            }));
        }
        let outcomes: Vec<_> = workers
            .into_iter()
            .map(|worker| worker.join().unwrap())
            .collect();
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

    #[test]
    fn cancellation_and_abort_remove_partial_files() {
        let root = temporary_root("cancel");
        let store = FileStore::new();
        let cancellation = CancellationToken::new();
        let mut session = store
            .begin_store(request(&root, "1.2.3.8"), cancellation.clone())
            .unwrap();
        session.write_dataset_chunk(b"partial").unwrap();
        cancellation.cancel();
        assert!(matches!(session.finish(), Err(StoreError::Cancelled)));
        let staging = root.join("PAT001/ACC001/1.2.3/.dcmget-staging");
        assert!(!staging.read_dir().unwrap().any(|_| true));

        let mut session = store
            .begin_store(request(&root, "1.2.3.9"), CancellationToken::new())
            .unwrap();
        session.write_dataset_chunk(b"partial").unwrap();
        session.abort().unwrap();
        assert!(!staging.read_dir().unwrap().any(|_| true));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn unsafe_or_empty_payload_never_publishes() {
        let root = temporary_root("invalid");
        let store = FileStore::new();
        let mut unsafe_request = request(&root, "1.2.3.10");
        unsafe_request.relative_directory = PathBuf::from("../escape");
        assert!(matches!(
            store.begin_store(unsafe_request, CancellationToken::new()),
            Err(StoreError::UnsafeRelativePath(_))
        ));

        let session = store
            .begin_store(request(&root, "1.2.3.11"), CancellationToken::new())
            .unwrap();
        assert!(matches!(session.finish(), Err(StoreError::EmptyDataset)));
        assert!(!root.join("PAT001/ACC001/1.2.3/1.2.3.11.dcm").exists());
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn preexisting_symlink_ancestor_cannot_escape_destination_root() {
        use std::os::unix::fs::symlink;

        let root = temporary_root("symlink-escape");
        let outside = temporary_root("symlink-outside");
        fs::create_dir(root.join("PAT001")).unwrap();
        symlink(&outside, root.join("PAT001/ACC001")).unwrap();

        let result =
            FileStore::new().begin_store(request(&root, "1.2.3.12"), CancellationToken::new());
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
