"""Read-only SMAC3 MPPI results; open with the project UV lab extra."""

import marimo

__generated_with = "0.25.2"
app = marimo.App(width="full")


@app.cell
def _():
    import json
    import statistics as stats
    from pathlib import Path

    import altair as alt
    import marimo as mo

    data = json.loads(Path(__file__).with_name("results.json").read_text(encoding="utf-8"))
    exp = data["experiment"]
    finalist = f"Trial {data['selection']['finalist']}"
    own_baseline = "Trial 0"
    recent = "Broad sweep: recent MPPI v2"

    def mean(values):
        values = [v for v in values if v is not None]
        return stats.mean(values) if values else None

    def loss(row, penalty=1000):
        if row["official_score"] is not None:
            return -row["official_score"]
        if row["outcome"] == "simulation_limit":
            return penalty
        return None

    def describe(rows, label):
        losses = [loss(r) for r in rows]
        complete = all(v is not None for v in losses)
        return {
            "Group": label,
            "Flights": len(rows),
            "Arrivals": sum(r["outcome"] == "success" for r in rows),
            "Caps": sum(r["outcome"] == "simulation_limit" for r in rows),
            "Native scores available": sum(r["official_score"] is not None for r in rows),
            "Incomplete evidence": sum(v is None for v in losses),
            "Mean loss (cap +1000)": mean(losses) if complete else None,
            "Mean loss (cap +500)": mean([loss(r, 500) for r in rows]) if complete else None,
            "Mean native score (scored only)": mean([r["official_score"] for r in rows]),
            "Mean damage J (all flights)": mean([r["damage_J"] for r in rows]),
            "Mean fuel kg (all flights)": mean([r["fuel_kg"] for r in rows]),
            "Mean arrival time s (arrivals only)": mean(
                [r["arrival_time_s"] for r in rows if r["outcome"] == "success"]
            ),
        }

    test = [r for r in data["rows"] if r["stage"] == "Held-out test"]
    test_seeds = exp["final_test"]
    archive_arms = sorted({r["arm"] for r in data["archive"]})
    broad_table = [
        describe([r for r in data["archive"] if r["arm"] == arm], arm) for arm in archive_arms
    ]
    broad_best = min(
        [r for r in broad_table if r["Mean loss (cap +1000)"] is not None],
        key=lambda r: r["Mean loss (cap +1000)"],
    )["Group"]
    return (
        alt,
        archive_arms,
        broad_best,
        broad_table,
        data,
        describe,
        exp,
        finalist,
        loss,
        mean,
        mo,
        own_baseline,
        recent,
        test,
        test_seeds,
    )


@app.cell
def _(data, finalist, mo, own_baseline, test, describe):
    _base = describe([r for r in test if r["arm"] == own_baseline], "Baseline")
    _tuned = describe([r for r in test if r["arm"] == finalist], "Finalist")
    mo.md(f"""
    # SMAC3 MPPI: training improvement did not survive the held-out test

    **Completed campaign:** `{data["campaign"]}` · 88 logical flights · 8 training seeds,
    4 additional confirmation seeds, 6 frozen held-out seeds.

    SMAC3 selected **{finalist}**. Training and confirmation improved, but the finalist
    arrived on **{_tuned["Arrivals"]}/6** held-out seeds versus **{_base["Arrivals"]}/6**
    for its own untuned, patched MPPI baseline. Mean held-out loss rose from
    **{_base["Mean loss (cap +1000)"]:.2f}** to
    **{_tuned["Mean loss (cap +1000)"]:.2f}** (lower is better).
    Mean damage increased by **{data["final_safety"]["mean_damage_delta_J"]:,.0f} J**.
    **The finalist fails the campaign arrival/damage gate; these results do not justify
    replacing its baseline.**

    Native scores remain null for caps. The +1000 optimisation penalty is a loss,
    never a native STP score. The four jointly scored held-out pairs alone are not
    evidence of an overall improvement: they exclude the two capped finalist flights.
    """)
    return


@app.cell
def _(data, exp, mo):
    mo.md(f"""
    ## Design and frozen settings

    Baseline plus seven proposals were evaluated on the same eight training seeds.
    The baseline and top two proposals received four additional confirmation seeds;
    selection used all 12 tuning seeds. Only the frozen baseline and finalist saw the
    six test seeds, selected by bank order before scores were inspected.

    Training: `{exp["train"]}`  
    Confirmation: `{exp["validation"]}`  
    Held-out: `{exp["final_test"]}`

    The optimisation bank is separate from the frozen 22-seed broad-sweep bank; all
    22 were excluded from tuning. This viability experiment tested the first six.
    One saved debris field is used with varied initial impulses and fixed radar seed
    100. Results do not measure generalisation across field layouts.

    Both campaign arms use the same frozen Harry copy of Ali's controller with the
    terminal-coast intersection patch. `avoidance="mppi"` and `plan_steering=True`
    are explicitly passed through `WSORB_STP_TUNING`; the capture checks every
    experiment manifest against its trial settings. All other policy settings are
    fixed. The broad-sweep versions have different code/defaults and are historical
    controller references, not the causal baseline for the tuning effect.

    Bounds: speed 7–14 m/s; horizon 15–40 s; risk weight 100–1600 (log).
    SMAC3 seed {exp["smac_seed"]}. Main ceiling 300 simulation seconds, normal
    scheduling, collision abort disabled. No extended-ceiling runs enter this report.
    """)
    return


@app.cell
def _(data, mo):
    mo.ui.table(
        data["trials"], selection=None, label="Trial settings and eight-seed training results"
    )
    return


@app.cell
def _(mo):
    penalty = mo.ui.dropdown([1000, 500], value=1000, label="Cap loss sensitivity")
    mo.output.replace(penalty)
    return (penalty,)


@app.cell
def _(alt, data, loss, mean, mo, penalty):
    _curve = []
    _best = float("inf")
    for _trial in data["trials"]:
        _index = _trial["trial"]
        _rows = [
            r for r in data["rows"] if r["stage"] == "Training" and r["arm"] == f"Trial {_index}"
        ]
        _value = mean([loss(r, penalty.value) for r in _rows])
        _best = min(_best, _value)
        _curve.extend(
            [
                {"Trial": _index, "Loss": _value, "Series": "Evaluated configuration"},
                {"Trial": _index, "Loss": _best, "Series": "Running minimum"},
            ]
        )
    _chart = (
        alt.Chart(alt.Data(values=_curve))
        .mark_line(point=True)
        .encode(
            x=alt.X("Trial:O", title="Configuration order (0 = source-default baseline)"),
            y=alt.Y(
                "Loss:Q",
                title="Mean loss over eight matched training seeds",
                scale=alt.Scale(zero=False),
            ),
            color="Series:N",
            tooltip=["Trial:O", "Series:N", alt.Tooltip("Loss:Q", format=".3f")],
        )
        .properties(height=310, title=f"Training objective, cap loss +{penalty.value}")
    )
    mo.vstack(
        [
            mo.ui.altair_chart(_chart),
            mo.md(
                "Each point is a complete eight-flight aggregate, not one seed. "
                "The running minimum "
                "is a descriptive best observed training loss, not a safety-qualified incumbent. "
                "There are only seven proposals: this is a small viability search, "
                "not convergence evidence."
            ),
        ]
    )
    return


@app.cell
def _(data, describe, finalist, mo, own_baseline):
    _table = []
    for _stage in ("Training", "Confirmation", "Training + confirmation", "Held-out test"):
        for _arm in (own_baseline, finalist):
            _rows = [
                r
                for r in data["rows"]
                if r["arm"] == _arm
                and (
                    r["stage"] == _stage
                    or (
                        _stage == "Training + confirmation"
                        and r["stage"] in ("Training", "Confirmation")
                    )
                )
            ]
            _table.append(describe(_rows, f"{_stage}: {_arm}"))
    mo.vstack(
        [
            mo.md(
                "## Stage results\n\nLower loss is better. "
                "Means labelled scored-only or arrival-only "
                "have smaller coverage when flights cap; compare arrivals first."
            ),
            mo.ui.table(_table, selection=None),
            mo.md(
                f"Top two training proposals: `{data['selection']['top_two']}`. "
                f"Frozen finalist: `{data['selection']['finalist']}`. "
                f"Changing cap loss to +500 changes selection: "
                f"**{data['selection']['penalty_changes_selection']}**. "
                "Both penalties also rank the held-out baseline ahead of the finalist."
            ),
        ]
    )
    return


@app.cell
def _(archive_arms, broad_best, broad_table, data, describe, mo, recent, test, test_seeds):
    _matched = [
        describe([r for r in data["archive"] if r["arm"] == arm and r["seed"] in test_seeds], arm)
        for arm in archive_arms
    ]
    _matched.extend(
        describe([r for r in test if r["arm"] == arm], arm)
        for arm in sorted({r["arm"] for r in test})
    )
    mo.vstack(
        [
            mo.md(f"""## Broad-sweep references: choose the metric before calling a controller best

        These four 22-seed scheduled arms keep controller versions separate. The recent
        MPPI reference is **{recent}**. The best of these arms under the campaign's
        +1000 cap-loss objective with complete evidence is **{broad_best}**.
        Selection uses the full broad
        sweep, not the six test scores. A high mean among arrivals can coexist with
        poor arrival coverage; it is not the same ranking as mean loss.

        The cone/timing-corrected Polyfit has 21 arrivals and one owner-interrupted
        flight (seed 1729). It remains visible but unranked: its mean loss is null,
        and comparisons omit that missing pair rather than treating it as a cap.
        The updated Polyfit arm with controller exceptions and its exception-selected
        follow-up cohort are excluded from this complete-cohort ranking. They cannot
        supply matched, complete optimisation evidence. No interrupted run or
        infrastructure exception is assigned an invented native score.
        """),
            mo.ui.table(broad_table, selection=None),
            mo.md(
                "### Same six test seeds across versions\n\nThe full-sweep table above "
                "establishes historical context; this table restricts every arm to the "
                "same six frozen seeds used for the optimiser test."
            ),
            mo.ui.table(_matched, selection=None),
        ]
    )
    return


@app.cell
def _(archive_arms, mo, own_baseline, recent):
    reference = mo.ui.dropdown(
        [own_baseline, *archive_arms], value=recent, label="Paired reference"
    )
    metric = mo.ui.dropdown(
        ["official_score", "loss", "damage_J", "fuel_kg", "arrival_time_s", "arrival_speed_m_s"],
        value="official_score",
        label="Paired metric",
    )
    mo.hstack([reference, metric])
    return metric, reference


@app.cell
def _(alt, data, finalist, loss, mean, metric, mo, own_baseline, reference, test, test_seeds):
    _all = test + [r for r in data["archive"] if r["seed"] in test_seeds]
    _lookup = {(r["arm"], r["seed"]): r for r in _all}
    _pairs = []
    _plot = []
    for _seed in test_seeds:
        _a, _b = _lookup[(reference.value, _seed)], _lookup[(finalist, _seed)]
        _field = metric.value
        _av = loss(_a) if _field == "loss" else _a[_field]
        _bv = loss(_b) if _field == "loss" else _b[_field]
        _pairs.append(
            {
                "Seed": _seed,
                "Reference outcome": _a["outcome"],
                "Finalist outcome": _b["outcome"],
                "Reference": _av,
                "Finalist": _bv,
                "Delta (finalist - reference)": _bv - _av
                if _av is not None and _bv is not None
                else None,
            }
        )
        for _arm in dict.fromkeys([reference.value, own_baseline, finalist]):
            _row = _lookup[(_arm, _seed)]
            _v = loss(_row) if _field == "loss" else _row[_field]
            if _v is not None:
                _plot.append(
                    {
                        "Seed": str(_seed),
                        "Value": _v,
                        "Controller": _arm,
                        "Outcome": _row["outcome"],
                    }
                )
    _valid = [r for r in _pairs if r["Delta (finalist - reference)"] is not None]
    _delta = mean([r["Delta (finalist - reference)"] for r in _valid])
    _direction = (
        "Higher is better" if metric.value == "official_score" else "Lower is generally better"
    )
    _chart = (
        alt.Chart(alt.Data(values=_plot))
        .mark_point(size=110)
        .encode(
            x=alt.X("Seed:N", sort=[str(s) for s in test_seeds]),
            y=alt.Y("Value:Q", title=metric.value, scale=alt.Scale(zero=False)),
            color="Controller:N",
            shape="Outcome:N",
            tooltip=["Seed:N", "Controller:N", "Outcome:N", "Value:Q"],
        )
        .properties(height=320, title="Same six frozen test seeds")
    )
    mo.vstack(
        [
            mo.md(
                f"## Paired held-out comparison\n\n**{reference.value}** → **{finalist}**. "
                f"{_direction}; speed alone is descriptive. Paired coverage: **{len(_valid)}/6**. "
                f"Mean paired delta: **{_delta:.3f}**. "
                "Native score points are omitted for caps; select loss to include "
                "their finite penalty. Incomplete historical flights stay missing. "
                "No pair with a missing score is silently assigned zero."
            ),
            mo.ui.altair_chart(_chart),
            mo.ui.table(_pairs, selection=None),
            mo.md(
                "Arrival speed uses the last telemetry sample and is not atomic "
                "with the score event. "
                "Damage/fuel for caps use the saved last-telemetry summaries. "
                "The historical comparison combines code and settings changes; only Trial 0 versus "
                "the finalist isolates tuning on the same patched controller."
            ),
        ]
    )
    return


@app.cell
def _(data, mo, test):
    mo.vstack(
        [
            mo.md(
                "## Final-test evidence and cap diagnostics\n\nNull scores are preserved. "
                "Final straight-line distance, fuel and damage help distinguish capped recovery "
                "attempts; this report does not infer an early-abort rule "
                "or assume a 420 s recovery."
            ),
            mo.ui.table(test, selection=None),
            mo.md(
                "## Provenance and comparison checks\n\nCapture fails if mission/scorer hashes, "
                "STP release or binaries, package inputs, referenced dependency hashes, "
                "seed scenario, sampling, scheduling or run limits differ. "
                "Each matched historical seed also requires "
                "the same effective seeds, seed adapter, seeded-spawner hash and manifest limits. "
                "Controller bundle hashes are intentionally distinct across historical versions. "
                "Observation adapter hashes are disclosed below; "
                "these are native receipt comparisons, not a claim that different "
                "controller implementations or instrumentation are identical."
            ),
            mo.accordion(
                {
                    "Archive compatibility checks": mo.json(data["compatibility"]),
                    "Pinned controller versions": mo.json(data["versions"]),
                    "Native runtime and inputs": mo.json(data["runtime"]),
                    "Experiment observation adapter hashes": mo.json(data["adapter_hashes"]),
                    "Fixed and resolved policy settings": mo.json(
                        data["experiment"]["resolved_settings"]
                    ),
                    "Evidence file SHA-256 index": mo.json(data["source_sha256"]),
                }
            ),
            mo.md(
                "The snapshot uses each source campaign's latest-attempt summary "
                "and checks it against "
                "its receipt. Retries are not extra independent seeds. Experiment retry count: "
                f"**{sum(r.get('retry_count', 0) for r in data['rows'])}**. "
                "The shipped results.json lets this notebook run without "
                "the large ignored telemetry trees; "
                "capture.py rebuilds it from the original evidence without launching flights."
            ),
        ]
    )
    return


if __name__ == "__main__":
    app.run()
