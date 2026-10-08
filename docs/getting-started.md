# Getting Started

**CubOS already installed?** Open it and follow [Use CubOS Operator](operator-ui.md).
You do not need to install developer tools or edit YAML files to use the app.

## Choose Your Setup Path

| Your setup | Start here |
|---|---|
| Windows installer supplied by your lab | Run the installer, then open **CubOS** from the desktop or Start menu. Python, Node.js, and Git are bundled or unnecessary on this computer. |
| Preinstalled CubOS appliance | Use the address your administrator supplied and follow the [Operator guide](operator-ui.md). |
| Installing from the source repository | Follow the installation steps below. Operator needs **Python 3.11+** and a compatible Node.js version to build the browser interface. |
| Building your own machine | Complete [Gantry Bring-Up](admin/gantry-bring-up.md), then follow the [Operator setup sequence](operator-ui.md#your-first-run). |
| Working from the terminal | Set up [gantry YAML](gantry-setup.md), [calibration](calibration.md), [deck YAML](deck.md), and [protocol YAML](protocol-yaml.md). |

The remaining instructions are for **installing from source**.

## Prerequisites

Use Python 3.11+ and Git for CubOS Operator (the core package alone supports Python 3.10+). **All shell commands
elsewhere in these docs assume a Unix-like shell.** On Windows, install
[Git for Windows](https://git-scm.com/download/win) (it bundles **Git
Bash**) and run every command in these docs from a Git Bash window — or
translate the command to PowerShell yourself. Where a step differs on
Windows, both are shown below.

### Windows

- Install [Git for Windows](https://git-scm.com/download/win). Use the Git
  Bash it installs for the commands in these docs.
- Install [Python 3.11 or newer](https://www.python.org/downloads/windows/).
  During setup, check "Add python.exe to PATH".
- Verify, from Git Bash or PowerShell:
  ```bash
  python --version
  git --version
  ```

### macOS

- Install [Homebrew](https://brew.sh) if you don't have it, then:
  ```bash
  brew install python@3.11 git
  ```
- Use the Homebrew `python3`, not the older system Python.

### Linux

- Install Python 3.11+ and Git with your distro's package manager, e.g. on
  Debian/Ubuntu:
  ```bash
  sudo apt install python3 python3-venv python3-pip git
  ```

For all platforms, you'll also need:

- For hardware runs: a GRBL-compatible gantry connected over serial. We
  currently support the
  [Genmitsu 3018-PROVer V2](https://www.sainsmart.com/products/genmitsu-3018-prover-v2-upgraded-semi-assembled-cnc-router-kit)
  (Cub) and the
  [ProverXL 4030 V2](https://www.sainsmart.com/products/proverxl-4030-v2)
  (CubXL), but CubOS should work with any GRBL controller that has homing
  switches.
- For real instruments: the vendor SDKs required by those vendor drivers
  (see [Instrument extras](#instrument-extras) below).

If you bought a CubOS system through [ursalabs.ai](https://ursalabs.ai), gantry
controller bring-up should already be handled. If you are setting up your own
machine, normalize controller direction, homing, and WPos reporting with
[Gantry Bring-Up](admin/gantry-bring-up.md) before calibration.

## Installation

```bash
git clone https://github.com/Ursa-Laboratories/CubOS.git
cd CubOS
python -m venv .venv
```

Activate the virtual environment:

- **macOS / Linux:**
  ```bash
  source .venv/bin/activate
  ```
- **Windows, Git Bash:**
  ```bash
  source .venv/Scripts/activate
  ```
- **Windows, PowerShell:**
  ```powershell
  .\.venv\Scripts\Activate.ps1
  ```

Then upgrade pip and install CubOS. Pip may print a notice that a newer version
is available; the first command below performs that update without depending on
the version numbers shown in the notice.

```bash
python -m pip install --upgrade pip
python -m pip install -e "packages/core[dev]"
python -m pip install -e services/api
```

The first install is the CubOS runtime (`cubos`); the second is the
`cubos_api` server that powers the [Operator UI](operator-ui.md). If you
plan to work only from the terminal with YAML files, the `services/api`
install is optional — but it is required before `python -m cubos_api` will
work. Note that `services/api` needs Python 3.11+ (the core package alone
works on 3.10).

!!! note "Every new terminal: activate the venv first"
    Everything above installs into `.venv`, so `python -m cubos_api` — and
    every other `python -m cubos...` command in these docs — only works
    while the virtual environment is active. Activation does not persist:
    each new terminal window starts without it. If a command fails with
    `No module named cubos_api` (or `No module named cubos`), re-run the
    activation command for your platform from
    [Installation](#installation) above — you never need to reinstall.

### Build the Operator UI (browser app)

The `cubos_api` server serves the Operator UI's compiled web assets from
`apps/operator-web/dist/`. That folder is not checked into the repository —
build it once with Node.js:

1. Install [Node.js 22.12+ (or 20.19+)](https://nodejs.org) (it includes
   `npm`).
2. From the repository root:

   ```bash
   cd apps/operator-web
   npm ci
   npm run build
   cd ../..
   ```

Skip this if you work only from the terminal. If you start
`python -m cubos_api` without building, the server still runs (the HTTP API
works), but it logs `compiled web assets were not found` and the browser
shows **404 Not Found** at `http://127.0.0.1:8742`. After building, start
`python -m cubos_api` again — the server only looks for the assets at
startup. Re-run `npm run build` whenever you pull changes that touch
`apps/operator-web/`.

### Start the Operator UI

With the virtual environment active, run from the repository root:

```bash
python -m cubos_api
```

Your browser opens at `http://127.0.0.1:8742` after a moment; leave the
terminal running. See [Use the Operator UI](operator-ui.md) for connecting
to the gantry and everything else you can do from the browser.

### Instrument extras

Instrument vendor SDKs are optional — install only the extras for hardware
you actually use. The extras currently defined in
`packages/core/pyproject.toml`:

| Extra | Installs | For |
|---|---|---|
| `dev` | `pytest` | Running the test suite |
| `docs` | `mkdocs`, `mkdocs-gen-files`, `mkdocstrings[python]`, `pymdown-extensions` | Building this documentation site |
| `asmi-vernier` (alias `asmi`) | `godirect` | Vernier Go Direct force sensor (ASMI instrument) |
| `potentiostat-admiral` (alias `potentiostat`) | `SquidstatPyLibrary` | Admiral Instruments SquidStat potentiostat |

For example, to work on an ASMI setup:

```bash
python -m pip install -e "packages/core[asmi-vernier]"
```

Instrument types without a dedicated extra (`filmetrics`, `pipette`,
`uv_curing`, `uvvis_ccs`, `camera`) don't require an extra vendor SDK install
beyond CubOS's base dependencies.

Customer/proprietary instruments ship as normal Python packages — install the
package with `python -m pip install`, and its instruments become available in
the gantry YAML like any built-in type.
