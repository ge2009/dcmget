# Findings & Decisions

## Requirements
- Windows x64 native application using Rust, GPUI, gpui-component, and dicom-rs.
- Final runtime contains no Python, DCMTK, Tauri, React management UI, Windows service, or remote management API.
- Main interface is native GPUI; only the offline OHIF viewer may use a loopback WebView/browser.
- Preserve Profile-based concurrency, task recovery, direct target-volume staging, `.dcm` files, anonymization, offline OHIF export, trial/licensing, updates, diagnostics, and standalone CLI behavior.
- Do not cut over until DICOM networking, offline-viewer export, upgrades, SMB/UNC paths, and clean Windows installation pass interoperability gates.

## Research Findings
- The clean migration base is commit `bb0cb711c616f171e52440d94a02f9cc4a252853`; the original worktree has extensive user-owned uncommitted Tauri/frontend changes.
- The clean base already contains mature Profile, task catalog/checkpoint, ledger, PDI, licensing, updater, support bundle, management and standalone CLI modules plus extensive behavior tests; the native migration must use these tests as the contract rather than recreating behavior from memory.
- Persistent state is spread across `tasks.sqlite3`, per-instance `active-task.sqlite3`, and `task-ledger.sqlite3`; legacy migration markers/backups already exist and need explicit Rust compatibility coverage.
- The clean base is product version 3.7.7. `AppConfig` schema version is 8 and contains legacy web/DCMTK fields, retry/circuit-breaker controls, directory templates, PDI, anonymization, minimum-free-space, and log sizing. Native parsing must retain unknown/deprecated fields on migration rather than destructively rewriting them.
- `TaskCatalog` schema version 1 enables WAL, FULL synchronous mode, foreign keys, foreground leasing, task/accession/process/receiver/PDI tables, bounded task details, and migration from `active-task.sqlite3` without deleting the source.
- Current `DcmGetAppService` already separates UI connections from long-running operations and uses a bounded event replay buffer. The Rust command/event service can preserve that behavior without preserving HTTP transport.
- The update trust contract is concrete: schema 1 Ed25519 envelopes with exactly `schema_version`, `algorithm`, `key_id`, base64 `payload`, and base64 `signature`; payloads are capped at 4 MiB and envelopes at 6 MiB. Rust must verify the exact payload bytes and retain the pinned `dcmget-update-2026-01` public key.
- Component updates are whole-file allowlisted patches, not binary diffs. They bind base/target install-tree SHA-256 values, per-file base/target hashes and a full-release trust chain; the native updater must preserve this format and protected user roots rather than inventing a new protocol.
- Current PDI assigns deterministic `DICOM/Pnnnnnn/Snnnnnn/Innnnnn` File IDs, keeps source SHA unchanged, deduplicates identical SOP content, rejects conflicting duplicate UIDs, and verifies every DICOMDIR reference exists and uses 8-character uppercase/digit/underscore segments.
- Strict PDI profile selection is transfer-syntax based: JPEG-only plus Explicit VR LE uses USB-JPEG, JPEG2000-only plus Explicit VR LE uses USB-JPEG2000, otherwise General Purpose. Compatibility output must be marked partial/non-strict; any DICOMDIR core failure prevents publication.
- dicom-rs 0.10 `movescu` already demonstrates the required Study Root presentation context, implicit-VR command encoding, query dataset encoding, separate Command/Data PDVs, and pending/final response loop. It does not expose remaining/completed/failed/warning counters or C-CANCEL, so DcmGet must extend this path rather than wrap its boolean result.
- The stock async `storescp` accumulates every Data PDV in an `instance_buffer`, parses the entire object, then writes it. DcmGet must instead stream Data PDVs into the destination-volume staging writer and use the C-STORE command/presentation context for SOP and transfer-syntax metadata.
- Current release assets already exclude JPEG conversion tools and include a Windows x64 DCMTK vendor archive; final native packaging needs an allowlist gate rather than merely ceasing to call the binaries.
- The current production DCMTK runtime uses four tools: `movescu`, `storescp`, `dcmdump`, and `dcmmkdir`. JPEG conversion tools are already excluded.
- dicom-rs 0.10 includes move/store tools, but they are explicitly not drop-in replacements; the stock storescp path buffers a whole object and cannot be used as the high-throughput production receiver unchanged.
- `dicom-ul` promiscuous abstract-syntax handling does not by itself guarantee acceptance of unknown transfer syntaxes required for raw pass-through.
- Promiscuous abstract-syntax negotiation also cannot be treated as proof that a C-STORE carries a Storage SOP Class. The native receiver now fails closed against the pinned standard SOP registry before creating a file sink; private/unknown Storage SOP UIDs require a future explicit allowlist rather than an unsafe generic-blob acceptance rule.
- A resolver is an internal trust boundary: its SOP Class, SOP Instance, and transfer-syntax values must match the C-STORE command and negotiated presentation context before any bytes reach the sink.
- Lexical `..` rejection alone does not contain writes. Every existing destination ancestor must also reject symlinks/Windows reparse points and remain canonically below the configured root.
- dicom-rs has no production-ready DICOMDIR writer. The user subsequently removed DICOMDIR from the required product scope; 4.0 will export original DICOM plus offline OHIF metadata and will not claim standards-conformant PDI media.
- GPUI and gpui-component are pre-1.0; all dependencies must be pinned and hidden behind a project adapter.

## Technical Decisions
| Decision | Rationale |
|----------|-----------|
| New Rust code lives in a dedicated `native/` workspace | Keeps the migration auditable and avoids coupling it to the uncommitted Tauri preview. |
| Domain/app interfaces are UI-agnostic | Desktop and CLI share behavior; GPUI holds view state only. |
| Receiver stages on the destination volume and atomically publishes | Avoids C-drive copies and preserves received data across interruption. |
| Hash while streaming and deduplicate by SOP UID plus content | Avoids a second full read and prevents overwrite on UID conflicts. |
| One active C-MOVE per Profile, Profiles may run concurrently | Preserves safe attribution while allowing configured parallelism. |
| Loopback viewer binds an ephemeral port with a per-session token | Removes remote management exposure while supporting OHIF. |
| Do not generate DICOMDIR | Avoids a high-risk custom media-directory writer and removes the last PDI-specific reason to retain DCMTK. |
| Reject unrecognized Storage SOP Classes by default | Preserves a safe protocol boundary; private Storage SOP support must be explicit and auditable. |

## Issues Encountered
| Issue | Resolution |
|-------|------------|

## Resources
- https://github.com/zed-industries/zed/tree/main/crates/gpui
- https://github.com/longbridge/gpui-component
- https://github.com/Enet4/dicom-rs
- https://docs.rs/dicom-ul/latest/dicom_ul/association/
- https://dicom.nema.org/medical/dicom/current/output/html/part10.html
- https://dicom.nema.org/medical/dicom/current/output/html/part11.html

## Visual/Browser Findings
- No visual artifacts inspected in this implementation session yet.
