# Diagonal routing and prompt status polling

Protocol routing attached the active instrument name to every access scope,
including ordinary transit. The diagonal eligibility check treated that identity
as fixture-access permission and skipped all diagonal candidates. Ordinary
same-Z transit now considers coordinated XY even with an instrument identity;
fixture/corridor access remains axis-constrained and all mounted-tool swept boxes
are still checked.

GRBL status is now requested before reading its response. Previously each
completion poll attempted an unsolicited read and could wait the full two-second
serial timeout before sending `?`. Acknowledgments and informational messages
are not completion; polling waits for a controller status and retains alarm/error
handling. Silent or noisy responses have bounded request/line counts. The serial
read timeout, feed rates, controller settings, and calibration are unchanged.

## Operator hardware validation (pending)

No agent-issued motion. After deployment, reconnect through Operator and verify
the configured deck, tools, calibration, and machine limits. With the operator
present and the physical stop accessible:

1. Use a no-liquid protocol move between two operator-verified clear points at
   the configured transit ceiling, differing by 5 mm in both X and Y. Verify the
   full tool envelopes clear the straight segment. Observe one coordinated XY
   segment and one `G01 X... Y...` in the command log, with separate Z movement.
2. At a verified clear pose with adequate Z room, command a 1 mm Z move and return
   through CubOS. Compare command/completion log timestamps with the former
   approximately 2.06-second delay. Check return-to-reference position physically.
3. Preview and then supervise a known obstructed diagonal route: confirm the
   planner retains a collision-free orthogonal/detour route. Verify existing tip
   access and withdrawal constraints on the station's actual rack.
4. During a clear low-speed transit, use Operator feed hold; confirm stopped/Hold
   state does not count as completed motion. Resume only under operator control.
   Alarm and disconnect paths are covered offline; do not deliberately crash the
   machine to exercise an alarm.

Physical diagonal accuracy, missed steps, actual clearance, and timing remain
unverified until the operator performs these checks. Offline tests do not prove
physical safety.

## Offline validation

The PR worktree's own editable core installation was verified before tests.
Focused driver, instrument-mount, planner, adversarial-route, and protocol-routing
suites: 240 passed, 15 subtests. Diff coverage against the stacked PR base
`review/coordinated-xy-base`: 97% (43/44 changed core lines).
