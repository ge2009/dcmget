# Task Handoff

## 1. Objective

Build a gated Windows x64 `DcmGet` 4.0 replacement using Rust, GPUI,
gpui-component, and dicom-rs. The final runtime must not contain Python,
DCMTK, Tauri, React management code, a Windows service, or a remote management
API.

## 2. Scope and Constraints

- Work only in `/Users/wenzhengde/.codex/worktrees/dcmget-gpui-native-v4` on
  `codex/gpui-native-v4`.
- The original worktree contains user-owned uncommitted Tauri/frontend work and
  must remain untouched.
- Windows x64 is the only release target. Windows ARM64 may use x64 emulation;
  32-bit binaries are rejected.
- The native receiver writes `.part` files on the destination volume and only
  publishes `.dcm` after a successful flush, sync, and same-volume rename.
- DICOMDIR is no longer a requirement. Offline export may contain original
  DICOM plus OHIF metadata, but must not claim PS3.10/PS3.11 PDI conformance.
- The old implementation remains the behavioral oracle until every native
  release gate has passed. Do not delete it during preview development.

## 3. Work Completed

- Added a pinned Rust 1.90 workspace and committed dependency lock under
  `native/`.
- Added domain, state, application, native DICOM, offline export,
  updater-verification, CLI, UI adapter, and GPUI desktop packages.
- Added tolerant legacy JSON parsing and WAL-safe SQLite migrations with
  non-destructive backup, recovery checkpoints, full-config Profile identity,
  and unresolved-task deletion guards.
- Added an application command/event boundary, four-Hz progress coalescing,
  one-active-task-per-Profile invariant, and legacy/native process locks.
- Added a dicom-rs Study Root C-MOVE and asynchronous Storage SCP prototype.
- Added chunked target-volume storage, SHA-256, duplicate/conflict handling,
  cancellation, late-store timing, and deterministic port release.
- Added a native GPUI shell behind `dcmget-ui-kit` with no business-state
  access from widgets.
- Added a real standalone native CLI download path with safe Accession Number
  routing, persistent SCP lifetime, sequential C-MOVE, late-store drain,
  explicit receiver shutdown, and exit codes `0/1/2/130`.
- Added atomic original-DICOM offline export with the existing OHIF index
  shape, SHA-256 manifest, duplicate/conflict behavior, local-reference
  validation, and no DICOMDIR/DCMTK dependency.
- Added `.github/workflows/windows-native-preview.yml` for Windows x64 tests,
  real GPUI compilation, AMD64 PE validation, and legacy-runtime rejection.
- Removed the DICOMDIR prototype after the requirement was dropped.

## 4. Current State

The repository contains a **technical preview foundation**, not a production
replacement. The native DICOM, CLI, and offline-export components are
implemented and locally tested. The desktop still renders a representative
snapshot and is not connected end to end to persistent tasks or the receiver.
The Windows workflow has not yet run on a Windows runner.

## 5. Outstanding Work

1. Connect `ApplicationService` to `StateRepository` and the native DICOM
   engine as the sole writer; verify crash recovery.
2. Add bounded retry/circuit-breaker behavior without repeating a partially
   received study blindly.
3. Connect metadata extraction, bundled viewer assets, and the loopback OHIF
   lifecycle to the validated export primitive; do not generate DICOMDIR.
4. Port anonymization, restricted malformed-Chinese fallback, task ledger,
   licensing, diagnostics, and the actual update replacement path.
5. Run Windows x64 CI, then third-party PACS, local-disk, UNC/mapped-SMB,
   long-duration throughput, large-object memory, and clean-install tests.
6. Build an installer only after the desktop is wired and all content gates
   pass.

## 6. Current Blockers

- Unknown or dicom-rs-registry-unsupported transfer syntaxes require a pinned
  dicom-ul negotiation-policy change before lossless raw pass-through can be
  claimed.
- There is no third-party PACS or vendor interoperability evidence yet; the
  current DIMSE evidence is same-process loopback only.
- The macOS environment could not complete optional Python dependency downloads
  from PyPI, so only the available legacy config baseline was run locally.

## 7. Important Decisions and Rationale

- Keep all GPUI/gpui-component calls behind `dcmget-ui-kit` because both are
  pre-1.0.
- Keep one active C-MOVE per Profile while allowing Profiles to run in
  parallel, preserving safe attribution without serializing the whole app.
- Keep exact received bytes and hash while streaming; do not decode Pixel Data
  on the receive hot path.
- Keep the updater out of preview payloads until it can actually replace and
  roll back files. A readiness stub must never report false success.
- Keep DCMTK out of the native runtime. The legacy implementation may remain as
  a test oracle until cutover.

## 8. Pitfalls and Failed Attempts

- Do not run Cargo integration tests while another worker is midway through a
  module edit; earlier transient compiler errors were caused by observing an
  incomplete shared-filesystem state.
- Rust 1.90 `File::try_lock` returns `TryLockError`; do not treat it as a plain
  `io::Error`.
- Do not infer local completeness from PACS C-MOVE status alone.
- Do not reintroduce DICOMDIR or `dcmmkdir`; that requirement was explicitly
  removed.
- Do not package `dcmget-updater.exe` while it is only a verifier/readiness
  stub.

## 9. Verification Status

- Existing Python configuration baseline: 52 tests passed.
- Native DICOM loopback after protocol/path review fixes: 35 tests passed;
  strict Clippy passed.
- Domain: 6 tests passed. State: 10 tests passed. Application: 6 tests passed.
- Native CLI: 11 tests passed. Offline export: 8 tests passed. Updater verifier:
  3 tests passed.
- Full non-window workspace: 79 tests passed; formatting and strict Clippy
  passed.
- Real GPUI feature: UI kit 3 tests and desktop 1 test passed; strict Clippy
  passed; one macOS native window was launched during feasibility testing.
- Not verified: Windows runner, real PACS, unknown transfer syntax, SMB/UNC,
  sustained throughput, 2 GiB memory target, viewer lifecycle, installer/update.

## 10. Recommended Next Action

First run `.github/workflows/windows-native-preview.yml` from the committed
branch. If that gate passes, start the application-service integration and
verify it with repository-backed task recovery tests. The local regression
commands are:

```shell
cargo fmt --all -- --check
cargo test --locked --workspace --exclude dcmget-desktop --exclude dcmget-ui-kit
cargo clippy --locked --workspace --exclude dcmget-desktop --exclude dcmget-ui-kit --all-targets -- -D warnings
cargo test --locked -p dcmget-ui-kit -p dcmget-desktop --no-default-features --features dcmget-desktop/gpui-ui
cargo clippy --locked -p dcmget-ui-kit -p dcmget-desktop --no-default-features --features dcmget-desktop/gpui-ui --all-targets -- -D warnings
```

Then run the new Windows preview workflow before claiming any Windows artifact.

## 11. Key References

- `task_plan.md`
- `findings.md`
- `progress.md`
- `native/README.md`
- `native/crates/dcmget-dicom/README.md`
- `.github/workflows/windows-native-preview.yml`
- `dcmget/core.py`, `dcmget/pdi.py`, and existing tests remain the migration
  behavior references.
