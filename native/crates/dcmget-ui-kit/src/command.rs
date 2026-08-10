use std::sync::{Arc, Mutex};

use crate::ProfileId;

/// A user intent emitted by the native desktop shell.
///
/// This is intentionally presentation-oriented. The desktop integration layer maps these values
/// to the application crate's public command type; the UI does not depend on storage or services.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DesktopCommand {
    SelectProfile(ProfileId),
    CreateTask {
        profile_id: ProfileId,
        accessions: String,
        destination: String,
    },
    OpenSettings,
    OpenDestination(ProfileId),
    PauseTask(String),
    ResumeTask(String),
    CancelTask(String),
    ToggleDetailedLogs(bool),
}

/// The sole side-effect boundary available to a `DcmGet` desktop page.
pub trait CommandSink: Send + Sync + 'static {
    fn submit(&self, command: DesktopCommand);
}

/// Thread-safe command recorder used by the GPUI technical gate and unit tests.
#[derive(Clone, Default)]
pub struct RecordingCommandSink {
    commands: Arc<Mutex<Vec<DesktopCommand>>>,
}

impl RecordingCommandSink {
    pub fn commands(&self) -> Vec<DesktopCommand> {
        self.commands
            .lock()
            .expect("recording command sink mutex poisoned")
            .clone()
    }
}

impl CommandSink for RecordingCommandSink {
    fn submit(&self, command: DesktopCommand) {
        self.commands
            .lock()
            .expect("recording command sink mutex poisoned")
            .push(command);
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
}
