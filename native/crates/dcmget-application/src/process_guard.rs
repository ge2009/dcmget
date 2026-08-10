use std::collections::BTreeSet;
use std::fs::{File, OpenOptions};
use std::io;
use std::path::{Path, PathBuf};

use thiserror::Error;

/// Holds the process-wide locks that prevent the legacy and native desktop
/// applications from writing the same state directory concurrently.
///
/// `gui-instance.json.lock` deliberately matches the legacy Python desktop's
/// authoritative `filelock` path. Legacy multi-instance releases instead hold
/// `instances/iN.lock`; the allocation lock is retained for the whole native
/// process lifetime so no Python instance can enter after this scan. Rust's
/// platform file locks use `flock` on supported Unix targets and `LockFileEx`
/// on Windows, matching the native locks used by Python `filelock`.
#[derive(Debug)]
pub struct ProcessGuard {
    state_root: PathBuf,
    legacy_lock: File,
    native_lock: File,
    legacy_allocation_lock: File,
    legacy_profile_locks: Vec<File>,
}

#[derive(Debug, Error)]
pub enum ProcessGuardError {
    #[error("another DcmGet process owns the state directory {0}")]
    AlreadyRunning(PathBuf),
    #[error("a legacy DcmGet process owns lock {0}; close every old DcmGet instance first")]
    LegacyInstanceRunning(PathBuf),
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
    #[error("cannot inspect legacy DcmGet profile locks in {path}: {source}")]
    InspectLegacyProfiles {
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

        let legacy_profiles_root = state_root.join("instances");
        std::fs::create_dir_all(&legacy_profiles_root).map_err(|source| {
            ProcessGuardError::Prepare {
                path: legacy_profiles_root.clone(),
                source,
            }
        })?;
        let allocation_path = legacy_profiles_root.join(".allocate.lock");
        let legacy_allocation_lock = open_lock(&allocation_path)?;
        try_legacy_lock(&legacy_allocation_lock, &allocation_path)?;

        let profile_numbers = legacy_profile_numbers(&legacy_profiles_root)?;
        let mut legacy_profile_locks = Vec::with_capacity(profile_numbers.len() * 2);
        for profile_number in profile_numbers {
            let profile_directory = legacy_profiles_root.join(format!("i{profile_number}"));
            let mut lock_paths = vec![legacy_profiles_root.join(format!("i{profile_number}.lock"))];
            if profile_directory.is_dir() {
                lock_paths.push(profile_directory.join("gui-instance.json.lock"));
            }
            for profile_path in lock_paths {
                let profile_lock = open_lock(&profile_path)?;
                try_legacy_lock(&profile_lock, &profile_path)?;
                legacy_profile_locks.push(profile_lock);
            }
        }

        Ok(Self {
            state_root,
            legacy_lock,
            native_lock,
            legacy_allocation_lock,
            legacy_profile_locks,
        })
    }

    #[must_use]
    pub fn state_root(&self) -> &Path {
        &self.state_root
    }
}

impl Drop for ProcessGuard {
    fn drop(&mut self) {
        for lock in self.legacy_profile_locks.iter().rev() {
            let _ = lock.unlock();
        }
        let _ = self.legacy_allocation_lock.unlock();
        let _ = self.native_lock.unlock();
        let _ = self.legacy_lock.unlock();
    }
}

fn legacy_profile_numbers(root: &Path) -> Result<Vec<u16>, ProcessGuardError> {
    let entries =
        std::fs::read_dir(root).map_err(|source| ProcessGuardError::InspectLegacyProfiles {
            path: root.to_path_buf(),
            source,
        })?;
    let mut numbers = BTreeSet::new();
    for entry in entries {
        let entry = entry.map_err(|source| ProcessGuardError::InspectLegacyProfiles {
            path: root.to_path_buf(),
            source,
        })?;
        let file_type =
            entry
                .file_type()
                .map_err(|source| ProcessGuardError::InspectLegacyProfiles {
                    path: entry.path(),
                    source,
                })?;
        let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
            continue;
        };
        let candidate = if file_type.is_dir() || file_type.is_symlink() {
            if let Some(candidate) = name.strip_suffix(".lock") {
                candidate
            } else {
                name.as_str()
            }
        } else if file_type.is_file() {
            let Some(candidate) = name.strip_suffix(".lock") else {
                continue;
            };
            candidate
        } else {
            continue;
        };
        if let Some(number) = legacy_profile_number(candidate) {
            numbers.insert(number);
        }
    }
    Ok(numbers.into_iter().collect())
}

fn legacy_profile_number(value: &str) -> Option<u16> {
    let digits = value.strip_prefix('i')?;
    if digits.is_empty()
        || digits.starts_with('0')
        || !digits.bytes().all(|byte| byte.is_ascii_digit())
    {
        return None;
    }
    digits
        .parse::<u16>()
        .ok()
        .filter(|number| (1..=9_999).contains(number))
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

fn try_legacy_lock(file: &File, path: &Path) -> Result<(), ProcessGuardError> {
    match file.try_lock() {
        Ok(()) => Ok(()),
        Err(std::fs::TryLockError::WouldBlock) => {
            Err(ProcessGuardError::LegacyInstanceRunning(path.to_path_buf()))
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
        assert!(temp.path().join("instances/.allocate.lock").is_file());
    }

    #[test]
    fn active_legacy_profile_rejects_native_without_changing_lock_file() {
        let temp = tempfile::tempdir().unwrap();
        let instances = temp.path().join("instances");
        std::fs::create_dir_all(instances.join("i2")).unwrap();
        let lock_path = instances.join("i2.lock");
        std::fs::write(&lock_path, b"legacy-lock-metadata").unwrap();
        let legacy = open_lock(&lock_path).unwrap();
        legacy.lock().unwrap();

        let error = ProcessGuard::acquire(temp.path()).unwrap_err();
        assert!(matches!(
            error,
            ProcessGuardError::LegacyInstanceRunning(path) if path == lock_path
        ));
        assert_eq!(std::fs::read(&lock_path).unwrap(), b"legacy-lock-metadata");

        legacy.unlock().unwrap();
        drop(legacy);
        ProcessGuard::acquire(temp.path()).unwrap();
    }

    #[test]
    fn active_legacy_activation_lock_rejects_native_without_changing_lock_file() {
        let temp = tempfile::tempdir().unwrap();
        let profile_directory = temp.path().join("instances/i5");
        std::fs::create_dir_all(&profile_directory).unwrap();
        let lock_path = profile_directory.join("gui-instance.json.lock");
        std::fs::write(&lock_path, b"legacy-activation-lock-metadata").unwrap();
        let legacy = open_lock(&lock_path).unwrap();
        legacy.lock().unwrap();

        let error = ProcessGuard::acquire(temp.path()).unwrap_err();
        assert!(matches!(
            error,
            ProcessGuardError::LegacyInstanceRunning(path) if path == lock_path
        ));
        assert_eq!(
            std::fs::read(&lock_path).unwrap(),
            b"legacy-activation-lock-metadata"
        );

        legacy.unlock().unwrap();
        drop(legacy);
        ProcessGuard::acquire(temp.path()).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn active_legacy_profile_behind_symlink_is_not_skipped() {
        use std::os::unix::fs::symlink;

        let temp = tempfile::tempdir().unwrap();
        let instances = temp.path().join("instances");
        std::fs::create_dir_all(&instances).unwrap();
        let target_path = temp.path().join("legacy-profile.lock");
        std::fs::write(&target_path, b"").unwrap();
        let lock_path = instances.join("i4.lock");
        symlink(&target_path, &lock_path).unwrap();
        let legacy = open_lock(&target_path).unwrap();
        legacy.lock().unwrap();

        let error = ProcessGuard::acquire(temp.path()).unwrap_err();
        assert!(matches!(
            error,
            ProcessGuardError::LegacyInstanceRunning(path) if path == lock_path
        ));
    }

    #[test]
    fn guard_holds_all_discovered_legacy_profile_locks_and_allocation_lock() {
        let temp = tempfile::tempdir().unwrap();
        let instances = temp.path().join("instances");
        std::fs::create_dir_all(instances.join("i1")).unwrap();
        std::fs::create_dir_all(&instances).unwrap();
        std::fs::write(instances.join("i3.lock"), b"").unwrap();

        let guard = ProcessGuard::acquire(temp.path()).unwrap();
        for path in [
            instances.join(".allocate.lock"),
            instances.join("i1.lock"),
            instances.join("i1/gui-instance.json.lock"),
            instances.join("i3.lock"),
        ] {
            let competing = open_lock(&path).unwrap();
            assert!(matches!(
                competing.try_lock(),
                Err(std::fs::TryLockError::WouldBlock)
            ));
        }

        drop(guard);
        for path in [
            instances.join(".allocate.lock"),
            instances.join("i1.lock"),
            instances.join("i1/gui-instance.json.lock"),
            instances.join("i3.lock"),
        ] {
            let competing = open_lock(&path).unwrap();
            competing.try_lock().unwrap();
            competing.unlock().unwrap();
        }
    }

    #[test]
    fn active_legacy_allocator_rejects_native_and_releases_partial_locks() {
        let temp = tempfile::tempdir().unwrap();
        let instances = temp.path().join("instances");
        std::fs::create_dir_all(&instances).unwrap();
        let allocation_path = instances.join(".allocate.lock");
        let allocation = open_lock(&allocation_path).unwrap();
        allocation.lock().unwrap();

        let error = ProcessGuard::acquire(temp.path()).unwrap_err();
        assert!(matches!(
            error,
            ProcessGuardError::LegacyInstanceRunning(path) if path == allocation_path
        ));

        allocation.unlock().unwrap();
        drop(allocation);
        ProcessGuard::acquire(temp.path()).unwrap();
    }
}
