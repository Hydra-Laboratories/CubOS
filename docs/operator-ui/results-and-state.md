# Results and State

A **campaign** groups a run's measurements. Two views collect what a protocol run leaves behind: **Results** holds the
instrument measurements, grouped into campaigns; **State** holds the
liquid-handling record. **Visualize** is a full-size copy of the deck map.

## Get Results

Every protocol run creates a campaign in the CubOS database, and every
`measure` or `scan` step stores its instrument output in it. Open the
**Results** view to see them.

![The Results view with two campaigns, annotated](../images/operator-ui/results.webp)

1. **Results tab.**
2. **Campaign row.** One per run, newest first, with the run's description
   and when it last recorded a measurement.
3. **Measurements.** How many instrument measurements the campaign holds.
   Motion-only runs show 0 and their **Download Data** button stays disabled.
4. **Download Data.** Downloads the campaign's raw data as one ZIP of CSV
   files that open directly in Excel: each instrument's settings and recorded
   values, every curve point, the list of wells, and a README explaining each
   column. Raw ASMI files are in `asmi/raw/`. See
   [Data](../data.md#download-data-zip) for the layout.
5. **Refresh.** Re-reads the campaign list.

The **Last Campaign** box in the header shows the number of the campaign
created by the most recent run, so you can find it in this list.

For where the database lives, what each table holds, and how to export from
the command line, see [Data](../data.md).

## Inspect Liquid State

If a run used **New fluid state** or **Resume existing state** (see
[Protocols](protocols.md#track-liquid-state-across-runs)), the **State** view
shows the resulting record.

![The State view after a pipetting run, annotated](../images/operator-ui/state.webp)

1. **Fluid state.** Pick which saved state to inspect. Each is listed by
   number and label.
2. **Containers.** Every container on the deck with its role, current
   volume against its working volume, and composition. Here the stock vial
   `s1` was seeded with 15 000 µL of water and two transfers moved 200 µL
   and 400 µL into the waste vials.
3. **Tips and attached pipette.** Whether a tip is currently on the
   pipette, and each tip-rack slot's status: **available**, **consumed**,
   or uncertain.

Further down, **Caps** lists cap state for capper-managed vials. The record
reflects commands and your observations; it is not a sensor reading of the
actual contents.

### Resolve an interrupted operation

If **Pending operations** lists an uncertain step, inspect the machine
before resuming that state:

1. Click **Resolve** beside the operation.
2. Choose **Applied** if the recorded action happened, or **Not applied**
   if it did not. If you cannot tell, leave it unresolved and ask for help.
3. Enter **Operator** and **Reason**, describing what you observed.
4. Click **Submit resolution**.

See [Interrupted operations](../fluid-state.md#interrupted-operations) for
what each resolution records.

## Visualize the Deck

The **Visualize** view shows the same deck map as the right-hand panel,
scaled up to the full left panel. Labware is drawn at its calibrated
position, instruments are drawn around the head, and the **HEAD** crosshair
follows the live gantry position. It is useful on a small screen, or when
you want to watch a run without the editors in the way.

![The Visualize view, showing the full-size deck map](../images/operator-ui/visualize.webp)
