use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::time::Duration;

use chrono::{SecondsFormat, Utc};
use rusqlite::Connection;
use rusqlite::backup::Backup;
use sha2::{Digest, Sha256};

use crate::StateError;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BackupEntry {
    pub source: PathBuf,
    pub backup: PathBuf,
    pub sha256: String,
    pub sqlite: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BackupSet {
    pub directory: PathBuf,
    pub entries: Vec<BackupEntry>,
}

impl BackupSet {
    pub fn entry_for(&self, source: &Path) -> Option<&BackupEntry> {
        self.entries.iter().find(|entry| entry.source == source)
    }
}

#[derive(Clone, Debug)]
pub struct BackupService {
    root: PathBuf,
}

impl BackupService {
    pub fn new(root: impl AsRef<Path>) -> Self {
        Self {
            root: root.as_ref().to_path_buf(),
        }
    }

    /// Publish a complete immutable backup directory without modifying any
    /// source. `SQLite` inputs use the online backup API so WAL content is
    /// captured consistently.
    pub fn create(&self, sources: &[(PathBuf, bool)]) -> Result<BackupSet, StateError> {
        fs::create_dir_all(&self.root)?;
        let timestamp = Utc::now()
            .to_rfc3339_opts(SecondsFormat::Secs, true)
            .replace([':', '-'], "");
        let final_directory = unique_directory(&self.root, &format!("legacy-{timestamp}"));
        let staging = final_directory.with_extension("partial");
        fs::create_dir(&staging)?;

        let result = (|| {
            let mut entries = Vec::new();
            for (index, (source, sqlite)) in sources.iter().enumerate() {
                reject_unsafe_source(source)?;
                let filename = source
                    .file_name()
                    .and_then(|name| name.to_str())
                    .unwrap_or("legacy-data");
                let destination = staging.join(format!("{index:04}-{filename}"));
                if *sqlite {
                    backup_sqlite(source, &destination)?;
                } else {
                    copy_regular_file(source, &destination)?;
                }
                entries.push(BackupEntry {
                    source: source.clone(),
                    sha256: sha256_file(&destination)?,
                    backup: final_directory.join(destination.file_name().unwrap_or_default()),
                    sqlite: *sqlite,
                });
            }
            sync_directory(&staging);
            fs::rename(&staging, &final_directory)?;
            sync_directory(&self.root);
            Ok(BackupSet {
                directory: final_directory,
                entries,
            })
        })();
        if result.is_err() {
            let _ = fs::remove_dir_all(&staging);
        }
        result
    }
}

fn reject_unsafe_source(path: &Path) -> Result<(), StateError> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(StateError::InvalidData(format!(
            "legacy backup source is not a regular file: {}",
            path.display()
        )));
    }
    Ok(())
}

fn backup_sqlite(source_path: &Path, destination_path: &Path) -> Result<(), StateError> {
    let source = Connection::open_with_flags(
        source_path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    let temporary = destination_path.with_extension("tmp");
    {
        let mut destination = Connection::open(&temporary)?;
        let backup = Backup::new(&source, &mut destination)?;
        backup.run_to_completion(128, Duration::from_millis(5), None)?;
        drop(backup);
        destination.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);")?;
    }
    // `FlushFileBuffers` requires a writable Windows handle. `File::open`
    // creates a read-only handle there, so calling `sync_all` on it fails with
    // ERROR_ACCESS_DENIED even though the backup itself completed correctly.
    OpenOptions::new()
        .read(true)
        .write(true)
        .open(&temporary)?
        .sync_all()?;
    fs::rename(&temporary, destination_path)?;
    Ok(())
}

fn copy_regular_file(source: &Path, destination: &Path) -> Result<(), StateError> {
    let mut input = File::open(source)?;
    let mut output = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(destination)?;
    let mut buffer = vec![0_u8; 1024 * 1024];
    loop {
        let count = input.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        output.write_all(&buffer[..count])?;
    }
    output.sync_all()?;
    Ok(())
}

fn sha256_file(path: &Path) -> Result<String, StateError> {
    let mut file = File::open(path)?;
    let mut digest = Sha256::new();
    let mut buffer = vec![0_u8; 1024 * 1024];
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

fn unique_directory(root: &Path, stem: &str) -> PathBuf {
    let first = root.join(stem);
    if !first.exists() && !first.with_extension("partial").exists() {
        return first;
    }
    (1_u32..=u32::MAX)
        .map(|suffix| root.join(format!("{stem}-{suffix}")))
        .find(|candidate| !candidate.exists() && !candidate.with_extension("partial").exists())
        .expect("u32 backup suffixes are sufficient")
}

fn sync_directory(path: &Path) {
    if let Ok(directory) = File::open(path) {
        let _ = directory.sync_all();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::params;

    #[test]
    fn backs_up_json_and_wal_sqlite_without_touching_sources() {
        let temp = tempfile::tempdir().unwrap();
        let config = temp.path().join("config.json");
        fs::write(&config, b"{\"config_version\":8}").unwrap();
        let database = temp.path().join("tasks.sqlite3");
        let connection = Connection::open(&database).unwrap();
        connection
            .execute_batch("PRAGMA journal_mode=WAL; CREATE TABLE values_(value TEXT);")
            .unwrap();
        connection
            .execute("INSERT INTO values_(value) VALUES(?1)", params!["kept"])
            .unwrap();

        let backup = BackupService::new(temp.path().join("backups"))
            .create(&[(config.clone(), false), (database.clone(), true)])
            .unwrap();

        assert_eq!(fs::read(&config).unwrap(), b"{\"config_version\":8}");
        assert_eq!(backup.entries.len(), 2);
        let sqlite_backup = &backup.entry_for(&database).unwrap().backup;
        let copied = Connection::open(sqlite_backup).unwrap();
        assert_eq!(
            copied
                .query_row("SELECT value FROM values_", [], |row| row
                    .get::<_, String>(0))
                .unwrap(),
            "kept"
        );
        assert!(backup.entries.iter().all(|entry| entry.sha256.len() == 64));
    }
}
