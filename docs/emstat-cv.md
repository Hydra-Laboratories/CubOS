# EmStat cyclic voltammetry

CubOS's `emstat` potentiostat uses the pinned BU-KABlab hardpotato driver.
Its CV script has several restrictions:

- `cycles` is the number of complete scans. One cycle returns to the starting
  potential after visiting both vertices.
- `end_V` must equal `start_V`. The underlying script cannot use a separate
  final potential.
- The voltage step is `scan_rate_V_per_s * sampling_interval_s`. It must be
  at least **0.001 V (1 mV)**. For example, 0.05 V/s and 0.01 s produce a
  0.5 mV step, which this driver cannot represent. At that scan rate, 0.02 s
  gives a 1 mV step. Choose settings suitable for your experiment.
- The scan rate must be at least 0.001 V/s. The dependency truncates voltage
  values, step size and scan rate to whole millivolt units; use settings that
  are exact multiples of those units. Vertices must remain distinct after
  conversion.

CubOS rejects unsupported settings before sending the CV experiment. It does
not automatically enlarge the voltage step. These restrictions are specific
to the EmStat dependency, not limits imposed on other potentiostat vendors.

## If a run returns no data packages

This means hardpotato returned no parsed measurement curves; it occurs before
CubOS stores or exports the result. Record the complete error, CV settings,
installed CubOS revision and hardpotato revision. Preserve the generated
`cv_*.mscr` and `.txt` files in the configured `data_dir` (or the
`cubos_emstat_*` temporary directory reported on connection).

Older CubOS versions passed the cycle count directly to hardpotato, which
subtracts one when generating its script. A one-cycle request therefore
produced invalid `nscans(0)`. Small voltage steps could also become `0m`.
PANDA-BEAR's default `nSweeps=2`, `dE=0.001` avoided these cases.

## Operator verification after an update

Use a lab-approved test cell, existing safe potential window and established
electrode positioning. With start and end potentials equal, use a 1 mV step
and run one cycle, then two. Confirm the generated script contains `nscans(1)`
and `nscans(2)`, respectively; confirm the measured curves match those counts
and the downloaded data contains aligned, nonempty time, voltage and current
columns. Recheck the established OCP and CA procedures.

Software tests use generated scripts and simulated transport. They do not
establish physical instrument performance.
