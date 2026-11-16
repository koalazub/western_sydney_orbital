# Official STP controller campaigns (Windows)

This runner uses **native Windows SpaceCRAFT + Compute_Server**, normal scheduling,
and the official STU4 mission. The shuttle and debris use Unreal Chaos physics;
server-only headless operation is not a validated competition flight. No local
WASM controller adapters are used. The current checked-in Tom controller is the
reference; completed flights establish the new baseline, not the scores in minutes.

## Quick use guide

### Add a controller

1. Put the official STP entrypoint and all supporting modules in
   `examples/<name>/`. Keep the native connection boilerplate and thruster-command
   interface. Dependencies must work in STP's Windows Python **3.12** environment.
2. Add an entry to the `controllers` array in [`campaign.json`](campaign.json):

   ```json
   {
     "id": "new_controller",
     "directory": "examples/new_controller",
     "entrypoint": "Controller.py",
     "environment": {}
   }
   ```

3. Check the candidate in a separate short diagnostic campaign first. In a local
   configuration copy, set `reference` to its new ID, `diagnostic_rerun` to `true`
   and `limits.simulation_s` to `40`. Prepare with `--config <copy> --mode pilot`
   and a new output directory. This schedules one fresh candidate flight; inspect
   `delivery.json` for native command readback, applied impulses and simulator
   motion. A short diagnostic is not a scored baseline run.
4. Keep `reference` set to `tom` in comparison campaigns. Prepare a new campaign
   whenever controller code or settings change; prepared sources are frozen.

The runner selects and launches each controller automatically. No manual copying
into the installed simulator is needed, and only one controller commands each flight.

### Run a campaign

Use the native Windows UV commands below, from the repository root:

1. **Prepare:** choose the configuration, mode and a new directory under `runs/`.
   Preparation freezes the mission, controller sources, settings and complete
   schedule; it starts no simulations.
2. **Run:** explicitly start that prepared campaign. The runner launches fresh
   SpaceCRAFT sessions at **1×**, captures evidence and cleans up each process tree.
3. **Analyze:** generate outcomes, official scores, independent score reconciliation,
   comparisons against Tom and incident timelines. Read `report.md` in the output
   directory, with `attempts.csv` and each attempt's native evidence beside it.

| Mode | Schedule |
| --- | --- |
| `pilot` | One fresh flight of the configured reference controller; Tom by default. |
| `baseline` | Five blocks per controller: **5 flights each**. |
| `seeded` | The first 22 saved debris-perturbation seeds, including 100 and official default 8193, with radar fixed at 100: **22 flights each**, **88 total** for the four configured current controllers. Override with `--seed-count N`. |

The current configuration sets `repetitions: 1` and a **300 s simulated-time limit**.
Each controller flies each seeded scenario once. Set `repetitions` to another
positive integer in a configuration copy if repeated measurements are needed.
Older frozen configurations without this field retain their two-pass interpretation;
existing prepared schedules and evidence are unchanged.

The default `execution_order` is `coverage_first`: every controller runs once on
seed 100, then once on each remaining seed. If multiple repetitions are configured,
later passes repeat those same combinations. Controller order rotates between seed blocks. This spreads
early results across contributors; repetitions retain their original matching
indices. `execution_order: "paired"` groups configured repetitions consecutively if needed.
Failed and interrupted attempts stay
recorded; they are never silently retried or replaced. Use the runner's `stop`
command for a clean stop. Full campaigns require an explicit start, and time-scale
acceleration remains future work.

### Unattended baseline and seeded queue

Prepare both campaign directories first, then give both paths to `run`, in order:

```powershell
uv run --no-project --python $stpPython python experiments/stp_benchmark/benchmark.py run runs/full/seeded runs/full/baseline --continue-on-controller-error --status-file runs/full/queue_status.json
```

This runs each campaign sequentially and writes its analysis at completion.
`queue_status.json` and each campaign's `status.json` show progress. The optional
continuation flag preserves a controller exception as a failed attempt and moves
to the next scheduled ID only after cleanup has been verified. It never retries
or replaces that attempt. Startup, simulator, sensor/evidence-stream and cleanup
failures still halt the queue for investigation; later campaigns do not start.
Without the flag, controller exceptions also halt as before.

While running, the process requests that Windows keep the system and display awake;
the request ends with the process and does not change Windows power settings.
Keep the Windows session unlocked for native SpaceCRAFT rendering. Reports and raw
evidence stay under the prepared `runs/` directories.

Check the entire queue from a second PowerShell terminal:

```powershell
uv run --no-project --python $stpPython python experiments/stp_benchmark/benchmark.py status runs/full
```

For one campaign, pass `runs/full/baseline` or `runs/full/seeded` instead. Status is
read-only and reports completed/remaining flights, outcomes, the active controller
and seed, and its latest range, speed, fuel, damage and telemetry timestamp. The
queue directory is recognized by its `queue_status.json` file.

Stop the whole queue cleanly, then resume the same prepared schedule:

```powershell
uv run --no-project --python $stpPython python experiments/stp_benchmark/benchmark.py stop runs/full
uv run --no-project --python $stpPython python experiments/stp_benchmark/benchmark.py run runs/full/seeded runs/full/baseline --resume --continue-on-controller-error --status-file runs/full/queue_status.json
```

Wait for the old runner to exit before resuming. Resume obtains the exclusive
flight lock before clearing stop markers. It keeps the frozen sources, seeds,
repetitions and IDs, preserves completed/failed/aborted attempts, and runs only
unstarted IDs. A process interruption is retained as an interrupted attempt;
available native logs and any already captured official receipt are recovered.
Previously unverified cleanup is labelled as such, not claimed as successful.
To rerun a failed or aborted flight deliberately, prepare a separate campaign.

## Runtime and mission

STP 0.54.2 Windows is installed at `K:\Work\SPT-Sim`, release build
`4b98c53dd4339223fab3df542d0a5b5d13393902`. Its SDK is Python **3.12**, separate
from this repository's Python 3.13 practice environment. Use UV with the native
STP interpreter and `--no-project`; do not install this repository into the SDK.
The owner approved this native Windows workflow instead of Nix/WSL for flights.
No build, deployment or account configuration changes are involved.

The installed competition checkout was old (`c1e3fc8f`, nominal fuel 1.15 kg).
`campaign.json` pins the official 11 November package
`0d753a28e1a4265b2839bf8e9fbbc9194efafb87`, nominal fuel **75 kg**. The package
snapshot downloaded for this work is under ignored `runs/stp-packages/`.
On another machine, obtain that exact revision of
[STU4-Sim](https://github.com/SimDynamX/STU4-Sim/tree/0d753a28e1a4265b2839bf8e9fbbc9194efafb87),
extract it under `runs/`, and set `package`, `install`, and `user_root` in a copy
of the configuration. The mission and scorer hashes must match. Never update
the distributed competition configuration to make a campaign work.

Sign in to SpaceCRAFT normally before the pilot if required. Close other
controller sessions. The runner never provisions credentials or changes login
settings. Each attempt launches the documented `SpaceCRAFT.exe --sim=...`
workflow with `-firstRole -quitOnStop`; configured Python systems receive their
original native connection arguments. The attempt is enclosed in a Windows job
before its first instruction. Cleanup kills only that job and its descendants.
A repository-wide file lock prevents two copies of this runner commanding flights.

Controller placement does not require editing the installed solution. STP accepts
the mission's explicit Python `Source` paths; the runner selects one fresh copy of
the requested controller and retains the native launch arguments. If installing a
controller manually for an ordinary interactive flight, the stock mission expects
`%USERPROFILE%\AppData\Local\SimDynamX\STPro\Assets\Repos\STU4-Solution\Controller.py`.
Back up that existing solution first, and copy **all** supporting modules from the
person's folder beside `Controller.py`. Manual copying is unnecessary for campaigns.

```powershell
$stpPython = "$env:USERPROFILE\AppData\Local\SimDynamX\STPro\Versions\v0.54.2\py\Scripts\python.exe"
uv run --no-project --python $stpPython python experiments/stp_benchmark/benchmark.py prepare --mode pilot --output runs/stp-pilot-001
uv run --no-project --python $stpPython python experiments/stp_benchmark/benchmark.py run runs/stp-pilot-001
uv run --no-project --python $stpPython python experiments/stp_benchmark/benchmark.py analyze runs/stp-pilot-001
```

`pilot` schedules one fresh Tom attempt with the current configuration. `prepare` launches nothing and freezes
sources, dependencies and the full schedule. All roots must be new. Existing
attempt folders are never overwritten, silently retried or replaced. An interrupted
attempt remains partial; analyze it without rerunning it. `run --max-attempts 1`
allows a bounded diagnostic start. Startup errors, evidence failures and stalls halt
the campaign for investigation. A subsequent explicit `run` runs only previously
unstarted scheduled IDs, never a failed ID.

To stop cleanly from a second terminal, use `benchmark.py stop runs/stp-pilot-001`
with the same UV/native-Python prefix. This requests job cleanup and final evidence
capture. Use it instead of force-stopping the enclosing terminal process. A stopped
campaign can continue with `run --resume`, preserving started attempts and running
only unstarted IDs. An initial delivery check
can use a config copy with `diagnostic_rerun=true` and a short simulation limit.

After inspecting the pilot, prepare `--mode baseline` for five fresh flights
per controller, or `--mode seeded` for the saved seed prefix and one run per
controller/seed. Controller order rotates between blocks. Commands
to run full campaigns are intentionally manual. The current configuration lists
Tom Kalman, Tom Polyfit, Ali MPPI and Bui Alpha-beta. Existing frozen campaigns
retain their own source versions and method labels; the earlier `restart-002`
Tom entrypoint in the `kalman` folder actually used Polyfit. Harry has no official
STP entrypoint. WSO requires its
own explicitly established clock/configuration and is not silently enabled.
Additional official entrypoints can declare their directory, entrypoint, extra
support files and existing environment settings in the configuration. Preparation
freezes exact source hashes plus the checkout revision; historical submission
identity is not inferred. Controllers are never tuned by this framework.

## Experimental seeds

### Shared seed bank

[`seed_bank.json`](seed_bank.json) is the versioned, ordered bank shared by all
new seeded campaigns. **Perturbation seed 100 is first**, followed by all 20 previously
declared seeds, then the official default **8193** at position 22. The existing
seed order is preserved. `seed_count` defaults to 22 so new campaigns include 8193;
an explicit smaller count selects only that shorter prefix. The already prepared
November 15 queue retains its frozen 20-seed sweep and covers debris seed 8193
in its separate unchanged competition-baseline phase. Seeded experiments apply the selected seed only
to debris perturbations. **The original radar model always keeps seed 100** and
is never copied into a reseeded variant. The unchanged November competition
baseline retains **radar seed 100 and spawner seed 8193**. The native
seed adapter pins both original source hashes before changing any copied script.

Every preparation starts at the beginning of the bank; flights do not consume
seeds or move a global cursor. All controllers receive the same selected prefix,
with one run per seed in the current single-pass configuration. Failed attempts keep their original seed.
Adding a controller or creating a new campaign reuses the same scenarios.

To request more scenarios, set `seed_count` in the configuration or pass
`--seed-count N` when preparing a seeded campaign. For example, from the repository
root with `$stpPython` set as above:

```powershell
uv run --no-project --python $stpPython python experiments/stp_benchmark/benchmark.py prepare --mode seeded --seed-count 30 --output runs/stp-seeded-030-001
```

This prepares **30 flights per controller** with the current configuration and launches nothing. If the bank has
22 saved seeds, preparation appends eight unique seeds before freezing the schedule.
Later campaigns reuse those same 30 values. Requests for fewer seeds select a
shorter prefix and never shorten or rewrite the bank. Extension uses stable SHA-256
generation by bank ID and index, so incremental extensions and different machines
produce the same sequence. Concurrent bank updates are locked and writes are atomic.

Each campaign saves the full bank snapshot in `snapshot/seed_bank.json` and its
exact selection, bank hash and selected-seed hash in `seed_selection.json` and
provenance. Attempts record their seed index; report rows include bank identity
and selection hash. Existing prepared campaigns remain unchanged when the bank grows.
Keep and share bank extensions with the controller configuration through version
control. A missing or altered original prefix, invalid/duplicate seed, changed
extension or different mission/scorer/scope is rejected rather than silently
creating a replacement history. Older configuration copies with inline `seeds`
should adopt the bank fields from `campaign.json` for new preparations.

Compare the same seed and repetition under the same pinned mission/scorer and
scheduling settings. Equal seeds do not guarantee identical asynchronous STP
execution; a single pass does not measure within-seed repeatability. A newly added seed
needs runs from each controller, including Tom, before a matched comparison exists.

### What the seeds control

The official mission loads a saved debris field (`DoSpawning=false`). Its spawner
applies seeded initial random impulses; the original radar noise model stays at
seed 100. Seeded campaigns patch **only a copy of the spawner script**, replacing
its reviewed seed anchor with the declared perturbation seed. They retain the
saved field and all competition physics/scoring/actuator and radar settings.
Radar seed 100 selects the Gaussian measurement-error sequence: position error
has a 2 m standard deviation per axis, closest-surface distance error 2 m,
range-rate error 0.02 m/s, and apparent-diameter error 5% of the true radar diameter.
Errors are drawn during initialization and subsequent round-robin debris updates.
They affect reported observations and potentially the controller's decisions,
not physical debris positions or initial impulses directly. The seed fixes the
random sequence; asynchronous update and controller timing can still change
which noisy readings a controller receives.
These are independent **debris-perturbation scenarios**, not newly generated
debris layouts or radar-noise scenarios. Hashes, seeds and changes are
recorded per attempt. `DoSpawning=true` is refused because the original controllers
wait indefinitely in that mode. Experimental results stay separate from baseline.

## Evidence and analysis

### Add a controller to the existing experiment

Use `--controllers` to select only the new controller and `--match-campaign` to
inherit the original seeded campaign's complete conditions and exact seed prefix:

```powershell
uv run --no-project --python $stpPython python experiments/stp_benchmark/benchmark.py prepare --mode seeded --controllers bui --match-campaign runs/stp-20261116T192404-HY-restart-002 --output runs/stp-20261116T192404-HY-bui-001
uv run --no-project --python $stpPython python experiments/stp_benchmark/benchmark.py run runs/stp-20261116T192404-HY-bui-001 --continue-on-controller-error
```

This schedules only **22 Bui flights**, with `coverage_first`, one pass and the
original 300-second limit. It leaves every original attempt, ID, outcome and
frozen controller version unchanged. Already-started failed flights are not
retried; unstarted original flights stay in the original campaign. Controller
IDs already present in the base are rejected. The new main checkout does not
replace old frozen controller sources. Conditions are checked against native
binary, asset, interpreter and machine provenance before preparation completes.

Combine their saved results by seed without rerunning or rewriting either source:

```powershell
uv run --no-project --python $stpPython python experiments/stp_benchmark/benchmark.py analyze runs/stp-20261116T192404-HY-restart-002 runs/stp-20261116T192404-HY-bui-001 --output runs/stp-20261116T192404-HY-combined-001
```

The combined output directory must be new. Source campaign/flight IDs and version
labels remain in `attempts.csv`; `analysis_sources.json` records their provenance
and capture hashes. The original campaign's reference remains the comparison
baseline. Seed sets, limits and native conditions must match; overlapping
controller IDs are rejected rather than pooled across versions. Missing flights
and scores stay missing, and the four-controller view includes unstarted original
attempts. Cross-session instrument versions are recorded explicitly. An addition
is part of the same experiment and is not a new randomized interleaving of all
controllers within a session. Reports generated before completion are partial.

### Dataset labels and source versions

Declare `author` and `method` on each controller configuration, for example
`Tom Kirkpatrick` / `Kalman`, `Tom Kirkpatrick` / `Polyfit`, or `Ali El Ali` / `MPPI`.
Update the method when selecting a new implementation; the controller ID alone
does not establish its method. Keep the reviewed implementation and metadata in
the same new preparation.

Preparation and analysis write `controller_versions.json`. Analysis carries its
labels into attempt CSVs, summaries, matched comparisons and the Markdown report.
A label includes author, method, a verified 12-character source commit and a
separate 12-character SHA-256 identifier for the complete frozen controller bundle.
Full values are retained in metadata/CSV fields. These SHA-256 identifiers are
file-content hashes, not commit hashes.

The source commit is the controller's explicit `source_commit`, or the preparation
checkout revision when no source pin is supplied, only when every frozen controller
file matches that commit, allowing CRLF/LF differences explicitly. A moved source
folder can declare its original `source_directory` at that commit. Preserve
`source_changes` for local adjustments; the origin is retained separately when
the adjusted files cannot match it exactly.
Git is used read-only to inspect committed blobs; no checkout or repository writes
occur. The bundle includes supporting files, so a helper-only change changes its
identity. Modified sources or unavailable local commit history produce an
`unverified commit` label, with the checkout commit recorded separately. No remote
history is fetched and no commit is invented. Author/method remain declared labels,
not automatic classification of arbitrary incoming code.

For an existing frozen dataset, `dataset_labels.json` can supply author/method
without changing its campaign or provenance. It maps the original controller IDs
to `author`, `method` and the exact `source_bundle_sha256` from
`controller_versions.json`. A different bundle is rejected. Re-analyze after the
running campaign finishes to refresh derived reports; do not change raw receipts,
frozen sources or prepared configuration merely to add display labels. Archived
datasets whose author/method are unavailable remain explicitly unspecified.

All raw evidence stays under ignored `runs/`. `campaign.json` contains the complete
predeclared schedule. `provenance.json` contains the STP build, binary/SDK hashes,
dependency versions, transitive config/data hashes, source snapshots, machine and
scheduling mode. Each run ID contains:

- `manifest.json`: effective mission/controller hashes, settings, seeds, limits,
  launch arguments and native UUID. `mission.simconf` records transport/path
  substitutions; physics parameters are preserved.
- `events.jsonl`: UTC and monotonic receive/acquisition timestamps, native process
  sequence numbers, commands requested and writes completed, read-only state/radar
  samples, original exceptions, camera requests/returns and scorer progress.
- `receipt.json`: official outcome, exact native score, one-decimal displayed score,
  original scorer arguments captured at the scoring call, scorer time/range,
  termination reason and cleanup. Unscored attempts keep `official_score=null`.
- `native/`, `launcher.log` and `controller/`: native logs/output copied immediately
  after each attempt, plus controller-created files and original source. These
  copies outlive STP's default newest-20-run retention.
- `incidents.json`: observed damage intervals, official failure, threshold indicators
  for overshoot/progress/command reversals/stale sensors/decision overruns, original
  exceptions and stalls. Sequence windows link preceding and subsequent evidence
  and the terminal receipt. Suspected causes stay null; missing data is explicit.
- `delivery.json`: completed writer calls matched to the **native actuator's**
  unmodified command reads, nonzero applied impulses and observed simulator motion.
  Writer calls alone never establish command delivery. Motion includes gravity and
  contact effects; exact acceleration attribution is not inferred from a flight.

Scoring is observed with Python 3.12 code-local monitoring around **unchanged** original
sources. It captures `YouWon`/`YouLost` arguments and the original score return,
then emits the receipt after the native alert call returns. The runner stops after
the first receipt; it never re-reads later counters as scoring inputs. It independently
reconciles the pinned scorer formula (time or distance term, damage, total fuel,
and 50-point offset). Scorer time deliberately differs from mission-start time.

The 11 November scorer penalizes `fuelUsed_kg / nominalFuel_kg` from the first
kilogram. It does **not** subtract a one-tank allowance, unlike the old installed
scorer. Both the official return and independently calculated score are retained.

Keep baseline campaigns at **1× time scale**. The original Python loops are paced
in wall time, so accelerated simulation changes update density per simulation
second. Faster seeded campaigns need separate matched 1× validation and must be
labelled experimental; changing the clock alone does not preserve controller or
physics behavior. Normal scheduling remains required; `-det` is not a Python
lockstep guarantee.

Code-local monitoring and sequential SDK sampling add overhead to all observed baseline
runs. Decision duration means loop-sleep return to `SetCommand`, including native
queries/camera/UI, not isolated optimizer CPU time. Original controller loop sleeps
are preserved; the runner adds no fixed readiness delays. Readiness and completion
come from authenticated loopback records, process exit and bounded deadlines.
Extra instrumented reruns must use a separate campaign with `diagnostic_rerun=true`.
Baseline and experimental/diagnostic results must not be pooled.

The capture adapter supports current `governor.burning`, historical
`governor.firing` and MPPI's `record["mode"]`. Missing optional diagnostics stay
null; state-sampling errors halt as evidence failures. Wall/stall watchdogs are
enforced even when records are continuously queued. JSON replacement briefly
polls readiness for transient Windows sharing locks, then propagates persistent
failures. Native logs already copied successfully are excluded from the
startup-log fallback to avoid duplicate storage. Last-sample time and relative
speed are exported alongside remaining distance in `attempts.csv`.

State reads are not atomic. Radar timestamps retain their source clock; sensor age
is calculated only when the radar process supplies its actual origin. Native contact
events/identities and maneuver-specific fuel attribution are unavailable until an
authoritative event interface is established. Damage increases indicate intervals,
not exact collision times or object identities. Camera C-call return events alone
do not establish successful pixels; controller image artifacts/native logs do.

Analysis produces `attempts.csv`, `summary.json`, `matched.csv`,
`scenario_comparisons.csv`, `failure_timelines.json`, `report.md` and
`cost_estimate.json`. All scheduled outcomes and scored coverage remain visible.
Distributions retain values, extrema, means and medians. Matched comparisons use
the same block/repetition; a seeded scenario comparison requires scores from both
controllers for every configured repetition. In a single pass this is the direct
score difference; multiple repetitions are averaged. Repetitions are repeatability measurements, not extra independent
scenarios. Collision incidence remains unavailable; damage-increase incidence is
reported separately. Pilot duration/storage extrapolations include a warning when
attempts did not complete; do not launch full campaigns automatically.

Focused checks (no full repository verification):

```powershell
uv run --no-project --python $stpPython python -m unittest discover -s tests -p test_official_stp_benchmark.py
```

Official workflow references:
[starting STP](https://github.com/SimDynamX/SpaceTeamsPRO/wiki/How-To-Start-a-Sim),
[Python systems](https://github.com/SimDynamX/SpaceTeamsPRO/wiki/Programming-STP-Sims-with-Python),
[competition API](https://github.com/SimDynamX/STU4-Solution/wiki/3.-competitor-python-api).
