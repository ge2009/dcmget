# dcmget-dicom

Native DICOM contracts and durable file-receive primitives for DcmGet 4.0.

Implemented here:

- structured C-MOVE request/result and receive types;
- async Study Root C-MOVE with counters, bounded timeouts, C-CANCEL and abort fallback;
- cancellable async Storage SCP with fragmented commands, C-ECHO, PDV-streamed C-STORE,
  a 16-association cap and deterministic port release;
- standard-registry Storage SOP Class gating plus exact command/context/resolver UID matching;
- cancellation and deterministic late-C-STORE policy;
- DICOM Part 10 file-meta construction;
- chunked writes to a target-volume `.part` file;
- SHA-256, flush/fsync, same-volume atomic publication;
- rejection of pre-existing symlink/reparse-point directory ancestors below the destination root;
- exact-byte duplicate suppression and conflict preservation.

Not implemented here yet:

- dataset-level SOP UID validation;
- transfer-syntax codec support.

The network adapter feeds negotiated dataset PDV chunks into
`StorePayloadSink`, but only negotiates transfer syntaxes which dicom-rs 0.10
marks supported. `TransferSyntaxSupport::RequiresDicomUlPatch` retains the
planned boundary for lossless raw preservation of unknown or
upstream-unsupported syntaxes. The crate does not claim that capability before
the pinned negotiation-policy patch exists.

Storage SOP Class acceptance is intentionally fail-closed: only Storage Service
classes recognized by the pinned `dicom-dictionary-std` registry are accepted.
Unknown and private SOP Class UIDs are rejected even if they are valid private
Storage SOP Classes. Supporting them later requires an explicit, user-reviewed
private SOP allowlist or an equivalent acceptance-policy extension; this crate
does not currently claim that compatibility.

Publication locking is bounded and process-local. The desktop application's
single-instance/legacy-process guard is therefore a required precondition; a
future requirement for independent writers targeting the same directory must
add a Windows-compatible inter-process file lease before enabling that mode.
