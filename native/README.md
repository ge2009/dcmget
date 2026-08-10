# DcmGet 4.0 native workspace

This workspace is the gated replacement for the Python/Tauri/DCMTK runtime.
It is intentionally developed beside the current implementation until native
DICOM networking, offline OHIF export, upgrades, and clean Windows x64
packaging satisfy the release gates recorded in the repository plan. The
native product does not generate `DICOMDIR` or claim standards-conformant PDI
media.

The default workspace members exclude the GPUI desktop while the upstream
pre-1.0 dependency is being qualified. Core validation remains runnable without
a window system:

```shell
cargo test --workspace --exclude dcmget-desktop --exclude dcmget-ui-kit
```

The standalone CLI has a native, unlicensed download path:

```shell
cargo run --locked -p dcmget-cli -- download \
  --config /path/to/config.json \
  --accessions /path/to/access.txt \
  --destination /path/to/output
```

It binds the configured Storage SCP first and performs one Study Root C-MOVE at
a time. Received objects first land in `.dcmget-staging` on the destination
volume, then publish atomically according to `directory_template` using the
DICOM Patient ID and Study Instance UID plus the requested Accession Number.
It prints one JSON result per Accession Number and exits with `0` for full
success, `1` for input/startup failure, `2` for an operational download
failure, or `130` for Ctrl-C. It intentionally has no PDI, license, or
registration path.

The pinned dicom-rs registry currently limits the native preview to recognized
standard Storage SOP Classes and transfer syntaxes. Unknown/private Storage SOP
Classes and unsupported transfer syntaxes fail closed until an explicit,
auditable acceptance policy is implemented and tested.

No native preview may write the production state directory unless the explicit
migration command has first created and verified a backup.
