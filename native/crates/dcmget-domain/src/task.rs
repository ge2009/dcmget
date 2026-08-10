use std::collections::BTreeSet;
use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use thiserror::Error;
use uuid::Uuid;

use crate::{AppConfig, ProfileId};

#[derive(Debug, Error)]
pub enum DomainError {
    #[error("expected a JSON object")]
    ExpectedJsonObject,
    #[error("invalid identifier: {0}")]
    InvalidIdentifier(String),
    #[error("invalid task: {0}")]
    InvalidTask(String),
    #[error("invalid legacy value: {0}")]
    InvalidLegacyValue(String),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
}

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize, Deserialize)]
#[serde(transparent)]
pub struct TaskId(String);

impl TaskId {
    /// Parse an external ID. Legacy 32-character UUIDs and readable native
    /// IDs share the same bounded, path-safe identifier contract.
    pub fn new(value: impl Into<String>) -> Result<Self, DomainError> {
        let value = value.into();
        if value.is_empty()
            || value.len() > 128
            || !value
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
        {
            return Err(DomainError::InvalidIdentifier(format!("task {value}")));
        }
        Ok(Self(value))
    }

    pub fn generate() -> Self {
        Self(Uuid::new_v4().simple().to_string())
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for TaskId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl FromStr for TaskId {
    type Err = DomainError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        Self::new(value)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskPhase {
    Queued,
    Running,
    PausePending,
    Paused,
    Cancelling,
    DownloadRetryable,
    PdiPending,
    PdiRunning,
    PdiRetryable,
    Cancelled,
    Completed,
    Failed,
}

impl TaskPhase {
    pub fn is_terminal(self) -> bool {
        matches!(self, Self::Cancelled | Self::Completed | Self::Failed)
    }

    pub fn can_delete(self) -> bool {
        self.is_terminal() || matches!(self, Self::DownloadRetryable | Self::PdiRetryable)
    }
}

impl fmt::Display for TaskPhase {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Queued => "queued",
            Self::Running => "running",
            Self::PausePending => "pause_pending",
            Self::Paused => "paused",
            Self::Cancelling => "cancelling",
            Self::DownloadRetryable => "download_retryable",
            Self::PdiPending => "pdi_pending",
            Self::PdiRunning => "pdi_running",
            Self::PdiRetryable => "pdi_retryable",
            Self::Cancelled => "cancelled",
            Self::Completed => "completed",
            Self::Failed => "failed",
        })
    }
}

impl FromStr for TaskPhase {
    type Err = DomainError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "queued" => Ok(Self::Queued),
            "running" => Ok(Self::Running),
            "pause_pending" => Ok(Self::PausePending),
            "paused" => Ok(Self::Paused),
            "cancelling" => Ok(Self::Cancelling),
            "download_retryable" => Ok(Self::DownloadRetryable),
            "pdi_pending" => Ok(Self::PdiPending),
            "pdi_running" => Ok(Self::PdiRunning),
            "pdi_retryable" => Ok(Self::PdiRetryable),
            "cancelled" => Ok(Self::Cancelled),
            "completed" => Ok(Self::Completed),
            "failed" => Ok(Self::Failed),
            _ => Err(DomainError::InvalidLegacyValue(format!(
                "task phase {value}"
            ))),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub enum AccessionStatus {
    #[serde(rename = "等待")]
    Waiting,
    #[serde(rename = "下载中")]
    Downloading,
    #[serde(rename = "完成")]
    Completed,
    #[serde(rename = "无数据")]
    NoData,
    #[serde(rename = "部分成功")]
    Partial,
    #[serde(rename = "失败")]
    Failed,
    #[serde(rename = "已取消")]
    Cancelled,
}

impl AccessionStatus {
    pub fn as_legacy_str(self) -> &'static str {
        match self {
            Self::Waiting => "等待",
            Self::Downloading => "下载中",
            Self::Completed => "完成",
            Self::NoData => "无数据",
            Self::Partial => "部分成功",
            Self::Failed => "失败",
            Self::Cancelled => "已取消",
        }
    }

    pub fn is_final(self) -> bool {
        matches!(
            self,
            Self::Completed | Self::NoData | Self::Partial | Self::Failed
        )
    }

    pub fn from_legacy(value: &str) -> Result<Self, DomainError> {
        match value {
            "等待" => Ok(Self::Waiting),
            "下载中" => Ok(Self::Downloading),
            "完成" => Ok(Self::Completed),
            "无数据" => Ok(Self::NoData),
            "部分成功" => Ok(Self::Partial),
            "失败" => Ok(Self::Failed),
            "已取消" => Ok(Self::Cancelled),
            _ => Err(DomainError::InvalidLegacyValue(format!("status {value}"))),
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
pub enum ResultVerificationStatus {
    #[serde(rename = "核对通过")]
    Matched,
    #[serde(rename = "内容不匹配")]
    Mismatch,
    #[default]
    #[serde(rename = "无法核对")]
    Unverifiable,
}

impl ResultVerificationStatus {
    fn from_legacy(value: &str) -> Result<Self, DomainError> {
        match value {
            "核对通过" => Ok(Self::Matched),
            "内容不匹配" => Ok(Self::Mismatch),
            "无法核对" => Ok(Self::Unverifiable),
            _ => Err(DomainError::InvalidLegacyValue(format!(
                "verification status {value}"
            ))),
        }
    }

    fn as_legacy_str(self) -> &'static str {
        match self {
            Self::Matched => "核对通过",
            Self::Mismatch => "内容不匹配",
            Self::Unverifiable => "无法核对",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct AccessionResult {
    pub accession: String,
    pub status: AccessionStatus,
    #[serde(default)]
    pub file_count: u64,
    #[serde(default)]
    pub duration_seconds: f64,
    #[serde(default)]
    pub message: String,
    #[serde(default)]
    pub output_directory: String,
    #[serde(default)]
    pub received_bytes: u64,
    #[serde(default)]
    pub speed_bytes_per_second: f64,
    #[serde(default)]
    pub archived_files: Vec<String>,
    #[serde(default)]
    pub new_file_count: u64,
    #[serde(default)]
    pub existing_skipped_count: u64,
    #[serde(default)]
    pub conflict_preserved_count: u64,
    #[serde(default)]
    pub verification_status: ResultVerificationStatus,
    #[serde(default)]
    pub verification_message: String,
    #[serde(default)]
    pub actual_accessions: Vec<String>,
    #[serde(default)]
    pub study_instance_uids: Vec<String>,
    #[serde(default)]
    pub series_instance_count: u64,
    #[serde(default)]
    pub sop_instance_count: u64,
    #[serde(default)]
    pub local_verified_files: u64,
    #[serde(default)]
    pub pacs_completed_suboperations: Option<i64>,
    #[serde(default)]
    pub pacs_expected_suboperations: Option<i64>,
    #[serde(default)]
    pub move_return_code: Option<i64>,
    #[serde(default)]
    pub move_dimse_status: Option<i64>,
    #[serde(default = "one")]
    pub attempt_count: u32,
    #[serde(default)]
    pub transient_failure: bool,
    #[serde(default)]
    pub safety_pause_reason: String,
}

impl AccessionResult {
    pub fn blank(accession: impl Into<String>, status: AccessionStatus) -> Self {
        Self {
            accession: accession.into(),
            status,
            file_count: 0,
            duration_seconds: 0.0,
            message: String::new(),
            output_directory: String::new(),
            received_bytes: 0,
            speed_bytes_per_second: 0.0,
            archived_files: Vec::new(),
            new_file_count: 0,
            existing_skipped_count: 0,
            conflict_preserved_count: 0,
            verification_status: ResultVerificationStatus::Unverifiable,
            verification_message: String::new(),
            actual_accessions: Vec::new(),
            study_instance_uids: Vec::new(),
            series_instance_count: 0,
            sop_instance_count: 0,
            local_verified_files: 0,
            pacs_completed_suboperations: None,
            pacs_expected_suboperations: None,
            move_return_code: None,
            move_dimse_status: None,
            attempt_count: 1,
            transient_failure: false,
            safety_pause_reason: String::new(),
        }
    }

    pub fn from_legacy_json(value: &str) -> Result<Self, DomainError> {
        let value: Value = serde_json::from_str(value)?;
        let mut raw = value
            .as_object()
            .cloned()
            .ok_or(DomainError::ExpectedJsonObject)?;
        let archive_stats_known = [
            "new_file_count",
            "existing_skipped_count",
            "conflict_preserved_count",
        ]
        .iter()
        .any(|key| raw.contains_key(*key));
        let accession = take_string(&mut raw, "accession", None)?;
        let status = AccessionStatus::from_legacy(&take_string(&mut raw, "status", None)?)?;
        let archived_files = unique_strings(raw.remove("archived_files"))?;
        let stored_file_count = take_u64(&mut raw, "file_count", 0);
        let conflict_preserved_count = take_u64(&mut raw, "conflict_preserved_count", 0);
        let file_count = if raw.contains_key("unique_file_count") {
            take_u64(&mut raw, "unique_file_count", 0)
                .max(archived_files.len() as u64 + conflict_preserved_count)
        } else if raw.get("file_count_semantics").and_then(Value::as_str) == Some("unique_sop_v1") {
            stored_file_count.max(archived_files.len() as u64 + conflict_preserved_count)
        } else if archived_files.is_empty() {
            stored_file_count
        } else {
            archived_files.len() as u64 + conflict_preserved_count
        };
        let verification_status = raw
            .remove("verification_status")
            .and_then(|value| value.as_str().map(str::to_owned))
            .unwrap_or_else(|| "无法核对".into());
        Ok(Self {
            accession,
            status,
            file_count,
            duration_seconds: take_f64(&mut raw, "duration_seconds", 0.0),
            message: take_string(&mut raw, "message", Some(""))?,
            output_directory: take_string(&mut raw, "output_directory", Some(""))?,
            received_bytes: take_u64(&mut raw, "received_bytes", 0),
            speed_bytes_per_second: take_f64(&mut raw, "speed_bytes_per_second", 0.0),
            archived_files,
            new_file_count: if archive_stats_known {
                take_u64(&mut raw, "new_file_count", 0)
            } else {
                stored_file_count
            },
            existing_skipped_count: take_u64(&mut raw, "existing_skipped_count", 0),
            conflict_preserved_count,
            verification_status: ResultVerificationStatus::from_legacy(&verification_status)?,
            verification_message: take_string(&mut raw, "verification_message", Some(""))?,
            actual_accessions: unique_strings(raw.remove("actual_accessions"))?,
            study_instance_uids: unique_strings(raw.remove("study_instance_uids"))?,
            series_instance_count: take_u64(&mut raw, "series_instance_count", 0),
            sop_instance_count: take_u64(&mut raw, "sop_instance_count", 0),
            local_verified_files: take_u64(&mut raw, "local_verified_files", 0),
            pacs_completed_suboperations: take_optional_i64(
                &mut raw,
                "pacs_completed_suboperations",
            )?,
            pacs_expected_suboperations: take_optional_i64(
                &mut raw,
                "pacs_expected_suboperations",
            )?,
            move_return_code: take_optional_i64(&mut raw, "move_return_code")?,
            move_dimse_status: take_optional_i64(&mut raw, "move_dimse_status")?,
            attempt_count: u32::try_from(take_u64(&mut raw, "attempt_count", 1).max(1))
                .unwrap_or(u32::MAX),
            transient_failure: take_bool(&mut raw, "transient_failure", false),
            safety_pause_reason: take_string(&mut raw, "safety_pause_reason", Some(""))?,
        })
    }

    pub fn to_legacy_json(&self) -> Result<String, DomainError> {
        let delivery_count =
            self.new_file_count + self.existing_skipped_count + self.conflict_preserved_count;
        let processed = if delivery_count == 0 {
            self.file_count
        } else {
            delivery_count
        };
        Ok(serde_json::to_string(&json!({
            "accession": self.accession,
            "archived_files": self.archived_files,
            "duration_seconds": self.duration_seconds,
            "file_count": processed,
            "file_count_semantics": "processed_store_v1",
            "unique_file_count": self.file_count,
            "new_file_count": self.new_file_count,
            "existing_skipped_count": self.existing_skipped_count,
            "conflict_preserved_count": self.conflict_preserved_count,
            "verification_status": self.verification_status.as_legacy_str(),
            "verification_message": self.verification_message,
            "actual_accessions": self.actual_accessions,
            "study_instance_uids": self.study_instance_uids,
            "series_instance_count": self.series_instance_count,
            "sop_instance_count": self.sop_instance_count,
            "local_verified_files": self.local_verified_files,
            "pacs_completed_suboperations": self.pacs_completed_suboperations,
            "pacs_expected_suboperations": self.pacs_expected_suboperations,
            "move_return_code": self.move_return_code,
            "move_dimse_status": self.move_dimse_status,
            "attempt_count": self.attempt_count.max(1),
            "transient_failure": self.transient_failure,
            "safety_pause_reason": self.safety_pause_reason,
            "message": self.message,
            "output_directory": self.output_directory,
            "received_bytes": self.received_bytes,
            "speed_bytes_per_second": self.speed_bytes_per_second,
            "status": self.status.as_legacy_str(),
        }))?)
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Task {
    pub id: TaskId,
    pub profile_id: ProfileId,
    pub name: String,
    pub phase: TaskPhase,
    pub config: AppConfig,
    pub accessions: Vec<String>,
    #[serde(default)]
    pub results: Vec<AccessionResult>,
    #[serde(default)]
    pub partial_results: Vec<AccessionResult>,
    pub trial_required: bool,
    pub trial_consumed: bool,
    #[serde(default)]
    pub pdi_attempt_id: String,
    #[serde(default)]
    pub current_accession: String,
    #[serde(default)]
    pub speed_bytes_per_second: f64,
    #[serde(default)]
    pub error_message: String,
    pub created_at: String,
    pub updated_at: String,
}

impl Task {
    pub fn validate_new(&self) -> Result<(), DomainError> {
        if self.accessions.is_empty() || self.accessions.iter().any(|value| value.trim().is_empty())
        {
            return Err(DomainError::InvalidTask(
                "at least one accession is required".into(),
            ));
        }
        if self.accessions.iter().collect::<BTreeSet<_>>().len() != self.accessions.len() {
            return Err(DomainError::InvalidTask("accessions must be unique".into()));
        }
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct TaskSummary {
    pub task_id: TaskId,
    pub profile_id: ProfileId,
    pub name: String,
    pub phase: TaskPhase,
    pub total_count: u64,
    pub processed_count: u64,
    pub pending_count: u64,
    pub completed_count: u64,
    pub failed_count: u64,
    pub file_count: u64,
    pub received_bytes: u64,
    pub speed_bytes_per_second: f64,
    pub current_accession: String,
    pub error_message: String,
    pub created_at: String,
    pub updated_at: String,
    #[serde(default)]
    pub no_data_count: u64,
    #[serde(default)]
    pub partial_count: u64,
    #[serde(default)]
    pub cancelled_count: u64,
}

fn one() -> u32 {
    1
}

fn take_string(
    raw: &mut Map<String, Value>,
    key: &str,
    default: Option<&str>,
) -> Result<String, DomainError> {
    match raw.remove(key) {
        Some(Value::String(value)) => Ok(value),
        Some(value) if default.is_some() => Ok(value.to_string().trim_matches('"').to_owned()),
        None if default.is_some() => Ok(default.unwrap_or_default().to_owned()),
        _ => Err(DomainError::InvalidLegacyValue(key.into())),
    }
}

fn take_u64(raw: &mut Map<String, Value>, key: &str, default: u64) -> u64 {
    match raw.remove(key) {
        Some(Value::Number(value)) => value.as_u64().unwrap_or(default),
        Some(Value::String(value)) => value.parse().unwrap_or(default),
        _ => default,
    }
}

fn take_f64(raw: &mut Map<String, Value>, key: &str, default: f64) -> f64 {
    match raw.remove(key) {
        Some(Value::Number(value)) => value.as_f64().unwrap_or(default),
        Some(Value::String(value)) => value.parse().unwrap_or(default),
        _ => default,
    }
}

fn take_bool(raw: &mut Map<String, Value>, key: &str, default: bool) -> bool {
    match raw.remove(key) {
        Some(Value::Bool(value)) => value,
        Some(Value::Number(value)) => value.as_i64().map_or(default, |v| v != 0),
        Some(Value::String(value)) => matches!(
            value.to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        ),
        _ => default,
    }
}

fn take_optional_i64(raw: &mut Map<String, Value>, key: &str) -> Result<Option<i64>, DomainError> {
    match raw.remove(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Number(value)) => value
            .as_i64()
            .map(Some)
            .ok_or_else(|| DomainError::InvalidLegacyValue(key.into())),
        Some(Value::String(value)) => value
            .parse()
            .map(Some)
            .map_err(|_| DomainError::InvalidLegacyValue(key.into())),
        _ => Err(DomainError::InvalidLegacyValue(key.into())),
    }
}

fn unique_strings(value: Option<Value>) -> Result<Vec<String>, DomainError> {
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    let values = value
        .as_array()
        .ok_or_else(|| DomainError::InvalidLegacyValue("string list".into()))?;
    let mut seen = BTreeSet::new();
    Ok(values
        .iter()
        .map(|value| value.as_str().unwrap_or_default().to_owned())
        .filter(|value| seen.insert(value.clone()))
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifiers_accept_legacy_and_readable_values() {
        let generated = TaskId::generate();
        assert_eq!(generated.as_str().len(), 32);
        assert_eq!(TaskId::new(generated.to_string()).unwrap(), generated);
        assert_eq!(TaskId::new("task-1").unwrap().as_str(), "task-1");
        assert!(TaskId::new("bad/task").is_err());
    }

    #[test]
    fn legacy_result_uses_archived_paths_for_unique_count() {
        let result = AccessionResult::from_legacy_json(
            &json!({
                "accession": "A001", "status": "完成", "file_count": 4,
                "archived_files": ["a.dcm", "b.dcm"]
            })
            .to_string(),
        )
        .unwrap();
        assert_eq!(result.file_count, 2);
        assert_eq!(result.new_file_count, 4);
        let round_trip =
            AccessionResult::from_legacy_json(&result.to_legacy_json().unwrap()).unwrap();
        assert_eq!(round_trip.file_count, 2);
    }
}
