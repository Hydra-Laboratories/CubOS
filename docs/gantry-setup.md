# Set Up Gantry YAML

The gantry YAML describes your machine: which gantry it is and which
instruments are mounted. Create it **before calibrating** — calibration reads
the mounted instruments from this file and writes its measurements back into
it.

Don't write one from scratch. Copy the seed config from `packages/core/configs/gantry/`
that matches your machine and instrument, rename the copy for your setup,
and adjust the instruments if needed.

## CUB vs CUB XL Seeds

CUB and CUB XL are different machine sizes, and every seed is built for one
of them (the `gantry_type` line at the top of the file). Pick a seed by
matching your machine first, then your instrument:

| Seed | Machine | Instruments |
|------|---------|-------------|
| `cub_filmetrics.yaml` | CUB | Filmetrics |
| `cub_raman.yaml` | CUB | Raman (ASMI mount) |
| `cub_sharc.yaml` | CUB | UV curing |
| `cub_xl_asmi.yaml` | CUB XL | ASMI |
| `cub_xl_sterling.yaml` | CUB XL | potentiostat + pipette |
| `cub_xl_sterling_3_instrument.yaml` | CUB XL | ASMI + potentiostat + pipette |
| `cub_xl_panda.yaml` | CUB XL | potentiostat + camera + capper |

If no seed has your instrument, copy any seed for your machine and swap the
`instruments:` entries. Never change `gantry_type` to make a seed fit — a
CUB config on a CUB XL (or vice versa) has the wrong travel limits and can
crash the gantry into the frame.

## Define Instruments

Instruments live under `instruments:` in the gantry YAML. The map key is the
name protocols use (`instrument: asmi`). A minimal entry is a type and a
vendor:

```yaml
instruments:
  asmi:
    type: asmi
    vendor: vernier
```

- **Mounting offsets are measured during [calibration](calibration.md)** —
  you don't fill them in by hand.
- **Some devices need connection settings** (a serial `port`, a
  `serial_number`, a vendor file path). The seeds already contain working
  values for their instruments — change only what your device needs.
- **Set `offline: true`** on an instrument to run without that hardware
  attached.

Supported types: `asmi`, `capper`, `filmetrics`, `lighting`, `pipette`,
`potentiostat`, `uv_curing`, `uvvis_ccs`, `camera`, `mounted_tool`.

Every instrument entry accepts these shared fields:

- `type` *(str, required)* — a type from `packages/core/src/cubos/instruments/registry.yaml`.
- `vendor` *(str, required)* — a vendor registered for that type. The
  `type`/`vendor` pair tells CubOS which Python driver class to load.
- `offset_x`, `offset_y`, `depth` *(float, default `0.0`)* — physical mounting
  offsets from the gantry's reference point. Calibration writes these for normal
  operator flows.

Driver-specific fields pass through to the vendor driver's constructor. Use
those fields for serial ports, serial numbers, executable paths, DLL paths, and
other vendor settings. Set `offline: true` to run a driver in its synthetic or
no-hardware mode.

`measurement_height` and `interwell_scan_height` are not instrument fields
anymore. CubOS rejects them in gantry YAML so they cannot be swallowed as
driver-specific extras; put those heights on the protocol `measure` or `scan`
step instead.

External instrument packages register through the
`cubos.instrument_registries` entry point group. For local overlays, set
`CUBOS_INSTRUMENT_REGISTRY_PATHS` to one or more registry YAML files separated
by the platform path separator (`:` on macOS/Linux, `;` on Windows).

Worked examples:

**`asmi` / `vernier`** — Vernier Go Direct force sensor. The `godirect` SDK
auto-detects the sensor over USB.

```yaml
instruments:
  asmi:
    type: asmi
    vendor: vernier
    offset_x: 0.0
    offset_y: 0.0
    depth: 0.0
    force_threshold: -50
    sensor_channels: [1]
```

**`filmetrics` / `kla`** — KLA Filmetrics thin-film measurement, driven through
a vendor executable and recipe file:

```yaml
instruments:
  filmetrics:
    type: filmetrics
    vendor: kla
    offline: true
    offset_x: 0.0
    offset_y: 0.0
    depth: 0.0
    exe_path: "C:\\Filmetrics\\Filmeasure.exe"
    recipe_name: "my_recipe"
```

**`pipette` / `opentrons`** — Opentrons pipette over a serial connection:

```yaml
instruments:
  pipette:
    type: pipette
    vendor: opentrons
    pipette_model: p300_single_gen2
    port: "COM7"
    baud_rate: 115200
    offline: true
    offset_x: 180.0
    offset_y: -25.0
    depth: 0.0
```

**`pipette` / `sartorius`** — Sartorius Picus 2 electronic pipette over USB
serial. Unlike the Opentrons vendor (a CubOS-driven stepper on a bare pipette
body), the Picus owns its own piston, so there is no plunger geometry to
calibrate — volumes are commanded directly:

```yaml
instruments:
  pipette:
    type: pipette
    vendor: sartorius
    pipette_model: picus2_1ch_1000   # or picus2_1ch_10, picus2_1ch_120, picus2_1ch_5000
    port: "/dev/ttyACM0"
    offline: true
    offset_x: 180.0
    offset_y: -25.0
    depth: 0.0
    # Optional, shown with defaults:
    # min_battery_percent: 20.0      # refuse to start below this charge
    # verify_model: true             # fail closed if the device is a different model
    # blowout_delay_ms: 3000
    # whole_microlitres_only: false  # force integer uL on the wire
```

The pipette asks for on-screen confirmation before accepting host motor
control; the driver satisfies that during `connect()` and, if it times out,
fails with a message naming the softkey to press.

Releasing motor control and the piston initialisation on connect both sweep
the piston, which expels whatever is in the tip. If a run fails between an
aspirate and its dispense, the driver therefore keeps the session open and
armed instead of releasing it, and refuses to home, until the liquid has been
dispensed or blown out (a one-step `blowout` protocol at a vial or the tip
disposal works). The warning is logged as `Pipette still holds N uL`.

**`potentiostat` / `admiral`** — Admiral SquidStat, addressed by serial port and
channel number:

```yaml
instruments:
  potentiostat:
    type: potentiostat
    vendor: admiral
    port: "/dev/ttyACM1"
    channel: 0
    offline: true
    offset_x: -55.0
    offset_y: 0.0
    depth: 58.0
```

**`uv_curing` / `excelitas`** — Excelitas UV curing lamp over serial:

```yaml
instruments:
  uv_curing:
    type: uv_curing
    vendor: excelitas
    offline: true
    offset_x: 0.0
    offset_y: 0.0
    depth: 0.0
    port: "/dev/ttyACM0"
    baud_rate: 19200
    default_intensity: 100.0
    default_exposure_time: 1.0
```

**`uvvis_ccs` / `thorlabs`** — Thorlabs CCS spectrometer, addressed by device
serial number plus the vendor driver DLL path:

```yaml
instruments:
  uvvis_ccs:
    type: uvvis_ccs
    vendor: thorlabs
    offline: true
    offset_x: 0.0
    offset_y: 0.0
    depth: 0.0
    serial_number: "M00123456"
    dll_path: "TLCCS_64.dll"
    default_integration_time_s: 0.24
```

**`camera` / `mount_only`** (or `raspberry_pi`) — a fixed-mount camera used as a
positional reference:

```yaml
instruments:
  camera:
    type: camera
    vendor: mount_only
    offline: true
    offset_x: 0.0
    offset_y: 0.0
    depth: 32.0
```

**`camera` / `flir`** (or `opencv`) — an acquiring camera for the `capture`
and `image_well` protocol commands (see [Protocol YAML: Lighting and imaging
commands](protocol-yaml.md#lighting-and-imaging-commands)). `flir` drives a
FLIR camera through the proprietary Spinnaker/PySpin SDK (manual install
from https://www.flir.com/products/spinnaker-sdk/); `opencv` drives a
plain USB webcam (`camera_id: -1` auto-detects the first responsive index).
Offline, both write placeholder PNGs so dry runs exercise the full
capture/persistence path:

```yaml
instruments:
  camera:
    type: camera
    vendor: flir
    camera_id: 0
    offline: true
    offset_x: 0.0
    offset_y: 0.0
    depth: 32.0
```

**`lighting` / `pawduino`** — the PANDA imaging lights (`white` ring +
`contact` red/blue), driven by the `set_lights` protocol command and
`image_well`. Calibration skips lighting instruments: they are co-mounted
with the camera, so the calibrated camera offsets are copied onto them
automatically. The capper and
pipette share this Arduino — give every Pawduino-backed instrument the
**same** `port`; the shared serial link opens it once and closes it when
the last instrument disconnects:

```yaml
instruments:
  lights:
    type: lighting
    vendor: pawduino
    port: "COM8"
    offline: true
    offset_x: 0.0
    offset_y: 0.0
    depth: 0.0
```

**`capper` / `pawduino`** — an electromagnet-actuated vial capper/decapper
over the same Arduino serial link family as the `pipette`/`opentrons`
driver (see [Protocol YAML: Capper commands](protocol-yaml.md#capper-commands)
for the `decap`/`cap` motion sequence these fields parameterize):

```yaml
instruments:
  vial_capper_decapper:
    type: capper
    vendor: pawduino
    port: "COM8"
    baud_rate: 115200
    offline: true
    offset_x: 62.9
    offset_y: 5.1
    depth: 61.5
    engage_depth_mm: -15.0
    capture_retries: 2
    capture_settle_s: 1.0
```

`park_position` is retired: `decap`/`cap` leave the tool at `safe_z` above
the vial and the next command lifts before it travels. A config that still
carries the key loads with a warning and the key is ignored; delete it.

`engage_depth_mm`/`capture_retries`/`capture_settle_s` are
required, not driver defaults — every capper's decap/cap motion sequence
comes entirely from this config, never hardcoded. `vendor: mock` is an
offline-only in-memory simulation useful for dry runs without any Arduino
attached.

**`mounted_tool` / `mount_only`** — any other passive mounted tool tracked
for offsets only (no actuation, no sensor):

```yaml
instruments:
  center_lens:
    type: mounted_tool
    vendor: mount_only
    offline: true
    offset_x: 58.0
    offset_y: 0.0
    depth: 48.0
```

## Next Step

Continue to [Calibrate Gantry](calibration.md).
