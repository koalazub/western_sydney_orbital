import marimo

__generated_with = "0.25.2"
app = marimo.App(width="medium")


@app.cell
def _():
    import csv
    import json
    from datetime import UTC, datetime
    from pathlib import Path
    from runpy import run_path

    import marimo as mo

    from wsorb.sweep import load_trial, run_sweep
    from wsorb.sweep_analysis import write_report

    root = Path(__file__).resolve().parents[2]
    return UTC, csv, datetime, json, load_trial, mo, root, run_path, run_sweep, write_report


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    # Seeded headless experiments

    A small example of calling the experiment APIs from your own notebook.
    The same compiled practice physics runs without the browser or Flight Lab.
    Opening this notebook does **not** start a sweep. Set `saved_directory` to
    inspect existing results. Starting new physics requires the run control below.

    From the repository root, open:

    ```sh
    just flight-notebook
    ```

    Saved inspection and report rebuilding need no engine build. Run `just build`
    only before starting a new physics sweep.

    Compare the same seeds across algorithms at each debris count. Keep the
    region, rock sizes and distribution fixed while varying count; count is a
    density proxy, not a measured local density. Start small, then add seeds
    and counts near the conditions where results deteriorate. Changing count
    does not guarantee retaining the same rocks.
    """)
    return


@app.cell
def _(json, root, run_path):
    # Edit these ordinary Python values, or replace the policy factories below.
    policies = run_path(str(root / "examples/flight_sweep.py"))["POLICIES"]
    config = json.loads((root / "examples/flight_sweep.json").read_text())
    config["seeds"] = [11, 22]
    config["debris_counts"] = [0, 500]
    config["duration_s"] = 300.0
    config["step_s"] = 0.5
    # config["algorithms"]["basic_pd_radar"]["velocity_gain_s"] = 0.09
    # policies["my_algorithm"] = my_policy_factory
    # config["algorithms"]["my_algorithm"] = {"gain": 0.1}
    saved_directory = None  # Or root / "runs/scratch/an-existing-sweep" to inspect it.
    return config, policies, saved_directory


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    Each policy factory receives algorithm settings and returns a function
    `policy(observation) -> (accel, angular_accel)`. A fresh factory/closure is
    created per trial, so tracking memory does not leak between seeds. Policies
    receive public observations, including radar, rather than hidden rock states.

    These defaults produce eight trials: two algorithms × two counts × two seeds.
    Each trial advances up to 300 simulation seconds; wall time depends on load.
    """)
    return


@app.cell
def _(mo):
    allow_run = mo.ui.checkbox(label="Allow a new sweep to run physics", value=False)
    run_button = mo.ui.run_button(label="Run configured sweep")
    mo.output.replace(mo.vstack([allow_run, run_button]))
    return allow_run, run_button


@app.cell
def _(
    UTC,
    allow_run,
    config,
    csv,
    datetime,
    mo,
    policies,
    root,
    run_button,
    run_sweep,
    saved_directory,
):
    if run_button.value and allow_run.value:
        output_directory = (
            root / "runs/scratch" / datetime.now(UTC).strftime("phobos-%Y%m%dT%H%M%S%fZ")
        )
        summaries = run_sweep(config, policies, output_directory, root=root)
    elif saved_directory is not None:
        output_directory = saved_directory
        with (output_directory / "runs.csv").open(newline="") as _stream:
            summaries = list(csv.DictReader(_stream))
    else:
        mo.stop(
            True,
            mo.md(
                "Set `saved_directory` to inspect a sweep without physics. To start a new "
                "sweep, enable **Allow a new sweep to run physics**, then press "
                "**Run configured sweep**."
            ),
        )
    mo.vstack([mo.md(f"Saved results: `{output_directory}`"), mo.ui.table(summaries)])
    return output_directory, summaries


@app.cell
def _(mo, summaries):
    trial_selector = mo.ui.dropdown(
        options={
            (
                f"{row['algorithm']} · seed {row['seed']} · "
                f"count {row['debris_count']} · rep {row.get('repetition', 'unknown')} · "
                f"{row['outcome']} · {row['trial_directory']}"
            ): row["trial_directory"]
            for row in summaries
        },
        label="Inspect a saved trial",
    )
    mo.output.replace(trial_selector)
    return (trial_selector,)


@app.cell
def _(load_trial, mo, output_directory, trial_selector):
    mo.stop(trial_selector.value is None, mo.md("Select a trial to inspect its measurements."))
    selected_trial = load_trial(output_directory / trial_selector.value)
    sample_metrics = []
    _previous_time = None
    for _sample in selected_trial["samples"]:
        _observation = _sample["observation"]
        _mission = _observation.get("mission", {})
        sample_metrics.append(
            {
                "time_s": _observation.get("time_s"),
                "phase": _mission.get("phase"),
                "target_distance_m": _mission.get("target_distance_m"),
                "relative_speed_mps": _mission.get("relative_speed_mps"),
                "fuel_kg_estimate": _mission.get("fuel_used_kg"),
                "damage_j_estimate": _mission.get("damage"),
                "requested_accel_body_mps2": _sample.get("accel_command"),
                "command_applies_from_s": (
                    _previous_time if _sample.get("accel_command") is not None else None
                ),
                "command_applies_to_s": (
                    _observation.get("time_s")
                    if _sample.get("accel_command") is not None and _previous_time is not None
                    else None
                ),
            }
        )
        _previous_time = _observation.get("time_s")
    mo.vstack([mo.md(f"Outcome: **{selected_trial['outcome']}**"), mo.ui.table(sample_metrics)])
    return (selected_trial,)


@app.cell
def _(mo):
    report_button = mo.ui.run_button(label="Rebuild report for selected saved trial, no physics")
    mo.output.replace(report_button)
    return (report_button,)


@app.cell
def _(mo, output_directory, report_button, trial_selector, write_report):
    report_path = output_directory / "report.html"
    if report_button.value:
        mo.stop(trial_selector.value is None, mo.md("Select a saved trial before rebuilding."))
        report_path = write_report(output_directory, trial=trial_selector.value)
    mo.stop(
        not report_path.is_file(),
        mo.md(
            "No saved HTML report. Select a trial and use **Rebuild report for selected "
            "saved trial, no physics**. This only replaces report.html; "
            "saved traces stay unchanged."
        ),
    )
    mo.download(
        report_path.read_bytes(),
        filename="flight-sweep-report.html",
        label="Download comparison report",
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## Read the failure evidence

    The corrected report keeps each trial separate and compares only the same
    explicit repetition, seed, count, settings, schedule and build receipts.
    It overlays actual ownship route projections and time traces for range,
    relative speed, fuel, command norm, radar range/age and damage. The shared
    time cursor has keyboard controls and sample tables. Inspect these together.
    Stored commands accompany post-step state for the **preceding** interval.
    Local success currently means entering the arrival radius, without an arrival-speed
    gate. Fuel and damage are practice-model estimates; policy/engine errors are separate outcomes.
    Two seeds are a workflow check, not strong statistical evidence.

    First damage identifies a sampled time interval, not a proven collision cause.
    Saved public observations and commands support analysis, but are **not full
    world checkpoints**. Exact collision replay is not implemented or guaranteed.
    Selecting a trial alone does not regenerate the report. The separate rebuild
    control explicitly replaces only `report.html` for that saved trial, never
    starts a simulator, and leaves datasets unchanged. Outside this notebook:

    ```sh
    devenv shell uv run --locked --extra lab python examples/flight_sweep.py \
      --analyze-only --output runs/EXISTING_SWEEP --trial trial-00002
    ```

    Legacy local scores are diagnostics, not official STP points; the report
    does not re-score them. The downloadable report loads chart libraries from
    the internet, while its tables remain available without them.
    """)
    return


if __name__ == "__main__":
    app.run()
