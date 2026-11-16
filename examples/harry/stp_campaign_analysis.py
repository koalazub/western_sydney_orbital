# /// script
# requires-python = ">=3.12"
# dependencies = ["marimo>=0.25.2", "altair>=6.3.0"]
# ///
"""Explore the largest native STP dataset without launching simulations."""

import marimo

__generated_with = "0.25.2"
app = marimo.App(width="full")


@app.cell
def _():
    import sys
    from pathlib import Path

    import altair as alt
    import marimo as mo

    notebook_directory = Path(__file__).resolve().parent
    if str(notebook_directory) not in sys.path:
        sys.path.insert(0, str(notebook_directory))
    from stp_analysis import (
        load_campaign,
        mean,
        overview,
        paired_scores,
        paired_summary,
        repeatability,
        seed_sensitivity,
        write_csv,
        write_json,
    )

    return (
        Path,
        alt,
        load_campaign,
        mean,
        mo,
        notebook_directory,
        overview,
        paired_scores,
        paired_summary,
        repeatability,
        seed_sensitivity,
        write_csv,
        write_json,
    )


@app.cell
def _(mo):
    mo.md("""
    # Native STP controller analysis

    **Largest dataset only:** the November 15–16 seeded campaign. Pilots, speed
    experiments, the superseded campaign and the unflown baseline are excluded.
    Unstarted, owner-aborted, startup-failed and other incomplete attempts are
    excluded. Completed 600 s timeouts remain valid outcomes, with no invented score.

    The default comparison uses the **common first-pass seeds** on which every
    controller completed a flight. Complete repeat pairs are analyzed separately.
    All reported scores come from the native competition scorer.
    """)
    return


@app.cell
def _(mo, notebook_directory):
    campaign_path = mo.ui.text(
        value=str(notebook_directory.parents[1] / "runs/stp-full-20261115-001/seeded-radar100"),
        label="Seeded campaign directory",
        full_width=True,
    )
    cohort_choice = mo.ui.dropdown(
        options={
            "Common first-pass seeds (primary comparison)": "common",
            "All completed first-pass flights": "first",
            "All completed flights (includes repeat measurements)": "all",
        },
        value="Common first-pass seeds (primary comparison)",
        label="Analysis cohort",
    )
    mo.vstack([campaign_path, cohort_choice])
    return campaign_path, cohort_choice


@app.cell
def _(Path, campaign_path, load_campaign, mo):
    mo.stop(
        not (Path(campaign_path.value) / "campaign.json").is_file(),
        mo.md("Campaign data is missing. Point the field above at the saved seeded campaign."),
    )
    dataset = load_campaign(campaign_path.value)
    return (dataset,)


@app.cell
def _(cohort_choice, dataset, overview):
    completed_rows = dataset["rows"]
    comparison_rows = (
        dataset["common"]
        if cohort_choice.value == "common"
        else [r for r in completed_rows if r["repetition"] == 0]
        if cohort_choice.value == "first"
        else completed_rows
    )
    controller_summary = overview(comparison_rows, dataset["controllers"])
    return comparison_rows, completed_rows, controller_summary


@app.cell
def _(comparison_rows, completed_rows, dataset, mo):
    _score_count = sum(r["official_score"] is not None for r in completed_rows)
    _mismatches = sum(
        abs(r["score_error"]) > 1e-8 for r in completed_rows if r["score_error"] is not None
    )
    _config = dataset["campaign"]["config"]
    mo.md(f"""
    ## Coverage and provenance

    **{len(completed_rows)} completed flights** retained; **{_score_count} native scores**.
    Current view: **{len(comparison_rows)} flights**.
    Common first pass: **{len(dataset["common_seeds"])} seeds ×
    {len(dataset["controllers"])} controllers**.
    Excluded receipts: `{dataset["excluded"]}`.

    Package `{_config["package_revision"]}`; fixed radar seed **100**;
    selected debris seeds `{_config["seeds"]}`. Seed 8193 and the later 22-seed default
    are outside this frozen campaign. No pilot or native-default results are pooled here.

    **Score reconciliation: {_mismatches} mismatches** at tolerance 1e-8.
    The 600 s runner limit can censor a flight before the simulator issues a score.
    Missing scores are never replaced with zero. Raw receipts remain authoritative;
    this notebook does not depend on potentially stale runner aggregate reports.
    """)
    return


@app.cell
def _(alt, controller_summary, mo):
    _data = alt.Data(values=controller_summary)
    _base = alt.Chart(_data).encode(y=alt.Y("controller:N", title=None))
    _bars = _base.mark_bar().encode(
        x=alt.X(
            "arrival_rate:Q", title="Successful arrival fraction", scale=alt.Scale(domain=[0, 1])
        ),
        color=alt.Color("controller:N", legend=None),
        tooltip=["controller:N", "completed:Q", "successes:Q", "timeouts:Q", "arrival_rate:Q"],
    )
    _intervals = _base.mark_rule(color="black", strokeWidth=2).encode(
        x="arrival_ci_low:Q",
        x2="arrival_ci_high:Q",
    )
    mo.vstack(
        [
            mo.md("""## Arrival coverage
        Timeouts count as unsuccessful arrivals within the 600 s observation window.
        Black lines are exploratory 95% Wilson intervals. For the common first pass,
        each controller has one observation per seed. These fixed scenarios are not
        a representative random sample of all possible debris fields.
        Intervals on the all-flight view
        treat correlated repetitions as separate trials and should not support inference.
        """),
            mo.ui.altair_chart((_bars + _intervals).properties(height=170)),
            mo.ui.table(controller_summary, selection=None, page_size=10),
        ]
    )
    return


@app.cell
def _(controller_summary, dataset, mean, mo):
    _ranked = [r for r in controller_summary if r["completed"]]
    mo.stop(not _ranked, mo.md("No completed flights available in this cohort."))
    _arrival_leader = max(_ranked, key=lambda r: r["arrival_rate"])
    _arrival_names = " and ".join(
        r["controller"] for r in _ranked if r["arrival_rate"] == _arrival_leader["arrival_rate"]
    )
    _scored = [r for r in _ranked if r["score_median"] is not None]
    _median_leader = max(_scored, key=lambda r: r["score_median"]) if _scored else None
    _common = [
        dict(
            controller=controller,
            time=mean(
                r["arrival_time_s"] for r in dataset["common"] if r["controller"] == controller
            ),
        )
        for controller in dataset["controllers"]
    ]
    _fast = [r for r in _common if r["time"] is not None]
    _fastest = min(_fast, key=lambda r: r["time"]) if _fast else None
    mo.md(f"""
    ## Findings from the selected cohort

    - **Arrival coverage:** {_arrival_names} reached the target in
      {_arrival_leader["successes"]}/{_arrival_leader["completed"]} completed flights
      ({_arrival_leader["arrival_rate"]:.1%}), the highest observed fraction in this view.
    - **Scored-flight median:** {_median_leader["controller"] if _median_leader else "unavailable"}
      has the highest median among scored flights in this view. Check the full
      distribution below: poor-score outliers can change the mean substantially.
    - **Successful-flight pace on common first-pass seeds:**
      {_fastest["controller"] if _fastest else "unavailable"} has the lowest mean
      arrival time. This comparison conditions on each controller's successful flights;
      the cumulative arrival curves provide a shared-denominator view.

    These observations describe this saved dataset. Arrival coverage, score quality
    and repeatability should be reported together rather than collapsed into one rank.
    """)
    return


@app.cell
def _(alt, comparison_rows, mo):
    scored_rows = [r for r in comparison_rows if r["official_score"] is not None]
    _chart = (
        alt.Chart(alt.Data(values=scored_rows))
        .mark_circle(size=65, opacity=0.65)
        .encode(
            x=alt.X("official_score:Q", title="Native official score (higher is better)"),
            y=alt.Y("controller:N", title=None),
            color=alt.Color("controller:N", legend=None),
            tooltip=["controller:N", "seed:O", "repetition:O", "official_score:Q", "run_id:N"],
        )
    )
    mo.vstack(
        [
            mo.md("""## Score distribution
        Each dot is a scored flight. These distributions are conditional on receiving
        a native score; they omit unscored timeouts. Mean score alone is therefore
        insufficient to rank controllers. Use arrival coverage alongside score and
        inspect poor-score outliers rather than dropping them.
        """),
            mo.ui.altair_chart(_chart.properties(height=190)),
            mo.ui.table(
                sorted(scored_rows, key=lambda r: r["official_score"])[:12],
                selection=None,
                page_size=6,
            ),
        ]
    )
    return (scored_rows,)


@app.cell
def _(alt, dataset, mean, mo, scored_rows):
    _parts = []
    for _controller in dataset["controllers"]:
        _group = [r for r in scored_rows if r["controller"] == _controller]
        for _component in ("offset_points", "time_points", "fuel_points", "damage_points"):
            _parts.append(
                dict(
                    controller=_controller,
                    component=_component,
                    points=mean(r.get(_component) for r in _group),
                )
            )
    _chart = (
        alt.Chart(alt.Data(values=_parts))
        .mark_bar()
        .encode(
            y=alt.Y("controller:N", title=None),
            x=alt.X(
                "points:Q", title="Mean score contribution (scored flights only)", stack="zero"
            ),
            color="component:N",
            tooltip=["controller:N", "component:N", "points:Q"],
        )
    )
    mo.vstack(
        [
            mo.md("""## Where score is gained and lost
    Successful arrival score = **50 + 100 × (120/time − fuel/75 − 2×damage/209200)**.
    Fuel has a penalty from the first kilogram. Damage is energy, not a collision count.
    Controller means may contain different successful seed subsets.
    """),
            mo.ui.altair_chart(_chart.properties(height=180)),
        ]
    )
    return


@app.cell
def _(alt, dataset, mo):
    _heat = [
        dict(
            controller=r["controller"],
            seed=str(r["seed"]),
            outcome=r["outcome"],
            score=r["official_score"],
        )
        for r in dataset["common"]
    ]
    _chart = (
        alt.Chart(alt.Data(values=_heat))
        .mark_rect()
        .encode(
            x=alt.X("seed:O", sort=[str(s) for s in dataset["common_seeds"]], title="Debris seed"),
            y=alt.Y("controller:N", title=None),
            color="outcome:N",
            tooltip=["controller:N", "seed:O", "outcome:N", "score:Q"],
        )
    )
    mo.vstack(
        [
            mo.md("""## Scenario sensitivity: common first pass
    This view always uses only seeds where every controller completed a first-pass
    flight. A seed missing any completed flight is excluded from this comparison.
    Radar noise seed stays fixed at 100; the debris seed changes initial impulses.
    """),
            mo.ui.altair_chart(_chart.properties(height=160)),
        ]
    )
    return


@app.cell
def _(Path, alt, dataset, mo, seed_sensitivity, write_csv):
    sensitivity_summary, seed_interactions = seed_sensitivity(
        dataset["common"], dataset["controllers"]
    )
    write_csv(Path(dataset["cache"]) / "seed_sensitivity.csv", sensitivity_summary)
    write_csv(Path(dataset["cache"]) / "seed_score_interactions.csv", seed_interactions)
    _chart = (
        alt.Chart(alt.Data(values=seed_interactions))
        .mark_rect()
        .encode(
            x=alt.X("seed:O", sort=dataset["common_seeds"], title="Debris seed (categorical)"),
            y=alt.Y("controller:N", title=None),
            color=alt.Color(
                "interaction_points:Q",
                title="Relative score shift",
                scale=alt.Scale(scheme="redblue", domainMid=0),
            ),
            tooltip=[
                "controller:N",
                "seed:O",
                "official_score:Q",
                "seed_mean_score:Q",
                "interaction_points:Q",
            ],
        )
    )
    mo.vstack(
        [
            mo.md("""## How sensitive is each controller to the seed?
    This section always uses **common first-pass seeds**, independently of the cohort
    selector. Score SD, interquartile range and extremes describe each controller's
    scored subset; arrival-time SD describes successful flights only. Check the
    scored counts and timeout counts before comparing spreads. Low spread alone
    does not imply good performance. SD needs at least two observations.

    The heatmap uses the **same jointly scored seeds for every controller**.
    Relative shift = score − controller mean − seed mean + grand mean, with all
    means calculated on that balanced subset. Positive values mean a controller
    performed better relative to its peers than its usual advantage on these seeds;
    negative values mean worse. Interaction SD summarizes these shifts in score
    points. Shared changes in seed difficulty are removed, not timeout risk.
    Unscored seeds are omitted here, never assigned zero; the outcome heatmap above
    retains them. An empty heatmap means no jointly scored seeds are available.

    Seed numbers are categorical identifiers, not a continuous difficulty scale.
    These are descriptive contrasts, not causal seed effects or population variance
    estimates: one first-pass flight per cell mixes scenario response with run
    variability. Compare the complete repeat pairs below before attributing a
    difference to the seed. Radar seed 100 stays fixed throughout.
    """),
            mo.ui.table(sensitivity_summary, selection=None),
            mo.ui.altair_chart(_chart.properties(height=180)),
        ]
    )
    return seed_interactions, sensitivity_summary


@app.cell
def _(Path, dataset, mo, sensitivity_summary, write_csv):
    seed_outcomes = []
    for _seed in dataset["common_seeds"]:
        _flights = [r for r in dataset["common"] if r["seed"] == _seed]
        seed_outcomes.append(
            dict(
                seed=_seed,
                successful_controllers=[
                    r["controller"] for r in _flights if r["outcome"] == "success"
                ],
                timeout_controllers=[
                    r["controller"] for r in _flights if r["outcome"] == "simulation_limit"
                ],
                successes=sum(r["outcome"] == "success" for r in _flights),
                completed=len(_flights),
            )
        )
    _spread = []
    for _summary in sensitivity_summary:
        _scored = [
            r
            for r in dataset["common"]
            if r["controller"] == _summary["controller"] and r["official_score"] is not None
        ]
        _worst = min(_scored, key=lambda r: r["official_score"]) if _scored else None
        _best = max(_scored, key=lambda r: r["official_score"]) if _scored else None
        _spread.append(
            dict(
                controller=_summary["controller"],
                scored_seeds=_summary["scored_seeds"],
                score_min=_summary["score_min"],
                score_max=_summary["score_max"],
                score_sample_sd=_summary["score_sd"],
                worst_scored_seed=_worst["seed"] if _worst else None,
                best_scored_seed=_best["seed"] if _best else None,
            )
        )
    _all_arrived = sum(r["successes"] == len(dataset["controllers"]) for r in seed_outcomes)
    _two_arrived = ", ".join(str(r["seed"]) for r in seed_outcomes if r["successes"] == 2)
    write_csv(Path(dataset["cache"]) / "seed_outcomes.csv", seed_outcomes)
    mo.vstack(
        [
            mo.md(f"""## Seed variability: results to discuss

        All four controllers arrived on **{_all_arrived} of {len(seed_outcomes)} common seeds**.
        Only two controllers arrived on seeds **{_two_arrived or "none"}**.
        Which controllers struggle changes with the seed; the outcome table identifies them.

        The score ranges and sample standard deviations below use scored first-pass
        flights only. Unscored timeouts are excluded, never replaced with zero.
        Tom's narrower successful-score spread comes alongside more timeouts and
        therefore does not establish greater overall robustness. The spread combines
        scenario sensitivity with execution variability; same-seed repeats below
        show why it cannot be attributed entirely to debris seed changes.
        """),
            mo.ui.table(_spread, selection=None),
            mo.ui.table(
                sorted(seed_outcomes, key=lambda r: (r["successes"], r["seed"])),
                selection=None,
                page_size=8,
            ),
        ]
    )
    return (seed_outcomes,)


@app.cell
def _(alt, dataset, mo):
    _curves = []
    for _controller in dataset["controllers"]:
        _flights = [r for r in dataset["common"] if r["controller"] == _controller]
        for _time in range(0, 601, 10):
            _arrived = sum(
                r["arrival_time_s"] is not None and r["arrival_time_s"] <= _time for r in _flights
            )
            _curves.append(
                dict(
                    controller=_controller,
                    time_s=_time,
                    arrived_fraction=_arrived / len(_flights) if _flights else None,
                )
            )
    _chart = (
        alt.Chart(alt.Data(values=_curves))
        .mark_line(interpolate="step-after")
        .encode(
            x=alt.X("time_s:Q", title="Simulation time (s)"),
            y=alt.Y(
                "arrived_fraction:Q",
                title="Fraction of common seeds arrived",
                scale=alt.Scale(domain=[0, 1]),
            ),
            color="controller:N",
            tooltip=["controller:N", "time_s:Q", "arrived_fraction:Q"],
        )
    )
    mo.vstack(
        [
            mo.md("""## Arrival speed and reliability together
    Cumulative successful arrivals use the same common first-pass denominator.
    Timeouts stay in that denominator through 600 s; they are not assigned an arrival time.
    """),
            mo.ui.altair_chart(_chart.properties(height=250)),
        ]
    )
    return


@app.cell
def _(alt, mo, scored_rows):
    _chart = (
        alt.Chart(alt.Data(values=scored_rows))
        .mark_circle(size=70, opacity=0.7)
        .encode(
            x=alt.X("arrival_time_s:Q", title="Native arrival time (s)"),
            y=alt.Y("fuel_kg:Q", title="Native fuel used (kg)"),
            color="controller:N",
            size=alt.Size("damage_J:Q", title="Damage (J)", scale=alt.Scale(range=[50, 500])),
            tooltip=[
                "controller:N",
                "seed:O",
                "repetition:O",
                "arrival_time_s:Q",
                "fuel_kg:Q",
                "damage_J:Q",
                "official_score:Q",
            ],
        )
    )
    mo.vstack(
        [
            mo.md("""## Fuel, damage and arrival tradeoffs
    These points use native scoring inputs. They show association, not causation.
    A zero-damage arrival can still score poorly from fuel or elapsed time.
    """),
            mo.ui.altair_chart(_chart.properties(height=280)),
        ]
    )
    return


@app.cell
def _(Path, alt, dataset, mo, paired_scores, paired_summary, write_csv):
    first_pass_pairs = paired_scores(dataset["common"])
    paired_statistics = paired_summary(first_pass_pairs)
    write_csv(Path(dataset["cache"]) / "paired_first_pass.csv", first_pass_pairs)
    _chart = (
        alt.Chart(alt.Data(values=first_pass_pairs))
        .mark_circle(size=65)
        .encode(
            x=alt.X("score_delta:Q", title="Candidate official score minus Tom"),
            y=alt.Y("controller:N", title=None),
            color="controller:N",
            tooltip=["controller:N", "seed:O", "score_delta:Q"],
        )
    )
    mo.vstack(
        [
            mo.md("""## Paired score differences against Tom
    Only common first-pass seeds where both flights have a native score enter this
    analysis. Timeout comparisons have no score delta. This is a selected successful
    subset, not an overall ranking. Bootstrap intervals resample those seed deltas
    3,000 times with a fixed analysis RNG seed of 100; they are exploratory,
    unadjusted 95% intervals and do not represent additional simulation runs.
    """),
            mo.ui.altair_chart(_chart.properties(height=170)),
            mo.ui.table(paired_statistics, selection=None),
        ]
    )
    return first_pass_pairs, paired_statistics


@app.cell
def _(Path, alt, completed_rows, dataset, mo, repeatability, write_csv):
    import statistics

    repeat_pairs = repeatability(completed_rows)
    write_csv(Path(dataset["cache"]) / "complete_repeat_pairs.csv", repeat_pairs)
    repeat_summary = []
    for _controller in dataset["controllers"]:
        _pairs = [r for r in repeat_pairs if r["controller"] == _controller]
        _changes = [abs(r["score_delta"]) for r in _pairs if r["score_delta"] is not None]
        repeat_summary.append(
            dict(
                controller=_controller,
                complete_pairs=len(_pairs),
                outcome_flips=sum(r["outcome_changed"] for r in _pairs),
                both_scored_pairs=len(_changes),
                median_absolute_score_change=statistics.median(_changes) if _changes else None,
                max_absolute_score_change=max(_changes) if _changes else None,
                outcome_flip_seeds=[r["seed"] for r in _pairs if r["outcome_changed"]],
            )
        )
    write_csv(Path(dataset["cache"]) / "repeatability_summary.csv", repeat_summary)
    _scored = [r for r in repeat_pairs if r["score_delta"] is not None]
    _chart = (
        alt.Chart(alt.Data(values=_scored))
        .mark_circle(size=65)
        .encode(
            x=alt.X("score_delta:Q", title="Second score minus first score"),
            y=alt.Y("controller:N", title=None),
            color="controller:N",
            tooltip=["controller:N", "seed:O", "score_delta:Q"],
        )
    )
    mo.vstack(
        [
            mo.md("""## Repeatability: complete pairs only
    A pair enters only when both recorded repetitions are completed flights for
    the same controller and debris seed. Incomplete pairs are ignored. Score deltas
    additionally require both native scores; outcome flips remain visible below.
    Repetitions measure variability on one seed, not independent scenarios.

    **Read the two denominators separately:** outcome flips use all complete pairs;
    absolute score changes use only pairs where both flights scored. A blank score
    change means an unavailable comparison, not zero variability. MPPI and Bui's
    unchanged arrival outcomes in the collected pairs do not guarantee unchanged
    scores. MVP's outcome flips and larger score changes warrant investigation,
    but these small, partially collected repeat samples do not establish population
    variability or isolate its cause. Fixed debris and radar seeds reproduce random
    sequences; asynchronous execution can still change the delivered observations.
    """),
            mo.ui.table(repeat_summary, selection=None),
            mo.ui.altair_chart(_chart.properties(height=170)),
            mo.ui.table(repeat_pairs, selection=None, page_size=8),
        ]
    )
    return repeat_pairs, repeat_summary


@app.cell
def _(alt, comparison_rows, mo):
    _timing = [
        dict(
            controller=r["controller"],
            seed=r["seed"],
            decision_p50_s=r.get("decision_p50_s"),
            command_interval_p50_s=r.get("command_interval_p50_s"),
            decision_p95_s=r.get("decision_p95_s"),
            observed_clock_ratio=r.get("observed_clock_ratio"),
        )
        for r in comparison_rows
    ]
    _chart = (
        alt.Chart(alt.Data(values=_timing))
        .mark_circle(size=60)
        .encode(
            x=alt.X(
                "command_interval_p50_s:Q",
                title="Per-flight median command interval (wall seconds)",
            ),
            y=alt.Y("controller:N", title=None),
            color="controller:N",
            tooltip=[
                "controller:N",
                "seed:O",
                "decision_p50_s:Q",
                "decision_p95_s:Q",
                "command_interval_p50_s:Q",
                "observed_clock_ratio:Q",
            ],
        )
    )
    mo.vstack(
        [
            mo.md("""## Controller pacing and observed simulation clock
    Each point is one flight's median command interval. Decision timing includes
    SDK, camera and UI work according to the capture definition; it is not isolated
    algorithm CPU time. Instrumentation overhead has not been removed. Observed
    clock ratios compare telemetry simulation-time change with monotonic wall time.
    """),
            mo.ui.altair_chart(_chart.properties(height=170)),
            mo.ui.table(_timing, selection=None, page_size=6),
        ]
    )
    return


@app.cell
def _(completed_rows, dataset, mean, mo):
    _audit = []
    for _controller in dataset["controllers"]:
        _group = [r for r in completed_rows if r["controller"] == _controller]
        _audit.append(
            dict(
                controller=_controller,
                completed=len(_group),
                flights_with_delivery_evidence=sum(
                    r.get("delivery_verified", False) for r in _group
                ),
                missing_telemetry=sum(not r.get("telemetry_samples") for r in _group),
                mission_id_anomalies=sum(len(r.get("native_mission_ids", [])) != 1 for r in _group),
                cleanup_anomalies=sum(r["cleanup"] != "owned_process_tree_exited" for r in _group),
                manifest_radar_hashes=len({r["radar_source_sha256"] for r in _group}),
                mean_observed_clock_ratio=mean(r.get("observed_clock_ratio") for r in _group),
                mean_per_flight_radar_age_p95_s=mean(
                    r.get("radar_worst_age_p95_s") for r in _group
                ),
            )
        )
    mo.vstack(
        [
            mo.md("""## Evidence audit across all completed flights
    This audit uses the entire retained largest dataset, independently of the cohort
    selector. Each flight should have telemetry, one native mission UUID and verified
    process cleanup. Radar source hash counts come from per-run manifests. Observed
    clock ratios near one support 1× playback over the sampled interval; they do not
    establish identical scheduling between controllers.
    """),
            mo.ui.table(_audit, selection=None),
        ]
    )
    return


@app.cell
def _(comparison_rows, mo):
    _delivery = [
        dict(
            controller=r["controller"],
            seed=r["seed"],
            repetition=r["repetition"],
            writes=r.get("command_writes"),
            actuator_samples=r.get("actuator_samples"),
            matched_linear_reads=r.get("linear_matched_reads"),
            nonzero_linear_reads=r.get("linear_nonzero_reads"),
            matched_angular_reads=r.get("angular_matched_reads"),
            nonzero_angular_reads=r.get("angular_nonzero_reads"),
            impulse_samples=r.get("nonzero_impulse_samples"),
            verified=r.get("delivery_verified"),
            cleanup=r["cleanup"],
            worst_radar_age_p95_s=r.get("radar_worst_age_p95_s"),
        )
        for r in comparison_rows
    ]
    mo.vstack(
        [
            mo.md("""## Command delivery and sensor freshness
    A verified flight has a nonzero native actuator read matching an earlier
    completed command write, plus a returned nonzero impulse sample. Linear and
    angular matches are counted separately: their SDK reads are not atomic.
    Exact-vector matching is conservative; matches may reuse a held command and
    do not establish per-write delivery, latency or isolated thrust causality.
    Radar age is the per-flight 95th percentile of the oldest nonnegative reported
    sensor age in each telemetry sample, when available.
    """),
            mo.ui.table(_delivery, selection=None, page_size=8),
        ]
    )
    return


@app.cell
def _(comparison_rows, mo):
    _timeouts = [
        dict(
            controller=r["controller"],
            seed=r["seed"],
            repetition=r["repetition"],
            closest_range_m=r.get("closest_range_m"),
            closest_time_s=r.get("closest_time_s"),
            final_range_m=r.get("last_range_m"),
            peak_speed_m_s=r.get("peak_speed_m_s"),
            last_fuel_kg=r.get("last_fuel_kg"),
            peak_damage_J=r.get("peak_damage_J"),
            damage_increase_intervals=r.get("damage_increase_intervals"),
            run_id=r["run_id"],
        )
        for r in comparison_rows
        if r["outcome"] == "simulation_limit"
    ]
    mo.vstack(
        [
            mo.md("""## Completed flights that reached the time limit
    Closest range distinguishes near approaches from trajectories that stayed far
    away. Peaks and closest range use all captured telemetry. Last fuel/range and
    peak damage are sampled diagnostics, not a recovered official score. A damage
    increase is an observed interval, not a counted collision. No causal label such
    as missed command, overshoot or radar failure is inferred from these scalars alone.
    """),
            mo.ui.table(_timeouts, selection=None, page_size=8),
        ]
    )
    return


@app.cell
def _(completed_rows, mo):
    run_choice = mo.ui.dropdown(
        options={
            f"{r['controller']} · seed {r['seed']} · repeat {r['repetition']} · {r['outcome']}": r[
                "run_id"
            ]
            for r in completed_rows
        },
        label="Inspect a completed flight",
        value=next(
            (
                f"{r['controller']} · seed {r['seed']} · repeat {r['repetition']} · {r['outcome']}"
                for r in completed_rows
                if r["controller"] == "ali_mppi"
            ),
            None,
        ),
    )
    mo.vstack([run_choice])
    return (run_choice,)


@app.cell
def _(alt, completed_rows, dataset, mo, run_choice):
    mo.stop(run_choice.value is None, mo.md("No completed flight selected."))
    selected_row = next(r for r in completed_rows if r["run_id"] == run_choice.value)
    _history = dataset["histories"][run_choice.value]
    _panels = []
    for _field, _title in (
        ("range_m", "Target range (m)"),
        ("relative_speed_m_s", "Relative speed (m/s)"),
        ("fuel_kg", "Telemetry fuel used (kg)"),
        ("damage_J", "Telemetry damage (J)"),
    ):
        _panels.append(
            alt.Chart(alt.Data(values=_history))
            .mark_line(point=False)
            .encode(
                x=alt.X("sim_s:Q", title="Simulation seconds"),
                y=alt.Y(f"{_field}:Q", title=_title),
                tooltip=["sim_s:Q", f"{_field}:Q"],
            )
            .properties(width=440, height=150)
        )
    mo.vstack(
        [
            mo.md(f"""## Flight history
    Run `{selected_row["run_id"]}`. Closest sampled range:
    **{selected_row.get("closest_range_m")} m**. These curves retain the first
    observation in each 2 s bucket plus the final observation; closest range and
    peak damage statistics use all telemetry samples. Brief contacts may fall between
    samples. Scoring receipts take precedence over the plotted pre-arrival samples.
    """),
            mo.ui.altair_chart(alt.vconcat(alt.hconcat(*_panels[:2]), alt.hconcat(*_panels[2:]))),
            mo.ui.table([selected_row], selection=None),
        ]
    )
    return (selected_row,)


@app.cell
def _(
    Path,
    controller_summary,
    dataset,
    mo,
    paired_statistics,
    repeat_pairs,
    repeat_summary,
    seed_outcomes,
    sensitivity_summary,
    write_json,
):
    analysis_snapshot = dict(
        campaign=dataset["root"],
        excluded=dataset["excluded"],
        common_first_pass_seeds=dataset["common_seeds"],
        selected_cohort_summary=controller_summary,
        paired_common_first_pass=paired_statistics,
        complete_repeat_pairs=len(repeat_pairs),
        repeatability_summary=repeat_summary,
        seed_outcomes=seed_outcomes,
        seed_sensitivity=sensitivity_summary,
    )
    write_json(Path(dataset["cache"]) / "notebook_summary.json", analysis_snapshot)
    mo.md(f"""
    ## Interpretation and exports

    Arrival coverage and scored-flight quality answer different questions.
    Prefer common seeds for controller comparisons, retain all outliers, and use
    complete repeat pairs to assess repeatability. Fuel/damage correlations and
    telemetry changes do not identify the cause of a controller failure.
    These are saved-field **initial-impulse scenarios**, not newly generated debris
    layouts. Fixed radar seed 100 fixes the noise draw sequence, while asynchronous
    timing can change the observations delivered to controllers.

    Compact, regenerable analysis outputs: `{dataset["cache"]}`.
    `completed_attempts.csv`, `common_first_pass.csv`, `paired_first_pass.csv`,
    `complete_repeat_pairs.csv`, `repeatability_summary.csv`, `seed_outcomes.csv`,
    `seed_sensitivity.csv`, `seed_score_interactions.csv`,
    and `notebook_summary.json` contain the analyzed
    rows. Per-flight caches are invalidated when receipt, manifest or event-file
    metadata changes. Raw evidence is read only; no simulator is started.
    """)
    return (analysis_snapshot,)


if __name__ == "__main__":
    app.run()
