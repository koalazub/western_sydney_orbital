# Tom controller

[`Controller.py`](Controller.py) is the current licensed STP entrypoint. Its
supporting modules live in this folder.

The local target-seeking profile is `tom_kalman_velocity_mvp`, loaded through the
reviewed bounded adapter in
[`src/wsorb/controllers/_tom.py`](../../src/wsorb/controllers/_tom.py). Select it
in **Controller profiles**, or run it through the sweep harness.

The stock body +Z template is preserved at
[`docs/history/controllers/tom/UserAPI.py`](../../docs/history/controllers/tom/UserAPI.py),
not in the current controller folder. Its local profile is `userapi_body_z` and
it is marked non-target-seeking in the catalog. The display-only `live_map.py`
is preserved beside it.

Use only one command writer per flight. Do not run this alongside WSO's
`stp_controller.py`, Bui's `Controller.py` or another Python writer.
