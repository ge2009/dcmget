use std::fmt;

use serde::{Deserialize, Serialize};

use crate::AppConfig;

use crate::DomainError;

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize, Deserialize)]
#[serde(transparent)]
pub struct ProfileId(String);

impl ProfileId {
    pub fn new(value: impl Into<String>) -> Result<Self, DomainError> {
        let value = value.into();
        validate_identifier(&value, "profile")?;
        Ok(Self(value))
    }

    pub fn legacy(number: u32) -> Result<Self, DomainError> {
        if number == 0 || number > 9_999 {
            return Err(DomainError::InvalidIdentifier(format!("profile i{number}")));
        }
        Self::new(format!("i{number}"))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for ProfileId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

fn validate_identifier(value: &str, label: &str) -> Result<(), DomainError> {
    if value.is_empty()
        || value.len() > 128
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
    {
        Err(DomainError::InvalidIdentifier(format!("{label} {value}")))
    } else {
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProfileRuntimeStatus {
    #[default]
    Stopped,
    Starting,
    Running,
    Stopping,
    Faulted,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Profile {
    pub id: ProfileId,
    pub display_name: String,
    pub config: AppConfig,
    #[serde(default)]
    pub runtime_status: ProfileRuntimeStatus,
    #[serde(default)]
    pub source_config_path: String,
    #[serde(default)]
    pub created_at: String,
    #[serde(default)]
    pub updated_at: String,
}
