#ifndef AppVersion
  #define AppVersion "3.7.5"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\build\windows\dist\DcmGet"
#endif
#ifndef ReleaseDir
  #define ReleaseDir "..\..\release\windows"
#endif
#ifndef BuildIcon
  #define BuildIcon "..\..\build\windows\dcmget.ico"
#endif
#ifndef LicenseFile
  #define LicenseFile "..\..\LICENSE"
#endif
#ifndef ChineseLanguageFile
  #define ChineseLanguageFile "compiler:Languages\ChineseSimplified.isl"
#endif
#define AppName "DcmGet"
#define AppExeName "DcmGet.exe"
; These legacy names are retained only so 3.7.5 can remove the Windows service
; installed by DcmGet 3.1.0 through 3.7.4 during upgrade or uninstall.
#define ServiceName "kayisoft-dcmget"
#define ServiceWrapperName "kayisoft-dcmget.exe"
#define ServiceConfigName "kayisoft-dcmget.xml"
#define ServiceTemplateName "kayisoft-dcmget.xml.template"
#define ServiceHostName "kayisoft-dcmget-host.ps1"
#define ServiceStateRegistryKey "Software\DcmGet\WindowsService"
#define WebView2RuntimeClientKey "SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
#define MinimumWebView2MajorVersion 111
#define FirewallRule "DcmGet Receiver TCP"
#define WebFirewallRule "DcmGet Web TCP"
#define LegacyFirewallRule "DcmGet storescp TCP"
#define LegacyPortFirewallRule "DcmGet storescp TCP 6666"

[Setup]
AppId={{40A584F5-1E96-4BA0-92DD-4543A404B586}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
UninstallDisplayName={#AppName}
AppPublisher=DcmGet contributors
AppComments=仅支持 64 位运行环境的 DICOM 下载工具，包含 DCMTK 3.7.0 与离线中文 OHIF 网页阅片器
DefaultDirName={autopf}\DcmGet
DefaultGroupName=DcmGet
DisableProgramGroupPage=yes
OutputDir={#ReleaseDir}
OutputBaseFilename=DcmGet-{#AppVersion}-Setup-x64
SetupIconFile={#BuildIcon}
UninstallDisplayIcon={app}\{#AppExeName}
LicenseFile={#LicenseFile}
PrivilegesRequired=admin
; 拒绝 32 位 Windows，同时允许 Windows 11 ARM64 通过 x64 兼容层安装和运行。
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; DcmGet processes are closed by PrepareToInstall after verifying that their
; executable paths belong to this installation.  Avoid Restart Manager's
; interactive close-applications page and never kill same-named tools elsewhere.
CloseApplications=no
RestartApplications=no
SetupLogging=yes
UsePreviousAppDir=yes
UsePreviousGroup=yes
UsePreviousTasks=yes
DisableDirPage=auto
VersionInfoVersion={#AppVersion}.0
VersionInfoProductName={#AppName}
VersionInfoDescription=DcmGet 一键安装程序

[Languages]
Name: "chinesesimp"; MessagesFile: "{#ChineseLanguageFile}"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Dirs]
Name: "{commonappdata}\DcmGet"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{localappdata}\DcmGet\logs"; Flags: uninsneveruninstall

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\{#ServiceWrapperName}"
Type: files; Name: "{app}\{#ServiceConfigName}"
Type: files; Name: "{app}\{#ServiceTemplateName}"
Type: files; Name: "{app}\{#ServiceHostName}"
Type: files; Name: "{app}\LICENSE-WINSW.txt"
Type: files; Name: "{autoprograms}\DcmGet.lnk"
Type: files; Name: "{autodesktop}\DcmGet.lnk"
Type: files; Name: "{autoprograms}\DcmGet.url"
Type: files; Name: "{autodesktop}\DcmGet.url"
Type: files; Name: "{autoprograms}\DcmGet 启动全部.lnk"
Type: files; Name: "{autoprograms}\DcmGet 停止全部.lnk"
Type: files; Name: "{autoprograms}\DcmGet 启动后台服务.lnk"
Type: files; Name: "{autoprograms}\DcmGet 停止后台服务.lnk"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
#ifdef WebView2RuntimePath
Source: "{#WebView2RuntimePath}"; DestDir: "{tmp}"; DestName: "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"; Flags: deleteafterinstall; AfterInstall: InstallWebView2Runtime
#endif
#ifdef VCRedistPath
Source: "{#VCRedistPath}"; DestDir: "{tmp}"; DestName: "vc_redist.x64.exe"; Flags: deleteafterinstall
#endif

[Icons]
Name: "{autoprograms}\DcmGet"; Filename: "{app}\{#AppExeName}"; Parameters: "--windows-desktop"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"
Name: "{autodesktop}\DcmGet"; Filename: "{app}\{#AppExeName}"; Parameters: "--windows-desktop"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{autoprograms}\DcmGet 诊断日志"; Filename: "{localappdata}\DcmGet\logs"

[Run]
#ifdef VCRedistPath
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "正在检查 Microsoft Visual C++ Runtime…"; Flags: runhidden waituntilterminated
#endif
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FirewallRule}"""; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#WebFirewallRule}"""; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#LegacyFirewallRule}"""; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#LegacyPortFirewallRule}"""; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#FirewallRule}"" dir=in action=allow program=""{app}\_internal\.runtime\dcmtk\windows-x86_64\dcmtk-3.7.0-win64-dynamic\bin\storescp.exe"" protocol=TCP profile=domain,private edge=no"; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#WebFirewallRule}"" dir=in action=allow program=""{app}\{#AppExeName}"" protocol=TCP profile=domain,private edge=no"; Flags: runhidden waituntilterminated
Filename: "{app}\{#AppExeName}"; Parameters: "--windows-desktop"; Description: "启动 DcmGet"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FirewallRule}"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveDcmGetFirewallRule"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#WebFirewallRule}"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveDcmGetWebFirewallRule"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#LegacyFirewallRule}"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveDcmGetLegacyFirewallRule"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#LegacyPortFirewallRule}"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveDcmGetLegacyPortFirewallRule"

[UninstallDelete]
Type: dirifempty; Name: "{app}\Dicom"
Type: files; Name: "{app}\{#ServiceConfigName}"
Type: files; Name: "{app}\{#ServiceTemplateName}"
Type: files; Name: "{app}\{#ServiceHostName}"
Type: files; Name: "{app}\{#ServiceWrapperName}"
Type: files; Name: "{app}\LICENSE-WINSW.txt"
Type: files; Name: "{autoprograms}\DcmGet.lnk"
Type: files; Name: "{autodesktop}\DcmGet.lnk"
Type: files; Name: "{autoprograms}\DcmGet.url"
Type: files; Name: "{autodesktop}\DcmGet.url"
Type: files; Name: "{autoprograms}\DcmGet 启动全部.lnk"
Type: files; Name: "{autoprograms}\DcmGet 停止全部.lnk"
Type: files; Name: "{autoprograms}\DcmGet 启动后台服务.lnk"
Type: files; Name: "{autoprograms}\DcmGet 停止后台服务.lnk"

[Code]

function ServiceWrapperPath(): String;
begin
  Result := ExpandConstant('{app}\{#ServiceWrapperName}');
end;

function ExecutableFromCommandLine(CommandLine: String): String;
var
  Tail: String;
  DelimiterPosition: Integer;
begin
  Result := '';
  CommandLine := Trim(CommandLine);
  if CommandLine = '' then
    Exit;
  if CommandLine[1] = '"' then
  begin
    Tail := Copy(CommandLine, 2, Length(CommandLine));
    DelimiterPosition := Pos('"', Tail);
    if DelimiterPosition = 0 then
      Exit;
    Result := Copy(Tail, 1, DelimiterPosition - 1);
  end
  else
  begin
    DelimiterPosition := Pos(' ', CommandLine);
    if DelimiterPosition = 0 then
      Result := CommandLine
    else
      Result := Copy(CommandLine, 1, DelimiterPosition - 1);
  end;
  if Result <> '' then
    Result := ExpandFileName(Result);
end;

function RegisteredServiceWrapperPath(): String;
var
  ImagePath: String;
begin
  Result := '';
  if RegQueryStringValue(
    HKLM,
    'SYSTEM\CurrentControlSet\Services\{#ServiceName}',
    'ImagePath',
    ImagePath
  ) then
    Result := ExecutableFromCommandLine(ImagePath);
end;

function DcmGetServiceBelongsToApp(): Boolean;
var
  RegisteredPath: String;
begin
  RegisteredPath := RegisteredServiceWrapperPath();
  Result := (RegisteredPath <> '') and
    (CompareText(RegisteredPath, ExpandFileName(ServiceWrapperPath())) = 0);
end;

function DcmGetServiceExists(): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(
    ExpandConstant('{sys}\sc.exe'),
    'query "{#ServiceName}"',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  ) and (ResultCode = 0);
end;

function WebView2VersionIsSupported(Version: String): Boolean;
var
  DelimiterPosition: Integer;
  MajorVersion: Integer;
begin
  Version := Trim(Version);
  if (Version = '') or (Version = '0.0.0.0') then
  begin
    Result := False;
    Exit;
  end;
  DelimiterPosition := Pos('.', Version);
  if DelimiterPosition > 0 then
    Version := Copy(Version, 1, DelimiterPosition - 1);
  MajorVersion := StrToIntDef(Version, 0);
  Result := MajorVersion >= {#MinimumWebView2MajorVersion};
end;

function WebView2RuntimeIsSupported(): Boolean;
var
  Version: String;
begin
  Result := False;
  Version := '';
  if RegQueryStringValue(
    HKLM32,
    '{#WebView2RuntimeClientKey}',
    'pv',
    Version
  ) and WebView2VersionIsSupported(Version) then
  begin
    Result := True;
    Exit;
  end;
  Version := '';
  if RegQueryStringValue(
    HKCU,
    '{#WebView2RuntimeClientKey}',
    'pv',
    Version
  ) then
    Result := WebView2VersionIsSupported(Version);
end;

procedure InstallWebView2Runtime();
var
  ResultCode: Integer;
  Attempt: Integer;
begin
  if WebView2RuntimeIsSupported() then
    Exit;
  if not Exec(
    ExpandConstant('{tmp}\MicrosoftEdgeWebView2RuntimeInstallerX64.exe'),
    '/silent /install',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  ) then
    RaiseException('无法启动 Microsoft Edge WebView2 Runtime 安装程序。');
  if ResultCode <> 0 then
    RaiseException(
      'Microsoft Edge WebView2 Runtime 安装失败，退出码 ' +
      IntToStr(ResultCode) + '。'
    );
  for Attempt := 0 to 239 do
  begin
    if WebView2RuntimeIsSupported() then
      Exit;
    Sleep(500);
  end;
  RaiseException(
    'Microsoft Edge WebView2 Runtime 未正确安装或版本低于 ' +
    IntToStr({#MinimumWebView2MajorVersion}) + '，请重新运行安装程序。'
  );
end;

procedure RequestExistingServiceStop();
var
  ResultCode: Integer;
begin
  if not DcmGetServiceExists() then
    Exit;
  Exec(
    ExpandConstant('{sys}\sc.exe'),
    'stop "{#ServiceName}"',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
end;

function RunManagedProcessCleanup(AppDir: String; var FailureMessage: String): Boolean;
var
  PowerShellPath: String;
  ScriptPath: String;
  CleanupLogPath: String;
  CleanupLines: TArrayOfString;
  CleanupIndex: Integer;
  ScriptText: String;
  Parameters: String;
  ResultCode: Integer;
begin
  FailureMessage := '';
  PowerShellPath := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  ScriptPath := ExpandConstant('{tmp}\dcmget-stop-installed-processes.ps1');
  CleanupLogPath := ExpandConstant('{tmp}\dcmget-stop-installed-processes.log');
  DeleteFile(CleanupLogPath);

  ScriptText :=
    'param([Parameter(Mandatory=$true)][string]$InstallRoot, [Parameter(Mandatory=$true)][string]$ErrorLog)' + #13#10 +
    '$ErrorActionPreference = ''Stop''' + #13#10 +
    'try {' + #13#10 +
    '$root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)' + #13#10 +
    '$rootPrefix = $root + [IO.Path]::DirectorySeparatorChar' + #13#10 +
    '$hostScript = [IO.Path]::Combine($root, ''{#ServiceHostName}'')' + #13#10 +
    '$names = @(''DcmGet.exe'', ''DcmGetPdiServer.exe'', ''storescp.exe'', ''movescu.exe'', ''{#ServiceWrapperName}'')' + #13#10 +
    'function Get-DcmGetInstalledProcess {' + #13#10 +
    '  @(Get-CimInstance Win32_Process | Where-Object {' + #13#10 +
    '    $path = [string]$_.ExecutablePath' + #13#10 +
    '    $command = [string]$_.CommandLine' + #13#10 +
    '    ($path -and ($names -contains [string]$_.Name) -and' + #13#10 +
    '      $path.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) -or' + #13#10 +
    '      ($command -and $command.IndexOf($hostScript, [StringComparison]::OrdinalIgnoreCase) -ge 0)' + #13#10 +
    '  })' + #13#10 +
    '}' + #13#10 +
    'for ($attempt = 0; $attempt -lt 20; $attempt++) {' + #13#10 +
    '  $targets = @(Get-DcmGetInstalledProcess)' + #13#10 +
    '  if ($targets.Count -eq 0) { break }' + #13#10 +
    '  foreach ($target in $targets) {' + #13#10 +
    '    $savedPreference = $ErrorActionPreference' + #13#10 +
    '    try {' + #13#10 +
    '      $ErrorActionPreference = ''SilentlyContinue''' + #13#10 +
    '      & "$env:SystemRoot\System32\taskkill.exe" /PID ([string]$target.ProcessId) /T /F 2>$null | Out-Null' + #13#10 +
    '    } finally {' + #13#10 +
    '      $ErrorActionPreference = $savedPreference' + #13#10 +
    '    }' + #13#10 +
    '  }' + #13#10 +
    '  Start-Sleep -Milliseconds 500' + #13#10 +
    '}' + #13#10 +
    '$survivors = @(Get-DcmGetInstalledProcess)' + #13#10 +
    'if ($survivors.Count -ne 0) {' + #13#10 +
    '  throw (''DcmGet processes are still running: '' + (($survivors | ForEach-Object ProcessId) -join '', ''))' + #13#10 +
    '}' + #13#10 +
    '} catch {' + #13#10 +
    '  try { ($_ | Format-List * -Force | Out-String) | Set-Content -LiteralPath $ErrorLog -Encoding UTF8 } catch {}' + #13#10 +
    '  exit 1' + #13#10 +
    '}' + #13#10;

  if not SaveStringToFile(ScriptPath, ScriptText, False) then
  begin
    FailureMessage := '无法准备 DcmGet 进程清理脚本，请检查临时目录权限。';
    Result := False;
    Exit;
  end;

  Parameters := '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' +
    AddQuotes(ScriptPath) + ' -InstallRoot ' + AddQuotes(AppDir) +
    ' -ErrorLog ' + AddQuotes(CleanupLogPath);
  if not Exec(PowerShellPath, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    FailureMessage := '无法启动 Windows PowerShell，因此不能安全结束旧版 DcmGet 进程。';
    Result := False;
    Exit;
  end;
  if ResultCode <> 0 then
  begin
    FailureMessage := '无法结束当前安装目录中的 DcmGet 相关进程，请稍后重新运行安装程序。';
    if LoadStringsFromFile(CleanupLogPath, CleanupLines) then
      for CleanupIndex := 0 to GetArrayLength(CleanupLines) - 1 do
        FailureMessage := FailureMessage + #13#10 + CleanupLines[CleanupIndex];
    Result := False;
    Exit;
  end;
  Result := True;
end;

procedure RemoveDcmGetServiceForUninstall();
var
  ResultCode: Integer;
  Attempt: Integer;
begin
  if not DcmGetServiceExists() then
    Exit;
  if not DcmGetServiceBelongsToApp() then
    RaiseException('无法卸载 kayisoft-dcmget：同名 Windows 服务不属于当前安装目录。');

  Exec(
    ExpandConstant('{sys}\sc.exe'),
    'delete "{#ServiceName}"',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
  for Attempt := 0 to 99 do
  begin
    if not DcmGetServiceExists() then
      Exit;
    Sleep(100);
  end;
  RaiseException('无法删除 kayisoft-dcmget Windows 服务，请关闭服务管理器后重试。');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  AppDir: String;
  FailureMessage: String;
begin
  Result := '';
  NeedsRestart := False;
  AppDir := ExpandConstant('{app}');
  if DcmGetServiceExists() and not DcmGetServiceBelongsToApp() then
  begin
    Result := 'Windows 中已存在同名 kayisoft-dcmget 服务，但它不属于当前安装目录。请联系管理员处理服务名称冲突。';
    Exit;
  end;
  RequestExistingServiceStop();
  if not RunManagedProcessCleanup(AppDir, FailureMessage) then
  begin
    Result := FailureMessage;
    Exit;
  end;
  RemoveDcmGetServiceForUninstall();
  RegDeleteKeyIncludingSubkeys(HKLM, '{#ServiceStateRegistryKey}');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  FailureMessage: String;
begin
  if CurUninstallStep <> usUninstall then
    Exit;
  if DcmGetServiceExists() and DcmGetServiceBelongsToApp() then
    RequestExistingServiceStop();
  if not RunManagedProcessCleanup(ExpandConstant('{app}'), FailureMessage) then
    RaiseException(FailureMessage);
  if DcmGetServiceExists() and DcmGetServiceBelongsToApp() then
    RemoveDcmGetServiceForUninstall();
  RegDeleteKeyIncludingSubkeys(HKLM, '{#ServiceStateRegistryKey}');
end;
