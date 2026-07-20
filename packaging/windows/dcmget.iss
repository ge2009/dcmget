#ifndef AppVersion
  #define AppVersion "3.1.0"
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
Name: "{autoprograms}\DcmGet 诊断日志"; Filename: "{localappdata}\DcmGet\logs"
Name: "{autoprograms}\DcmGet 启动全部"; Filename: "{sys}\sc.exe"; Parameters: "start {#ServiceName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"; Flags: runminimized
Name: "{autoprograms}\DcmGet 停止全部"; Filename: "{sys}\sc.exe"; Parameters: "stop {#ServiceName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"; Flags: runminimized

[INI]
Filename: "{autoprograms}\DcmGet.url"; Section: "InternetShortcut"; Key: "URL"; String: "{code:GetPrimaryWebUrl}"
Filename: "{autoprograms}\DcmGet.url"; Section: "InternetShortcut"; Key: "IconFile"; String: "{app}\{#AppExeName}"
Filename: "{autoprograms}\DcmGet.url"; Section: "InternetShortcut"; Key: "IconIndex"; String: "0"
Filename: "{autodesktop}\DcmGet.url"; Section: "InternetShortcut"; Key: "URL"; String: "{code:GetPrimaryWebUrl}"; Tasks: desktopicon
Filename: "{autodesktop}\DcmGet.url"; Section: "InternetShortcut"; Key: "IconFile"; String: "{app}\{#AppExeName}"; Tasks: desktopicon
Filename: "{autodesktop}\DcmGet.url"; Section: "InternetShortcut"; Key: "IconIndex"; String: "0"; Tasks: desktopicon

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
Filename: "{app}\{#ServiceWrapperName}"; Parameters: "stop"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "StopDcmGetService"
Filename: "{app}\{#ServiceWrapperName}"; Parameters: "uninstall"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "UninstallDcmGetService"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FirewallRule}"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveDcmGetFirewallRule"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#WebFirewallRule}"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveDcmGetWebFirewallRule"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#LegacyFirewallRule}"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveDcmGetLegacyFirewallRule"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#LegacyPortFirewallRule}"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveDcmGetLegacyPortFirewallRule"

[UninstallDelete]
Type: files; Name: "{app}\{#ServiceConfigName}"
Type: files; Name: "{app}\{#ServiceTemplateName}"
Type: files; Name: "{app}\{#ServiceHostName}"
Type: files; Name: "{app}\{#ServiceWrapperName}"
Type: files; Name: "{app}\LICENSE-WINSW.txt"
Type: files; Name: "{autoprograms}\DcmGet.url"
Type: files; Name: "{autodesktop}\DcmGet.url"

[Code]
var
  ServiceWasInstalled: Boolean;
  ServiceExistedBeforeInstall: Boolean;
  ServiceWasActiveBeforeInstall: Boolean;

function ReadConfiguredWebPort(ConfigPath: String): Integer;
var
  Content: AnsiString;
  Tail: String;
  Digits: String;
  KeyPosition: Integer;
  ColonPosition: Integer;
  CharacterIndex: Integer;
  ParsedPort: Integer;
begin
  Result := 0;
  if not LoadStringFromFile(ConfigPath, Content) then
    Exit;
  KeyPosition := Pos('"web_port"', Content);
  if KeyPosition = 0 then
    Exit;
  Tail := Copy(Content, KeyPosition + Length('"web_port"'), Length(Content));
  ColonPosition := Pos(':', Tail);
  if ColonPosition = 0 then
    Exit;
  CharacterIndex := ColonPosition + 1;
  while (CharacterIndex <= Length(Tail)) and
    ((Tail[CharacterIndex] = ' ') or (Tail[CharacterIndex] = #9) or
     (Tail[CharacterIndex] = #10) or (Tail[CharacterIndex] = #13)) do
    CharacterIndex := CharacterIndex + 1;
  Digits := '';
  while (CharacterIndex <= Length(Tail)) and
    (Tail[CharacterIndex] >= '0') and (Tail[CharacterIndex] <= '9') do
  begin
    Digits := Digits + Tail[CharacterIndex];
    CharacterIndex := CharacterIndex + 1;
  end;
  ParsedPort := StrToIntDef(Digits, 0);
  if (ParsedPort >= 1) and (ParsedPort <= 65535) then
    Result := ParsedPort;
end;

function GetPrimaryWebUrl(Param: String): String;
var
  WebPort: Integer;
begin
  WebPort := ReadConfiguredWebPort(
    ExpandConstant('{userappdata}\DcmGet\instances\i1\config.json')
  );
  if WebPort = 0 then
    WebPort := ReadConfiguredWebPort(
      ExpandConstant('{userappdata}\DcmGet\config.json')
    );
  if WebPort = 0 then
    WebPort := 8787;
  Result := 'http://127.0.0.1:' + IntToStr(WebPort) + '/';
end;

function ServiceWrapperPath(): String;
begin
  Result := ExpandConstant('{app}\{#ServiceWrapperName}');
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

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  AppDir: String;
  PowerShellPath: String;
  ScriptPath: String;
  ScriptText: String;
  Parameters: String;
  ResultCode: Integer;
begin
  Result := '';
  NeedsRestart := False;
  AppDir := ExpandConstant('{app}');
  ServiceWasInstalled := DcmGetServiceExists();
  ServiceExistedBeforeInstall := ServiceWasInstalled;
  ServiceWasActiveBeforeInstall := ServiceWasInstalled and DcmGetServiceIsActive();
  if ServiceWasInstalled and not FileExists(ServiceWrapperPath()) then
  begin
    Result := 'Windows 中已存在同名 kayisoft-dcmget 服务，但它不属于当前安装目录。请联系管理员处理服务名称冲突。';
    Exit;
  end;
  RequestExistingServiceStop();
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
    '$names = @(''DcmGet.exe'', ''DcmGetPdiServer.exe'', ''storescp.exe'', ''movescu.exe'')' + #13#10 +
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
    Result := '无法准备 DcmGet 进程清理脚本，请检查临时目录权限。';
    Exit;
  end;

  Parameters := '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' +
    AddQuotes(ScriptPath) + ' -InstallRoot ' + AddQuotes(AppDir);
  if not Exec(PowerShellPath, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Result := '无法启动 Windows PowerShell，因此不能安全结束旧版 DcmGet 进程。';
    Exit;
  end;
  if ResultCode <> 0 then
    Result := '无法结束当前安装目录中的 DcmGet 相关进程，请稍后重新运行安装程序。';
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
    StringChangeEx(Lines[Index], '@APPDATA@', XmlEscape(ExpandConstant('{userappdata}')), True);
    StringChangeEx(Lines[Index], '@LOCALAPPDATA@', XmlEscape(ExpandConstant('{localappdata}')), True);
    StringChangeEx(Lines[Index], '@USERPROFILE@', XmlEscape(InstallingUserProfile()), True);
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
    RunServiceCommand('refresh')
  else
    RunServiceCommand('install');
  ServiceWasInstalled := True;
end;
