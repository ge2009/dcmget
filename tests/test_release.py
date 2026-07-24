from __future__ import annotations

import argparse
import hashlib
import io
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
import scripts.build_windows as windows_build
from scripts.build_windows import prepare_winsw_service_wrapper, validate_release_version


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


def test_windows_build_downloads_only_pinned_amd64_winsw(
    tmp_path: Path, monkeypatch
):
    payload_path = _write_pe(tmp_path / "source.exe", IMAGE_FILE_MACHINE_AMD64)
    payload = payload_path.read_bytes()
    expected = hashlib.sha256(payload).hexdigest()
    target = tmp_path / "runtime" / "WinSW-x64.exe"
    requests = []

    def open_fixture(request, *, timeout):
        requests.append((request.full_url, timeout))
        return io.BytesIO(payload)

    monkeypatch.setattr(windows_build, "WINSW_SHA256", expected)
    assert prepare_winsw_service_wrapper(target, opener=open_fixture) == target.resolve()
    assert target.read_bytes() == payload
    assert requests == [(windows_build.WINSW_URL, 120)]

    assert prepare_winsw_service_wrapper(
        target,
        opener=lambda *_args, **_kwargs: pytest.fail("verified WinSW was downloaded again"),
    ) == target.resolve()


def test_windows_build_rejects_winsw_checksum_mismatch(tmp_path: Path, monkeypatch):
    payload = _write_pe(tmp_path / "source.exe", IMAGE_FILE_MACHINE_AMD64).read_bytes()
    target = tmp_path / "WinSW-x64.exe"
    monkeypatch.setattr(windows_build, "WINSW_SHA256", "0" * 64)

    with pytest.raises(RuntimeError, match="WinSW v2.12.0 SHA-256"):
        prepare_winsw_service_wrapper(
            target,
            opener=lambda *_args, **_kwargs: io.BytesIO(payload),
        )
    assert not target.exists()


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
    install_test = workflow.index("Silent install, in-place upgrade and uninstall test")
    assert signing_step < install_test
    assert "sign_windows_payloads([Path(os.environ['DCMGET_SETUP_PATH'])])" in workflow
    assert "--verify-existing-signatures" in workflow


def test_windows_service_tree_fixture_uses_explicit_powershell_children():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )
    child = (root / ".github/scripts/windows-service-tree-child.ps1").read_text(
        encoding="utf-8"
    )
    fixture_section = workflow[
        workflow.index("$serviceTreeChild = Join-Path $serviceTreeFixture \"service-tree-child.ps1\"") :
        workflow.index("Builtin Users receive only query/start/stop rights.")
    ]

    assert "service-tree-child.ps1" in fixture_section
    assert "service-tree-identities.json" in fixture_section
    assert 'WindowsPowerShell\\v1.0\\powershell.exe' in fixture_section
    assert "CreationTicks = ([DateTime]$childCim.CreationDate)" in fixture_section
    assert ".github\\scripts\\windows-service-tree-child.ps1" in fixture_section
    assert '"$env:SystemRoot\\System32\\ping.exe"' in child
    assert "OwnerProcessId = $PID" in child
    assert '"ready-{0}.json" -f $Label' in child
    assert 'Copy-Item "$env:SystemRoot\\System32\\cmd.exe"' not in fixture_section
    assert '@("/d", "/c", "ping.exe -t 127.0.0.1 >NUL")' not in fixture_section


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
    assert '$upgradeWeb = Start-Process "$installDir/DcmGet.exe"' in workflow
    assert "Installed Web self-test failed" in workflow
    assert "/DAppVersion=2.0.0" not in workflow


def test_windows_installer_repairs_offline_webview2_for_native_react_shell():
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
    assert 'Start-Process "$installDir/DcmGet.exe" -ArgumentList @("--native-shell-url", "http://127.0.0.1:8786/")' in workflow
    assert "Installed native shell did not start a WebView2 process" in workflow
    assert "target: 'edge111'" in vite
    assert "DcmGet 界面正在加载" in index
    assert "修复 Microsoft Edge WebView2 Runtime" in index


def test_windows_installer_stops_only_dcmget_processes_from_install_directory():
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
    assert (
        "$names = @(''DcmGet.exe'', ''DcmGetPdiServer.exe'', ''storescp.exe'', "
        "''movescu.exe'', ''{#ServiceWrapperName}'')" in installer
    )
    assert "$attempt -lt 140" in installer
    assert installer.index("kayisoft-dcmget service did not stop") < installer.index(
        "$names = @(''DcmGet.exe''"
    )
    assert 'taskkill.exe" /PID ([string]$target.ProcessId) /T /F' in installer
    assert "Get-Process -Name" not in installer

    assert "Installer did not stop managed process" in workflow
    assert "Installer left a managed child process running" in workflow
    assert "Installer killed same-named process outside install directory" in workflow
    assert '$outsideTool = Join-Path $outsideRoot "storescp.exe"' in workflow


def test_windows_installer_manages_passwordless_winsw_service_and_all_profiles():
    root = Path(__file__).resolve().parents[1]
    installer = (root / "packaging/windows/dcmget.iss").read_text(encoding="utf-8")
    template = (root / "packaging/windows/kayisoft-dcmget.xml.template").read_text(
        encoding="utf-8"
    )
    host = (root / "packaging/windows/kayisoft-dcmget-host.ps1").read_text(
        encoding="utf-8"
    )
    workflow = (root / ".github/workflows/windows-release.yml").read_text(
        encoding="utf-8"
    )

    assert windows_build.WINSW_URL.endswith("/v2.12.0/WinSW-x64.exe")
    assert (
        windows_build.WINSW_SHA256
        == "05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da"
    )
    assert "<id>kayisoft-dcmget</id>" in template
    assert "<user>LocalSystem</user>" in template
    assert "<domain>NT AUTHORITY</domain>" not in template
    assert "<startmode>Automatic</startmode>" in template
    assert "<stopparentprocessfirst>true</stopparentprocessfirst>" in template
    assert "<securityDescriptor>" in template
    assert ";;;BU)" in template
    assert "@APPDATA@" in template and "@LOCALAPPDATA@" in template
    assert "kayisoft-dcmget-host.ps1" in template
    assert '$startInfo.Arguments = "--windows-management --no-open-browser"' in host
    assert "function Start-DcmGetManagement" in host
    assert "$managementProcess = $null" in host
    assert "$managementRetryAfter = $null" in host
    assert "function Test-CompleteProfileConfig" in host
    assert "ConvertFrom-Json -InputObject $content -ErrorAction Stop" in host
    assert "Get-ConfiguredProfileNumbers" in host
    assert '$runtimeStatePath = Join-Path $env:LOCALAPPDATA "DcmGet\\management\\profile-runtime.json"' in host
    assert "function Get-DesiredProfileNumbers" in host
    assert '$parsed.schema -ne "dcmget-profile-runtime"' in host
    assert "desired_running_profiles" in host
    assert "$startupProfileNumbers = @(Get-ConfiguredProfileNumbers)" not in host
    assert "$defaultProfilePending" not in host
    assert "$managedProfiles = @{}" in host
    assert "function Get-InstalledProfileProcesses" in host
    assert "function Update-ManagedProfiles" in host
    assert "[string]::Equals($path, $application, [StringComparison]::OrdinalIgnoreCase)" in host
    assert "--profile(?:\\s+|=)([1-9][0-9]{0,3})" in host
    assert "Adopted running DcmGet profile $number" in host
    assert 'Stop-DcmGetProcess $script:processes[[int]$number] "deleted DcmGet profile $number"' in host
    assert "Stopped supervising deleted DcmGet profile $number" in host
    assert "Stopped supervising disabled DcmGet profile $number" in host
    assert "return $true" in host
    assert "return $false" in host
    assert "Will retry stopping disabled DcmGet profile $number" in host
    assert "Will retry stopping deleted DcmGet profile $number" in host
    assert host.index("if (-not (Stop-DcmGetProcess") < host.index(
        "Stopped supervising disabled DcmGet profile $number"
    )
    assert "[DateTime]::UtcNow.AddSeconds(4)" in host
    assert "$lastDesiredProfileNumbers = @(Get-DesiredProfileNumbers)" in host
    assert "$managedProfileNumbers = @(Update-ManagedProfiles $lastDesiredProfileNumbers)" in host
    update_managed_body = host.split(
        "function Update-ManagedProfiles", 1
    )[1].split(
        'Write-Output "DcmGet service host started', 1
    )[0]
    assert "Write-Output" not in update_managed_body
    assert update_managed_body.count("Write-Host") == 3
    assert "foreach ($number in $managedProfileNumbers)" in host
    assert "$managedProfileNumbers = @(Get-ConfiguredProfileNumbers)" not in host
    assert "profile ${number}:" in host
    assert "profile $number:" not in host
    assert "while ($true)" in host
    assert "Start-Sleep -Seconds 2" in host
    assert host.index("if (-not (Test-RunningProcess $managementProcess))") < host.index(
        "$managedProfileNumbers = @(Update-ManagedProfiles $lastDesiredProfileNumbers)"
    ) < host.index("foreach ($number in $managedProfileNumbers)")
    assert "function Stop-DcmGetProcesses" in host
    assert 'Stop-DcmGetProcess $script:managementProcess "DcmGet management hub"' in host
    assert 'Stop-DcmGetProcess $process "DcmGet profile $number"' in host
    assert 'taskkill.exe" /PID ([string]$Process.Id) /T /F' in host
    assert "} finally {\n    Stop-DcmGetProcesses\n}" in host

    assert 'DestName: "{#ServiceWrapperName}"' in installer
    assert "ConfigureAndInstallDcmGetService" in installer
    configure_service = installer.split(
        "procedure ConfigureAndInstallDcmGetService();", 1
    )[1].split("\nend;", 1)[0]
    assert "RunServiceCommand('refresh')" not in installer
    assert "RemoveDcmGetServiceForUninstall();" in configure_service
    assert configure_service.index("RemoveDcmGetServiceForUninstall();") < configure_service.index(
        "RunServiceCommand('install')"
    )
    assert "  RequestExistingServiceStop();" in installer
    assert "function RunManagedProcessCleanup(AppDir: String; var FailureMessage: String): Boolean;" in installer
    assert installer.index("  RequestExistingServiceStop();") < installer.index(
        "  if not RunManagedProcessCleanup(AppDir, FailureMessage) then"
    )
    assert "procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);" in installer
    assert "if not RunManagedProcessCleanup(ExpandConstant('{app}'), FailureMessage) then" in installer
    assert "procedure RemoveDcmGetServiceForUninstall();" in installer
    assert "RemoveDcmGetServiceForUninstall();" in installer
    assert 'Type: dirifempty; Name: "{app}\\Dicom"' in installer
    assert "DcmGetServiceBelongsToApp" in installer
    assert "RegisteredServiceWrapperPath" in installer
    assert "RegQueryStringValue(" in installer
    assert "'ImagePath'" in installer
    assert "ServiceWasInstalled and not FileExists(ServiceWrapperPath())" not in installer
    assert "ServiceWasInstalled and not DcmGetServiceBelongsToApp()" in installer
    assert "'delete \"{#ServiceName}\"'" in installer
    assert "''{#ServiceWrapperName}''" in installer
    assert 'Parameters: "start"' in installer
    assert "Check: ShouldStartDcmGetService" in installer
    assert "ServiceExistedBeforeInstall" in installer
    assert "ServiceWasActiveBeforeInstall" in installer
    assert "$service.Status -ne ''Stopped''" in installer
    assert 'Name: "{autoprograms}\\DcmGet 启动后台服务"' in installer
    assert 'Name: "{autoprograms}\\DcmGet 停止后台服务"' in installer
    assert installer.count(
        'Type: files; Name: "{autoprograms}\\DcmGet 启动全部.lnk"'
    ) == 2
    assert installer.count(
        'Type: files; Name: "{autoprograms}\\DcmGet 停止全部.lnk"'
    ) == 2
    assert 'Filename: "{sys}\\sc.exe"' in installer
    assert 'Name: "{autoprograms}\\DcmGet"; Filename: "{app}\\{#AppExeName}"' in installer
    assert '#define ManagementUrl "http://127.0.0.1:8786/"' in installer
    assert installer.count('Parameters: "--native-shell-url ""{#ManagementUrl}"""') == 2
    assert 'Type: files; Name: "{autoprograms}\\DcmGet.url"' in installer
    assert 'Type: files; Name: "{autodesktop}\\DcmGet.url"' in installer
    assert '[INI]' not in installer
    assert "GetPrimaryWebUrl" not in installer
    assert "ReadConfiguredWebPort" not in installer
    assert "GetEnv('HOMEDRIVE') + GetEnv('HOMEPATH')" in installer
    assert '#define ServiceStateRegistryKey "Software\\DcmGet\\WindowsService"' in installer
    assert "procedure LoadPreservedServiceEnvironment();" in installer
    assert "ServiceEnvironmentValue" in installer
    assert "GetServiceAppDataRoot" in installer
    assert "GetServiceLocalAppDataRoot" in installer
    assert "GetServiceUserProfileRoot" in installer
    assert installer.count("Flags: createvalueifdoesntexist uninsdeletevalue") == 3
    assert "XmlEscape(GetServiceAppDataRoot(''))" in installer
    assert "XmlEscape(GetServiceLocalAppDataRoot(''))" in installer
    assert "XmlEscape(GetServiceUserProfileRoot(''))" in installer

    assert "Verify pinned WinSW service wrapper" in workflow
    assert "Verify Windows PowerShell service host syntax" in workflow
    assert "WinSW checksum mismatch" in workflow
    assert "kayisoft-dcmget" in workflow
    assert "Windows service lifecycle, upgrade-state and uninstall test" in workflow
    assert "Windows service dynamic Profile adoption test" in workflow
    assert "Windows service controls, process-tree and uninstall test" in workflow
    assert "Could not stop fixture process" in workflow
    assert "CreationTicks = [long]$record.CreationTicks" in workflow
    assert "$serviceTreeIdentities" in workflow
    assert "$directServiceTreeIdentities" in workflow
    assert "Service tree fixture did not maintain all helper processes" in workflow
    assert "Service tree fixture direct process no longer running" in workflow
    assert "-ne [long]$record.CreationTicks" in workflow
    assert "Service tree fixture did not publish all verified children" in workflow
    assert "Service tree fixture verified child no longer running" in workflow
    assert "$owner[0].ProcessId -ne [int]$record.OwnerProcessId" in workflow
    assert "Service tree process survived stop" in workflow
    assert '$fixtureLabels = @("DcmGet", "storescp", "movescu", "DcmGetPdiServer")' in workflow
    assert '[PSCustomObject]@{' in workflow and "Label = [string]$record.Label" in workflow
    assert '$opsPasswordText = "Dg!" + [Guid]::NewGuid().ToString("N").Substring(0, 11)' in workflow
    assert '$attempt -lt 120 -and (Test-Path $installDir)' in workflow
    assert "--- Remaining installation directory contents ---" in workflow
    assert 'dicom_destination_folder = $upgradeDicomDir' in workflow
    assert "$upgradeWebPort = 8787" in workflow
    assert 'Assert-FixedDcmGetPortAvailable 8786 "management"' in workflow
    assert 'Assert-FixedDcmGetPortAvailable 8787 "Profile 1"' in workflow
    assert "is occupied before installer testing" in workflow
    assert "Profile 1 Web port changed from 8787" in workflow
    assert "Service host adopted Profile 2 before config.json was complete" in workflow
    assert "Service host auto-started a cloned Profile before explicit start" in workflow
    assert "Start-AdoptAndRestartProfile" in workflow
    assert "function Set-DesiredProfiles" in workflow
    assert 'schema = "dcmget-profile-runtime"' in workflow
    assert "desired_running_profiles" in workflow
    assert "Service host did not restart adopted Profile 2" in workflow
    assert "Deleted Profile $profileNumber remained running" in workflow
    assert "Service host restarted a Profile after the operator stopped it" in workflow
    assert "Service host adopted a newly cloned Profile before explicit startup configuration" not in workflow
    assert "function Wait-DcmGetManagement" in workflow
    assert 'http://127.0.0.1:$managementPort/' in workflow
    assert '$_.LocalAddress -eq "0.0.0.0"' in workflow
    assert "DcmGet management hub did not become ready on 0.0.0.0:$managementPort" in workflow
    assert "Service-aware upgrade left old manager/profile process running" in workflow
    assert "Service-aware upgrade changed the existing user configuration" in workflow
    assert "Stopped-service upgrade changed Profile 1 configuration" in workflow
    assert "Stopped-service upgrade changed Profile 2 configuration" in workflow
    assert "Service stop left DcmGet manager/profile processes running" in workflow
    assert "Assert-NoDcmGetServiceApplications" in workflow
    assert "Installed management application is not AMD64" in workflow
    assert '"/TASKS=desktopicon"' in workflow
    assert "Installed DcmGet desktop shortcut is missing" in workflow
    assert '--native-shell-url \"http://127.0.0.1:8786/\"' in workflow
    assert "Uninstall removed or changed downloaded DICOM data" in workflow
    assert "Stopped-service upgrade unexpectedly restarted the service" in workflow
    assert "Stable service APPDATA was not registered" in workflow
    assert "Missing-wrapper repair failed" in workflow
    assert "Missing-wrapper repair unexpectedly restarted the service" in workflow
    assert "Missing-wrapper repair changed the stable service user directories" in workflow
    assert "missing-wrapper uninstall test" in workflow
    assert "Uninstall left kayisoft-dcmget service behind" in workflow
    assert "Uninstall left stable service state behind" in workflow

    workflow_lines = workflow.splitlines(keepends=True)
    run_block_lengths: list[int] = []
    for index, line in enumerate(workflow_lines):
        if line.strip() != "run: |":
            continue
        indentation = len(line) - len(line.lstrip())
        body: list[str] = []
        for candidate in workflow_lines[index + 1 :]:
            candidate_indentation = len(candidate) - len(candidate.lstrip())
            if candidate.strip() and candidate_indentation <= indentation:
                break
            body.append(candidate)
        run_block_lengths.append(len("".join(body)))
    assert run_block_lengths
    assert max(run_block_lengths) < 21_000


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
    assert 'Uninstall left the Web firewall rule behind' in workflow
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
