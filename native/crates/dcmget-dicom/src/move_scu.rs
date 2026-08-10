use std::time::Duration;

use dicom_dictionary_std::uids;
use dicom_encoding::transfer_syntax::TransferSyntaxIndex;
use dicom_transfer_syntax_registry::TransferSyntaxRegistry;
use dicom_ul::Pdu;
use dicom_ul::association::client::ClientAssociationOptions;
use dicom_ul::pdu::{PDataValue, PDataValueType};

use crate::CancellationToken;
use crate::dimse::{
    C_MOVE_RSP, CommandAssembler, NO_DATASET, cancel_move_command, command_dataset_type,
    command_field, decode_command, encode_command, move_counters, move_query, move_request_command,
    responded_message_id, status,
};
use crate::model::{
    AssociationFailure, MoveAttemptResult, MoveCounters, MoveFinalStatus, MoveRequest,
    MoveStatusClass,
};

const STUDY_ROOT_MOVE_UID: &str = "1.2.840.10008.5.1.4.1.2.2.2";

/// Operational knobs which do not belong to a persisted task request.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StudyMoveScuConfig {
    pub message_id: u16,
    pub maximum_pdu_length: u32,
    pub cancel_grace: Duration,
}

impl Default for StudyMoveScuConfig {
    fn default() -> Self {
        Self {
            message_id: 1,
            maximum_pdu_length: 64 * 1024,
            cancel_grace: Duration::from_secs(2),
        }
    }
}

/// Native Study Root C-MOVE SCU backed by dicom-rs 0.10.
#[derive(Debug, Clone, Copy, Default)]
pub struct StudyMoveScu {
    config: StudyMoveScuConfig,
}

impl StudyMoveScu {
    #[must_use]
    pub fn new(config: StudyMoveScuConfig) -> Self {
        Self { config }
    }

    /// Execute a single Study Root C-MOVE by Accession Number.
    ///
    /// Local receive counters remain zero here by design. `ApplicationService`
    /// merges them from `StorageScpEvent`s so a PACS response is never treated
    /// as proof that files reached disk.
    #[allow(clippy::too_many_lines)] // linear association lifecycle is clearer in one owner
    pub async fn execute(
        &self,
        request: &MoveRequest,
        cancellation: &CancellationToken,
    ) -> MoveAttemptResult {
        let mut result = empty_result();
        if cancellation.is_cancelled() {
            result.cancel_requested = true;
            return result;
        }

        let options = ClientAssociationOptions::new()
            .calling_ae_title(&request.calling_ae_title)
            .called_ae_title(&request.pacs_ae_title)
            .with_abstract_syntax(STUDY_ROOT_MOVE_UID)
            .max_pdu_length(self.config.maximum_pdu_length)
            .connection_timeout(request.association_timeout)
            .read_timeout(request.dimse_timeout)
            .write_timeout(request.dimse_timeout);

        let establish = options.establish_async((request.pacs.host.as_str(), request.pacs.port));
        let mut association =
            match tokio::time::timeout(request.association_timeout, establish).await {
                Err(_) => {
                    result.association_failure = Some(AssociationFailure::ConnectTimeout);
                    return result;
                }
                Ok(Err(error)) => {
                    result.association_failure = Some(AssociationFailure::Transport {
                        message: error.to_string(),
                    });
                    return result;
                }
                Ok(Ok(association)) => association,
            };

        let Some(context) = association
            .presentation_contexts()
            .iter()
            .find(|context| context.abstract_syntax == STUDY_ROOT_MOVE_UID)
            .cloned()
        else {
            result.association_failure = Some(AssociationFailure::Rejected {
                reason: "PACS did not accept Study Root C-MOVE".to_owned(),
            });
            let _ = association.abort().await;
            return result;
        };

        let Some(query_transfer_syntax) = TransferSyntaxRegistry.get(&context.transfer_syntax)
        else {
            result.association_failure = Some(AssociationFailure::Protocol {
                message: format!(
                    "PACS selected an unregistered query transfer syntax: {}",
                    context.transfer_syntax
                ),
            });
            let _ = association.abort().await;
            return result;
        };

        let command = match encode_command(&move_request_command(
            uids::STUDY_ROOT_QUERY_RETRIEVE_INFORMATION_MODEL_MOVE,
            &request.storage_ae_title,
            self.config.message_id,
        )) {
            Ok(command) => command,
            Err(error) => {
                result.association_failure = Some(protocol_failure(error));
                let _ = association.abort().await;
                return result;
            }
        };
        let mut query = Vec::with_capacity(128);
        if let Err(error) = move_query(&request.accession_number)
            .write_dataset_with_ts(&mut query, query_transfer_syntax)
        {
            result.association_failure = Some(AssociationFailure::Protocol {
                message: format!("failed to encode C-MOVE identifier: {error}"),
            });
            let _ = association.abort().await;
            return result;
        }

        if let Err(error) = association
            .send(&Pdu::PData {
                data: vec![PDataValue {
                    presentation_context_id: context.id,
                    value_type: PDataValueType::Command,
                    is_last: true,
                    data: command,
                }],
            })
            .await
        {
            result.association_failure = Some(transport_failure(error));
            let _ = association.abort().await;
            return result;
        }
        if let Err(error) = association
            .send(&Pdu::PData {
                data: vec![PDataValue {
                    presentation_context_id: context.id,
                    value_type: PDataValueType::Data,
                    is_last: true,
                    data: query,
                }],
            })
            .await
        {
            result.association_failure = Some(transport_failure(error));
            let _ = association.abort().await;
            return result;
        }

        let mut assembler = CommandAssembler::default();
        let mut awaiting_final_dataset = false;
        loop {
            tokio::select! {
                () = cancellation.cancelled() => {
                    result.cancel_requested = true;
                    return self.cancel_or_abort(
                        association,
                        context.id,
                        &mut assembler,
                        &mut awaiting_final_dataset,
                        result,
                    ).await;
                }
                response = tokio::time::timeout(request.dimse_timeout, association.receive()) => {
                    match response {
                        Err(_) => {
                            result.association_failure = Some(AssociationFailure::DimseTimeout);
                            let _ = association.abort().await;
                            return result;
                        }
                        Ok(Err(error)) => {
                            result.association_failure = Some(transport_failure(error));
                            let _ = association.abort().await;
                            return result;
                        }
                        Ok(Ok(pdu)) => match consume_move_pdu(
                            pdu,
                            context.id,
                            self.config.message_id,
                            &mut assembler,
                            &mut awaiting_final_dataset,
                            &mut result,
                        ) {
                            Ok(MoveResponseProgress::Pending | MoveResponseProgress::NoCommand | MoveResponseProgress::AwaitingFinalDataset) => {}
                            Ok(MoveResponseProgress::Final) => {
                                let _ = association.release().await;
                                return result;
                            }
                            Err(failure) => {
                                result.association_failure = Some(failure);
                                let _ = association.abort().await;
                                return result;
                            }
                        }
                    }
                }
            }
        }
    }

    async fn cancel_or_abort(
        &self,
        mut association: dicom_ul::association::client::AsyncClientAssociation<
            tokio::net::TcpStream,
        >,
        presentation_context_id: u8,
        assembler: &mut CommandAssembler,
        awaiting_final_dataset: &mut bool,
        mut result: MoveAttemptResult,
    ) -> MoveAttemptResult {
        let cancel = match encode_command(&cancel_move_command(self.config.message_id)) {
            Ok(cancel) => cancel,
            Err(error) => {
                result.association_failure = Some(protocol_failure(error));
                abort_with_timeout(association, self.config.cancel_grace).await;
                return result;
            }
        };
        let cancel_pdu = Pdu::PData {
            data: vec![PDataValue {
                presentation_context_id,
                value_type: PDataValueType::Command,
                is_last: true,
                data: cancel,
            }],
        };
        let cancel_send = association.send(&cancel_pdu);
        match tokio::time::timeout(self.config.cancel_grace, cancel_send).await {
            Err(_) => {
                result.association_failure = Some(AssociationFailure::CancelledAfterAbort);
                abort_with_timeout(association, self.config.cancel_grace).await;
                return result;
            }
            Ok(Err(error)) => {
                result.association_failure = Some(transport_failure(error));
                abort_with_timeout(association, self.config.cancel_grace).await;
                return result;
            }
            Ok(Ok(())) => {}
        }

        let deadline = tokio::time::Instant::now() + self.config.cancel_grace;
        loop {
            let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
            if remaining.is_zero() {
                result.association_failure = Some(AssociationFailure::CancelledAfterAbort);
                abort_with_timeout(association, self.config.cancel_grace).await;
                return result;
            }
            match tokio::time::timeout(remaining, association.receive()).await {
                Err(_) => {
                    result.association_failure = Some(AssociationFailure::CancelledAfterAbort);
                    abort_with_timeout(association, self.config.cancel_grace).await;
                    return result;
                }
                Ok(Err(error)) => {
                    result.association_failure = Some(transport_failure(error));
                    abort_with_timeout(association, self.config.cancel_grace).await;
                    return result;
                }
                Ok(Ok(pdu)) => match consume_move_pdu(
                    pdu,
                    presentation_context_id,
                    self.config.message_id,
                    assembler,
                    awaiting_final_dataset,
                    &mut result,
                ) {
                    Ok(MoveResponseProgress::Final) => {
                        let _ = association.release().await;
                        return result;
                    }
                    Ok(
                        MoveResponseProgress::Pending
                        | MoveResponseProgress::NoCommand
                        | MoveResponseProgress::AwaitingFinalDataset,
                    ) => {}
                    Err(failure) => {
                        result.association_failure = Some(failure);
                        abort_with_timeout(association, self.config.cancel_grace).await;
                        return result;
                    }
                },
            }
        }
    }
}

async fn abort_with_timeout(
    association: dicom_ul::association::client::AsyncClientAssociation<tokio::net::TcpStream>,
    timeout: Duration,
) {
    let _ = tokio::time::timeout(timeout, association.abort()).await;
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum MoveResponseProgress {
    Pending,
    Final,
    AwaitingFinalDataset,
    NoCommand,
}

fn consume_move_pdu(
    pdu: Pdu,
    expected_context_id: u8,
    expected_message_id: u16,
    assembler: &mut CommandAssembler,
    awaiting_final_dataset: &mut bool,
    result: &mut MoveAttemptResult,
) -> Result<MoveResponseProgress, AssociationFailure> {
    let Pdu::PData { data } = pdu else {
        return Err(AssociationFailure::Protocol {
            message: "PACS sent a non-P-DATA PDU while C-MOVE was active".to_owned(),
        });
    };
    let mut progress = MoveResponseProgress::NoCommand;
    for value in data {
        if value.value_type == PDataValueType::Data {
            // A failed C-MOVE may include an identifier containing failed SOP
            // UIDs. It is not needed for counters and is intentionally bounded
            // by dicom-ul's negotiated PDU size rather than accumulated here.
            if *awaiting_final_dataset && value.is_last {
                *awaiting_final_dataset = false;
                progress = MoveResponseProgress::Final;
            }
            continue;
        }
        if *awaiting_final_dataset {
            return Err(AssociationFailure::Protocol {
                message: "PACS sent a new command before the final C-MOVE identifier ended"
                    .to_owned(),
            });
        }
        let completed = assembler.push(&value).map_err(protocol_failure)?;
        let Some(completed) = completed else {
            continue;
        };
        if completed.presentation_context_id != expected_context_id {
            return Err(AssociationFailure::Protocol {
                message: format!(
                    "C-MOVE response used presentation context {}, expected {expected_context_id}",
                    completed.presentation_context_id
                ),
            });
        }
        let command = decode_command(&completed.bytes).map_err(protocol_failure)?;
        if command_field(&command).map_err(protocol_failure)? != C_MOVE_RSP {
            return Err(AssociationFailure::Protocol {
                message: "PACS returned a command other than C-MOVE-RSP".to_owned(),
            });
        }
        if responded_message_id(&command).map_err(protocol_failure)? != expected_message_id {
            return Err(AssociationFailure::Protocol {
                message: "C-MOVE-RSP referred to a different Message ID".to_owned(),
            });
        }
        let code = status(&command).map_err(protocol_failure)?;
        result.counters = merge_counters(result.counters, move_counters(&command));
        let final_status = MoveFinalStatus::from_code(code);
        if final_status.class == MoveStatusClass::Pending {
            result.pending_responses = result.pending_responses.saturating_add(1);
            progress = MoveResponseProgress::Pending;
        } else {
            result.final_status = Some(final_status);
            if command_dataset_type(&command).map_err(protocol_failure)? == NO_DATASET {
                progress = MoveResponseProgress::Final;
            } else {
                *awaiting_final_dataset = true;
                progress = MoveResponseProgress::AwaitingFinalDataset;
            }
        }
    }
    Ok(progress)
}

fn merge_counters(previous: MoveCounters, current: MoveCounters) -> MoveCounters {
    MoveCounters {
        remaining: current.remaining.or(previous.remaining),
        completed: current.completed.or(previous.completed),
        failed: current.failed.or(previous.failed),
        warning: current.warning.or(previous.warning),
    }
}

fn empty_result() -> MoveAttemptResult {
    MoveAttemptResult {
        final_status: None,
        counters: MoveCounters::default(),
        pending_responses: 0,
        locally_received_operations: 0,
        locally_unique_sop_instances: 0,
        association_failure: None,
        cancel_requested: false,
    }
}

fn protocol_failure(error: impl std::fmt::Display) -> AssociationFailure {
    AssociationFailure::Protocol {
        message: error.to_string(),
    }
}

fn transport_failure(error: impl std::fmt::Display) -> AssociationFailure {
    AssociationFailure::Transport {
        message: error.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use dicom_core::{DataElement, VR, dicom_value};
    use dicom_dictionary_std::tags;
    use dicom_ul::association::server::{AsyncServerAssociation, ServerAssociationOptions};
    use tokio::net::{TcpListener, TcpStream};
    use tokio::sync::oneshot;

    use super::*;
    use crate::dimse::{CommandObject, HAS_DATASET, NO_DATASET, message_id, required_text};

    fn move_response(status_code: u16, remaining: Option<u16>, completed: u16) -> Vec<u8> {
        move_response_with_dataset_type(status_code, remaining, completed, NO_DATASET)
    }

    fn move_response_with_dataset_type(
        status_code: u16,
        remaining: Option<u16>,
        completed: u16,
        dataset_type: u16,
    ) -> Vec<u8> {
        let mut elements = vec![
            DataElement::new(tags::COMMAND_FIELD, VR::US, dicom_value!(U16, [C_MOVE_RSP])),
            DataElement::new(
                tags::MESSAGE_ID_BEING_RESPONDED_TO,
                VR::US,
                dicom_value!(U16, [1]),
            ),
            DataElement::new(
                tags::COMMAND_DATA_SET_TYPE,
                VR::US,
                dicom_value!(U16, [dataset_type]),
            ),
            DataElement::new(tags::STATUS, VR::US, dicom_value!(U16, [status_code])),
            DataElement::new(
                tags::NUMBER_OF_COMPLETED_SUBOPERATIONS,
                VR::US,
                dicom_value!(U16, [completed]),
            ),
        ];
        if let Some(remaining) = remaining {
            elements.push(DataElement::new(
                tags::NUMBER_OF_REMAINING_SUBOPERATIONS,
                VR::US,
                dicom_value!(U16, [remaining]),
            ));
        }
        encode_command(&CommandObject::command_from_element_iter(elements)).unwrap()
    }

    async fn receive_move_request(
        association: &mut AsyncServerAssociation<TcpStream>,
    ) -> (u8, CommandObject, CommandObject) {
        let context = association.presentation_contexts()[0].clone();
        let mut command_assembler = CommandAssembler::default();
        let mut request_command = None;
        let mut identifier_bytes = Vec::new();
        while request_command.is_none() || identifier_bytes.is_empty() {
            let Pdu::PData { data } = association.receive().await.unwrap() else {
                panic!("expected C-MOVE request P-DATA")
            };
            for value in data {
                match value.value_type {
                    PDataValueType::Command => {
                        if let Some(completed) = command_assembler.push(&value).unwrap() {
                            request_command = Some(decode_command(&completed.bytes).unwrap());
                        }
                    }
                    PDataValueType::Data => identifier_bytes.extend_from_slice(&value.data),
                }
            }
        }
        let transfer_syntax = TransferSyntaxRegistry
            .get(&context.transfer_syntax)
            .expect("negotiated query transfer syntax");
        let identifier =
            CommandObject::read_dataset_with_ts(identifier_bytes.as_slice(), transfer_syntax)
                .unwrap();
        (context.id, request_command.unwrap(), identifier)
    }

    async fn send_fragmented_command(
        association: &mut AsyncServerAssociation<TcpStream>,
        context_id: u8,
        command: Vec<u8>,
    ) {
        let split = command.len() / 2;
        for (is_last, data) in [
            (false, command[..split].to_vec()),
            (true, command[split..].to_vec()),
        ] {
            association
                .send(&Pdu::PData {
                    data: vec![PDataValue {
                        presentation_context_id: context_id,
                        value_type: PDataValueType::Command,
                        is_last,
                        data,
                    }],
                })
                .await
                .unwrap();
        }
    }

    async fn accept_move_association(listener: &TcpListener) -> AsyncServerAssociation<TcpStream> {
        let (stream, _) = listener.accept().await.unwrap();
        ServerAssociationOptions::new()
            .accept_called_ae_title()
            .ae_title("PACS")
            .with_abstract_syntax(STUDY_ROOT_MOVE_UID)
            .establish_async(stream)
            .await
            .unwrap()
    }

    async fn complete_release(association: &mut AsyncServerAssociation<TcpStream>) {
        assert_eq!(association.receive().await.unwrap(), Pdu::ReleaseRQ);
        association.send(&Pdu::ReleaseRP).await.unwrap();
    }

    #[test]
    fn fragmented_pending_and_final_responses_update_counters() {
        let pending = move_response(0xFF00, Some(4), 2);
        let final_response = move_response(0x0000, Some(0), 6);
        let split = pending.len() / 2;
        let mut assembler = CommandAssembler::default();
        let mut awaiting_final_dataset = false;
        let mut result = empty_result();

        assert_eq!(
            consume_move_pdu(
                Pdu::PData {
                    data: vec![PDataValue {
                        presentation_context_id: 1,
                        value_type: PDataValueType::Command,
                        is_last: false,
                        data: pending[..split].to_vec(),
                    }],
                },
                1,
                1,
                &mut assembler,
                &mut awaiting_final_dataset,
                &mut result,
            )
            .unwrap(),
            MoveResponseProgress::NoCommand
        );
        assert_eq!(
            consume_move_pdu(
                Pdu::PData {
                    data: vec![
                        PDataValue {
                            presentation_context_id: 1,
                            value_type: PDataValueType::Command,
                            is_last: true,
                            data: pending[split..].to_vec(),
                        },
                        PDataValue {
                            presentation_context_id: 1,
                            value_type: PDataValueType::Data,
                            is_last: true,
                            data: vec![1, 2, 3],
                        },
                    ],
                },
                1,
                1,
                &mut assembler,
                &mut awaiting_final_dataset,
                &mut result,
            )
            .unwrap(),
            MoveResponseProgress::Pending
        );
        assert_eq!(result.pending_responses, 1);
        assert_eq!(result.counters.remaining, Some(4));
        assert_eq!(
            consume_move_pdu(
                Pdu::PData {
                    data: vec![PDataValue {
                        presentation_context_id: 1,
                        value_type: PDataValueType::Command,
                        is_last: true,
                        data: final_response,
                    }],
                },
                1,
                1,
                &mut assembler,
                &mut awaiting_final_dataset,
                &mut result,
            )
            .unwrap(),
            MoveResponseProgress::Final
        );
        assert_eq!(result.counters.completed, Some(6));
        assert_eq!(result.final_status.unwrap().class, MoveStatusClass::Success);
    }

    #[test]
    fn move_request_uses_dataset_and_cancel_does_not() {
        let request = move_request_command(STUDY_ROOT_MOVE_UID, "DCMGET", 1);
        assert_eq!(
            request
                .element(tags::COMMAND_DATA_SET_TYPE)
                .unwrap()
                .uint16()
                .unwrap(),
            HAS_DATASET
        );
        let cancel = cancel_move_command(1);
        assert_eq!(
            cancel
                .element(tags::COMMAND_DATA_SET_TYPE)
                .unwrap()
                .uint16()
                .unwrap(),
            NO_DATASET
        );
    }

    #[test]
    fn final_response_identifier_is_drained_before_completion() {
        let final_response = move_response_with_dataset_type(0xB000, Some(0), 4, HAS_DATASET);
        let mut assembler = CommandAssembler::default();
        let mut awaiting_final_dataset = false;
        let mut result = empty_result();
        assert_eq!(
            consume_move_pdu(
                Pdu::PData {
                    data: vec![PDataValue {
                        presentation_context_id: 1,
                        value_type: PDataValueType::Command,
                        is_last: true,
                        data: final_response,
                    }],
                },
                1,
                1,
                &mut assembler,
                &mut awaiting_final_dataset,
                &mut result,
            )
            .unwrap(),
            MoveResponseProgress::AwaitingFinalDataset
        );
        assert!(awaiting_final_dataset);
        assert_eq!(
            consume_move_pdu(
                Pdu::PData {
                    data: vec![PDataValue {
                        presentation_context_id: 1,
                        value_type: PDataValueType::Data,
                        is_last: true,
                        data: vec![0, 0],
                    }],
                },
                1,
                1,
                &mut assembler,
                &mut awaiting_final_dataset,
                &mut result,
            )
            .unwrap(),
            MoveResponseProgress::Final
        );
        assert!(!awaiting_final_dataset);
        assert_eq!(result.final_status.unwrap().class, MoveStatusClass::Warning);
    }

    #[tokio::test]
    async fn loopback_move_reads_fragmented_pending_and_final_counters() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let mut association = accept_move_association(&listener).await;
            let (context_id, command, identifier) = receive_move_request(&mut association).await;
            assert_eq!(command_field(&command).unwrap(), crate::dimse::C_MOVE_RQ);
            assert_eq!(message_id(&command).unwrap(), 1);
            assert_eq!(
                required_text(&identifier, tags::QUERY_RETRIEVE_LEVEL, "level").unwrap(),
                "STUDY"
            );
            assert_eq!(
                required_text(&identifier, tags::ACCESSION_NUMBER, "accession").unwrap(),
                "ACC-LOOPBACK"
            );
            send_fragmented_command(
                &mut association,
                context_id,
                move_response(0xFF00, Some(3), 2),
            )
            .await;
            send_fragmented_command(
                &mut association,
                context_id,
                move_response(0x0000, Some(0), 5),
            )
            .await;
            complete_release(&mut association).await;
        });

        let mut request = MoveRequest::study_by_accession(
            "profile-1",
            "task-1",
            "ACC-LOOPBACK",
            crate::DicomEndpoint {
                host: address.ip().to_string(),
                port: address.port(),
            },
            "DCMGET",
            "PACS",
            "STORESCP",
        );
        request.dimse_timeout = Duration::from_secs(2);
        let result = tokio::time::timeout(
            Duration::from_secs(3),
            StudyMoveScu::default().execute(&request, &CancellationToken::new()),
        )
        .await
        .expect("loopback C-MOVE timed out");
        assert_eq!(result.pending_responses, 1);
        assert_eq!(result.counters.completed, Some(5));
        assert_eq!(result.counters.remaining, Some(0));
        assert_eq!(result.final_status.unwrap().class, MoveStatusClass::Success);
        assert!(result.association_failure.is_none());
        tokio::time::timeout(Duration::from_secs(3), server)
            .await
            .expect("loopback PACS did not stop")
            .unwrap();
    }

    #[tokio::test]
    async fn loopback_move_sends_cancel_and_accepts_cancel_final_status() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let (request_seen, request_seen_receiver) = oneshot::channel();
        let server = tokio::spawn(async move {
            let mut association = accept_move_association(&listener).await;
            let (context_id, _, _) = receive_move_request(&mut association).await;
            request_seen.send(()).unwrap();
            let Pdu::PData { data } = association.receive().await.unwrap() else {
                panic!("expected C-CANCEL-RQ")
            };
            let cancel = decode_command(&data[0].data).unwrap();
            assert_eq!(command_field(&cancel).unwrap(), crate::dimse::C_CANCEL_RQ);
            assert_eq!(responded_message_id(&cancel).unwrap(), 1);
            send_fragmented_command(
                &mut association,
                context_id,
                move_response(0xFE00, Some(2), 3),
            )
            .await;
            complete_release(&mut association).await;
        });

        let mut request = MoveRequest::study_by_accession(
            "profile-1",
            "task-1",
            "ACC-CANCEL",
            crate::DicomEndpoint {
                host: address.ip().to_string(),
                port: address.port(),
            },
            "DCMGET",
            "PACS",
            "STORESCP",
        );
        request.dimse_timeout = Duration::from_secs(2);
        let cancellation = CancellationToken::new();
        let cancellation_sender = cancellation.clone();
        tokio::spawn(async move {
            request_seen_receiver.await.unwrap();
            cancellation_sender.cancel();
        });
        let result = tokio::time::timeout(
            Duration::from_secs(3),
            StudyMoveScu::default().execute(&request, &cancellation),
        )
        .await
        .expect("cancelled loopback C-MOVE timed out");
        assert!(result.cancel_requested);
        assert_eq!(
            result.final_status.unwrap().class,
            MoveStatusClass::Cancelled
        );
        assert!(result.association_failure.is_none());
        tokio::time::timeout(Duration::from_secs(3), server)
            .await
            .expect("cancel loopback PACS did not stop")
            .unwrap();
    }

    #[tokio::test]
    async fn cancellation_aborts_when_pacs_does_not_send_a_final_response() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let (request_seen, request_seen_receiver) = oneshot::channel();
        let server = tokio::spawn(async move {
            let mut association = accept_move_association(&listener).await;
            receive_move_request(&mut association).await;
            request_seen.send(()).unwrap();
            let Pdu::PData { data } = association.receive().await.unwrap() else {
                panic!("expected C-CANCEL-RQ")
            };
            let cancel = decode_command(&data[0].data).unwrap();
            assert_eq!(command_field(&cancel).unwrap(), crate::dimse::C_CANCEL_RQ);
            assert!(matches!(
                association.receive().await.unwrap(),
                Pdu::AbortRQ { .. }
            ));
        });

        let mut request = MoveRequest::study_by_accession(
            "profile-1",
            "task-1",
            "ACC-ABORT",
            crate::DicomEndpoint {
                host: address.ip().to_string(),
                port: address.port(),
            },
            "DCMGET",
            "PACS",
            "STORESCP",
        );
        request.dimse_timeout = Duration::from_secs(2);
        let cancellation = CancellationToken::new();
        let cancellation_sender = cancellation.clone();
        tokio::spawn(async move {
            request_seen_receiver.await.unwrap();
            cancellation_sender.cancel();
        });
        let result = StudyMoveScu::new(StudyMoveScuConfig {
            cancel_grace: Duration::from_millis(25),
            ..StudyMoveScuConfig::default()
        })
        .execute(&request, &cancellation)
        .await;
        assert!(result.cancel_requested);
        assert_eq!(
            result.association_failure,
            Some(AssociationFailure::CancelledAfterAbort)
        );
        tokio::time::timeout(Duration::from_secs(3), server)
            .await
            .expect("abort loopback PACS did not stop")
            .unwrap();
    }
}
