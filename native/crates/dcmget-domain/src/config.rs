use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::{Map, Value, json};

use crate::DomainError;

pub const CURRENT_CONFIG_VERSION: u32 = 8;
pub const DEFAULT_DIRECTORY_TEMPLATE: &str = "{PatientID}/{AccessionNumber}/{StudyInstanceUID}";
pub const ANONYMIZATION_PROFILES: &[&str] = &["basic", "research", "strict"];
const DEFAULT_MINIMUM_FREE_SPACE_BYTES: u64 = 2 * 1024 * 1024 * 1024;

/// Version 8 configuration as written by the Python product.
///
/// `dcmtk_bin_dir` and `web_*` remain serialized solely for downgrade and
/// old-profile compatibility. The native runtime intentionally ignores them.
#[derive(Clone, Debug, PartialEq)]
// These flags mirror the deployed JSON contract; replacing them with enums would
// break round-trip compatibility with existing profiles.
#[allow(clippy::struct_excessive_bools)]
pub struct AppConfig {
    pub config_version: u32,
    pub dcmtk_bin_dir: String,
    pub access_numbers_file_path: String,
    pub dicom_destination_folder: String,
    pub pacs_server_ip: String,
    pub pacs_server_port: u16,
    pub calling_ae_title: String,
    pub pacs_ae_title: String,
    pub storage_ae_title: String,
    pub storage_port: u16,
    pub max_concurrent_moves: u16,
    pub web_bind_address: String,
    pub web_port: u16,
    pub web_open_browser: bool,
    pub web_session_timeout_minutes: u32,
    pub directory_template: String,
    pub anonymization_enabled: bool,
    pub anonymization_profile: String,
    pub pdi_export_enabled: bool,
    pub pdi_institution_name: String,
    pub pdi_output_folder: String,
    pub pdi_include_ohif_viewer: bool,
    pub pdi_volume_size_bytes: u64,
    pub minimum_free_space_bytes: u64,
    pub auto_retry_attempts: u16,
    pub auto_retry_backoff_seconds: u32,
    pub circuit_breaker_failures: u16,
    pub max_log_file_size_bytes: u64,
    /// Unknown future fields are retained across a read/write cycle.
    pub extra: BTreeMap<String, Value>,
}

impl Default for AppConfig {
    fn default() -> Self {
        Self {
            config_version: CURRENT_CONFIG_VERSION,
            dcmtk_bin_dir: String::new(),
            access_numbers_file_path: "access.txt".into(),
            dicom_destination_folder: "Dicom".into(),
            pacs_server_ip: "127.0.0.1".into(),
            pacs_server_port: 8104,
            calling_ae_title: "DCMGET".into(),
            pacs_ae_title: "ANY-SCP".into(),
            storage_ae_title: "DCMGET".into(),
            storage_port: 6666,
            max_concurrent_moves: 2,
            web_bind_address: "0.0.0.0".into(),
            web_port: 8787,
            web_open_browser: true,
            web_session_timeout_minutes: 480,
            directory_template: DEFAULT_DIRECTORY_TEMPLATE.into(),
            anonymization_enabled: false,
            anonymization_profile: "research".into(),
            pdi_export_enabled: false,
            pdi_institution_name: String::new(),
            pdi_output_folder: String::new(),
            pdi_include_ohif_viewer: true,
            pdi_volume_size_bytes: 0,
            minimum_free_space_bytes: DEFAULT_MINIMUM_FREE_SPACE_BYTES,
            auto_retry_attempts: 2,
            auto_retry_backoff_seconds: 3,
            circuit_breaker_failures: 5,
            max_log_file_size_bytes: 104_857_600,
            extra: BTreeMap::new(),
        }
    }
}

impl AppConfig {
    pub fn from_json(source: &str) -> Result<Self, DomainError> {
        Self::from_json_slice(source.as_bytes())
    }

    pub fn from_json_slice(bytes: &[u8]) -> Result<Self, DomainError> {
        let bytes = bytes.strip_prefix(&[0xEF, 0xBB, 0xBF]).unwrap_or(bytes);
        let value = serde_json::from_slice(bytes)?;
        Self::from_legacy_value(&value)
    }

    pub fn from_legacy_value(value: &Value) -> Result<Self, DomainError> {
        let mut raw = value
            .as_object()
            .cloned()
            .ok_or(DomainError::ExpectedJsonObject)?;
        let source_version = value_u32(raw.get("config_version"), 1);
        let mut config = Self::default();

        if source_version < 2 {
            config.dcmtk_bin_dir =
                legacy_dcmtk_dir(value_string(raw.get("movescu_executable_path"), ""));
            config.access_numbers_file_path =
                value_string(raw.get("access_numbers_file_path"), "access.txt");
            config.dicom_destination_folder =
                value_string(raw.get("dicom_destination_folder"), "Dicom");
            config.pacs_server_ip = value_string(raw.get("pacs_server_ip"), "127.0.0.1");
            config.pacs_server_port = bounded_port(raw.get("pacs_server_port"), 8104);
            config.calling_ae_title = normalized_ae(raw.get("application_entity_title"), "DCMGET");
            config.pacs_ae_title = normalized_ae(raw.get("called_ae_title"), "ANY-SCP");
            config.storage_ae_title = normalized_ae(raw.get("calling_ae_title"), "DCMGET");
            config.storage_port = bounded_port(raw.get("network_port"), 6666);
            config.max_log_file_size_bytes =
                value_u64(raw.get("max_log_file_size_bytes"), 104_857_600);
        } else {
            config.dcmtk_bin_dir = value_string(raw.get("dcmtk_bin_dir"), "");
            config.access_numbers_file_path =
                value_string(raw.get("access_numbers_file_path"), "access.txt");
            config.dicom_destination_folder =
                value_string(raw.get("dicom_destination_folder"), "Dicom");
            config.pacs_server_ip = value_string(raw.get("pacs_server_ip"), "127.0.0.1");
            config.pacs_server_port = bounded_port(raw.get("pacs_server_port"), 8104);
            config.calling_ae_title = normalized_ae(raw.get("calling_ae_title"), "DCMGET");
            config.pacs_ae_title = normalized_ae(raw.get("pacs_ae_title"), "ANY-SCP");
            config.storage_ae_title = normalized_ae(raw.get("storage_ae_title"), "DCMGET");
            config.storage_port = bounded_port(raw.get("storage_port"), 6666);
            config.web_bind_address = value_string(raw.get("web_bind_address"), "0.0.0.0")
                .trim()
                .into();
            config.web_port = bounded_port(raw.get("web_port"), 8787);
            config.web_open_browser = value_bool(raw.get("web_open_browser"), true);
            config.web_session_timeout_minutes =
                value_u32(raw.get("web_session_timeout_minutes"), 480);
            config.directory_template =
                value_string(raw.get("directory_template"), DEFAULT_DIRECTORY_TEMPLATE);
            config.anonymization_enabled = value_bool(raw.get("anonymization_enabled"), false);
            config.anonymization_profile =
                value_string(raw.get("anonymization_profile"), "research")
                    .trim()
                    .to_ascii_lowercase();
            config.pdi_export_enabled = value_bool(raw.get("pdi_export_enabled"), false);
            config.pdi_institution_name = value_string(raw.get("pdi_institution_name"), "");
            config.pdi_output_folder = value_string(raw.get("pdi_output_folder"), "");
            config.pdi_include_ohif_viewer = if raw.contains_key("pdi_include_ohif_viewer") {
                value_bool(raw.get("pdi_include_ohif_viewer"), true)
            } else if source_version <= 4 {
                value_bool(raw.get("pdi_include_html_preview"), true)
                    || value_bool(raw.get("pdi_include_weasis_windows"), true)
            } else {
                true
            };
            config.pdi_volume_size_bytes = value_u64(raw.get("pdi_volume_size_bytes"), 0);
            config.minimum_free_space_bytes = value_u64(
                raw.get("minimum_free_space_bytes"),
                DEFAULT_MINIMUM_FREE_SPACE_BYTES,
            );
            config.auto_retry_attempts = value_u16(raw.get("auto_retry_attempts"), 2);
            config.auto_retry_backoff_seconds = value_u32(raw.get("auto_retry_backoff_seconds"), 3);
            config.circuit_breaker_failures = value_u16(raw.get("circuit_breaker_failures"), 5);
            config.max_log_file_size_bytes =
                value_u64(raw.get("max_log_file_size_bytes"), 104_857_600);
        }

        config.config_version = CURRENT_CONFIG_VERSION;
        // Retained only for rollback. Native scheduling is one C-MOVE/profile.
        config.max_concurrent_moves = 2;
        for key in KNOWN_KEYS {
            raw.remove(*key);
        }
        for key in OBSOLETE_PDI_KEYS {
            raw.remove(*key);
        }
        config.extra = raw.into_iter().collect();
        Ok(config)
    }

    pub fn to_legacy_value(&self) -> Value {
        let mut map: Map<String, Value> = self.extra.clone().into_iter().collect();
        let fields = [
            ("config_version", json!(CURRENT_CONFIG_VERSION)),
            ("dcmtk_bin_dir", json!(self.dcmtk_bin_dir)),
            (
                "access_numbers_file_path",
                json!(self.access_numbers_file_path),
            ),
            (
                "dicom_destination_folder",
                json!(self.dicom_destination_folder),
            ),
            ("pacs_server_ip", json!(self.pacs_server_ip)),
            ("pacs_server_port", json!(self.pacs_server_port)),
            ("calling_ae_title", json!(self.calling_ae_title)),
            ("pacs_ae_title", json!(self.pacs_ae_title)),
            ("storage_ae_title", json!(self.storage_ae_title)),
            ("storage_port", json!(self.storage_port)),
            ("max_concurrent_moves", json!(2)),
            ("web_bind_address", json!(self.web_bind_address)),
            ("web_port", json!(self.web_port)),
            ("web_open_browser", json!(self.web_open_browser)),
            (
                "web_session_timeout_minutes",
                json!(self.web_session_timeout_minutes),
            ),
            ("directory_template", json!(self.directory_template)),
            ("anonymization_enabled", json!(self.anonymization_enabled)),
            ("anonymization_profile", json!(self.anonymization_profile)),
            ("pdi_export_enabled", json!(self.pdi_export_enabled)),
            ("pdi_institution_name", json!(self.pdi_institution_name)),
            ("pdi_output_folder", json!(self.pdi_output_folder)),
            (
                "pdi_include_ohif_viewer",
                json!(self.pdi_include_ohif_viewer),
            ),
            ("pdi_volume_size_bytes", json!(self.pdi_volume_size_bytes)),
            (
                "minimum_free_space_bytes",
                json!(self.minimum_free_space_bytes),
            ),
            ("auto_retry_attempts", json!(self.auto_retry_attempts)),
            (
                "auto_retry_backoff_seconds",
                json!(self.auto_retry_backoff_seconds),
            ),
            (
                "circuit_breaker_failures",
                json!(self.circuit_breaker_failures),
            ),
            (
                "max_log_file_size_bytes",
                json!(self.max_log_file_size_bytes),
            ),
        ];
        for (key, value) in fields {
            map.insert(key.into(), value);
        }
        Value::Object(map)
    }

    pub fn validate(&self) -> Vec<ValidationIssue> {
        let mut issues = Vec::new();
        required(
            &mut issues,
            "dicom_destination_folder",
            &self.dicom_destination_folder,
        );
        required(&mut issues, "pacs_server_ip", &self.pacs_server_ip);
        for (field, label, value) in [
            (
                "calling_ae_title",
                "本机调用 AE Title",
                &self.calling_ae_title,
            ),
            ("pacs_ae_title", "PACS AE Title", &self.pacs_ae_title),
            ("storage_ae_title", "接收 AE Title", &self.storage_ae_title),
        ] {
            if let Err(message) = validate_ae_title(value, label) {
                issues.push(ValidationIssue::new(field, message));
            }
        }
        if self.max_log_file_size_bytes < 1024 {
            issues.push(ValidationIssue::new(
                "max_log_file_size_bytes",
                "日志大小至少为 1024 字节",
            ));
        }
        if self.auto_retry_attempts > 10 {
            issues.push(ValidationIssue::new(
                "auto_retry_attempts",
                "自动重试次数必须在 0 到 10 之间",
            ));
        }
        if self.auto_retry_backoff_seconds > 300 {
            issues.push(ValidationIssue::new(
                "auto_retry_backoff_seconds",
                "重试等待时间必须在 0 到 300 秒之间",
            ));
        }
        if !(2..=100).contains(&self.circuit_breaker_failures) {
            issues.push(ValidationIssue::new(
                "circuit_breaker_failures",
                "连续失败暂停阈值必须在 2 到 100 之间",
            ));
        }
        if self.anonymization_enabled
            && !ANONYMIZATION_PROFILES.contains(&self.anonymization_profile.as_str())
        {
            issues.push(ValidationIssue::new(
                "anonymization_profile",
                "请选择有效的匿名方案",
            ));
        }
        if self.pdi_export_enabled && self.pdi_institution_name.trim().is_empty() {
            issues.push(ValidationIssue::new(
                "pdi_institution_name",
                "启用 PDI 时请输入机构名称",
            ));
        }
        validate_directory_template(&self.directory_template, &mut issues);
        issues
    }
}

impl Serialize for AppConfig {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        self.to_legacy_value().serialize(serializer)
    }
}

impl<'de> Deserialize<'de> for AppConfig {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = Value::deserialize(deserializer)?;
        Self::from_legacy_value(&value).map_err(serde::de::Error::custom)
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ValidationIssue {
    pub field: String,
    pub message: String,
}

impl ValidationIssue {
    pub fn new(field: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            field: field.into(),
            message: message.into(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AccessionParseResult {
    pub values: Vec<String>,
    pub blank_count: usize,
    pub duplicate_count: usize,
    pub invalid_values: Vec<String>,
}

pub fn parse_accessions(input: &str) -> AccessionParseResult {
    let mut values = Vec::new();
    let mut seen = BTreeSet::new();
    let mut blank_count = 0;
    let mut duplicate_count = 0;
    let mut invalid_values = Vec::new();
    for line in input.lines() {
        let value = line.trim().to_owned();
        if value.is_empty() {
            blank_count += 1;
        } else if value
            .chars()
            .any(|ch| ch == '\\' || ch == '*' || ch == '?' || ch.is_control() || ch == '\u{7f}')
        {
            invalid_values.push(value);
        } else if !seen.insert(value.clone()) {
            duplicate_count += 1;
        } else {
            values.push(value);
        }
    }
    AccessionParseResult {
        values,
        blank_count,
        duplicate_count,
        invalid_values,
    }
}

pub fn validate_ae_title(value: &str, label: &str) -> Result<(), String> {
    let title = value.trim_matches(' ');
    if title.is_empty() {
        return Err(format!("请输入{label}"));
    }
    if title.len() > 16 {
        return Err(format!("{label}最多 16 个字符"));
    }
    if title.bytes().any(|byte| !(0x20..=0x7e).contains(&byte)) || title.contains('\\') {
        return Err(format!(
            "{label}只能使用可打印 ASCII 字符，且不能包含反斜杠（\\）"
        ));
    }
    Ok(())
}

fn validate_directory_template(template: &str, issues: &mut Vec<ValidationIssue>) {
    let normalized = template.trim().replace('\\', "/");
    let fields = ["{PatientID}", "{AccessionNumber}", "{StudyInstanceUID}"];
    if normalized.is_empty() || !fields.iter().any(|field| normalized.contains(field)) {
        issues.push(ValidationIssue::new(
            "directory_template",
            "目录模板至少包含一个 DICOM 字段",
        ));
        return;
    }
    let remainder = fields
        .iter()
        .fold(normalized.clone(), |value, field| value.replace(field, ""));
    if remainder.contains('{') || remainder.contains('}') {
        issues.push(ValidationIssue::new(
            "directory_template",
            "目录模板包含不支持的字段或不完整的花括号",
        ));
    } else if normalized.starts_with('/')
        || normalized.as_bytes().get(1) == Some(&b':')
        || normalized
            .split('/')
            .any(|segment| matches!(segment, "." | ".."))
    {
        issues.push(ValidationIssue::new(
            "directory_template",
            "目录模板不能使用绝对路径或上级目录",
        ));
    }
}

fn required(issues: &mut Vec<ValidationIssue>, field: &str, value: &str) {
    if value.trim().is_empty() {
        issues.push(ValidationIssue::new(field, "此项不能为空"));
    }
}

fn normalized_ae(value: Option<&Value>, default: &str) -> String {
    value_string(value, default).trim_matches(' ').to_owned()
}

fn value_string(value: Option<&Value>, default: &str) -> String {
    match value {
        Some(Value::String(value)) => value.clone(),
        Some(Value::Number(value)) => value.to_string(),
        Some(Value::Bool(value)) => value.to_string(),
        _ => default.to_owned(),
    }
}

fn value_u64(value: Option<&Value>, default: u64) -> u64 {
    match value {
        Some(Value::Number(value)) => value.as_u64().unwrap_or(default),
        Some(Value::String(value)) => value.trim().parse().unwrap_or(default),
        _ => default,
    }
}

fn value_u32(value: Option<&Value>, default: u32) -> u32 {
    u32::try_from(value_u64(value, u64::from(default))).unwrap_or(default)
}

fn value_u16(value: Option<&Value>, default: u16) -> u16 {
    u16::try_from(value_u64(value, u64::from(default))).unwrap_or(default)
}

fn bounded_port(value: Option<&Value>, default: u16) -> u16 {
    u16::try_from(value_u64(value, u64::from(default)))
        .ok()
        .filter(|port| *port > 0)
        .unwrap_or(default)
}

fn value_bool(value: Option<&Value>, default: bool) -> bool {
    match value {
        Some(Value::Bool(value)) => *value,
        Some(Value::String(value)) => match value.trim().to_ascii_lowercase().as_str() {
            "1" | "true" | "yes" | "on" => true,
            "0" | "false" | "no" | "off" => false,
            _ => default,
        },
        Some(Value::Number(value)) => value.as_f64().map_or(default, |v| v != 0.0),
        _ => default,
    }
}

fn legacy_dcmtk_dir(value: String) -> String {
    if value.is_empty() {
        return value;
    }
    let path = Path::new(&value);
    let executable = path
        .extension()
        .is_some_and(|extension| extension.eq_ignore_ascii_case("exe"))
        || path.file_name().is_some_and(|name| name == "movescu");
    if executable {
        path.parent()
            .map(|parent| parent.to_string_lossy().into_owned())
            .unwrap_or_default()
    } else {
        value
    }
}

const OBSOLETE_PDI_KEYS: &[&str] = &[
    "pdi_include_html_preview",
    "pdi_preview_mode",
    "pdi_include_weasis_windows",
];

const KNOWN_KEYS: &[&str] = &[
    "config_version",
    "dcmtk_bin_dir",
    "access_numbers_file_path",
    "dicom_destination_folder",
    "pacs_server_ip",
    "pacs_server_port",
    "calling_ae_title",
    "pacs_ae_title",
    "storage_ae_title",
    "storage_port",
    "max_concurrent_moves",
    "web_bind_address",
    "web_port",
    "web_open_browser",
    "web_session_timeout_minutes",
    "directory_template",
    "anonymization_enabled",
    "anonymization_profile",
    "pdi_export_enabled",
    "pdi_institution_name",
    "pdi_output_folder",
    "pdi_include_ohif_viewer",
    "pdi_volume_size_bytes",
    "minimum_free_space_bytes",
    "auto_retry_attempts",
    "auto_retry_backoff_seconds",
    "circuit_breaker_failures",
    "max_log_file_size_bytes",
    "movescu_executable_path",
    "application_entity_title",
    "called_ae_title",
    "network_port",
];

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn migrates_v1_and_preserves_unknown_future_fields() {
        let config = AppConfig::from_legacy_value(&json!({
            "movescu_executable_path": "C:/dcmtk/bin/movescu.exe",
            "application_entity_title": "CALLING",
            "called_ae_title": "PACS",
            "calling_ae_title": "STORAGE",
            "network_port": 11112,
            "max_log_file_size_bytes": "2097152",
            "future_setting": {"enabled": true}
        }))
        .unwrap();
        assert_eq!(config.dcmtk_bin_dir, "C:/dcmtk/bin");
        assert_eq!(config.calling_ae_title, "CALLING");
        assert_eq!(config.storage_ae_title, "STORAGE");
        assert_eq!(config.storage_port, 11112);
        assert_eq!(config.max_log_file_size_bytes, 2_097_152);
        assert_eq!(
            config.to_legacy_value()["future_setting"],
            json!({"enabled": true})
        );
    }

    #[test]
    fn migrates_old_pdi_flags_and_string_values() {
        let config = AppConfig::from_legacy_value(&json!({
            "config_version": 4,
            "storage_port": "16666",
            "pdi_include_html_preview": false,
            "pdi_include_weasis_windows": false,
            "max_concurrent_moves": 99
        }))
        .unwrap();
        assert_eq!(config.storage_port, 16666);
        assert_eq!(config.max_concurrent_moves, 2);
        assert!(!config.pdi_include_ohif_viewer);
        assert!(
            config
                .to_legacy_value()
                .get("pdi_include_html_preview")
                .is_none()
        );
    }

    #[test]
    fn parse_accessions_matches_legacy_order() {
        let parsed = parse_accessions(" A001\n\nA002\nA001\nA?\n");
        assert_eq!(parsed.values, ["A001", "A002"]);
        assert_eq!(parsed.blank_count, 1);
        assert_eq!(parsed.duplicate_count, 1);
        assert_eq!(parsed.invalid_values, ["A?"]);
    }

    #[test]
    fn deprecated_web_values_do_not_block_native_validation() {
        let config = AppConfig {
            web_port: 6666,
            storage_port: 6666,
            web_bind_address: "not-an-ip".into(),
            ..AppConfig::default()
        };
        assert!(
            config
                .validate()
                .iter()
                .all(|issue| !issue.field.starts_with("web_"))
        );
    }
}
