import os
import subprocess
import sys
from pathlib import Path


def test_single_instrument_calibration_imports_from_source_tree() -> None:
    """The calibration tool resolves through the CubOS namespace."""
    project_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root / "src")

    result = subprocess.run(
        [
            sys.executable,
            "-c",
                "import cubos.tools.calibration.single_instrument_calibration",
        ],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        "Import failed with stderr:\n"
        f"{result.stderr}"
    )
