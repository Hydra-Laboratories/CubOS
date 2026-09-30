# CubOS Windows Installer

This directory builds a self-contained Windows operator installer from the
CubOS monorepo. It bundles an app-local Python 3.11 runtime, an offline
wheelhouse containing `cubos` and `cubos-api`, compiled operator web assets,
an Electron desktop shell, generic config seeds, and support scripts.

The operator machine does not need Python, Node.js, Git, or internet access.

## Packaging machine requirements

- Windows x64
- Git
- Python 3.11
- Node.js/npm compatible with the lockfiles under `apps/operator-web` and
  `apps/operator-desktop`
- Inno Setup 6
- Internet access while building dependencies

## Build

From the monorepo root:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\build-installer.ps1
```

Output is written to `deploy\windows\build\dist\`.

Useful overrides:

```powershell
.\deploy\windows\build-installer.ps1 `
  -CubOSRepoUrl https://github.com/Ursa-Laboratories/CubOS.git `
  -Branch main `
  -AppVersion 0.1.123 `
  -BuildPythonPath "C:\Python311\python.exe" `
  -PythonVersion 3.11.9 `
  -InnoCompiler "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
```

GitHub Actions passes `-CubOSSourceDir "$env:GITHUB_WORKSPACE"` so the installer
is built from the checked-out commit.

## Installed locations

- Runtime: `%LOCALAPPDATA%\Programs\UrsaLabs\CubOS`
- Configs: `%LOCALAPPDATA%\UrsaLabs\CubOS\configs`
- Data: `%LOCALAPPDATA%\UrsaLabs\CubOS\data\panda_data.db`
- Logs: `%LOCALAPPDATA%\UrsaLabs\CubOS\logs`

The installer creates a Start menu shortcut and, by default, a desktop shortcut
for `CubOS.exe`, plus shortcuts for configs, logs, and diagnostic export. The
desktop application owns the hidden API process, waits for it to become ready,
and displays the operator interface in a native application window. Closing
the window shuts down the API process. No terminal or browser window is shown.

## Validation checklist

1. Build on the Windows GitHub Actions runner or a clean Windows packaging host.
2. Install on a clean VM without Python, Node.js, or Git on `PATH`.
3. Start CubOS from its desktop icon and confirm one native application window
   opens without a terminal or browser window.
4. Confirm the private venv imports `cubos` and `cubos_api`.
5. Confirm generic config seeds are copied on first launch.
6. Confirm `godirect` imports (all bundled public drivers install by default).
7. Close CubOS and confirm its hidden Python child process exits.
8. Export diagnostics and inspect the archive.

UI-only validation must not connect hardware. Connect, home, jog, calibration,
and protocol runs require a separate operator-led hardware test.
