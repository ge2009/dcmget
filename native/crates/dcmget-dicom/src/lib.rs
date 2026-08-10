//! Native DICOM primitives for `DcmGet`.
//!
//! It contains a Study Root C-MOVE SCU and an asynchronous Storage SCP for the
//! registry-supported transfer syntaxes in dicom-rs 0.10, plus the durable
//! streaming C-STORE payload sink. Raw preservation of transfer syntaxes which
//! dicom-ul cannot negotiate remains an explicit patch boundary; this crate
//! does not claim those syntaxes are supported.

mod cancel;
mod dimse;
mod late_store;
mod model;
mod move_scu;
mod part10;
mod storage_scp;
mod store;

pub use cancel::{CancellationToken, OperationCancelled};
pub use late_store::{LateStoreDecision, LateStorePolicy, LateStoreTracker};
pub use model::{
    AssociationFailure, DicomEndpoint, MoveAttemptResult, MoveCounters, MoveFinalStatus,
    MoveRequest, MoveStatusClass, QuarantineOutcome, QuarantineStoreRequest, QuarantineTarget,
    ReceiveDisposition, ReceiveOutcome, ReceiveRoute, Sha256Digest, StoreRequest,
};
pub use move_scu::{StudyMoveScu, StudyMoveScuConfig};
pub use part10::{FileMeta, Part10Error, build_part10_header};
pub use storage_scp::{
    CStoreCommand, MAX_STORAGE_ASSOCIATIONS, StorageScpConfig, StorageScpError, StorageScpEvent,
    StorageScpHandle, StorageScpService, StoreRequestResolveError, StoreRequestResolver,
    TransferSyntaxSupport, transfer_syntax_support,
};
pub use store::{
    C_STORE_FAILURE_CANNOT_UNDERSTAND, C_STORE_SUCCESS, FileStore, FileStoreSession,
    RawTransferSyntaxPolicy, StoreError, StorePayloadSession, StorePayloadSink,
    TransferSyntaxAcceptancePolicy,
};
