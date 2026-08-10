use std::collections::HashSet;
use std::fs::{self, File};
use std::hash::BuildHasher;
use std::io::{BufReader, ErrorKind, Read};
use std::path::{Component, Path, PathBuf};
use std::sync::Mutex;

use dicom_dictionary_std::tags;
use dicom_object::OpenFileOptions;
use sha1::Sha1;
use sha2::{Digest, Sha256};
use thiserror::Error;

const STAGING_ROOT_NAME: &str = ".dcmget-staging";
const MAX_COMPONENT_BYTES: usize = 180;
const COMPONENT_PREFIX_BYTES: usize = 128;
const ARCHIVE_HASH_BUFFER_BYTES: usize = 1024 * 1024;
const CONFLICT_DIGEST_BYTES: usize = 16;
static ARCHIVE_PUBLICATION_LOCK: Mutex<()> = Mutex::new(());

#[derive(Debug, Error)]
pub enum ArchiveError {
    #[error("archive worker failed: {0}")]
    Worker(String),
    #[error("{0}")]
    Operation(String),
}

/// A per-accession receive directory below the destination volume's hidden
/// staging root. Keeping the staging path on the destination volume makes the
/// final `rename` publication atomic.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StagingDirectory {
    path: PathBuf,
    relative_path: PathBuf,
}

impl StagingDirectory {
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }

    #[must_use]
    pub fn relative_path(&self) -> &Path {
        &self.relative_path
    }
}

#[derive(Debug, Default)]
pub struct ArchiveBatchResult {
    archived_files: HashSet<PathBuf>,
    failures: Vec<String>,
    conflicts: u64,
}

impl ArchiveBatchResult {
    #[must_use]
    pub fn failures(&self) -> &[String] {
        &self.failures
    }

    #[must_use]
    pub fn into_parts(self) -> (HashSet<PathBuf>, Vec<String>, u64) {
        (self.archived_files, self.failures, self.conflicts)
    }
}

#[derive(Debug)]
struct ArchiveMetadata {
    patient_id: String,
    study_instance_uid: String,
    sop_instance_uid: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ArchiveDisposition {
    Published,
    ExistingSkipped,
    ConflictPreserved,
}

/// Create or reuse a safe per-accession staging directory below
/// `.dcmget-staging` on the destination volume.
pub async fn prepare_staging_directory(
    destination_root: &Path,
    requested_accession: &str,
) -> Result<StagingDirectory, ArchiveError> {
    let destination_root = destination_root.to_path_buf();
    let relative_path =
        PathBuf::from(STAGING_ROOT_NAME).join(safe_accession_component(requested_accession));
    let worker_relative_path = relative_path.clone();
    let path = tokio::task::spawn_blocking(move || {
        ensure_archive_directory(&destination_root, &worker_relative_path)
    })
    .await
    .map_err(|error| ArchiveError::Worker(error.to_string()))?
    .map_err(ArchiveError::Operation)?;
    Ok(StagingDirectory {
        path,
        relative_path,
    })
}

/// Parse received DICOM metadata and atomically publish each staged file using
/// the configured directory template. Per-file failures are returned in the
/// batch and leave that file in staging so the caller can report a retryable
/// failure without losing the only received copy.
pub async fn archive_received_files<S: BuildHasher>(
    destination_root: &Path,
    staging_directory: &Path,
    requested_accession: &str,
    directory_template: &str,
    received_files: &HashSet<PathBuf, S>,
) -> Result<ArchiveBatchResult, ArchiveError> {
    let destination_root = destination_root.to_path_buf();
    let staging_directory = staging_directory.to_path_buf();
    let requested_accession = requested_accession.to_owned();
    let directory_template = directory_template.to_owned();
    let received_files = received_files.iter().cloned().collect::<Vec<_>>();
    tokio::task::spawn_blocking(move || {
        archive_received_files_blocking(
            &destination_root,
            &staging_directory,
            &requested_accession,
            &directory_template,
            received_files,
        )
    })
    .await
    .map_err(|error| ArchiveError::Worker(error.to_string()))
}

fn archive_received_files_blocking(
    destination_root: &Path,
    staging_directory: &Path,
    requested_accession: &str,
    directory_template: &str,
    mut received_files: Vec<PathBuf>,
) -> ArchiveBatchResult {
    received_files.sort();
    let mut batch = ArchiveBatchResult::default();
    for source in received_files {
        if !source.starts_with(staging_directory) {
            batch.archived_files.insert(source);
            continue;
        }
        match archive_one_received_file(
            &source,
            destination_root,
            requested_accession,
            directory_template,
        ) {
            Ok((path, disposition)) => {
                batch.archived_files.insert(path);
                if disposition == ArchiveDisposition::ConflictPreserved {
                    batch.conflicts = batch.conflicts.saturating_add(1);
                }
            }
            Err(error) => {
                let name = source
                    .file_name()
                    .and_then(|value| value.to_str())
                    .unwrap_or("unknown.dcm");
                batch.failures.push(format!("{name} 归档失败：{error}"));
                // A failed parse or publication must never discard the only
                // received copy. Keeping it in staging also makes retry safe.
                batch.archived_files.insert(source);
            }
        }
    }
    batch
}

fn archive_one_received_file(
    source: &Path,
    destination_root: &Path,
    requested_accession: &str,
    directory_template: &str,
) -> Result<(PathBuf, ArchiveDisposition), String> {
    let metadata = read_archive_metadata(source)?;
    let relative_directory = render_directory_template(
        directory_template,
        &metadata.patient_id,
        requested_accession,
        &metadata.study_instance_uid,
    );
    let target_directory = ensure_archive_directory(destination_root, &relative_directory)?;
    let target = target_directory.join(format!("{}.dcm", metadata.sop_instance_uid));
    publish_archived_file(
        source,
        &target,
        destination_root,
        &metadata.sop_instance_uid,
    )
}

fn read_archive_metadata(path: &Path) -> Result<ArchiveMetadata, String> {
    let object = OpenFileOptions::new()
        .read_until(tags::PIXEL_DATA)
        .open_file(path)
        .map_err(|error| format!("无法读取 DICOM 元数据：{error}"))?;
    let patient_id = optional_dicom_text(&object, "PatientID", "UNKNOWN_PATIENT");
    let study_instance_uid = optional_dicom_text(&object, "StudyInstanceUID", "UNKNOWN_STUDY");
    let sop_instance_uid = object
        .element_by_name("SOPInstanceUID")
        .map_err(|_| "缺少 SOP Instance UID".to_owned())?
        .value()
        .to_str()
        .map_err(|error| format!("无法读取 SOP Instance UID：{error}"))?
        .trim()
        .to_owned();
    if !valid_dicom_uid(&sop_instance_uid) {
        return Err("SOP Instance UID 无效".to_owned());
    }
    Ok(ArchiveMetadata {
        patient_id,
        study_instance_uid,
        sop_instance_uid,
    })
}

fn optional_dicom_text(
    object: &dicom_object::DefaultDicomObject,
    name: &str,
    fallback: &str,
) -> String {
    object
        .element_by_name(name)
        .ok()
        .and_then(|element| element.value().to_str().ok())
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| fallback.to_owned())
}

fn valid_dicom_uid(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && !value.starts_with('.')
        && !value.ends_with('.')
        && value.split('.').all(|component| {
            !component.is_empty() && component.bytes().all(|byte| byte.is_ascii_digit())
        })
}

fn render_directory_template(
    template: &str,
    patient_id: &str,
    requested_accession: &str,
    study_instance_uid: &str,
) -> PathBuf {
    let normalized = template.trim().replace('\\', "/");
    let replacements = [
        (
            "{PatientID}",
            safe_path_component(patient_id, "UNKNOWN_PATIENTID"),
        ),
        (
            "{AccessionNumber}",
            safe_path_component(requested_accession, "UNKNOWN_ACCESSIONNUMBER"),
        ),
        (
            "{StudyInstanceUID}",
            safe_path_component(study_instance_uid, "UNKNOWN_STUDYINSTANCEUID"),
        ),
    ];
    let rendered = replace_template_placeholders(&normalized, &replacements);
    let components = rendered
        .split('/')
        .filter(|component| !component.trim().is_empty())
        .map(|component| safe_path_component(component, "UNKNOWN"))
        .collect::<Vec<_>>();
    if components.is_empty() {
        PathBuf::from("UNKNOWN")
    } else {
        components.iter().collect()
    }
}

fn replace_template_placeholders(template: &str, replacements: &[(&str, String)]) -> String {
    let mut remaining = template;
    let mut output = String::with_capacity(template.len());
    while !remaining.is_empty() {
        if let Some((placeholder, value)) = replacements
            .iter()
            .find(|(placeholder, _)| remaining.starts_with(placeholder))
        {
            output.push_str(value);
            remaining = &remaining[placeholder.len()..];
            continue;
        }
        let character = remaining
            .chars()
            .next()
            .expect("remaining template is not empty");
        output.push(character);
        remaining = &remaining[character.len_utf8()..];
    }
    output
}

fn safe_path_component(value: &str, fallback: &str) -> String {
    let source = value.trim();
    let replaced = source
        .chars()
        .map(|character| {
            if matches!(
                character,
                '\\' | '/' | ':' | '*' | '?' | '"' | '<' | '>' | '|'
            ) || character <= '\u{1f}'
            {
                '_'
            } else {
                character
            }
        })
        .collect::<String>();
    let mut cleaned = replaced.trim_matches([' ', '.']).to_owned();
    if cleaned.is_empty() || matches!(cleaned.as_str(), "." | "..") {
        fallback.clone_into(&mut cleaned);
    }
    let reserved_stem = cleaned.split('.').next().unwrap_or_default().to_uppercase();
    if is_reserved_windows_stem(&reserved_stem) {
        cleaned.insert(0, '_');
    }
    if cleaned != source {
        let digest = Sha1::digest(source.as_bytes());
        cleaned.push('-');
        cleaned.push_str(&hex_prefix(&digest, 4));
    }
    cleaned.chars().take(MAX_COMPONENT_BYTES).collect()
}

fn is_reserved_windows_stem(value: &str) -> bool {
    matches!(value, "CON" | "PRN" | "AUX" | "NUL")
        || value
            .strip_prefix("COM")
            .or_else(|| value.strip_prefix("LPT"))
            .is_some_and(|suffix| {
                matches!(suffix, "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9")
            })
}

fn ensure_archive_directory(root: &Path, relative: &Path) -> Result<PathBuf, String> {
    if relative
        .components()
        .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err("目录模板生成了不安全路径".to_owned());
    }
    let canonical_root = fs::canonicalize(root)
        .map_err(|error| format!("无法解析目标目录 {}：{error}", root.display()))?;
    let mut directory = canonical_root.clone();
    for component in relative.components() {
        let Component::Normal(component) = component else {
            return Err("目录模板生成了不安全路径".to_owned());
        };
        directory.push(component);
        match fs::create_dir(&directory) {
            Ok(()) => {}
            Err(error) if error.kind() == ErrorKind::AlreadyExists => {}
            Err(error) => {
                return Err(format!("无法创建归档目录 {}：{error}", directory.display()));
            }
        }
        let metadata = fs::symlink_metadata(&directory)
            .map_err(|error| format!("无法检查归档目录 {}：{error}", directory.display()))?;
        if archive_path_is_link(&metadata) || !metadata.is_dir() {
            return Err(format!("归档路径不是安全目录：{}", directory.display()));
        }
        let canonical_directory = fs::canonicalize(&directory)
            .map_err(|error| format!("无法解析归档目录 {}：{error}", directory.display()))?;
        if !canonical_directory.starts_with(&canonical_root) {
            return Err(format!("归档目录越过目标根目录：{}", directory.display()));
        }
    }
    Ok(directory)
}

#[cfg(not(windows))]
fn archive_path_is_link(metadata: &fs::Metadata) -> bool {
    metadata.file_type().is_symlink()
}

#[cfg(windows)]
fn archive_path_is_link(metadata: &fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;

    const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0400;
    metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
}

fn publish_archived_file(
    source: &Path,
    target: &Path,
    destination_root: &Path,
    sop_instance_uid: &str,
) -> Result<(PathBuf, ArchiveDisposition), String> {
    let _guard = ARCHIVE_PUBLICATION_LOCK
        .lock()
        .map_err(|_| "归档发布锁异常".to_owned())?;
    if target.exists() {
        if file_sha256(source)? == file_sha256(target)? {
            fs::remove_file(source)
                .map_err(|error| format!("无法清理重复暂存文件 {}：{error}", source.display()))?;
            return Ok((target.to_path_buf(), ArchiveDisposition::ExistingSkipped));
        }
        let conflict = preserve_archive_conflict(source, destination_root, sop_instance_uid)?;
        return Ok((conflict, ArchiveDisposition::ConflictPreserved));
    }
    fs::rename(source, target).map_err(|error| {
        format!(
            "无法原子发布 {} 到 {}：{error}",
            source.display(),
            target.display()
        )
    })?;
    Ok((target.to_path_buf(), ArchiveDisposition::Published))
}

fn preserve_archive_conflict(
    source: &Path,
    destination_root: &Path,
    sop_instance_uid: &str,
) -> Result<PathBuf, String> {
    let conflict_directory =
        ensure_archive_directory(destination_root, Path::new("_DcmGetConflicts"))?;
    let source_digest = file_sha256(source)?;
    let digest = hex_prefix(&source_digest, CONFLICT_DIGEST_BYTES);
    for suffix in 0..1_000_u16 {
        let suffix = if suffix == 0 {
            String::new()
        } else {
            format!("-{suffix}")
        };
        let candidate =
            conflict_directory.join(format!("{sop_instance_uid}-sha256-{digest}{suffix}.dcm"));
        if candidate.exists() {
            if file_sha256(&candidate)? == source_digest {
                fs::remove_file(source).map_err(|error| {
                    format!("无法清理重复冲突文件 {}：{error}", source.display())
                })?;
                return Ok(candidate);
            }
            continue;
        }
        fs::rename(source, &candidate)
            .map_err(|error| format!("无法将冲突对象隔离到 {}：{error}", candidate.display()))?;
        return Ok(candidate);
    }
    Err("无法为冲突对象分配隔离文件名".to_owned())
}

fn file_sha256(path: &Path) -> Result<[u8; 32], String> {
    let file = File::open(path).map_err(|error| format!("无法读取 {}：{error}", path.display()))?;
    let mut reader = BufReader::with_capacity(ARCHIVE_HASH_BUFFER_BYTES, file);
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; ARCHIVE_HASH_BUFFER_BYTES];
    loop {
        let count = reader
            .read(&mut buffer)
            .map_err(|error| format!("无法读取 {}：{error}", path.display()))?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(hasher.finalize().into())
}

fn hex_prefix(bytes: &[u8], length: usize) -> String {
    let mut output = String::with_capacity(length.saturating_mul(2));
    for byte in bytes.iter().take(length) {
        output.push(char::from(b"0123456789abcdef"[usize::from(byte >> 4)]));
        output.push(char::from(b"0123456789abcdef"[usize::from(byte & 0x0F)]));
    }
    output
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

#[cfg(test)]
mod tests {
    use super::*;
    use dicom_core::{DataElement, PrimitiveValue, VR};
    use dicom_object::{FileMetaTableBuilder, InMemDicomObject};

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
            PrimitiveValue::from("WRONG"),
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

    #[tokio::test]
    async fn staging_directory_is_hidden_safe_and_bounded() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path().canonicalize().unwrap();
        for accession in ["../outside", "..\\outside", "CON", "A:B", "检查号/一"] {
            let staging = prepare_staging_directory(&root, accession).await.unwrap();
            assert!(staging.path().starts_with(root.join(STAGING_ROOT_NAME)));
            assert_eq!(staging.relative_path().components().count(), 2);
            let component = staging
                .relative_path()
                .file_name()
                .unwrap()
                .to_string_lossy();
            assert!(component.starts_with("accession-"));
            assert!(!component.contains('/'));
            assert!(!component.contains('\\'));
            assert!(component.len() <= MAX_COMPONENT_BYTES);
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
    fn template_components_preserve_chinese_and_do_not_reexpand_dataset_text() {
        let rendered = render_directory_template(
            "{PatientID}/{AccessionNumber}/{StudyInstanceUID}",
            "患者:一? ",
            "CON",
            "1.2.3",
        );
        let components = rendered
            .components()
            .map(|component| component.as_os_str().to_string_lossy().into_owned())
            .collect::<Vec<_>>();

        assert!(components[0].starts_with("患者_一_-"));
        assert_eq!(components[1], "_CON-7679a072");
        assert_eq!(components[2], "1.2.3");
        assert!(components.iter().all(|component| {
            !component.chars().any(|character| {
                matches!(
                    character,
                    '\\' | '/' | ':' | '*' | '?' | '"' | '<' | '>' | '|'
                ) || character <= '\u{1f}'
            })
        }));

        let injected = render_directory_template(
            "{PatientID}/{AccessionNumber}",
            "{AccessionNumber}",
            "REQUESTED",
            "1.2.3",
        );
        assert_eq!(
            injected,
            PathBuf::from("{AccessionNumber}").join("REQUESTED")
        );
    }

    #[tokio::test]
    async fn identical_existing_target_is_idempotent() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path().canonicalize().unwrap();
        let target_directory = root.join("A001/1.2.5");
        fs::create_dir_all(&target_directory).unwrap();
        let target = target_directory.join("1.2.5.6.dcm");
        write_test_dicom(&target, "P003", "1.2.5", "1.2.5.6");
        let staging = prepare_staging_directory(&root, "A001").await.unwrap();
        let source = staging.path().join("1.2.5.6.dcm");
        fs::copy(&target, &source).unwrap();

        let batch = archive_received_files(
            &root,
            staging.path(),
            "A001",
            "{AccessionNumber}/{StudyInstanceUID}",
            &HashSet::from([source.clone()]),
        )
        .await
        .unwrap();
        let (files, failures, conflicts) = batch.into_parts();

        assert!(failures.is_empty());
        assert_eq!(conflicts, 0);
        assert_eq!(files, HashSet::from([target]));
        assert!(!source.exists());
    }

    #[tokio::test]
    async fn differing_existing_target_is_preserved_in_conflict_directory() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path().canonicalize().unwrap();
        let target_directory = root.join("A001/1.2.6");
        fs::create_dir_all(&target_directory).unwrap();
        let target = target_directory.join("1.2.6.7.dcm");
        write_test_dicom(&target, "OLD", "1.2.6", "1.2.6.7");
        let target_sha = file_sha256(&target).unwrap();
        let staging = prepare_staging_directory(&root, "A001").await.unwrap();
        let source = staging.path().join("1.2.6.7.dcm");
        write_test_dicom(&source, "NEW", "1.2.6", "1.2.6.7");
        let incoming_sha = file_sha256(&source).unwrap();

        let batch = archive_received_files(
            &root,
            staging.path(),
            "A001",
            "{AccessionNumber}/{StudyInstanceUID}",
            &HashSet::from([source.clone()]),
        )
        .await
        .unwrap();
        let (files, failures, conflicts) = batch.into_parts();

        assert!(failures.is_empty());
        assert_eq!(conflicts, 1);
        assert_eq!(file_sha256(&target).unwrap(), target_sha);
        let conflict = files.into_iter().next().unwrap();
        assert!(conflict.starts_with(root.join("_DcmGetConflicts")));
        assert_eq!(file_sha256(&conflict).unwrap(), incoming_sha);
        assert!(!source.exists());
    }
}
