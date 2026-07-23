#ifndef AppVersion
  #define AppVersion "3.5.2"
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
#ifndef WinSWPath
  #define WinSWPath "..\..\.runtime\winsw\v2.12.0\WinSW-x64.exe"
#endif
#ifndef WinSWLicenseFile
  #define WinSWLicenseFile "LICENSE-WINSW.txt"
#endif
#ifndef ServiceTemplateFile
  #define ServiceTemplateFile "kayisoft-dcmget.xml.template"
#endif
#ifndef ServiceHostFile
  #define ServiceHostFile "kayisoft-dcmget-host.ps1"
#endif

#define AppName "DcmGet"
#define AppExeName "DcmGet.exe"
#define ServiceName "kayisoft-dcmget"
#define ServiceWrapperName "kayisoft-dcmget.exe"
#define ServiceConfigName "kayisoft-dcmget.xml"
#define ServiceTemplateName "kayisoft-dcmget.xml.template"
#define ServiceHostName "kayisoft-dcmget-host.ps1"
#define ServiceStateRegistryKey "Software\DcmGet\WindowsService"
#define ManagementUrl "http://127.0.0.1:8786/"
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
Name: "{localappdata}\DcmGet\logs\service"; Flags: uninsneveruninstall

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

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#WinSWPath}"; DestDir: "{app}"; DestName: "{#ServiceWrapperName}"; Flags: ignoreversion
Source: "{#WinSWLicenseFile}"; DestDir: "{app}"; DestName: "LICENSE-WINSW.txt"; Flags: ignoreversion
Source: "{#ServiceHostFile}"; DestDir: "{app}"; DestName: "{#ServiceHostName}"; Flags: ignoreversion
Source: "{#ServiceTemplateFile}"; DestDir: "{app}"; DestName: "{#ServiceTemplateName}"; Flags: ignoreversion; AfterInstall: ConfigureAndInstallDcmGetService
#ifdef VCRedistPath
Source: "{#VCRedistPath}"; DestDir: "{tmp}"; DestName: "vc_redist.x64.exe"; Flags: deleteafterinstall
#endif

[Icons]
Name: "{autoprograms}\DcmGet"; Filename: "{app}\{#AppExeName}"; Parameters: "--native-shell-url ""{#ManagementUrl}"""; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"
Name: "{autodesktop}\DcmGet"; Filename: "{app}\{#AppExeName}"; Parameters: "--native-shell-url ""{#ManagementUrl}"""; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{autoprograms}\DcmGet 诊断日志"; Filename: "{localappdata}\DcmGet\logs"
Name: "{autoprograms}\DcmGet 启动后台服务"; Filename: "{sys}\sc.exe"; Parameters: "start {#ServiceName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"; Flags: runminimized
Name: "{autoprograms}\DcmGet 停止后台服务"; Filename: "{sys}\sc.exe"; Parameters: "stop {#ServiceName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"; Flags: runminimized

[Registry]
Root: HKLM; Subkey: "{#ServiceStateRegistryKey}"; ValueType: string; ValueName: "AppDataRoot"; ValueData: "{code:GetServiceAppDataRoot}"; Flags: createvalueifdoesntexist uninsdeletevalue
Root: HKLM; Subkey: "{#ServiceStateRegistryKey}"; ValueType: string; ValueName: "LocalAppDataRoot"; ValueData: "{code:GetServiceLocalAppDataRoot}"; Flags: createvalueifdoesntexist uninsdeletevalue
Root: HKLM; Subkey: "{#ServiceStateRegistryKey}"; ValueType: string; ValueName: "UserProfileRoot"; ValueData: "{code:GetServiceUserProfileRoot}"; Flags: createvalueifdoesntexist uninsdeletevalue uninsdeletekeyifempty

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
Filename: "{app}\{#ServiceWrapperName}"; Parameters: "start"; StatusMsg: "正在启动 DcmGet Windows 服务…"; Flags: runhidden waituntilterminated; Check: ShouldStartDcmGetService

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

[Code]
var
  ServiceWasInstalled: Boolean;
  ServiceExistedBeforeInstall: Boolean;
  ServiceWasActiveBeforeInstall: Boolean;
  ServiceAppDataRoot: String;
  ServiceLocalAppDataRoot: String;
  ServiceUserProfileRoot: String;

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

function DcmGetServiceIsActive(): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(
    ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoLogo -NoProfile -NonInteractive -Command "$service = Get-Service -Name ''{#ServiceName}'' -ErrorAction SilentlyContinue; if (($null -ne $service) -and ($service.Status -ne ''Stopped'')) { exit 0 } else { exit 1 }"',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  ) and (ResultCode = 0);
end;

function ShouldStartDcmGetService(): Boolean;
begin
  Result := (not ServiceExistedBeforeInstall) or ServiceWasActiveBeforeInstall;
end;

procedure RequestExistingServiceStop();
var
  ResultCode: Integer;
begin
  if not ServiceWasInstalled then
    Exit;
  if FileExists(ServiceWrapperPath()) then
    Exec(
      ServiceWrapperPath(),
      'stop',
      ExpandConstant('{app}'),
      SW_HIDE,
      ewWaitUntilTerminated,
      ResultCode
    )
  else
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
  ScriptText: String;
  Parameters: String;
  ResultCode: Integer;
begin
  FailureMessage := '';
  PowerShellPath := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  ScriptPath := ExpandConstant('{tmp}\dcmget-stop-installed-processes.ps1');

  ScriptText :=
    'param([Parameter(Mandatory=$true)][string]$InstallRoot)' + #13#10 +
    '$ErrorActionPreference = ''Stop''' + #13#10 +
    '$root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)' + #13#10 +
    '$rootPrefix = $root + [IO.Path]::DirectorySeparatorChar' + #13#10 +
    '$serviceName = ''{#ServiceName}''' + #13#10 +
    '$hostScript = [IO.Path]::Combine($root, ''{#ServiceHostName}'')' + #13#10 +
    '$service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue' + #13#10 +
    'if ($null -ne $service -and $service.Status -ne ''Stopped'') {' + #13#10 +
    '  Stop-Service -Name $serviceName -Force -ErrorAction SilentlyContinue' + #13#10 +
    '}' + #13#10 +
    'for ($attempt = 0; $attempt -lt 140; $attempt++) {' + #13#10 +
    '  $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue' + #13#10 +
    '  if ($null -eq $service -or $service.Status -eq ''Stopped'') { break }' + #13#10 +
    '  Start-Sleep -Milliseconds 250' + #13#10 +
    '}' + #13#10 +
    'if ($null -ne $service -and $service.Status -ne ''Stopped'') {' + #13#10 +
    '  throw ''kayisoft-dcmget service did not stop''' + #13#10 +
    '}' + #13#10 +
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
    'for ($attempt = 0; $attempt -lt 3; $attempt++) {' + #13#10 +
    '  $targets = @(Get-DcmGetInstalledProcess)' + #13#10 +
    '  if ($targets.Count -eq 0) { break }' + #13#10 +
    '  foreach ($target in $targets) {' + #13#10 +
    '    & "$env:SystemRoot\System32\taskkill.exe" /PID ([string]$target.ProcessId) /T /F 2>$null | Out-Null' + #13#10 +
    '  }' + #13#10 +
    '  Start-Sleep -Milliseconds 350' + #13#10 +
    '}' + #13#10 +
    '$survivors = @(Get-DcmGetInstalledProcess)' + #13#10 +
    'if ($survivors.Count -ne 0) {' + #13#10 +
    '  throw (''DcmGet processes are still running: '' + (($survivors | ForEach-Object ProcessId) -join '', ''))' + #13#10 +
    '}' + #13#10;

  if not SaveStringToFile(ScriptPath, ScriptText, False) then
  begin
    FailureMessage := '无法准备 DcmGet 进程清理脚本，请检查临时目录权限。';
    Result := False;
    Exit;
  end;

  Parameters := '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' +
    AddQuotes(ScriptPath) + ' -InstallRoot ' + AddQuotes(AppDir);
  if not Exec(PowerShellPath, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    FailureMessage := '无法启动 Windows PowerShell，因此不能安全结束旧版 DcmGet 进程。';
    Result := False;
    Exit;
  end;
  if ResultCode <> 0 then
  begin
    FailureMessage := '无法结束当前安装目录中的 DcmGet 相关进程，请稍后重新运行安装程序。';
    Result := False;
    Exit;
  end;
  Result := True;
end;

function XmlUnescape(Value: String): String;
begin
  Result := Value;
  StringChangeEx(Result, '&quot;', '"', True);
  StringChangeEx(Result, '&apos;', '''', True);
  StringChangeEx(Result, '&gt;', '>', True);
  StringChangeEx(Result, '&lt;', '<', True);
  StringChangeEx(Result, '&amp;', '&', True);
end;

function ServiceEnvironmentValue(Line: String; Name: String): String;
var
  Marker: String;
  Tail: String;
  EndPosition: Integer;
begin
  Result := '';
  Marker := '<env name="' + Name + '" value="';
  EndPosition := Pos(Marker, Line);
  if EndPosition = 0 then
    Exit;
  Tail := Copy(
    Line,
    EndPosition + Length(Marker),
    Length(Line)
  );
  EndPosition := Pos('"', Tail);
  if EndPosition = 0 then
    Exit;
  Result := XmlUnescape(Copy(Tail, 1, EndPosition - 1));
end;

procedure LoadPreservedServiceEnvironment();
var
  Lines: TArrayOfString;
  Index: Integer;
  Value: String;
begin
  RegQueryStringValue(
    HKLM,
    '{#ServiceStateRegistryKey}',
    'AppDataRoot',
    ServiceAppDataRoot
  );
  RegQueryStringValue(
    HKLM,
    '{#ServiceStateRegistryKey}',
    'LocalAppDataRoot',
    ServiceLocalAppDataRoot
  );
  RegQueryStringValue(
    HKLM,
    '{#ServiceStateRegistryKey}',
    'UserProfileRoot',
    ServiceUserProfileRoot
  );
  if (ServiceAppDataRoot <> '') and
    (ServiceLocalAppDataRoot <> '') and
    (ServiceUserProfileRoot <> '') then
    Exit;

  if not LoadStringsFromFile(
    ExpandConstant('{app}\{#ServiceConfigName}'),
    Lines
  ) then
    Exit;
  for Index := 0 to GetArrayLength(Lines) - 1 do
  begin
    if ServiceAppDataRoot = '' then
    begin
      Value := ServiceEnvironmentValue(Lines[Index], 'APPDATA');
      if Value <> '' then
        ServiceAppDataRoot := Value;
    end;
    if ServiceLocalAppDataRoot = '' then
    begin
      Value := ServiceEnvironmentValue(Lines[Index], 'LOCALAPPDATA');
      if Value <> '' then
        ServiceLocalAppDataRoot := Value;
    end;
    if ServiceUserProfileRoot = '' then
    begin
      Value := ServiceEnvironmentValue(Lines[Index], 'USERPROFILE');
      if Value <> '' then
        ServiceUserProfileRoot := Value;
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  AppDir: String;
  FailureMessage: String;
begin
  Result := '';
  NeedsRestart := False;
  AppDir := ExpandConstant('{app}');
  ServiceWasInstalled := DcmGetServiceExists();
  ServiceExistedBeforeInstall := ServiceWasInstalled;
  ServiceWasActiveBeforeInstall := ServiceWasInstalled and DcmGetServiceIsActive();
  if ServiceWasInstalled and not DcmGetServiceBelongsToApp() then
  begin
    Result := 'Windows 中已存在同名 kayisoft-dcmget 服务，但它不属于当前安装目录。请联系管理员处理服务名称冲突。';
    Exit;
  end;
  if ServiceWasInstalled then
    LoadPreservedServiceEnvironment();
  RequestExistingServiceStop();
  if not RunManagedProcessCleanup(AppDir, FailureMessage) then
    Result := FailureMessage;
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

  if FileExists(ServiceWrapperPath()) then
    Exec(
      ServiceWrapperPath(),
      'uninstall',
      ExpandConstant('{app}'),
      SW_HIDE,
      ewWaitUntilTerminated,
      ResultCode
    );
  if DcmGetServiceExists() then
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

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  FailureMessage: String;
begin
  if CurUninstallStep <> usUninstall then
    Exit;
  if not RunManagedProcessCleanup(ExpandConstant('{app}'), FailureMessage) then
    RaiseException(FailureMessage);
  RemoveDcmGetServiceForUninstall();
end;

function XmlEscape(Value: String): String;
begin
  Result := Value;
  StringChangeEx(Result, '&', '&amp;', True);
  StringChangeEx(Result, '<', '&lt;', True);
  StringChangeEx(Result, '>', '&gt;', True);
  StringChangeEx(Result, '"', '&quot;', True);
  StringChangeEx(Result, '''', '&apos;', True);
end;

function InstallingUserProfile(): String;
begin
  Result := GetEnv('USERPROFILE');
  if Result = '' then
    Result := GetEnv('HOMEDRIVE') + GetEnv('HOMEPATH');
  if Result = '' then
  begin
    Result := ExtractFileDir(ExpandConstant('{userappdata}'));
    if CompareText(ExtractFileName(Result), 'AppData') = 0 then
      Result := ExtractFileDir(Result);
  end;
end;

function GetServiceAppDataRoot(Param: String): String;
begin
  Result := ServiceAppDataRoot;
  if Result = '' then
    Result := ExpandConstant('{userappdata}');
end;

function GetServiceLocalAppDataRoot(Param: String): String;
begin
  Result := ServiceLocalAppDataRoot;
  if Result = '' then
    Result := ExpandConstant('{localappdata}');
end;

function GetServiceUserProfileRoot(Param: String): String;
begin
  Result := ServiceUserProfileRoot;
  if Result = '' then
    Result := InstallingUserProfile();
end;

procedure WriteDcmGetServiceConfig();
var
  Lines: TArrayOfString;
  Index: Integer;
  TemplatePath: String;
  ConfigPath: String;
begin
  TemplatePath := ExpandConstant('{app}\{#ServiceTemplateName}');
  ConfigPath := ExpandConstant('{app}\{#ServiceConfigName}');
  if not LoadStringsFromFile(TemplatePath, Lines) then
    RaiseException('无法读取 DcmGet Windows 服务配置模板。');
  for Index := 0 to GetArrayLength(Lines) - 1 do
  begin
    StringChangeEx(Lines[Index], '@APPDATA@', XmlEscape(GetServiceAppDataRoot('')), True);
    StringChangeEx(Lines[Index], '@LOCALAPPDATA@', XmlEscape(GetServiceLocalAppDataRoot('')), True);
    StringChangeEx(Lines[Index], '@USERPROFILE@', XmlEscape(GetServiceUserProfileRoot('')), True);
  end;
  if not SaveStringsToUTF8File(ConfigPath, Lines, False) then
    RaiseException('无法写入 DcmGet Windows 服务配置。');
end;

procedure RunServiceCommand(Command: String);
var
  ResultCode: Integer;
begin
  if not Exec(
    ServiceWrapperPath(),
    Command,
    ExpandConstant('{app}'),
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  ) then
    RaiseException('无法执行 DcmGet Windows 服务命令：' + Command + '。');
  if ResultCode <> 0 then
    RaiseException('DcmGet Windows 服务命令失败：' + Command + '，退出码 ' + IntToStr(ResultCode) + '。');
end;

procedure ConfigureAndInstallDcmGetService();
begin
  WriteDcmGetServiceConfig();
  if ServiceWasInstalled then
  begin
    { WinSW 2.12.0 does not support the newer refresh command.  Re-registering
      the stopped, app-owned service keeps repair/upgrade installs idempotent. }
    RemoveDcmGetServiceForUninstall();
    ServiceWasInstalled := False;
  end;
  RunServiceCommand('install');
  ServiceWasInstalled := True;
end;
