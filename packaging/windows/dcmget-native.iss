#ifndef AppVersion
  #define AppVersion "4.0.0-preview.2"
#endif
#ifndef VersionInfoNumeric
  #define VersionInfoNumeric "4.0.0.2"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\native\dist\DcmGet-4.0.0-preview.2-windows-x64-installer"
#endif
#ifndef ReleaseDir
  #define ReleaseDir "..\..\native\release\windows"
#endif
#ifndef LicenseFile
  #define LicenseFile "..\..\LICENSE"
#endif
#ifndef ChineseLanguageFile
  #define ChineseLanguageFile "compiler:Languages\ChineseSimplified.isl"
#endif

#define AppName "DcmGet 4 Preview"
#define InstallerAppId "{{9A382E04-4A7B-42D8-AFD9-9A5BBCFB07D3}"
#define AppExeName "dcmget-desktop.exe"
#define AppCliName "dcmget-cli.exe"
#define DesktopFirewallRule "DcmGet 4 Preview Desktop Receiver TCP"
#define CliFirewallRule "DcmGet 4 Preview CLI Receiver TCP"

[Setup]
AppId={#InstallerAppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
AppPublisher=DcmGet contributors
AppComments=Windows x64 原生 GPUI 技术预览；与现有 DcmGet 独立安装
DefaultDirName={autopf}\DcmGet 4 Preview
DefaultGroupName=DcmGet 4 Preview
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
RestartApplications=no
SetupLogging=yes
UsePreviousAppDir=yes
UsePreviousGroup=yes
UsePreviousTasks=yes
DisableDirPage=auto
VersionInfoVersion={#VersionInfoNumeric}
VersionInfoProductName={#AppName}
VersionInfoDescription=DcmGet GPUI 原生技术预览安装程序
OutputDir={#ReleaseDir}
OutputBaseFilename=DcmGet-{#AppVersion}-Setup-preview-x64
LicenseFile={#LicenseFile}

[Languages]
Name: "chinesesimp"; MessagesFile: "{#ChineseLanguageFile}"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\DcmGet 4 Preview"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"
Name: "{autodesktop}\DcmGet 4 Preview"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#DesktopFirewallRule}"""; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#DesktopFirewallRule}"" dir=in action=allow program=""{app}\{#AppExeName}"" protocol=TCP profile=domain,private edge=no"; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#CliFirewallRule}"""; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#CliFirewallRule}"" dir=in action=allow program=""{app}\{#AppCliName}"" protocol=TCP profile=domain,private edge=no"; Flags: runhidden waituntilterminated
Filename: "{app}\{#AppExeName}"; Description: "启动 DcmGet 4 Preview"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#DesktopFirewallRule}"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveDcmGet4PreviewDesktopFirewallRule"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#CliFirewallRule}"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveDcmGet4PreviewCliFirewallRule"

[Code]

function StopPreviewProcesses(var FailureMessage: String): Boolean;
var
  PowerShellPath: String;
  ScriptPath: String;
  ErrorPath: String;
  ScriptText: String;
  Parameters: String;
  ResultCode: Integer;
  ErrorLines: TArrayOfString;
  ErrorIndex: Integer;
begin
  FailureMessage := '';
  PowerShellPath := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  ScriptPath := ExpandConstant('{tmp}\dcmget4-preview-stop.ps1');
  ErrorPath := ExpandConstant('{tmp}\dcmget4-preview-stop.log');
  DeleteFile(ErrorPath);

  ScriptText :=
    'param([Parameter(Mandatory=$true)][string]$InstallRoot, [Parameter(Mandatory=$true)][string]$ErrorLog)' + #13#10 +
    '$ErrorActionPreference = ''Stop''' + #13#10 +
    'try {' + #13#10 +
    '  $root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)' + #13#10 +
    '  $prefix = $root + [IO.Path]::DirectorySeparatorChar' + #13#10 +
    '  $names = @(''{#AppExeName}'', ''{#AppCliName}'')' + #13#10 +
    '  function Get-PreviewProcess {' + #13#10 +
    '    @(Get-CimInstance Win32_Process | Where-Object {' + #13#10 +
    '      $path = [string]$_.ExecutablePath' + #13#10 +
    '      $path -and ($names -contains [string]$_.Name) -and $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)' + #13#10 +
    '    })' + #13#10 +
    '  }' + #13#10 +
    '  for ($attempt = 0; $attempt -lt 20; $attempt++) {' + #13#10 +
    '    $targets = @(Get-PreviewProcess)' + #13#10 +
    '    if ($targets.Count -eq 0) { break }' + #13#10 +
    '    foreach ($target in $targets) {' + #13#10 +
    '      & "$env:SystemRoot\System32\taskkill.exe" /PID ([string]$target.ProcessId) /T /F 2>$null | Out-Null' + #13#10 +
    '    }' + #13#10 +
    '    Start-Sleep -Milliseconds 250' + #13#10 +
    '  }' + #13#10 +
    '  $survivors = @(Get-PreviewProcess)' + #13#10 +
    '  if ($survivors.Count -ne 0) { throw ''DcmGet 4 Preview processes are still running.'' }' + #13#10 +
    '} catch {' + #13#10 +
    '  try { ($_ | Format-List * -Force | Out-String) | Set-Content -LiteralPath $ErrorLog -Encoding UTF8 } catch {}' + #13#10 +
    '  exit 1' + #13#10 +
    '}' + #13#10;

  if not SaveStringToFile(ScriptPath, ScriptText, False) then
  begin
    FailureMessage := '无法准备进程清理脚本，请检查临时目录权限。';
    Result := False;
    Exit;
  end;

  Parameters := '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' +
    AddQuotes(ScriptPath) + ' -InstallRoot ' + AddQuotes(ExpandConstant('{app}')) +
    ' -ErrorLog ' + AddQuotes(ErrorPath);
  if not Exec(PowerShellPath, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    FailureMessage := '无法启动 Windows PowerShell，因此不能安全结束当前预览版进程。';
    Result := False;
    Exit;
  end;

  if ResultCode <> 0 then
  begin
    FailureMessage := '无法结束当前 DcmGet 4 Preview 进程，请关闭程序后重试。';
    if LoadStringsFromFile(ErrorPath, ErrorLines) then
      for ErrorIndex := 0 to GetArrayLength(ErrorLines) - 1 do
        FailureMessage := FailureMessage + #13#10 + ErrorLines[ErrorIndex];
    Result := False;
    Exit;
  end;

  Result := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  NeedsRestart := False;
  if not StopPreviewProcesses(Result) then
    Exit;
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  FailureMessage: String;
begin
  if CurUninstallStep <> usUninstall then
    Exit;
  if not StopPreviewProcesses(FailureMessage) then
    RaiseException(FailureMessage);
end;
