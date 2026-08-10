use dicom_core::{DataElement, PrimitiveValue, VR, dicom_value};
use dicom_dictionary_std::{StandardDataDictionary, tags};
use dicom_object::InMemDicomObject;
use dicom_transfer_syntax_registry::entries;
use dicom_ul::pdu::{PDataValue, PDataValueType};

use crate::model::MoveCounters;

pub(crate) const C_STORE_RQ: u16 = 0x0001;
pub(crate) const C_STORE_RSP: u16 = 0x8001;
pub(crate) const C_MOVE_RQ: u16 = 0x0021;
pub(crate) const C_MOVE_RSP: u16 = 0x8021;
pub(crate) const C_ECHO_RQ: u16 = 0x0030;
pub(crate) const C_ECHO_RSP: u16 = 0x8030;
pub(crate) const C_CANCEL_RQ: u16 = 0x0FFF;
pub(crate) const NO_DATASET: u16 = 0x0101;
pub(crate) const HAS_DATASET: u16 = 0x0001;
const MAX_COMMAND_BYTES: usize = 1024 * 1024;

pub(crate) type CommandObject = InMemDicomObject<StandardDataDictionary>;

#[derive(Debug, thiserror::Error)]
pub(crate) enum DimseError {
    #[error("DIMSE command exceeded {MAX_COMMAND_BYTES} bytes")]
    CommandTooLarge,
    #[error("fragmented DIMSE command changed presentation context from {expected} to {actual}")]
    MixedPresentationContext { expected: u8, actual: u8 },
    #[error("failed to decode DIMSE command: {0}")]
    Decode(String),
    #[error("failed to encode DIMSE command: {0}")]
    Encode(String),
    #[error("DIMSE command is missing {field}")]
    MissingField { field: &'static str },
    #[error("DIMSE command field {field} has an invalid value: {message}")]
    InvalidField {
        field: &'static str,
        message: String,
    },
}

#[derive(Debug, Default)]
pub(crate) struct CommandAssembler {
    presentation_context_id: Option<u8>,
    bytes: Vec<u8>,
}

#[derive(Debug)]
pub(crate) struct AssembledCommand {
    pub presentation_context_id: u8,
    pub bytes: Vec<u8>,
}

impl CommandAssembler {
    pub fn push(&mut self, value: &PDataValue) -> Result<Option<AssembledCommand>, DimseError> {
        debug_assert_eq!(value.value_type, PDataValueType::Command);
        match self.presentation_context_id {
            Some(expected) if expected != value.presentation_context_id => {
                return Err(DimseError::MixedPresentationContext {
                    expected,
                    actual: value.presentation_context_id,
                });
            }
            None => self.presentation_context_id = Some(value.presentation_context_id),
            Some(_) => {}
        }
        if self.bytes.len().saturating_add(value.data.len()) > MAX_COMMAND_BYTES {
            self.reset();
            return Err(DimseError::CommandTooLarge);
        }
        self.bytes.extend_from_slice(&value.data);
        if !value.is_last {
            return Ok(None);
        }
        let presentation_context_id = self
            .presentation_context_id
            .take()
            .expect("a pushed command always sets a presentation context");
        Ok(Some(AssembledCommand {
            presentation_context_id,
            bytes: std::mem::take(&mut self.bytes),
        }))
    }

    pub fn is_partial(&self) -> bool {
        self.presentation_context_id.is_some()
    }

    pub fn reset(&mut self) {
        self.presentation_context_id = None;
        self.bytes.clear();
    }
}

pub(crate) fn encode_command(command: &CommandObject) -> Result<Vec<u8>, DimseError> {
    let transfer_syntax = entries::IMPLICIT_VR_LITTLE_ENDIAN.erased();
    let mut bytes = Vec::with_capacity(256);
    command
        .write_dataset_with_ts(&mut bytes, &transfer_syntax)
        .map_err(|error| DimseError::Encode(error.to_string()))?;
    Ok(bytes)
}

pub(crate) fn decode_command(bytes: &[u8]) -> Result<CommandObject, DimseError> {
    let transfer_syntax = entries::IMPLICIT_VR_LITTLE_ENDIAN.erased();
    CommandObject::read_dataset_with_ts(bytes, &transfer_syntax)
        .map_err(|error| DimseError::Decode(error.to_string()))
}

pub(crate) fn command_field(command: &CommandObject) -> Result<u16, DimseError> {
    required_u16(command, tags::COMMAND_FIELD, "Command Field")
}

pub(crate) fn command_dataset_type(command: &CommandObject) -> Result<u16, DimseError> {
    required_u16(
        command,
        tags::COMMAND_DATA_SET_TYPE,
        "Command Data Set Type",
    )
}

pub(crate) fn message_id(command: &CommandObject) -> Result<u16, DimseError> {
    required_u16(command, tags::MESSAGE_ID, "Message ID")
}

pub(crate) fn responded_message_id(command: &CommandObject) -> Result<u16, DimseError> {
    required_u16(
        command,
        tags::MESSAGE_ID_BEING_RESPONDED_TO,
        "Message ID Being Responded To",
    )
}

pub(crate) fn status(command: &CommandObject) -> Result<u16, DimseError> {
    required_u16(command, tags::STATUS, "Status")
}

pub(crate) fn required_text(
    command: &CommandObject,
    tag: dicom_core::Tag,
    field: &'static str,
) -> Result<String, DimseError> {
    command
        .element(tag)
        .map_err(|_| DimseError::MissingField { field })?
        .to_str()
        .map(|value| value.trim_matches(['\0', ' ']).to_owned())
        .map_err(|error| DimseError::InvalidField {
            field,
            message: error.to_string(),
        })
}

fn required_u16(
    command: &CommandObject,
    tag: dicom_core::Tag,
    field: &'static str,
) -> Result<u16, DimseError> {
    command
        .element(tag)
        .map_err(|_| DimseError::MissingField { field })?
        .uint16()
        .map_err(|error| DimseError::InvalidField {
            field,
            message: error.to_string(),
        })
}

fn optional_u16(command: &CommandObject, tag: dicom_core::Tag) -> Option<u16> {
    command.element(tag).ok()?.uint16().ok()
}

pub(crate) fn move_counters(command: &CommandObject) -> MoveCounters {
    MoveCounters {
        remaining: optional_u16(command, tags::NUMBER_OF_REMAINING_SUBOPERATIONS).map(u32::from),
        completed: optional_u16(command, tags::NUMBER_OF_COMPLETED_SUBOPERATIONS).map(u32::from),
        failed: optional_u16(command, tags::NUMBER_OF_FAILED_SUBOPERATIONS).map(u32::from),
        warning: optional_u16(command, tags::NUMBER_OF_WARNING_SUBOPERATIONS).map(u32::from),
    }
}

pub(crate) fn move_request_command(
    sop_class_uid: &str,
    move_destination: &str,
    message_id: u16,
) -> CommandObject {
    CommandObject::command_from_element_iter([
        DataElement::new(
            tags::AFFECTED_SOP_CLASS_UID,
            VR::UI,
            PrimitiveValue::from(sop_class_uid),
        ),
        DataElement::new(tags::COMMAND_FIELD, VR::US, dicom_value!(U16, [C_MOVE_RQ])),
        DataElement::new(tags::MESSAGE_ID, VR::US, dicom_value!(U16, [message_id])),
        DataElement::new(tags::PRIORITY, VR::US, dicom_value!(U16, [0x0000])),
        DataElement::new(
            tags::COMMAND_DATA_SET_TYPE,
            VR::US,
            dicom_value!(U16, [HAS_DATASET]),
        ),
        DataElement::new(
            tags::MOVE_DESTINATION,
            VR::AE,
            PrimitiveValue::from(move_destination),
        ),
    ])
}

pub(crate) fn move_query(accession_number: &str) -> CommandObject {
    let mut query = CommandObject::new_empty();
    query.put(DataElement::new(
        tags::QUERY_RETRIEVE_LEVEL,
        VR::CS,
        PrimitiveValue::from("STUDY"),
    ));
    query.put(DataElement::new(
        tags::ACCESSION_NUMBER,
        VR::SH,
        PrimitiveValue::from(accession_number),
    ));
    query
}

pub(crate) fn cancel_move_command(message_id: u16) -> CommandObject {
    CommandObject::command_from_element_iter([
        DataElement::new(
            tags::COMMAND_FIELD,
            VR::US,
            dicom_value!(U16, [C_CANCEL_RQ]),
        ),
        DataElement::new(
            tags::MESSAGE_ID_BEING_RESPONDED_TO,
            VR::US,
            dicom_value!(U16, [message_id]),
        ),
        DataElement::new(
            tags::COMMAND_DATA_SET_TYPE,
            VR::US,
            dicom_value!(U16, [NO_DATASET]),
        ),
    ])
}

pub(crate) fn echo_response(message_id: u16) -> CommandObject {
    CommandObject::command_from_element_iter([
        DataElement::new(
            tags::AFFECTED_SOP_CLASS_UID,
            VR::UI,
            PrimitiveValue::from(dicom_dictionary_std::uids::VERIFICATION),
        ),
        DataElement::new(tags::COMMAND_FIELD, VR::US, dicom_value!(U16, [C_ECHO_RSP])),
        DataElement::new(
            tags::MESSAGE_ID_BEING_RESPONDED_TO,
            VR::US,
            dicom_value!(U16, [message_id]),
        ),
        DataElement::new(
            tags::COMMAND_DATA_SET_TYPE,
            VR::US,
            dicom_value!(U16, [NO_DATASET]),
        ),
        DataElement::new(tags::STATUS, VR::US, dicom_value!(U16, [0x0000])),
    ])
}

pub(crate) fn store_response(
    message_id: u16,
    sop_class_uid: &str,
    sop_instance_uid: &str,
    response_status: u16,
) -> CommandObject {
    CommandObject::command_from_element_iter([
        DataElement::new(
            tags::AFFECTED_SOP_CLASS_UID,
            VR::UI,
            PrimitiveValue::from(sop_class_uid),
        ),
        DataElement::new(
            tags::COMMAND_FIELD,
            VR::US,
            dicom_value!(U16, [C_STORE_RSP]),
        ),
        DataElement::new(
            tags::MESSAGE_ID_BEING_RESPONDED_TO,
            VR::US,
            dicom_value!(U16, [message_id]),
        ),
        DataElement::new(
            tags::COMMAND_DATA_SET_TYPE,
            VR::US,
            dicom_value!(U16, [NO_DATASET]),
        ),
        DataElement::new(tags::STATUS, VR::US, dicom_value!(U16, [response_status])),
        DataElement::new(
            tags::AFFECTED_SOP_INSTANCE_UID,
            VR::UI,
            PrimitiveValue::from(sop_instance_uid),
        ),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fragmented_command_is_reassembled() {
        let encoded = encode_command(&echo_response(9)).expect("encode command");
        let split = encoded.len() / 2;
        let mut assembler = CommandAssembler::default();
        assert!(
            assembler
                .push(&PDataValue {
                    presentation_context_id: 1,
                    value_type: PDataValueType::Command,
                    is_last: false,
                    data: encoded[..split].to_vec(),
                })
                .expect("first fragment")
                .is_none()
        );
        let completed = assembler
            .push(&PDataValue {
                presentation_context_id: 1,
                value_type: PDataValueType::Command,
                is_last: true,
                data: encoded[split..].to_vec(),
            })
            .expect("last fragment")
            .expect("complete command");
        assert_eq!(completed.presentation_context_id, 1);
        assert_eq!(
            command_field(&decode_command(&completed.bytes).unwrap()).unwrap(),
            C_ECHO_RSP
        );
    }

    #[test]
    fn fragmented_command_cannot_change_context() {
        let mut assembler = CommandAssembler::default();
        assembler
            .push(&PDataValue {
                presentation_context_id: 1,
                value_type: PDataValueType::Command,
                is_last: false,
                data: vec![1],
            })
            .unwrap();
        assert!(matches!(
            assembler.push(&PDataValue {
                presentation_context_id: 3,
                value_type: PDataValueType::Command,
                is_last: true,
                data: vec![2],
            }),
            Err(DimseError::MixedPresentationContext { .. })
        ));
    }
}
