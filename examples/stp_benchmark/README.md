# Official STP controller campaigns (Windows)

This runner uses **native Windows SpaceCRAFT + Compute_Server**, normal scheduling,
and the official STU4 mission. The shuttle and debris use Unreal Chaos physics;
server-only headless operation is not a validated competition flight. No local
WASM controller adapters are used. The current checked-in Tom controller is the
reference; completed flights establish the new baseline, not the scores in minutes.

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
uv run --no-project --python $stpPython python examples/stp_benchmark/benchmark.py prepare --mode pilot --output runs/stp-pilot-001
uv run --no-project --python $stpPython python examples/stp_benchmark/benchmark.py run runs/stp-pilot-001
uv run --no-project --python $stpPython python examples/stp_benchmark/benchmark.py analyze runs/stp-pilot-001
```

`pilot` schedules two fresh Tom attempts. `prepare` launches nothing and freezes
sources, dependencies and the full schedule. All roots must be new. Existing
attempt folders are never overwritten, silently retried or replaced. An interrupted
attempt remains partial; analyze it without rerunning it. `run --max-attempts 1`
allows a bounded diagnostic start. Startup errors, evidence failures and stalls halt
the campaign for investigation. A subsequent explicit `run` runs only previously
unstarted scheduled IDs, never a failed ID.

To stop cleanly from a second terminal, use `benchmark.py stop runs/stp-pilot-001`
with the same UV/native-Python prefix. This requests job cleanup and final evidence
capture. Use it instead of force-stopping the enclosing terminal process. A stopped
campaign cannot restart; prepare a new root and run IDs. An initial delivery check
can use a config copy with `diagnostic_rerun=true` and a short simulation limit.

After inspecting the pilot, prepare `--mode baseline` for five fresh two-run pairs
per controller, or `--mode seeded` for the 20 predeclared seeds and two consecutive
runs per controller/seed. Controller-pair order rotates between blocks. Commands
to run full campaigns are intentionally manual. The default four profiles are Tom,
Bui, Ali MVP and Ali MPPI. Harry has no official STP entrypoint. WSO requires its
own explicitly established clock/configuration and is not silently enabled.
Additional official entrypoints can declare their directory, entrypoint, extra
support files and existing environment settings in the configuration. Preparation
freezes exact source hashes plus the checkout revision; historical submission
identity is not inferred. Controllers are never tuned by this framework.

## Experimental seeds

The official mission loads a saved debris field (`DoSpawning=false`). Its spawner
applies seeded initial random impulses; the radar also seeds its noise. Seeded
campaigns patch **only copies** of those two scripts, replacing the reviewed seed
anchors with the declared seed. They retain the saved field and all competition
physics/scoring/actuator settings. These are independent **perturbation/noise
scenarios**, not newly generated debris layouts. Hashes, seeds and changes are
recorded per attempt. `DoSpawning=true` is refused because the original controllers
wait indefinitely in that mode. Experimental results stay separate from baseline.

## Evidence and analysis

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
the same block/repetition; complete two-run means form each seeded scenario
comparison. Repetitions are repeatability measurements, not extra independent
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
