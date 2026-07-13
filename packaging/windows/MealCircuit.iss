#ifndef MyAppVersion
#define MyAppVersion "0.3.0"
#endif
#ifndef SourceDir
#define SourceDir "..\..\dist\MealCircuit"
#endif
#ifndef OutputDir
#define OutputDir "..\..\dist\installer"
#endif

[Setup]
AppId={{8D92EA6D-E875-49C7-A8D7-AB482E64A63A}
AppName=MealCircuit
AppVersion={#MyAppVersion}
AppPublisher=MealCircuit contributors
DefaultDirName={autopf}\MealCircuit
DefaultGroupName=MealCircuit
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=MealCircuit-{#MyAppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
WizardStyle=modern
UninstallDisplayIcon={app}\MealCircuit.exe

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\MealCircuit"; Filename: "{app}\MealCircuit.exe"
Name: "{autodesktop}\MealCircuit"; Filename: "{app}\MealCircuit.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\MealCircuit.exe"; Description: "Launch MealCircuit"; Flags: nowait postinstall skipifsilent
