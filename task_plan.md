# Task Plan: DcmGet 4.0 GPUI + Native Rust/DICOM

## Goal
Deliver a Windows x64 native DcmGet 4.0 architecture and implementation that can replace the Python/Tauri/DCMTK product only after GPUI, native DICOM networking, state migration, offline-viewer export, update, and clean-install gates pass.

## Current Phase
Phase 3 — Native DICOM technical preview and interoperability gate

## Phases

### Phase 0: Baseline and isolation
- [x] Create an isolated `codex/gpui-native-v4` worktree from clean HEAD.
- [x] Inventory the production contracts, persistent schemas, test fixtures, and release constraints.
- [ ] Record existing behavior/performance baselines that can be measured locally.
- **Status:** in_progress — behavior inventory is frozen; disk/SMB throughput and real PACS baselines remain pending.

### Phase 1: Native Rust workspace and GPUI feasibility gate
- [x] Create the pinned Rust workspace and shared domain/application interfaces.
- [x] Implement the `dcmget-ui-kit` adapter and a representative GPUI desktop shell.
- [x] Verify local Rust tests, formatting, dependency locking, and a macOS host window.
- [ ] Verify the real GPUI feature on Windows x64 through the preview workflow.
- **Status:** in_progress — local feasibility passed; Windows CI/runtime evidence is still required.

### Phase 2: State compatibility and task application service
- [x] Implement tolerant legacy configuration parsing, backup, and non-destructive core-task migration.
- [x] Implement Profile/Task domain types, bounded command/event bus, recovery checkpoints, and SQLite persistence.
- [x] Implement legacy/native process locks and reject orphan task creation.
- [ ] Connect `ApplicationService` to `StateRepository` as the sole writer and migrate the legacy acceptance ledger, licensing, and update state.
- **Status:** in_progress — storage and orchestration foundations pass unit tests but are not yet end-to-end wired.

### Phase 3: Native DICOM receive/retrieve engine
- [x] Implement streaming Part 10 receive/publish, SHA-256 deduplication, and quarantine primitives.
- [x] Implement dicom-rs C-MOVE and Storage SCP lifecycle with cancellation and late-store state handling.
- [ ] Complete the unknown-transfer-syntax strategy, dataset UID checks, bounded retry integration, and async disk-I/O benchmark.
- [ ] Validate against synthetic PACS and the frozen DCMTK behavior baseline.
- **Status:** in_progress — same-process DIMSE loopback passes; third-party and vendor PACS evidence is not yet available.

### Phase 4: Offline viewer export, anonymization, and character sets
- [x] Implement original-DICOM + offline-OHIF export with atomic publication and independent reference validation; do not emit or claim a standards-conformant DICOMDIR/PDI volume.
- [ ] Port anonymization and constrained malformed-Chinese fallback behavior.
- [ ] Implement loopback-only OHIF lifecycle with embedded/external fallback.
- **Status:** in_progress — the validated export directory exists; metadata extraction, viewer assets, and lifecycle integration remain pending.

### Phase 5: Product operations and packaging
- [ ] Port licensing, diagnostics, signed replacement updates, and release notes.
- [x] Implement the unlicensed standalone native CLI download path with explicit exit codes.
- [ ] Produce Windows x64 installer/portable/CLI artifacts with SBOM and content gates.
- [ ] Validate clean-machine upgrade, rollback, SMB/UNC paths, and process cleanup.
- **Status:** in_progress — CLI transfer is implemented locally; product operations and Windows artifacts remain pending.

### Phase 6: Cutover
- [ ] Run real PACS and viewer interoperability pilots.
- [ ] Remove Python/DCMTK/Tauri/React management runtime only after all gates pass.
- [ ] Publish 4.0.0 and retain a verified rollback package and state backup.
- **Status:** pending

## Key Questions
1. Which legacy configuration and SQLite shapes must remain byte-/schema-compatible?
2. Which GPUI/gpui-component revisions build reliably on Windows x64 and the current macOS development host?
3. Can dicom-rs/dicom-ul preserve raw unknown transfer syntaxes without whole-object buffering?
4. Can the offline OHIF export validate every original-DICOM reference without requiring DICOMDIR or DCMTK?

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| Use an isolated worktree based on `bb0cb71` | Preserves the user's dirty Tauri/frontend work and avoids mixing migration code with uncommitted files. |
| Treat the old implementation as an executable specification until cutover | Zero-DCMTK/zero-Python is a release target, not permission to delete proven behavior early. |
| Keep GPUI behind `dcmget-ui-kit` | GPUI and gpui-component are pre-1.0 and must not leak unstable APIs into application code. |
| Use Rust as the sole future business-state writer | Prevents state corruption from concurrent Python/Rust writers. |
| Use DCMTK only as a migration/CI oracle | Final runtime must contain no DCMTK, but independent interoperability evidence remains necessary. |
| Drop DICOMDIR from the 4.0 product scope | The user confirmed it is optional; offline OHIF viewing does not require it, and removing it avoids a high-risk custom PS3.10/PS3.11 implementation. |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| Workspace dependency patch included a non-existent `rand` context line | 1 | Re-apply as separate exact hunks and add `rand` explicitly only if tests require it. |
| System Python 3.14 has no pytest | 1 | Locate and use the configured workspace Python/venv instead of installing into the system interpreter. |
| Offline Cargo cache lacks pinned clap 4.5.48 | 1 | Inspect cached versions and either use the already-vendored compatible version or generate the lock with verified network access. |
| PyPI dependency installation timed out on `pynetdicom` | 1 | Use the local uv cache for available baseline dependencies and keep network-dependent integration checks explicitly pending. |
| `ed25519-dalek` lacked PEM decoding support | 1 | Enable its explicit `pem` feature; `pkcs8` alone only provides DER decoding. |
| Workspace test observed `dcmget-state` while its owner was mid-edit | 1 | Do not patch another worker's files; wait for its scoped tests and rerun integration afterward. |
| `dcmget-application` manifest briefly contained two `[dev-dependencies]` tables | 1 | Merge the new test dependency into the existing table before retrying Cargo. |
| Local pydicom 3.0.1 installation timed out after three retries | 1 | Keep the cross-parser check in Windows CI and report local pydicom interoperability as pending instead of weakening the gate. |

## Notes
- Never remove the old runtime until the corresponding native gate has passed.
- Do not touch the original worktree's user-owned modifications.
- Update this file after every completed phase and log all failed validation attempts.
