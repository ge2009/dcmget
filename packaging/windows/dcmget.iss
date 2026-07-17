#ifndef AppVersion
  #define AppVersion "2.8.3"
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
#define FirewallRule "DcmGet Receiver TCP"
#define LegacyFirewallRule "DcmGet storescp TCP"
#define LegacyPortFirewallRule "DcmGet storescp TCP 6666"

[Setup]
AppId={{40A584F5-1E96-4BA0-92DD-4543A404B586}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
UninstallDisplayName={#AppName}
AppPublisher=DcmGet contributors
AppComments=多任务 DICOM 下载工作台，包含 DCMTK 3.7.0 与离线中文 OHIF 网页阅片器
DefaultDirName={autopf}\DcmGet
DefaultGroupName=DcmGet
DisableProgramGroupPage=yes
OutputDir={#ReleaseDir}
OutputBaseFilename=DcmGet-{#AppVersion}-Setup-x64
SetupIconFile={#BuildIcon}
UninstallDisplayIcon={app}\{#AppExeName}
LicenseFile={#LicenseFile}
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
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

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
#ifdef VCRedistPath
Source: "{#VCRedistPath}"; DestDir: "{tmp}"; DestName: "vc_redist.x64.exe"; Flags: deleteafterinstall
#endif

[Icons]
Name: "{autoprograms}\DcmGet"; Filename: "{app}\{#AppExeName}"
Name: "{autoprograms}\DcmGet 诊断日志"; Filename: "{localappdata}\DcmGet\logs"
Name: "{autodesktop}\DcmGet"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
#ifdef VCRedistPath
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "正在检查 Microsoft Visual C++ Runtime…"; Flags: runhidden waituntilterminated
#endif
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FirewallRule}"""; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#LegacyFirewallRule}"""; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#LegacyPortFirewallRule}"""; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#FirewallRule}"" dir=in action=allow program=""{app}\{#AppExeName}"" protocol=TCP profile=domain,private edge=no"; Flags: runhidden waituntilterminated
Filename: "{app}\{#AppExeName}"; Description: "启动 DcmGet"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FirewallRule}"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveDcmGetFirewallRule"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#LegacyFirewallRule}"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveDcmGetLegacyFirewallRule"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#LegacyPortFirewallRule}"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveDcmGetLegacyPortFirewallRule"
