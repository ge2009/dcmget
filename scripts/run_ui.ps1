$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
& .\.venv\Scripts\python.exe DICOM_download_ui.py --config config.json
exit $LASTEXITCODE
