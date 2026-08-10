use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::time::Duration;

use chrono::{SecondsFormat, Utc};
use dcmget_domain::{
    AccessionResult, AccessionStatus, AppConfig, PdiResult, PdiStatus, Profile, ProfileId,
    ProfileRuntimeStatus, Task, TaskId, TaskPhase, TaskSummary,
};
use rusqlite::{Connection, OptionalExtension, Transaction, TransactionBehavior, params};
use serde_json::Value;
use thiserror::Error;

const STATE_SCHEMA_VERSION: u32 = 1;

#[derive(Debug, Error)]
pub enum StateError {
    #[error("state path has no parent: {0}")]
    InvalidStatePath(PathBuf),
    #[error("state schema version {found} is not supported (expected {expected})")]
    UnsupportedSchema { found: u32, expected: u32 },
    #[error("profile {0} does not exist")]
    ProfileNotFound(ProfileId),
    #[error("task {0} does not exist")]
    TaskNotFound(TaskId),
    #[error("task {0} cannot be deleted while it is active")]
    ActiveTask(TaskId),
    #[error("terminal task {0} still has unresolved accession state")]
    UnresolvedTerminalTask(TaskId),
    #[error("invalid persisted data: {0}")]
    InvalidData(String),
    #[error(transparent)]
    Domain(#[from] dcmget_domain::DomainError),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
    #[error(transparent)]
    Sqlite(#[from] rusqlite::Error),
    #[error(transparent)]
    Io(#[from] std::io::Error),
}

#[derive(Clone, Debug)]
pub struct StateRepository {
    path: PathBuf,
}

impl StateRepository {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, StateError> {
        let path = path.as_ref().to_path_buf();
        let parent = path
            .parent()
            .ok_or_else(|| StateError::InvalidStatePath(path.clone()))?;
        std::fs::create_dir_all(parent)?;
        let repository = Self { path };
        repository.initialize()?;
        Ok(repository)
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    fn connect(&self) -> Result<Connection, StateError> {
        let connection = Connection::open(&self.path)?;
        connection.busy_timeout(Duration::from_secs(5))?;
        connection.execute_batch("PRAGMA foreign_keys=ON;")?;
        Ok(connection)
    }

    fn initialize(&self) -> Result<(), StateError> {
        let connection = self.connect()?;
        connection.execute_batch(
            r"
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS state_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS profiles (
                profile_id TEXT PRIMARY KEY,
                legacy_number INTEGER UNIQUE,
                display_name TEXT NOT NULL,
                config_json TEXT NOT NULL,
                source_config_path TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE RESTRICT,
                name TEXT NOT NULL,
                phase TEXT NOT NULL,
                config_json TEXT NOT NULL,
                trial_required INTEGER NOT NULL,
                trial_consumed INTEGER NOT NULL DEFAULT 0,
                pdi_attempt_id TEXT NOT NULL DEFAULT '',
                current_accession TEXT NOT NULL DEFAULT '',
                speed_bytes_per_second REAL NOT NULL DEFAULT 0,
                error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS tasks_by_profile
                ON tasks(profile_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS accessions (
                task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                accession TEXT NOT NULL,
                status TEXT,
                file_count INTEGER NOT NULL DEFAULT 0,
                received_bytes INTEGER NOT NULL DEFAULT 0,
                speed_bytes_per_second REAL NOT NULL DEFAULT 0,
                result_json TEXT,
                partial_json TEXT,
                PRIMARY KEY(task_id, position),
                UNIQUE(task_id, accession)
            );
            CREATE INDEX IF NOT EXISTS accessions_pending
                ON accessions(task_id, position) WHERE result_json IS NULL;
            CREATE TABLE IF NOT EXISTS pdi_results (
                task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                output_directory TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                warnings_json TEXT NOT NULL DEFAULT '[]',
                source_count INTEGER NOT NULL DEFAULT 0,
                exported_count INTEGER NOT NULL DEFAULT 0,
                duplicate_count INTEGER NOT NULL DEFAULT 0,
                indexed_count INTEGER NOT NULL DEFAULT 0,
                strict_profile INTEGER,
                core_tool_failure INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS migration_records (
                source_key TEXT PRIMARY KEY,
                source_digest TEXT NOT NULL,
                backup_path TEXT NOT NULL,
                migrated_at TEXT NOT NULL
            );
            ",
        )?;
        let version: Option<String> = connection
            .query_row(
                "SELECT value FROM state_metadata WHERE key='schema_version'",
                [],
                |row| row.get(0),
            )
            .optional()?;
        if let Some(version) = version {
            let found = version
                .parse::<u32>()
                .map_err(|_| StateError::InvalidData("invalid state schema version".into()))?;
            if found != STATE_SCHEMA_VERSION {
                return Err(StateError::UnsupportedSchema {
                    found,
                    expected: STATE_SCHEMA_VERSION,
                });
            }
        }
        connection.execute(
            "INSERT OR IGNORE INTO state_metadata(key,value) VALUES('schema_version', ?1)",
            [STATE_SCHEMA_VERSION.to_string()],
        )?;
        Ok(())
    }

    pub fn upsert_profile(&self, profile: &Profile) -> Result<(), StateError> {
        let now = now();
        let created_at = if profile.created_at.is_empty() {
            now.as_str()
        } else {
            profile.created_at.as_str()
        };
        let updated_at = if profile.updated_at.is_empty() {
            now.as_str()
        } else {
            profile.updated_at.as_str()
        };
        let legacy_number = profile
            .id
            .as_str()
            .strip_prefix('i')
            .and_then(|value| value.parse::<u32>().ok());
        self.connect()?.execute(
            r"
            INSERT INTO profiles(
                profile_id, legacy_number, display_name, config_json,
                source_config_path, created_at, updated_at
            ) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7)
            ON CONFLICT(profile_id) DO UPDATE SET
                legacy_number=excluded.legacy_number,
                display_name=excluded.display_name,
                config_json=excluded.config_json,
                source_config_path=excluded.source_config_path,
                updated_at=excluded.updated_at
            ",
            params![
                profile.id.as_str(),
                legacy_number,
                profile.display_name,
                serde_json::to_string(&profile.config)?,
                profile.source_config_path,
                created_at,
                updated_at,
            ],
        )?;
        Ok(())
    }

    pub fn get_profile(&self, profile_id: &ProfileId) -> Result<Profile, StateError> {
        self.connect()?
            .query_row(
                r"
                SELECT display_name, config_json, source_config_path,
                       created_at, updated_at
                FROM profiles WHERE profile_id=?1
                ",
                [profile_id.as_str()],
                |row| profile_from_row_offset(profile_id.clone(), row, 0),
            )
            .optional()?
            .ok_or_else(|| StateError::ProfileNotFound(profile_id.clone()))
    }

    pub fn list_profiles(&self) -> Result<Vec<Profile>, StateError> {
        let connection = self.connect()?;
        let mut statement = connection.prepare(
            r"
            SELECT profile_id, display_name, config_json, source_config_path,
                   created_at, updated_at
            FROM profiles
            ORDER BY COALESCE(legacy_number, 2147483647), profile_id
            ",
        )?;
        let rows = statement.query_map([], |row| {
            let id: String = row.get(0)?;
            let id = ProfileId::new(id).map_err(domain_to_sql)?;
            profile_from_row_offset(id, row, 1)
        })?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(StateError::from)
    }

    pub fn create_task(
        &self,
        profile_id: &ProfileId,
        name: impl Into<String>,
        config: AppConfig,
        accessions: Vec<String>,
        trial_required: bool,
    ) -> Result<Task, StateError> {
        let timestamp = now();
        let task = Task {
            id: TaskId::generate(),
            profile_id: profile_id.clone(),
            name: name.into(),
            phase: TaskPhase::Queued,
            config,
            accessions,
            results: Vec::new(),
            partial_results: Vec::new(),
            trial_required,
            trial_consumed: false,
            pdi_attempt_id: String::new(),
            current_accession: String::new(),
            speed_bytes_per_second: 0.0,
            error_message: String::new(),
            created_at: timestamp.clone(),
            updated_at: timestamp,
        };
        self.insert_task(&task)?;
        Ok(task)
    }

    pub fn insert_task(&self, task: &Task) -> Result<(), StateError> {
        task.validate_new()?;
        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        if !profile_exists(&transaction, &task.profile_id)? {
            return Err(StateError::ProfileNotFound(task.profile_id.clone()));
        }
        insert_task_row(&transaction, task)?;
        transaction.commit()?;
        Ok(())
    }

    pub fn get_task(&self, task_id: &TaskId) -> Result<Task, StateError> {
        let connection = self.connect()?;
        let row = connection
            .query_row(
                r"
                SELECT profile_id, name, phase, config_json, trial_required,
                       trial_consumed, pdi_attempt_id, current_accession,
                       speed_bytes_per_second, error_message, created_at, updated_at
                FROM tasks WHERE task_id=?1
                ",
                [task_id.as_str()],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, String>(3)?,
                        row.get::<_, bool>(4)?,
                        row.get::<_, bool>(5)?,
                        row.get::<_, String>(6)?,
                        row.get::<_, String>(7)?,
                        row.get::<_, f64>(8)?,
                        row.get::<_, String>(9)?,
                        row.get::<_, String>(10)?,
                        row.get::<_, String>(11)?,
                    ))
                },
            )
            .optional()?
            .ok_or_else(|| StateError::TaskNotFound(task_id.clone()))?;
        let mut accessions = Vec::new();
        let mut results = Vec::new();
        let mut partial_results = Vec::new();
        let mut statement = connection.prepare(
            "SELECT accession,result_json,partial_json FROM accessions WHERE task_id=?1 ORDER BY position",
        )?;
        let rows = statement.query_map([task_id.as_str()], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, Option<String>>(1)?,
                row.get::<_, Option<String>>(2)?,
            ))
        })?;
        for row in rows {
            let (accession, result_json, partial_json) = row?;
            accessions.push(accession);
            if let Some(value) = result_json {
                results.push(AccessionResult::from_legacy_json(&value)?);
            }
            if let Some(value) = partial_json {
                partial_results.push(AccessionResult::from_legacy_json(&value)?);
            }
        }
        Ok(Task {
            id: task_id.clone(),
            profile_id: ProfileId::new(row.0)?,
            name: row.1,
            phase: TaskPhase::from_str(&row.2)?,
            config: serde_json::from_str(&row.3)?,
            accessions,
            results,
            partial_results,
            trial_required: row.4,
            trial_consumed: row.5,
            pdi_attempt_id: row.6,
            current_accession: row.7,
            speed_bytes_per_second: row.8,
            error_message: row.9,
            created_at: row.10,
            updated_at: row.11,
        })
    }

    pub fn has_task(&self, task_id: &TaskId) -> Result<bool, StateError> {
        Ok(self.connect()?.query_row(
            "SELECT EXISTS(SELECT 1 FROM tasks WHERE task_id=?1)",
            [task_id.as_str()],
            |row| row.get(0),
        )?)
    }

    pub fn list_task_summaries(&self) -> Result<Vec<TaskSummary>, StateError> {
        let connection = self.connect()?;
        let ids = {
            let mut statement =
                connection.prepare("SELECT task_id FROM tasks ORDER BY created_at DESC")?;
            statement
                .query_map([], |row| row.get::<_, String>(0))?
                .collect::<Result<Vec<_>, _>>()?
        };
        ids.into_iter()
            .map(|id| task_summary(&connection, &TaskId::new(id)?))
            .collect()
    }

    pub fn get_task_summary(&self, task_id: &TaskId) -> Result<TaskSummary, StateError> {
        task_summary(&self.connect()?, task_id)
    }

    pub fn next_pending(&self, task_id: &TaskId) -> Result<Option<String>, StateError> {
        let connection = self.connect()?;
        require_task(&connection, task_id)?;
        connection
            .query_row(
                "SELECT accession FROM accessions WHERE task_id=?1 AND result_json IS NULL ORDER BY position LIMIT 1",
                [task_id.as_str()],
                |row| row.get(0),
            )
            .optional()
            .map_err(StateError::from)
    }

    pub fn set_task_phase(&self, task_id: &TaskId, phase: TaskPhase) -> Result<(), StateError> {
        let changed = self.connect()?.execute(
            "UPDATE tasks SET phase=?1,updated_at=?2 WHERE task_id=?3",
            params![phase.to_string(), now(), task_id.as_str()],
        )?;
        if changed == 0 {
            return Err(StateError::TaskNotFound(task_id.clone()));
        }
        Ok(())
    }

    /// Requeue only failed or partial accessions for an explicit retry.
    ///
    /// Completed and confirmed no-data results remain final, so a resumed task
    /// never repeats successful C-MOVEs. The prior failed result is retained as
    /// partial diagnostic state while files already on disk remain available
    /// for SOP-instance de-duplication by the receiver.
    pub fn reset_results_for_retry(&self, task_id: &TaskId) -> Result<u64, StateError> {
        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        require_task(&transaction, task_id)?;
        let changed = transaction.execute(
            r"
            UPDATE accessions
            SET status=NULL,
                partial_json=COALESCE(partial_json,result_json),
                result_json=NULL,
                speed_bytes_per_second=0
            WHERE task_id=?1 AND status IN ('失败','部分成功')
            ",
            [task_id.as_str()],
        )?;
        transaction.commit()?;
        u64::try_from(changed)
            .map_err(|_| StateError::InvalidData("retry row count exceeds u64".into()))
    }

    /// Convert every unresolved accession into an explicit cancelled result so
    /// a terminal cancelled task remains inspectable and can be deleted.
    pub fn finalize_cancelled_accessions(&self, task_id: &TaskId) -> Result<u64, StateError> {
        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        require_task(&transaction, task_id)?;
        let pending = {
            let mut statement = transaction.prepare(
                "SELECT accession,file_count,received_bytes,speed_bytes_per_second FROM accessions WHERE task_id=?1 AND result_json IS NULL",
            )?;
            statement
                .query_map([task_id.as_str()], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, i64>(2)?,
                        row.get::<_, f64>(3)?,
                    ))
                })?
                .collect::<Result<Vec<_>, _>>()?
        };
        for (accession, file_count, received_bytes, speed_bytes_per_second) in &pending {
            let mut result = AccessionResult::blank(accession, AccessionStatus::Cancelled);
            result.file_count = from_i64(*file_count)?;
            result.received_bytes = from_i64(*received_bytes)?;
            result.speed_bytes_per_second = (*speed_bytes_per_second).max(0.0);
            "任务已取消".clone_into(&mut result.message);
            transaction.execute(
                r"
                UPDATE accessions SET status='已取消',result_json=?1,partial_json=NULL
                WHERE task_id=?2 AND accession=?3 AND result_json IS NULL
                ",
                params![result.to_legacy_json()?, task_id.as_str(), accession],
            )?;
        }
        transaction.commit()?;
        u64::try_from(pending.len())
            .map_err(|_| StateError::InvalidData("cancelled row count exceeds u64".into()))
    }

    pub fn update_task_runtime(
        &self,
        task_id: &TaskId,
        current_accession: &str,
        speed_bytes_per_second: f64,
        error_message: &str,
    ) -> Result<(), StateError> {
        let changed = self.connect()?.execute(
            r"
            UPDATE tasks SET current_accession=?1,speed_bytes_per_second=?2,
                             error_message=?3,updated_at=?4
            WHERE task_id=?5
            ",
            params![
                current_accession,
                speed_bytes_per_second.max(0.0),
                error_message,
                now(),
                task_id.as_str(),
            ],
        )?;
        if changed == 0 {
            return Err(StateError::TaskNotFound(task_id.clone()));
        }
        Ok(())
    }

    pub fn record_result(
        &self,
        task_id: &TaskId,
        result: &AccessionResult,
    ) -> Result<(), StateError> {
        let connection = self.connect()?;
        require_task(&connection, task_id)?;
        let exists: bool = connection.query_row(
            "SELECT EXISTS(SELECT 1 FROM accessions WHERE task_id=?1 AND accession=?2)",
            params![task_id.as_str(), result.accession],
            |row| row.get(0),
        )?;
        if !exists {
            return Err(StateError::InvalidData(format!(
                "accession {} is not part of task {task_id}",
                result.accession
            )));
        }
        if result.status == AccessionStatus::Downloading {
            connection.execute(
                r"
                UPDATE accessions SET file_count=?1,received_bytes=?2,
                    speed_bytes_per_second=?3
                WHERE task_id=?4 AND accession=?5 AND result_json IS NULL
                ",
                params![
                    to_i64(result.file_count)?,
                    to_i64(result.received_bytes)?,
                    result.speed_bytes_per_second.max(0.0),
                    task_id.as_str(),
                    result.accession,
                ],
            )?;
        } else if result.status == AccessionStatus::Cancelled {
            connection.execute(
                r"
                UPDATE accessions SET status=NULL,file_count=?1,received_bytes=?2,
                    speed_bytes_per_second=?3,partial_json=?4
                WHERE task_id=?5 AND accession=?6 AND result_json IS NULL
                ",
                params![
                    to_i64(result.file_count)?,
                    to_i64(result.received_bytes)?,
                    result.speed_bytes_per_second.max(0.0),
                    result.to_legacy_json()?,
                    task_id.as_str(),
                    result.accession,
                ],
            )?;
        } else if result.status.is_final() {
            connection.execute(
                r"
                UPDATE accessions SET status=?1,file_count=?2,received_bytes=?3,
                    speed_bytes_per_second=?4,result_json=?5,partial_json=NULL
                WHERE task_id=?6 AND accession=?7
                ",
                params![
                    result.status.as_legacy_str(),
                    to_i64(result.file_count)?,
                    to_i64(result.received_bytes)?,
                    result.speed_bytes_per_second.max(0.0),
                    result.to_legacy_json()?,
                    task_id.as_str(),
                    result.accession,
                ],
            )?;
        }
        Ok(())
    }

    pub fn save_pdi_result(&self, task_id: &TaskId, result: &PdiResult) -> Result<(), StateError> {
        let connection = self.connect()?;
        require_task(&connection, task_id)?;
        let status = serde_json::to_value(result.status)?
            .as_str()
            .ok_or_else(|| StateError::InvalidData("invalid PDI status".into()))?
            .to_owned();
        connection.execute(
            r"
            INSERT INTO pdi_results(
                task_id,status,output_directory,message,warnings_json,
                source_count,exported_count,duplicate_count,indexed_count,
                strict_profile,core_tool_failure,updated_at
            ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12)
            ON CONFLICT(task_id) DO UPDATE SET
                status=excluded.status,output_directory=excluded.output_directory,
                message=excluded.message,warnings_json=excluded.warnings_json,
                source_count=excluded.source_count,exported_count=excluded.exported_count,
                duplicate_count=excluded.duplicate_count,indexed_count=excluded.indexed_count,
                strict_profile=excluded.strict_profile,
                core_tool_failure=excluded.core_tool_failure,updated_at=excluded.updated_at
            ",
            params![
                task_id.as_str(),
                status,
                result.output_directory,
                result.message,
                serde_json::to_string(&result.warnings)?,
                to_i64(result.source_count)?,
                to_i64(result.exported_count)?,
                to_i64(result.duplicate_count)?,
                to_i64(result.indexed_count)?,
                result.strict_profile,
                result.core_tool_failure,
                now(),
            ],
        )?;
        Ok(())
    }

    pub fn load_pdi_result(&self, task_id: &TaskId) -> Result<Option<PdiResult>, StateError> {
        self.connect()?
            .query_row(
                r"
                SELECT status,output_directory,message,warnings_json,source_count,
                       exported_count,duplicate_count,indexed_count,strict_profile,
                       core_tool_failure
                FROM pdi_results WHERE task_id=?1
                ",
                [task_id.as_str()],
                |row| {
                    let status: String = row.get(0)?;
                    let status: PdiStatus =
                        serde_json::from_value(Value::String(status)).map_err(json_to_sql)?;
                    let warnings: String = row.get(3)?;
                    Ok(PdiResult {
                        status,
                        output_directory: row.get(1)?,
                        message: row.get(2)?,
                        warnings: serde_json::from_str(&warnings).map_err(json_to_sql)?,
                        source_count: from_i64(row.get(4)?)?,
                        exported_count: from_i64(row.get(5)?)?,
                        duplicate_count: from_i64(row.get(6)?)?,
                        indexed_count: from_i64(row.get(7)?)?,
                        strict_profile: row.get(8)?,
                        core_tool_failure: row.get(9)?,
                    })
                },
            )
            .optional()
            .map_err(StateError::from)
    }

    pub fn delete_task(&self, task_id: &TaskId) -> Result<(), StateError> {
        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let phase: String = transaction
            .query_row(
                "SELECT phase FROM tasks WHERE task_id=?1",
                [task_id.as_str()],
                |row| row.get(0),
            )
            .optional()?
            .ok_or_else(|| StateError::TaskNotFound(task_id.clone()))?;
        let phase = TaskPhase::from_str(&phase)?;
        if !phase.can_delete() {
            return Err(StateError::ActiveTask(task_id.clone()));
        }
        if phase.is_terminal() {
            let has_unresolved: bool = transaction.query_row(
                "SELECT EXISTS(SELECT 1 FROM accessions WHERE task_id=?1 AND (result_json IS NULL OR partial_json IS NOT NULL))",
                [task_id.as_str()],
                |row| row.get(0),
            )?;
            if has_unresolved {
                return Err(StateError::UnresolvedTerminalTask(task_id.clone()));
            }
        }
        transaction.execute("DELETE FROM tasks WHERE task_id=?1", [task_id.as_str()])?;
        transaction.commit()?;
        Ok(())
    }

    pub(crate) fn migration_record(
        &self,
        source_key: &str,
    ) -> Result<Option<(String, String)>, StateError> {
        self.connect()?
            .query_row(
                "SELECT source_digest,backup_path FROM migration_records WHERE source_key=?1",
                [source_key],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()
            .map_err(StateError::from)
    }

    pub(crate) fn record_migration(
        &self,
        source_key: &str,
        digest: &str,
        backup_path: &Path,
    ) -> Result<(), StateError> {
        self.connect()?.execute(
            r"
            INSERT INTO migration_records(source_key,source_digest,backup_path,migrated_at)
            VALUES(?1,?2,?3,?4)
            ON CONFLICT(source_key) DO UPDATE SET source_digest=excluded.source_digest,
                backup_path=excluded.backup_path,migrated_at=excluded.migrated_at
            ",
            params![source_key, digest, backup_path.to_string_lossy(), now()],
        )?;
        Ok(())
    }
}

fn insert_task_row(transaction: &Transaction<'_>, task: &Task) -> Result<(), StateError> {
    transaction.execute(
        r"
        INSERT INTO tasks(
            task_id,profile_id,name,phase,config_json,trial_required,
            trial_consumed,pdi_attempt_id,current_accession,
            speed_bytes_per_second,error_message,created_at,updated_at
        ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13)
        ",
        params![
            task.id.as_str(),
            task.profile_id.as_str(),
            task.name,
            task.phase.to_string(),
            serde_json::to_string(&task.config)?,
            task.trial_required,
            task.trial_consumed,
            task.pdi_attempt_id,
            task.current_accession,
            task.speed_bytes_per_second.max(0.0),
            task.error_message,
            task.created_at,
            task.updated_at,
        ],
    )?;
    transaction.execute_batch("PRAGMA defer_foreign_keys=ON;")?;
    let result_by_accession = task
        .results
        .iter()
        .map(|result| (result.accession.as_str(), result))
        .collect::<std::collections::BTreeMap<_, _>>();
    let partial_by_accession = task
        .partial_results
        .iter()
        .map(|result| (result.accession.as_str(), result))
        .collect::<std::collections::BTreeMap<_, _>>();
    for (position, accession) in task.accessions.iter().enumerate() {
        let result = result_by_accession.get(accession.as_str()).copied();
        let partial = partial_by_accession.get(accession.as_str()).copied();
        let shown = result.or(partial);
        transaction.execute(
            r"
            INSERT INTO accessions(
                task_id,position,accession,status,file_count,received_bytes,
                speed_bytes_per_second,result_json,partial_json
            ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)
            ",
            params![
                task.id.as_str(),
                i64::try_from(position)
                    .map_err(|_| StateError::InvalidData("too many accessions".into()))?,
                accession,
                result.map(|value| value.status.as_legacy_str()),
                to_i64(shown.map_or(0, |value| value.file_count))?,
                to_i64(shown.map_or(0, |value| value.received_bytes))?,
                shown.map_or(0.0, |value| value.speed_bytes_per_second.max(0.0)),
                result.map(AccessionResult::to_legacy_json).transpose()?,
                partial.map(AccessionResult::to_legacy_json).transpose()?,
            ],
        )?;
    }
    Ok(())
}

fn task_summary(connection: &Connection, task_id: &TaskId) -> Result<TaskSummary, StateError> {
    let task = connection.query_row(
        "SELECT profile_id,name,phase,current_accession,speed_bytes_per_second,error_message,created_at,updated_at FROM tasks WHERE task_id=?1",
        [task_id.as_str()],
        |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?, row.get::<_, String>(2)?,
            row.get::<_, String>(3)?, row.get::<_, f64>(4)?, row.get::<_, String>(5)?,
            row.get::<_, String>(6)?, row.get::<_, String>(7)?)),
    ).optional()?.ok_or_else(|| StateError::TaskNotFound(task_id.clone()))?;
    let counts = connection.query_row(
        r"
        SELECT COUNT(*),
            COALESCE(SUM(CASE WHEN status IS NOT NULL THEN 1 ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN status IN ('完成','无数据') THEN 1 ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN status IN ('失败','部分成功') THEN 1 ELSE 0 END),0),
            COALESCE(SUM(file_count),0),COALESCE(SUM(received_bytes),0),
            COALESCE(SUM(CASE WHEN status='无数据' THEN 1 ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN status='部分成功' THEN 1 ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN status='已取消' THEN 1 ELSE 0 END),0)
        FROM accessions WHERE task_id=?1
        ",
        [task_id.as_str()],
        |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, i64>(1)?,
                row.get::<_, i64>(2)?,
                row.get::<_, i64>(3)?,
                row.get::<_, i64>(4)?,
                row.get::<_, i64>(5)?,
                row.get::<_, i64>(6)?,
                row.get::<_, i64>(7)?,
                row.get::<_, i64>(8)?,
            ))
        },
    )?;
    let total = from_i64(counts.0)?;
    let processed = from_i64(counts.1)?;
    Ok(TaskSummary {
        task_id: task_id.clone(),
        profile_id: ProfileId::new(task.0)?,
        name: task.1,
        phase: TaskPhase::from_str(&task.2)?,
        total_count: total,
        processed_count: processed,
        pending_count: total.saturating_sub(processed),
        completed_count: from_i64(counts.2)?,
        failed_count: from_i64(counts.3)?,
        file_count: from_i64(counts.4)?,
        received_bytes: from_i64(counts.5)?,
        speed_bytes_per_second: task.4.max(0.0),
        current_accession: task.3,
        error_message: task.5,
        created_at: task.6,
        updated_at: task.7,
        no_data_count: from_i64(counts.6)?,
        partial_count: from_i64(counts.7)?,
        cancelled_count: from_i64(counts.8)?,
    })
}

fn profile_from_row_offset(
    profile_id: ProfileId,
    row: &rusqlite::Row<'_>,
    offset: usize,
) -> rusqlite::Result<Profile> {
    let config_json: String = row.get(offset + 1)?;
    let config = serde_json::from_str(&config_json).map_err(json_to_sql)?;
    Ok(Profile {
        id: profile_id,
        display_name: row.get(offset)?,
        config,
        runtime_status: ProfileRuntimeStatus::Stopped,
        source_config_path: row.get(offset + 2)?,
        created_at: row.get(offset + 3)?,
        updated_at: row.get(offset + 4)?,
    })
}

fn profile_exists(
    transaction: &Transaction<'_>,
    profile_id: &ProfileId,
) -> Result<bool, StateError> {
    Ok(transaction.query_row(
        "SELECT EXISTS(SELECT 1 FROM profiles WHERE profile_id=?1)",
        [profile_id.as_str()],
        |row| row.get(0),
    )?)
}

fn require_task(connection: &Connection, task_id: &TaskId) -> Result<(), StateError> {
    let exists: bool = connection.query_row(
        "SELECT EXISTS(SELECT 1 FROM tasks WHERE task_id=?1)",
        [task_id.as_str()],
        |row| row.get(0),
    )?;
    if exists {
        Ok(())
    } else {
        Err(StateError::TaskNotFound(task_id.clone()))
    }
}

fn to_i64(value: u64) -> Result<i64, StateError> {
    i64::try_from(value).map_err(|_| StateError::InvalidData("integer exceeds SQLite range".into()))
}

fn from_i64(value: i64) -> rusqlite::Result<u64> {
    u64::try_from(value).map_err(|error| {
        rusqlite::Error::FromSqlConversionFailure(
            0,
            rusqlite::types::Type::Integer,
            Box::new(error),
        )
    })
}

fn domain_to_sql(error: dcmget_domain::DomainError) -> rusqlite::Error {
    rusqlite::Error::FromSqlConversionFailure(0, rusqlite::types::Type::Text, Box::new(error))
}

fn json_to_sql(error: serde_json::Error) -> rusqlite::Error {
    rusqlite::Error::FromSqlConversionFailure(0, rusqlite::types::Type::Text, Box::new(error))
}

fn now() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Micros, true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn repository() -> (TempDir, StateRepository, ProfileId) {
        let temp = tempfile::tempdir().unwrap();
        let repository = StateRepository::open(temp.path().join("state.sqlite3")).unwrap();
        let profile_id = ProfileId::legacy(1).unwrap();
        repository
            .upsert_profile(&Profile {
                id: profile_id.clone(),
                display_name: "实例 1".into(),
                config: AppConfig::default(),
                runtime_status: ProfileRuntimeStatus::Stopped,
                source_config_path: String::new(),
                created_at: String::new(),
                updated_at: String::new(),
            })
            .unwrap();
        (temp, repository, profile_id)
    }

    #[test]
    fn profiles_and_tasks_round_trip() {
        let (_temp, repository, profile_id) = repository();
        let task = repository
            .create_task(
                &profile_id,
                "测试",
                AppConfig::default(),
                vec!["A001".into(), "A002".into()],
                false,
            )
            .unwrap();
        let mut completed = AccessionResult::blank("A001", AccessionStatus::Completed);
        completed.file_count = 2;
        completed.received_bytes = 100;
        completed.archived_files = vec!["1.dcm".into(), "2.dcm".into()];
        repository.record_result(&task.id, &completed).unwrap();
        let summary = repository.get_task_summary(&task.id).unwrap();
        assert_eq!(summary.total_count, 2);
        assert_eq!(summary.processed_count, 1);
        assert_eq!(summary.file_count, 2);
        assert_eq!(
            repository.next_pending(&task.id).unwrap().as_deref(),
            Some("A002")
        );
        let loaded = repository.get_task(&task.id).unwrap();
        assert_eq!(loaded.results, [completed]);
    }

    #[test]
    fn retry_requeues_only_failed_and_partial_accessions() {
        let (_temp, repository, profile_id) = repository();
        let task = repository
            .create_task(
                &profile_id,
                "retry",
                AppConfig::default(),
                vec!["A001".into(), "A002".into(), "A003".into()],
                false,
            )
            .unwrap();
        repository
            .record_result(
                &task.id,
                &AccessionResult::blank("A001", AccessionStatus::Completed),
            )
            .unwrap();
        repository
            .record_result(
                &task.id,
                &AccessionResult::blank("A002", AccessionStatus::Failed),
            )
            .unwrap();
        assert_eq!(repository.reset_results_for_retry(&task.id).unwrap(), 1);
        let loaded = repository.get_task(&task.id).unwrap();
        assert_eq!(
            loaded
                .results
                .iter()
                .map(|result| result.accession.as_str())
                .collect::<Vec<_>>(),
            ["A001"]
        );
        assert_eq!(loaded.partial_results[0].accession, "A002");
        assert_eq!(
            repository.next_pending(&task.id).unwrap().as_deref(),
            Some("A002")
        );
    }

    #[test]
    fn explicit_cancellation_resolves_pending_rows_for_deletion() {
        let (_temp, repository, profile_id) = repository();
        let task = repository
            .create_task(
                &profile_id,
                "cancel",
                AppConfig::default(),
                vec!["A001".into(), "A002".into()],
                false,
            )
            .unwrap();

        assert_eq!(
            repository.finalize_cancelled_accessions(&task.id).unwrap(),
            2
        );
        repository
            .set_task_phase(&task.id, TaskPhase::Cancelled)
            .unwrap();
        let summary = repository.get_task_summary(&task.id).unwrap();
        assert_eq!(summary.cancelled_count, 2);

        repository.delete_task(&task.id).unwrap();
        assert!(!repository.has_task(&task.id).unwrap());
    }

    #[test]
    fn delete_is_restricted_to_terminal_or_retryable_tasks() {
        let (_temp, repository, profile_id) = repository();
        let task = repository
            .create_task(
                &profile_id,
                "active",
                AppConfig::default(),
                vec!["A001".into(), "A002".into()],
                false,
            )
            .unwrap();
        assert!(matches!(
            repository.delete_task(&task.id),
            Err(StateError::ActiveTask(_))
        ));
        repository
            .record_result(
                &task.id,
                &AccessionResult::blank("A001", AccessionStatus::Cancelled),
            )
            .unwrap();
        repository
            .set_task_phase(&task.id, TaskPhase::Completed)
            .unwrap();
        assert!(matches!(
            repository.delete_task(&task.id),
            Err(StateError::UnresolvedTerminalTask(_))
        ));
        assert!(repository.get_task(&task.id).is_ok());
        for accession in ["A001", "A002"] {
            repository
                .record_result(
                    &task.id,
                    &AccessionResult::blank(accession, AccessionStatus::Completed),
                )
                .unwrap();
        }
        repository.delete_task(&task.id).unwrap();
        assert!(matches!(
            repository.get_task(&task.id),
            Err(StateError::TaskNotFound(_))
        ));
    }

    #[test]
    fn pdi_result_round_trips() {
        let (_temp, repository, profile_id) = repository();
        let task = repository
            .create_task(
                &profile_id,
                "pdi",
                AppConfig::default(),
                vec!["A001".into()],
                false,
            )
            .unwrap();
        let result = PdiResult {
            status: PdiStatus::Partial,
            output_directory: "PDI".into(),
            message: "viewer missing".into(),
            warnings: vec!["warning".into()],
            source_count: 3,
            exported_count: 2,
            duplicate_count: 1,
            indexed_count: 2,
            strict_profile: Some(false),
            core_tool_failure: false,
        };
        repository.save_pdi_result(&task.id, &result).unwrap();
        assert_eq!(repository.load_pdi_result(&task.id).unwrap(), Some(result));
    }
}
