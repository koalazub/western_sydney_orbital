# Bui: fixed Polyfit SMAC3 run

Use a normal checkout of the supplied branch. Run every command in PowerShell
from the project root, containing `experiments/`, `controllers/` and `docs/`.
Use your own installed STU4-Sim mission/config/data files. Supply their package
root explicitly with `--package`; this repository contains hash pins, not a copy
of STU4-Sim. Your licensed installation supplies native executables and SDK assets.
Preparation reports missing or mismatched mission/scorer/radar/actuator/entity/data
files and stops before creating a campaign. The files must match the reviewed
version, not merely the newest available release. Do not bypass or rewrite pins
to accept different conditions. See [AGENTS-POLYFIT-OPTIMISER.md](AGENTS-POLYFIT-OPTIMISER.md) for LLM guidance.

## Selected controller and evidence

Use `controllers/harry/polyfit/Controller.py`, copied from the newer
`controllers/tom/polyfit_v2_bodge/Controller.py`, which is preserved. The copy adds
finite cone-angle handling (clipped cosine and explicit degenerate-vector rejection)
to the existing logging-crash fix. See `controllers/harry/polyfit/readme.md`. It uses a 30-degree
forward cone, a 50 m core bubble and up to 500 radar rows. It is distinct from
`polyfit_bodge_v2`, the earlier cone/timing-corrected experiment with 21/22
arrivals. The repaired upstream variant's native follow-up had 9 arrivals and
4 caps on 13 exception-selected seeds. That cohort cannot establish that it is
the best full-bank controller. This new matched experiment measures viability.

`bui_polyfit.json` pins the exact controller SHA-256. The copied controller also retains
the existing native `WSORB_STP_TUNING` settings. The source-default baseline is
cruise speed 8 m/s, avoidance margin 6 m, avoidance horizon 50 s. SMAC3 searches
[6,12] m/s, [3,12] m and [15,60] s. All other logic/settings stay frozen.
A changed source fails the profile check instead of silently selecting another
implementation. Do not update the profile pin to bypass a mismatch.

The design matches Harry's MPPI viability run: eight matched training seeds for
baseline plus seven proposals (64 flights), four additional validation seeds for
baseline plus the top two (12 flights), then six held-out seeds for baseline and
finalist (12 flights). Total **88 logical flights**, approximately **6 h 7 min to
7 h 20 min** at 250-300 s per flight before overhead or evidence retries.

Training: 438237952, 2486588451, 3927309276, 3969613433, 428062188,
4266923988, 2541959213, 649422304.
Validation: 1705635923, 2272052480, 1475239320, 3546960591.
Held-out test: 100, 1729, 3253, 4999, 7919, 104729.
Seeds vary debris impulses in one saved field, with radar seed fixed at 100.
The original 22-seed bank remains intact and excluded from tuning.

## Install prerequisites and prepare on Bui's machine

Install UV and licensed native Windows STP **0.54.2** normally. Use its Python
**3.12** interpreter; UV creates an isolated optimiser environment and does not
install dependencies into STP's SDK environment. Keep the Windows desktop
unlocked and signed into STP for rendered native flights. Use one flight runner
per machine. No cloud runners or billing changes are required.

Set `$stpInstall` to the directory containing `SpaceCRAFT/`, `Compute_Server/`
and `ReleaseVersion_0.54.2_Win64_User.txt`. The package pins are STU4 revision
`0d753a28e1a4265b2839bf8e9fbbc9194efafb87`, FINAL_SUBMISSION mission SHA-256
`04abcfb54321b22b68322ecf98d785cc548a7bd7b7ba447aac511d9a4d38e06e`, scorer SHA-256
`ba8cc418522040692df09de7bbdaa2522b17e6e6cfa94d3364febc40ddf93cc4`.
Preparation validates native assets and freezes fresh machine-specific provenance.
Do not copy a campaign already prepared on Harry's machine.

```powershell
$stpInstall = "C:\Path\To\SPT-Sim" # Replace with your installation directory
$stu4Package = "$env:USERPROFILE\AppData\Local\SimDynamX\STPro\Assets\Repos\STU4-Sim" # Adjust if needed
$stpPython = "$env:USERPROFILE\AppData\Local\SimDynamX\STPro\Versions\v0.54.2\py\Scripts\python.exe"
$campaign = "runs/$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'))-bui-polyfit-smac3"
uv run --project experiments/stp_optimiser --locked --python $stpPython python experiments/stp_optimiser/optimise.py prepare --config experiments/stp_optimiser/bui_polyfit.json --install $stpInstall --package $stu4Package --output $campaign
```

Both `--install` and `--package` are required for this profile. The package folder
must contain `Sims/FINAL_SUBMISSION.simconf`, `Systems/Scoring.py`, `Systems/Radar.py`
and the other reviewed referenced inputs. The profile retains SHA-256 pins for
all package-local dependencies of the reviewed mission, including actuator,
spawner, debris entities and thruster data. Inputs are read only; they are never
automatically updated or replaced.

If `FINAL_SUBMISSION.simconf` is absent, obtain the reviewed final-submission
configuration from Harry using the team's existing simulator-config update
process. That mission includes the previously reviewed camera-reference adjustment;
checking out the upstream revision alone does not guarantee it is present. Do not
substitute another mission or change its hash. A mismatch report names the files
that need updating. If STPro user data is elsewhere, add `--user-root "D:/your/STPro"`
and set `$stpPython` to the corresponding native 3.12 interpreter. Quote paths
containing spaces. Preparation starts no flights.

## Launch and resume

Use the campaign path printed by preparation, retaining `$campaign` in this shell:

```powershell
uv run --project "$campaign/adapter" --locked --python $stpPython python "$campaign/adapter/optimise.py" run $campaign
```

After the previous runner has exited, resume the same preparation:

```powershell
uv run --project "$campaign/adapter" --locked --python $stpPython python "$campaign/adapter/optimise.py" run $campaign --resume
```

For progress or a clean stop, give the active native stage to the existing harness:

```powershell
$active = "$campaign/baseline/native" # Later: polyfit/screen-01/native, etc.
uv run --no-project --python $stpPython python experiments/stp_benchmark/benchmark.py status $active
uv run --no-project --python $stpPython python experiments/stp_benchmark/benchmark.py stop $active
```

Later paths are `polyfit/screen-01/native` through `screen-07/native`,
`polyfit/confirm/native`, then `polyfit/final-test/native`. No receipts or attempt
folders may be deleted to reset a retry budget. Infrastructure failures remain
incomplete; preserve the error and ask for investigation. A genuine mid-flight
interruption may need a separately reviewed recovery rather than an automatic
replacement. Interrupted unflown stage preparation is recoverable: a partial
`native/` directory is preserved as `native-incomplete-<UUID>/` and rebuilt using
the same frozen proposal and seeds. Any physical receipt/manifest/telemetry blocks
that automatic recovery. Complete stage IDs and evidence retries are retained.
A failure during the initial `prepare` command still requires a new output path;
it has not launched a flight. No collision early abort is enabled; the ceiling is 300 s.

## Return results to Harry

At completion, the key outputs are `polyfit/screen.json`, `polyfit/selection.json`
and `polyfit/final-test/comparison.json`. Return the evidence too, so scores and
runtime compatibility can be checked. This PowerShell command bundles all campaign
JSON, frozen controller sources and native stage evidence, excluding the large
recreatable `adapter/.venv` optimiser environment:

```powershell
Compress-Archive -Path "$campaign/*.json", "$campaign/sources", "$campaign/baseline", "$campaign/polyfit" -DestinationPath "$campaign-results.zip"
```

Send that ZIP to Harry using your normal shared channel. No automatic upload or
message is sent. If a run halts, send the partial archive plus the original error;
missing scores stay null. Results from different machines are labelled as such,
not silently pooled with Harry's flights. Matched seeds alone do not remove
asynchronous timing or machine effects.
