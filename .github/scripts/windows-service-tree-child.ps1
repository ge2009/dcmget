param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("DcmGet", "storescp", "movescu", "DcmGetPdiServer")]
    [string]$Label
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ping = $null
$readyTemp = $null

try {
    $ping = Start-Process "$env:SystemRoot\System32\ping.exe" `
        -ArgumentList @("-t", "127.0.0.1") `
        -PassThru `
        -WindowStyle Hidden
    $pingCim = Get-CimInstance Win32_Process `
        -Filter "ProcessId = $($ping.Id)" `
        -ErrorAction Stop
    $fixtureRoot = Split-Path -Parent $PSCommandPath
    $readyPath = Join-Path $fixtureRoot ("ready-{0}.json" -f $Label)
    $readyTemp = "$readyPath.$PID.tmp"
    [PSCustomObject]@{
        Label = $Label
        OwnerProcessId = $PID
        ProcessId = $ping.Id
        CreationTicks = ([DateTime]$pingCim.CreationDate).ToUniversalTime().Ticks
    } |
        ConvertTo-Json -Compress |
        Set-Content $readyTemp -Encoding utf8
    Move-Item -LiteralPath $readyTemp -Destination $readyPath -Force
    $readyTemp = $null
    while ($true) { Start-Sleep -Seconds 1 }
} finally {
    if ($null -ne $readyTemp) {
        Remove-Item -LiteralPath $readyTemp -Force -ErrorAction SilentlyContinue
    }
    if ($null -ne $ping) {
        try {
            if (-not $ping.HasExited) {
                & "$env:SystemRoot\System32\taskkill.exe" /PID ([string]$ping.Id) /T /F 2>$null | Out-Null
                [void]$ping.WaitForExit(5000)
            }
        } catch {
            Write-Warning "Could not stop fixture ping $Label/$($ping.Id): $($_.Exception.Message)"
        }
    }
}
