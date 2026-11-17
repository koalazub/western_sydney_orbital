"""Read-only quick analysis of the final native STP campaign.

Open with marimo edit from the project UV environment (lab extra).
"""

import marimo

__generated_with = "0.25.2"
app = marimo.App(width="full")


@app.cell
def _():
    import csv
    import json
    import math
    import statistics as stats
    from pathlib import Path

    import altair as alt
    import marimo as mo

    root = Path(__file__).resolve().parents[4]
    run_dir = root / "runs/stp-20261117T040830Z-HY-final-001"
    _combined = root / "runs/stp-20261117T040830Z-HY-updated-candidates-combined-001"
    if (_combined / "attempts.csv").exists():
        run_dir = _combined
    with (run_dir / "attempts.csv").open(encoding="utf-8", newline="") as _stream:
        rows = list(csv.DictReader(_stream))
    versions = json.loads((run_dir / "controller_versions.json").read_text(encoding="utf-8"))

    _aliases = {"tom_polyfit": "polyfit_bodge", "tom_polyfit_v2": "polyfit_bodge_v2"}
    for _row in rows:
        _row["frozen_controller_id"] = _row["controller"]
        _row["controller"] = _aliases.get(_row["controller"], _row["controller"])
    versions = {_aliases.get(k, k): v for k, v in versions.items()}

    def number(value):
        try:
            result = float(value)
            return result if math.isfinite(result) else None
        except (TypeError, ValueError):
            return None

    def median(values):
        present = [v for v in values if v is not None]
        return stats.median(present) if present else None

    return Path, alt, json, median, mo, number, rows, run_dir, versions


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    # Final STP run overview Ã‚Â· 20261117T040830Z-HY

    Read-only analysis of **final-001**: native licensed STP 0.54.2, 22 saved
    impulse seeds Ãƒâ€” three controllers Ãƒâ€” one repetition, 300 s simulation budget.
    The historical original/V2 arms are displayed as **polyfit_bodge** and
    **polyfit_bodge_v2**; frozen IDs and hashes remain unchanged. When the matched
    MPPI-v2 combined report is available, it is loaded automatically. MPPI-v2 uses
    +Z attitude alignment, an 8.6-degree thrust gate, 25 m radial launch clearance,
    estimated radar-clock alignment and a combined raw/track emergency guard.
    See [MPPI-v2 design and execution](ali-mppi-v2.md).
    Pilot and earlier competition-config batches are excluded. No flights are launched.

    The primary comparison is corrected-cone V2 versus original-cone Polyfit.
    Both have identical own-position history interpolation, radar timestamp rebasing,
    duplicate/invalid/old-row handling, zero clock offset and disabled malformed fallback
    logging. Cruise remains 8 m/s; tracker, planner, governor and candidates are retained.
    Their intended code difference is the cone correction. MPPI is an exploratory
    whole-controller reference: Ali's native history correction retains a current-position
    fallback when history is unavailable. Shared radar/mission origin is an assumption.

    See [experimental design](experimental-design.md) and [source-change notes](README.md).
    The final-submission mission changes only the Pilot/OrbitPawn RotateToEntity
    references relative to the competition config; static equality elsewhere does not
    establish exact runtime equivalence. Official revision:
    `0d753a28e1a4265b2839bf8e9fbbc9194efafb87`; mission SHA-256:
    `04abcfb54321b22b68322ecf98d785cc548a7bd7b7ba447aac511d9a4d38e06e`.

    Latest eligible physical attempts count once per logical run, never best scores.
    Missing scores stay missing. Seeds vary impulses in one saved debris layout;
    one repetition cannot estimate within-seed repeatability. Asynchronous observations
    may differ between arms. Damage increases are not identified collision events.
    """)
    return


@app.cell(hide_code=True)
def _(mo, versions):
    mo.vstack(
        [
            mo.md(
                """
                ## Frozen controller provenance
                Polyfit commits below are **base/checkout commits**, not exact commits
                for the modified sources. Full bundle hashes identify the executed
                edits.
                """
            ),
            mo.ui.table(
                [
                    {
                        "controller": key,
                        "method": value["controller_method"],
                        "exact_source_commit": value["controller_source_commit"]
                        or "Uncommitted modifications",
                        "base_checkout_commit": value["controller_checkout_commit"],
                        "match": value["controller_commit_match"],
                        "changes": value["controller_source_changes"]
                        or "Exact checkout; native history correction",
                        "source_sha256": value["controller_source_sha256"],
                    }
                    for key, value in versions.items()
                ],
                selection=None,
            ),
        ]
    )
    return


@app.cell
def _(median, number, rows, versions):
    summaries = []
    for controller in versions:
        selected = [r for r in rows if r["controller"] == controller]
        successful = [r for r in selected if r["outcome"] == "success"]
        scores = [number(r["official_score"]) for r in selected]
        damage = [number(r["damage_J"]) for r in selected]
        summaries.append(
            {
                "controller": controller,
                "scheduled": len(selected),
                "arrivals": len(successful),
                "success_percent": 100 * len(successful) / len(selected),
                "timeouts": sum(r["outcome"] == "simulation_limit" for r in selected),
                "other_outcomes": [
                    r["outcome"]
                    for r in selected
                    if r["outcome"] not in {"success", "simulation_limit"}
                ],
                "zero_damage_arrivals": sum(number(r["damage_J"]) == 0 for r in successful),
                "damage_observations": sum(v is not None for v in damage),
                "median_damage_J_all": median(damage),
                "worst_damage_J_all": max((v for v in damage if v is not None), default=None),
                "scored_n": sum(v is not None for v in scores),
                "median_official_score": median(scores),
                "worst_official_score": min((v for v in scores if v is not None), default=None),
                "median_arrival_s_success_only": median(
                    number(r["arrival_time_s"]) for r in successful
                ),
                "median_fuel_kg_all": median(number(r["fuel_kg"]) for r in selected),
                "runs_retried": sum(int(r["retry_count"]) > 0 for r in selected),
                "extra_attempts": sum(int(r["retry_count"]) for r in selected),
            }
        )
    return (summaries,)


@app.cell(hide_code=True)
def _(alt, mo, rows, summaries):
    outcomes = (
        alt.Chart(alt.Data(values=rows))
        .mark_bar()
        .encode(
            x=alt.X("controller:N", title=None),
            y=alt.Y("count():Q", title="Scheduled runs"),
            color="outcome:N",
            tooltip=["controller:N", "outcome:N", "count():Q"],
        )
        .properties(title="All scheduled outcomes", width=450)
    )
    mo.vstack(
        [
            mo.md(
                """
                ## Outcomes and descriptive results
                Score statistics include scored flights only; arrival times include
                successes only. Fuel and damage include available final observations for
                all outcomes and can represent partial flights.
                """
            ),
            mo.ui.table(summaries, selection=None),
            mo.ui.altair_chart(outcomes),
        ]
    )
    return


@app.cell(hide_code=True)
def _(mo):
    score_cohort = mo.ui.dropdown(
        options=[
            "All scored flights per controller",
            "Same seeds scored by all available controllers",
            "Same seeds scored by original and V2",
            "Same seeds scored by original and MPPI",
            "Same seeds scored by frozen and updated MPPI",
            "Same seeds scored by bodge and upstream Polyfit",
        ],
        value="All scored flights per controller",
        label="Score comparison cohort",
    )
    mo.vstack(
        [
            mo.md("""
        ## Why is original Polyfit's median score higher?

        Across each controller's scored flights, original Polyfit's median is
        **47.10**, versus **26.39** for V2 and **19.40** for MPPI. Its scored-flight
        median fuel use is **46.55 kg**, versus **62.20** and **65.98 kg**. Arrival-time
        medians are similar (203.36, 203.49 and 205.79 s). The fuel penalty is a
        major descriptive explanation; damage and the distribution tails also matter.

        Original Polyfit has only **14/22 scored arrivals**, versus **21/22** and
        **18/22**. Its eight timeouts are absent from the score median. Choose a shared
        seed cohort to check how much the comparison changes when the sampled scenarios
        match. Even matched scored cohorts exclude failures and retain survivor selection.
        A higher median alone does not establish statistical significance or superiority.
        """),
            score_cohort,
        ]
    )
    return (score_cohort,)


@app.cell
def _(median, number, rows, score_cohort):
    _controllers = list(dict.fromkeys(r["controller"] for r in rows))
    _scored = [r for r in rows if number(r["official_score"]) is not None]
    _keys = {
        c: {(r["seed"], r["repetition"]) for r in _scored if r["controller"] == c}
        for c in _controllers
    }
    _mode = score_cohort.value
    _included = _controllers
    if _mode == "Same seeds scored by original and V2":
        _included = ["polyfit_bodge", "polyfit_bodge_v2"]
    elif _mode == "Same seeds scored by original and MPPI":
        _included = ["polyfit_bodge", "ali_mppi"]
    if _mode == "Same seeds scored by frozen and updated MPPI":
        _included = [c for c in ["ali_mppi", "ali_mppi_v2"] if c in _controllers]
    if _mode == "Same seeds scored by bodge and upstream Polyfit":
        _included = [c for c in ["polyfit_bodge", "tom_polyfit_updated"] if c in _controllers]
    _common = set.intersection(*[_keys[c] for c in _included])
    _chosen = [
        r
        for r in _scored
        if r["controller"] in _included
        and (
            _mode == "All scored flights per controller" or (r["seed"], r["repetition"]) in _common
        )
    ]
    score_components = [
        "offset_points",
        "time_points",
        "distance_points",
        "damage_points",
        "fuel_points",
    ]
    score_metrics = []
    score_details = []
    score_component_means = []
    median_witnesses = []
    for _c in _included:
        _group = sorted(
            [r for r in _chosen if r["controller"] == _c],
            key=lambda r: number(r["official_score"]),
        )
        _n = len(_group)
        if not _n:
            continue
        _values = [number(r["official_score"]) for r in _group]
        score_metrics.append(
            {
                "controller": _c,
                "scored_n": _n,
                "median_score": median(_values),
                "mean_score": sum(_values) / _n,
                "min_score": min(_values),
                "max_score": max(_values),
                "median_fuel_kg": median(number(r["fuel_kg"]) for r in _group),
                "median_arrival_s": median(number(r["arrival_time_s"]) for r in _group),
                "median_damage_J": median(number(r["damage_J"]) for r in _group),
            }
        )
        for _component in score_components:
            _present = [number(r[_component]) for r in _group if number(r[_component]) is not None]
            score_component_means.append(
                {
                    "controller": _c,
                    "component": _component,
                    "n": len(_present),
                    "mean_points": sum(_present) / len(_present) if _present else None,
                }
            )
        for _i, _r in enumerate(_group):
            _detail = {
                "controller": _c,
                "seed": _r["seed"],
                "official_score": number(_r["official_score"]),
                "fuel_kg": number(_r["fuel_kg"]),
                "arrival_time_s": number(_r["arrival_time_s"]),
                "damage_J": number(_r["damage_J"]),
                **{k: number(_r[k]) for k in score_components},
                "physical_run_id": _r["physical_run_id"],
            }
            _parts = [_detail[k] for k in score_components]
            _detail["official_minus_component_sum"] = (
                _detail["official_score"] - sum(_parts)
                if all(v is not None for v in _parts)
                else None
            )
            score_details.append(_detail)
            if _i in {(_n - 1) // 2, _n // 2}:
                median_witnesses.append(_detail)
    return median_witnesses, score_component_means, score_components, score_details, score_metrics


@app.cell(hide_code=True)
def _(alt, median_witnesses, mo, score_component_means, score_details, score_metrics):
    _scatter = (
        alt.Chart(alt.Data(values=score_details))
        .mark_circle(size=75)
        .encode(
            x=alt.X("fuel_kg:Q", title="Fuel used (kg), scored flights"),
            y=alt.Y("official_score:Q", title="Official score"),
            color="controller:N",
            tooltip=[
                "controller:N",
                "seed:N",
                "official_score:Q",
                "fuel_kg:Q",
                "arrival_time_s:Q",
                "damage_J:Q",
            ],
        )
        .properties(width=550, height=300)
    )
    _components = (
        alt.Chart(alt.Data(values=score_component_means))
        .mark_bar()
        .encode(
            x=alt.X("controller:N", title=None),
            y=alt.Y("mean_points:Q", title="Mean score component (points)"),
            color="component:N",
            tooltip=["controller:N", "component:N", "n:Q", "mean_points:Q"],
        )
        .properties(width=450, height=300)
    )
    mo.vstack(
        [
            mo.ui.table(score_metrics, selection=None),
            mo.hstack([mo.ui.altair_chart(_scatter), mo.ui.altair_chart(_components)]),
            mo.md("""
        **Flights defining the median:** an odd-sized sample uses its central flight;
        an even-sized sample averages its two central scores. Inspect those flights'
        components below to explain the displayed median directly. Component medians
        generally do not sum to the median total. The chart uses component **means**,
        which do sum to the mean total when all components are present. Positive and
        negative bars are stacked separately. The residual column checks the recorded
        component sum against the official score.
        """),
            mo.ui.table(median_witnesses, selection=None),
            mo.accordion(
                {
                    "All scored flights in selected cohort": mo.ui.table(
                        score_details, selection=None
                    )
                }
            ),
        ]
    )
    return


@app.cell
def _(number, rows, score_components):
    _original = {
        (r["seed"], r["repetition"]): r
        for r in rows
        if r["controller"] == "polyfit_bodge" and number(r["official_score"]) is not None
    }
    score_attribution = []
    for _other in ["polyfit_bodge_v2", "ali_mppi"]:
        _matched = [
            (_original[(r["seed"], r["repetition"])], r)
            for r in rows
            if r["controller"] == _other
            and number(r["official_score"]) is not None
            and (r["seed"], r["repetition"]) in _original
        ]
        for _field in ["official_score", *score_components]:
            _deltas = [
                number(a[_field]) - number(b[_field])
                for a, b in _matched
                if number(a[_field]) is not None and number(b[_field]) is not None
            ]
            score_attribution.append(
                {
                    "comparison": f"original minus {_other}",
                    "component": _field,
                    "paired_n": len(_deltas),
                    "mean_point_difference": sum(_deltas) / len(_deltas) if _deltas else None,
                }
            )
    return (score_attribution,)


@app.cell(hide_code=True)
def _(mo, score_attribution):
    mo.vstack(
        [
            mo.md("""
        ### Matched mean score attribution Ã‚Â· independent of the selector

        These comparisons use each pair of controllers' jointly scored seeds.
        **Positive means original Polyfit scores more.** Component mean differences
        add to the mean total difference; differences of medians do not offer that
        decomposition. Against V2 (13 pairs), original averages **27.68 points more**:
        about **23.49 fuel**, **2.91 damage**, and **1.28 time** points.
        Against MPPI (11 pairs), original averages **4.84 points less** despite its
        higher unpaired median: **+3.68 fuel**, **Ã¢Ë†â€™6.70 damage**, **Ã¢Ë†â€™1.83 time**.
        This illustrates both cohort selection and the sensitivity of means to poor
        flights. These are descriptive associations, not proof of a controller mechanism.
        Failures without scores remain excluded; see all scheduled outcomes above.
        """),
            mo.ui.table(score_attribution, selection=None),
        ]
    )
    return


@app.cell
def _(median, number, rows):
    by_key = {(r["controller"], r["seed"], r["repetition"]): r for r in rows}
    pairs = []
    for baseline in rows:
        if baseline["controller"] != "polyfit_bodge":
            continue
        treatment = by_key[("polyfit_bodge_v2", baseline["seed"], baseline["repetition"])]
        pair = {
            "seed": baseline["seed"],
            "repetition": baseline["repetition"],
            "original_outcome": baseline["outcome"],
            "v2_outcome": treatment["outcome"],
        }
        for metric in ["damage_J", "official_score", "arrival_time_s", "fuel_kg"]:
            a, b = number(baseline[metric]), number(treatment[metric])
            pair[f"delta_{metric}"] = b - a if a is not None and b is not None else None
        pair["joint_success"] = baseline["outcome"] == treatment["outcome"] == "success"
        pairs.append(pair)
    paired_stats = []
    for metric in ["damage_J", "official_score", "arrival_time_s", "fuel_kg"]:
        eligible = pairs if metric == "damage_J" else [p for p in pairs if p["joint_success"]]
        deltas = [p[f"delta_{metric}"] for p in eligible if p[f"delta_{metric}"] is not None]
        paired_stats.append(
            {
                "metric_V2_minus_original": metric,
                "paired_n": len(deltas),
                "median_delta": median(deltas),
                "min_delta": min(deltas, default=None),
                "max_delta": max(deltas, default=None),
            }
        )
    return paired_stats, pairs


@app.cell(hide_code=True)
def _(alt, mo, paired_stats, pairs):
    paired_plot = (
        alt.Chart(alt.Data(values=pairs))
        .mark_bar()
        .encode(
            x=alt.X("seed:N", sort=None),
            y=alt.Y("delta_damage_J:Q", title="V2 Ã¢Ë†â€™ original damage (J)"),
            tooltip=["seed:N", "original_outcome:N", "v2_outcome:N", "delta_damage_J:Q"],
        )
        .properties(title="Paired final damage, including partial flights", width="container")
    )
    mo.vstack(
        [
            mo.md(
                """
                ## Paired cone comparison
                Negative deltas favour V2 for damage, time and fuel; positive score
                deltas favour V2. Damage uses all available pairs; time, fuel and score
                use jointly successful pairs and therefore have survivor selection.
                Interpret with the outcome table above.
                """
            ),
            mo.ui.table(paired_stats, selection=None),
            mo.ui.altair_chart(paired_plot),
            mo.ui.table(pairs, selection=None),
        ]
    )
    return


@app.cell
def _(Path, json, number, rows, run_dir):
    timeouts = []
    for row in rows:
        if row["outcome"] != "simulation_limit":
            continue
        first = None
        official_initial = None
        # Stream only timeout evidence; no multi-gigabyte in-memory event collection.
        with (Path(row["source_campaign"]) / row["physical_run_id"] / "events.jsonl").open(
            encoding="utf-8"
        ) as _stream:
            for line in _stream:
                event = json.loads(line)
                candidate = number(event.get("initial_range_m"))
                if candidate is not None and candidate > 0 and official_initial is None:
                    official_initial = candidate
                if event.get("kind") == "telemetry" and first is None:
                    distance = number(event.get("range_m"))
                    if distance is not None and distance > 0:
                        first = (distance, number(event.get("sim_s")))
        initial = official_initial if official_initial is not None else first[0] if first else None
        final = number(row["range_m"])
        last_time = number(row["last_telemetry_sim_s"])
        timeouts.append(
            {
                "controller": row["controller"],
                "seed": row["seed"],
                "final_range_m": final,
                "last_telemetry_sim_s": last_time,
                "sample_minus_cutoff_s": last_time - 300 if last_time is not None else None,
                "initial_range_m": initial,
                "initial_source": "official recorded"
                if official_initial is not None
                else "first valid telemetry approximation"
                if first
                else "missing",
                "initial_sample_sim_s": first[1] if first and official_initial is None else None,
                "fractional_progress": 1 - final / initial
                if final is not None and initial
                else None,
                "fuel_kg": number(row["fuel_kg"]),
                "damage_J": number(row["damage_J"]),
                "relative_speed_m_s": number(row["last_relative_speed_m_s"]),
            }
        )
    return (timeouts,)


@app.cell(hide_code=True)
def _(mo, rows, timeouts):
    exceptions = [
        {
            key: row[key]
            for key in [
                "controller",
                "seed",
                "outcome",
                "termination_reason",
                "retry_count",
                "prior_outcomes",
                "physical_attempt_ids",
            ]
        }
        for row in rows
        if int(row["retry_count"]) or row["outcome"] not in {"success", "simulation_limit"}
    ]
    coverage = sum(r["fractional_progress"] is not None for r in timeouts)
    mo.vstack(
        [
            mo.md(
                f"""
                ## Simulation-budget timeouts
                Progress coverage: **{coverage}/{len(timeouts)}** timeouts. Range is the
                last observed centre-to-centre distance, not necessarily at exactly 300
                s. Positive sample-minus-cutoff means the last sample was after the
                cutoff. Progress is `1 Ã¢Ë†â€™ final / initial`, remains unclipped, and is
                descriptive rather than an official score. Aborts and evidence failures
                are separate below.
                """
            ),
            mo.ui.table(timeouts, selection=None),
            mo.md(
                """
                ## Exceptions and retry provenance
                Recovered evidence failures remain visible here. These are additional
                physical attempts, not additional independent scenarios.
                """
            ),
            mo.ui.table(exceptions, selection=None),
        ]
    )
    return


@app.cell(hide_code=True)
def _(median, mo, number, rows, score_components):
    _old = {(r["seed"], r["repetition"]): r for r in rows if r["controller"] == "ali_mppi"}
    _new = [r for r in rows if r["controller"] == "ali_mppi_v2"]
    _paired = []
    for _r in _new:
        _a = _old.get((_r["seed"], _r["repetition"]))
        if _a is None:
            continue
        _joint = _a["outcome"] == _r["outcome"] == "success"
        _p = {
            "seed": _r["seed"],
            "old_outcome": _a["outcome"],
            "new_outcome": _r["outcome"],
            "joint_success": _joint,
        }
        for _field in [
            "official_score",
            "fuel_kg",
            "arrival_time_s",
            "damage_J",
            *score_components,
        ]:
            _x, _y = number(_a[_field]), number(_r[_field])
            _p["delta_" + _field] = (
                _y - _x
                if _x is not None and _y is not None and (_joint or _field == "damage_J")
                else None
            )
        _paired.append(_p)
    _summary = []
    for _field in ["official_score", "fuel_kg", "arrival_time_s", "damage_J", *score_components]:
        _ds = [r["delta_" + _field] for r in _paired if r["delta_" + _field] is not None]
        _summary.append(
            {
                "metric_new_minus_old": _field,
                "paired_n": len(_ds),
                "median_delta": median(_ds),
                "mean_delta": sum(_ds) / len(_ds) if _ds else None,
                "min_delta": min(_ds, default=None),
                "max_delta": max(_ds, default=None),
            }
        )
    mo.vstack(
        [
            mo.md(f"""
        ## Updated versus frozen MPPI

        Updated flights available: **{len(_new)}/22**. Differences are **new minus old**:
        negative fuel/time/damage is favourable; positive score is favourable.
        Damage includes available partial-flight observations; other deltas require
        joint success. Compare with all-attempt outcomes above. Component **mean**
        differences sum to the mean score difference with complete observations.
        This is a whole-policy, cross-session comparison, not isolated fuel attribution.
        If no new flights appear, the combined analysis has not yet been generated.
        """),
            mo.ui.table(_summary, selection=None),
            mo.ui.table(_paired, selection=None) if _paired else mo.md("Awaiting matched results."),
        ]
    )
    return


@app.cell(hide_code=True)
def _(median, mo, number, rows):
    _upstream = [r for r in rows if r["controller"] == "tom_polyfit_updated"]
    _results = []
    _details = []
    for _reference in ["polyfit_bodge", "polyfit_bodge_v2"]:
        _baseline = {(r["seed"], r["repetition"]): r for r in rows if r["controller"] == _reference}
        _pairs = []
        for _r in _upstream:
            _a = _baseline.get((_r["seed"], _r["repetition"]))
            if _a is None:
                continue
            _p = {
                "reference": _reference,
                "seed": _r["seed"],
                "reference_outcome": _a["outcome"],
                "upstream_outcome": _r["outcome"],
            }
            for _metric in ["official_score", "fuel_kg", "arrival_time_s", "damage_J"]:
                _x, _y = number(_a[_metric]), number(_r[_metric])
                _joint = _a["outcome"] == _r["outcome"] == "success"
                _p[_metric] = (
                    _y - _x
                    if _x is not None and _y is not None and (_joint or _metric == "damage_J")
                    else None
                )
            _pairs.append(_p)
        _details.extend(_pairs)
        for _metric in ["official_score", "fuel_kg", "arrival_time_s", "damage_J"]:
            _values = [p[_metric] for p in _pairs if p[_metric] is not None]
            _results.append(
                {
                    "reference": _reference,
                    "metric_upstream_minus_reference": _metric,
                    "paired_n": len(_values),
                    "median_delta": median(_values),
                    "mean_delta": sum(_values) / len(_values) if _values else None,
                    "min_delta": min(_values, default=None),
                    "max_delta": max(_values, default=None),
                }
            )
    mo.vstack(
        [
            mo.md(f"""
        ## Upstream Polyfit versus preserved bodge variants

        Upstream flights available: **{len(_upstream)}/22**. `tom_polyfit_updated`
        identifies Tom's main source at `7d757fe2c988`, last changed by
        `8e2dc968d22f` (NaN/order-of-operations correction). It is separate from
        both timing-corrected bodge variants; this is a whole-source comparison.
        Differences are upstream minus reference. Damage uses available all-outcome
        observations; score, fuel and time use jointly successful seeds. The outcome
        table above discloses failures and scored coverage. No missing score is zero-filled.
        """),
            mo.ui.table(_results, selection=None),
            mo.ui.table(_details, selection=None)
            if _details
            else mo.md("Awaiting upstream results."),
        ]
    )
    return


if __name__ == "__main__":
    app.run()
