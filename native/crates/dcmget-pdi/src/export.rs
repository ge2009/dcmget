use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File, OpenOptions},
    io::{self, BufReader, BufWriter, Read, Write},
    path::{Component, Path, PathBuf},
};

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use thiserror::Error;
use uuid::Uuid;

pub const STUDY_INDEX_PATH: &str = "VIEWER/.dcmget/index";
pub const MANIFEST_NAME: &str = "MANIFEST.SHA256";
pub const MAX_INDEXED_FRAMES: usize = 100_000;
pub const MAX_INDEX_ESTIMATED_BYTES: usize = 64 * 1024 * 1024;

const COPY_BUFFER_BYTES: usize = 1024 * 1024;

/// One original DICOM object and the already-naturalized metadata required by
/// the offline OHIF index.
///
/// The exporter deliberately does not parse, transcode, or anonymize the
/// source. Callers remain responsible for deriving and validating metadata.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OfflineInstance {
    pub source: PathBuf,
    pub study_instance_uid: String,
    pub series_instance_uid: String,
    pub sop_instance_uid: String,
    pub metadata: Value,
    pub frame_count: u32,
}

/// A destination root plus a single safe directory name keeps publication on
/// one volume and makes path traversal impossible by construction.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OfflineExportRequest {
    pub output_root: PathBuf,
    pub export_name: String,
    pub instances: Vec<OfflineInstance>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExportedDicom {
    pub sop_instance_uid: String,
    pub relative_path: PathBuf,
    pub sha256: String,
    pub size: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OfflineExportResult {
    pub output_directory: PathBuf,
    pub exported_count: usize,
    pub duplicate_count: usize,
    pub indexed_count: usize,
    pub dicom_files: Vec<ExportedDicom>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OfflineVerification {
    pub manifest_file_count: usize,
    pub dicom_file_count: usize,
    pub indexed_count: usize,
}

#[derive(Debug, Error)]
pub enum OfflineExportError {
    #[error("offline export requires at least one DICOM instance")]
    EmptyExport,
    #[error("invalid offline export directory name: {0}")]
    InvalidExportName(String),
    #[error("offline export output root is not a regular directory: {0}")]
    InvalidOutputRoot(PathBuf),
    #[error("offline export destination already exists: {0}")]
    DestinationExists(PathBuf),
    #[error("invalid {field}: {value}")]
    InvalidUid { field: &'static str, value: String },
    #[error("offline export metadata for SOP Instance UID {0} must be a JSON object")]
    InvalidMetadata(String),
    #[error("offline export frame count for SOP Instance UID {0} must be at least one")]
    InvalidFrameCount(String),
    #[error("offline export index exceeds its {0} safety limit")]
    IndexLimit(&'static str),
    #[error("symbolic links are not allowed in offline export paths: {0}")]
    SymlinkNotAllowed(PathBuf),
    #[error("offline export source is not a regular file: {0}")]
    InvalidSource(PathBuf),
    #[error("SOP Instance UID {0} refers to different source content")]
    SopConflict(String),
    #[error("offline export validation failed: {0}")]
    Validation(String),
    #[error("failed to serialize offline export index: {0}")]
    Json(#[from] serde_json::Error),
    #[error("{operation} failed for {path}: {source}")]
    Io {
        operation: &'static str,
        path: PathBuf,
        #[source]
        source: io::Error,
    },
}

#[derive(Debug, Clone)]
struct PreparedInstance {
    study_instance_uid: String,
    series_instance_uid: String,
    sop_instance_uid: String,
    metadata: Map<String, Value>,
    frame_count: usize,
    relative_path: PathBuf,
    digest: String,
    size: u64,
}

#[derive(Debug)]
struct PartialDirectory {
    path: PathBuf,
    active: bool,
}

impl PartialDirectory {
    fn create(output_root: &Path) -> Result<Self, OfflineExportError> {
        for _ in 0..16 {
            let name = format!(".dcmget-{}.partial", Uuid::new_v4().simple());
            let path = output_root.join(name);
            match fs::create_dir(&path) {
                Ok(()) => return Ok(Self { path, active: true }),
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
                Err(source) => return Err(io_error("create partial directory", &path, source)),
            }
        }
        Err(OfflineExportError::Validation(
            "could not allocate a unique partial directory".to_owned(),
        ))
    }

    fn publish(mut self, destination: &Path) -> Result<(), OfflineExportError> {
        reject_existing_destination(destination)?;
        fs::rename(&self.path, destination)
            .map_err(|source| io_error("atomically publish directory", destination, source))?;
        self.active = false;
        Ok(())
    }
}

impl Drop for PartialDirectory {
    fn drop(&mut self) {
        if self.active {
            // A best-effort cleanup is sufficient for safety: if removal is
            // impossible, the unpublishable directory remains hidden and the
            // requested final directory is never created.
            let _ = fs::remove_dir_all(&self.path);
        }
    }
}

/// Copy original DICOM files, build the Python-compatible offline OHIF index,
/// validate it and its SHA-256 manifest, then atomically publish the directory.
pub fn export_offline(
    request: &OfflineExportRequest,
) -> Result<OfflineExportResult, OfflineExportError> {
    if request.instances.is_empty() {
        return Err(OfflineExportError::EmptyExport);
    }
    validate_export_name(&request.export_name)?;
    let output_root = canonical_output_root(&request.output_root)?;
    let destination = output_root.join(&request.export_name);
    reject_existing_destination(&destination)?;
    validate_inputs(&request.instances)?;

    let partial = PartialDirectory::create(&output_root)?;
    let dicom_root = partial.path.join("DICOM");
    create_directory(&dicom_root)?;

    let (prepared, duplicate_count) = prepare_instances(&request.instances, &dicom_root)?;
    validate_index_limits(&prepared)?;
    write_study_index(&partial.path, &prepared)?;
    write_manifest(&partial.path)?;
    let verification = verify_prepared_export(&partial.path, &prepared)?;

    let dicom_files = prepared
        .values()
        .map(|item| ExportedDicom {
            sop_instance_uid: item.sop_instance_uid.clone(),
            relative_path: item.relative_path.clone(),
            sha256: item.digest.clone(),
            size: item.size,
        })
        .collect::<Vec<_>>();
    partial.publish(&destination)?;

    Ok(OfflineExportResult {
        output_directory: destination,
        exported_count: dicom_files.len(),
        duplicate_count,
        indexed_count: verification.indexed_count,
        dicom_files,
    })
}

/// Independently verify an already-published directory's local references and
/// complete SHA-256 manifest.
pub fn verify_offline_export(root: &Path) -> Result<OfflineVerification, OfflineExportError> {
    let metadata = fs::symlink_metadata(root)
        .map_err(|source| io_error("inspect export directory", root, source))?;
    if metadata.file_type().is_symlink() {
        return Err(OfflineExportError::SymlinkNotAllowed(root.to_path_buf()));
    }
    if !metadata.is_dir() {
        return Err(OfflineExportError::InvalidOutputRoot(root.to_path_buf()));
    }

    let manifest = verify_manifest(root)?;
    let index = verify_index(root)?;
    let dicom_files = manifest
        .keys()
        .filter(|relative| is_dicom_relative(relative))
        .cloned()
        .collect::<BTreeSet<_>>();
    if index.referenced_files != dicom_files {
        return Err(OfflineExportError::Validation(
            "offline index references do not exactly match manifest DICOM files".to_owned(),
        ));
    }
    if root.join("DICOMDIR").try_exists().map_err(|source| {
        io_error(
            "inspect unsupported DICOMDIR",
            &root.join("DICOMDIR"),
            source,
        )
    })? {
        return Err(OfflineExportError::Validation(
            "unsupported DICOMDIR is present".to_owned(),
        ));
    }

    Ok(OfflineVerification {
        manifest_file_count: manifest.len(),
        dicom_file_count: dicom_files.len(),
        indexed_count: index.urls.len(),
    })
}

fn canonical_output_root(root: &Path) -> Result<PathBuf, OfflineExportError> {
    let metadata = fs::symlink_metadata(root)
        .map_err(|source| io_error("inspect output root", root, source))?;
    if metadata.file_type().is_symlink() {
        return Err(OfflineExportError::SymlinkNotAllowed(root.to_path_buf()));
    }
    if !metadata.is_dir() {
        return Err(OfflineExportError::InvalidOutputRoot(root.to_path_buf()));
    }
    fs::canonicalize(root).map_err(|source| io_error("canonicalize output root", root, source))
}

fn validate_export_name(name: &str) -> Result<(), OfflineExportError> {
    let path = Path::new(name);
    let mut components = path.components();
    let exactly_one_normal =
        matches!(components.next(), Some(Component::Normal(_))) && components.next().is_none();
    let invalid_cross_platform = name.is_empty()
        || name == "."
        || name == ".."
        || name.starts_with('.')
        || name.ends_with(['.', ' '])
        || name.contains(['/', '\\', ':', '\0']);
    if !exactly_one_normal || invalid_cross_platform {
        return Err(OfflineExportError::InvalidExportName(name.to_owned()));
    }
    Ok(())
}

fn validate_inputs(instances: &[OfflineInstance]) -> Result<(), OfflineExportError> {
    for instance in instances {
        validate_uid("Study Instance UID", &instance.study_instance_uid)?;
        validate_uid("Series Instance UID", &instance.series_instance_uid)?;
        validate_uid("SOP Instance UID", &instance.sop_instance_uid)?;
        if !instance.metadata.is_object() {
            return Err(OfflineExportError::InvalidMetadata(
                instance.sop_instance_uid.clone(),
            ));
        }
        if instance.frame_count == 0 {
            return Err(OfflineExportError::InvalidFrameCount(
                instance.sop_instance_uid.clone(),
            ));
        }
        validate_source(&instance.source)?;
    }
    Ok(())
}

fn validate_uid(field: &'static str, value: &str) -> Result<(), OfflineExportError> {
    let valid = !value.is_empty()
        && value.len() <= 64
        && !value.starts_with('.')
        && !value.ends_with('.')
        && !value.contains("..")
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || byte == b'.');
    if valid {
        Ok(())
    } else {
        Err(OfflineExportError::InvalidUid {
            field,
            value: value.to_owned(),
        })
    }
}

fn validate_source(source: &Path) -> Result<(), OfflineExportError> {
    let metadata = fs::symlink_metadata(source).map_err(|error| map_source_error(source, error))?;
    if metadata.file_type().is_symlink() {
        return Err(OfflineExportError::SymlinkNotAllowed(source.to_path_buf()));
    }
    if !metadata.is_file() {
        return Err(OfflineExportError::InvalidSource(source.to_path_buf()));
    }
    Ok(())
}

fn reject_existing_destination(destination: &Path) -> Result<(), OfflineExportError> {
    match fs::symlink_metadata(destination) {
        Ok(_) => Err(OfflineExportError::DestinationExists(
            destination.to_path_buf(),
        )),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(source) => Err(io_error("inspect export destination", destination, source)),
    }
}

fn prepare_instances(
    instances: &[OfflineInstance],
    dicom_root: &Path,
) -> Result<(BTreeMap<String, PreparedInstance>, usize), OfflineExportError> {
    let mut prepared: BTreeMap<String, PreparedInstance> = BTreeMap::new();
    let mut duplicate_count = 0;

    for instance in instances {
        if let Some(existing) = prepared.get(&instance.sop_instance_uid) {
            let (digest, _) = hash_file(&instance.source)?;
            if digest != existing.digest {
                return Err(OfflineExportError::SopConflict(
                    instance.sop_instance_uid.clone(),
                ));
            }
            duplicate_count += 1;
            continue;
        }

        let relative_path =
            PathBuf::from("DICOM").join(format!("{}.dcm", instance.sop_instance_uid));
        let destination = dicom_root.join(format!("{}.dcm", instance.sop_instance_uid));
        let (digest, size) = copy_and_hash(&instance.source, &destination)?;
        let metadata = instance
            .metadata
            .as_object()
            .expect("metadata was validated before the partial directory was created")
            .clone();
        prepared.insert(
            instance.sop_instance_uid.clone(),
            PreparedInstance {
                study_instance_uid: instance.study_instance_uid.clone(),
                series_instance_uid: instance.series_instance_uid.clone(),
                sop_instance_uid: instance.sop_instance_uid.clone(),
                metadata,
                frame_count: usize::try_from(instance.frame_count)
                    .expect("u32 always fits usize on supported targets"),
                relative_path,
                digest,
                size,
            },
        );
    }

    Ok((prepared, duplicate_count))
}

fn validate_index_limits(
    prepared: &BTreeMap<String, PreparedInstance>,
) -> Result<(), OfflineExportError> {
    let mut frames = 0_usize;
    let mut estimated_bytes = 32_usize;
    for item in prepared.values() {
        frames = frames
            .checked_add(item.frame_count)
            .ok_or(OfflineExportError::IndexLimit("frame-count"))?;
        if frames > MAX_INDEXED_FRAMES {
            return Err(OfflineExportError::IndexLimit("frame-count"));
        }
        let metadata_bytes = serde_json::to_vec(&item.metadata)?.len();
        let url_bytes = local_url(&item.relative_path)?.len();
        let per_frame = metadata_bytes
            .checked_add(url_bytes)
            .and_then(|value| value.checked_add(96))
            .ok_or(OfflineExportError::IndexLimit("estimated-size"))?;
        estimated_bytes = estimated_bytes
            .checked_add(
                item.frame_count
                    .checked_mul(per_frame)
                    .ok_or(OfflineExportError::IndexLimit("estimated-size"))?,
            )
            .and_then(|value| value.checked_add(512))
            .ok_or(OfflineExportError::IndexLimit("estimated-size"))?;
        if estimated_bytes > MAX_INDEX_ESTIMATED_BYTES {
            return Err(OfflineExportError::IndexLimit("estimated-size"));
        }
    }
    Ok(())
}

#[derive(Debug, Serialize, Deserialize)]
struct StudyIndex {
    #[serde(rename = "StudyInstanceUID")]
    study_instance_uid: String,
    #[serde(rename = "StudyDate")]
    study_date: Value,
    #[serde(rename = "StudyTime")]
    study_time: Value,
    #[serde(rename = "PatientName")]
    patient_name: Value,
    #[serde(rename = "PatientID")]
    patient_id: Value,
    #[serde(rename = "AccessionNumber")]
    accession_number: Value,
    #[serde(rename = "PatientSex")]
    patient_sex: Value,
    #[serde(rename = "PatientAge")]
    patient_age: Value,
    #[serde(rename = "PatientWeight")]
    patient_weight: Value,
    #[serde(rename = "StudyDescription")]
    study_description: Value,
    #[serde(rename = "InstitutionName")]
    institution_name: Value,
    series: Vec<SeriesIndex>,
    #[serde(rename = "NumInstances")]
    num_instances: usize,
    #[serde(rename = "Modalities")]
    modalities: String,
}

#[derive(Debug, Serialize, Deserialize)]
struct SeriesIndex {
    #[serde(rename = "SeriesInstanceUID")]
    series_instance_uid: String,
    #[serde(rename = "SeriesNumber")]
    series_number: Value,
    #[serde(rename = "SeriesDate")]
    series_date: Value,
    #[serde(rename = "SeriesTime")]
    series_time: Value,
    #[serde(rename = "Modality")]
    modality: Value,
    #[serde(rename = "SliceThickness")]
    slice_thickness: Value,
    #[serde(rename = "SeriesDescription")]
    series_description: Value,
    #[serde(rename = "ProtocolName")]
    protocol_name: Value,
    instances: Vec<IndexInstance>,
}

#[derive(Debug, Serialize, Deserialize)]
struct IndexInstance {
    metadata: Map<String, Value>,
    url: String,
}

#[derive(Debug, Serialize, Deserialize)]
struct StudyIndexPayload {
    studies: Vec<StudyIndex>,
}

#[derive(Debug)]
struct StudyBuilder<'a> {
    first: &'a PreparedInstance,
    series: BTreeMap<String, SeriesBuilder<'a>>,
}

#[derive(Debug)]
struct SeriesBuilder<'a> {
    first: &'a PreparedInstance,
    instances: Vec<&'a PreparedInstance>,
}

fn write_study_index(
    root: &Path,
    prepared: &BTreeMap<String, PreparedInstance>,
) -> Result<(), OfflineExportError> {
    let payload = build_study_index(prepared)?;
    let index_path = root.join(STUDY_INDEX_PATH);
    let parent = index_path.parent().ok_or_else(|| {
        OfflineExportError::Validation("study index has no parent directory".to_owned())
    })?;
    create_directories(parent)?;
    let file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&index_path)
        .map_err(|source| io_error("create study index", &index_path, source))?;
    let mut writer = BufWriter::new(file);
    serde_json::to_writer_pretty(&mut writer, &payload)?;
    writer
        .write_all(b"\n")
        .map_err(|source| io_error("write study index", &index_path, source))?;
    sync_writer(writer, "sync study index", &index_path)
}

fn build_study_index(
    prepared: &BTreeMap<String, PreparedInstance>,
) -> Result<StudyIndexPayload, OfflineExportError> {
    let mut builders: BTreeMap<String, StudyBuilder<'_>> = BTreeMap::new();
    for item in prepared.values() {
        let study = builders
            .entry(item.study_instance_uid.clone())
            .or_insert_with(|| StudyBuilder {
                first: item,
                series: BTreeMap::new(),
            });
        let series = study
            .series
            .entry(item.series_instance_uid.clone())
            .or_insert_with(|| SeriesBuilder {
                first: item,
                instances: Vec::new(),
            });
        series.instances.push(item);
    }

    let mut studies = Vec::with_capacity(builders.len());
    for (study_uid, builder) in builders {
        let mut series_values = Vec::with_capacity(builder.series.len());
        for (series_uid, mut series_builder) in builder.series {
            series_builder.instances.sort_by(|left, right| {
                metadata_number(&left.metadata, "InstanceNumber")
                    .total_cmp(&metadata_number(&right.metadata, "InstanceNumber"))
                    .then_with(|| left.sop_instance_uid.cmp(&right.sop_instance_uid))
            });
            let mut instances = Vec::new();
            for item in series_builder.instances {
                let base_url = local_url(&item.relative_path)?;
                for frame in 1..=item.frame_count {
                    instances.push(IndexInstance {
                        metadata: item.metadata.clone(),
                        url: if item.frame_count == 1 {
                            base_url.clone()
                        } else {
                            format!("{base_url}?frame={frame}")
                        },
                    });
                }
            }
            let metadata = &series_builder.first.metadata;
            series_values.push(SeriesIndex {
                series_instance_uid: series_uid,
                series_number: metadata_value(metadata, "SeriesNumber", Value::from(0)),
                series_date: metadata_value(metadata, "SeriesDate", Value::from("")),
                series_time: metadata_value(metadata, "SeriesTime", Value::from("")),
                modality: metadata_value(metadata, "Modality", Value::from("")),
                slice_thickness: metadata_value(metadata, "SliceThickness", Value::from("")),
                series_description: metadata_value(metadata, "SeriesDescription", Value::from("")),
                protocol_name: metadata_value(metadata, "ProtocolName", Value::from("")),
                instances,
            });
        }
        series_values.sort_by(|left, right| {
            value_number(&left.series_number)
                .total_cmp(&value_number(&right.series_number))
                .then_with(|| left.series_instance_uid.cmp(&right.series_instance_uid))
        });
        let modalities = series_values
            .iter()
            .filter_map(|series| series.modality.as_str())
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .fold(Vec::<&str>::new(), |mut values, value| {
                if !values.contains(&value) {
                    values.push(value);
                }
                values
            })
            .join("\\");
        let num_instances = series_values
            .iter()
            .map(|series| series.instances.len())
            .sum();
        let metadata = &builder.first.metadata;
        let patient_id = metadata_value(metadata, "PatientID", Value::from(""));
        studies.push(StudyIndex {
            study_instance_uid: study_uid,
            study_date: metadata_value(metadata, "StudyDate", Value::from("")),
            study_time: metadata_value(metadata, "StudyTime", Value::from("")),
            patient_name: metadata_value(metadata, "PatientName", Value::Array(Vec::new())),
            patient_id: if value_is_empty(&patient_id) {
                Value::from("UNKNOWN")
            } else {
                patient_id
            },
            accession_number: metadata_value(metadata, "AccessionNumber", Value::from("")),
            patient_sex: metadata_value(metadata, "PatientSex", Value::from("")),
            patient_age: metadata_value(metadata, "PatientAge", Value::from("")),
            patient_weight: metadata_value(metadata, "PatientWeight", Value::from("")),
            study_description: metadata_value(metadata, "StudyDescription", Value::from("")),
            institution_name: metadata_value(metadata, "InstitutionName", Value::from("")),
            series: series_values,
            num_instances,
            modalities,
        });
    }
    Ok(StudyIndexPayload { studies })
}

fn metadata_value(metadata: &Map<String, Value>, key: &str, default: Value) -> Value {
    metadata
        .get(key)
        .filter(|value| !value.is_null())
        .cloned()
        .unwrap_or(default)
}

fn metadata_number(metadata: &Map<String, Value>, key: &str) -> f64 {
    metadata.get(key).map_or(f64::INFINITY, value_number)
}

fn value_number(value: &Value) -> f64 {
    value
        .as_f64()
        .or_else(|| value.as_str().and_then(|text| text.parse().ok()))
        .unwrap_or(f64::INFINITY)
}

fn value_is_empty(value: &Value) -> bool {
    value.is_null() || value.as_str().is_some_and(str::is_empty)
}

fn local_url(relative_path: &Path) -> Result<String, OfflineExportError> {
    let relative = portable_relative(relative_path)?;
    if !is_dicom_relative(&relative) {
        return Err(OfflineExportError::Validation(format!(
            "DICOM path is outside the DICOM directory: {relative}"
        )));
    }
    Ok(format!("dicomweb:/{relative}"))
}

fn write_manifest(root: &Path) -> Result<(), OfflineExportError> {
    let manifest_path = root.join(MANIFEST_NAME);
    let files = collect_files(root)?;
    let file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&manifest_path)
        .map_err(|source| io_error("create SHA-256 manifest", &manifest_path, source))?;
    let mut writer = BufWriter::new(file);
    for (relative, path) in files {
        if relative == MANIFEST_NAME {
            continue;
        }
        let (digest, _) = hash_file(&path)?;
        writeln!(writer, "{digest}  {relative}")
            .map_err(|source| io_error("write SHA-256 manifest", &manifest_path, source))?;
    }
    sync_writer(writer, "sync SHA-256 manifest", &manifest_path)
}

fn verify_prepared_export(
    root: &Path,
    prepared: &BTreeMap<String, PreparedInstance>,
) -> Result<OfflineVerification, OfflineExportError> {
    let verification = verify_offline_export(root)?;
    if verification.dicom_file_count != prepared.len() {
        return Err(OfflineExportError::Validation(
            "exported DICOM count does not match prepared instances".to_owned(),
        ));
    }
    let expected_frames: usize = prepared.values().map(|item| item.frame_count).sum();
    if verification.indexed_count != expected_frames {
        return Err(OfflineExportError::Validation(
            "offline index frame count does not match prepared instances".to_owned(),
        ));
    }
    let manifest = verify_manifest(root)?;
    for item in prepared.values() {
        let relative = portable_relative(&item.relative_path)?;
        if manifest.get(&relative) != Some(&item.digest) {
            return Err(OfflineExportError::Validation(format!(
                "manifest digest does not match copied DICOM: {relative}"
            )));
        }
    }
    let actual_urls = verify_index(root)?.urls;
    let expected_urls = prepared
        .values()
        .flat_map(|item| {
            let base = local_url(&item.relative_path)
                .expect("prepared DICOM paths are generated and already validated");
            (1..=item.frame_count).map(move |frame| {
                if item.frame_count == 1 {
                    base.clone()
                } else {
                    format!("{base}?frame={frame}")
                }
            })
        })
        .collect::<BTreeSet<_>>();
    if actual_urls != expected_urls {
        return Err(OfflineExportError::Validation(
            "offline index frame URLs do not match prepared instances".to_owned(),
        ));
    }
    Ok(verification)
}

fn verify_manifest(root: &Path) -> Result<BTreeMap<String, String>, OfflineExportError> {
    let manifest_path = root.join(MANIFEST_NAME);
    reject_symlink(&manifest_path)?;
    let contents = fs::read_to_string(&manifest_path)
        .map_err(|source| io_error("read SHA-256 manifest", &manifest_path, source))?;
    let mut expected = BTreeMap::new();
    for line in contents.lines() {
        let (digest, relative) = line.split_once("  ").ok_or_else(|| {
            OfflineExportError::Validation("invalid SHA-256 manifest line".to_owned())
        })?;
        if digest.len() != 64
            || !digest
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            || !valid_portable_relative(relative)
            || relative == MANIFEST_NAME
            || expected
                .insert(relative.to_owned(), digest.to_owned())
                .is_some()
        {
            return Err(OfflineExportError::Validation(
                "invalid or duplicate SHA-256 manifest entry".to_owned(),
            ));
        }
    }
    if expected.is_empty() {
        return Err(OfflineExportError::Validation(
            "SHA-256 manifest is empty".to_owned(),
        ));
    }

    let actual_files = collect_files(root)?;
    let actual_names = actual_files
        .keys()
        .filter(|relative| relative.as_str() != MANIFEST_NAME)
        .cloned()
        .collect::<BTreeSet<_>>();
    if actual_names != expected.keys().cloned().collect() {
        return Err(OfflineExportError::Validation(
            "SHA-256 manifest file set does not match export contents".to_owned(),
        ));
    }
    for (relative, digest) in &expected {
        let path = actual_files.get(relative).ok_or_else(|| {
            OfflineExportError::Validation(format!("manifest file is missing: {relative}"))
        })?;
        let (actual_digest, _) = hash_file(path)?;
        if &actual_digest != digest {
            return Err(OfflineExportError::Validation(format!(
                "SHA-256 mismatch for {relative}"
            )));
        }
    }
    Ok(expected)
}

#[derive(Debug)]
struct VerifiedIndex {
    referenced_files: BTreeSet<String>,
    urls: BTreeSet<String>,
}

fn verify_index(root: &Path) -> Result<VerifiedIndex, OfflineExportError> {
    let index_path = root.join(STUDY_INDEX_PATH);
    reject_symlink(&index_path)?;
    let file = File::open(&index_path)
        .map_err(|source| io_error("open study index", &index_path, source))?;
    let payload: StudyIndexPayload = serde_json::from_reader(BufReader::new(file))?;
    let dicom_root = root.join("DICOM");
    reject_symlink(&dicom_root)?;
    let canonical_dicom_root = fs::canonicalize(&dicom_root)
        .map_err(|source| io_error("canonicalize DICOM directory", &dicom_root, source))?;
    let mut referenced_files = BTreeSet::new();
    let mut urls = BTreeSet::new();
    for study in payload.studies {
        for series in study.series {
            for instance in series.instances {
                let (relative, frame) = parse_local_url(&instance.url)?;
                if !urls.insert(instance.url) {
                    return Err(OfflineExportError::Validation(
                        "offline index contains a duplicate frame URL".to_owned(),
                    ));
                }
                if frame == Some(0) {
                    return Err(OfflineExportError::Validation(
                        "offline index frame numbers start at one".to_owned(),
                    ));
                }
                if referenced_files.insert(relative.clone()) {
                    let path = safe_join(root, &relative)?;
                    reject_symlink(&path)?;
                    let metadata = fs::metadata(&path)
                        .map_err(|source| io_error("inspect indexed DICOM", &path, source))?;
                    if !metadata.is_file() {
                        return Err(OfflineExportError::Validation(format!(
                            "offline index target is not a file: {relative}"
                        )));
                    }
                    let canonical_target = fs::canonicalize(&path)
                        .map_err(|source| io_error("canonicalize indexed DICOM", &path, source))?;
                    if !canonical_target.starts_with(&canonical_dicom_root) {
                        return Err(OfflineExportError::Validation(format!(
                            "offline index target escapes DICOM directory: {relative}"
                        )));
                    }
                }
            }
        }
    }
    if urls.is_empty() {
        return Err(OfflineExportError::Validation(
            "offline index has no instances".to_owned(),
        ));
    }
    Ok(VerifiedIndex {
        referenced_files,
        urls,
    })
}

fn parse_local_url(url: &str) -> Result<(String, Option<usize>), OfflineExportError> {
    const PREFIX: &str = "dicomweb:/";
    if url.starts_with("http://")
        || url.starts_with("https://")
        || !url.starts_with(PREFIX)
        || url.contains('%')
    {
        return Err(OfflineExportError::Validation(format!(
            "offline index contains a non-local DICOM URL: {url}"
        )));
    }
    let remainder = &url[PREFIX.len()..];
    let (relative, query) = remainder
        .split_once('?')
        .map_or((remainder, None), |(path, value)| (path, Some(value)));
    if !is_dicom_relative(relative) {
        return Err(OfflineExportError::Validation(format!(
            "offline index contains an unsafe DICOM path: {relative}"
        )));
    }
    let frame = query
        .map(|value| {
            value
                .strip_prefix("frame=")
                .filter(|number| {
                    !number.is_empty() && number.bytes().all(|byte| byte.is_ascii_digit())
                })
                .ok_or_else(|| {
                    OfflineExportError::Validation(format!(
                        "offline index contains an invalid frame query: {url}"
                    ))
                })?
                .parse::<usize>()
                .map_err(|_| {
                    OfflineExportError::Validation(format!(
                        "offline index frame number is too large: {url}"
                    ))
                })
        })
        .transpose()?;
    Ok((relative.to_owned(), frame))
}

fn safe_join(root: &Path, relative: &str) -> Result<PathBuf, OfflineExportError> {
    if !valid_portable_relative(relative) {
        return Err(OfflineExportError::Validation(format!(
            "unsafe relative export path: {relative}"
        )));
    }
    Ok(relative
        .split('/')
        .fold(root.to_path_buf(), |path, part| path.join(part)))
}

fn valid_portable_relative(relative: &str) -> bool {
    !relative.is_empty()
        && !relative.starts_with('/')
        && !relative.contains(['\\', ':', '\0'])
        && relative
            .split('/')
            .all(|part| !part.is_empty() && part != "." && part != "..")
}

fn is_dicom_relative(relative: &str) -> bool {
    valid_portable_relative(relative)
        && relative.starts_with("DICOM/")
        && Path::new(relative)
            .extension()
            .is_some_and(|extension| extension.eq_ignore_ascii_case("dcm"))
}

fn collect_files(root: &Path) -> Result<BTreeMap<String, PathBuf>, OfflineExportError> {
    let mut files = BTreeMap::new();
    collect_files_from(root, root, &mut files)?;
    Ok(files)
}

fn collect_files_from(
    root: &Path,
    directory: &Path,
    files: &mut BTreeMap<String, PathBuf>,
) -> Result<(), OfflineExportError> {
    let entries = fs::read_dir(directory)
        .map_err(|source| io_error("scan export directory", directory, source))?;
    for entry in entries {
        let entry =
            entry.map_err(|source| io_error("read export directory entry", directory, source))?;
        let path = entry.path();
        let metadata = fs::symlink_metadata(&path)
            .map_err(|source| io_error("inspect export path", &path, source))?;
        if metadata.file_type().is_symlink() {
            return Err(OfflineExportError::SymlinkNotAllowed(path));
        }
        if metadata.is_dir() {
            collect_files_from(root, &path, files)?;
        } else if metadata.is_file() {
            let relative_path = path.strip_prefix(root).map_err(|_| {
                OfflineExportError::Validation("export path escaped its root".to_owned())
            })?;
            let relative = portable_relative(relative_path)?;
            if files.insert(relative.clone(), path).is_some() {
                return Err(OfflineExportError::Validation(format!(
                    "duplicate export path: {relative}"
                )));
            }
        } else {
            return Err(OfflineExportError::Validation(format!(
                "unsupported export filesystem object: {}",
                path.display()
            )));
        }
    }
    Ok(())
}

fn portable_relative(path: &Path) -> Result<String, OfflineExportError> {
    let mut parts = Vec::new();
    for component in path.components() {
        match component {
            Component::Normal(part) => parts.push(part.to_string_lossy().into_owned()),
            _ => {
                return Err(OfflineExportError::Validation(format!(
                    "path is not a safe relative path: {}",
                    path.display()
                )));
            }
        }
    }
    let relative = parts.join("/");
    if valid_portable_relative(&relative) {
        Ok(relative)
    } else {
        Err(OfflineExportError::Validation(format!(
            "path is not portable: {}",
            path.display()
        )))
    }
}

fn copy_and_hash(source: &Path, destination: &Path) -> Result<(String, u64), OfflineExportError> {
    validate_source(source)?;
    let source_file = File::open(source).map_err(|error| map_source_error(source, error))?;
    let destination_file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(destination)
        .map_err(|source| io_error("create copied DICOM", destination, source))?;
    let mut reader = BufReader::with_capacity(COPY_BUFFER_BYTES, source_file);
    let mut writer = BufWriter::with_capacity(COPY_BUFFER_BYTES, destination_file);
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; COPY_BUFFER_BYTES];
    let mut size = 0_u64;
    loop {
        let read = reader
            .read(&mut buffer)
            .map_err(|error| map_source_error(source, error))?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
        writer
            .write_all(&buffer[..read])
            .map_err(|source| io_error("copy DICOM bytes", destination, source))?;
        size = size
            .checked_add(u64::try_from(read).expect("buffer length always fits u64"))
            .ok_or_else(|| OfflineExportError::Validation("DICOM size overflow".to_owned()))?;
    }
    sync_writer(writer, "sync copied DICOM", destination)?;
    Ok((format!("{:x}", hasher.finalize()), size))
}

fn hash_file(path: &Path) -> Result<(String, u64), OfflineExportError> {
    reject_symlink(path)?;
    let file =
        File::open(path).map_err(|source| io_error("open file for hashing", path, source))?;
    let mut reader = BufReader::with_capacity(COPY_BUFFER_BYTES, file);
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; COPY_BUFFER_BYTES];
    let mut size = 0_u64;
    loop {
        let read = reader
            .read(&mut buffer)
            .map_err(|source| io_error("read file for hashing", path, source))?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
        size = size
            .checked_add(u64::try_from(read).expect("buffer length always fits u64"))
            .ok_or_else(|| OfflineExportError::Validation("file size overflow".to_owned()))?;
    }
    Ok((format!("{:x}", hasher.finalize()), size))
}

fn reject_symlink(path: &Path) -> Result<(), OfflineExportError> {
    let metadata =
        fs::symlink_metadata(path).map_err(|source| io_error("inspect path", path, source))?;
    if metadata.file_type().is_symlink() {
        Err(OfflineExportError::SymlinkNotAllowed(path.to_path_buf()))
    } else {
        Ok(())
    }
}

fn create_directory(path: &Path) -> Result<(), OfflineExportError> {
    fs::create_dir(path).map_err(|source| io_error("create directory", path, source))
}

fn create_directories(path: &Path) -> Result<(), OfflineExportError> {
    fs::create_dir_all(path).map_err(|source| io_error("create directories", path, source))
}

fn sync_writer(
    mut writer: BufWriter<File>,
    operation: &'static str,
    path: &Path,
) -> Result<(), OfflineExportError> {
    writer
        .flush()
        .map_err(|source| io_error(operation, path, source))?;
    writer
        .get_ref()
        .sync_all()
        .map_err(|source| io_error(operation, path, source))
}

fn map_source_error(path: &Path, error: io::Error) -> OfflineExportError {
    if error.kind() == io::ErrorKind::NotFound {
        OfflineExportError::InvalidSource(path.to_path_buf())
    } else {
        io_error("read DICOM source", path, error)
    }
}

fn io_error(operation: &'static str, path: &Path, source: io::Error) -> OfflineExportError {
    OfflineExportError::Io {
        operation,
        path: path.to_path_buf(),
        source,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use tempfile::TempDir;

    fn source(root: &Path, name: &str, bytes: &[u8]) -> PathBuf {
        let path = root.join(name);
        fs::write(&path, bytes).unwrap();
        path
    }

    fn instance(source: PathBuf, sop: &str, frame_count: u32) -> OfflineInstance {
        OfflineInstance {
            source,
            study_instance_uid: "1.2.840.1".to_owned(),
            series_instance_uid: "1.2.840.1.1".to_owned(),
            sop_instance_uid: sop.to_owned(),
            metadata: json!({
                "PatientName": "测试患者",
                "PatientID": "P1",
                "StudyDate": "20260810",
                "AccessionNumber": "A1",
                "StudyDescription": "CT",
                "SeriesNumber": 2,
                "SeriesDescription": "AXIAL",
                "Modality": "CT",
                "InstanceNumber": 3,
                "SOPInstanceUID": sop,
            }),
            frame_count,
        }
    }

    fn request(root: &Path, instances: Vec<OfflineInstance>) -> OfflineExportRequest {
        OfflineExportRequest {
            output_root: root.to_path_buf(),
            export_name: "DCMGET_OFFLINE_TEST".to_owned(),
            instances,
        }
    }

    #[test]
    fn publishes_validated_export_with_one_atomic_directory_rename() {
        let temp = TempDir::new().unwrap();
        let source = source(temp.path(), "source.dcm", b"dicom-one");
        let final_path = fs::canonicalize(temp.path())
            .unwrap()
            .join("DCMGET_OFFLINE_TEST");

        let result =
            export_offline(&request(temp.path(), vec![instance(source, "1.2.3", 1)])).unwrap();

        assert_eq!(result.output_directory, final_path);
        assert!(final_path.is_dir());
        assert_eq!(result.exported_count, 1);
        assert_eq!(result.indexed_count, 1);
        assert!(!final_path.join("DICOMDIR").exists());
        assert!(temp.path().read_dir().unwrap().all(|entry| {
            !entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .ends_with(".partial")
        }));
        assert_eq!(
            verify_offline_export(&final_path).unwrap(),
            OfflineVerification {
                manifest_file_count: 2,
                dicom_file_count: 1,
                indexed_count: 1,
            }
        );
    }

    #[test]
    fn duplicate_sop_with_same_content_is_deduplicated() {
        let temp = TempDir::new().unwrap();
        let first = source(temp.path(), "first.dcm", b"same-content");
        let second = source(temp.path(), "second.dcm", b"same-content");

        let result = export_offline(&request(
            temp.path(),
            vec![instance(first, "1.2.3", 1), instance(second, "1.2.3", 1)],
        ))
        .unwrap();

        assert_eq!(result.exported_count, 1);
        assert_eq!(result.duplicate_count, 1);
        assert_eq!(result.indexed_count, 1);
    }

    #[test]
    fn duplicate_sop_with_different_content_never_publishes() {
        let temp = TempDir::new().unwrap();
        let first = source(temp.path(), "first.dcm", b"first-content");
        let second = source(temp.path(), "second.dcm", b"second-content");

        let error = export_offline(&request(
            temp.path(),
            vec![instance(first, "1.2.3", 1), instance(second, "1.2.3", 1)],
        ))
        .unwrap_err();

        assert!(matches!(error, OfflineExportError::SopConflict(uid) if uid == "1.2.3"));
        assert!(!temp.path().join("DCMGET_OFFLINE_TEST").exists());
        assert!(temp.path().read_dir().unwrap().all(|entry| {
            !entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .ends_with(".partial")
        }));
    }

    #[test]
    fn index_uses_only_local_python_compatible_dicomweb_urls() {
        let temp = TempDir::new().unwrap();
        let source = source(temp.path(), "frames.dcm", b"multi-frame-dicom");
        let result =
            export_offline(&request(temp.path(), vec![instance(source, "1.2.3.4", 3)])).unwrap();
        let index_path = result.output_directory.join(STUDY_INDEX_PATH);
        let payload: Value = serde_json::from_slice(&fs::read(index_path).unwrap()).unwrap();
        let instances = payload["studies"][0]["series"][0]["instances"]
            .as_array()
            .unwrap();
        let urls = instances
            .iter()
            .map(|entry| entry["url"].as_str().unwrap())
            .collect::<Vec<_>>();

        assert_eq!(
            urls,
            vec![
                "dicomweb:/DICOM/1.2.3.4.dcm?frame=1",
                "dicomweb:/DICOM/1.2.3.4.dcm?frame=2",
                "dicomweb:/DICOM/1.2.3.4.dcm?frame=3",
            ]
        );
        assert!(
            urls.iter()
                .all(|url| !url.contains("http://") && !url.contains("https://"))
        );
        assert_eq!(payload["studies"][0]["NumInstances"], 3);
        assert_eq!(payload["studies"][0]["Modalities"], "CT");
    }

    #[test]
    fn copying_preserves_source_sha256() {
        let temp = TempDir::new().unwrap();
        let source = source(temp.path(), "source.dcm", b"immutable-source-payload");
        let before = hash_file(&source).unwrap().0;

        let result = export_offline(&request(
            temp.path(),
            vec![instance(source.clone(), "1.2.3", 1)],
        ))
        .unwrap();

        assert_eq!(hash_file(&source).unwrap().0, before);
        assert_eq!(result.dicom_files[0].sha256, before);
        assert_eq!(
            hash_file(&result.output_directory.join("DICOM/1.2.3.dcm"))
                .unwrap()
                .0,
            before
        );
    }

    #[test]
    fn path_escape_and_existing_destination_are_rejected() {
        let temp = TempDir::new().unwrap();
        let source_path = source(temp.path(), "source.dcm", b"dicom");
        let mut invalid = request(temp.path(), vec![instance(source_path.clone(), "1.2.3", 1)]);
        invalid.export_name = "../escape".to_owned();
        assert!(matches!(
            export_offline(&invalid),
            Err(OfflineExportError::InvalidExportName(_))
        ));
        let mut invalid_uid = request(temp.path(), vec![instance(source_path, "1.2.3/escape", 1)]);
        invalid_uid.export_name = "SAFE".to_owned();
        assert!(matches!(
            export_offline(&invalid_uid),
            Err(OfflineExportError::InvalidUid { .. })
        ));

        let destination = temp.path().join("ALREADY_THERE");
        fs::create_dir(&destination).unwrap();
        fs::write(destination.join("keep.txt"), b"keep").unwrap();
        let source = source(temp.path(), "source-2.dcm", b"dicom-2");
        let mut existing = request(temp.path(), vec![instance(source, "1.2.4", 1)]);
        existing.export_name = "ALREADY_THERE".to_owned();
        assert!(matches!(
            export_offline(&existing),
            Err(OfflineExportError::DestinationExists(_))
        ));
        assert_eq!(fs::read(destination.join("keep.txt")).unwrap(), b"keep");
    }

    #[cfg(unix)]
    #[test]
    fn source_and_output_symlinks_are_rejected() {
        use std::os::unix::fs::symlink;

        let temp = TempDir::new().unwrap();
        let source = source(temp.path(), "source.dcm", b"dicom");
        let linked_source = temp.path().join("linked-source.dcm");
        symlink(&source, &linked_source).unwrap();
        assert!(matches!(
            export_offline(&request(
                temp.path(),
                vec![instance(linked_source, "1.2.3", 1)]
            )),
            Err(OfflineExportError::SymlinkNotAllowed(_))
        ));

        let real_output = temp.path().join("real-output");
        fs::create_dir(&real_output).unwrap();
        let linked_output = temp.path().join("linked-output");
        symlink(&real_output, &linked_output).unwrap();
        assert!(matches!(
            export_offline(&request(&linked_output, vec![instance(source, "1.2.3", 1)])),
            Err(OfflineExportError::SymlinkNotAllowed(_))
        ));
    }

    #[cfg(unix)]
    #[test]
    fn independent_verifier_rejects_internal_symlinks() {
        use std::os::unix::fs::symlink;

        let temp = TempDir::new().unwrap();
        let source = source(temp.path(), "source.dcm", b"dicom");
        let result = export_offline(&request(
            temp.path(),
            vec![instance(source.clone(), "1.2.3", 1)],
        ))
        .unwrap();
        let exported = result.output_directory.join("DICOM/1.2.3.dcm");
        fs::remove_file(&exported).unwrap();
        symlink(source, exported).unwrap();

        assert!(matches!(
            verify_offline_export(&result.output_directory),
            Err(OfflineExportError::SymlinkNotAllowed(_))
        ));
    }
}
