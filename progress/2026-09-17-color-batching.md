# Color campaign source preservation and six-sample batches

## Delivered behavior

The color builder now requires the selected source protocol in both real and mock modes. It preserves protocol 03's stock height -20 mm, mixing height -7 mm, 60 uL mixing volume, three cycles, and other supported command arguments. It no longer substitutes -8/-3/225 defaults. Unsupported source layouts are rejected rather than silently reduced.

Six samples run as one native batch: one red tip for six individual transfers, one yellow tip, one blue tip, then a separate clean mixing tip and camera score per well. A full batch has 54 steps and nine tips. Eighteen samples use three batches and 27 tips. Single-sample campaigns remain supported. Each sample retains its own parameters, well, score path, and measurement status. Optimizer proposals exclude pending points without inventing observations; incomplete batches stop without automatic replay.

## Verification

Exact Pi configuration copies passed offline route validation for six samples (144 targets, 138 plans), a two-sample tail (60 targets, 58 plans with prior tips virtually consumed), and the three batches of an 18-sample plan. Stock action Z is 38.5 mm and mix action Z is 31.5 mm. Temporary runtime checks cover 6+2 and 6+6 batches, initial recipes, unique proposals, partial failure, per-well provenance, and missing-score preservation. Existing campaign/optimizer checks and 17 updated explicit-source fixture checks passed. Frontend build/lint and source review passed.

No physical protocol, movement, or capture was initiated. Permanent new regression cases remain deferred at the user's request. TODO(iter): add permanent batch/source-preservation/proposal/recovery/UI migration regressions before merge.

## Pi deployment and current setup

Published branch: `feat/ade-camera-campaign-review`. Initial batch deployment commit: `b09f2158` (follow-up migration guard may be a later commit). The service was idle, stopped briefly to back up data, updated, restarted, and health-checked. Backup: `/home/cub/cubos-data/backups/color-batch6-20260917`.

`configs/campaign/color_match_2.yaml` explicitly selects `batch_size: 6` and source `03_routing_side_exit_review.yaml` in both spec and color setup. Existing gantry, deck, and protocol 03 bytes were verified unchanged. New setup is loaded in Operator without inventory or target evidence; no run was started. Older generated protocols and campaign history remain intact.

Inventory state 1 has an uncertain A2 pickup; state 3 has an uncertain 200 uL transfer from stocks.A1 to plate.A3 and an attached-tip record. Do not treat them as fresh inventory. Reconcile against the actual machine and liquids, or create a new truthful inventory record. State 2 reports 96 available tips, but this is not independent confirmation of the physical rack.

Offline diagnostic YAML and review notes are saved locally under `/Users/alexchan/Documents/Ursa/CubOS-local-configs/color-batch-2026-09-17/`. They illustrate geometry and sample mapping using an accepted historical reference; they are explicitly diagnostic, not an approved physical run.

## Operator validation still required

Load protocol 03 and the six-sample preset, confirm/reconcile physical inventory and bare-pipette state, review an accepted current target with matching camera settings, then build and validate. Inspect the first batch's stock/mix heights and verify dye tips dispense clear of liquid at the calibrated well reference. Start only a supervised batch and inspect each sample's result and state journal. Do not resume the cancelled old campaign to test this new workflow.
