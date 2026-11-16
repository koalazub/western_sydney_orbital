# Bui controller

[`Controller.py`](Controller.py) is the current licensed STP entrypoint. It is
Bui's self-contained 7 m/s alpha-beta tracker with the avoid/hold/correction
state machine and body-axis trial burns. It no longer imports the `mvp/`
package.

The [`mvp/`](mvp/) folder and the `bui_alpha_beta_mvp` profile are the earlier
local comparison implementation, loaded through the reviewed bounded adapter in
[`src/wsorb/controllers/_bui.py`](../../src/wsorb/controllers/_bui.py). They are
retained for local seed comparisons and are not imported by the current STP
entrypoint. Select the profile in **Controller profiles** with the private
controller runner, or run it through the sweep harness.

This profile is experimental. The local adapter bounds the original accelerate
and sidestep requests to the reviewed caps; it is not certified avoidance or
docking. Use only one command writer per flight. Do not run this alongside WSO's
`stp_controller.py`, Tom's controller or another Python writer.

The current entrypoint follows the flattening to `controllers/`; the old
`examples/bui/` path is no longer used.
