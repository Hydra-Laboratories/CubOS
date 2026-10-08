# CubOS Pi update — 2026-09-14

The Pi now runs `deploy/cubos-pi-routing-2026-09-14` at `52c7fc74`.

- Live Operator: http://10.193.33.24:8742/
- Simulation review: http://10.193.33.24:8760/?profile=saved-side-exit

In Operator, use `picus1000_routing_review.yaml`, `color_matching_routing_review.yaml`, and `03_routing_side_exit_review.yaml` or `04_routing_waste_review.yaml`. They are new files beside the original calibrated setup, which was preserved. The new gantry copy points at the attached Picus USB serial47683878 and uses safe Z66.5 within the existing56mm travel span.

The gantry is connected and Idle. CubOS reports a calibration warning: actual homing pull-off `$27=0.5mm`, while the saved expectation is2mm. Review this in the calibration flow before relying on the saved coordinates or running physically. No controller setting was changed to hide the warning, and no motion or pipette actuation was performed during deployment.

Both review protocols passed offline setup validation; three simulator demonstrations ran on the Pi. Nominal collision geometry still needs physical measurement and supervised validation. Routing v1 does not support durable fluid-state execution/resume; do not bypass that restriction for a physical active-learning campaign.

Rollback backups: `/home/cub/cubos-data/backups/routing-2026-09-14`. The in-app updater follows the deployment branch so it will retain these changes. Return its branch to main only when these features are integrated there or intentionally rolling back.
