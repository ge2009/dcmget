use std::collections::HashMap;
use std::ffi::OsString;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::net::{Ipv4Addr, SocketAddrV4, TcpListener};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, Once, mpsc};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use dcmget_application::{ApplicationHandle, ApplicationService, BootstrapPaths, BootstrapResult};
use dcmget_domain as domain;
use dcmget_ui_kit as ui;

const COMMAND_TIMEOUT: Duration = Duration::from_secs(3);
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(15);
const RUNTIME_SHUTDOWN_GRACE: Duration = Duration::from_secs(2);
const SMOKE_RECEIVER_TIMEOUT: Duration = Duration::from_secs(15);
const SMOKE_TASK_TIMEOUT: Duration = Duration::from_secs(90);
const DIAGNOSTIC_LOG_MAX_BYTES: u64 = 20 * 1024 * 1024;

static INSTALL_PANIC_LOGGER: Once = Once::new();

#[derive(Clone, Debug)]
struct DesktopPaths {
    config_root: PathBuf,
    state_root: PathBuf,
    native_database: PathBuf,
    backup_root: PathBuf,
    diagnostic_log: PathBuf,
}

impl DesktopPaths {
    fn detect() -> Result<Self, String> {
        let config_base = environment_path("APPDATA")
            .or_else(|| environment_path("XDG_CONFIG_HOME"))
            .or_else(|| home_path().map(|home| home.join(".config")))
            .ok_or_else(|| {
                "无法确定配置目录：APPDATA、XDG_CONFIG_HOME 和 HOME 均未设置".to_owned()
            })?;
        let state_base = environment_path("LOCALAPPDATA")
            .or_else(|| environment_path("XDG_DATA_HOME"))
            .or_else(|| home_path().map(|home| home.join(".local").join("share")))
            .ok_or_else(|| {
                "无法确定状态目录：LOCALAPPDATA、XDG_DATA_HOME 和 HOME 均未设置".to_owned()
            })?;
        Ok(Self::from_bases(&config_base, &state_base))
    }

    fn from_bases(config_base: &Path, state_base: &Path) -> Self {
        let config_root = config_base.join("DcmGet");
        let state_root = state_base.join("DcmGet");
        let native_root = state_root.join("native");
        Self {
            config_root,
            state_root,
            native_database: native_root.join("state.sqlite3"),
            backup_root: native_root.join("backups"),
            diagnostic_log: native_root.join("logs").join("dcmget-native.log"),
        }
    }

    fn bootstrap_paths(&self) -> BootstrapPaths {
        BootstrapPaths {
            config_root: self.config_root.clone(),
            state_root: self.state_root.clone(),
            native_database: self.native_database.clone(),
            backup_root: self.backup_root.clone(),
        }
    }
}

#[derive(Clone)]
struct DiagnosticLog {
    path: PathBuf,
}

impl DiagnosticLog {
    fn open(path: &Path) -> Result<Self, String> {
        let parent = path
            .parent()
            .ok_or_else(|| format!("诊断日志路径无父目录：{}", path.display()))?;
        fs::create_dir_all(parent)
            .map_err(|error| format!("无法创建诊断日志目录 {}：{error}", parent.display()))?;
        if fs::metadata(path).is_ok_and(|metadata| metadata.len() >= DIAGNOSTIC_LOG_MAX_BYTES) {
            let rotated = path.with_extension("log.1");
            let _ = fs::remove_file(&rotated);
            fs::rename(path, &rotated).map_err(|error| {
                format!(
                    "无法轮转诊断日志 {} 到 {}：{error}",
                    path.display(),
                    rotated.display()
                )
            })?;
        }
        let log = Self {
            path: path.to_path_buf(),
        };
        Ok(log)
    }

    fn append(&self, level: &str, message: &str) {
        let timestamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |duration| duration.as_secs());
        let message = single_line(message);
        if let Ok(mut file) = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
        {
            let _ = writeln!(file, "{timestamp} {level} {message}");
        }
    }

    fn install_panic_hook(&self) {
        let log = self.clone();
        INSTALL_PANIC_LOGGER.call_once(move || {
            let previous = std::panic::take_hook();
            std::panic::set_hook(Box::new(move |info| {
                let location = info.location().map_or_else(
                    || "unknown".to_owned(),
                    |location| format!("{}:{}", location.file(), location.line()),
                );
                // Do not persist the panic payload: vendor/DICOM errors may contain patient data.
                log.append("CRITICAL", &format!("panic at {location}"));
                previous(info);
            }));
        });
    }
}

fn single_line(message: &str) -> String {
    message
        .chars()
        .map(|character| {
            if matches!(character, '\r' | '\n' | '\t') {
                ' '
            } else {
                character
            }
        })
        .take(2_000)
        .collect()
}

fn environment_path(name: &str) -> Option<PathBuf> {
    std::env::var_os(name)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
}

fn home_path() -> Option<PathBuf> {
    environment_path("HOME").or_else(|| environment_path("USERPROFILE"))
}

#[derive(Default)]
struct BridgeSignals {
    selected_profile_id: Option<String>,
    detailed_logs: bool,
    last_error: Option<String>,
    startup_warnings: Vec<String>,
}

#[derive(Clone, Copy, Debug, Default)]
struct BootstrapInfo {
    profile_count: usize,
    legacy_source_loaded: bool,
}

struct ApplicationRuntime {
    handle: ApplicationHandle,
    executor: tokio::runtime::Handle,
    signals: Arc<Mutex<BridgeSignals>>,
    completion: Mutex<Option<mpsc::Receiver<()>>>,
    worker: Mutex<Option<JoinHandle<()>>>,
    shutdown_started: AtomicBool,
    bootstrap_info: BootstrapInfo,
    diagnostics: DiagnosticLog,
}

impl ApplicationRuntime {
    #[allow(clippy::too_many_lines)]
    fn start(paths: &DesktopPaths) -> Result<Self, String> {
        let diagnostics = DiagnosticLog::open(&paths.diagnostic_log)?;
        diagnostics.install_panic_hook();
        diagnostics.append("INFO", "SESSION START");
        if let Some(parent) = paths.native_database.parent() {
            fs::create_dir_all(parent)
                .map_err(|error| format!("无法创建原生状态目录 {}：{error}", parent.display()))?;
        }
        fs::create_dir_all(&paths.backup_root).map_err(|error| {
            format!(
                "无法创建配置备份目录 {}：{error}",
                paths.backup_root.display()
            )
        })?;

        let BootstrapResult {
            service,
            handle,
            migration,
        } = ApplicationService::bootstrap(paths.bootstrap_paths())
            .map_err(|error| format!("DcmGet 后台初始化失败：{error}"))?;
        let initial = handle.snapshot();
        let bootstrap_info = BootstrapInfo {
            profile_count: initial.profiles.len(),
            legacy_source_loaded: migration.profiles_imported > 0
                || migration.tasks_imported > 0
                || migration.sources_skipped > 0,
        };
        let startup_warnings = migration
            .warnings
            .iter()
            .map(|warning| {
                format!(
                    "旧版数据迁移警告（{}）：{}",
                    warning.source.display(),
                    warning.message
                )
            })
            .collect();
        let signals = Arc::new(Mutex::new(BridgeSignals {
            startup_warnings,
            ..BridgeSignals::default()
        }));
        let event_signals = Arc::clone(&signals);
        let event_diagnostics = diagnostics.clone();
        let mut events = handle.subscribe();
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .thread_name("dcmget-native")
            .build()
            .map_err(|error| format!("无法创建 DcmGet 后台运行时：{error}"))?;
        let executor = runtime.handle().clone();
        let (completion_sender, completion_receiver) = mpsc::channel();
        let worker_diagnostics = diagnostics.clone();
        let worker = thread::Builder::new()
            .name("dcmget-application".to_owned())
            .spawn(move || {
                runtime.block_on(async move {
                    let event_worker = tokio::spawn(async move {
                        loop {
                            match events.recv().await {
                                Ok(domain::AppEvent::CommandRejected { message }) => {
                                    event_diagnostics.append("ERROR", &message);
                                    lock(&event_signals).last_error = Some(message);
                                }
                                Ok(domain::AppEvent::LogAppended { entry }) => {
                                    let level = match entry.level {
                                        domain::LogLevel::Error => "ERROR",
                                        domain::LogLevel::Warning => "WARN",
                                        domain::LogLevel::Info => "INFO",
                                        domain::LogLevel::Debug => "DEBUG",
                                    };
                                    event_diagnostics.append(
                                        level,
                                        &format!("{}: {}", entry.source, entry.message),
                                    );
                                }
                                Ok(_) => {}
                                Err(tokio::sync::broadcast::error::RecvError::Lagged(count)) => {
                                    event_diagnostics.append(
                                        "ERROR",
                                        &format!("event stream lagged by {count}"),
                                    );
                                    lock(&event_signals).last_error = Some(format!(
                                        "后台事件过快，界面跳过了 {count} 条中间更新；最终状态仍会同步"
                                    ));
                                }
                                Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
                            }
                        }
                    });
                    service.run().await;
                    event_worker.abort();
                    let _ = event_worker.await;
                });
                // Blocking filesystem calls (notably a disconnected SMB share) cannot be
                // force-cancelled by Rust. Do not let Tokio wait forever for such workers after
                // the receiver and its listening socket have already been stopped.
                runtime.shutdown_timeout(RUNTIME_SHUTDOWN_GRACE);
                worker_diagnostics.append("INFO", "SESSION BACKEND STOPPED");
                let _ = completion_sender.send(());
            })
            .map_err(|error| format!("无法启动 DcmGet 后台线程：{error}"))?;

        Ok(Self {
            handle,
            executor,
            signals,
            completion: Mutex::new(Some(completion_receiver)),
            worker: Mutex::new(Some(worker)),
            shutdown_started: AtomicBool::new(false),
            bootstrap_info,
            diagnostics,
        })
    }

    fn enqueue(&self, command: domain::AppCommand) {
        lock(&self.signals).last_error = None;
        let handle = self.handle.clone();
        let signals = Arc::clone(&self.signals);
        self.executor.spawn(async move {
            if let Err(error) = handle.send(command).await {
                lock(&signals).last_error = Some(error.to_string());
            }
        });
    }

    fn send_blocking(&self, command: domain::AppCommand, timeout: Duration) -> Result<(), String> {
        let handle = self.handle.clone();
        let (sender, receiver) = mpsc::channel();
        self.executor.spawn(async move {
            let _ = sender.send(
                handle
                    .send(command)
                    .await
                    .map_err(|error| error.to_string()),
            );
        });
        receiver
            .recv_timeout(timeout)
            .map_err(|_| "向 DcmGet 后台提交命令超时".to_owned())??;
        Ok(())
    }

    fn set_local_error(&self, message: impl Into<String>) {
        let message = message.into();
        self.diagnostics.append("ERROR", &message);
        lock(&self.signals).last_error = Some(message);
    }

    fn presentation_snapshot(&self) -> ui::WorkspaceSnapshot {
        let application = self.handle.snapshot();
        let signals = lock(&self.signals);
        map_snapshot(&application, &signals)
    }

    fn shutdown(&self) -> Result<(), String> {
        let should_request = self
            .shutdown_started
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_ok();
        let mut first_error = None;
        if should_request
            && let Err(error) =
                self.send_blocking(domain::AppCommand::ExitApplication, COMMAND_TIMEOUT)
        {
            // A timed-out submission may not have reached the service. Permit a later explicit
            // shutdown or Drop to retry instead of permanently suppressing ExitApplication.
            self.shutdown_started.store(false, Ordering::Release);
            first_error = Some(error);
        }

        let mut completion = lock(&self.completion);
        let mut completed = completion.is_none();
        if let Some(receiver) = completion.as_ref() {
            match receiver.recv_timeout(SHUTDOWN_TIMEOUT) {
                Ok(()) => {
                    *completion = None;
                    completed = true;
                    first_error = None;
                }
                Err(_) if first_error.is_none() => {
                    first_error = Some("等待接收器和下载任务退出超时".to_owned());
                }
                Err(_) => {}
            }
        }
        drop(completion);

        if completed
            && let Some(worker) = lock(&self.worker).take()
            && worker.join().is_err()
        {
            first_error = Some("DcmGet 后台线程异常退出".to_owned());
        }
        if let Some(error) = first_error {
            self.diagnostics.append("ERROR", &error);
            Err(error)
        } else {
            self.diagnostics.append("INFO", "SESSION NORMAL EXIT");
            Ok(())
        }
    }
}

impl Drop for ApplicationRuntime {
    fn drop(&mut self) {
        let _ = self.shutdown();
    }
}

struct ResilientBackend {
    paths: Result<DesktopPaths, String>,
    application: Mutex<Option<Arc<ApplicationRuntime>>>,
    startup_error: Mutex<Option<String>>,
}

impl ResilientBackend {
    fn new() -> Self {
        let paths = DesktopPaths::detect();
        let backend = Self {
            paths,
            application: Mutex::new(None),
            startup_error: Mutex::new(None),
        };
        backend.try_open();
        backend
    }

    fn try_open(&self) {
        let mut application = lock(&self.application);
        if application.is_some() {
            return;
        }
        let result = self
            .paths
            .as_ref()
            .map_err(Clone::clone)
            .and_then(ApplicationRuntime::start);
        match result {
            Ok(runtime) => {
                *application = Some(Arc::new(runtime));
                *lock(&self.startup_error) = None;
            }
            Err(error) => {
                if let Ok(paths) = &self.paths
                    && let Ok(diagnostics) = DiagnosticLog::open(&paths.diagnostic_log)
                {
                    diagnostics.install_panic_hook();
                    diagnostics.append("ERROR", &format!("SESSION START FAILED: {error}"));
                }
                *lock(&self.startup_error) = Some(error);
            }
        }
    }

    fn application(&self) -> Option<Arc<ApplicationRuntime>> {
        lock(&self.application).clone()
    }

    fn set_startup_error(&self, message: impl Into<String>) {
        *lock(&self.startup_error) = Some(message.into());
    }
}

impl ui::CommandSink for ResilientBackend {
    fn submit(&self, command: ui::DesktopCommand) {
        if matches!(&command, ui::DesktopCommand::OpenLogDirectory) {
            match &self.paths {
                Ok(paths) => {
                    if let Some(directory) = paths.diagnostic_log.parent()
                        && let Err(error) = fs::create_dir_all(directory)
                            .map_err(|error| {
                                format!("无法创建日志目录 {}：{error}", directory.display())
                            })
                            .and_then(|()| open_directory(directory))
                    {
                        self.set_startup_error(error);
                    }
                }
                Err(error) => self.set_startup_error(error.clone()),
            }
            return;
        }
        if matches!(command, ui::DesktopCommand::ReloadWorkspace) && self.application().is_none() {
            self.try_open();
            return;
        }
        let Some(application) = self.application() else {
            self.set_startup_error("后台尚未就绪，请先修复启动错误后重新加载");
            return;
        };
        submit_desktop_command(&application, command);
    }
}

impl ui::SnapshotSource for ResilientBackend {
    fn snapshot(&self) -> Result<ui::WorkspaceSnapshot, String> {
        if let Some(application) = self.application() {
            return Ok(application.presentation_snapshot());
        }
        Err(lock(&self.startup_error)
            .clone()
            .unwrap_or_else(|| "DcmGet 后台正在初始化".to_owned()))
    }
}

impl ui::WorkspaceBackend for ResilientBackend {
    fn shutdown(&self) -> Result<(), String> {
        if let Some(application) = self.application() {
            application.shutdown()?;
        }
        Ok(())
    }
}

pub fn open() -> Arc<dyn ui::WorkspaceBackend> {
    Arc::new(ResilientBackend::new())
}

#[allow(clippy::too_many_lines)]
fn submit_desktop_command(application: &ApplicationRuntime, command: ui::DesktopCommand) {
    match command {
        ui::DesktopCommand::ReloadWorkspace => {
            application.enqueue(domain::AppCommand::ReloadWorkspace);
        }
        ui::DesktopCommand::SelectProfile(profile_id) => {
            lock(&application.signals).selected_profile_id = Some(profile_id.as_str().to_owned());
        }
        ui::DesktopCommand::StartProfile(profile_id) => {
            if let Some(profile_id) = domain_profile_id(&profile_id, application) {
                application.enqueue(domain::AppCommand::StartProfile { profile_id });
            }
        }
        ui::DesktopCommand::StopProfile(profile_id) => {
            if let Some(profile_id) = domain_profile_id(&profile_id, application) {
                application.enqueue(domain::AppCommand::StopProfile { profile_id });
            }
        }
        ui::DesktopCommand::CreateProfile {
            display_name,
            pacs_server_ip,
            pacs_server_port,
            calling_ae_title,
            pacs_ae_title,
            storage_ae_title,
            storage_port,
            default_destination,
            anonymization_enabled,
        } => {
            let generated = domain::TaskId::generate();
            let profile_id = match domain::ProfileId::new(format!("profile-{generated}")) {
                Ok(profile_id) => profile_id,
                Err(error) => {
                    application.set_local_error(error.to_string());
                    return;
                }
            };
            let mut config = domain::AppConfig::default();
            apply_settings(
                &mut config,
                &pacs_server_ip,
                pacs_server_port,
                &calling_ae_title,
                &pacs_ae_title,
                &storage_ae_title,
                storage_port,
                &default_destination,
                anonymization_enabled,
            );
            lock(&application.signals).selected_profile_id = Some(profile_id.as_str().to_owned());
            application.enqueue(domain::AppCommand::UpsertProfile {
                profile: Box::new(domain::Profile {
                    id: profile_id,
                    display_name,
                    config,
                    runtime_status: domain::ProfileRuntimeStatus::Stopped,
                    source_config_path: String::new(),
                    created_at: String::new(),
                    updated_at: String::new(),
                }),
            });
        }
        ui::DesktopCommand::SaveProfile {
            profile_id,
            display_name,
            pacs_server_ip,
            pacs_server_port,
            calling_ae_title,
            pacs_ae_title,
            storage_ae_title,
            storage_port,
            default_destination,
            anonymization_enabled,
        } => {
            let Some(domain_id) = domain_profile_id(&profile_id, application) else {
                return;
            };
            let Some(mut profile) = application
                .handle
                .snapshot()
                .profiles
                .into_iter()
                .find(|profile| profile.id == domain_id)
            else {
                application.set_local_error(format!("Profile {} 不存在", profile_id.as_str()));
                return;
            };
            profile.display_name = display_name;
            apply_settings(
                &mut profile.config,
                &pacs_server_ip,
                pacs_server_port,
                &calling_ae_title,
                &pacs_ae_title,
                &storage_ae_title,
                storage_port,
                &default_destination,
                anonymization_enabled,
            );
            application.enqueue(domain::AppCommand::UpsertProfile {
                profile: Box::new(profile),
            });
        }
        ui::DesktopCommand::CreateTask {
            profile_id,
            accessions,
            destination,
        } => {
            let parsed = domain::parse_accessions(&accessions);
            if parsed.values.is_empty() || !parsed.invalid_values.is_empty() {
                application
                    .set_local_error("至少需要一个有效检查号，且检查号不能包含通配符或控制字符");
                return;
            }
            let Some(profile_id) = domain_profile_id(&profile_id, application) else {
                return;
            };
            let name = if parsed.values.len() == 1 {
                format!("影像下载 {}", parsed.values[0])
            } else {
                format!("影像下载（{} 个检查号）", parsed.values.len())
            };
            application.enqueue(domain::AppCommand::CreateTask {
                task_id: domain::TaskId::generate(),
                profile_id,
                name,
                accessions: parsed.values,
                destination,
            });
        }
        ui::DesktopCommand::OpenSettings => {}
        ui::DesktopCommand::OpenDestination(profile_id) => {
            open_profile_destination(application, &profile_id);
        }
        ui::DesktopCommand::OpenLogDirectory => {
            let Some(directory) = application.diagnostics.path.parent() else {
                application.set_local_error("诊断日志路径无父目录");
                return;
            };
            if let Err(error) = open_directory(directory) {
                application.set_local_error(error);
            }
        }
        ui::DesktopCommand::PauseTask(task_id) => {
            enqueue_task_command(application, &task_id, |task_id| {
                domain::AppCommand::PauseTask { task_id }
            });
        }
        ui::DesktopCommand::ResumeTask(task_id) => {
            enqueue_task_command(application, &task_id, |task_id| {
                domain::AppCommand::ResumeTask { task_id }
            });
        }
        ui::DesktopCommand::StartTask(task_id) => {
            enqueue_task_command(application, &task_id, |task_id| {
                domain::AppCommand::StartTask { task_id }
            });
        }
        ui::DesktopCommand::CancelTask(task_id) => {
            enqueue_task_command(application, &task_id, |task_id| {
                domain::AppCommand::CancelTask { task_id }
            });
        }
        ui::DesktopCommand::DeleteTask(task_id) => {
            enqueue_task_command(application, &task_id, |task_id| {
                domain::AppCommand::DeleteTask { task_id }
            });
        }
        ui::DesktopCommand::ToggleDetailedLogs(enabled) => {
            lock(&application.signals).detailed_logs = enabled;
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn apply_settings(
    config: &mut domain::AppConfig,
    pacs_server_ip: &str,
    pacs_server_port: u16,
    calling_ae_title: &str,
    pacs_ae_title: &str,
    storage_ae_title: &str,
    storage_port: u16,
    default_destination: &str,
    anonymization_enabled: bool,
) {
    pacs_server_ip.trim().clone_into(&mut config.pacs_server_ip);
    config.pacs_server_port = pacs_server_port;
    calling_ae_title
        .trim()
        .clone_into(&mut config.calling_ae_title);
    pacs_ae_title.trim().clone_into(&mut config.pacs_ae_title);
    storage_ae_title
        .trim()
        .clone_into(&mut config.storage_ae_title);
    config.storage_port = storage_port;
    default_destination
        .trim()
        .clone_into(&mut config.dicom_destination_folder);
    config.anonymization_enabled = anonymization_enabled;
    config.pdi_export_enabled = false;
}

fn domain_profile_id(
    profile_id: &ui::ProfileId,
    application: &ApplicationRuntime,
) -> Option<domain::ProfileId> {
    match domain::ProfileId::new(profile_id.as_str()) {
        Ok(profile_id) => Some(profile_id),
        Err(error) => {
            application.set_local_error(error.to_string());
            None
        }
    }
}

fn enqueue_task_command(
    application: &ApplicationRuntime,
    task_id: &str,
    command: impl FnOnce(domain::TaskId) -> domain::AppCommand,
) {
    match domain::TaskId::new(task_id) {
        Ok(task_id) => application.enqueue(command(task_id)),
        Err(error) => application.set_local_error(error.to_string()),
    }
}

fn open_profile_destination(application: &ApplicationRuntime, profile_id: &ui::ProfileId) {
    lock(&application.signals).last_error = None;
    let Some(profile) = application
        .handle
        .snapshot()
        .profiles
        .into_iter()
        .find(|profile| profile.id.as_str() == profile_id.as_str())
    else {
        application.set_local_error(format!("Profile {} 不存在", profile_id.as_str()));
        return;
    };
    let destination = PathBuf::from(&profile.config.dicom_destination_folder);
    if !destination.is_dir() {
        application.set_local_error(format!(
            "目标目录不存在或当前用户无法访问：{}",
            destination.display()
        ));
        return;
    }
    if let Err(error) = open_directory(&destination) {
        application.set_local_error(error);
    }
}

fn open_directory(directory: &Path) -> Result<(), String> {
    if !directory.is_dir() {
        return Err(format!(
            "目录不存在或当前用户无法访问：{}",
            directory.display()
        ));
    }
    let result = if cfg!(target_os = "windows") {
        Command::new("explorer.exe").arg(directory).spawn()
    } else if cfg!(target_os = "macos") {
        Command::new("open").arg(directory).spawn()
    } else {
        Command::new("xdg-open").arg(directory).spawn()
    };
    result
        .map(|_| ())
        .map_err(|error| format!("无法打开目录 {}：{error}", directory.display()))
}

#[allow(clippy::too_many_lines)]
fn map_snapshot(
    application: &dcmget_application::ApplicationSnapshot,
    signals: &BridgeSignals,
) -> ui::WorkspaceSnapshot {
    let selected_domain_id = signals
        .selected_profile_id
        .as_deref()
        .filter(|selected| {
            application
                .profiles
                .iter()
                .any(|profile| profile.id.as_str() == *selected)
        })
        .or_else(|| {
            application
                .profiles
                .first()
                .map(|profile| profile.id.as_str())
        });
    let selected_profile_id = selected_domain_id.map(ui::ProfileId::new);
    let tasks_by_profile = application.tasks.iter().fold(
        HashMap::<&str, Vec<&domain::TaskSummary>>::new(),
        |mut grouped, task| {
            grouped
                .entry(task.profile_id.as_str())
                .or_default()
                .push(task);
            grouped
        },
    );
    let profiles = application
        .profiles
        .iter()
        .map(|profile| {
            let profile_tasks = tasks_by_profile
                .get(profile.id.as_str())
                .map_or(&[][..], Vec::as_slice);
            let speed = profile_tasks
                .iter()
                .filter(|task| {
                    matches!(
                        task.phase,
                        domain::TaskPhase::Running | domain::TaskPhase::PausePending
                    )
                })
                .map(|task| speed_to_u64(task.speed_bytes_per_second))
                .sum();
            let active_tasks = profile_tasks
                .iter()
                .filter(|task| {
                    matches!(
                        task.phase,
                        domain::TaskPhase::Running
                            | domain::TaskPhase::PausePending
                            | domain::TaskPhase::Cancelling
                    )
                })
                .count();
            ui::ProfileSummary {
                id: ui::ProfileId::new(profile.id.as_str()),
                name: profile.display_name.clone(),
                ae_title: profile.config.storage_ae_title.clone(),
                port: profile.config.storage_port,
                status: map_profile_status(profile.runtime_status, active_tasks > 0),
                speed_bytes_per_second: speed,
                active_tasks: u32::try_from(active_tasks).unwrap_or(u32::MAX),
                settings: ui::ProfileSettings {
                    pacs_server_ip: profile.config.pacs_server_ip.clone(),
                    pacs_server_port: profile.config.pacs_server_port,
                    calling_ae_title: profile.config.calling_ae_title.clone(),
                    pacs_ae_title: profile.config.pacs_ae_title.clone(),
                    storage_ae_title: profile.config.storage_ae_title.clone(),
                    storage_port: profile.config.storage_port,
                    default_destination: profile.config.dicom_destination_folder.clone(),
                    anonymization_enabled: profile.config.anonymization_enabled,
                },
            }
        })
        .collect();
    let tasks = application
        .tasks
        .iter()
        .filter(|task| {
            selected_domain_id.is_none_or(|selected| task.profile_id.as_str() == selected)
        })
        .map(map_task_summary)
        .collect();
    let mut errors = application
        .logs
        .iter()
        .filter(|entry| signals.detailed_logs || entry.level == domain::LogLevel::Error)
        .map(map_log_entry)
        .collect::<Vec<_>>();
    if let Some(message) = signals.last_error.as_ref()
        && !errors.iter().any(|entry| entry.message == *message)
    {
        errors.push(ui::LogEntry {
            timestamp: "当前".to_owned(),
            level: ui::LogLevel::Error,
            source: "应用".to_owned(),
            message: message.clone(),
        });
    }
    errors.extend(signals.startup_warnings.iter().map(|message| ui::LogEntry {
        timestamp: "启动".to_owned(),
        level: ui::LogLevel::Warning,
        source: "迁移".to_owned(),
        message: message.clone(),
    }));
    let receiver_status = map_receiver_status(application, selected_domain_id);
    let load_error =
        (!application.startup_error.trim().is_empty()).then(|| application.startup_error.clone());
    ui::WorkspaceSnapshot {
        profiles,
        selected_profile_id,
        receiver_status,
        aggregate_speed_bytes_per_second: application.aggregate_speed_bytes_per_second,
        tasks,
        errors,
        detailed_logs_enabled: signals.detailed_logs,
        load_error,
    }
}

fn map_profile_status(
    status: domain::ProfileRuntimeStatus,
    has_active_tasks: bool,
) -> ui::ProfileStatus {
    match status {
        domain::ProfileRuntimeStatus::Stopped => ui::ProfileStatus::Stopped,
        domain::ProfileRuntimeStatus::Starting | domain::ProfileRuntimeStatus::Stopping => {
            ui::ProfileStatus::Starting
        }
        domain::ProfileRuntimeStatus::Running if has_active_tasks => ui::ProfileStatus::Busy,
        domain::ProfileRuntimeStatus::Running => ui::ProfileStatus::Ready,
        domain::ProfileRuntimeStatus::Faulted => ui::ProfileStatus::Error,
    }
}

fn map_receiver_status(
    application: &dcmget_application::ApplicationSnapshot,
    selected_profile_id: Option<&str>,
) -> ui::ReceiverStatus {
    let selected = selected_profile_id.and_then(|selected| {
        application
            .receiver_statuses
            .iter()
            .find(|status| status.profile_id.as_str() == selected)
    });
    let Some(status) = selected else {
        return ui::ReceiverStatus::Offline;
    };
    match status.state {
        domain::ReceiverState::Stopped => ui::ReceiverStatus::Offline,
        domain::ReceiverState::Starting | domain::ReceiverState::Stopping => {
            ui::ReceiverStatus::Starting
        }
        domain::ReceiverState::Listening if status.active_associations > 0 => {
            ui::ReceiverStatus::Receiving
        }
        domain::ReceiverState::Listening => ui::ReceiverStatus::Listening,
        domain::ReceiverState::Faulted => ui::ReceiverStatus::Error,
    }
}

fn map_task_summary(task: &domain::TaskSummary) -> ui::TaskSummary {
    ui::TaskSummary {
        id: task.task_id.to_string(),
        title: task.name.clone(),
        status: match task.phase {
            domain::TaskPhase::Queued => ui::TaskStatus::Waiting,
            domain::TaskPhase::Running
            | domain::TaskPhase::PdiPending
            | domain::TaskPhase::PdiRunning => ui::TaskStatus::Running,
            domain::TaskPhase::PausePending => ui::TaskStatus::Pausing,
            domain::TaskPhase::Paused => ui::TaskStatus::Paused,
            domain::TaskPhase::Cancelling => ui::TaskStatus::Cancelling,
            domain::TaskPhase::DownloadRetryable | domain::TaskPhase::Failed => {
                ui::TaskStatus::Failed
            }
            domain::TaskPhase::PdiRetryable => ui::TaskStatus::Partial,
            domain::TaskPhase::Cancelled => ui::TaskStatus::Cancelled,
            domain::TaskPhase::Completed => ui::TaskStatus::Completed,
        },
        completed: u32::try_from(task.processed_count).unwrap_or(u32::MAX),
        total: u32::try_from(task.total_count).unwrap_or(u32::MAX),
        files: task.file_count,
        speed_bytes_per_second: speed_to_u64(task.speed_bytes_per_second),
        current_accession: (!task.current_accession.is_empty())
            .then(|| task.current_accession.clone()),
        error_summary: (!task.error_message.is_empty()).then(|| task.error_message.clone()),
    }
}

fn map_log_entry(entry: &domain::LogEntry) -> ui::LogEntry {
    ui::LogEntry {
        timestamp: entry.timestamp.clone(),
        level: match entry.level {
            domain::LogLevel::Debug => ui::LogLevel::Debug,
            domain::LogLevel::Info => ui::LogLevel::Info,
            domain::LogLevel::Warning => ui::LogLevel::Warning,
            domain::LogLevel::Error => ui::LogLevel::Error,
        },
        source: entry.source.clone(),
        message: entry.message.clone(),
    }
}

#[derive(Debug)]
pub struct SmokeOptions {
    report_path: PathBuf,
    accession: String,
}

pub fn smoke_options(
    args: impl IntoIterator<Item = OsString>,
) -> Result<Option<SmokeOptions>, String> {
    let arguments = args.into_iter().collect::<Vec<_>>();
    if !arguments.iter().any(|value| value == "--backend-smoke") {
        return Ok(None);
    }
    let report_path = argument_value(&arguments, "--backend-smoke-report")
        .map(PathBuf::from)
        .ok_or_else(|| "--backend-smoke 必须同时提供 --backend-smoke-report <path>".to_owned())?;
    let accession = argument_value(&arguments, "--backend-smoke-accession")
        .and_then(|value| value.into_string().ok())
        .unwrap_or_else(|| "CI-NO-PACS-0001".to_owned());
    Ok(Some(SmokeOptions {
        report_path,
        accession,
    }))
}

fn argument_value(arguments: &[OsString], name: &str) -> Option<OsString> {
    arguments
        .iter()
        .position(|value| value == name)
        .and_then(|index| arguments.get(index + 1))
        .cloned()
}

#[allow(clippy::too_many_lines)]
pub fn run_backend_smoke(options: &SmokeOptions) -> i32 {
    let mut report = serde_json::json!({
        "schema_version": 1,
        "ok": false,
        "profile_id": "",
        "profile_count": 0,
        "legacy_source_loaded": false,
        "task_phase": "",
        "failure_observed": false,
        "receiver_started": false,
        "receiver_stopped": false,
        "port_released": false,
        "shutdown_complete": false,
        "storage_port": 0,
        "error": "",
    });
    let paths = match DesktopPaths::detect() {
        Ok(paths) => paths,
        Err(error) => return finish_smoke_report(&options.report_path, report, error),
    };
    let application = match ApplicationRuntime::start(&paths) {
        Ok(application) => application,
        Err(error) => return finish_smoke_report(&options.report_path, report, error),
    };
    report["profile_count"] = application.bootstrap_info.profile_count.into();
    report["legacy_source_loaded"] = application.bootstrap_info.legacy_source_loaded.into();

    let result = (|| -> Result<(domain::ProfileId, u16), String> {
        let initial = application.handle.snapshot();
        let profile = initial
            .profiles
            .first()
            .cloned()
            .ok_or_else(|| "smoke fixture 没有可用 Profile".to_owned())?;
        let profile_id = profile.id.clone();
        let port = profile.config.storage_port;
        report["profile_id"] = profile_id.to_string().into();
        report["storage_port"] = port.into();
        application.send_blocking(
            domain::AppCommand::StartProfile {
                profile_id: profile_id.clone(),
            },
            COMMAND_TIMEOUT,
        )?;
        wait_snapshot(&application, SMOKE_RECEIVER_TIMEOUT, |snapshot| {
            snapshot.receiver_statuses.iter().any(|status| {
                status.profile_id == profile_id && status.state == domain::ReceiverState::Listening
            })
        })
        .ok_or_else(|| {
            format!(
                "接收器未能就绪：{}",
                application.handle.snapshot().runtime_error
            )
        })?;
        report["receiver_started"] = true.into();

        let task_id = domain::TaskId::generate();
        let destination = paths.state_root.join("native").join("smoke-download");
        fs::create_dir_all(&destination).map_err(|error| {
            format!("无法创建 smoke 下载目录 {}：{error}", destination.display())
        })?;
        application.send_blocking(
            domain::AppCommand::CreateTask {
                task_id: task_id.clone(),
                profile_id: profile_id.clone(),
                name: "Native backend smoke".to_owned(),
                accessions: vec![options.accession.clone()],
                destination: destination.to_string_lossy().into_owned(),
            },
            COMMAND_TIMEOUT,
        )?;
        let terminal = wait_snapshot(&application, SMOKE_TASK_TIMEOUT, |snapshot| {
            snapshot.tasks.iter().any(|task| {
                task.task_id == task_id
                    && matches!(
                        task.phase,
                        domain::TaskPhase::DownloadRetryable | domain::TaskPhase::Failed
                    )
            })
        })
        .ok_or_else(|| {
            format!(
                "未观察到预期的无 PACS 失败状态：{}",
                application.handle.snapshot().runtime_error
            )
        })?;
        let task = terminal
            .tasks
            .iter()
            .find(|task| task.task_id == task_id)
            .ok_or_else(|| "smoke 任务状态丢失".to_owned())?;
        report["task_phase"] = task.phase.to_string().into();
        report["failure_observed"] = true.into();

        application.send_blocking(
            domain::AppCommand::StopProfile {
                profile_id: profile_id.clone(),
            },
            COMMAND_TIMEOUT,
        )?;
        let stopped = wait_snapshot(&application, SHUTDOWN_TIMEOUT, |snapshot| {
            snapshot.receiver_statuses.iter().any(|status| {
                status.profile_id == profile_id && status.state == domain::ReceiverState::Stopped
            })
        })
        .is_some();
        report["receiver_stopped"] = stopped.into();
        if !stopped {
            return Err("接收器停止超时".to_owned());
        }
        Ok((profile_id, port))
    })();

    let port = result.as_ref().ok().map(|(_, port)| *port);
    if let Err(error) = result {
        report["error"] = error.into();
    }
    match application.shutdown() {
        Ok(()) => report["shutdown_complete"] = true.into(),
        Err(error) => report["error"] = error.into(),
    }
    if let Some(port) = port {
        report["port_released"] = TcpListener::bind(SocketAddrV4::new(Ipv4Addr::LOCALHOST, port))
            .is_ok()
            .into();
    }
    let ok = report["receiver_started"].as_bool() == Some(true)
        && report["failure_observed"].as_bool() == Some(true)
        && report["receiver_stopped"].as_bool() == Some(true)
        && report["port_released"].as_bool() == Some(true)
        && report["shutdown_complete"].as_bool() == Some(true);
    report["ok"] = ok.into();
    let write_result = write_json_atomic(&options.report_path, &report);
    if let Err(error) = write_result {
        eprintln!("cannot write backend smoke report: {error}");
        return 1;
    }
    i32::from(!ok)
}

fn wait_snapshot(
    application: &ApplicationRuntime,
    timeout: Duration,
    predicate: impl Fn(&dcmget_application::ApplicationSnapshot) -> bool,
) -> Option<dcmget_application::ApplicationSnapshot> {
    let deadline = Instant::now() + timeout;
    loop {
        let snapshot = application.handle.snapshot();
        if predicate(&snapshot) {
            return Some(snapshot);
        }
        if Instant::now() >= deadline || snapshot.shutting_down {
            return None;
        }
        thread::sleep(Duration::from_millis(100));
    }
}

#[allow(
    clippy::cast_possible_truncation,
    clippy::cast_precision_loss,
    clippy::cast_sign_loss
)]
fn speed_to_u64(value: f64) -> u64 {
    if !value.is_finite() || value <= 0.0 {
        0
    } else if value >= u64::MAX as f64 {
        u64::MAX
    } else {
        value as u64
    }
}

fn finish_smoke_report(path: &Path, mut report: serde_json::Value, error: String) -> i32 {
    report["error"] = error.into();
    if let Err(write_error) = write_json_atomic(path, &report) {
        eprintln!("cannot write backend smoke report: {write_error}");
    }
    1
}

fn write_json_atomic(path: &Path, value: &serde_json::Value) -> Result<(), String> {
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent)
        .map_err(|error| format!("无法创建报告目录 {}：{error}", parent.display()))?;
    let temporary = path.with_extension("tmp");
    let bytes = serde_json::to_vec(value).map_err(|error| error.to_string())?;
    fs::write(&temporary, bytes)
        .map_err(|error| format!("无法写入报告 {}：{error}", temporary.display()))?;
    if path.exists() {
        fs::remove_file(path)
            .map_err(|error| format!("无法替换旧报告 {}：{error}", path.display()))?;
    }
    fs::rename(&temporary, path)
        .map_err(|error| format!("无法发布报告 {}：{error}", path.display()))
}

fn lock<T>(mutex: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_windows_style_roots_without_mixing_config_and_state() {
        let roaming = PathBuf::from(r"C:\Users\tester\AppData\Roaming");
        let local = PathBuf::from(r"C:\Users\tester\AppData\Local");
        let paths = DesktopPaths::from_bases(&roaming, &local);

        assert_eq!(paths.config_root, roaming.join("DcmGet"));
        assert_eq!(paths.state_root, local.join("DcmGet"));
        assert_eq!(
            paths.native_database,
            local.join("DcmGet/native/state.sqlite3")
        );
        assert_eq!(paths.backup_root, local.join("DcmGet/native/backups"));
        assert_eq!(
            paths.diagnostic_log,
            local.join("DcmGet/native/logs/dcmget-native.log")
        );
    }

    #[test]
    fn diagnostic_messages_are_bounded_to_one_line() {
        let input = format!("first\nsecond\t{}", "x".repeat(2_100));
        let line = single_line(&input);

        assert!(
            !line
                .chars()
                .any(|value| matches!(value, '\n' | '\r' | '\t'))
        );
        assert_eq!(line.chars().count(), 2_000);
        assert!(line.starts_with("first second "));
    }

    #[test]
    fn profile_settings_can_explicitly_disable_legacy_anonymization() {
        let mut config = domain::AppConfig {
            anonymization_enabled: true,
            pdi_export_enabled: true,
            ..domain::AppConfig::default()
        };

        apply_settings(
            &mut config,
            "192.0.2.10",
            104,
            "DCMGET",
            "PACS",
            "DCMGET",
            6666,
            r"D:\DICOM",
            false,
        );

        assert!(!config.anonymization_enabled);
        assert!(!config.pdi_export_enabled);
    }

    #[test]
    fn parses_windows_backend_smoke_contract() {
        let options = smoke_options([
            OsString::from("dcmget-desktop.exe"),
            OsString::from("--backend-smoke"),
            OsString::from("--exit-after-ready"),
            OsString::from("--backend-smoke-report"),
            OsString::from(r"C:\Temp\report.json"),
            OsString::from("--backend-smoke-accession"),
            OsString::from("CI-NO-PACS-0001"),
        ])
        .unwrap()
        .unwrap();

        assert_eq!(options.report_path, PathBuf::from(r"C:\Temp\report.json"));
        assert_eq!(options.accession, "CI-NO-PACS-0001");
    }
}
