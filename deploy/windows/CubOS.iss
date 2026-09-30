#ifndef SourceDir
#define SourceDir "build\stage"
#endif

#ifndef OutputDir
#define OutputDir "dist"
#endif

#ifndef AppVersion
#define AppVersion "0.1.0"
#endif

[Setup]
AppId={{3A7528D5-1BB4-4F1E-B745-62D7419AB0BA}
AppName=CubOS
AppPublisher=Ursa Laboratories
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\UrsaLabs\CubOS
DefaultGroupName=CubOS
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=CubOS-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest
WizardStyle=modern
UninstallDisplayName=CubOS

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Dirs]
Name: "{localappdata}\UrsaLabs\CubOS\configs"
Name: "{localappdata}\UrsaLabs\CubOS\logs"

[Files]
Source: "{#SourceDir}\python-installer.exe"; DestDir: "{app}\installers"; DestName: "python-installer.exe"; Flags: ignoreversion
Source: "{#SourceDir}\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\scripts\*"; DestDir: "{app}\scripts"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\wheelhouse\*"; DestDir: "{app}\wheelhouse"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\requirements\*"; DestDir: "{app}\requirements"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\desktop\*"; DestDir: "{app}\desktop"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\build-info.json"; DestDir: "{app}"; Flags: ignoreversion

[Run]
Filename: "{app}\desktop\CubOS.exe"; Description: "Start CubOS"; WorkingDir: "{app}\desktop"; Flags: nowait postinstall skipifsilent unchecked

[Icons]
Name: "{group}\CubOS"; Filename: "{app}\desktop\CubOS.exe"; WorkingDir: "{app}\desktop"
Name: "{group}\CubOS Configs"; Filename: "explorer.exe"; Parameters: """{localappdata}\UrsaLabs\CubOS\configs"""
Name: "{group}\CubOS Logs"; Filename: "explorer.exe"; Parameters: """{localappdata}\UrsaLabs\CubOS\logs"""
Name: "{group}\Export Diagnostics"; Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\Export-Diagnostics.ps1"" -InstallDir ""{app}"""; WorkingDir: "{app}"
Name: "{group}\Uninstall CubOS"; Filename: "{uninstallexe}"
Name: "{autodesktop}\CubOS"; Filename: "{app}\desktop\CubOS.exe"; WorkingDir: "{app}\desktop"; Tasks: desktopicon

[UninstallDelete]
Type: filesandordirs; Name: "{app}\venv"
Type: filesandordirs; Name: "{app}\Python"
Type: files; Name: "{app}\runtime-installed.txt"

[Code]
procedure RunPowerShellChecked(
  ScriptName: String;
  ScriptArguments: String;
  StatusText: String
);
var
  ResultCode: Integer;
  PowerShellPath: String;
  Parameters: String;
begin
  WizardForm.StatusLabel.Caption := StatusText;
  PowerShellPath := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  Parameters :=
    '-NoProfile -ExecutionPolicy Bypass -File "' +
    ExpandConstant('{app}\scripts\' + ScriptName) + '" ' + ScriptArguments;

  if not Exec(
    PowerShellPath,
    Parameters,
    ExpandConstant('{app}'),
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  ) then
    RaiseException(
      Format('Unable to start %s (Windows error %d).', [ScriptName, ResultCode])
    );

  if ResultCode <> 0 then
    RaiseException(
      Format('%s failed with exit code %d. See the CubOS logs for details.', [ScriptName, ResultCode])
    );
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep <> ssPostInstall then
    exit;

  RunPowerShellChecked(
    'Install-Python.ps1',
    '-InstallDir "' + ExpandConstant('{app}') +
      '" -PythonInstaller "' +
      ExpandConstant('{app}\installers\python-installer.exe') + '"',
    'Installing private Python runtime...'
  );

  RunPowerShellChecked(
    'Install-Runtime.ps1',
    '-InstallDir "' + ExpandConstant('{app}') + '"',
    'Installing CubOS and CubOS API runtime packages...'
  );
end;
