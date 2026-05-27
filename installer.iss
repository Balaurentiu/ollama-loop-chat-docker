[Setup]
AppName=Ollama Chat
AppVersion={#AppVersion}
AppPublisher=Balaurentiu
AppPublisherURL=https://github.com/Balaurentiu/ollama-loop-chat-docker
DefaultDirName={autopf}\OllamaChat
DefaultGroupName=Ollama Chat
OutputDir=installer_out
OutputBaseFilename=OllamaChat-Setup-{#AppVersion}
SetupIconFile=assets\icon.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\OllamaChat.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Crează shortcut pe Desktop"; GroupDescription: "Shortcut-uri:"; Flags: checked
Name: "startupicon"; Description: "Pornire automată cu Windows"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
; Tray app (main exe + dependencies)
Source: "tray_dist\OllamaChat\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Flask server
Source: "server_dist\server\*"; DestDir: "{app}\server"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Ollama Chat";        Filename: "{app}\OllamaChat.exe"
Name: "{group}\Dezinstalează";      Filename: "{uninstallexe}"
Name: "{commondesktop}\Ollama Chat"; Filename: "{app}\OllamaChat.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "OllamaChat"; ValueData: """{app}\OllamaChat.exe"""; Flags: uninsdeletevalue; Tasks: startupicon

[Run]
Filename: "{app}\OllamaChat.exe"; Description: "Pornește Ollama Chat"; Flags: nowait postinstall skipifsilent
