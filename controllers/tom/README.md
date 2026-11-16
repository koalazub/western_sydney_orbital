# Tom controller

[`Controller.py`](Controller.py) is Tom's current licensed STP entrypoint,
brought over from `Legolas4224/STU4-Solution` `main` at `2e4553a` (the no-Kalman
`Rock`/`Tracker`/`Avoider`/`BurnGovernor`/`GoalPlanner` version) with the cruise
speed set to the 8 m/s Tom described. The entrypoint is self-contained and does
not import the older tracker modules in this folder. Only repository formatting,
unused imports and that cruise value were adjusted from the fork file.

The local comparison profile is still `tom_kalman_velocity_mvp`, loaded through
the reviewed bounded adapter in
[`src/wsorb/controllers/_tom.py`](../../src/wsorb/controllers/_tom.py). That
profile is the earlier Kalman adapter and uses
[`debris_tracker.py`](debris_tracker.py) and [`avoidance.py`](avoidance.py);
those files are retained for the comparison, not imported by the current STP
entrypoint. Select the profile in **Controller profiles**, or run it through the
sweep harness.

The stock body +Z template is preserved at
[`docs/history/controllers/tom/UserAPI.py`](../../docs/history/controllers/tom/UserAPI.py),
not in the current controller folder. Its local profile is `userapi_body_z` and
it is marked non-target-seeking in the catalog. The display-only `live_map.py`
is preserved beside it.

Use only one command writer per flight. Do not run this alongside WSO's
`stp_controller.py`, Bui's `Controller.py` or another Python writer.
