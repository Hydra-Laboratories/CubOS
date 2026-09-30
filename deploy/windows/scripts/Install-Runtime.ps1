param(
    [string]$InstallDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$UserRoot = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "UrsaLabs\CubOS"
$LogDir = Join-Path $UserRoot "logs"
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $LogDir "cubos-install-runtime-$Timestamp.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Progress state shared across the step/heartbeat helpers below. The runtime
# install is a long, mostly-silent sequence of pip commands, so we surface an
# explicit "[step/total]" banner per phase plus a periodic heartbeat while a
# command is still running. Everything is emitted as pipeline output (via
# Tee-Object) rather than Write-Host so it survives being piped through
# Start-CubOS.ps1's Tee-Object into the launch log and the operator's console.
$script:ProgressActivity = "Installing CubOS and CubOS API runtime packages"
$script:StepIndex = 0
$script:TotalSteps = 0

function Write-Log {
    param([string]$Message)

    $Line = "$(Get-Date -Format o) $Message"
    $Line | Tee-Object -FilePath $LogPath -Append
}

function Set-TotalSteps {
    param([int]$Count)

    $script:TotalSteps = $Count
}

function Get-StepPercent {
    if ($script:TotalSteps -le 0) {
        return 0
    }
    $Percent = [int](100 * ($script:StepIndex - 1) / $script:TotalSteps)
    if ($Percent -lt 0) {
        return 0
    }
    if ($Percent -gt 100) {
        return 100
    }
    return $Percent
}

function Start-Step {
    param([string]$Title)

    $script:StepIndex++
    $Label = "[$($script:StepIndex)/$($script:TotalSteps)] $Title"
    "" | Tee-Object -FilePath $LogPath -Append
    Write-Log "==> $Label"
    Write-Progress -Activity $script:ProgressActivity -Status $Label -PercentComplete (Get-StepPercent)
}

function Invoke-LoggedNative {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$Activity = ""
    )

    Write-Log "> $FilePath $($Arguments -join ' ')"

    $Label = if ($Activity) { $Activity } else { "working" }

    $OutFile = [System.IO.Path]::GetTempFileName()
    $ErrFile = [System.IO.Path]::GetTempFileName()
    $ExitCodeFile = [System.IO.Path]::GetTempFileName()
    Remove-Item -LiteralPath $ExitCodeFile -Force
    $StartTime = Get-Date
    $LastBeat = Get-Date
    $SeenOut = 0
    $SeenErr = 0
    $ExitCode = $null

    try {
        # Windows PowerShell 5.1 only populates Process.ExitCode when
        # Start-Process uses -Wait. Waiting there would prevent the progress
        # heartbeat, so a child PowerShell writes the native exit code to a
        # side-channel file while this process continues streaming output.
        $Payload = @{
            FilePath = $FilePath
            Arguments = @($Arguments)
        } | ConvertTo-Json -Compress
        $PayloadBase64 = [Convert]::ToBase64String(
            [System.Text.Encoding]::UTF8.GetBytes($Payload)
        )
        $EscapedExitCodeFile = $ExitCodeFile.Replace("'", "''")
        $Runner = @"
`$PayloadJson = [System.Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String('$PayloadBase64')
)
`$Payload = `$PayloadJson | ConvertFrom-Json
`$NativeExitCode = 1
`$ProgressPreference = 'SilentlyContinue'
try {
    & ([string]`$Payload.FilePath) @(`$Payload.Arguments)
    if (`$null -eq `$LASTEXITCODE) {
        `$NativeExitCode = if (`$?) { 0 } else { 1 }
    }
    else {
        `$NativeExitCode = [int]`$LASTEXITCODE
    }
}
catch {
    Write-Error `$_.Exception.Message
}
finally {
    [System.IO.File]::WriteAllText('$EscapedExitCodeFile', [string]`$NativeExitCode)
}
exit `$NativeExitCode
"@
        $EncodedRunner = [Convert]::ToBase64String(
            [System.Text.Encoding]::Unicode.GetBytes($Runner)
        )
        $StartArgs = @{
            FilePath               = (Join-Path $PSHOME "powershell.exe")
            ArgumentList           = @(
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy", "Bypass",
                "-EncodedCommand", $EncodedRunner
            )
            NoNewWindow            = $true
            PassThru               = $true
            RedirectStandardOutput = $OutFile
            RedirectStandardError  = $ErrFile
        }

        $Process = Start-Process @StartArgs

        while (-not $Process.HasExited) {
            Start-Sleep -Milliseconds 1000
            $Printed = $false

            # Emit newly completed output lines. Hold back the last line while
            # the process runs because it may still be partially written.
            $OutLines = @(Get-Content -LiteralPath $OutFile -ErrorAction SilentlyContinue)
            while ($SeenOut -lt ($OutLines.Count - 1)) {
                $OutLines[$SeenOut] | Tee-Object -FilePath $LogPath -Append
                $SeenOut++
                $Printed = $true
            }

            $ErrLines = @(Get-Content -LiteralPath $ErrFile -ErrorAction SilentlyContinue)
            while ($SeenErr -lt ($ErrLines.Count - 1)) {
                $ErrLines[$SeenErr] | Tee-Object -FilePath $LogPath -Append
                $SeenErr++
                $Printed = $true
            }

            $Now = Get-Date
            if ($Printed) {
                $LastBeat = $Now
            }
            elseif (($Now - $LastBeat).TotalSeconds -ge 5) {
                $Elapsed = [int]($Now - $StartTime).TotalSeconds
                "    ...still $Label (${Elapsed}s elapsed)" | Tee-Object -FilePath $LogPath -Append
                $LastBeat = $Now
                Write-Progress -Activity $script:ProgressActivity -Status "$Label (${Elapsed}s)" -PercentComplete (Get-StepPercent)
            }
        }

        $Process.WaitForExit()
        if (-not (Test-Path -LiteralPath $ExitCodeFile)) {
            throw "Could not determine the exit code for $FilePath"
        }
        $ExitCodeText = (Get-Content -LiteralPath $ExitCodeFile -Raw).Trim()
        $ParsedExitCode = 0
        if (-not [int]::TryParse($ExitCodeText, [ref]$ParsedExitCode)) {
            throw "Invalid exit code '$ExitCodeText' reported for $FilePath"
        }
        $ExitCode = $ParsedExitCode

        # Flush any trailing output, including a final line without a newline.
        $OutLines = @(Get-Content -LiteralPath $OutFile -ErrorAction SilentlyContinue)
        while ($SeenOut -lt $OutLines.Count) {
            $OutLines[$SeenOut] | Tee-Object -FilePath $LogPath -Append
            $SeenOut++
        }
        $ErrLines = @(Get-Content -LiteralPath $ErrFile -ErrorAction SilentlyContinue)
        while ($SeenErr -lt $ErrLines.Count) {
            $ErrLines[$SeenErr] | Tee-Object -FilePath $LogPath -Append
            $SeenErr++
        }

        if ($ExitCode -eq 0) {
            $Total = [int]((Get-Date) - $StartTime).TotalSeconds
            "    done ${Label} (${Total}s)" | Tee-Object -FilePath $LogPath -Append
        }
    }
    finally {
        Remove-Item -LiteralPath $OutFile, $ErrFile, $ExitCodeFile -Force -ErrorAction SilentlyContinue
    }

    if ($ExitCode -ne 0) {
        throw "$FilePath $($Arguments -join ' ') failed with exit code $ExitCode"
    }
}

$Python = Join-Path $InstallDir "Python\python.exe"
$VenvDir = Join-Path $InstallDir "venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$Wheelhouse = Join-Path $InstallDir "wheelhouse"
$Requirements = Join-Path $InstallDir "requirements\runtime-requirements.txt"
$DriverRequirementsDir = Join-Path $InstallDir "requirements\drivers"
$Marker = Join-Path $InstallDir "runtime-installed.txt"

# TODO: non-pip drivers (e.g. uvvis) aren't covered here -- see
# packages/core/src/cubos/instruments/README.md "External proprietary drivers".
$SelectedDriverGroups = @(
    if (Test-Path $DriverRequirementsDir) {
        Get-ChildItem -Path $DriverRequirementsDir -Filter "*.txt" |
            ForEach-Object { $_.BaseName.ToLowerInvariant() } |
            Sort-Object
    }
)

try {
    Write-Log "Installing CubOS runtime"
    Write-Log "Install directory: $InstallDir"
    Write-Log "Expected Python: $Python"
    Write-Log "Runtime virtual environment: $VenvDir"
    Write-Log "Wheelhouse: $Wheelhouse"
    Write-Log "Requirements: $Requirements"
    Write-Log "Installing public driver groups: $(if ($SelectedDriverGroups.Count) { $SelectedDriverGroups -join ', ' } else { 'none' })"

    if (-not (Test-Path $Python)) {
        throw "Python runtime not found at $Python"
    }

    if (-not (Test-Path $Wheelhouse)) {
        throw "Wheelhouse not found at $Wheelhouse"
    }

    if (-not (Test-Path $Requirements)) {
        throw "Runtime requirements file not found at $Requirements"
    }

    # Steps: virtual environment + core dependencies + one per driver group +
    # CubOS/CubOS + verification. Keeping the total in sync with the Start-Step
    # calls below is what makes the "[step/total]" banner and progress bar
    # meaningful to the operator.
    Set-TotalSteps (4 + $SelectedDriverGroups.Count)

    Start-Step "Preparing Python virtual environment"
    if (-not (Test-Path $VenvPython)) {
        Invoke-LoggedNative $Python @("-m", "venv", $VenvDir)
    }
    else {
        Write-Log "Virtual environment already present at $VenvDir"
    }

    if (-not (Test-Path $VenvPython)) {
        throw "Virtual environment creation completed but python.exe was not found at $VenvPython"
    }

    Start-Step "Installing core runtime dependencies"
    Invoke-LoggedNative $VenvPython @("-m", "pip", "install", "--no-index", "--find-links", $Wheelhouse, "-r", $Requirements) -Activity "installing core runtime dependencies"

    foreach ($DriverGroup in $SelectedDriverGroups) {
        Start-Step "Installing hardware driver support: $DriverGroup"
        $DriverRequirements = Join-Path $DriverRequirementsDir "$DriverGroup.txt"
        if (-not (Test-Path $DriverRequirements)) {
            throw "No public driver requirements file found for '$DriverGroup' at $DriverRequirements"
        }
        Invoke-LoggedNative $VenvPython @("-m", "pip", "install", "--no-index", "--find-links", $Wheelhouse, "-r", $DriverRequirements) -Activity "installing driver group '$DriverGroup'"
    }

    Start-Step "Installing CubOS and CubOS API"
    Invoke-LoggedNative $VenvPython @("-m", "pip", "install", "--no-index", "--find-links", $Wheelhouse, "--no-deps", "--force-reinstall", "cubos", "cubos-api") -Activity "installing CubOS and CubOS API"

    Start-Step "Verifying installation"
    Invoke-LoggedNative $VenvPython @("-m", "pip", "check") -Activity "checking installed dependencies"
    Invoke-LoggedNative $VenvPython @("-c", "import cubos, cubos_api; print('CubOS runtime import check passed')") -Activity "verifying CubOS and CubOS API imports"

    Write-Progress -Activity $script:ProgressActivity -Completed

    @(
        "Installed $(Get-Date -Format o)",
        "Python=$VenvPython",
        "DriverGroups=$(if ($SelectedDriverGroups.Count) { $SelectedDriverGroups -join ',' } else { 'none' })"
    ) | Set-Content -Path $Marker -Encoding UTF8
    Write-Log "Runtime install complete"
    exit 0
}
catch {
    Write-Progress -Activity $script:ProgressActivity -Completed
    Write-Log "ERROR: $($_.Exception.Message)"
    if ($_.ScriptStackTrace) {
        Write-Log $_.ScriptStackTrace
    }
    exit 1
}
