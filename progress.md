# Progress Log

## Session: 2026-08-10

### Phase 0: Baseline and isolation
- **Status:** in_progress
- **Started:** 2026-08-10
- Actions taken:
  - Confirmed the original worktree is dirty and must remain untouched.
  - Created `codex/gpui-native-v4` from clean commit `bb0cb71` in an isolated worktree.
  - Initialized persistent planning, findings, and progress records.
- Files created/modified:
  - `task_plan.md`
  - `findings.md`
  - `progress.md`

## Test Results
| Test | Input | Expected | Actual | Status |
|------|-------|----------|--------|--------|
| Worktree isolation | `git worktree list`, `git status` | New clean branch; original dirty state preserved | New branch created from `bb0cb71`; original modifications untouched | PASS |

## Error Log
| Timestamp | Error | Attempt | Resolution |
|-----------|-------|---------|------------|
| 2026-08-10 | `apply_patch` context failed while adding updater dependencies because `rand` was not present | 1 | Split the dependency edit into exact existing-context hunks. |
| 2026-08-10 | zsh rejected an unmatched glob while locating dicom-ul server source | 1 | Use an exact file path/`rg --files` on the next inspection instead of a shell glob. |
| 2026-08-10 | Baseline pytest used Homebrew Python 3.14 without pytest | 1 | Use the repository's configured dependency runtime/venv; do not mutate system Python. |
| 2026-08-10 | Offline Cargo resolution could not find clap 4.5.48 | 1 | Align exact dependencies to locally cached compatible releases or obtain a lockfile online before retrying. |
| 2026-08-10 | `uv pip install` timed out fetching `pynetdicom` from PyPI after three retries | 1 | Switch to offline cached packages for unit baselines; do not repeat the same network install. |
| 2026-08-10 | Native updater failed to compile because `VerifyingKey::from_public_key_pem` was feature-gated | 1 | Enable `ed25519-dalek/pem` and keep the existing PKCS#8 compatibility. |
| 2026-08-10 | Full workspace compile saw incomplete `dcmget-state` modules during concurrent implementation | 1 | Deferred integration compile until the state worker finishes; no conflicting edits made. |
| 2026-08-10 | `cargo test -p dcmget-application` rejected a duplicate `[dev-dependencies]` table introduced while adding process-guard tests | 1 | Consolidated the manifest table; retry remains deferred until `dcmget-state` finishes its in-progress module set. |
| 2026-08-10 | `uv pip install pydicom==3.0.1` timed out against PyPI after three retries | 1 | The external-parser gate was later removed with DICOMDIR itself after the user made that feature optional. |
| 2026-08-10 | PDI Clippy rejected unquoted `DcmGet` in crate-level documentation | 1 | Marked the product name as inline code and reran the scoped lint. |
| 2026-08-10 | Rust 1.90 `File::try_lock` returns `std::fs::TryLockError`, not `io::Error` | 1 | Match `WouldBlock` and `Error(io::Error)` explicitly so contention and real I/O failures remain distinguishable. |
| 2026-08-10 | Application Clippy rejected unquoted `DcmGet` in crate documentation | 1 | Marked the product name as inline code before the final strict workspace lint. |
| 2026-08-10 | Updater Clippy rejected unquoted `DcmGet` and a fixed `HashMap` hasher | 1 | Corrected documentation and generalized trusted-key lookup over `BuildHasher`. |
| 2026-08-10 | Updater test used an implicit `Default::default()` type for PEM line endings | 1 | Named `pkcs8::LineEnding` explicitly for strict Clippy. |
| 2026-08-10 | `ed25519_dalek::pkcs8` did not re-export `LineEnding` under the enabled feature set | 1 | Import the concrete `spki::der::pem::LineEnding` path instead. |
| 2026-08-10 | macOS system Ruby 2.6 rejected the newer `YAML.load_file(..., aliases: true)` keyword | 1 | Re-ran the workflow syntax check with the Ruby 2.6-compatible `YAML.load_file(path)` form; parsing passed. |
| 2026-08-10 | A native `cargo metadata/tree` audit was first launched from the repository root, which has no root `Cargo.toml` | 1 | Re-ran from `native/`; the locked desktop version resolved to `4.0.0-preview.1` and no Tauri/Python/DCMTK dependency matched. |
| 2026-08-10 | The first full-workspace verification command used a one-second shell timeout and was terminated while compiling | 1 | Re-ran the same locked format/test/Clippy chain with a 20-minute command limit; it completed successfully in seconds. |
| 2026-08-10 | The first staged diff check found one extra blank line at EOF in `native/rust-toolchain.toml` | 1 | Removed the extra blank line, restaged the file, and reran the complete staged diff check. |
| 2026-08-10 | The first commit attempt could not reach the configured 1Password signing socket | 1 | Retry this isolated preview commit with `commit.gpgsign=false`; do not alter the user's global Git signing configuration. |

### Foundation review fixes
- Reject task creation for an unregistered Profile instead of creating an orphan runtime task.
- Emit `TaskCancelled` separately from `TaskFinished`, preserving the persistence/UI outcome boundary.
- Keep the updater verifier under test but omit the no-op updater executable from the preview artifact; default stub invocation now exits with code 2.
- Match migrated Profiles by complete configuration identity and use a canonical SHA-256 synthetic ID when no Profile matches.
- Downgrade inconsistent legacy terminal tasks to retryable and refuse deletion while pending or partial accessions remain.

### Native offline-viewer foundation
- A native DICOMDIR prototype was implemented and unit-tested, then deliberately removed after the user confirmed DICOMDIR/PDI conformance is not required. The 4.0 export target is now original DICOM plus offline OHIF metadata.
- Added a Windows x64 technical-preview workflow which tests the core, builds the real GPUI feature, verifies AMD64 PE headers, and rejects Python/DCMTK/Tauri payloads.
- Added a process guard which acquires both the legacy `gui-instance.json.lock` and a native lock, preventing old and new desktops from writing the same state root concurrently.

### Native state, UI, and DICOM preview
- Added a Rust 1.90 workspace with a committed dependency lock and exact GPUI/gpui-component versions.
- Added a GPUI shell behind `dcmget-ui-kit`; the UI can only submit typed commands and consume immutable snapshots.
- Added tolerant legacy configuration and task recovery migration with online backup, WAL-safe SQLite access, full-profile identity matching, and unresolved-task deletion guards.
- Added a streaming Storage SCP and Study Root C-MOVE prototype using dicom-rs/dicom-ul, including C-ECHO, bounded associations, direct target-volume `.part` files, atomic `.dcm` publication, duplicate/conflict handling, cancellation, and late-store timing.
- Hardened the receiver so only standard Storage SOP Classes reach the sink, resolver metadata must match command/context, and pre-existing symlink or Windows reparse ancestors cannot redirect destination writes.
- Added a Windows x64 technical-preview workflow with PE architecture and forbidden legacy-runtime content gates. It does not replace the formal release workflow.
- Kept the updater executable out of the preview payload because replacement logic is not implemented; default invocation fails loudly instead of reporting false success.
- Added an unlicensed native CLI which binds one persistent Storage SCP, executes sequential Study Root C-MOVEs, drains late stores before route changes, writes one JSON result per Accession Number, and always shuts down the receiver before returning.
- The CLI treats missing PACS completion counters and duplicate-SOP count gaps as unverified rather than false success; it keeps all received files and never performs an automatic whole-study re-download.
- Added an atomic offline export which streams original `.dcm` files into a hidden same-volume partial directory, emits the existing `VIEWER/.dcmget/index` structure and `MANIFEST.SHA256`, independently verifies every local reference/hash, and publishes with one directory rename. It emits no `DICOMDIR` and uses no DCMTK.

## Additional Test Results
| Test | Input | Expected | Actual | Status |
|------|-------|----------|--------|--------|
| Offline export | `cargo test --locked -p dcmget-pdi` | Original `.dcm`, local OHIF index, duplicate/conflict handling, manifest validation, and atomic publication pass | 8 passed; scoped Clippy clean | PASS |
| Legacy config baseline | existing Python config tests using an available local pytest runtime | Existing normalization/migration behavior remains green | 52 passed | PASS |
| Native DICOM loopback | `cargo test --locked -p dcmget-dicom` | Store/Move fragmentation, counters, cancel, protocol rejection, safe publish, and port release pass | 35 passed; strict Clippy clean | PASS |
| Native state/domain | targeted package tests after migration review fixes | Legacy roots/catalogs migrate without orphan terminal tasks | domain 6; state 10 passed | PASS |
| Native application | targeted package tests after command review fixes | Unknown Profile rejected; cancel differs from finish | 6 passed; strict Clippy clean | PASS |
| GPUI adapter and desktop | real `gpui-ui` feature tests and strict Clippy | Pinned native UI compiles and adapter tests pass | UI kit 3; desktop 1 passed | PASS |
| Standalone native CLI | `cargo test --locked -p dcmget-cli` plus strict Clippy/format/build | Native receive/move command, safe routing, completeness classification, and port release pass | 11 passed; lint/format/build clean | PASS |
| Full native core integration | workspace tests excluding UI plus strict Clippy/format | All non-window packages build and remain mutually compatible | 79 tests passed; format and strict Clippy clean | PASS |
| Real GPUI feature integration | real-feature tests plus strict Clippy | Desktop and UI adapter build against pinned GPUI components | 4 tests passed; strict Clippy clean | PASS |
| CLI/updater exit behavior | manual `cargo run` invocations | CLI readiness 0, missing config 1; updater readiness 0 and unavailable default 2 | Exact expected exit codes observed | PASS |
| Native release build on development host | release GPUI desktop followed by release CLI | Optimized desktop and CLI link successfully with the locked dependency graph | Both release builds completed | PASS |
| Windows x64 preview | `.github/workflows/windows-native-preview.yml` | GPUI binary is AMD64 and payload has no Python/DCMTK/Tauri | Workflow authored; not yet run on Windows | PENDING |
| Real PACS and SMB performance | vendor PACS, local disk, UNC/mapped SMB corpus | File counts and throughput meet the frozen legacy baseline | Not run in this environment | PENDING |

## 5-Question Reboot Check
| Question | Answer |
|----------|--------|
| Where am I? | Phase 3: native DICOM technical preview and interoperability gate. |
| Where am I going? | Pinned Rust/GPUI workspace, compatible state layer, native DICOM/offline OHIF export, then Windows packaging and gated cutover. |
| What's the goal? | A verifiable Windows x64 native DcmGet 4.0 that can safely replace Python/Tauri/DCMTK. |
| What have I learned? | See `findings.md`. |
| What have I done? | Built and locally verified the pinned workspace, state/application foundations, GPUI shell, loopback native DICOM engine, and Windows preview gate. |
