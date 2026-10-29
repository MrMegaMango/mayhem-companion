; Compile from the repository with packaging/build.py or ISCC /D overrides.
#ifndef AppVersion
  #define AppVersion "0.8.0"
#endif
#ifndef BundleDir
  #error BundleDir must point to the complete PyInstaller onedir bundle.
#endif
#ifndef InstallerOutputDir
  #error InstallerOutputDir must point to the installer output directory.
#endif

#define AppExeName "MayhemCompanion.exe"

[Setup]
AppId={{4E3E7B1C-7FB1-4B70-99E4-AC89C949AF53}
AppName=Mayhem Companion
AppVersion={#AppVersion}
AppPublisher=Mayhem Companion
AppPublisherURL=https://arammayhem.pages.dev/
AppSupportURL=https://github.com/MrMegaMango/mayhem-companion/issues
AppUpdatesURL=https://github.com/MrMegaMango/mayhem-companion/releases/latest
DefaultDirName={localappdata}\Programs\MayhemCompanion
DefaultGroupName=Mayhem Companion
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
DisableDirPage=yes
DisableProgramGroupPage=yes
AllowNoIcons=yes
OutputDir={#InstallerOutputDir}
OutputBaseFilename=MayhemCompanionSetup
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#AppExeName}
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
AppMutex=Local\MayhemCompanion.Personal
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Mayhem Companion"; Filename: "{app}\{#AppExeName}"; Parameters: "--show"; WorkingDir: "{app}"; Check: not WizardNoIcons
Name: "{autodesktop}\Mayhem Companion"; Filename: "{app}\{#AppExeName}"; Parameters: "--show"; WorkingDir: "{app}"; Tasks: desktopicon; Check: not WizardNoIcons

[Run]
; Resume an already-enabled watcher stopped during an upgrade, including silent installs.
Filename: "{app}\{#AppExeName}"; Parameters: "--resume-owned-startup"; Flags: runhidden waituntilterminated
Filename: "{app}\{#AppExeName}"; Parameters: "--show"; Description: "Open Mayhem Companion"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; This helper removes only a Startup shortcut owned by this installed executable.
; Settings, downloaded data, and the legacy source installation are preserved.
Filename: "{app}\{#AppExeName}"; Parameters: "--uninstall-cleanup"; RunOnceId: "RemoveOwnedStartup"; Flags: runhidden waituntilterminated skipifdoesntexist
