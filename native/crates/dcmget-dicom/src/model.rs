use std::fmt;
use std::path::PathBuf;
use std::time::Duration;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DicomEndpoint {
    pub host: String,
    pub port: u16,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MoveRequest {
    pub profile_id: String,
    pub task_id: String,
    pub accession_number: String,
    pub pacs: DicomEndpoint,
    pub calling_ae_title: String,
    pub pacs_ae_title: String,
    pub storage_ae_title: String,
    pub association_timeout: Duration,
    pub dimse_timeout: Duration,
}

impl MoveRequest {
    /// Build the existing `DcmGet` STUDY-root, Accession Number query contract.
    #[must_use]
    pub fn study_by_accession(
        profile_id: impl Into<String>,
        task_id: impl Into<String>,
        accession_number: impl Into<String>,
        pacs: DicomEndpoint,
        calling_ae_title: impl Into<String>,
        pacs_ae_title: impl Into<String>,
        storage_ae_title: impl Into<String>,
    ) -> Self {
        Self {
            profile_id: profile_id.into(),
            task_id: task_id.into(),
            accession_number: accession_number.into(),
            pacs,
            calling_ae_title: calling_ae_title.into(),
            pacs_ae_title: pacs_ae_title.into(),
            storage_ae_title: storage_ae_title.into(),
            association_timeout: Duration::from_secs(30),
            dimse_timeout: Duration::from_secs(300),
        }
    }

    /// Query keys to pass to a future dicom-rs DIMSE adapter.
    #[must_use]
    pub fn query_keys(&self) -> [(&'static str, &str); 2] {
        [
            ("QueryRetrieveLevel", "STUDY"),
            ("AccessionNumber", &self.accession_number),
        ]
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct MoveCounters {
    pub remaining: Option<u32>,
    pub completed: Option<u32>,
    pub failed: Option<u32>,
    pub warning: Option<u32>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MoveStatusClass {
    Success,
    Pending,
    Cancelled,
    Warning,
    Failure,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MoveFinalStatus {
    pub code: u16,
    pub class: MoveStatusClass,
}

impl MoveFinalStatus {
    #[must_use]
    pub fn from_code(code: u16) -> Self {
        let class = match code {
            0x0000 => MoveStatusClass::Success,
            0xFF00 | 0xFF01 => MoveStatusClass::Pending,
            0xFE00 => MoveStatusClass::Cancelled,
            0xB000..=0xBFFF => MoveStatusClass::Warning,
            0xA000..=0xAFFF
            | 0xC000..=0xCFFF
            | 0x0105
            | 0x0106
            | 0x0110..=0x0112
            | 0x0115
            | 0x0117..=0x0124
            | 0x0210..=0x0213 => MoveStatusClass::Failure,
            _ => MoveStatusClass::Unknown,
        };
        Self { code, class }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AssociationFailure {
    Rejected {
        reason: String,
    },
    ConnectTimeout,
    DimseTimeout,
    Transport {
        message: String,
    },
    Protocol {
        message: String,
    },
    /// The peer did not complete cancellation within the configured grace
    /// period, so the association was aborted to guarantee a bounded stop.
    CancelledAfterAbort,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MoveAttemptResult {
    pub final_status: Option<MoveFinalStatus>,
    pub counters: MoveCounters,
    pub pending_responses: u32,
    pub locally_received_operations: u32,
    pub locally_unique_sop_instances: u32,
    pub association_failure: Option<AssociationFailure>,
    pub cancel_requested: bool,
}

impl MoveAttemptResult {
    #[must_use]
    pub fn has_final_response(&self) -> bool {
        self.final_status
            .is_some_and(|status| status.class != MoveStatusClass::Pending)
    }

    /// A successful PACS status does not prove local completeness.
    #[must_use]
    pub fn local_count_gap(&self) -> Option<(u32, u32)> {
        let completed = self.counters.completed?;
        (self.locally_received_operations < completed)
            .then_some((completed, self.locally_received_operations))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReceiveRoute {
    pub profile_id: String,
    pub task_id: String,
    pub accession_number: String,
    pub destination_root: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoreRequest {
    pub route: ReceiveRoute,
    pub relative_directory: PathBuf,
    pub sop_class_uid: String,
    pub sop_instance_uid: String,
    pub transfer_syntax_uid: String,
}

/// Trusted location used when a C-STORE cannot be published as a normal task
/// object. `active_task_id` records the route which was active when the target
/// was selected; it is correlation context only and does not attribute the
/// quarantined object to that task.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QuarantineTarget {
    pub profile_id: String,
    pub destination_root: PathBuf,
    pub active_task_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QuarantineStoreRequest {
    pub target: QuarantineTarget,
    pub sop_class_uid: String,
    pub sop_instance_uid: String,
    pub transfer_syntax_uid: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReceiveDisposition {
    Published,
    ExistingSkipped,
    ConflictPreserved,
    /// A complete Part 10 payload was preserved outside normal task output
    /// because ownership or command metadata could not be trusted.
    Quarantined,
}

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
pub struct Sha256Digest(pub [u8; 32]);

impl Sha256Digest {
    #[must_use]
    pub fn to_hex(self) -> String {
        const HEX: &[u8; 16] = b"0123456789abcdef";
        let mut output = String::with_capacity(64);
        for byte in self.0 {
            output.push(char::from(HEX[usize::from(byte >> 4)]));
            output.push(char::from(HEX[usize::from(byte & 0x0F)]));
        }
        output
    }
}

impl fmt::Debug for Sha256Digest {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.to_hex())
    }
}

impl fmt::Display for Sha256Digest {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.to_hex())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReceiveOutcome {
    pub disposition: ReceiveDisposition,
    pub path: PathBuf,
    pub sop_instance_uid: String,
    pub sha256: Sha256Digest,
    pub file_bytes: u64,
    pub dataset_bytes: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QuarantineOutcome {
    pub profile_id: String,
    pub active_task_id: Option<String>,
    pub reason: String,
    pub payload: ReceiveOutcome,
}

impl ReceiveOutcome {
    #[must_use]
    pub fn recommended_c_store_status(&self) -> u16 {
        match self.disposition {
            ReceiveDisposition::Published | ReceiveDisposition::ExistingSkipped => 0x0000,
            ReceiveDisposition::ConflictPreserved | ReceiveDisposition::Quarantined => 0xC000,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn move_contract_uses_study_root_accession_and_existing_timeouts() {
        let request = MoveRequest::study_by_accession(
            "profile-1",
            "task-1",
            "ACC001",
            DicomEndpoint {
                host: "127.0.0.1".to_owned(),
                port: 104,
            },
            "DCMGET",
            "PACS",
            "DCMGET",
        );
        assert_eq!(
            request.query_keys(),
            [
                ("QueryRetrieveLevel", "STUDY"),
                ("AccessionNumber", "ACC001"),
            ]
        );
        assert_eq!(request.association_timeout, Duration::from_secs(30));
        assert_eq!(request.dimse_timeout, Duration::from_secs(300));
    }

    #[test]
    fn move_status_and_local_count_are_kept_separate() {
        let result = MoveAttemptResult {
            final_status: Some(MoveFinalStatus::from_code(0x0000)),
            counters: MoveCounters {
                completed: Some(2),
                ..MoveCounters::default()
            },
            pending_responses: 1,
            locally_received_operations: 1,
            locally_unique_sop_instances: 1,
            association_failure: None,
            cancel_requested: false,
        };
        assert!(result.has_final_response());
        assert_eq!(result.local_count_gap(), Some((2, 1)));
        assert_eq!(
            MoveFinalStatus::from_code(0xB000).class,
            MoveStatusClass::Warning
        );
        assert_eq!(
            MoveFinalStatus::from_code(0xC000).class,
            MoveStatusClass::Failure
        );
    }
}
