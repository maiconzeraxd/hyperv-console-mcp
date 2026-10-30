; Hyper-V Console: the installer Inno Setup wraps around the frozen folder.
;
; Per-user on purpose. The files land under %LOCALAPPDATA%\Programs and
; installing asks for no elevation, which matters for a tool whose first screen
; should be the window and not a UAC prompt. The launcher itself is built with
; a requireAdministrator manifest, so opening it after the install asks for
; elevation exactly once. The setup routine may still enable Hyper-V, which
; needs a restart it does not take on its own.
;
; Built by .github/workflows/release.yml:
;
;   ISCC.exe /DAppVersion=0.1.0 /DSourceDir=..\dist\HyperVConsoleMCP \
;            /DSetupIcon=..\dist\icon.ico installer.iss

#define AppName "Hyper-V Console"
#define AppPublisher "hyperv-console-mcp contributors"
#define AppUrl "https://github.com/maiconzeraxd/hyperv-console-mcp"
#define Launcher "HyperVConsoleMCP.exe"
#define SetupExe "hyperv-setup.exe"

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\HyperVConsoleMCP"
#endif
#ifndef SetupIcon
  #define SetupIcon "..\dist\icon.ico"
#endif

[Setup]
; Never change this GUID: it is how Windows recognises an upgrade as the same
; program rather than a second one beside it.
AppId={{7F3B2C4E-9A61-4B7D-8E52-1C6D9A0B4F31}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}/blob/main/docs/SETUP.md
AppUpdatesURL={#AppUrl}/releases
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#SourceDir}\..\installer
OutputBaseFilename=HyperVConsoleMCP-Setup-{#AppVersion}
SetupIconFile={#SetupIcon}
UninstallDisplayIcon={app}\{#Launcher}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
AllowNoIcons=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#Launcher}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#Launcher}"; Tasks: desktopicon

[Run]
; The routine, not a second installer: --no-shortcut because this script makes
; the shortcuts, and the routine writes the client entry, runs the self-test and
; says what to do next in a console the operator can read.
Filename: "{app}\{#SetupExe}"; Parameters: "--no-shortcut"; Description: "Set up {#AppName} for this account now"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; Before the files go: the routine removes our entry from each client's config
; and the Start Menu shortcut. Credentials and the state folder are kept unless
; the operator asks for --remove-credentials and --remove-data.
Filename: "{app}\{#SetupExe}"; Parameters: "--uninstall"; Flags: runhidden waituntilterminated skipifdoesntexist
