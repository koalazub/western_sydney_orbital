# Tom controller (earlier version)

[`Controller.py`](Controller.py) is the earlier STP entrypoint, retained for
comparison rather than as the current one. The current no-Kalman 8 m/s version
is [`../polyfit/Controller.py`](../polyfit/Controller.py). This folder also
keeps the earlier tracker and avoidance modules beside the snapshot.

The local comparison profile is `tom_kalman_velocity_mvp`, loaded through the
reviewed bounded adapter in
[`src/wsorb/controllers/_tom.py`](../../../src/wsorb/controllers/_tom.py). That
adapter reproduces the earlier Kalman tracker and 17 m/s cruise and uses
[`debris_tracker.py`](debris_tracker.py) and [`avoidance.py`](avoidance.py).
The STP snapshot imports the tracker and avoidance modules beside it. Select the
profile in **Controller profiles**, or run it through the sweep harness.

The stock body +Z template is preserved at
[`docs/history/controllers/tom/UserAPI.py`](../../../docs/history/controllers/tom/UserAPI.py),
not in the current controller folder. Its local profile is `userapi_body_z` and
it is marked non-target-seeking in the catalog. The display-only `live_map.py`
is preserved beside it.

Use only one command writer per flight. Do not run this alongside WSO's
`stp_controller.py`, Bui's `Controller.py` or another Python writer.
