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
$runtimeStatePath = Join-Path $env:LOCALAPPDATA "DcmGet\management\profile-runtime.json"
$managementProcess = $null
$managementRetryAfter = $null
$processes = @{}
$retryAfter = @{}
$managedProfiles = @{}
$profileMissingAfter = @{}
$lastDesiredProfileNumbers = @()

function Test-CompleteProfileConfig([string]$Path) {
    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            return $false
        }
        $content = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 -ErrorAction Stop
        if ([string]::IsNullOrWhiteSpace($content) -or -not $content.TrimStart().StartsWith("{")) {
            return $false
        }
        $parsed = ConvertFrom-Json -InputObject $content -ErrorAction Stop
        return $parsed -is [PSCustomObject]
    } catch {
        return $false
    }
}

function Get-ConfiguredProfileNumbers {
    if (-not (Test-Path -LiteralPath $profileRoot -PathType Container)) {
        return @()
    }
    $numbers = @(
        Get-ChildItem -LiteralPath $profileRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Name -match '^i([1-9][0-9]{0,3})$' -and
                ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0 -and
                (Test-CompleteProfileConfig (Join-Path $_.FullName "config.json"))
            } |
            ForEach-Object { [int]$_.Name.Substring(1) } |
            Sort-Object -Unique
    )
    return $numbers
}

function Get-DesiredProfileNumbers {
    if (-not (Test-Path -LiteralPath $runtimeStatePath -PathType Leaf)) {
        return @()
    }
    $item = Get-Item -LiteralPath $runtimeStatePath -Force -ErrorAction Stop
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Profile runtime state is a reparse point: $runtimeStatePath"
    }
    if ($item.Length -gt 65536) {
        throw "Profile runtime state is too large: $runtimeStatePath"
    }
    $content = Get-Content -LiteralPath $runtimeStatePath -Raw -Encoding UTF8 -ErrorAction Stop
    $parsed = ConvertFrom-Json -InputObject $content -ErrorAction Stop
    if (
        $null -eq $parsed -or
        $parsed.schema -ne "dcmget-profile-runtime" -or
        [int]$parsed.version -ne 1
    ) {
        throw "Profile runtime state schema is invalid: $runtimeStatePath"
    }
    $numbers = @()
    foreach ($rawNumber in @($parsed.desired_running_profiles)) {
        $number = 0
        if (-not [int]::TryParse([string]$rawNumber, [ref]$number) -or $number -lt 1 -or $number -gt 9999) {
            throw "Profile runtime state contains an invalid Profile number: $rawNumber"
        }
        $numbers += $number
    }
    return @($numbers | Sort-Object -Unique)
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

function Start-DcmGetManagement {
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $application
    $startInfo.Arguments = "--windows-management --no-open-browser"
    $startInfo.WorkingDirectory = Split-Path -Parent $application
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $process = [Diagnostics.Process]::Start($startInfo)
    if ($null -eq $process) {
        throw "Could not start DcmGet management hub"
    }
    $script:managementProcess = $process
    $script:managementRetryAfter = [DateTime]::UtcNow.AddSeconds(10)
    Write-Output "Started DcmGet management hub (PID $($process.Id))."
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

function Get-InstalledProfileProcesses {
    $records = @(
        Get-CimInstance Win32_Process -Filter "Name = 'DcmGet.exe'" -ErrorAction SilentlyContinue |
            ForEach-Object {
                $path = [string]$_.ExecutablePath
                $command = [string]$_.CommandLine
                if (
                    [string]::Equals($path, $application, [StringComparison]::OrdinalIgnoreCase) -and
                    $command -match '(?i)(?:^|\s)--profile(?:\s+|=)([1-9][0-9]{0,3})(?:\s|$)'
                ) {
                    try {
                        $process = [Diagnostics.Process]::GetProcessById([int]$_.ProcessId)
                        if (Test-RunningProcess $process) {
                            [PSCustomObject]@{
                                Number = [int]$Matches[1]
                                Process = $process
                            }
                        }
                    } catch {
                        # The process exited between the CIM snapshot and adoption.
                    }
                }
            }
    )
    return $records
}

function Stop-DcmGetProcess([object]$Process, [string]$Description) {
    if (-not (Test-RunningProcess $Process)) {
        return $true
    }
    try {
        & "$env:SystemRoot\System32\taskkill.exe" /PID ([string]$Process.Id) /T /F 2>$null | Out-Null
        $taskkillExitCode = $LASTEXITCODE
        [void]$Process.WaitForExit(5000)
        if (-not (Test-RunningProcess $Process)) {
            return $true
        }
        Write-Warning "$Description process $($Process.Id) survived taskkill (exit code $taskkillExitCode)."
        return $false
    } catch {
        Write-Warning "Could not stop $Description process $($Process.Id): $($_.Exception.Message)"
        return -not (Test-RunningProcess $Process)
    }
}

function Stop-DcmGetProcesses {
    if ($null -ne $script:managementProcess) {
        [void](Stop-DcmGetProcess $script:managementProcess "DcmGet management hub")
    }
    foreach ($number in @($script:processes.Keys)) {
        $process = $script:processes[$number]
        if ($null -eq $process) {
            continue
        }
        [void](Stop-DcmGetProcess $process "DcmGet profile $number")
    }
}

function Update-ManagedProfiles([int[]]$DesiredProfileNumbers) {
    $configuredNumbers = @(Get-ConfiguredProfileNumbers)
    $configured = @{}
    foreach ($number in $configuredNumbers) {
        $configured[[int]$number] = $true
    }
    $desired = @{}
    foreach ($number in @($DesiredProfileNumbers)) {
        $desired[[int]$number] = $true
    }

    foreach ($number in @($script:managedProfiles.Keys)) {
        if (-not $desired.ContainsKey([int]$number)) {
            if ($script:processes.ContainsKey([int]$number)) {
                if (-not (Stop-DcmGetProcess $script:processes[[int]$number] "stopped DcmGet profile $number")) {
                    Write-Warning "Will retry stopping disabled DcmGet profile $number."
                    continue
                }
            }
            [void]$script:managedProfiles.Remove([int]$number)
            [void]$script:processes.Remove([int]$number)
            [void]$script:retryAfter.Remove([int]$number)
            [void]$script:profileMissingAfter.Remove([int]$number)
            Write-Output "Stopped supervising disabled DcmGet profile $number."
            continue
        }
        if ($configured.ContainsKey([int]$number)) {
            [void]$script:profileMissingAfter.Remove([int]$number)
            continue
        }
        $removeAfter = $script:profileMissingAfter[[int]$number]
        if ($null -eq $removeAfter) {
            $script:profileMissingAfter[[int]$number] = [DateTime]::UtcNow.AddSeconds(4)
            continue
        }
        if ([DateTime]::UtcNow -lt $removeAfter) {
            continue
        }
        if ($script:processes.ContainsKey([int]$number)) {
            if (-not (Stop-DcmGetProcess $script:processes[[int]$number] "deleted DcmGet profile $number")) {
                Write-Warning "Will retry stopping deleted DcmGet profile $number."
                continue
            }
        }
        [void]$script:managedProfiles.Remove([int]$number)
        [void]$script:processes.Remove([int]$number)
        [void]$script:retryAfter.Remove([int]$number)
        [void]$script:profileMissingAfter.Remove([int]$number)
        Write-Output "Stopped supervising deleted DcmGet profile $number."
    }

    foreach ($record in @(Get-InstalledProfileProcesses)) {
        $number = [int]$record.Number
        if (-not $configured.ContainsKey($number) -or -not $desired.ContainsKey($number)) {
            continue
        }
        $tracked = $script:processes[$number]
        if ($script:managedProfiles.ContainsKey($number) -and (Test-RunningProcess $tracked)) {
            continue
        }
        $script:managedProfiles[$number] = $true
        $script:processes[$number] = $record.Process
        $script:retryAfter[$number] = [DateTime]::UtcNow.AddSeconds(10)
        Write-Output "Adopted running DcmGet profile $number (PID $($record.Process.Id))."
    }

    return @(
        $DesiredProfileNumbers |
            Where-Object { $configured.ContainsKey([int]$_) } |
            ForEach-Object { [int]$_ } |
            Sort-Object -Unique
    )
}

Write-Output "DcmGet service host started; APPDATA=$env:APPDATA; LOCALAPPDATA=$env:LOCALAPPDATA"
try {
    $lastDesiredProfileNumbers = @(Get-DesiredProfileNumbers)
} catch {
    Write-Warning "Could not read Profile runtime state at startup: $($_.Exception.Message)"
    $lastDesiredProfileNumbers = @()
}
Write-Output "Desired startup Profiles: $($lastDesiredProfileNumbers -join ', ')"
try {
    while ($true) {
        if (-not (Test-RunningProcess $managementProcess)) {
            if ($null -eq $managementRetryAfter -or [DateTime]::UtcNow -ge $managementRetryAfter) {
                try {
                    Start-DcmGetManagement
                } catch {
                    $managementRetryAfter = [DateTime]::UtcNow.AddSeconds(10)
                    Write-Warning "Could not start DcmGet management hub: $($_.Exception.Message)"
                }
            }
        }
        try {
            $lastDesiredProfileNumbers = @(Get-DesiredProfileNumbers)
        } catch {
            Write-Warning "Could not refresh Profile runtime state; keeping the last valid selection: $($_.Exception.Message)"
        }
        $managedProfileNumbers = @(Update-ManagedProfiles $lastDesiredProfileNumbers)
        foreach ($number in $managedProfileNumbers) {
            $managedProfiles[[int]$number] = $true
        }
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
                Write-Warning "Could not start DcmGet profile ${number}: $($_.Exception.Message)"
            }
        }
        Start-Sleep -Seconds 2
    }
} finally {
    Stop-DcmGetProcesses
}
