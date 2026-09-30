# Picus 2 120 µL + vertical 42 mm tip rack (cubos.local)

Station config change only; no repo code changed. Branch `feat/ade-camera-campaign-review` @ 86261a4.

## What changed on the Pi (2026-09-17, backup `cubos-data/backups/pre-120ul-vertical-20260917-163956`)

- `gantry/picus120.yaml`: pipette port → `usb-Sartorius_Picus_2_47982929-if00`; `cnc.safe_z` 66.5 → 94.601 (= z_max) so an attached tip clears loaded rack tips during XY travel. Working volume, block height 40, depth −42 (tip-attached calibration, frame is tip-end referenced) are the operator's 16:04 wizard values, unchanged.
- `deck/cub_deck.yaml` tips: side-exit removed, `access.pick_up_tip: vertical`, `tip_length` 70 → 42, `pickup_z` 11 → **81.0** (operator confirmed press-down at carriage Z 39.0, 0.4 mm above z_min 38.6; bare-nozzle plane is carriage + 42). The saved 11 was carriage 53 − 42, the tip length subtracted in the wrong direction. Waste point shifted by the measured frame delta (−10.8, +1.7, +2.8) → (219.2, 130.7, 62.8); confirm on hardware.
- `protocol/05_color_match_120ul.yaml`: protocol 03 with mix height −7 → −2. Tip end bottoms out at Z 38.6, 2.4 mm below the plate rim (41); −7 is unreachable. Stocks at −20 keep 0.9 mm margin.
- Fluid state 5 `color_match_5` seeded from state 4 (A3 200, A4 50, yellow 5000, blue 4980) + red 5000 (operator-confirmed), 96 tips @ 42 mm. State 4 stays bound to the 70 mm deck fingerprint and is retired.
- Preset `campaign/color_match_120.yaml`: gantry picus120, source 05, RGB target (157, 0, 189), state 5, A5–B10 in 3 batches of 6.

## Verified offline

`validate_setup` PASS (bounds, semantics, 28/36 collision plans) for picus120 + cub_deck + {05 source, generated batch protocol}; `/campaigns/validate` accepted the spec. GRBL `$27` now reads 2.0 (wizard programmed it). Not hardware-validated.

## Before the first run

1. Engagement has no downward margin (0.4 mm to the Z floor); if pickups are unreliable the rack needs a riser, not a lower `pickup_z`. Rebuild the campaign after any deck edit.
2. Confirm the pipette is physically bare (state 5 assumes bare).
3. Disconnect/Connect the gantry with `picus120.yaml` so the session picks up the new safe_z; instruments connect at run start from the file.
4. Supervised dry run: first pickup + drop at waste before batch 1.
