# Phobos SMAC3 experiment

Use the `harry/optimisation` checkout. This thin adapter uses the licensed native
Windows STP benchmark harness; it contains no simulator, scorer, process launcher,
retry policy or replacement controller. The terminal-coast patch lives in
`controllers/harry/mppi/`; tuning inputs use `WSORB_STP_TUNING` through the existing
harness `environment` field.

For Bui's separate Polyfit experiment, use the pinned profile and root-directory
commands in [BUI_POLYFIT.md](BUI_POLYFIT.md). The default preparation remains MPPI;
`--config` selects the Polyfit profile and machine paths are explicit inputs.

## Frozen design

The active campaign tunes **Harry's terminal-coast-patched Ali MPPI only**. All earlier preparations are preserved. Use a fresh preparation for this variant.

For this viability experiment, the final test uses **six held-out seeds**, the
first six in the existing frozen bank order: **100, 1729, 3253, 4999, 7919, 104729**.
Selection uses bank order, not historical scores. The full 22-seed bank remains
unchanged and all 22 remain excluded from optimisation training and validation.
The unchanged bank is
`docs/analytics/stp-runs/20261117T040830Z-HY/final-seed-bank.json`.
Optimisation uses a **separate 12-seed bank** generated reproducibly by the
existing harness seed algorithm with ID `phobos-smac3-optimisation-v1`, excluding
all final-test seeds. Preparation freezes both banks before any proposal or flight.
The first eight optimisation seeds are training/screening; the next four are
validation/confirmation. Every configuration receives the same block within a
stage. These seeds vary initial debris impulses within one saved field; radar stays at seed 100.
They are not independent field layouts or radar-noise scenarios.

For MPPI: baseline plus seven SMAC3 proposals on eight training seeds (64 flights);
baseline plus the two lowest-loss proposals on four additional validation seeds
(12 flights); select using all 12 tuning seeds; freeze the finalist; baseline and
finalist on **six final-test seeds** (12 flights). The updated budget is
**88 flights**, approximately 6 h 7 min–7 h 20 min at 250–300 s,
before overhead, evidence retries or optional extended flights. No flights start
with `prepare`. No full repository verification is needed for this isolated adapter.

The native entrypoint is `controllers/harry/mppi/controller.py`; its provenance and
terminal-coast change are documented in `controllers/harry/mppi/README.md`.
The original `controllers/ali/controller.py` is preserved. Its source defaults
are 8 m/s, 30 s, risk weight 400. The experiment explicitly enables
`avoidance="mppi"`, `plan_steering=true` for baseline and proposals; the unchanged
source default without campaign inputs remains `avoidance="avoider"`. Search
bounds are [7,14], [15,40], [100,1600], with logarithmic risk weight.
The separate Bui profile selects `controllers/harry/polyfit/Controller.py`, a
copy with finite cone-angle handling. Polyfit is not staged by the default MPPI
profile. Both profiles validate the original frozen bank before deriving stages.
All other settings and logic stay fixed. Controller bundles, adapter, lockfile,
mission, scorer, binaries and asset hashes are frozen. Every new stage must match
the initial baseline runtime signature before launch. The baseline snapshot, live
binaries, external dependencies, release and runner environment are rechecked;
resumption cannot silently adopt updated native conditions. A source commit is recorded
as origin; SHA-256 bundle pins identify the input changes even before a commit.

SMAC3 2.4.0, ConfigSpace 1.2.1 and scikit-learn 1.6.1 are locked in this isolated UV project. One
SMAC trial evaluates an entire matched training block. SMAC's `deterministic=True`
means one aggregate call per proposal; native scheduling remains stochastic.
The initial design is baseline only, followed by seven sequential proposals.
No adaptive seed racing or validation/test data enters SMAC. A persisted proposal
journal is replayed with the pinned seed 73 and dependencies on resume; a proposal
mismatch fails closed. Receipts are reused only within their original frozen
logical runs, never chosen by best retry score.

## Loss and selection

Loss is negative mean **exact native STP score**. Native scored failures count with
their real score. Unscored simulation caps and controller exceptions retain
`official_score=null` and receive loss 1000. The report repeats rankings with
loss 500, including whether the screening shortlist changes; if it does, the
alternative finalist cannot be established without additional confirmation data.
Infrastructure failures, owner stops and missing receipts are incomplete, halt the
adapter and never improve a candidate's mean. Rank requires the complete identical
seed block. The harness retains its three evidence retries after verified cleanup.

On all 12 tuning seeds, the finalist must have at least baseline's arrival count
and mean paired damage increase at most **2092 J** (1% of native failure threshold).
Missing damage evidence cannot pass. Choose the lowest-loss eligible candidate;
retain baseline unless an eligible candidate has strictly lower matched loss.
Baseline wins ties, including under the alternative penalty. Final-test safety/score results are reported,
never used to select another finalist. These are small-sample experiments, not a
repeatability or broad generalisation claim.

The main ceiling remains 300 simulation seconds. Collision early abort is disabled;
controller recovery remains unchanged. Receipt/manifest paths, exact scores,
paired score/damage/fuel/arrival-time/arrival-speed differences, final straight-line
distance, runtime, nulls, coverage and retries are retained in JSON. Arrival speed
is the last telemetry sample near scoring and is explicitly not an atomic scorer
measurement. Native damage increases do not establish an exact collision count.

## Prepare, launch and resume (PowerShell, repository root)

```powershell
$stpPython = "$env:USERPROFILE\AppData\Local\SimDynamX\STPro\Versions\v0.54.2\py\Scripts\python.exe"
$campaign = "runs/$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'))-phobos-harry-mppi-viability"
uv run --project experiments/stp_optimiser --locked --python $stpPython python experiments/stp_optimiser/optimise.py prepare --output $campaign
```

Keep the printed campaign path. Launch the **frozen adapter**, not mutable sources:

```powershell
uv run --project "$campaign/adapter" --locked --python $stpPython python "$campaign/adapter/optimise.py" run $campaign
```

After the old runner exits, resume that same path without replacing evidence:

```powershell
uv run --project "$campaign/adapter" --locked --python $stpPython python "$campaign/adapter/optimise.py" run $campaign --resume
```

To run only tuning, add `--stage tune`; to run the frozen final tests later, use
`--stage final-test`. The MPPI selection must exist before its final test starts.
Native Windows, the unlocked rendered SpaceCRAFT session and normal licensed STP
login are required. No credential or account changes are made.

For progress/clean stop, use the existing benchmark `status`/`stop` commands with
the active `native` directory shown by the runner. For example, the initial path:

```powershell
uv run --no-project --python $stpPython python experiments/stp_benchmark/benchmark.py status "$campaign/baseline/native"
uv run --no-project --python $stpPython python experiments/stp_benchmark/benchmark.py stop "$campaign/baseline/native"
```

Active later paths are `<controller>/screen-01/native` through `screen-07/native`,
`<controller>/confirm/native`, then `<controller>/final-test/native`. An interruption
inside native stage preparation preserves unflown partial staging under
`native-incomplete-<UUID>` and rebuilds from the same frozen inputs. Any physical
flight evidence blocks rebuilding; do not delete it to force recovery. Interrupted physical flights follow the
existing harness policy and can require a separately reviewed recovery campaign.
A normal resume never overwrites them or resets evidence retry budgets.

`experiment.json` records seeds, resolved defaults and source pins. `archive.json`
recursively includes attempted historical receipts, including caps and incomplete
evidence, and classifies comparable complete receipts by mission/scorer/build/assets/seed scope,
radar, scheduling, sampling and limits, with hash-pinned receipt/manifest pointers.
Historical baseline receipts are conservatively not reused: the exposed input
revision and explicit resolved MPPI policy must match too. Different conditions
stay labelled historical; no direct score delta is computed for them. Final-test
comparisons include the finalist, its own baseline and compatible previous
controllers per seed. Historical comparisons are restricted to the six selected final-test seeds.
Historical final-test scores never enter SMAC or selection. Null historical scores
remain null; score differences require two actual native scores. Historical
coverage reports attempted/scored counts and outcomes per source campaign and
controller, retaining exception-selected cohorts separately.

Outputs include per-controller `screen.json`, `selection.json` and
`final-test/comparison.json`, with raw harness evidence under each `native` folder.
Optional 420 s paired reruns are deliberately outside this adapter: inspect cap
traces first, use at most two matched seeds/controller, rerun baseline and finalist,
and report cap-to-arrival conversions and fuel/damage/runtime separately. No
collision abort rule is proposed or enabled.

## Focused verification, no flights

```powershell
uv run --project experiments/stp_optimiser --locked --python $stpPython python -m unittest discover -s experiments/stp_optimiser -p test_optimise.py
uv run --project experiments/stp_optimiser --locked --python $stpPython python -m unittest discover -s tests -p test_official_stp_benchmark.py
```

Tests execute the real controller input and constructor/state wiring without SDK
connections, exercise the active MPPI planner and Polyfit clearance/planning paths,
check native outcomes/loss/matched coverage/safety, run real SMAC ask/tell and
resumption against synthetic evidence only, and check interrupted proposal reuse.
Actual native flight performance remains unmeasured until the owner launches.
