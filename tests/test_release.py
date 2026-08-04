from __future__ import annotations

import argparse
import struct
from pathlib import Path

import pytest

from DICOM_download_ui import (
    build_parser,
    validate_frozen_pdi_resources,
    validate_web_resources,
)
from dcmget import __version__
from dcmget.architecture import (
    ArchitectureError,
    IMAGE_FILE_MACHINE_AMD64,
    IMAGE_FILE_MACHINE_ARM64,
    IMAGE_FILE_MACHINE_I386,
    ensure_supported_runtime,
    pe_machine,
    require_amd64_pe,
)
from dcmget.pdi_server import PdiRequestHandler
from dcmget.release_notes import load_release_notes
from scripts.build_deploy_bundle import VERSION as DEPLOY_VERSION, source_files
from scripts.build_windows import validate_release_version


def _write_pe(path: Path, machine: int) -> Path:
    content = bytearray(256)
    content[:2] = b"MZ"
    struct.pack_into("<I", content, 0x3C, 0x80)
    content[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", content, 0x84, machine)
    path.write_bytes(content)
    return path


def test_root_and_packaged_release_notes_stay_in_sync():
    root = Path(__file__).resolve().parents[1]

    assert (root / "CHANGELOG.md").read_bytes() == (
        root / "dcmget" / "CHANGELOG.md"
    ).read_bytes()
    assert f"## {__version__}" in load_release_notes(root)


def test_windows_build_rejects_a_version_different_from_source():
    assert validate_release_version(__version__) == __version__

    with pytest.raises(argparse.ArgumentTypeError, match="与源码版本"):
        validate_release_version("9.9.9")


def test_pe_architecture_validation_accepts_only_amd64(tmp_path: Path):
    amd64 = _write_pe(tmp_path / "amd64.exe", IMAGE_FILE_MACHINE_AMD64)
    x86 = _write_pe(tmp_path / "x86.exe", IMAGE_FILE_MACHINE_I386)
    arm64 = _write_pe(tmp_path / "arm64.exe", IMAGE_FILE_MACHINE_ARM64)

    assert pe_machine(amd64) == IMAGE_FILE_MACHINE_AMD64
    require_amd64_pe(amd64)
    with pytest.raises(ArchitectureError, match="x86/32-bit"):
        require_amd64_pe(x86)
    with pytest.raises(ArchitectureError, match="ARM64"):
        require_amd64_pe(arm64)
    with pytest.raises(ArchitectureError, match="无法读取 Windows PE"):
        pe_machine(tmp_path / "missing.exe")


def test_runtime_guard_rejects_32_bit_and_native_windows_arm64(tmp_path: Path):
    amd64 = _write_pe(tmp_path / "amd64.exe", IMAGE_FILE_MACHINE_AMD64)
    arm64 = _write_pe(tmp_path / "arm64.exe", IMAGE_FILE_MACHINE_ARM64)

    with pytest.raises(ArchitectureError, match="32 位"):
        ensure_supported_runtime(platform_name="linux", pointer_bits=32)
    ensure_supported_runtime(platform_name="linux", pointer_bits=64)
    ensure_supported_runtime(
        platform_name="win32", executable=amd64, pointer_bits=64
    )
    with pytest.raises(ArchitectureError, match="ARM64"):
        ensure_supported_runtime(
            platform_name="win32", executable=arm64, pointer_bits=64
        )


def test_source_deploy_contains_transitive_requirement_files():
    root = Path(__file__).resolve().parents[1]
    bundled = {path.relative_to(root).as_posix() for path in source_files(root)}

    assert {"requirements.txt", "requirements-dev.txt", "requirements-build.txt"} <= bundled
    assert "dcmget/architecture.py" in bundled
    assert "dcmget/nicegui_ui.py" not in bundled
    assert not any(name.startswith("dcmget/webui/") for name in bundled)
    assert "dcmget/storage_scp.py" in bundled
    assert "frontend/package.json" in bundled
    assert "frontend/package-lock.json" in bundled
    assert not any("node_modules" in Path(name).parts for name in bundled)
    assert not any(name.endswith(".tsbuildinfo") for name in bundled)


def test_pynetdicom_is_a_runtime_and_frozen_build_dependency():
    root = Path(__file__).resolve().parents[1]
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    build = (root / "scripts/build_windows.py").read_text(encoding="utf-8")
    notices = (root / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

    assert "pynetdicom>=3.0,<4" in requirements
    assert '"pynetdicom>=3.0,<4"' in project
    assert '"--collect-submodules",\n        "pynetdicom"' in build
    assert "pynetdicom" in notices


def test_brand_assets_are_real_hidpi_images_and_windows_icon_has_256px():
    root = Path(__file__).resolve().parents[1]
    logo = (root / "logo.png").read_bytes()
    assert logo[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", logo[16:24])
    assert (width, height) == (1024, 1024)
    assert logo[25] in {4, 6}  # Grayscale-alpha or RGBA.

    assert (root / "logo.icns").read_bytes()[:4] == b"icns"
    build_source = (root / "scripts" / "build_windows.py").read_text(encoding="utf-8")
    assert "(256, 256)" in build_source

    bundled = {path.relative_to(root).as_posix() for path in source_files(root)}
    assert {
        "logo.icns",
        "logo.png",
        "assets/branding/dcmget-icon-image2-source.png",
    } <= bundled


def test_react_theme_defaults_to_light_without_overriding_saved_dark_choice():
    root = Path(__file__).resolve().parents[1]
    bootstrap = (root / "frontend" / "public" / "theme.js").read_text(
        encoding="utf-8"
    )
    index = (root / "frontend" / "index.html").read_text(encoding="utf-8")

    assert "saved === 'dark'" in bootstrap
    assert "saved !== 'light'" not in bootstrap
    assert "dataset.theme = 'light'" in bootstrap
    assert 'name="theme-color" content="#fafafa"' in index


def test_release_version_sources_and_web_self_test_flags_stay_in_sync():
    root = Path(__file__).resolve().parents[1]
    windows_workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )
    entry = (root / "DICOM_download_ui.py").read_text(encoding="utf-8")

    assert DEPLOY_VERSION == __version__
    assert PdiRequestHandler.server_version == f"DcmGetPDI/{__version__}"
    assert f"default: {__version__}" in windows_workflow
    assert build_parser().parse_args(["--web-self-test"]).web_self_test
    # Keep the old automation flag as a compatibility alias for existing jobs.
    assert build_parser().parse_args(["--ui-self-test"]).web_self_test
    assert "Web self-test OK" in entry


def test_web_profile_argument_uses_the_persistent_slot_range():
    parser = build_parser()

    assert parser.parse_args(["--profile", "1"]).profile == 1
    assert parser.parse_args(["--profile", "9999"]).profile == 9999
    with pytest.raises(SystemExit):
        parser.parse_args(["--profile", "0"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--profile", "10000"])


def test_windows_release_artifacts_are_split_to_avoid_duplicate_runtime_downloads():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )

    for suffix in ("Setup-x64", "Portable-x64", "Windows-x64-ZIP"):
        assert f"DcmGet-${{{{ inputs.version }}}}-{suffix}" in workflow
    assert "name: DcmGet-${{ inputs.version }}-windows-x64\n" not in workflow


def test_windows_release_is_x64_only_and_allows_arm64_compatibility():
    root = Path(__file__).resolve().parents[1]
    installer = (root / "packaging/windows/dcmget.iss").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )
    ci = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    build = (root / "scripts/build_windows.py").read_text(encoding="utf-8")
    bootstrap = (root / "scripts/bootstrap_windows.ps1").read_text(
        encoding="utf-8"
    )
    entry = (root / "DICOM_download_ui.py").read_text(encoding="utf-8")
    cli = (root / "DICOM_download_script.py").read_text(encoding="utf-8")
    project = (root / "pyproject.toml").read_text(encoding="utf-8")

    assert "ArchitecturesAllowed=x64compatible" in installer
    assert "ArchitecturesInstallIn64BitMode=x64compatible" in installer
    assert "architecture: x64" in workflow
    assert "--verify-architecture-only" in workflow
    assert "Verify AMD64 application and DCMTK payloads" in workflow
    assert "ensure_supported_runtime()" in build
    assert "require_amd64_pe(dcmtk_bin / name" in build
    assert "verify_built_architecture(version)" in build
    assert "ensure_supported_runtime" in bootstrap
    assert "ensure_supported_runtime()" in entry
    assert "ensure_supported_runtime()" in cli
    assert "Reject 32-bit Python runtimes" in ci
    assert "actions/upload-artifact" not in ci
    assert '"fastapi>=0.139.2,<0.140"' in project
    assert '"uvicorn>=0.51,<0.52"' in project
    assert 'dcmget = ["CHANGELOG.md", "webui-react/*"]' in project


def test_offline_web_runtime_and_static_frontend_are_packaged():
    root = Path(__file__).resolve().parents[1]
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    build = (root / "scripts/build_windows.py").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert "fastapi>=0.139.2,<0.140" in requirements
    assert "nicegui" not in requirements.casefold()
    assert 'pywebview>=6.2.1,<6.3; sys_platform == "win32"' in requirements
    assert "uvicorn>=0.51,<0.52" in requirements
    assert '"fastapi>=0.139.2,<0.140"' in project
    assert "nicegui" not in project.casefold()
    assert '"pywebview>=6.2.1,<6.3; sys_platform == \'win32\'"' in project
    assert '"uvicorn>=0.51,<0.52"' in project
    assert 'f"{react_webui_root()}:dcmget/webui-react"' in build
    assert '"--collect-submodules",\n        "uvicorn"' in build
    assert '"--collect-all",\n        "nicegui"' not in build
    assert '"--collect-all",\n        "webview"' in build
    assert '"--hidden-import",\n        "dcmget.nicegui_ui"' not in build
    assert "Assert-WebResources $unpackedResourceRoot" in workflow
    assert "actions/setup-node@v4" in workflow
    assert "npm --prefix frontend ci" in workflow
    assert "npm --prefix frontend run typecheck" in workflow
    assert "npm --prefix frontend test" in workflow
    assert "npm --prefix frontend run build" in workflow
    assert "Portable EXE is missing DcmGet React Web index" in workflow
    assert "Portable EXE is missing FastAPI" in workflow
    assert "Portable EXE is missing Uvicorn" in workflow
    assert "0.0.0.0:8787" in readme
    assert "HTTP 未加密" in readme


def test_react_resource_validation_rejects_external_runtime_references(
    tmp_path: Path,
):
    webui = tmp_path / "dcmget" / "webui-react"
    webui.mkdir(parents=True)
    (webui / "index.html").write_text(
        '<!doctype html><link rel="stylesheet" href="/assets/app.css">'
        '<script src="/assets/app.js"></script>',
        encoding="utf-8",
    )
    (webui / "app.css").write_text("body{}", encoding="utf-8")
    (webui / "app.js").write_text(
        'const svgNamespace = "http://www.w3.org/2000/svg";',
        encoding="utf-8",
    )
    (webui / "theme.js").write_text("void 0;", encoding="utf-8")

    assert validate_web_resources(tmp_path) == webui

    (webui / "index.html").write_text(
        '<!doctype html><script src="https://cdn.example.invalid/app.js"></script>',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="index.html"):
        validate_web_resources(tmp_path)

    (webui / "index.html").write_text(
        '<!doctype html><link rel="stylesheet" href="/assets/app.css">'
        '<script src="/assets/app.js"></script>',
        encoding="utf-8",
    )
    (webui / "app.css").write_text(
        '@import url("https://cdn.example.invalid/app.css");',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="app.css"):
        validate_web_resources(tmp_path)

    (webui / "app.css").write_text("body{}", encoding="utf-8")
    (webui / "app.js").write_text(
        'fetch("https://api.example.invalid/task")',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="app.js"):
        validate_web_resources(tmp_path)

    (webui / "app.js").write_text("void 0;", encoding="utf-8")
    (webui / "theme.js").write_text(
        'navigator.sendBeacon("https://telemetry.example.invalid/event")',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="theme.js"):
        validate_web_resources(tmp_path)


def test_windows_release_packages_only_the_required_dcmtk_runtime():
    root = Path(__file__).resolve().parents[1]
    build = (root / "scripts/build_windows.py").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )
    downloader = (root / "scripts/download_dcmtk.py").read_text(encoding="utf-8")

    assert "stage_minimal_windows_dcmtk(PLATFORM_RUNTIME)" in build
    assert "verify_packaged_dcmtk_tree(" in build
    for name in ("movescu.exe", "storescp.exe", "dcmmkdir.exe", "dcmdump.exe"):
        assert name in build
        assert name in workflow
    assert '"dcmj2pnm",' not in downloader
    assert '"dcmdjpeg",' not in downloader
    assert "Unused dcmj2pnm.exe was packaged" in workflow
    assert "Unused dcmdjpeg.exe was packaged" in workflow
    assert "Assert-MinimalDcmtk $onedirRuntime $true" in workflow
    assert "Portable DCMTK bin allowlist mismatch" in workflow
    assert "Installed DCMTK bin allowlist mismatch" in workflow


def test_windows_release_validates_real_profile_shortcut_properties():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )

    assert "Verify real profile desktop shortcut" in workflow
    assert "default_instance_shortcut_name(6666, 'DCMGET')" in workflow
    assert "web_port=8787" in workflow
    assert '"dcmget-6666-DCMGET.url"' in workflow
    assert "URL=http://127\\.0\\.0\\.1:8787/" in workflow
    assert "WScript.Shell" in workflow
    assert "Portable EXE is missing profile shortcut support" in workflow


def test_windows_release_tests_the_signed_installer_and_only_reverifies_it():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )

    signing_step = workflow.index("Sign installer before testing exact release artifact")
    install_test = workflow.index("Silent install and in-place upgrade test")
    assert signing_step < install_test
    assert "sign_windows_payloads([Path(os.environ['DCMGET_SETUP_PATH'])])" in workflow
    assert "--verify-existing-signatures" in workflow


def test_windows_pdi_smoke_uses_authenticated_directory_entry():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )

    assert "secrets.token_urlsafe(32)" in workflow
    assert '"--session-token", $token' in workflow
    assert '"http://127.0.0.1:$port/ready/$token"' in workflow
    assert '"http://127.0.0.1:$port/open/$token" -WebSession $session' in workflow
    assert '"dicomweb:/DICOM/I000001"' in workflow
    assert "/viewer/dicomjson/" not in workflow


def test_windows_upgrade_uses_a_pinned_real_previous_release_build():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )

    assert "ref: dc5547ee4bb7884867ecc97d64e1c11d63bed5d3" in workflow
    assert "path: upgrade-baseline" in workflow
    assert 'Copy-Item -LiteralPath ".runtime\\downloads"' in workflow
    assert 'Copy-Item -LiteralPath ".runtime\\ohif\\cache"' in workflow
    assert 'python -m venv (Join-Path $baselineRoot ".venv")' in workflow
    assert '& $baselinePython -m pip install -r (Join-Path $baselineRoot "requirements-build.txt")' in workflow
    assert "& $baselinePython scripts/download_dcmtk.py --platform windows-x86_64" in workflow
    assert "Pinned 2.9.1 DCMTK preparation failed" in workflow
    assert "& $baselinePython scripts/prepare_ohif.py --offline" in workflow
    assert 'Copy-Item -LiteralPath ".runtime\\ohif" -Destination' not in workflow
    assert "& $baselinePython scripts/build_windows.py --version 2.9.1" in workflow
    assert 'Join-Path $baselineRoot "packaging\\windows\\dcmget.iss"' in workflow
    assert "DcmGet-2.9.1-Setup-x64.exe" in workflow
    assert '$baselineRecords[0].DisplayVersion -ne "2.9.1"' in workflow
    assert "config_version = 6" in workflow
    assert 'Join-Path $configDir "instances\\i1\\config.json"' in workflow
    assert "Installed 2.9.1 UI self-test failed" in workflow
    assert "Upgrade changed the existing 2.9.1 Profile 1 configuration" in workflow
    assert '$desktopManager = Start-Process "$installDir/DcmGet.exe"' in workflow
    assert 'ArgumentList "--windows-desktop"' in workflow
    assert "Installed desktop manager did not expose the management API" in workflow
    assert "/DAppVersion=2.0.0" not in workflow


def test_windows_upgrade_gate_restores_a_real_large_291_checkpoint():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )
    schemas = (root / "frontend" / "src" / "schemas.ts").read_text(
        encoding="utf-8"
    )

    assert "from dcmget.task_state import TaskCheckpointStore" in workflow
    assert '$fixturePath = Join-Path $baselineRoot ".dcmget-create-291-task.py"' in workflow
    assert 'range(1, 202)' in workflow
    assert "store.start(config, accessions, trial_required=False)" in workflow
    assert "store.record_result(" in workflow
    assert "Pinned 2.9.1 large-task checkpoint is missing" in workflow
    assert 'Invoke-RestMethod "$profileUrl/api/bootstrap"' in workflow
    assert '"$managementUrl/api/management/profiles/1/start"' in workflow
    assert "$bootstrap.task.large_batch -ne $true" in workflow
    assert 'Properties.Name -notcontains "accessions"' in workflow
    assert 'Properties.Name -notcontains "results"' in workflow
    assert "$null -ne $bootstrap.task.accessions" in workflow
    assert "$null -ne $bootstrap.task.results" in workflow
    assert 'Invoke-RestMethod "$profileUrl/api/task"' in workflow
    assert 'Invoke-RestMethod "$profileUrl/api/task/end"' in workflow
    assert "Ending the upgraded task deleted an existing DICOM result" in workflow
    assert "accessions: z.array(z.unknown()).nullish()" in schemas
    assert "results: z.array(TaskItemSchema).nullish()" in schemas


def test_windows_installer_repairs_offline_webview2_for_desktop_manager():
    root = Path(__file__).resolve().parents[1]
    installer = (root / "packaging/windows/dcmget.iss").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )
    vite = (root / "frontend" / "vite.config.ts").read_text(encoding="utf-8")
    index = (root / "frontend" / "index.html").read_text(encoding="utf-8")

    assert '#define MinimumWebView2MajorVersion 111' in installer
    assert "MicrosoftEdgeWebView2RuntimeInstallerX64.exe" in installer
    assert "AfterInstall: InstallWebView2Runtime" in installer
    assert "procedure InstallWebView2Runtime();" in installer
    assert "'/silent /install'" in installer
    assert "WebView2RuntimeIsSupported()" in installer
    assert "https://go.microsoft.com/fwlink/?linkid=2124701" in workflow
    assert "WebView2 Runtime installer is unexpectedly small" in workflow
    assert "Get-AuthenticodeSignature" in workflow
    assert 'Subject -notmatch "CN=Microsoft Corporation(?:,|$)"' in workflow
    assert '"/DWebView2RuntimePath=$webview2"' in workflow
    assert "Upgrade did not install the bundled WebView2 Runtime" in workflow
    assert (
        'Start-Process "$installDir/DcmGet.exe" '
        '-ArgumentList "--windows-desktop"' in workflow
    )
    assert "did not start a WebView2 process" in workflow
    assert "target: 'edge111'" in vite
    assert "DcmGet 界面正在加载" in index
    assert "修复 Microsoft Edge WebView2 Runtime" in index


def test_windows_installer_process_cleanup_is_limited_to_install_directory():
    root = Path(__file__).resolve().parents[1]
    installer = (root / "packaging/windows/dcmget.iss").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )

    assert "CloseApplications=no" in installer
    assert "function PrepareToInstall(var NeedsRestart: Boolean): String;" in installer
    assert "Get-CimInstance Win32_Process" in installer
    assert "ExecutablePath" in installer
    assert "$path.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)" in installer
    for name in ("DcmGet.exe", "DcmGetPdiServer.exe", "storescp.exe", "movescu.exe"):
        assert name in installer
    assert 'taskkill.exe" /PID ([string]$target.ProcessId) /T /F' in installer
    assert "Get-Process -Name" not in installer

    assert "Installer did not stop managed process" in workflow
    assert "Installer left a managed child process running" in workflow
    assert "Installer killed same-named process outside install directory" in workflow
    assert '$outsideTool = Join-Path $outsideRoot "storescp.exe"' in workflow


def test_windows_installer_uses_desktop_entrypoint_without_service_installation():
    root = Path(__file__).resolve().parents[1]
    installer = (root / "packaging/windows/dcmget.iss").read_text(encoding="utf-8")
    files_section = installer.split("[Files]", 1)[1].split("[Icons]", 1)[0]
    icons_section = installer.split("[Icons]", 1)[1].split("[Run]", 1)[0]
    run_section = installer.split("[Run]", 1)[1].split("[UninstallRun]", 1)[0]

    assert build_parser().parse_args(["--windows-desktop"]).windows_desktop
    assert icons_section.count('Parameters: "--windows-desktop"') == 2
    assert (
        'Filename: "{app}\\{#AppExeName}"; Parameters: "--windows-desktop"'
        in run_section
    )
    assert "--native-shell-url" not in installer
    assert 'Name: "{autoprograms}\\DcmGet 启动后台服务"' not in icons_section
    assert 'Name: "{autoprograms}\\DcmGet 停止后台服务"' not in icons_section

    for service_install_marker in (
        "#ifndef WinSWPath",
        'Source: "{#WinSWPath}"',
        'DestName: "{#ServiceWrapperName}"',
        "ConfigureAndInstallDcmGetService",
        "RunServiceCommand('install')",
        "Check: ShouldStartDcmGetService",
        "ServiceExistedBeforeInstall",
        "ServiceWasActiveBeforeInstall",
        "New-Service",
        "Start-Service",
        "'create \"{#ServiceName}\"'",
    ):
        assert service_install_marker not in files_section
        assert service_install_marker not in installer
    assert 'Parameters: "start {#ServiceName}"' not in installer
    assert "[Registry]" not in installer


def test_windows_installer_only_removes_an_owned_legacy_service():
    root = Path(__file__).resolve().parents[1]
    installer = (root / "packaging/windows/dcmget.iss").read_text(encoding="utf-8")
    install_delete = installer.split("[InstallDelete]", 1)[1].split("[Files]", 1)[0]
    uninstall_delete = installer.split("[UninstallDelete]", 1)[1].split("[Code]", 1)[0]

    assert "installed by DcmGet 3.1.0 through 3.7.4" in installer
    for legacy_file in (
        "{#ServiceWrapperName}",
        "{#ServiceConfigName}",
        "{#ServiceTemplateName}",
        "{#ServiceHostName}",
        "LICENSE-WINSW.txt",
    ):
        assert legacy_file in install_delete
        assert legacy_file in uninstall_delete

    assert "function RegisteredServiceWrapperPath(): String;" in installer
    assert "'SYSTEM\\CurrentControlSet\\Services\\{#ServiceName}'" in installer
    assert "'ImagePath'" in installer
    assert (
        "CompareText(RegisteredPath, ExpandFileName(ServiceWrapperPath())) = 0"
        in installer
    )

    prepare = installer.split(
        "function PrepareToInstall(var NeedsRestart: Boolean): String;", 1
    )[1].split("procedure RemoveDcmGetServiceForUninstall();", 1)[0]
    assert "DcmGetServiceExists() and not DcmGetServiceBelongsToApp()" in prepare
    assert prepare.index("not DcmGetServiceBelongsToApp()") < prepare.index(
        "RequestExistingServiceStop();"
    ) < prepare.index("RemoveDcmGetServiceForUninstall();")
    assert "RegDeleteKeyIncludingSubkeys(HKLM, '{#ServiceStateRegistryKey}')" in prepare

    remove = installer.rsplit(
        "procedure RemoveDcmGetServiceForUninstall();", 1
    )[1].split(
        "procedure CurUninstallStepChanged", 1
    )[0]
    assert "if not DcmGetServiceBelongsToApp() then" in remove
    assert remove.index("if not DcmGetServiceBelongsToApp() then") < remove.index(
        "'uninstall'"
    ) < remove.index("'delete \"{#ServiceName}\"'")


def test_windows_build_and_release_workflow_have_no_active_winsw_dependency():
    root = Path(__file__).resolve().parents[1]
    build = (root / "scripts/build_windows.py").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )

    assert "winsw" not in build.casefold()
    for active_dependency in (
        ".runtime/winsw",
        ".runtime\\winsw",
        "/DWinSWPath=",
        "Verify pinned WinSW service wrapper",
        "WinSW checksum mismatch",
        "WinSW-x64.exe",
    ):
        assert active_dependency.casefold() not in workflow.casefold()
    assert "--windows-desktop" in workflow
    assert "Windows desktop lifecycle and uninstall test" in workflow


def test_windows_firewall_is_limited_to_web_receiver_and_private_networks():
    root = Path(__file__).resolve().parents[1]
    installer = (root / "packaging/windows/dcmget.iss").read_text(encoding="utf-8")
    bootstrap = (root / "scripts/bootstrap_windows.ps1").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )

    assert (
        'program=""{app}\\_internal\\.runtime\\dcmtk\\windows-x86_64'
        '\\dcmtk-3.7.0-win64-dynamic\\bin\\storescp.exe""' in installer
    )
    assert "profile=domain,private" in installer
    assert "profile=public" not in installer.lower()
    assert 'program=""{app}\\{#AppExeName}""' in installer
    assert 'localport=6666' not in installer
    assert '#define FirewallRule "DcmGet Receiver TCP"' in installer
    assert '#define WebFirewallRule "DcmGet Web TCP"' in installer
    assert '#define LegacyFirewallRule "DcmGet storescp TCP"' in installer
    assert '#define LegacyPortFirewallRule "DcmGet storescp TCP 6666"' in installer
    assert "-Program $ReceiverProgram" in bootstrap
    assert "-Program $WebProgram" in bootstrap
    assert "-LocalPort" not in bootstrap
    assert "-Profile Domain,Private" in bootstrap
    assert '$RuleName = "DcmGet Receiver TCP"' in bootstrap
    assert '$WebRuleName = "DcmGet Web TCP"' in bootstrap
    assert '-Filter "storescp.exe" -File' in bootstrap
    assert 'Assert-DcmGetFirewallRule "DcmGet Receiver TCP" $expectedReceiver' in workflow
    assert 'Assert-DcmGetFirewallRule "DcmGet Web TCP" $expectedWeb' in workflow
    assert '$rules.Count -ne 1' in workflow
    assert '$portFilters[0].LocalPort.ToString() -ne "Any"' in workflow
    assert "storage_port = 16666" in workflow
    assert "Upgrade left the legacy storescp program rule behind" in workflow
    assert "Upgrade left the legacy TCP 6666 firewall rule behind" in workflow
    assert '$applicationFilters.Count -ne 1' in workflow
    assert (
        'Join-Path $installDir "_internal\\.runtime\\dcmtk\\windows-x86_64'
        '\\dcmtk-3.7.0-win64-dynamic\\bin\\storescp.exe"' in workflow
    )
    assert "[StringComparison]::OrdinalIgnoreCase" in workflow
    assert '$profileNames.Count -ne 2' in workflow
    assert '$profileNames -notcontains "Domain"' in workflow
    assert '$profileNames -notcontains "Private"' in workflow
    assert '$rule.Direction.ToString() -ne "Inbound"' in workflow
    assert '$rule.Action.ToString() -ne "Allow"' in workflow
    assert '$rule.Enabled.ToString() -ne "True"' in workflow
    assert '$rule.EdgeTraversalPolicy.ToString() -ne "Block"' in workflow
    assert 'RunOnceId: "RemoveDcmGetWebFirewallRule"' in installer
    assert "DCMGET_PAYLOAD.SHA256" in workflow


def test_frozen_self_test_requires_offline_ohif_and_local_server(
    tmp_path: Path, monkeypatch
):
    import DICOM_download_ui as entry

    monkeypatch.setattr(entry, "is_frozen", lambda: True)
    with pytest.raises(RuntimeError, match="PDI 离线资源缺失"):
        validate_frozen_pdi_resources(tmp_path)

    ohif = tmp_path / ".runtime" / "ohif" / "ohif-3.12.6"
    ohif.mkdir(parents=True)
    (tmp_path / "DcmGetPdiServer.exe").write_bytes(b"server")
    server_script = tmp_path / "dcmget" / "pdi_server.py"
    server_script.parent.mkdir()
    server_script.write_text("# offline server\n", encoding="utf-8")
    (tmp_path / "dcmget" / "architecture.py").write_text(
        "# architecture guard\n", encoding="utf-8"
    )
    for name in (
        "index.html",
        "app-config.js",
        "init-service-worker.js",
        "LICENSE-OHIF.txt",
        "THIRD_PARTY-OHIF.md",
        "DCMGET_OHIF_PAYLOAD.json",
        "DCMGET_PAYLOAD.SHA256",
    ):
        (ohif / name).write_text("offline", encoding="utf-8")

    validate_frozen_pdi_resources(tmp_path)
    (ohif / "app-config.js").write_text("https://remote.invalid", encoding="utf-8")
    with pytest.raises(RuntimeError, match="外部地址"):
        validate_frozen_pdi_resources(tmp_path)
