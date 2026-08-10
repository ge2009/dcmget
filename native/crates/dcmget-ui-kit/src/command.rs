#[cfg(any(test, feature = "mock-ui"))]
use std::sync::{Arc, Mutex};

use crate::{ProfileId, WorkspaceSnapshot};

/// A user intent emitted by the native desktop shell.
///
/// This is intentionally presentation-oriented. The desktop integration layer maps these values
/// to the application crate's public command type; the UI does not depend on storage or services.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DesktopCommand {
    ReloadWorkspace,
    SelectProfile(ProfileId),
    StartProfile(ProfileId),
    StopProfile(ProfileId),
    CreateProfile {
        display_name: String,
        pacs_server_ip: String,
        pacs_server_port: u16,
        calling_ae_title: String,
        pacs_ae_title: String,
        storage_ae_title: String,
        storage_port: u16,
        default_destination: String,
        anonymization_enabled: bool,
    },
    SaveProfile {
        profile_id: ProfileId,
        display_name: String,
        pacs_server_ip: String,
        pacs_server_port: u16,
        calling_ae_title: String,
        pacs_ae_title: String,
        storage_ae_title: String,
        storage_port: u16,
        default_destination: String,
        anonymization_enabled: bool,
    },
    CreateTask {
        profile_id: ProfileId,
        accessions: String,
        destination: String,
    },
    OpenSettings,
    OpenDestination(ProfileId),
    OpenLogDirectory,
    PauseTask(String),
    ResumeTask(String),
    StartTask(String),
    CancelTask(String),
    DeleteTask(String),
    ToggleDetailedLogs(bool),
}

/// The sole side-effect boundary available to a `DcmGet` desktop page.
pub trait CommandSink: Send + Sync + 'static {
    fn submit(&self, command: DesktopCommand);
}

/// Read-only presentation state supplied by the application integration layer.
///
/// Implementations may cache the latest application snapshot, but must not fabricate progress or
/// task data when the application service is unavailable. Returning an error lets the desktop
/// render a recoverable startup/runtime failure instead of silently falling back to demo data.
pub trait SnapshotSource: Send + Sync + 'static {
    fn snapshot(&self) -> Result<WorkspaceSnapshot, String>;
}

/// Complete backend contract consumed by the native desktop shell.
pub trait WorkspaceBackend: CommandSink + SnapshotSource {
    /// Request a graceful application shutdown and wait for background workers to release their
    /// ports/processes. Implementations must bound the wait and return an actionable error.
    fn shutdown(&self) -> Result<(), String>;
}

/// Thread-safe command recorder used by the GPUI technical gate and unit tests.
#[cfg(any(test, feature = "mock-ui"))]
#[derive(Clone, Default)]
pub struct RecordingCommandSink {
    commands: Arc<Mutex<Vec<DesktopCommand>>>,
}

#[cfg(any(test, feature = "mock-ui"))]
impl RecordingCommandSink {
    pub fn commands(&self) -> Vec<DesktopCommand> {
        self.commands
            .lock()
            .expect("recording command sink mutex poisoned")
            .clone()
    }
}

#[cfg(any(test, feature = "mock-ui"))]
impl CommandSink for RecordingCommandSink {
    fn submit(&self, command: DesktopCommand) {
        self.commands
            .lock()
            .expect("recording command sink mutex poisoned")
            .push(command);
    }
}

#[cfg(any(test, feature = "mock-ui"))]
impl SnapshotSource for RecordingCommandSink {
    fn snapshot(&self) -> Result<WorkspaceSnapshot, String> {
        Ok(WorkspaceSnapshot::technical_gate_sample())
    }
}

#[cfg(any(test, feature = "mock-ui"))]
impl WorkspaceBackend for RecordingCommandSink {
    fn shutdown(&self) -> Result<(), String> {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recording_sink_preserves_command_order() {
        let sink = RecordingCommandSink::default();
        sink.submit(DesktopCommand::SelectProfile(ProfileId::new("profile-a")));
        sink.submit(DesktopCommand::ToggleDetailedLogs(true));

        assert_eq!(
            sink.commands(),
            vec![
                DesktopCommand::SelectProfile(ProfileId::new("profile-a")),
                DesktopCommand::ToggleDetailedLogs(true),
            ]
        );
    }

    #[test]
    fn recording_sink_keeps_new_profile_and_open_destination_as_single_intents() {
        let sink = RecordingCommandSink::default();
        let profile_id = ProfileId::new("profile-a");
        sink.submit(DesktopCommand::CreateProfile {
            display_name: "CT".into(),
            pacs_server_ip: "127.0.0.1".into(),
            pacs_server_port: 104,
            calling_ae_title: "DCMGET".into(),
            pacs_ae_title: "PACS".into(),
            storage_ae_title: "DCMGET".into(),
            storage_port: 6666,
            default_destination: "D:/DICOM".into(),
            anonymization_enabled: false,
        });
        sink.submit(DesktopCommand::OpenDestination(profile_id.clone()));

        let commands = sink.commands();
        assert_eq!(commands.len(), 2);
        assert!(matches!(commands[0], DesktopCommand::CreateProfile { .. }));
        assert_eq!(commands[1], DesktopCommand::OpenDestination(profile_id));
    }
}
