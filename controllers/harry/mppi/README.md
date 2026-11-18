# Harry MPPI: terminal-coast correction

Derived on 18 November 2026 from the current `controllers/ali/controller.py`
on `harry/optimisation`, including its existing native tuning input hook.
Ali El Ali is the original controller author; Harry's variant changes terminal
coasting. The source checkout origin is
`800413a72396892a5337978a12e2dfd0247aa13f`; the source file includes local tuning
input changes, so that commit alone does not identify its contents.

The exact copied source has SHA-256
`e74e206a57dfe6590e3a56075165c6903f5c5df5e74b575bba16ac92b599d1fd`.
It is also frozen in
`runs/20261118T070841Z-phobos-mppi-smac3/sources/ali/controller.py`.
The earlier overview's `ali_mppi_v2` is a different version, source SHA-256
`2f00c3eb300a5cc1bcd1edc41aecaf2b4b9dafbfa09e38d725292ef141040cb1`.
No results from either source establish this variant's native performance.

## Change

Previously, the controller cut translation thrust whenever it was within 250 m
of the destination centre and travelling at least 4 m/s, even if its trajectory
missed the 50 m arrival sphere. Terminal coasting now additionally requires that
the forward, constant-relative-velocity trajectory intersects the arrival sphere.
Misses, receding trajectories and stationary relative motion outside the sphere
retain course correction. Inside the sphere, the coast condition remains allowed.

The pure `coast_intersects_goal` calculation uses destination-relative velocity.
The native observation now reads destination position and velocity from the same
SDK state object. Callers without target velocity retain the stationary-target
assumption. This is a local straight-line prediction, not a guarantee under
future gravity, target acceleration or contact. Emergency and collision escape
priority remain unchanged.

The original Ali file is preserved. All other defaults are copied unchanged,
including launch clearance 0 m, emergency margin 3 m, line tracker, disabled MPPI
uncertainty inflation and disabled previous-plan candidate. This does not restore
the archived MPPI-v2 defaults (including its 25 m launch clearance).

## Tuning

The optimiser now stages this directory for the active MPPI arm. The historical
arm key `ali` is retained for compatibility, while author/method/source-change
metadata explicitly identify this Harry variant. Baseline and proposals use the
same patched revision. The existing `WSORB_STP_TUNING` environment input supplies
cruise speed, planning horizon and risk weight and explicitly enables
`avoidance="mppi"` and `plan_steering=true`. No native STP flights are launched by
preparation or focused tests. New campaign snapshots and SHA-256 bundle hashes
identify the exact variant used for each flight.
