# Picus 2 1000 µL color-matching station

Operator UI: http://10.193.33.24:8742/ (Wi-Fi). Configs live on the Pi under `/home/cub/cubos-data/configs`; these local files are copies.

## Files

- Gantry: `picus1000_arducam_usb_seed.yaml`
- Deck: `color_matching_side_exit_seed.yaml`
- First dry pickup/ejection: `01_side_exit_pickup_check.yaml`
- Three-color transfer/mix/capture: `02_color_matching.yaml`

The Picus was identified as Sartorius Picus 2, serial 47684951, with stable serial path `/dev/serial/by-id/usb-Sartorius_Picus_2_47684951-if00` (currently `/dev/ttyACM0`), at 230400 baud. Model verification remains enabled. The front USB camera was identified as Arducam IMX323 on `/dev/video0`; the overhead camera is not part of this setup. Recheck `camera_id` if another USB camera is added or device numbering changes.

The GRBL settings mirror a live read from this controller (300/200/80 mm spans; 2 mm pull-off; 800 steps/mm). The provisional working envelope is X0–298, Y0–198, Z0–56, with 56 mm retained from the demo. Mechanical Z travel is explicitly 56 mm for this extended small CUB; the controller's pre-calibration 80 mm setting is retained separately as an expected firmware value, not a mechanical measurement. Calibration uses the 56 mm value. The pipette depth (-70), front-camera offsets (0,-28,0), deck locations and engagement heights are provisional. An offline PASS checks configured targets; it does not validate these physical measurements.

The stock holder is 2 rows × 6 columns (A1–A6, B1–B6) at 20 mm center-to-center spacing in both axes. Its A1 anchor remains provisional and must be calibrated against the physical holder.

## Continue from the saved working deck

Use `cub_deck.yaml` for ongoing calibration. Importing the original seed copies it over the working deck and can replace saved anchors. Refresh the browser page to reload current data; do not re-import the seed after calibrating. Plate A1 XY is now (17.083,71.694), taken from the operator-identified position. Its previous Z15 was retained because the reported pose was above A1, not a contact-height measurement.

## Operator sequence

1. In **Gantry**, load `picus1000_arducam_usb_seed.yaml`. Connect. If the controller shows Alarm, release the physical E-stop as needed and use the operator's Home/calibration flow.
2. Open gantry/instrument **Calibrate**. Use a fixed, repeatable reference near the front-left of the deck. Measure its actual height above the deck (including any holder). Choose the pipette as the contact/reference instrument, calibrate its XY/Z, and calibrate the front camera over the same mark. If using an attached tip during calibration, enable the tip-attached option and enter the measured extension (70 mm is provisional). Save as `picus1000_arducam_usb_calibrated.yaml`.
3. In **Deck**, load `color_matching_side_exit_seed.yaml`, then **Calibrate**. Calibrate plate A1/A2, stock-vial A1/A2 and rim heights, tip-rack A1/A2 and pickup Z, and the tip discard point. The inward rack is 8×12 with 10 mm pitch: seeded A1=(263,26,70), A2=(253,26,70), A12=(153,26,70). Save as `color_matching_side_exit_calibrated.yaml`.
4. In the deck's raw YAML, verify `side_exit.lift_mm` (at least 30 mm) and `side_exit.exit_x` (seeded X140) are still appropriate after calibration. These describe the opening/clearance, not the tip grid; calibrating A1/A2 does not relocate the exit automatically.
5. In each protocol's raw YAML, verify named corridor positions `rack_exit_lane` and `clear_of_plate`. These are absolute coordinates and must be updated if calibration moves the rack, plate or origin. The Y corridor is seeded at Y110, outside the rack footprint. Preserve the two-stage Z-then-X pickup.
6. With an empty tip and no liquids, validate and run `01_side_exit_pickup_check.yaml` using the **calibrated** gantry and deck files. Observe at least 30 mm of lift, then inward X-only exit, followed by movement through the clear corridor and discard. This consumes A12: replenish the rack before the color demo or change the demo's tip slots to fresh exposed tips.
7. Add red/yellow/blue stock to stocks.A1/A2/A3, confirm the plate's working capacity is at least 300 µL, and place a real tip-waste receptacle at the calibrated discard point. In **Protocol**, load `02_color_matching.yaml`, select the calibrated gantry/deck, Validate, then Run. Choose **No state tracking** for this first simple run unless you have explicitly initialized fluid/tip state. Start with a fresh rack and no tip already attached; accept the Picus remote-control prompt when requested.

The 17-step color demo picks A12/A11/A10, dispenses 100 µL of each stock into plate.A1, mixes 60 µL for three cycles, discards tips and captures an Arducam image. A return corridor before each discard avoids crossing the loaded tip rack. Camera capture records an image; it does not itself perform color analysis or optimization.

No homing, calibration, pipette actuation or protocol execution was performed during setup. Physical calibration and supervised validation remain required.
