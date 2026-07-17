$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "未找到 Python 3.10+。请先从 https://www.python.org/downloads/windows/ 安装并勾选 Add Python to PATH。"
}
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) { throw "需要 Python 3.10 或更高版本。" }

python -m venv --clear .venv
if ($LASTEXITCODE -ne 0) { throw "创建 Python 虚拟环境失败。" }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "升级 pip 失败。" }
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "安装 Python 依赖失败。" }
& .\.venv\Scripts\python.exe scripts\download_dcmtk.py
if ($LASTEXITCODE -ne 0) { throw "下载或校验 DCMTK 失败。" }
if ($env:DCMGET_SKIP_OHIF -eq "1") {
    Write-Host "已按 DCMGET_SKIP_OHIF=1 跳过 OHIF Viewer 离线资源。"
} else {
    & .\.venv\Scripts\python.exe scripts\prepare_ohif.py
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "OHIF Viewer 离线资源准备失败；DICOMDIR 和原始 DICOM 仍可用。"
    }
}
if (-not (Test-Path config.json)) { Copy-Item config.example.json config.json }

$Runtime = Join-Path $env:WINDIR "System32\VCRUNTIME140.dll"
if (-not (Test-Path $Runtime)) {
    Write-Warning "未检测到 Microsoft Visual C++ Runtime。请安装：https://aka.ms/vs/17/release/vc_redist.x64.exe"
}

$RuleName = "DcmGet Receiver TCP"
$IsAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if ($IsAdmin) {
    $ReceiverProgram = (Resolve-Path ".venv\Scripts\python.exe").Path
    @("DcmGet Receiver TCP", "DcmGet storescp TCP", "DcmGet storescp TCP 6666") | ForEach-Object {
        Get-NetFirewallRule -DisplayName $_ -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    }
    New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow `
        -Program $ReceiverProgram -Protocol TCP `
        -Profile Domain,Private -EdgeTraversalPolicy Block | Out-Null
    Write-Host "已确认 DICOM 接收器防火墙规则：$RuleName"
} else {
    Write-Warning "当前不是管理员，未创建 DICOM 接收器防火墙规则。需要跨主机接收时，请以管理员身份重新运行本脚本。"
}

Write-Host "部署完成。运行 .\scripts\run_ui.ps1 启动 DcmGet。"
