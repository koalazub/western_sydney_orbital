# Polyfit optimiser instructions for Bui's LLM

Read and follow the repository `AGENTS.md` too. These notes describe the separate
Polyfit experiment. Explicit owner instructions take precedence. Work from the
project root; do not alter another running or frozen campaign.

## Objective and frozen implementation

Run the existing licensed native STP harness with SMAC3 to test whether the copied,
fixed Polyfit improves exact native score while preserving arrivals and limiting
damage. Use `experiments/stp_optimiser/bui_polyfit.json`, which selects only
`controllers/harry/polyfit/Controller.py`. Its copy provenance and finite cone-angle
fix are in `controllers/harry/polyfit/readme.md`. Preserve Tom's original controller.
Do not select the similarly named `polyfit_bodge_v2` or an MPPI arm by mistake.

The profile pins the controller SHA-256. Freeze the implementation before tuning.
Do not modify control logic, settings bounds, scorer, seeds, mission or losses to
improve results or make a failed check pass. Request a separately reviewed experiment
if a logic or scenario change is needed. Do not infer that this version is the
best full-bank Polyfit from its exception-selected historical follow-up.

## Machine and simulator setup

Use Bui's licensed Windows STP 0.54.2 and its native Python 3.12 interpreter via UV.
The optimiser has its own locked project at `experiments/stp_optimiser`; do not
install its packages into STP's SDK environment. Keep the desktop unlocked and
signed into STP for rendered flights. Native flights use the existing approved
Windows workflow; this task does not build or deploy the web simulator.

Obtain the actual STP install, STPro user root and STU4-Sim package locations from
Bui or his existing installation. Pass `--install` and `--package` explicitly.
The branch deliberately contains no copy of STU4-Sim. Use Bui's installed
configs and the existing team update process. Preparation compares exact reviewed
mission, scorer, radar, actuator, spawner, debris-entity and thruster-data hashes.
An "up to date" label alone does not establish matching conditions.

The base package revision is `0d753a28e1a4265b2839bf8e9fbbc9194efafb87`.
The mission is `Sims/FINAL_SUBMISSION.simconf`, SHA-256
`04abcfb54321b22b68322ecf98d785cc548a7bd7b7ba447aac511d9a4d38e06e`;
scorer SHA-256 is `ba8cc418522040692df09de7bbdaa2522b17e6e6cfa94d3364febc40ddf93cc4`.
If the final-submission mission is absent, obtain Harry's reviewed configuration;
it contains a prior camera-reference adjustment and is not guaranteed by checking
out the upstream revision. Report the exact missing/mismatched filenames. Never
silently substitute a mission, rewrite a pin, edit installed configs, provision
credentials, change account settings or create a replacement launch workflow.
Do not copy an already prepared campaign from Harry's machine to Bui's machine.

## Design and settings

- Baseline plus seven SMAC3 proposals on the same eight training seeds: 64 flights.
- Baseline plus the top two on four additional validation seeds: 12 flights.
- Freeze the finalist using all 12 tuning seeds; baseline and finalist on six
  held-out seeds: 12 flights. Total 88 before evidence retries.
- Training: 438237952, 2486588451, 3927309276, 3969613433, 428062188,
  4266923988, 2541959213, 649422304.
- Validation: 1705635923, 2272052480, 1475239320, 3546960591.
- Held-out: 100, 1729, 3253, 4999, 7919, 104729. Keep these scores out of SMAC3
  and finalist selection. Preserve the full original 22-seed bank and its pin.
- Tune cruise speed [6,12] m/s, margin [3,12] m, horizon [15,60] s through the
  existing controller `environment` field. Baseline values are 8, 6 and 50.
- Keep all other settings fixed, normal scheduling, one command writer, radar seed
  100, 300 s ceiling, and collision early abort disabled. Allow collision recovery.

These seeds vary debris impulses in one saved field. Do not claim independent
layouts, general collision safety, deterministic STP execution or exact WASM/STP
equivalence. Expected flight time is about 6 h 7 min to 7 h 20 min before overhead
and retries. Do not start flights merely because preparation was requested. If
Bui explicitly asks to launch, use the frozen adapter without repeated permission
questions unless an unresolved blocker requires his decision.

## Preparation, run and recovery

Follow `experiments/stp_optimiser/BUI_POLYFIT.md` for exact root-directory commands.
Prepare into a fresh `runs/<UTC timestamp>-bui-polyfit-smac3` directory with
`--config experiments/stp_optimiser/bui_polyfit.json --install <path> --package <path>`.
Check that `experiment.json` selects only `polyfit`, has an 88-flight budget and
records the intended settings and seeds. Preparation starts no simulations.

Launch and resume the same preparation using its `adapter/optimise.py` and locked
`adapter` UV project, not mutable working sources. Use `--resume` only after the
old runner has exited. Query the existing harness `status` and request its clean
`stop` for the active `native` stage. Wait using status/evidence events and process
completion, never fixed sleeps. Preserve all source snapshots and raw evidence.

Unflown partial stage preparation is retained under `native-incomplete-<UUID>` and
rebuilt from the same frozen proposal/seed inputs. Physical receipts, manifests or
telemetry block that automatic rebuilding. Never delete attempt folders, choose
the best retry, reset retry budgets or automatically replace a physical flight.
The harness alone owns process cleanup and its three evidence-failure retries.
Exhausted infrastructure failures remain incomplete and halt selection. An initial
preparation failure needs a fresh output path, not an overwrite. Investigate
ambiguous or physical-flight interruptions with Bui before proposing recovery.

## Evidence and return to Harry

Use `receipt.json` for the exact native score and outcome, `manifest.json` for
provenance, and telemetry for cap/collision diagnosis. Keep `official_score=null`
for unscored caps. The declared optimisation loss is 1000 for unscored caps or
controller exceptions, with a 500 sensitivity check; missing infrastructure evidence
cannot improve a mean. Require matched coverage. A finalist must preserve baseline
arrivals, keep mean paired damage increase at most 2092 J, and strictly improve
matched loss. Baseline wins ties or when no candidate qualifies.

Report paired scores, arrivals, damage, fuel, arrival time/speed where available,
caps and retry incidence. Last telemetry speed is not atomic with scoring.
Keep different machines/cohorts and incompatible historical conditions separate.
Do not reinterpret missing native scores as successes or fabricate evidence.

At completion, provide `polyfit/screen.json`, `polyfit/selection.json`,
`polyfit/final-test/comparison.json`, campaign JSON, frozen sources and native
receipt/manifest evidence. The documented Compress-Archive command excludes the
recreatable optimiser `.venv`. Preserve partial results and original errors if
halted. Present the resulting archive to Bui for return to Harry; do not send or
upload externally unless Bui explicitly identifies the recipient/channel and asks.
The harness still freezes config snapshots under ignored `runs/` for provenance;
leave those existing snapshots intact. No paid runner infrastructure, deployment,
automation or account-wide change is part of this experiment.
