[Setup]
AppName=视频工具合集
AppVersion=1.7.71
AppPublisher=christiancagfr-alt
AppPublisherURL=https://github.com/christiancagfr-alt/video-toolkit
AppSupportURL=https://github.com/christiancagfr-alt/video-toolkit/issues
AppUpdatesURL=https://github.com/christiancagfr-alt/video-toolkit/releases/latest
DefaultDirName={localappdata}\Programs\VideoToolkit
DefaultGroupName=视频工具合集
UninstallDisplayIcon={app}\VideoToolkit.exe
Compression=lzma2
SolidCompression=yes
OutputDir=.
OutputBaseFilename=VideoToolkit_Setup_v1.7.71
SetupIconFile=logo.ico
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
WizardStyle=modern
ShowLanguageDialog=no
LanguageDetectionMethod=uilanguage

[Languages]
; 简体中文安装向导（文件与本脚本同目录，避免依赖本机 Inno 是否自带中文包）
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Files]
Source: "dist_folder\VideoToolkit\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\视频工具合集"; Filename: "{app}\VideoToolkit.exe"
Name: "{userdesktop}\视频工具合集"; Filename: "{app}\VideoToolkit.exe"

[Run]
Filename: "{app}\VideoToolkit.exe"; Description: "运行视频工具合集"; Flags: postinstall nowait
