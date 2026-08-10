use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use dicom_core::dictionary::UidDictionary;
use dicom_dictionary_std::StandardSopClassDictionary;
use dicom_dictionary_std::{tags, uids};
use dicom_encoding::transfer_syntax::TransferSyntaxIndex;
use dicom_transfer_syntax_registry::TransferSyntaxRegistry;
use dicom_ul::Pdu;
use dicom_ul::association::server::ServerAssociationOptions;
use dicom_ul::pdu::{PDataValue, PDataValueType};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{Semaphore, broadcast, watch};
use tokio::task::{JoinHandle, JoinSet};

use crate::CancellationToken;
use crate::dimse::{
    C_ECHO_RQ, C_STORE_RQ, CommandAssembler, HAS_DATASET, NO_DATASET, command_dataset_type,
    command_field, decode_command, echo_response, encode_command, message_id, required_text,
    store_response,
};
use crate::model::{ReceiveOutcome, StoreRequest};
use crate::store::{StoreError, StorePayloadSession, StorePayloadSink};

pub const MAX_STORAGE_ASSOCIATIONS: usize = 16;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TransferSyntaxSupport {
    RegistrySupported,
    /// dicom-ul 0.10 rejects this syntax during association negotiation.
    /// Raw preservation requires the pinned negotiation-policy patch planned
    /// for `DcmGet` rather than pretending the syntax is supported.
    RequiresDicomUlPatch,
}

#[must_use]
pub fn transfer_syntax_support(transfer_syntax_uid: &str) -> TransferSyntaxSupport {
    if TransferSyntaxRegistry
        .get(transfer_syntax_uid)
        .is_some_and(|syntax| !syntax.is_unsupported())
    {
        TransferSyntaxSupport::RegistrySupported
    } else {
        TransferSyntaxSupport::RequiresDicomUlPatch
    }
}

/// Whether the standard SOP class registry identifies this UID as a Storage
/// Service SOP Class.
///
/// The registry does not expose the PS3.4 service-class relationship directly,
/// but its normative SOP Class names consistently contain ` Storage` for
/// Storage Service instances. Media Storage Directory Storage is specifically
/// a media interchange SOP Class rather than a network Storage Service class.
#[must_use]
fn is_storage_sop_class(sop_class_uid: &str) -> bool {
    StandardSopClassDictionary
        .by_uid(sop_class_uid)
        .is_some_and(|entry| {
            entry.name.contains(" Storage") && entry.alias != "MediaStorageDirectoryStorage"
        })
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CStoreCommand {
    pub message_id: u16,
    pub sop_class_uid: String,
    pub sop_instance_uid: String,
    pub move_originator_ae_title: Option<String>,
    pub move_originator_message_id: Option<u16>,
}

pub trait StoreRequestResolver: Send + Sync {
    fn resolve(
        &self,
        command: &CStoreCommand,
        transfer_syntax_uid: &str,
    ) -> Result<StoreRequest, StoreRequestResolveError>;
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("cannot attribute received C-STORE: {message}")]
pub struct StoreRequestResolveError {
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StorageScpConfig {
    pub bind_address: SocketAddr,
    pub ae_title: String,
    pub maximum_associations: usize,
    pub maximum_pdu_length: u32,
    pub read_timeout: Duration,
    pub write_timeout: Duration,
}

impl StorageScpConfig {
    #[must_use]
    pub fn new(bind_address: SocketAddr, ae_title: impl Into<String>) -> Self {
        Self {
            bind_address,
            ae_title: ae_title.into(),
            maximum_associations: MAX_STORAGE_ASSOCIATIONS,
            maximum_pdu_length: 128 * 1024,
            read_timeout: Duration::from_secs(300),
            write_timeout: Duration::from_secs(30),
        }
    }

    fn validate(&self) -> Result<(), StorageScpError> {
        if self.ae_title.is_empty()
            || self.ae_title.len() > 16
            || !self
                .ae_title
                .bytes()
                .all(|byte| byte.is_ascii_graphic() && byte != b'\\')
        {
            return Err(StorageScpError::InvalidConfig {
                message: "AE title must contain 1-16 printable ASCII characters".to_owned(),
            });
        }
        if !(1..=MAX_STORAGE_ASSOCIATIONS).contains(&self.maximum_associations) {
            return Err(StorageScpError::InvalidConfig {
                message: format!(
                    "maximum_associations must be between 1 and {MAX_STORAGE_ASSOCIATIONS}"
                ),
            });
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StorageScpEvent {
    Ready {
        address: SocketAddr,
    },
    AssociationOpened {
        peer: SocketAddr,
    },
    AssociationClosed {
        peer: SocketAddr,
    },
    EchoCompleted {
        peer: SocketAddr,
    },
    StoreCompleted {
        peer: SocketAddr,
        request: Box<StoreRequest>,
        outcome: ReceiveOutcome,
    },
    StoreFailed {
        peer: SocketAddr,
        sop_instance_uid: Option<String>,
        message: String,
    },
    AssociationFailed {
        peer: SocketAddr,
        message: String,
    },
}

#[derive(Debug, thiserror::Error)]
pub enum StorageScpError {
    #[error("invalid Storage SCP configuration: {message}")]
    InvalidConfig { message: String },
    #[error("cannot bind Storage SCP to {address}: {source}")]
    Bind {
        address: SocketAddr,
        #[source]
        source: std::io::Error,
    },
    #[error("Storage SCP task failed: {message}")]
    Task { message: String },
}

pub struct StorageScpHandle {
    local_address: SocketAddr,
    shutdown: watch::Sender<bool>,
    events: broadcast::Sender<StorageScpEvent>,
    task: Option<JoinHandle<Result<(), StorageScpError>>>,
}

impl StorageScpHandle {
    #[must_use]
    pub fn local_address(&self) -> SocketAddr {
        self.local_address
    }

    #[must_use]
    pub fn is_ready(&self) -> bool {
        self.task.as_ref().is_some_and(|task| !task.is_finished())
    }

    pub fn subscribe(&self) -> broadcast::Receiver<StorageScpEvent> {
        let receiver = self.events.subscribe();
        let _ = self.events.send(StorageScpEvent::Ready {
            address: self.local_address,
        });
        receiver
    }

    /// Stop accepting, abort active associations, drain their tasks and then
    /// return. After this future resolves the listening port is released.
    pub async fn shutdown(mut self) -> Result<(), StorageScpError> {
        let _ = self.shutdown.send(true);
        let Some(task) = self.task.take() else {
            return Ok(());
        };
        task.await.map_err(|error| StorageScpError::Task {
            message: error.to_string(),
        })?
    }
}

impl Drop for StorageScpHandle {
    fn drop(&mut self) {
        let _ = self.shutdown.send(true);
    }
}

pub struct StorageScpService;

impl StorageScpService {
    /// Bind first and only then return a ready handle, so C-MOVE is never sent
    /// before the receiver actually owns its port.
    pub async fn start<S, R>(
        config: StorageScpConfig,
        sink: S,
        resolver: R,
    ) -> Result<StorageScpHandle, StorageScpError>
    where
        S: StorePayloadSink + Send + Sync + 'static,
        S::Session: Send + 'static,
        R: StoreRequestResolver + 'static,
    {
        config.validate()?;
        let listener = TcpListener::bind(config.bind_address)
            .await
            .map_err(|source| StorageScpError::Bind {
                address: config.bind_address,
                source,
            })?;
        let local_address = listener
            .local_addr()
            .map_err(|source| StorageScpError::Bind {
                address: config.bind_address,
                source,
            })?;
        let (shutdown, shutdown_receiver) = watch::channel(false);
        let (events, _) = broadcast::channel(256);
        let task = tokio::spawn(run_listener(
            listener,
            config,
            Arc::new(sink),
            Arc::new(resolver),
            shutdown_receiver,
            events.clone(),
        ));
        Ok(StorageScpHandle {
            local_address,
            shutdown,
            events,
            task: Some(task),
        })
    }
}

async fn run_listener<S, R>(
    listener: TcpListener,
    config: StorageScpConfig,
    sink: Arc<S>,
    resolver: Arc<R>,
    mut shutdown: watch::Receiver<bool>,
    events: broadcast::Sender<StorageScpEvent>,
) -> Result<(), StorageScpError>
where
    S: StorePayloadSink + Send + Sync + 'static,
    S::Session: Send + 'static,
    R: StoreRequestResolver + 'static,
{
    let capacity = Arc::new(Semaphore::new(config.maximum_associations));
    let mut associations = JoinSet::new();
    loop {
        let permit = tokio::select! {
            changed = shutdown.changed() => {
                let _ = changed;
                break;
            }
            permit = Arc::clone(&capacity).acquire_owned() => {
                permit.map_err(|error| StorageScpError::Task { message: error.to_string() })?
            }
        };
        let accepted = tokio::select! {
            changed = shutdown.changed() => {
                let _ = changed;
                drop(permit);
                break;
            }
            accepted = listener.accept() => accepted,
        };
        let (stream, peer) = match accepted {
            Ok(accepted) => accepted,
            Err(error) => {
                return Err(StorageScpError::Task {
                    message: format!("accept failed: {error}"),
                });
            }
        };
        let association_shutdown = shutdown.clone();
        let association_config = config.clone();
        let association_sink = Arc::clone(&sink);
        let association_resolver = Arc::clone(&resolver);
        let association_events = events.clone();
        associations.spawn(async move {
            let _permit = permit;
            let _ = association_events.send(StorageScpEvent::AssociationOpened { peer });
            if let Err(error) = serve_association(
                stream,
                peer,
                &association_config,
                association_sink.as_ref(),
                association_resolver.as_ref(),
                association_shutdown,
                &association_events,
            )
            .await
            {
                let _ = association_events.send(StorageScpEvent::AssociationFailed {
                    peer,
                    message: error.to_string(),
                });
            }
            let _ = association_events.send(StorageScpEvent::AssociationClosed { peer });
        });
    }
    drop(listener);
    while associations.join_next().await.is_some() {}
    Ok(())
}

#[derive(Debug, thiserror::Error)]
enum AssociationServiceError {
    #[error("association negotiation failed: {0}")]
    Establish(String),
    #[error("association transport failed: {0}")]
    Transport(String),
    #[error("invalid DIMSE message: {0}")]
    Protocol(String),
}

#[allow(clippy::too_many_lines)] // one DIMSE state machine owns command/data ordering
async fn serve_association<S, R>(
    stream: TcpStream,
    peer: SocketAddr,
    config: &StorageScpConfig,
    sink: &S,
    resolver: &R,
    mut shutdown: watch::Receiver<bool>,
    events: &broadcast::Sender<StorageScpEvent>,
) -> Result<(), AssociationServiceError>
where
    S: StorePayloadSink + Send + Sync,
    R: StoreRequestResolver,
{
    let mut options = ServerAssociationOptions::new()
        .accept_called_ae_title()
        .ae_title(&config.ae_title)
        .strict(true)
        .promiscuous(true)
        .max_pdu_length(config.maximum_pdu_length)
        .read_timeout(config.read_timeout)
        .write_timeout(config.write_timeout)
        .with_abstract_syntax(uids::VERIFICATION);
    for transfer_syntax in TransferSyntaxRegistry.iter() {
        if !transfer_syntax.is_unsupported() {
            options = options.with_transfer_syntax(transfer_syntax.uid());
        }
    }
    let establish = options.establish_async(stream);
    let mut association = tokio::select! {
        changed = shutdown.changed() => {
            let _ = changed;
            return Ok(());
        }
        association = establish => association
            .map_err(|error| AssociationServiceError::Establish(error.to_string()))?,
    };
    let cancellation = CancellationToken::new();
    let mut command_assembler = CommandAssembler::default();
    let mut active_store: Option<ActiveStore<S::Session>> = None;

    loop {
        let pdu = tokio::select! {
            changed = shutdown.changed() => {
                let _ = changed;
                cancellation.cancel();
                if let Some(active) = active_store.take() {
                    active.abort();
                }
                let _ = tokio::time::timeout(Duration::from_secs(1), association.abort()).await;
                return Ok(());
            }
            received = association.receive() => received
                .map_err(|error| AssociationServiceError::Transport(error.to_string()))?,
        };

        match pdu {
            Pdu::PData { data } => {
                for value in data {
                    match value.value_type {
                        PDataValueType::Command => {
                            if active_store.is_some() {
                                return Err(AssociationServiceError::Protocol(
                                    "received a new command before the C-STORE dataset ended"
                                        .to_owned(),
                                ));
                            }
                            let assembled = command_assembler.push(&value).map_err(|error| {
                                AssociationServiceError::Protocol(error.to_string())
                            })?;
                            let Some(assembled) = assembled else {
                                continue;
                            };
                            let command = decode_command(&assembled.bytes).map_err(|error| {
                                AssociationServiceError::Protocol(error.to_string())
                            })?;
                            match command_field(&command).map_err(|error| {
                                AssociationServiceError::Protocol(error.to_string())
                            })? {
                                C_ECHO_RQ => {
                                    if command_dataset_type(&command).map_err(|error| {
                                        AssociationServiceError::Protocol(error.to_string())
                                    })? != NO_DATASET
                                    {
                                        return Err(AssociationServiceError::Protocol(
                                            "C-ECHO-RQ unexpectedly declared a dataset".to_owned(),
                                        ));
                                    }
                                    let Some(context) = association
                                        .presentation_contexts()
                                        .iter()
                                        .find(|context| {
                                            context.id == assembled.presentation_context_id
                                        })
                                    else {
                                        return Err(AssociationServiceError::Protocol(
                                            "C-ECHO used an unnegotiated presentation context"
                                                .to_owned(),
                                        ));
                                    };
                                    if context.abstract_syntax != uids::VERIFICATION {
                                        return Err(AssociationServiceError::Protocol(
                                            "C-ECHO requires the Verification presentation context"
                                                .to_owned(),
                                        ));
                                    }
                                    if required_text(
                                        &command,
                                        tags::AFFECTED_SOP_CLASS_UID,
                                        "Affected SOP Class UID",
                                    )
                                    .map_err(|error| {
                                        AssociationServiceError::Protocol(error.to_string())
                                    })? != uids::VERIFICATION
                                    {
                                        return Err(AssociationServiceError::Protocol(
                                            "C-ECHO affected SOP Class must be Verification"
                                                .to_owned(),
                                        ));
                                    }
                                    let response =
                                        echo_response(message_id(&command).map_err(|error| {
                                            AssociationServiceError::Protocol(error.to_string())
                                        })?);
                                    send_command(
                                        &mut association,
                                        assembled.presentation_context_id,
                                        &response,
                                    )
                                    .await?;
                                    let _ = events.send(StorageScpEvent::EchoCompleted { peer });
                                }
                                C_STORE_RQ => {
                                    if command_dataset_type(&command).map_err(|error| {
                                        AssociationServiceError::Protocol(error.to_string())
                                    })? != HAS_DATASET
                                    {
                                        return Err(AssociationServiceError::Protocol(
                                            "C-STORE-RQ did not declare a dataset".to_owned(),
                                        ));
                                    }
                                    let parsed =
                                        parse_store_command(&command).map_err(|error| {
                                            AssociationServiceError::Protocol(error.to_string())
                                        })?;
                                    let Some(context) = association
                                        .presentation_contexts()
                                        .iter()
                                        .find(|context| {
                                            context.id == assembled.presentation_context_id
                                        })
                                    else {
                                        return Err(AssociationServiceError::Protocol(
                                            "C-STORE used an unnegotiated presentation context"
                                                .to_owned(),
                                        ));
                                    };
                                    if context.abstract_syntax != parsed.sop_class_uid {
                                        return Err(AssociationServiceError::Protocol(
                                            "C-STORE SOP Class does not match its presentation context"
                                                .to_owned(),
                                        ));
                                    }
                                    if transfer_syntax_support(&context.transfer_syntax)
                                        != TransferSyntaxSupport::RegistrySupported
                                    {
                                        return Err(AssociationServiceError::Protocol(format!(
                                            "transfer syntax {} requires the dicom-ul raw negotiation patch",
                                            context.transfer_syntax
                                        )));
                                    }
                                    active_store = Some(
                                        if is_storage_sop_class(&parsed.sop_class_uid) {
                                            ActiveStore::begin(
                                                parsed,
                                                assembled.presentation_context_id,
                                                &context.transfer_syntax,
                                                sink,
                                                resolver,
                                                cancellation.clone(),
                                            )
                                        } else {
                                            let message = format!(
                                                "SOP Class {} is not a standard Storage Service SOP Class",
                                                parsed.sop_class_uid
                                            );
                                            ActiveStore::rejected(
                                                parsed,
                                                assembled.presentation_context_id,
                                                message,
                                            )
                                        },
                                    );
                                }
                                other => {
                                    return Err(AssociationServiceError::Protocol(format!(
                                        "unsupported DIMSE command field 0x{other:04X}"
                                    )));
                                }
                            }
                        }
                        PDataValueType::Data => {
                            if command_assembler.is_partial() {
                                return Err(AssociationServiceError::Protocol(
                                    "dataset arrived before the fragmented command ended"
                                        .to_owned(),
                                ));
                            }
                            let Some(active) = active_store.as_mut() else {
                                return Err(AssociationServiceError::Protocol(
                                    "dataset arrived without a C-STORE command".to_owned(),
                                ));
                            };
                            active.write(&value);
                            if value.is_last {
                                let completed = active_store
                                    .take()
                                    .expect("active store was checked above")
                                    .finish();
                                let response_status = completed.response_status;
                                send_command(
                                    &mut association,
                                    completed.presentation_context_id,
                                    &store_response(
                                        completed.command.message_id,
                                        &completed.command.sop_class_uid,
                                        &completed.command.sop_instance_uid,
                                        response_status,
                                    ),
                                )
                                .await?;
                                match completed.result {
                                    Ok((request, outcome)) => {
                                        let _ = events.send(StorageScpEvent::StoreCompleted {
                                            peer,
                                            request: Box::new(request),
                                            outcome,
                                        });
                                    }
                                    Err(message) => {
                                        let _ = events.send(StorageScpEvent::StoreFailed {
                                            peer,
                                            sop_instance_uid: Some(
                                                completed.command.sop_instance_uid,
                                            ),
                                            message,
                                        });
                                    }
                                }
                            }
                        }
                    }
                }
            }
            Pdu::ReleaseRQ => {
                association
                    .send(&Pdu::ReleaseRP)
                    .await
                    .map_err(|error| AssociationServiceError::Transport(error.to_string()))?;
                return Ok(());
            }
            Pdu::AbortRQ { .. } => return Ok(()),
            other => {
                return Err(AssociationServiceError::Protocol(format!(
                    "unexpected PDU while association was active: {}",
                    other.short_description()
                )));
            }
        }
    }
}

async fn send_command(
    association: &mut dicom_ul::association::server::AsyncServerAssociation<TcpStream>,
    presentation_context_id: u8,
    command: &crate::dimse::CommandObject,
) -> Result<(), AssociationServiceError> {
    let bytes = encode_command(command)
        .map_err(|error| AssociationServiceError::Protocol(error.to_string()))?;
    association
        .send(&Pdu::PData {
            data: vec![PDataValue {
                presentation_context_id,
                value_type: PDataValueType::Command,
                is_last: true,
                data: bytes,
            }],
        })
        .await
        .map_err(|error| AssociationServiceError::Transport(error.to_string()))
}

fn parse_store_command(
    command: &crate::dimse::CommandObject,
) -> Result<CStoreCommand, crate::dimse::DimseError> {
    let optional_text = |tag| {
        command
            .element(tag)
            .ok()
            .and_then(|element| element.to_str().ok())
            .map(|value| value.trim_matches(['\0', ' ']).to_owned())
    };
    let optional_u16 = |tag| {
        command
            .element(tag)
            .ok()
            .and_then(|element| element.uint16().ok())
    };
    Ok(CStoreCommand {
        message_id: message_id(command)?,
        sop_class_uid: required_text(
            command,
            tags::AFFECTED_SOP_CLASS_UID,
            "Affected SOP Class UID",
        )?,
        sop_instance_uid: required_text(
            command,
            tags::AFFECTED_SOP_INSTANCE_UID,
            "Affected SOP Instance UID",
        )?,
        move_originator_ae_title: optional_text(tags::MOVE_ORIGINATOR_APPLICATION_ENTITY_TITLE),
        move_originator_message_id: optional_u16(tags::MOVE_ORIGINATOR_MESSAGE_ID),
    })
}

struct ActiveStore<T> {
    command: CStoreCommand,
    request: Option<StoreRequest>,
    presentation_context_id: u8,
    session: Option<T>,
    failure: Option<String>,
}

impl<T> ActiveStore<T>
where
    T: StorePayloadSession,
{
    fn rejected(command: CStoreCommand, presentation_context_id: u8, message: String) -> Self {
        Self {
            command,
            request: None,
            presentation_context_id,
            session: None,
            failure: Some(message),
        }
    }

    fn begin<S, R>(
        command: CStoreCommand,
        presentation_context_id: u8,
        transfer_syntax_uid: &str,
        sink: &S,
        resolver: &R,
        cancellation: CancellationToken,
    ) -> Self
    where
        S: StorePayloadSink<Session = T>,
        R: StoreRequestResolver,
    {
        match resolver.resolve(&command, transfer_syntax_uid) {
            Err(error) => Self::rejected(command, presentation_context_id, error.to_string()),
            Ok(request) => {
                if let Err(message) =
                    validate_resolved_request(&command, transfer_syntax_uid, &request)
                {
                    return Self::rejected(command, presentation_context_id, message);
                }
                match sink.begin_store(request.clone(), cancellation) {
                    Ok(session) => Self {
                        command,
                        request: Some(request),
                        presentation_context_id,
                        session: Some(session),
                        failure: None,
                    },
                    Err(error) => Self {
                        command,
                        request: Some(request),
                        presentation_context_id,
                        session: None,
                        failure: Some(error.to_string()),
                    },
                }
            }
        }
    }

    fn write(&mut self, value: &PDataValue) {
        if value.presentation_context_id != self.presentation_context_id {
            self.fail("C-STORE dataset changed presentation context".to_owned());
            return;
        }
        let Some(session) = self.session.as_mut() else {
            return;
        };
        if let Err(error) = session.write_dataset_chunk(&value.data) {
            self.fail(error.to_string());
        }
    }

    fn fail(&mut self, message: String) {
        if let Some(session) = self.session.take() {
            let _ = session.abort();
        }
        self.failure.get_or_insert(message);
    }

    fn finish(mut self) -> CompletedStore {
        let result = if let Some(message) = self.failure.take() {
            Err(message)
        } else {
            match self.session.take() {
                Some(session) => match session.finish() {
                    Ok(outcome) => self
                        .request
                        .take()
                        .map(|request| (request, outcome))
                        .ok_or_else(|| "store request was lost before publication".to_owned()),
                    Err(error) => Err(error.to_string()),
                },
                None => Err("store session was not created".to_owned()),
            }
        };
        let response_status = result.as_ref().map_or_else(
            |_| StoreError::EmptyDataset.recommended_c_store_status(),
            |(_, outcome)| outcome.recommended_c_store_status(),
        );
        CompletedStore {
            command: self.command,
            presentation_context_id: self.presentation_context_id,
            response_status,
            result,
        }
    }

    fn abort(mut self) {
        if let Some(session) = self.session.take() {
            let _ = session.abort();
        }
    }
}

fn validate_resolved_request(
    command: &CStoreCommand,
    transfer_syntax_uid: &str,
    request: &StoreRequest,
) -> Result<(), String> {
    if request.sop_class_uid != command.sop_class_uid {
        return Err("resolved SOP Class UID does not match the C-STORE command".to_owned());
    }
    if request.sop_instance_uid != command.sop_instance_uid {
        return Err("resolved SOP Instance UID does not match the C-STORE command".to_owned());
    }
    if request.transfer_syntax_uid != transfer_syntax_uid {
        return Err(
            "resolved Transfer Syntax UID does not match the negotiated presentation context"
                .to_owned(),
        );
    }
    Ok(())
}

struct CompletedStore {
    command: CStoreCommand,
    presentation_context_id: u8,
    response_status: u16,
    result: Result<(StoreRequest, ReceiveOutcome), String>,
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use dicom_core::{DataElement, PrimitiveValue, VR, dicom_value};

    use super::*;
    use crate::dimse::{C_STORE_RSP, CommandObject, HAS_DATASET, responded_message_id, status};
    use crate::{FileStore, ReceiveRoute};

    #[derive(Clone)]
    struct FixedResolver {
        root: PathBuf,
    }

    impl StoreRequestResolver for FixedResolver {
        fn resolve(
            &self,
            command: &CStoreCommand,
            transfer_syntax_uid: &str,
        ) -> Result<StoreRequest, StoreRequestResolveError> {
            Ok(StoreRequest {
                route: ReceiveRoute {
                    profile_id: "profile-1".to_owned(),
                    task_id: "task-1".to_owned(),
                    accession_number: "ACC-1".to_owned(),
                    destination_root: self.root.clone(),
                },
                relative_directory: PathBuf::from("ACC-1"),
                sop_class_uid: command.sop_class_uid.clone(),
                sop_instance_uid: command.sop_instance_uid.clone(),
                transfer_syntax_uid: transfer_syntax_uid.to_owned(),
            })
        }
    }

    struct ReturningResolver {
        request: StoreRequest,
    }

    impl StoreRequestResolver for ReturningResolver {
        fn resolve(
            &self,
            _command: &CStoreCommand,
            _transfer_syntax_uid: &str,
        ) -> Result<StoreRequest, StoreRequestResolveError> {
            Ok(self.request.clone())
        }
    }

    fn store_request_command(
        message_id: u16,
        sop_class_uid: &str,
        sop_instance_uid: &str,
    ) -> Vec<u8> {
        encode_command(&CommandObject::command_from_element_iter([
            DataElement::new(
                tags::AFFECTED_SOP_CLASS_UID,
                VR::UI,
                PrimitiveValue::from(sop_class_uid),
            ),
            DataElement::new(tags::COMMAND_FIELD, VR::US, dicom_value!(U16, [C_STORE_RQ])),
            DataElement::new(tags::MESSAGE_ID, VR::US, dicom_value!(U16, [message_id])),
            DataElement::new(tags::PRIORITY, VR::US, dicom_value!(U16, [0x0000])),
            DataElement::new(
                tags::COMMAND_DATA_SET_TYPE,
                VR::US,
                dicom_value!(U16, [HAS_DATASET]),
            ),
            DataElement::new(
                tags::AFFECTED_SOP_INSTANCE_UID,
                VR::UI,
                PrimitiveValue::from(sop_instance_uid),
            ),
        ]))
        .unwrap()
    }

    #[test]
    fn unknown_transfer_syntax_is_not_claimed_as_supported() {
        assert_eq!(
            transfer_syntax_support("1.2.826.0.1.3680043.10.999.1"),
            TransferSyntaxSupport::RequiresDicomUlPatch
        );
        assert_eq!(
            transfer_syntax_support("1.2.840.10008.1.2.1"),
            TransferSyntaxSupport::RegistrySupported
        );
    }

    #[test]
    fn standard_registry_distinguishes_storage_from_non_storage_sop_classes() {
        assert!(is_storage_sop_class(uids::CT_IMAGE_STORAGE));
        assert!(is_storage_sop_class(uids::HANGING_PROTOCOL_STORAGE));
        assert!(!is_storage_sop_class(uids::VERIFICATION));
        assert!(!is_storage_sop_class(
            uids::STUDY_ROOT_QUERY_RETRIEVE_INFORMATION_MODEL_FIND
        ));
        assert!(!is_storage_sop_class(uids::STORAGE_COMMITMENT_PUSH_MODEL));
        assert!(!is_storage_sop_class(uids::MEDIA_STORAGE_DIRECTORY_STORAGE));
        assert!(!is_storage_sop_class("1.2.826.0.1.3680043.10.999.2"));
    }

    #[test]
    fn resolver_metadata_mismatch_never_starts_a_store_session() {
        let temporary = tempfile::tempdir().unwrap();
        let command = CStoreCommand {
            message_id: 1,
            sop_class_uid: uids::CT_IMAGE_STORAGE.to_owned(),
            sop_instance_uid: "1.2.826.0.1.3680043.10.987.100".to_owned(),
            move_originator_ae_title: None,
            move_originator_message_id: None,
        };
        let exact = FixedResolver {
            root: temporary.path().to_path_buf(),
        }
        .resolve(&command, uids::EXPLICIT_VR_LITTLE_ENDIAN)
        .unwrap();
        let mut mismatched_requests = Vec::new();
        let mut request = exact.clone();
        request.sop_class_uid = uids::MR_IMAGE_STORAGE.to_owned();
        mismatched_requests.push(request);
        let mut request = exact.clone();
        request.sop_instance_uid = "1.2.826.0.1.3680043.10.987.101".to_owned();
        mismatched_requests.push(request);
        let mut request = exact;
        request.transfer_syntax_uid = uids::IMPLICIT_VR_LITTLE_ENDIAN.to_owned();
        mismatched_requests.push(request);

        for request in mismatched_requests {
            let active = ActiveStore::begin(
                command.clone(),
                1,
                uids::EXPLICIT_VR_LITTLE_ENDIAN,
                &FileStore::new(),
                &ReturningResolver { request },
                CancellationToken::new(),
            );
            let completed = active.finish();
            assert_eq!(completed.response_status, 0xC000);
            assert!(completed.result.is_err());
        }
        assert!(!temporary.path().join("ACC-1").exists());
    }

    #[test]
    fn association_limit_cannot_exceed_sixteen() {
        let mut config = StorageScpConfig::new("127.0.0.1:0".parse().unwrap(), "DCMGET");
        config.maximum_associations = 17;
        assert!(matches!(
            config.validate(),
            Err(StorageScpError::InvalidConfig { .. })
        ));
    }

    #[tokio::test]
    async fn fragmented_c_store_streams_to_disk_and_shutdown_releases_port() {
        let temporary = tempfile::tempdir().unwrap();
        let config = StorageScpConfig::new("127.0.0.1:0".parse().unwrap(), "DCMGET");
        let handle = StorageScpService::start(
            config,
            FileStore::new(),
            FixedResolver {
                root: temporary.path().to_path_buf(),
            },
        )
        .await
        .unwrap();
        let address = handle.local_address();
        assert!(handle.is_ready());
        let mut events = handle.subscribe();
        assert_eq!(
            events.recv().await.unwrap(),
            StorageScpEvent::Ready { address }
        );

        let mut client = dicom_ul::association::client::ClientAssociationOptions::new()
            .calling_ae_title("TESTSCU")
            .called_ae_title("DCMGET")
            .with_presentation_context(uids::CT_IMAGE_STORAGE, vec!["1.2.840.10008.1.2.1"])
            .establish_async(address)
            .await
            .unwrap();
        let context_id = client.presentation_contexts()[0].id;
        let command =
            store_request_command(7, uids::CT_IMAGE_STORAGE, "1.2.826.0.1.3680043.10.987.99");
        let split = command.len() / 2;
        client
            .send(&Pdu::PData {
                data: vec![PDataValue {
                    presentation_context_id: context_id,
                    value_type: PDataValueType::Command,
                    is_last: false,
                    data: command[..split].to_vec(),
                }],
            })
            .await
            .unwrap();
        client
            .send(&Pdu::PData {
                data: vec![PDataValue {
                    presentation_context_id: context_id,
                    value_type: PDataValueType::Command,
                    is_last: true,
                    data: command[split..].to_vec(),
                }],
            })
            .await
            .unwrap();
        for (last, data) in [(false, vec![1, 2, 3]), (true, vec![4, 5, 6])] {
            client
                .send(&Pdu::PData {
                    data: vec![PDataValue {
                        presentation_context_id: context_id,
                        value_type: PDataValueType::Data,
                        is_last: last,
                        data,
                    }],
                })
                .await
                .unwrap();
        }
        let response = client.receive().await.unwrap();
        let Pdu::PData { data } = response else {
            panic!("expected C-STORE-RSP")
        };
        let response = decode_command(&data[0].data).unwrap();
        assert_eq!(command_field(&response).unwrap(), C_STORE_RSP);
        assert_eq!(responded_message_id(&response).unwrap(), 7);
        assert_eq!(status(&response).unwrap(), 0x0000);
        client.release().await.unwrap();

        let path = temporary
            .path()
            .join("ACC-1/1.2.826.0.1.3680043.10.987.99.dcm");
        let bytes = std::fs::read(path).unwrap();
        assert_eq!(&bytes[128..132], b"DICM");
        assert!(bytes.ends_with(&[1, 2, 3, 4, 5, 6]));

        handle.shutdown().await.unwrap();
        let rebound = TcpListener::bind(address).await.unwrap();
        drop(rebound);
    }

    #[tokio::test]
    async fn non_storage_c_store_fails_without_writing() {
        let temporary = tempfile::tempdir().unwrap();
        let handle = StorageScpService::start(
            StorageScpConfig::new("127.0.0.1:0".parse().unwrap(), "DCMGET"),
            FileStore::new(),
            FixedResolver {
                root: temporary.path().to_path_buf(),
            },
        )
        .await
        .unwrap();
        let non_storage_sop = uids::STUDY_ROOT_QUERY_RETRIEVE_INFORMATION_MODEL_FIND;
        let mut client = dicom_ul::association::client::ClientAssociationOptions::new()
            .calling_ae_title("TESTSCU")
            .called_ae_title("DCMGET")
            .with_presentation_context(non_storage_sop, vec![uids::EXPLICIT_VR_LITTLE_ENDIAN])
            .establish_async(handle.local_address())
            .await
            .unwrap();
        let context_id = client.presentation_contexts()[0].id;
        let command = store_request_command(8, non_storage_sop, "1.2.826.0.1.3680043.10.987.102");
        client
            .send(&Pdu::PData {
                data: vec![
                    PDataValue {
                        presentation_context_id: context_id,
                        value_type: PDataValueType::Command,
                        is_last: true,
                        data: command,
                    },
                    PDataValue {
                        presentation_context_id: context_id,
                        value_type: PDataValueType::Data,
                        is_last: true,
                        data: vec![1, 2, 3],
                    },
                ],
            })
            .await
            .unwrap();
        let Pdu::PData { data } = client.receive().await.unwrap() else {
            panic!("expected C-STORE-RSP")
        };
        let response = decode_command(&data[0].data).unwrap();
        assert_eq!(command_field(&response).unwrap(), C_STORE_RSP);
        assert_eq!(responded_message_id(&response).unwrap(), 8);
        assert_eq!(status(&response).unwrap(), 0xC000);
        assert!(!temporary.path().join("ACC-1").exists());

        client.release().await.unwrap();
        handle.shutdown().await.unwrap();
    }

    #[tokio::test]
    async fn c_echo_command_can_be_fragmented() {
        let temporary = tempfile::tempdir().unwrap();
        let handle = StorageScpService::start(
            StorageScpConfig::new("127.0.0.1:0".parse().unwrap(), "DCMGET"),
            FileStore::new(),
            FixedResolver {
                root: temporary.path().to_path_buf(),
            },
        )
        .await
        .unwrap();
        let mut client = dicom_ul::association::client::ClientAssociationOptions::new()
            .calling_ae_title("TESTSCU")
            .called_ae_title("DCMGET")
            .with_abstract_syntax(uids::VERIFICATION)
            .establish_async(handle.local_address())
            .await
            .unwrap();
        let context_id = client.presentation_contexts()[0].id;
        let command = encode_command(&CommandObject::command_from_element_iter([
            DataElement::new(
                tags::AFFECTED_SOP_CLASS_UID,
                VR::UI,
                PrimitiveValue::from(uids::VERIFICATION),
            ),
            DataElement::new(tags::COMMAND_FIELD, VR::US, dicom_value!(U16, [C_ECHO_RQ])),
            DataElement::new(tags::MESSAGE_ID, VR::US, dicom_value!(U16, [3])),
            DataElement::new(
                tags::COMMAND_DATA_SET_TYPE,
                VR::US,
                dicom_value!(U16, [crate::dimse::NO_DATASET]),
            ),
        ]))
        .unwrap();
        let split = command.len() / 2;
        for (last, data) in [
            (false, command[..split].to_vec()),
            (true, command[split..].to_vec()),
        ] {
            client
                .send(&Pdu::PData {
                    data: vec![PDataValue {
                        presentation_context_id: context_id,
                        value_type: PDataValueType::Command,
                        is_last: last,
                        data,
                    }],
                })
                .await
                .unwrap();
        }
        let Pdu::PData { data } = client.receive().await.unwrap() else {
            panic!("expected C-ECHO-RSP")
        };
        let response = decode_command(&data[0].data).unwrap();
        assert_eq!(command_field(&response).unwrap(), crate::dimse::C_ECHO_RSP);
        assert_eq!(responded_message_id(&response).unwrap(), 3);
        assert_eq!(status(&response).unwrap(), 0);
        client.release().await.unwrap();
        handle.shutdown().await.unwrap();
    }
}
