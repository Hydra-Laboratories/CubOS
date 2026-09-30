import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
import tomllib

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
WINDOWS_INSTALLER = REPO_ROOT / "deploy" / "windows"
DESKTOP_APP = REPO_ROOT / "apps" / "operator-desktop"


def _extract_ini_section(text: str, section: str) -> str:
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == f"[{section}]":
            start = index + 1
            break
    if start is None:
        return ""
    end = start
    while end < len(lines) and not lines[end].strip().startswith("["):
        end += 1
    return "\n".join(lines[start:end])


def test_windows_runtime_uses_venv_and_installs_core_and_api() -> None:
    install_runtime = (WINDOWS_INSTALLER / "scripts" / "Install-Runtime.ps1").read_text()
    start_cubos = (WINDOWS_INSTALLER / "scripts" / "Start-CubOS.ps1").read_text()

    assert 'Join-Path $InstallDir "venv"' in install_runtime
    assert 'Join-Path $VenvDir "Scripts\\python.exe"' in install_runtime
    assert 'Invoke-LoggedNative $Python @("-m", "venv", $VenvDir)' in install_runtime
    assert '"--upgrade", "pip"' not in install_runtime
    assert '"cubos", "cubos-api"' in install_runtime

    assert 'Join-Path $InstallDir "venv\\Scripts\\python.exe"' in start_cubos
    assert "& $RuntimePython -m cubos_api" in start_cubos


def test_windows_installer_requires_build_python_to_match_bundled_python() -> None:
    build_script = (WINDOWS_INSTALLER / "build-installer.ps1").read_text()

    assert "$BuildPythonVersion" in build_script
    assert "$ExpectedPythonVersion" in build_script
    assert "does not match bundled Python" in build_script
    assert "-BuildPythonPath" in build_script


def test_windows_runtime_install_reports_visible_progress() -> None:
    """The runtime install is a long, mostly-silent sequence of offline pip
    commands. Without incremental feedback it looks frozen, so Install-Runtime
    must emit a per-phase banner, drive a progress bar, and print a heartbeat
    while a command is still running."""
    install_runtime = (WINDOWS_INSTALLER / "scripts" / "Install-Runtime.ps1").read_text()

    # Numbered per-phase banners with a total-step count.
    assert "Set-TotalSteps" in install_runtime
    assert "function Start-Step" in install_runtime
    assert "[$($script:StepIndex)/$($script:TotalSteps)]" in install_runtime

    # A progress bar the operator (or Inno wizard host) can render.
    assert "Write-Progress" in install_runtime

    # A heartbeat emitted during silent stretches so the step never looks hung.
    assert "still" in install_runtime.lower()
    assert "elapsed" in install_runtime.lower()

    # The step total must match the number of fixed phases (venv, core deps,
    # CubOS/CubOS API, verify) plus one step per selected driver group.
    assert "Set-TotalSteps (4 + $SelectedDriverGroups.Count)" in install_runtime
    start_step_calls = install_runtime.count('Start-Step "')
    assert start_step_calls == 5  # 4 fixed phases + the one inside the driver loop


def test_windows_runtime_uses_exit_code_side_channel_for_progress_processes() -> None:
    install_runtime = (WINDOWS_INSTALLER / "scripts" / "Install-Runtime.ps1").read_text()

    assert "$ExitCodeFile" in install_runtime
    assert "[System.IO.File]::WriteAllText" in install_runtime
    assert "[int]::TryParse" in install_runtime
    assert "$Process.ExitCode" not in install_runtime


def test_windows_installer_fails_when_runtime_setup_fails() -> None:
    iss = (WINDOWS_INSTALLER / "CubOS.iss").read_text()
    run_section = _extract_ini_section(iss, "Run")

    assert "Install-Python.ps1" not in run_section
    assert "Install-Runtime.ps1" not in run_section
    assert "procedure CurStepChanged" in iss
    assert "CurStep <> ssPostInstall" in iss
    assert "Install-Python.ps1" in iss
    assert "Install-Runtime.ps1" in iss
    assert "ewWaitUntilTerminated" in iss
    assert "if ResultCode <> 0 then" in iss
    assert "RaiseException" in iss


def test_windows_installer_packages_native_desktop_app() -> None:
    iss = (WINDOWS_INSTALLER / "CubOS.iss").read_text()
    build_script = (WINDOWS_INSTALLER / "build-installer.ps1").read_text()
    workflow = (REPO_ROOT / ".github" / "workflows" / "windows-installer.yml").read_text()
    run_section = _extract_ini_section(iss, "Run")
    icons_section = _extract_ini_section(iss, "Icons")
    desktop_package = json.loads((DESKTOP_APP / "package.json").read_text())

    assert 'Source: "{#SourceDir}\\desktop\\*"' in iss
    assert 'Filename: "{app}\\desktop\\CubOS.exe"' in run_section
    assert "powershell.exe" not in run_section
    assert 'Name: "{group}\\CubOS"; Filename: "{app}\\desktop\\CubOS.exe"' in icons_section
    assert 'Name: "{autodesktop}\\CubOS"; Filename: "{app}\\desktop\\CubOS.exe"' in icons_section
    desktop_task = next(
        line for line in iss.splitlines() if 'Name: "desktopicon"' in line
    )
    assert "unchecked" not in desktop_task.lower()

    assert '"apps\\operator-desktop"' in build_script
    assert '"pack:win"' in build_script
    assert '"dist\\win-unpacked"' in build_script
    assert "apps/operator-desktop/**" in workflow
    assert desktop_package["build"]["productName"] == "CubOS"
    assert desktop_package["build"]["win"]["target"] == "dir"


def test_windows_installer_installs_all_bundled_public_drivers_by_default() -> None:
    """No per-driver checkbox; Install-Runtime.ps1 installs every driver file it finds."""
    iss = (WINDOWS_INSTALLER / "CubOS.iss").read_text()
    build_script = (WINDOWS_INSTALLER / "build-installer.ps1").read_text()
    install_runtime = (WINDOWS_INSTALLER / "scripts" / "Install-Runtime.ps1").read_text()
    runtime_requirements = (WINDOWS_INSTALLER / "runtime-requirements.txt").read_text()
    asmi_requirements = (
        WINDOWS_INSTALLER / "requirements" / "drivers" / "asmi.txt"
    ).read_text()

    tasks_section = _extract_ini_section(iss, "Tasks")
    assert tasks_section
    task_names = re.findall(r'Name:\s*"([^"]+)"', tasks_section)
    assert task_names == ["desktopicon"]
    assert "asmi" not in iss.lower()
    assert "GetDriverGroups" not in iss
    assert "WizardIsTaskSelected" not in iss
    assert "-DriverGroups" not in iss

    assert "Get-ChildItem -Path $DriverRequirementsDir" in install_runtime
    assert '-Filter "*.txt"' in install_runtime
    assert "$SelectedDriverGroups" in install_runtime
    assert "godirect" not in runtime_requirements.lower()
    assert "godirect>=1.2.1" in asmi_requirements
    assert "$DriverRequirementsDir" in build_script
    assert "requirements\\drivers" in build_script

    assert "TODO" in install_runtime
    assert "uvvis" in install_runtime.lower()
    assert "instruments/README.md" in install_runtime


@pytest.mark.skipif(
    shutil.which("pwsh") is None, reason="pwsh is not available on PATH"
)
def test_windows_runtime_installs_every_driver_requirements_file() -> None:
    """Runs the real $SelectedDriverGroups snippet against a synthetic drivers directory."""
    install_runtime = (WINDOWS_INSTALLER / "scripts" / "Install-Runtime.ps1").read_text()
    match = re.search(r"\$SelectedDriverGroups = @\(.*?\n\)", install_runtime, re.DOTALL)
    assert match, "could not find the $SelectedDriverGroups construction in Install-Runtime.ps1"
    selection_snippet = match.group(0)

    with tempfile.TemporaryDirectory() as tmp:
        drivers_dir = Path(tmp) / "requirements" / "drivers"
        drivers_dir.mkdir(parents=True)
        (drivers_dir / "asmi.txt").write_text("godirect>=1.2.1\n")
        (drivers_dir / "widgetcam.txt").write_text("widgetcam-sdk>=2.0\n")
        (drivers_dir / "README.md").write_text("not a requirements file\n")

        escaped_dir = str(drivers_dir).replace("'", "''")
        probe = (
            f"$DriverRequirementsDir = '{escaped_dir}'; "
            f"{selection_snippet}; "
            "$SelectedDriverGroups -join ','"
        )
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", probe],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        assert result.stdout.strip() == "asmi,widgetcam"


def _requirement_name(spec: str) -> str:
    """Return the PEP 503-normalized distribution name from a requirement line."""
    # Strip inline comments, environment markers, and the extras/version tail.
    spec = spec.split("#", 1)[0].split(";", 1)[0].strip()
    name = re.split(r"[\s<>=!~\[]", spec, maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


def test_windows_runtime_requirements_cover_core_and_api_dependencies() -> None:
    """The offline installer builds its wheelhouse and venv from
    runtime-requirements.txt and installs cubos/cubos-api with --no-deps, so every
    runtime dependency in both pyproject files must be listed there. A missing
    entry (e.g. ruamel.yaml) is never bundled or installed and only surfaces as
    a ``pip check`` failure on the user's machine."""
    projects = [
        tomllib.loads((REPO_ROOT / "packages/core/pyproject.toml").read_text()),
        tomllib.loads((REPO_ROOT / "services/api/pyproject.toml").read_text()),
    ]
    runtime_requirements = (
        WINDOWS_INSTALLER / "runtime-requirements.txt"
    ).read_text()

    listed = {
        _requirement_name(line)
        for line in runtime_requirements.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }

    # cubos is a direct git dependency built and installed as its own wheel
    # (--no-deps), so it is intentionally absent from runtime-requirements.txt.
    required = {
        _requirement_name(dep)
        for project in projects
        for dep in project["project"]["dependencies"]
        if _requirement_name(dep) != "cubos"
    }

    missing = required - listed
    assert not missing, (
        "deploy/windows/runtime-requirements.txt is missing CubOS runtime "
        f"dependencies: {sorted(missing)}"
    )


def test_godirect_is_an_optional_api_dependency() -> None:
    project = tomllib.loads((REPO_ROOT / "services/api/pyproject.toml").read_text())

    dependencies = "\n".join(project["project"]["dependencies"]).lower()
    optional = project["project"]["optional-dependencies"]

    assert "godirect" not in dependencies
    assert optional["asmi"] == ["godirect>=1.2.1"]


def test_windows_launcher_seeds_only_generic_config_templates() -> None:
    start_cubos = (WINDOWS_INSTALLER / "scripts" / "Start-CubOS.ps1").read_text()

    assert 'Join-Path $CubOSDir "services\\api\\configs"' in start_cubos
    assert '"gantry\\cub_seed.yaml"' in start_cubos
    assert '"gantry\\cub_xl_seed.yaml"' in start_cubos
    assert '"deck\\cub_deck_example.yaml"' in start_cubos
    assert '"deck\\cubxl_deck_example.yaml"' in start_cubos

    for named_config in (
        "cub_filmetrics",
        "cub_xl_asmi",
        "cub_xl_panda",
        "cub_xl_sterling",
        "asmi_deck",
        "filmetrics_deck",
        "panda_deck",
        "sterling_deck",
    ):
        assert named_config not in start_cubos


def test_windows_launcher_points_api_at_bundled_frontend() -> None:
    start_cubos = (WINDOWS_INSTALLER / "scripts" / "Start-CubOS.ps1").read_text()

    assert '$FrontendDist = Join-Path $CubOSDir "apps\\operator-web\\dist"' in start_cubos
    assert '$env:CUBOS_WEB_DIR = Join-Path $CubOSDir "apps\\operator-web"' in start_cubos
    assert "$env:CUBOS_WEB_DIST = $FrontendDist" in start_cubos


@pytest.mark.skipif(
    shutil.which("pwsh") is None, reason="pwsh is not available on PATH"
)
def test_windows_installer_powershell_scripts_are_syntactically_valid() -> None:
    script_paths = sorted(WINDOWS_INSTALLER.glob("**/*.ps1"))
    assert script_paths, "expected at least one .ps1 script under deploy/windows"

    for script_path in script_paths:
        escaped_path = str(script_path).replace("'", "''")
        parse_command = (
            "$errors = $null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            f"'{escaped_path}', [ref]$null, [ref]$errors) | Out-Null; "
            "if ($errors.Count -gt 0) { "
            "$errors | ForEach-Object { Write-Error $_.Message }; exit 1 "
            "} else { exit 0 }"
        )
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", parse_command],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"{script_path} failed PowerShell parse validation: "
            f"{result.stderr or result.stdout}"
        )
