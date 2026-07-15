$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "未找到 Python 3.10+。请先从 https://www.python.org/downloads/windows/ 安装并勾选 Add Python to PATH。"
}
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) { throw "需要 Python 3.10 或更高版本。" }

python -m venv --clear .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe scripts\download_dcmtk.py
if (-not (Test-Path config.json)) { Copy-Item config.example.json config.json }

$Runtime = Join-Path $env:WINDIR "System32\VCRUNTIME140.dll"
if (-not (Test-Path $Runtime)) {
    Write-Warning "未检测到 Microsoft Visual C++ Runtime。请安装：https://aka.ms/vs/17/release/vc_redist.x64.exe"
}

$Config = Get-Content config.json -Raw | ConvertFrom-Json
$RuleName = "DcmGet storescp TCP $($Config.storage_port)"
$IsAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if ($IsAdmin) {
    if (-not (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Config.storage_port | Out-Null
    }
    Write-Host "已确认 storescp 防火墙规则：$RuleName"
} else {
    Write-Warning "当前不是管理员，未创建 storescp 防火墙规则。需要跨主机接收时，请以管理员身份重新运行本脚本。"
}

Write-Host "部署完成。运行 .\scripts\run_ui.ps1 启动 DcmGet。"
