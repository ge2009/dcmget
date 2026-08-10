use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::str::FromStr;

use chrono::{SecondsFormat, Utc};
use dcmget_domain::{
    AccessionResult, AppConfig, PdiResult, PdiStatus, Profile, ProfileId, ProfileRuntimeStatus,
    Task, TaskId, TaskPhase,
};
use rusqlite::{Connection, OpenFlags, OptionalExtension};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::{BackupService, BackupSet, StateError, StateRepository};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LegacyProfileSource {
    pub profile_id: ProfileId,
    pub config_path: PathBuf,
    pub metadata_path: Option<PathBuf>,
    pub active_task_path: Option<PathBuf>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LegacyLayout {
    pub config_root: PathBuf,
    pub state_root: PathBuf,
    pub root_active_task_path: Option<PathBuf>,
    pub task_catalog_path: Option<PathBuf>,
    pub profiles: Vec<LegacyProfileSource>,
}

impl LegacyLayout {
    pub fn discover(
        config_root: impl AsRef<Path>,
        state_root: impl AsRef<Path>,
    ) -> Result<Self, StateError> {
        let config_root = config_root.as_ref().to_path_buf();
        let state_root = state_root.as_ref().to_path_buf();
        let mut profiles = Vec::new();
        let instances = config_root.join("instances");
        if instances.is_dir() {
            for entry in fs::read_dir(&instances)? {
                let entry = entry?;
                if entry.file_type()?.is_symlink() || !entry.file_type()?.is_dir() {
                    continue;
                }
                let Some(number) = legacy_profile_number(&entry.file_name().to_string_lossy())
                else {
                    continue;
                };
                let config_path = entry.path().join("config.json");
                if !config_path.is_file() {
                    continue;
                }
                let metadata_path = entry.path().join("profile-meta.json");
                let active_task = state_root
                    .join("instances")
                    .join(format!("i{number}"))
                    .join("active-task.sqlite3");
                profiles.push(LegacyProfileSource {
                    profile_id: ProfileId::legacy(number)?,
                    config_path,
                    metadata_path: metadata_path.is_file().then_some(metadata_path),
                    active_task_path: active_task.is_file().then_some(active_task),
                });
            }
        }
        profiles.sort_by(|left, right| left.profile_id.cmp(&right.profile_id));

        // Products before profile isolation stored a single config and active
        // checkpoint at the roots. Import them as i1 only if no i1 profile was
        // discovered; the source files remain untouched.
        let root_config = config_root.join("config.json");
        if root_config.is_file()
            && !profiles
                .iter()
                .any(|profile| profile.profile_id.as_str() == "i1")
        {
            let active = state_root.join("active-task.sqlite3");
            profiles.push(LegacyProfileSource {
                profile_id: ProfileId::legacy(1)?,
                config_path: root_config,
                metadata_path: None,
                active_task_path: active.is_file().then_some(active),
            });
        }

        let root_active_task = state_root.join("active-task.sqlite3");
        let catalog = state_root.join("tasks.sqlite3");
        Ok(Self {
            config_root,
            state_root,
            root_active_task_path: root_active_task.is_file().then_some(root_active_task),
            task_catalog_path: catalog.is_file().then_some(catalog),
            profiles,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MigrationWarning {
    pub source: PathBuf,
    pub message: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MigrationReport {
    pub backup: BackupSet,
    pub profiles_imported: usize,
    pub tasks_imported: usize,
    pub sources_skipped: usize,
    pub warnings: Vec<MigrationWarning>,
}

#[derive(Clone, Debug)]
pub struct MigrationService {
    backup: BackupService,
}

impl MigrationService {
    pub fn new(backup_root: impl AsRef<Path>) -> Self {
        Self {
            backup: BackupService::new(backup_root),
        }
    }

    /// Back up every existing source first, then import only from those
    /// immutable copies. No legacy file is opened for writing or deleted.
    // The sequential order is part of the migration contract: profiles first,
    // then per-profile checkpoints, root checkpoint, and finally the catalog.
    #[allow(clippy::too_many_lines)]
    pub fn migrate(
        &self,
        layout: &LegacyLayout,
        repository: &StateRepository,
    ) -> Result<MigrationReport, StateError> {
        let sources = migration_sources(layout);
        let backup = self.backup.create(&sources)?;
        let mut report = MigrationReport {
            backup,
            profiles_imported: 0,
            tasks_imported: 0,
            sources_skipped: 0,
            warnings: Vec::new(),
        };

        for source in &layout.profiles {
            let Some(config_entry) = report.backup.entry_for(&source.config_path) else {
                continue;
            };
            if already_migrated(repository, config_entry)? {
                report.sources_skipped += 1;
                continue;
            }
            match import_profile(source, &report.backup, repository, &mut report.warnings) {
                Ok(()) => {
                    report.profiles_imported += 1;
                    repository.record_migration(
                        &source_key(&config_entry.source),
                        &config_entry.sha256,
                        &config_entry.backup,
                    )?;
                }
                Err(error) => report.warnings.push(MigrationWarning {
                    source: source.config_path.clone(),
                    message: error.to_string(),
                }),
            }
        }

        for source in &layout.profiles {
            let Some(active_path) = &source.active_task_path else {
                continue;
            };
            let Some(entry) = report.backup.entry_for(active_path) else {
                continue;
            };
            if already_migrated(repository, entry)? {
                report.sources_skipped += 1;
                continue;
            }
            match import_active_checkpoint(&entry.backup, Some(&source.profile_id), repository) {
                Ok(imported) => {
                    report.tasks_imported += imported;
                    repository.record_migration(
                        &source_key(&entry.source),
                        &entry.sha256,
                        &entry.backup,
                    )?;
                }
                Err(error) => report.warnings.push(MigrationWarning {
                    source: active_path.clone(),
                    message: error.to_string(),
                }),
            }
        }

        if let Some(active_path) = &layout.root_active_task_path
            && let Some(entry) = report.backup.entry_for(active_path)
        {
            if already_migrated(repository, entry)? {
                report.sources_skipped += 1;
            } else {
                match import_active_checkpoint(&entry.backup, None, repository) {
                    Ok(imported) => {
                        report.tasks_imported += imported;
                        repository.record_migration(
                            &source_key(&entry.source),
                            &entry.sha256,
                            &entry.backup,
                        )?;
                    }
                    Err(error) => report.warnings.push(MigrationWarning {
                        source: active_path.clone(),
                        message: error.to_string(),
                    }),
                }
            }
        }

        if let Some(catalog_path) = &layout.task_catalog_path
            && let Some(entry) = report.backup.entry_for(catalog_path)
        {
            if already_migrated(repository, entry)? {
                report.sources_skipped += 1;
            } else {
                match import_task_catalog(&entry.backup, repository, &mut report.warnings) {
                    Ok(imported) => {
                        report.tasks_imported += imported;
                        repository.record_migration(
                            &source_key(&entry.source),
                            &entry.sha256,
                            &entry.backup,
                        )?;
                    }
                    Err(error) => report.warnings.push(MigrationWarning {
                        source: catalog_path.clone(),
                        message: error.to_string(),
                    }),
                }
            }
        }
        Ok(report)
    }
}

fn migration_sources(layout: &LegacyLayout) -> Vec<(PathBuf, bool)> {
    let mut seen = BTreeSet::new();
    let mut sources = Vec::new();
    for profile in &layout.profiles {
        for (path, sqlite) in [
            (Some(&profile.config_path), false),
            (profile.metadata_path.as_ref(), false),
            (profile.active_task_path.as_ref(), true),
        ] {
            if let Some(path) = path
                && seen.insert(path.clone())
            {
                sources.push((path.clone(), sqlite));
            }
        }
    }
    if let Some(path) = &layout.root_active_task_path
        && seen.insert(path.clone())
    {
        sources.push((path.clone(), true));
    }
    if let Some(path) = &layout.task_catalog_path
        && seen.insert(path.clone())
    {
        sources.push((path.clone(), true));
    }
    sources
}

fn import_profile(
    source: &LegacyProfileSource,
    backup: &BackupSet,
    repository: &StateRepository,
    warnings: &mut Vec<MigrationWarning>,
) -> Result<(), StateError> {
    let config_entry = backup
        .entry_for(&source.config_path)
        .ok_or_else(|| StateError::InvalidData("profile backup missing".into()))?;
    let config = AppConfig::from_json_slice(&fs::read(&config_entry.backup)?)?;
    let fallback = format!(
        "实例 {}",
        source.profile_id.as_str().trim_start_matches('i')
    );
    let display_name = source
        .metadata_path
        .as_ref()
        .and_then(|path| backup.entry_for(path))
        .map(|entry| read_display_name(&entry.backup))
        .transpose()
        .unwrap_or_else(|error| {
            warnings.push(MigrationWarning {
                source: source.metadata_path.clone().unwrap_or_default(),
                message: format!("Profile metadata ignored: {error}"),
            });
            None
        })
        .flatten()
        .unwrap_or(fallback);
    let timestamp = now();
    repository.upsert_profile(&Profile {
        id: source.profile_id.clone(),
        display_name,
        config,
        runtime_status: ProfileRuntimeStatus::Stopped,
        source_config_path: source.config_path.to_string_lossy().into_owned(),
        created_at: timestamp.clone(),
        updated_at: timestamp,
    })
}

fn read_display_name(path: &Path) -> Result<Option<String>, StateError> {
    let value: Value = serde_json::from_slice(&fs::read(path)?)?;
    let Some(object) = value.as_object() else {
        return Err(StateError::InvalidData(
            "profile metadata is not an object".into(),
        ));
    };
    if object.get("schema").and_then(Value::as_str) != Some("dcmget-profile-meta")
        || object.get("version").and_then(Value::as_u64) != Some(1)
    {
        return Err(StateError::InvalidData(
            "unsupported profile metadata".into(),
        ));
    }
    Ok(object
        .get("display_name")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_owned))
}

fn import_active_checkpoint(
    path: &Path,
    profile_hint: Option<&ProfileId>,
    repository: &StateRepository,
) -> Result<usize, StateError> {
    let connection = read_only_sqlite(path)?;
    require_tables(&connection, &["metadata", "accessions"])?;
    let metadata = connection
        .prepare("SELECT key,value FROM metadata")?
        .query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })?
        .collect::<Result<BTreeMap<_, _>, _>>()?;
    if metadata.get("version").map(String::as_str) != Some("1") {
        return Err(StateError::InvalidData(
            "unsupported active-task schema".into(),
        ));
    }
    let task_id = TaskId::new(required_metadata(&metadata, "task_id")?)?;
    if repository.has_task(&task_id)? {
        return Ok(0);
    }
    let config: AppConfig = serde_json::from_str(required_metadata(&metadata, "config")?)?;
    let profile_id = match profile_hint {
        Some(profile_id) => profile_id.clone(),
        None => profile_for_config(repository, &config)?,
    };
    ensure_profile(repository, &profile_id, &config)?;
    let mut accessions = Vec::new();
    let mut results = Vec::new();
    let mut partial_results = Vec::new();
    let mut statement = connection
        .prepare("SELECT accession,result_json,partial_json FROM accessions ORDER BY position")?;
    for row in statement.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, Option<String>>(1)?,
            row.get::<_, Option<String>>(2)?,
        ))
    })? {
        let (accession, result, partial) = row?;
        accessions.push(accession);
        if let Some(value) = result {
            results.push(AccessionResult::from_legacy_json(&value)?);
        }
        if let Some(value) = partial {
            partial_results.push(AccessionResult::from_legacy_json(&value)?);
        }
    }
    let phase = match metadata.get("phase").map_or("downloading", String::as_str) {
        "downloading" => TaskPhase::Queued,
        other => TaskPhase::from_str(other)?,
    };
    repository.insert_task(&Task {
        id: task_id.clone(),
        profile_id,
        name: format!(
            "恢复任务 {}",
            &task_id.as_str()[..task_id.as_str().len().min(8)]
        ),
        phase,
        config,
        accessions,
        results,
        partial_results,
        trial_required: metadata.get("trial_required").map(String::as_str) == Some("1"),
        trial_consumed: false,
        pdi_attempt_id: metadata.get("pdi_attempt_id").cloned().unwrap_or_default(),
        current_accession: String::new(),
        speed_bytes_per_second: 0.0,
        error_message: metadata
            .get("interrupted_reason")
            .cloned()
            .unwrap_or_default(),
        created_at: required_metadata(&metadata, "created_at")?.to_owned(),
        updated_at: now(),
    })?;
    Ok(1)
}

// Keeping the complete legacy row mapping in one function makes schema review
// and future migrations auditable against the Python CREATE TABLE statement.
#[allow(clippy::too_many_lines)]
fn import_task_catalog(
    path: &Path,
    repository: &StateRepository,
    warnings: &mut Vec<MigrationWarning>,
) -> Result<usize, StateError> {
    let connection = read_only_sqlite(path)?;
    require_tables(&connection, &["catalog_metadata", "tasks", "accessions"])?;
    let version: Option<String> = connection
        .query_row(
            "SELECT value FROM catalog_metadata WHERE key='version'",
            [],
            |row| row.get(0),
        )
        .optional()?;
    if version.as_deref() != Some("1") {
        return Err(StateError::InvalidData(
            "unsupported task catalog schema".into(),
        ));
    }
    let columns = table_columns(&connection, "tasks")?;
    let trial_consumed = if columns.contains("trial_consumed") {
        "trial_consumed"
    } else {
        "0"
    };
    let pdi_attempt = if columns.contains("pdi_attempt_id") {
        "pdi_attempt_id"
    } else {
        "''"
    };
    let sql = format!(
        "SELECT task_id,name,phase,config_json,trial_required,{trial_consumed},{pdi_attempt},current_accession,speed_bytes_per_second,error_message,created_at,updated_at FROM tasks ORDER BY created_at"
    );
    let mut statement = connection.prepare(&sql)?;
    let rows = statement.query_map([], |row| {
        Ok(LegacyTaskRow {
            task_id: row.get(0)?,
            name: row.get(1)?,
            phase: row.get(2)?,
            config_json: row.get(3)?,
            trial_required: row.get(4)?,
            trial_consumed: row.get(5)?,
            pdi_attempt_id: row.get(6)?,
            current_accession: row.get(7)?,
            speed: row.get(8)?,
            error_message: row.get(9)?,
            created_at: row.get(10)?,
            updated_at: row.get(11)?,
        })
    })?;
    let mut imported = 0;
    for row in rows {
        let row = row?;
        let task_id = TaskId::new(row.task_id)?;
        if repository.has_task(&task_id)? {
            continue;
        }
        let config: AppConfig = serde_json::from_str(&row.config_json)?;
        let profile_id = profile_for_config(repository, &config)?;
        let mut accessions = Vec::new();
        let mut results = Vec::new();
        let mut partial_results = Vec::new();
        let mut accession_statement = connection.prepare(
            "SELECT accession,result_json,partial_json FROM accessions WHERE task_id=?1 ORDER BY position",
        )?;
        for accession_row in accession_statement.query_map([task_id.as_str()], |item| {
            Ok((
                item.get::<_, String>(0)?,
                item.get::<_, Option<String>>(1)?,
                item.get::<_, Option<String>>(2)?,
            ))
        })? {
            let (accession, result, partial) = accession_row?;
            accessions.push(accession);
            if let Some(value) = result {
                results.push(AccessionResult::from_legacy_json(&value)?);
            }
            if let Some(value) = partial {
                partial_results.push(AccessionResult::from_legacy_json(&value)?);
            }
        }
        let imported_phase = TaskPhase::from_str(&row.phase)?;
        let (phase, terminal_state_repaired) =
            normalize_imported_phase(imported_phase, &accessions, &results, &partial_results);
        let error_message = if terminal_state_repaired {
            append_migration_note(
                &row.error_message,
                "旧版任务终态仍包含未完成检查号，已转为可恢复状态",
            )
        } else {
            row.error_message
        };
        repository.insert_task(&Task {
            id: task_id.clone(),
            profile_id,
            name: row.name,
            phase,
            config,
            accessions,
            results,
            partial_results,
            trial_required: row.trial_required,
            trial_consumed: row.trial_consumed,
            pdi_attempt_id: row.pdi_attempt_id,
            current_accession: row.current_accession,
            speed_bytes_per_second: row.speed.max(0.0),
            error_message,
            created_at: row.created_at,
            updated_at: row.updated_at,
        })?;
        if table_exists(&connection, "pdi_results")?
            && let Some(result) = read_pdi_result(&connection, &task_id)?
        {
            repository.save_pdi_result(&task_id, &result)?;
        }
        imported += 1;
    }
    for table in ["task_processes", "receiver_sessions"] {
        if table_exists(&connection, table)? {
            let count: i64 =
                connection.query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| {
                    row.get(0)
                })?;
            if count > 0 {
                warnings.push(MigrationWarning {
                    source: path.to_path_buf(),
                    message: format!(
                        "{count} obsolete {table} row(s) were not restored as live processes"
                    ),
                });
            }
        }
    }
    Ok(imported)
}

struct LegacyTaskRow {
    task_id: String,
    name: String,
    phase: String,
    config_json: String,
    trial_required: bool,
    trial_consumed: bool,
    pdi_attempt_id: String,
    current_accession: String,
    speed: f64,
    error_message: String,
    created_at: String,
    updated_at: String,
}

fn read_pdi_result(
    connection: &Connection,
    task_id: &TaskId,
) -> Result<Option<PdiResult>, StateError> {
    connection.query_row(
        "SELECT status,output_directory,message,warnings_json,source_count,exported_count,duplicate_count,indexed_count,strict_profile,core_tool_failure FROM pdi_results WHERE task_id=?1",
        [task_id.as_str()],
        |row| {
            let status: String = row.get(0)?;
            let status: PdiStatus = serde_json::from_value(Value::String(status)).map_err(json_to_sql)?;
            let warnings: String = row.get(3)?;
            Ok(PdiResult {
                status, output_directory: row.get(1)?, message: row.get(2)?,
                warnings: serde_json::from_str(&warnings).map_err(json_to_sql)?,
                source_count: nonnegative(row.get(4)?)?, exported_count: nonnegative(row.get(5)?)?,
                duplicate_count: nonnegative(row.get(6)?)?, indexed_count: nonnegative(row.get(7)?)?,
                strict_profile: row.get(8)?, core_tool_failure: row.get(9)?,
            })
        },
    ).optional().map_err(StateError::from)
}

fn profile_for_config(
    repository: &StateRepository,
    config: &AppConfig,
) -> Result<ProfileId, StateError> {
    if let Some(profile) = repository
        .list_profiles()?
        .into_iter()
        .find(|profile| profile.config == *config)
    {
        return Ok(profile.id);
    }
    let fingerprint = profile_config_fingerprint(config)?;
    let candidate = ProfileId::new(format!("legacy-{fingerprint}"))?;
    match repository.get_profile(&candidate) {
        Ok(profile) if profile.config == *config => Ok(candidate),
        Ok(_) => Err(StateError::InvalidData(format!(
            "profile fingerprint collision for {candidate}"
        ))),
        Err(StateError::ProfileNotFound(_)) => {
            insert_migrated_profile(repository, &candidate, config)?;
            Ok(candidate)
        }
        Err(error) => Err(error),
    }
}

fn ensure_profile(
    repository: &StateRepository,
    profile_id: &ProfileId,
    config: &AppConfig,
) -> Result<(), StateError> {
    match repository.get_profile(profile_id) {
        Ok(_) => Ok(()),
        Err(StateError::ProfileNotFound(_)) => {
            insert_migrated_profile(repository, profile_id, config)
        }
        Err(error) => Err(error),
    }
}

fn insert_migrated_profile(
    repository: &StateRepository,
    profile_id: &ProfileId,
    config: &AppConfig,
) -> Result<(), StateError> {
    let timestamp = now();
    repository.upsert_profile(&Profile {
        id: profile_id.clone(),
        display_name: format!("迁移 Profile {profile_id}"),
        config: config.clone(),
        runtime_status: ProfileRuntimeStatus::Stopped,
        source_config_path: String::new(),
        created_at: timestamp.clone(),
        updated_at: timestamp,
    })
}

fn profile_config_fingerprint(config: &AppConfig) -> Result<String, StateError> {
    let mut canonical = Vec::new();
    write_canonical_json(&config.to_legacy_value(), &mut canonical)?;
    Ok(format!("{:x}", Sha256::digest(canonical)))
}

fn write_canonical_json(value: &Value, output: &mut Vec<u8>) -> Result<(), StateError> {
    match value {
        Value::Array(values) => {
            output.push(b'[');
            for (index, value) in values.iter().enumerate() {
                if index > 0 {
                    output.push(b',');
                }
                write_canonical_json(value, output)?;
            }
            output.push(b']');
        }
        Value::Object(values) => {
            output.push(b'{');
            let mut keys = values.keys().collect::<Vec<_>>();
            keys.sort_unstable();
            for (index, key) in keys.into_iter().enumerate() {
                if index > 0 {
                    output.push(b',');
                }
                serde_json::to_writer(&mut *output, key)?;
                output.push(b':');
                write_canonical_json(&values[key], output)?;
            }
            output.push(b'}');
        }
        scalar => serde_json::to_writer(output, scalar)?,
    }
    Ok(())
}

fn normalize_imported_phase(
    phase: TaskPhase,
    accessions: &[String],
    results: &[AccessionResult],
    partial_results: &[AccessionResult],
) -> (TaskPhase, bool) {
    let final_accessions = results
        .iter()
        .map(|result| result.accession.as_str())
        .collect::<BTreeSet<_>>();
    let unresolved = !partial_results.is_empty()
        || accessions
            .iter()
            .any(|accession| !final_accessions.contains(accession.as_str()));
    if phase.is_terminal() && unresolved {
        (TaskPhase::DownloadRetryable, true)
    } else {
        (phase, false)
    }
}

fn append_migration_note(existing: &str, note: &str) -> String {
    if existing.trim().is_empty() {
        note.to_owned()
    } else {
        format!("{existing}; {note}")
    }
}

fn already_migrated(
    repository: &StateRepository,
    entry: &crate::BackupEntry,
) -> Result<bool, StateError> {
    Ok(repository
        .migration_record(&source_key(&entry.source))?
        .is_some_and(|(digest, _)| digest == entry.sha256))
}

fn read_only_sqlite(path: &Path) -> Result<Connection, StateError> {
    Ok(Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?)
}

fn require_tables(connection: &Connection, names: &[&str]) -> Result<(), StateError> {
    for name in names {
        if !table_exists(connection, name)? {
            return Err(StateError::InvalidData(format!(
                "legacy database is missing table {name}"
            )));
        }
    }
    Ok(())
}

fn table_exists(connection: &Connection, name: &str) -> Result<bool, StateError> {
    Ok(connection.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1)",
        [name],
        |row| row.get(0),
    )?)
}

fn table_columns(connection: &Connection, table: &str) -> Result<BTreeSet<String>, StateError> {
    let mut statement = connection.prepare(&format!("PRAGMA table_info({table})"))?;
    Ok(statement
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<Result<BTreeSet<_>, _>>()?)
}

fn required_metadata<'a>(
    metadata: &'a BTreeMap<String, String>,
    key: &str,
) -> Result<&'a str, StateError> {
    metadata
        .get(key)
        .map(String::as_str)
        .ok_or_else(|| StateError::InvalidData(format!("active task is missing {key}")))
}

fn source_key(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

fn legacy_profile_number(value: &str) -> Option<u32> {
    value
        .strip_prefix('i')?
        .parse::<u32>()
        .ok()
        .filter(|number| (1..=9_999).contains(number))
}

fn now() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Micros, true)
}

fn nonnegative(value: i64) -> rusqlite::Result<u64> {
    u64::try_from(value).map_err(|error| {
        rusqlite::Error::FromSqlConversionFailure(
            0,
            rusqlite::types::Type::Integer,
            Box::new(error),
        )
    })
}

fn json_to_sql(error: serde_json::Error) -> rusqlite::Error {
    rusqlite::Error::FromSqlConversionFailure(0, rusqlite::types::Type::Text, Box::new(error))
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::params;

    fn write_profile(config_root: &Path, state_root: &Path) -> (PathBuf, PathBuf) {
        let profile = config_root.join("instances/i1");
        fs::create_dir_all(&profile).unwrap();
        fs::create_dir_all(state_root.join("instances/i1")).unwrap();
        fs::write(
            profile.join("config.json"),
            serde_json::to_vec(&AppConfig::default()).unwrap(),
        )
        .unwrap();
        fs::write(
            profile.join("profile-meta.json"),
            r#"{"schema":"dcmget-profile-meta","version":1,"display_name":"测试节点"}"#,
        )
        .unwrap();
        (
            profile.join("config.json"),
            state_root.join("instances/i1/active-task.sqlite3"),
        )
    }

    fn write_active_task(path: &Path) -> TaskId {
        let id = TaskId::generate();
        let connection = Connection::open(path).unwrap();
        connection.execute_batch("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL); CREATE TABLE accessions(position INTEGER PRIMARY KEY,accession TEXT NOT NULL UNIQUE,result_json TEXT,partial_json TEXT);").unwrap();
        let config = serde_json::to_string(&AppConfig::default()).unwrap();
        for (key, value) in [
            ("version", "1".to_owned()),
            ("task_id", id.to_string()),
            ("created_at", "2026-01-01T00:00:00+00:00".into()),
            ("trial_required", "0".into()),
            ("phase", "downloading".into()),
            ("config", config),
        ] {
            connection
                .execute(
                    "INSERT INTO metadata(key,value) VALUES(?1,?2)",
                    params![key, value],
                )
                .unwrap();
        }
        connection
            .execute(
                "INSERT INTO accessions(position,accession) VALUES(0,'A001')",
                [],
            )
            .unwrap();
        id
    }

    fn write_catalog_with_unresolved_terminal_task(path: &Path, config: &AppConfig) -> TaskId {
        let id = TaskId::generate();
        let connection = Connection::open(path).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE catalog_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                 CREATE TABLE tasks(
                    task_id TEXT PRIMARY KEY,name TEXT NOT NULL,phase TEXT NOT NULL,
                    config_json TEXT NOT NULL,trial_required INTEGER NOT NULL,
                    trial_consumed INTEGER NOT NULL,pdi_attempt_id TEXT NOT NULL,
                    current_accession TEXT NOT NULL,speed_bytes_per_second REAL NOT NULL,
                    error_message TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
                 );
                 CREATE TABLE accessions(
                    task_id TEXT NOT NULL,position INTEGER NOT NULL,accession TEXT NOT NULL,
                    status TEXT,file_count INTEGER NOT NULL,received_bytes INTEGER NOT NULL,
                    speed_bytes_per_second REAL NOT NULL,result_json TEXT,partial_json TEXT
                 );
                 INSERT INTO catalog_metadata(key,value) VALUES('version','1');",
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO tasks(
                    task_id,name,phase,config_json,trial_required,trial_consumed,
                    pdi_attempt_id,current_accession,speed_bytes_per_second,
                    error_message,created_at,updated_at
                 ) VALUES(?1,'legacy completed','completed',?2,0,0,'','',0,'',?3,?3)",
                params![
                    id.as_str(),
                    serde_json::to_string(config).unwrap(),
                    "2026-01-01T00:00:00+00:00"
                ],
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO accessions(
                    task_id,position,accession,status,file_count,received_bytes,
                    speed_bytes_per_second,result_json,partial_json
                 ) VALUES(?1,0,'A001',NULL,0,0,0,NULL,NULL)",
                [id.as_str()],
            )
            .unwrap();
        id
    }

    fn stored_profile(id: ProfileId, config: AppConfig) -> Profile {
        Profile {
            id,
            display_name: "测试 Profile".into(),
            config,
            runtime_status: ProfileRuntimeStatus::Stopped,
            source_config_path: String::new(),
            created_at: "2026-01-01T00:00:00Z".into(),
            updated_at: "2026-01-01T00:00:00Z".into(),
        }
    }

    #[test]
    fn discovers_backs_up_and_imports_profile_and_active_task_idempotently() {
        let temp = tempfile::tempdir().unwrap();
        let config_root = temp.path().join("config");
        let state_root = temp.path().join("state");
        let (_config, active) = write_profile(&config_root, &state_root);
        let task_id = write_active_task(&active);
        let layout = LegacyLayout::discover(&config_root, &state_root).unwrap();
        let repository = StateRepository::open(temp.path().join("native/state.sqlite3")).unwrap();
        let service = MigrationService::new(temp.path().join("backups"));

        let first = service.migrate(&layout, &repository).unwrap();
        assert_eq!(first.profiles_imported, 1);
        assert_eq!(first.tasks_imported, 1);
        assert!(active.is_file());
        assert_eq!(
            repository
                .get_profile(&ProfileId::legacy(1).unwrap())
                .unwrap()
                .display_name,
            "测试节点"
        );
        assert_eq!(repository.get_task(&task_id).unwrap().accessions, ["A001"]);

        let second = service.migrate(&layout, &repository).unwrap();
        assert_eq!(second.tasks_imported, 0);
        assert!(second.sources_skipped >= 2);
    }

    #[test]
    fn imports_root_checkpoint_even_when_profile_directories_exist() {
        let temp = tempfile::tempdir().unwrap();
        let config_root = temp.path().join("config");
        let state_root = temp.path().join("state");
        write_profile(&config_root, &state_root);
        let root_checkpoint = state_root.join("active-task.sqlite3");
        let task_id = write_active_task(&root_checkpoint);
        let layout = LegacyLayout::discover(&config_root, &state_root).unwrap();
        assert_eq!(layout.root_active_task_path, Some(root_checkpoint));

        let repository = StateRepository::open(temp.path().join("native/state.sqlite3")).unwrap();
        let report = MigrationService::new(temp.path().join("backups"))
            .migrate(&layout, &repository)
            .unwrap();

        assert_eq!(report.tasks_imported, 1);
        assert_eq!(
            repository.get_task(&task_id).unwrap().profile_id,
            ProfileId::legacy(1).unwrap()
        );
    }

    #[test]
    fn profile_matching_uses_the_complete_config_identity() {
        let temp = tempfile::tempdir().unwrap();
        let repository = StateRepository::open(temp.path().join("state.sqlite3")).unwrap();
        let first = AppConfig {
            pacs_server_ip: "10.0.0.1".into(),
            ..AppConfig::default()
        };
        let second = AppConfig {
            pacs_server_ip: "10.0.0.2".into(),
            ..first.clone()
        };
        let first_id = ProfileId::legacy(1).unwrap();
        let second_id = ProfileId::legacy(2).unwrap();
        repository
            .upsert_profile(&stored_profile(first_id, first))
            .unwrap();
        repository
            .upsert_profile(&stored_profile(second_id.clone(), second.clone()))
            .unwrap();

        assert_eq!(profile_for_config(&repository, &second).unwrap(), second_id);
    }

    #[test]
    fn synthetic_profile_reuse_rejects_a_fingerprint_collision() {
        let temp = tempfile::tempdir().unwrap();
        let repository = StateRepository::open(temp.path().join("state.sqlite3")).unwrap();
        let requested = AppConfig::default();
        let fingerprint = profile_config_fingerprint(&requested).unwrap();
        let candidate = ProfileId::new(format!("legacy-{fingerprint}")).unwrap();
        let conflicting = AppConfig {
            pacs_server_ip: "10.0.0.99".into(),
            ..requested.clone()
        };
        repository
            .upsert_profile(&stored_profile(candidate, conflicting))
            .unwrap();

        assert!(matches!(
            profile_for_config(&repository, &requested),
            Err(StateError::InvalidData(message)) if message.contains("fingerprint collision")
        ));
    }

    #[test]
    fn catalog_terminal_task_with_pending_rows_becomes_retryable() {
        let temp = tempfile::tempdir().unwrap();
        let catalog = temp.path().join("tasks.sqlite3");
        let config = AppConfig::default();
        let task_id = write_catalog_with_unresolved_terminal_task(&catalog, &config);
        let repository = StateRepository::open(temp.path().join("native.sqlite3")).unwrap();
        let mut warnings = Vec::new();

        assert_eq!(
            import_task_catalog(&catalog, &repository, &mut warnings).unwrap(),
            1
        );
        let imported = repository.get_task(&task_id).unwrap();
        assert_eq!(imported.phase, TaskPhase::DownloadRetryable);
        assert!(imported.error_message.contains("已转为可恢复状态"));
    }

    #[test]
    fn future_task_catalog_version_is_backed_up_but_not_imported() {
        let temp = tempfile::tempdir().unwrap();
        let config_root = temp.path().join("config");
        let state_root = temp.path().join("state");
        fs::create_dir_all(&state_root).unwrap();
        let catalog = state_root.join("tasks.sqlite3");
        let connection = Connection::open(&catalog).unwrap();
        connection.execute_batch("CREATE TABLE catalog_metadata(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE tasks(id TEXT); CREATE TABLE accessions(id TEXT); INSERT INTO catalog_metadata VALUES('version','99');").unwrap();
        let layout = LegacyLayout::discover(&config_root, &state_root).unwrap();
        let repository = StateRepository::open(temp.path().join("native/state.sqlite3")).unwrap();
        let report = MigrationService::new(temp.path().join("backups"))
            .migrate(&layout, &repository)
            .unwrap();
        assert_eq!(report.tasks_imported, 0);
        assert_eq!(report.warnings.len(), 1);
        assert!(report.backup.entry_for(&catalog).unwrap().backup.is_file());
    }
}
