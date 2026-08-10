const PREAMBLE_LENGTH: usize = 128;
const DICOM_PREFIX: &[u8; 4] = b"DICM";
const IMPLEMENTATION_CLASS_UID: &str = "1.2.826.0.1.3680043.10.987.4";
const IMPLEMENTATION_VERSION_NAME: &str = "DCMGET_4_0";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileMeta {
    pub media_storage_sop_class_uid: String,
    pub media_storage_sop_instance_uid: String,
    pub transfer_syntax_uid: String,
}

impl FileMeta {
    pub fn validate(&self) -> Result<(), Part10Error> {
        validate_uid(
            "Media Storage SOP Class UID",
            &self.media_storage_sop_class_uid,
        )?;
        validate_uid(
            "Media Storage SOP Instance UID",
            &self.media_storage_sop_instance_uid,
        )?;
        validate_uid("Transfer Syntax UID", &self.transfer_syntax_uid)
    }
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum Part10Error {
    #[error("{field} is not a valid DICOM UID: {value}")]
    InvalidUid { field: &'static str, value: String },
    #[error("file meta information is too large")]
    FileMetaTooLarge,
}

/// Build a DICOM Part 10 preamble and Explicit VR Little Endian group 0002.
///
/// `dataset` bytes are intentionally not accepted here: the caller writes
/// negotiated PDV payload chunks directly after this header.
pub fn build_part10_header(meta: &FileMeta) -> Result<Vec<u8>, Part10Error> {
    meta.validate()?;
    let mut body = Vec::with_capacity(256);
    push_long_value(&mut body, 0x0002, 0x0001, *b"OB", &[0x00, 0x01])?;
    push_text_value(
        &mut body,
        0x0002,
        0x0002,
        *b"UI",
        &meta.media_storage_sop_class_uid,
        0,
    )?;
    push_text_value(
        &mut body,
        0x0002,
        0x0003,
        *b"UI",
        &meta.media_storage_sop_instance_uid,
        0,
    )?;
    push_text_value(
        &mut body,
        0x0002,
        0x0010,
        *b"UI",
        &meta.transfer_syntax_uid,
        0,
    )?;
    push_text_value(
        &mut body,
        0x0002,
        0x0012,
        *b"UI",
        IMPLEMENTATION_CLASS_UID,
        0,
    )?;
    push_text_value(
        &mut body,
        0x0002,
        0x0013,
        *b"SH",
        IMPLEMENTATION_VERSION_NAME,
        b' ',
    )?;

    let group_length = u32::try_from(body.len()).map_err(|_| Part10Error::FileMetaTooLarge)?;
    let mut output = Vec::with_capacity(PREAMBLE_LENGTH + DICOM_PREFIX.len() + 12 + body.len());
    output.resize(PREAMBLE_LENGTH, 0);
    output.extend_from_slice(DICOM_PREFIX);
    push_tag(&mut output, 0x0002, 0x0000);
    output.extend_from_slice(b"UL");
    output.extend_from_slice(&4_u16.to_le_bytes());
    output.extend_from_slice(&group_length.to_le_bytes());
    output.extend_from_slice(&body);
    Ok(output)
}

fn push_tag(output: &mut Vec<u8>, group: u16, element: u16) {
    output.extend_from_slice(&group.to_le_bytes());
    output.extend_from_slice(&element.to_le_bytes());
}

fn push_long_value(
    output: &mut Vec<u8>,
    group: u16,
    element: u16,
    vr: [u8; 2],
    value: &[u8],
) -> Result<(), Part10Error> {
    let length = u32::try_from(value.len()).map_err(|_| Part10Error::FileMetaTooLarge)?;
    push_tag(output, group, element);
    output.extend_from_slice(&vr);
    output.extend_from_slice(&[0, 0]);
    output.extend_from_slice(&length.to_le_bytes());
    output.extend_from_slice(value);
    if !value.len().is_multiple_of(2) {
        output.push(0);
    }
    Ok(())
}

fn push_text_value(
    output: &mut Vec<u8>,
    group: u16,
    element: u16,
    vr: [u8; 2],
    value: &str,
    padding: u8,
) -> Result<(), Part10Error> {
    let padded_length = value.len() + (value.len() % 2);
    let length = u16::try_from(padded_length).map_err(|_| Part10Error::FileMetaTooLarge)?;
    push_tag(output, group, element);
    output.extend_from_slice(&vr);
    output.extend_from_slice(&length.to_le_bytes());
    output.extend_from_slice(value.as_bytes());
    if !value.len().is_multiple_of(2) {
        output.push(padding);
    }
    Ok(())
}

fn validate_uid(field: &'static str, value: &str) -> Result<(), Part10Error> {
    let valid = !value.is_empty()
        && value.len() <= 64
        && !value.starts_with('.')
        && !value.ends_with('.')
        && value.split('.').all(|component| {
            !component.is_empty()
                && component.bytes().all(|byte| byte.is_ascii_digit())
                && (component == "0" || !component.starts_with('0'))
        });
    if valid {
        Ok(())
    } else {
        Err(Part10Error::InvalidUid {
            field,
            value: value.to_owned(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn meta() -> FileMeta {
        FileMeta {
            media_storage_sop_class_uid: "1.2.840.10008.5.1.4.1.1.2".to_owned(),
            media_storage_sop_instance_uid: "1.2.826.0.1.3680043.10.1".to_owned(),
            transfer_syntax_uid: "1.2.840.10008.1.2.1".to_owned(),
        }
    }

    #[test]
    fn header_has_preamble_prefix_and_correct_group_length() {
        let header = build_part10_header(&meta()).expect("valid file meta");
        assert!(header[..128].iter().all(|byte| *byte == 0));
        assert_eq!(&header[128..132], b"DICM");
        assert_eq!(&header[132..136], &[0x02, 0x00, 0x00, 0x00]);
        assert_eq!(&header[136..138], b"UL");
        assert_eq!(u16::from_le_bytes([header[138], header[139]]), 4);
        let declared = u32::from_le_bytes(header[140..144].try_into().unwrap());
        assert_eq!(usize::try_from(declared).unwrap(), header.len() - 144);
    }

    #[test]
    fn ui_values_are_even_and_null_padded() {
        let mut value = meta();
        value.media_storage_sop_instance_uid = "1.2.3.4.5".to_owned();
        let header = build_part10_header(&value).expect("valid file meta");
        let needle = b"1.2.3.4.5\0";
        assert!(header.windows(needle.len()).any(|window| window == needle));
    }

    #[test]
    fn invalid_uid_is_rejected_before_any_file_is_created() {
        let mut value = meta();
        value.transfer_syntax_uid = "1.02.bad".to_owned();
        assert!(matches!(
            build_part10_header(&value),
            Err(Part10Error::InvalidUid {
                field: "Transfer Syntax UID",
                ..
            })
        ));
    }
}
