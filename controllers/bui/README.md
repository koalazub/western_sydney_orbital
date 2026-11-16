# Bui controller

[`Controller.py`](Controller.py) is the licensed STP entrypoint. Keep the
[`mvp/`](mvp/) folder beside it; the script adds its own directory to the
import path and imports the `mvp` package from there.

The local profile is `bui_alpha_beta_mvp`. The reviewed bounded adapter lives in
[`src/wsorb/controllers/_bui.py`](../../src/wsorb/controllers/_bui.py) and loads
only the allowlisted math modules from [`mvp/`](mvp/). Select it in **Controller
profiles** with the private controller runner, or run it through the sweep
harness.

This profile is experimental. The local adapter bounds the original accelerate
and sidestep requests to the reviewed caps; it is not certified avoidance or
docking. Use only one command writer per flight. Do not run this alongside WSO's
`stp_controller.py`, Tom's controller or another Python writer.

The code here is the teammate's original implementation. The local comparison
metadata and owner label are in `src/wsorb/controllers/catalog.json`.
