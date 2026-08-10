use std::fs::{File, OpenOptions};
use std::io;
use std::path::{Path, PathBuf};

use thiserror::Error;

/// Holds the process-wide locks that prevent the legacy and native desktop
/// applications from writing the same state directory concurrently.
///
/// `gui-instance.json.lock` deliberately matches the legacy Python desktop's
/// authoritative `filelock` path. The native-specific lock provides a stable
/// name after the legacy activation protocol is retired.
#[derive(Debug)]
pub struct ProcessGuard {
    state_root: PathBuf,
    legacy_lock: File,
    native_lock: File,
}

#[derive(Debug, Error)]
pub enum ProcessGuardError {
    #[error("another DcmGet process owns the state directory {0}")]
    AlreadyRunning(PathBuf),
    #[error("cannot prepare DcmGet state directory {path}: {source}")]
    Prepare {
        path: PathBuf,
        #[source]
        source: io::Error,
    },
    #[error("cannot open DcmGet process lock {path}: {source}")]
    Open {
        path: PathBuf,
        #[source]
        source: io::Error,
    },
    #[error("cannot acquire DcmGet process lock {path}: {source}")]
    Lock {
        path: PathBuf,
        #[source]
        source: io::Error,
    },
}

impl ProcessGuard {
    pub fn acquire(state_root: impl AsRef<Path>) -> Result<Self, ProcessGuardError> {
        let state_root = state_root.as_ref().to_path_buf();
        std::fs::create_dir_all(&state_root).map_err(|source| ProcessGuardError::Prepare {
            path: state_root.clone(),
            source,
        })?;

        let legacy_lock = open_lock(&state_root.join("gui-instance.json.lock"))?;
        let legacy_path = state_root.join("gui-instance.json.lock");
        try_lock(&legacy_lock, &legacy_path, &state_root)?;

        let native_path = state_root.join("native-instance.lock");
        let native_lock = open_lock(&native_path)?;
        if let Err(error) = try_lock(&native_lock, &native_path, &state_root) {
            let _ = legacy_lock.unlock();
            return Err(error);
        }

        Ok(Self {
            state_root,
            legacy_lock,
            native_lock,
        })
    }

    #[must_use]
    pub fn state_root(&self) -> &Path {
        &self.state_root
    }
}

impl Drop for ProcessGuard {
    fn drop(&mut self) {
        let _ = self.native_lock.unlock();
        let _ = self.legacy_lock.unlock();
    }
}

fn open_lock(path: &Path) -> Result<File, ProcessGuardError> {
    OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(path)
        .map_err(|source| ProcessGuardError::Open {
            path: path.to_path_buf(),
            source,
        })
}

fn try_lock(file: &File, path: &Path, state_root: &Path) -> Result<(), ProcessGuardError> {
    match file.try_lock() {
        Ok(()) => Ok(()),
        Err(std::fs::TryLockError::WouldBlock) => {
            Err(ProcessGuardError::AlreadyRunning(state_root.to_path_buf()))
        }
        Err(std::fs::TryLockError::Error(source)) => Err(ProcessGuardError::Lock {
            path: path.to_path_buf(),
            source,
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn second_process_guard_is_rejected_until_first_is_dropped() {
        let temp = tempfile::tempdir().unwrap();
        let first = ProcessGuard::acquire(temp.path()).unwrap();

        let error = ProcessGuard::acquire(temp.path()).unwrap_err();
        assert!(matches!(error, ProcessGuardError::AlreadyRunning(_)));

        drop(first);
        ProcessGuard::acquire(temp.path()).unwrap();
    }

    #[test]
    fn guard_uses_the_legacy_lock_name() {
        let temp = tempfile::tempdir().unwrap();
        let _guard = ProcessGuard::acquire(temp.path()).unwrap();
        assert!(temp.path().join("gui-instance.json.lock").is_file());
        assert!(temp.path().join("native-instance.lock").is_file());
    }
}
