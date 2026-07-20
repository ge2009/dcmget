param(
    [Parameter(Mandatory = $true)]
    [string]$Executable,
    [Parameter(Mandatory = $true)]
    [string]$AppDataRoot,
    [Parameter(Mandatory = $true)]
    [string]$LocalAppDataRoot,
    [Parameter(Mandatory = $true)]
    [string]$UserProfileRoot
)

$ErrorActionPreference = "Stop"
$env:APPDATA = [IO.Path]::GetFullPath($AppDataRoot)
$env:LOCALAPPDATA = [IO.Path]::GetFullPath($LocalAppDataRoot)
$env:USERPROFILE = [IO.Path]::GetFullPath($UserProfileRoot)
$application = [IO.Path]::GetFullPath($Executable)
if (-not (Test-Path -LiteralPath $application -PathType Leaf)) {
    throw "DcmGet executable does not exist: $application"
}

$profileRoot = Join-Path $env:APPDATA "DcmGet\instances"
$processes = @{}
$retryAfter = @{}

function Get-ConfiguredProfileNumbers {
    if (-not (Test-Path -LiteralPath $profileRoot -PathType Container)) {
        return @(1)
    }
    $numbers = @(
        Get-ChildItem -LiteralPath $profileRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Name -match '^i([1-9][0-9]{0,3})$' -and
                ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0 -and
                (Test-Path -LiteralPath (Join-Path $_.FullName "config.json") -PathType Leaf)
            } |
            ForEach-Object { [int]$_.Name.Substring(1) } |
            Sort-Object -Unique
    )
    if ($numbers.Count -eq 0) {
        return @(1)
    }
    return $numbers
}

function Test-RunningProcess([object]$Process) {
    if ($null -eq $Process) {
        return $false
    }
    try {
        return -not $Process.HasExited
    } catch {
        return $false
    }
}

function Start-DcmGetProfile([int]$Number) {
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $application
    $startInfo.Arguments = "--profile $Number --no-open-browser"
    $startInfo.WorkingDirectory = Split-Path -Parent $application
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $process = [Diagnostics.Process]::Start($startInfo)
    if ($null -eq $process) {
        throw "Could not start DcmGet profile $Number"
    }
    $script:processes[$Number] = $process
    $script:retryAfter[$Number] = [DateTime]::UtcNow.AddSeconds(10)
    Write-Output "Started DcmGet profile $Number (PID $($process.Id))."
}

Write-Output "DcmGet service host started; APPDATA=$env:APPDATA; LOCALAPPDATA=$env:LOCALAPPDATA"
$managedProfileNumbers = @(Get-ConfiguredProfileNumbers)
Write-Output "Managing startup profile snapshot: $($managedProfileNumbers -join ', ')"
while ($true) {
    foreach ($number in $managedProfileNumbers) {
        if (Test-RunningProcess $processes[$number]) {
            continue
        }
        $notBefore = $retryAfter[$number]
        if ($null -ne $notBefore -and [DateTime]::UtcNow -lt $notBefore) {
            continue
        }
        try {
            Start-DcmGetProfile $number
        } catch {
            $retryAfter[$number] = [DateTime]::UtcNow.AddSeconds(10)
            Write-Warning "Could not start DcmGet profile $number: $($_.Exception.Message)"
        }
    }
    Start-Sleep -Seconds 2
}
