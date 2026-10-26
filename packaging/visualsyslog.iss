[Setup]
AppId={{C50A8D2A-9433-47A2-95D8-7E0F844C983A}
AppName=VisualSyslog
AppVersion=2.2.0
AppPublisher=VisualSyslog contributors
DefaultDirName={autopf}\VisualSyslog
DefaultGroupName=VisualSyslog
OutputDir=..\dist
OutputBaseFilename=VisualSyslog-Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64

[Files]
Source: "..\dist\VisualSyslog.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\VisualSyslog"; Filename: "{app}\VisualSyslog.exe"
Name: "{autodesktop}\VisualSyslog"; Filename: "{app}\VisualSyslog.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na área de trabalho"; Flags: unchecked

[Run]
Filename: "{app}\VisualSyslog.exe"; Description: "Iniciar VisualSyslog"; Flags: postinstall nowait skipifsilent
