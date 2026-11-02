# Tom controller

[`Controller.py`](Controller.py) is the licensed STP entrypoint. Its supporting
modules live in this folder.

The local target-seeking profile is `tom_kalman_velocity_mvp`, loaded through the
reviewed bounded adapter in
[`src/wsorb/controllers/_tom.py`](../../src/wsorb/controllers/_tom.py). Select it
in **Controller profiles**, or run it through the sweep harness.

[`UserAPI.py`](UserAPI.py) is the stock body +Z template, not a target-seeking
controller. Its local profile is `userapi_body_z` and it is marked
non-target-seeking in the catalog.

Use only one command writer per flight. Do not run this alongside WSO's
`stp_controller.py`, Bui's `Controller.py` or another Python writer.
