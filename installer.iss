; VoiceRelay Windows 安装包脚本（Inno Setup 6）
; 产物：installer_output/VoiceRelay-setup.exe

[Setup]
AppId={{8E1C2A4B-7C3D-4E5F-9A0B-1D2E3F4A5B6C}
AppName=配音中转站
AppVersion=1.0.0
AppPublisher=VoiceRelay
DefaultDirName={autopf}\VoiceRelay
DefaultGroupName=配音中转站
OutputDir=installer_output
OutputBaseFilename=VoiceRelay-setup
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
WizardStyle=modern

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Files]
Source: "dist\VoiceRelay\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\配音中转站"; Filename: "{app}\VoiceRelay.exe"
Name: "{autodesktop}\配音中转站"; Filename: "{app}\VoiceRelay.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Run]
Filename: "{app}\VoiceRelay.exe"; Description: "立即运行配音中转站"; Flags: nowait postinstall skipifsilent
