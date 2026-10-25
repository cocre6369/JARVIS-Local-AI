; ---------------------------------------------------------------------------------
; JARVIS installer - Inno Setup 6 script.
;
; Builds a single setup executable that a non-technical user can double-click:
;   * installs to %LOCALAPPDATA%\JARVIS (no admin rights needed)
;   * creates a desktop shortcut and a Start Menu entry
;   * optionally starts JARVIS once the install finishes (the first-run wizard opens)
;   * offers a clean uninstall entry in "Add or remove programs"
;
; Build (from the repository root):
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DAppVersion=1.0.0 ^
;        /Odist /FJARVIS-Setup-1.0.0 packaging\installer.iss
; ---------------------------------------------------------------------------------
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

#define AppName "JARVIS"
#define AppPublisher "JARVIS Local"
#define AppExeName "JARVIS.exe"
#define SourceDir "..\dist\JARVIS"

[Setup]
AppId={{7C1F4C0E-3B2A-4E9D-8F55-2A6D1C0BE117}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\JARVIS
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; No administrator rights required: JARVIS never installs into Program Files.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=JARVIS-Setup-{#AppVersion}
SetupIconFile=assets\jarvis.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; A friendly name in Add/Remove programs, and the version shown there.
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} local AI assistant setup

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "launchafter"; Description: "Start JARVIS when the install finishes"; GroupDescription: "Finish:"; Flags: checkedonce

[Files]
; Everything PyInstaller produced, including the bundled runtimes.
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The icon is not part of the frozen bundle; the installer copies it next to the
; exe so the data dir and shortcuts have art.
Source: "assets\jarvis.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "assets\jarvis.png"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postcreate skipifsilent; Tasks: launchafter

[UninstallDelete]
; Scratch data (screenshots, temp files) is disposable; settings and memories are
; kept so a reinstall never loses them.
Type: filesandordirs; Name: "{localappdata}\JARVIS-data\scratch"

[Code]
// Refuse to install over a running JARVIS - PyInstaller bundles lock while open.
function InitializeSetup(): Boolean;
begin
  Result := True;
  if CheckForMutexes('jarvis-single-instance') then
  begin
    if MsgBox('JARVIS is still running. Close it before installing?', mbConfirmation, MB_YESNO) = IDYES then
      Result := False;
  end;
end;
