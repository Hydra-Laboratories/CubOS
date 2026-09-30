param(
    [string]$InstallDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$UserRoot = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "UrsaLabs\CubOS"
$LogDir = Join-Path $UserRoot "logs"
$DataDir = Join-Path $UserRoot "data"
$DataDbPath = Join-Path $DataDir "panda_data.db"
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $LogDir "cubos-launch-$Timestamp.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

function Write-Log {
    param([string]$Message)

    $Line = "$(Get-Date -Format o) $Message"
    $Line | Tee-Object -FilePath $LogPath -Append
}

function Show-Failure {
    param([string]$Message)

    Write-Host ""
    Write-Host "CubOS failed to start." -ForegroundColor Red
    Write-Host $Message -ForegroundColor Red
    Write-Host "Log written to: $LogPath"
    Read-Host "Press Enter to close"
}

$Python = Join-Path $InstallDir "Python\python.exe"
$RuntimePython = Join-Path $InstallDir "venv\Scripts\python.exe"
$PythonInstaller = Join-Path $InstallDir "installers\python-installer.exe"
$InstallPythonScript = Join-Path $InstallDir "scripts\Install-Python.ps1"
$InstallRuntimeScript = Join-Path $InstallDir "scripts\Install-Runtime.ps1"
$RuntimeMarker = Join-Path $InstallDir "runtime-installed.txt"
$CubOSDir = Join-Path $InstallDir "app\CubOS"
$ApiSeedConfigDir = Join-Path $CubOSDir "services\api\configs"
$ConfigDir = if ($env:CUBOS_CONFIG_DIR) { $env:CUBOS_CONFIG_DIR } else { Join-Path $UserRoot "configs" }

function Invoke-LauncherScript {
    param(
        [string]$ScriptPath,
        [string[]]$Arguments
    )

    if (-not (Test-Path $ScriptPath)) {
        throw "Required installer script not found at $ScriptPath"
    }

    Write-Log "> powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ScriptPath $($Arguments -join ' ')"
    $PreviousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ScriptPath @Arguments 2>&1 | ForEach-Object { $_.ToString() } | Tee-Object -FilePath $LogPath -Append
    }
    finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
    if ($LASTEXITCODE -ne 0) {
        throw "$ScriptPath failed with exit code $LASTEXITCODE"
    }
}

try {
    Write-Log "Starting CubOS launcher"
    Write-Log "Install directory: $InstallDir"
    Write-Log "Expected Python: $Python"
    Write-Log "Expected runtime Python: $RuntimePython"
    Write-Log "CubOS source directory: $CubOSDir"
    Write-Log "Config directory: $ConfigDir"
    Write-Log "Data database path: $DataDbPath"

    $NeedsRuntimeInstall = (-not (Test-Path $RuntimeMarker)) -or (-not (Test-Path $RuntimePython))

    if (-not (Test-Path $Python)) {
        Write-Log "Python runtime missing; installing from bundled installer"
        Invoke-LauncherScript $InstallPythonScript @("-InstallDir", $InstallDir, "-PythonInstaller", $PythonInstaller)
        $NeedsRuntimeInstall = $true
    }

    if (-not (Test-Path $CubOSDir)) {
        throw "CubOS source directory not found at $CubOSDir"
    }

    if ($NeedsRuntimeInstall) {
        Write-Log "CubOS runtime packages need installation"
        Invoke-LauncherScript $InstallRuntimeScript @("-InstallDir", $InstallDir)
    }

    $FrontendDist = Join-Path $CubOSDir "apps\operator-web\dist"
    if (-not (Test-Path $FrontendDist)) {
        throw "CubOS frontend build not found at $FrontendDist. Rebuild the installer so Node.js is not needed on the operator machine."
    }

    New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null

    $ExistingConfig = Get-ChildItem -Path $ConfigDir -Recurse -Filter "*.yaml" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $ExistingConfig -and (Test-Path $ApiSeedConfigDir)) {
        $GantryConfigDir = Join-Path $ConfigDir "gantry"
        $DeckConfigDir = Join-Path $ConfigDir "deck"
        New-Item -ItemType Directory -Force -Path $GantryConfigDir | Out-Null
        New-Item -ItemType Directory -Force -Path $DeckConfigDir | Out-Null
        Write-Log "Seeding config directory with generic templates from $ApiSeedConfigDir"
        Copy-Item -Path (Join-Path $ApiSeedConfigDir "gantry\cub_seed.yaml") -Destination $GantryConfigDir -Force
        Copy-Item -Path (Join-Path $ApiSeedConfigDir "gantry\cub_xl_seed.yaml") -Destination $GantryConfigDir -Force
        Copy-Item -Path (Join-Path $ApiSeedConfigDir "deck\cub_deck_example.yaml") -Destination $DeckConfigDir -Force
        Copy-Item -Path (Join-Path $ApiSeedConfigDir "deck\cubxl_deck_example.yaml") -Destination $DeckConfigDir -Force
    }

    $env:CUBOS_CONFIG_DIR = $ConfigDir
    $env:CUBOS_DATA_DB_PATH = $DataDbPath
    $env:CUBOS_HOST = "127.0.0.1"
    $env:CUBOS_PORT = "8742"
    $env:CUBOS_OPEN_BROWSER = "true"
    $env:CUBOS_WEB_DIR = Join-Path $CubOSDir "apps\operator-web"
    $env:CUBOS_WEB_DIST = $FrontendDist
    $env:PYTHONUTF8 = "1"

    Write-Log "Launching CubOS at http://127.0.0.1:8742"

    Push-Location $CubOSDir
    try {
        $PreviousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            & $RuntimePython -m cubos_api 2>&1 | ForEach-Object { $_.ToString() } | Tee-Object -FilePath $LogPath -Append
            $ExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
        }
    }
    finally {
        Pop-Location
    }

    if ($ExitCode -ne 0) {
        throw "python -m cubos_api exited with code $ExitCode"
    }

    exit 0
}
catch {
    $Message = $_.Exception.Message
    Write-Log "ERROR: $Message"
    if ($_.ScriptStackTrace) {
        Write-Log $_.ScriptStackTrace
    }
    Show-Failure $Message
    exit 1
}
