# Tom controller

[`polyfit/Controller.py`](polyfit/Controller.py) is Tom's current licensed STP
entrypoint, brought over from `Legolas4224/STU4-Solution` `main` at `2e4553a`.
It is the no-Kalman `Rock`/`Tracker`/`Avoider`/`BurnGovernor`/`GoalPlanner`
version at 8 m/s. Only repository formatting, unused imports and that cruise
value were adjusted from the fork file.

[`kalman/`](kalman/) is the earlier version, retained for comparison. Its
`tom_kalman_velocity_mvp` local profile reproduces the earlier Kalman tracker
and 17 m/s cruise through the reviewed bounded adapter in
[`src/wsorb/controllers/_tom.py`](../../src/wsorb/controllers/_tom.py);
[`kalman/Controller.py`](kalman/Controller.py) is the earlier STP snapshot that
stays beside the tracker and avoidance modules.

Use only one command writer per flight. Do not run either entrypoint alongside
WSO's `stp_controller.py`, Bui's `Controller.py` or another Python writer.

The controller index for the whole team is [`../README.md`](../README.md).

[`polyfit_v2_bodge/Controller.py`](polyfit_v2_bodge/Controller.py) copies updated
upstream Polyfit with only its malformed `Dodge info` simulator alert commented
out. It is distinct from `polyfit_bodge_v2`. Its native exception-seed follow-up
is documented in `docs/analytics/stp-runs/20261117T040830Z-HY/updated-candidates.md`.

[`polyfit_dodge/Controller.py`](polyfit_dodge/Controller.py) is the current
target-cone, time-to-collision and progress-candidate version. Its local native
review, including the avoidance deadlock on seed 101, radar timing regression and
fuel/points trade-off, is in
[`docs/analytics/tom-polyfit-dodge/README.md`](../../docs/analytics/tom-polyfit-dodge/README.md).
