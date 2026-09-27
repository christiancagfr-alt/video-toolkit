[Setup]
AppName=VideoToolkit
AppVersion=1.7.65
AppPublisher=secure-artifacts
AppPublisherURL=https://github.com/secure-artifacts/video-toolkit
AppSupportURL=https://github.com/secure-artifacts/video-toolkit/issues
DefaultDirName={localappdata}\Programs\VideoToolkit
DefaultGroupName=VideoToolkit
UninstallDisplayIcon={app}\VideoToolkit.exe
Compression=lzma2
SolidCompression=yes
OutputDir=.
OutputBaseFilename=VideoToolkit_Setup_v1.7.65
SetupIconFile=logo.ico
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
WizardStyle=modern

[Files]
Source: "dist_folder\VideoToolkit\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\VideoToolkit"; Filename: "{app}\VideoToolkit.exe"
Name: "{userdesktop}\VideoToolkit"; Filename: "{app}\VideoToolkit.exe"

[Run]
Filename: "{app}\VideoToolkit.exe"; Description: "运行 VideoToolkit"; Flags: postinstall nowait
