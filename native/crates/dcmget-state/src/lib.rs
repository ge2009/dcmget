//! Persistent state and non-destructive legacy migration for `DcmGet` 4.

mod backup;
mod migration;
mod repository;

pub use backup::{BackupEntry, BackupService, BackupSet};
pub use dcmget_domain::AppConfig as LegacyConfig;
pub use migration::{
    LegacyLayout, LegacyProfileSource, MigrationReport, MigrationService, MigrationWarning,
};
pub use repository::{StateError, StateRepository};
