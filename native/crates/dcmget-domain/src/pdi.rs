use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub enum PdiStatus {
    #[serde(rename = "等待")]
    Waiting,
    #[serde(rename = "生成中")]
    Generating,
    #[serde(rename = "完成")]
    Completed,
    #[serde(rename = "部分成功")]
    Partial,
    #[serde(rename = "失败")]
    Failed,
    #[serde(rename = "已取消")]
    Cancelled,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct PdiResult {
    pub status: PdiStatus,
    #[serde(default)]
    pub output_directory: String,
    #[serde(default)]
    pub message: String,
    #[serde(default)]
    pub warnings: Vec<String>,
    #[serde(default)]
    pub source_count: u64,
    #[serde(default)]
    pub exported_count: u64,
    #[serde(default)]
    pub duplicate_count: u64,
    #[serde(default)]
    pub indexed_count: u64,
    #[serde(default)]
    pub strict_profile: Option<bool>,
    #[serde(default)]
    pub core_tool_failure: bool,
}
