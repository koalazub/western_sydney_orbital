"""Matched-block SMAC3 adapter for the existing licensed native STP harness.

No simulator, scoring, retry, process or controller algorithm is implemented here.
"""

from __future__ import annotations

import argparse
import ast
import copy
import importlib.metadata
import json
import math
import shutil
import statistics
import sys
import tempfile
from collections import Counter
from pathlib import Path

REPO = next(
    p for p in Path(__file__).resolve().parents if (p / "experiments/stp_benchmark").is_dir()
)
HERE = Path(__file__).resolve().parent
HARNESS = HERE / "harness" if (HERE / "harness").is_dir() else REPO / "experiments/stp_benchmark"
sys.path.insert(0, str(HARNESS))
import benchmark  # noqa: E402
import evidence  # noqa: E402
import seeds as seed_tools  # noqa: E402

benchmark.REPO = REPO
SPACES = {
    "ali": {
        "directory": "controllers/harry/mppi",
        "entrypoint": "controller.py",
        "bounds": {
            "cruise_speed_mps": [7.0, 14.0],
            "plan_horizon_s": [15.0, 40.0],
            "risk_weight": [100.0, 1600.0],
        },
        "defaults": {"cruise_speed_mps": 8.0, "plan_horizon_s": 30.0, "risk_weight": 400.0},
        "fixed": {"avoidance": "mppi", "plan_steering": True},
    },
    "polyfit": {
        "directory": "controllers/tom/polyfit_v2_bodge",
        "entrypoint": "Controller.py",
        "bounds": {"cruise_speed": [6.0, 12.0], "margin": [3.0, 12.0], "horizon": [15.0, 60.0]},
        "defaults": {"cruise_speed": 8.0, "margin": 6.0, "horizon": 50.0},
        "fixed": {},
    },
}
ACTIVE_CONTROLLERS = ("ali",)
TRAIN_SEED_COUNT = 8
VALIDATION_SEED_COUNT = 4
FINAL_TEST_SEED_COUNT = 6
CAP_LOSS = 1000.0
ALTERNATIVE_CAP_LOSS = 500.0
# 1% of the native failure threshold, an absolute mean paired damage allowance.
DAMAGE_ALLOWANCE_J = 2092.0
COMPLETE_OUTCOMES = {"success", "failure", "simulation_limit", "exception"}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    evidence.write_json(path, value)


def configuration_space(name):
    from ConfigSpace import ConfigurationSpace, Float

    spec = SPACES[name]
    cs = ConfigurationSpace(seed=73)
    cs.add(
        [
            Float(k, tuple(v), default=spec["defaults"][k], log=k == "risk_weight")
            for k, v in spec["bounds"].items()
        ]
    )
    return cs


def settings(name, values):
    spec = SPACES[name]
    if set(values) != set(spec["bounds"]):
        raise ValueError("Trial must resolve every tunable setting")
    for key, value in values.items():
        low, high = spec["bounds"][key]
        if type(value) not in (int, float) or not low <= value <= high:
            raise ValueError(f"Invalid setting {key}")
    return dict(values) | spec["fixed"]


def controller(root, name, values, label):
    spec = SPACES[name]
    return dict(
        id=label,
        author="Ali El Ali; Harry Yendle terminal-coast patch"
        if name == "ali"
        else "Tom Kirkpatrick",
        method="MPPI with arrival-intersection terminal coast"
        if name == "ali"
        else "Polyfit v2 bodge",
        directory=str(root / "sources" / name),
        entrypoint=spec["entrypoint"],
        environment={"WSORB_STP_TUNING": json.dumps(settings(name, values), sort_keys=True)},
        source_commit=read(root / "experiment.json")["checkout_revision"],
        source_changes=(
            "Harry terminal-coast intersection patch plus native tuning inputs; "
            "frozen bundle SHA-256 is authoritative"
        ),
    )


def derived_bank(parent, values, bank_id):
    if not set(values) <= set(parent["seeds"]):
        raise ValueError("Stage seeds must be members of the frozen parent bank")
    return dict(
        schema=seed_tools.BANK_SCHEMA,
        id=bank_id,
        scenario=parent["scenario"],
        extension_algorithm="sha256-index-v1",
        seeds=values,
    )


def runtime_signature(native, provenance):
    """Compare pinned native conditions while allowing relocated snapshot paths."""
    snapshot = (native / "snapshot").resolve()
    dependencies = {}
    for name, record in provenance["dependencies"].items():
        path = Path(name)
        key = (
            "snapshot/" + path.relative_to(snapshot).as_posix()
            if path.is_absolute() and path.is_relative_to(snapshot)
            else name
        )
        if "sha256" not in record:
            raise ValueError(f"Unresolved native dependency: {name}")
        dependencies[key] = record
    keys = (
        "mission_sha256",
        "scorer_sha256",
        "package_revision",
        "release",
        "binaries",
        "python",
        "python_dependencies",
        "machine",
        "scheduling",
    )
    return {key: provenance[key] for key in keys} | {"dependencies": dependencies}


def assert_runtime(root, native=None, live=False):
    """Anchor every stage to the original baseline, including after resumption."""
    baseline = root / "baseline/native"
    anchor = read(root / "runtime-pin.json")
    # Protect the anchor provenance as well as its referenced package contents.
    if evidence.digest(baseline / "provenance.json") != anchor["provenance_sha256"]:
        raise ValueError("Original native provenance changed")
    original = read(baseline / "provenance.json")
    if benchmark.hashes(baseline / "snapshot") != original["snapshot_hashes"]:
        raise ValueError("Original native snapshot changed")
    if runtime_signature(baseline, original) != anchor["signature"]:
        raise ValueError("Original native runtime signature changed")
    if native is not None:
        signature = runtime_signature(native, read(native / "provenance.json"))
        if signature != anchor["signature"]:
            differences = [key for key in signature if signature[key] != anchor["signature"][key]]
            raise ValueError("Native stage conditions differ: " + ", ".join(differences))
    if live:
        if (
            sys.version != original["python"]
            or {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}
            != original["python_dependencies"]
        ):
            raise ValueError("Native runner Python environment changed")
        for path, expected in original["binaries"].items():
            if evidence.digest(path) != expected:
                raise ValueError(f"Native STP binary changed since baseline: {path}")
        for path, expected in original["dependencies"].items():
            if evidence.digest(path) != expected["sha256"]:
                raise ValueError(f"Native asset changed since baseline: {path}")
        release = benchmark.path(read(root / "experiment.json")["native_config"]["install"])
        if (release / "ReleaseVersion_0.54.2_Win64_User.txt").read_text() != original["release"]:
            raise ValueError("Native STP release changed since baseline")


def prepare_stage(root, target, values, controllers):
    """Use the harness's bank validation, staging and complete provenance unchanged."""
    experiment = read(root / "experiment.json")
    parent = read(
        root
        / ("final-bank.json" if values == experiment["final_test"] else "optimisation-bank.json")
    )
    if target != root / "baseline":
        assert_runtime(root, live=True)
    target.mkdir(parents=True, exist_ok=False)
    bank = derived_bank(parent, values, target.relative_to(root).as_posix())
    write(target / "bank.json", bank)
    config = copy.deepcopy(experiment["native_config"])
    config.update(
        controllers=controllers,
        reference=controllers[0]["id"],
        repetitions=1,
        seed_bank=str(target / "bank.json"),
        seed_bank_id=bank["id"],
        seed_bank_prefix_count=len(values),
        seed_bank_prefix_sha256=seed_tools.seed_digest(values),
        seed_count=len(values),
        seeds=values,
        execution_batch=target.relative_to(root).as_posix(),
    )
    write(target / "input.json", config)
    benchmark.prepare(target / "input.json", target / "native", "seeded")
    if (root / "runtime-pin.json").exists():
        assert_runtime(root, target / "native")
    return target / "native"


def loss(row, cap_loss=CAP_LOSS):
    if row["outcome"] not in COMPLETE_OUTCOMES:
        raise ValueError(f"Incomplete native evaluation: {row['outcome']}")
    score = row.get("official_score")
    if score is not None:
        if row["outcome"] not in {"success", "failure"} or not math.isfinite(score):
            raise ValueError("Invalid native score/outcome")
        return -score
    if row["outcome"] in {"success", "failure"}:
        raise ValueError("Scored native outcome missing its exact score")
    return cap_loss


def rows(native, label, assigned):
    campaign = read(native / "campaign.json")
    result = []
    for attempt in campaign["attempts"]:
        if attempt["controller"]["id"] != label:
            continue
        folder, retry, receipt = evidence.latest_attempt(native, attempt["run_id"])
        if not receipt or receipt.get("outcome") not in COMPLETE_OUTCOMES:
            raise ValueError(
                f"Incomplete evidence at {folder}; resolve the failure before continuing"
            )
        if receipt.get("cleanup") != "owned_process_tree_exited" or receipt.get("cleanup_errors"):
            raise ValueError("Incomplete native cleanup; evaluation cannot be ranked")
        if (
            receipt["outcome"] == "exception"
            and receipt.get("error", {}).get("role") != "controller"
        ):
            raise ValueError("Infrastructure exception is incomplete evidence")
        manifest = read(folder / "manifest.json")
        if manifest["settings"] != attempt["controller"]["environment"] or manifest[
            "effective_seeds"
        ] != {"spawner": attempt["seed"], "radar": 100}:
            raise ValueError("Manifest settings/seed mismatch")
        inputs = receipt.get("inputs") or {}
        last = {}
        # Stream telemetry; the optimiser never needs to retain a full trace in memory.
        events = folder / "events.jsonl"
        if events.exists():
            with events.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("kind") == "telemetry":
                        last = event
        result.append(
            dict(
                seed=attempt["seed"],
                outcome=receipt["outcome"],
                official_score=receipt.get("official_score"),
                damage_J=inputs.get("damageTaken_J", last.get("damage_J")),
                fuel_kg=inputs.get("fuelUsed_kg", last.get("fuel_kg")),
                final_distance_m=inputs.get(
                    "distanceToShip", receipt.get("range_m", last.get("range_m"))
                ),
                arrival_time_s=inputs.get("time_s") if receipt["outcome"] == "success" else None,
                arrival_speed_m_s=last.get("relative_speed_m_s")
                if receipt["outcome"] == "success"
                else None,
                speed_source="last telemetry sample; not atomic with scoring",
                last_sim_s=last.get("sim_s"),
                wall_s=receipt.get("wall_s"),
                retry_count=retry,
                receipt=str(folder / "receipt.json"),
                manifest=str(folder / "manifest.json"),
            )
        )
    if sorted(r["seed"] for r in result) != sorted(assigned):
        raise ValueError("Cannot rank unmatched or missing seed coverage")
    for row in result:
        loss(row)
    return result


def run_native(native, resume):
    root = next(p for p in native.parents if (p / "runtime-pin.json").is_file())
    assert_runtime(root, native, live=True)
    if not benchmark.run_campaign(native, continue_controller_errors=True, resume=resume):
        raise RuntimeError(
            f"Native harness halted at {native}; retry policy and all evidence preserved"
        )


def average_loss(group, cap_loss=CAP_LOSS):
    return statistics.mean(loss(r, cap_loss) for r in group)


def rank(candidates, baseline, cap_loss=CAP_LOSS):
    expected = sorted(r["seed"] for r in baseline)
    for group in candidates.values():
        if sorted(r["seed"] for r in group) != expected:
            raise ValueError("Candidates require identical complete coverage")
    return sorted(candidates, key=lambda key: (average_loss(candidates[key], cap_loss), key))


def paired(candidate, baseline):
    baseline = {r["seed"]: r for r in baseline}
    return [
        dict(
            seed=r["seed"],
            score_delta=r["official_score"] - baseline[r["seed"]]["official_score"]
            if r["official_score"] is not None and baseline[r["seed"]]["official_score"] is not None
            else None,
            **{
                f"{k}_delta": r[k] - baseline[r["seed"]][k]
                if r[k] is not None and baseline[r["seed"]][k] is not None
                else None
                for k in ("damage_J", "fuel_kg", "arrival_time_s", "arrival_speed_m_s")
            },
        )
        for r in candidate
    ]


def safety(candidate, baseline):
    deltas = paired(candidate, baseline)
    observed = all(d["damage_J_delta"] is not None for d in deltas)
    return dict(
        arrivals=sum(r["outcome"] == "success" for r in candidate),
        baseline_arrivals=sum(r["outcome"] == "success" for r in baseline),
        mean_damage_delta_J=statistics.mean(d["damage_J_delta"] for d in deltas)
        if observed
        else None,
        eligible=observed
        and sum(r["outcome"] == "success" for r in candidate)
        >= sum(r["outcome"] == "success" for r in baseline)
        and statistics.mean(d["damage_J_delta"] for d in deltas) <= DAMAGE_ALLOWANCE_J,
    )


def choose_finalist(combined, candidates, cap_loss=CAP_LOSS):
    """Baseline wins ties; only safe, strictly lower-loss settings replace it."""
    baseline = combined[0]
    ordered = rank({i: combined[i] for i in candidates}, baseline, cap_loss)
    return next(
        (
            i
            for i in ordered
            if safety(combined[i], baseline)["eligible"]
            and average_loss(combined[i], cap_loss) < average_loss(baseline, cap_loss)
        ),
        0,
    )


def make_smac(name, output):
    from smac import HyperparameterOptimizationFacade as Facade
    from smac import Scenario
    from smac.initial_design.default_design import DefaultInitialDesign

    scenario = Scenario(
        configuration_space(name),
        name=name,
        output_directory=output,
        deterministic=True,
        n_trials=8,
        seed=73,
        use_default_config=False,
    )
    # Each trial uses the same matched training block; no individual-seed racing.
    return Facade(
        scenario,
        lambda config, seed: 0.0,
        initial_design=DefaultInitialDesign(scenario, n_configs=1),
        intensifier=Facade.get_intensifier(scenario, max_config_calls=1),
        overwrite=False,
        logging_level=False,
    )


def tune(root, name, resume):
    from smac.runhistory.dataclasses import TrialValue

    experiment = read(root / "experiment.json")
    train, validation = experiment["train"], experiment["validation"]
    folder = root / name
    folder.mkdir(exist_ok=True)
    journal_path = folder / "screen.json"
    journal = read(journal_path) if journal_path.exists() else []
    all_rows = {}
    # Replay persisted ask/tell history in a fresh temporary model directory. Never
    # replay flights. Check every proposal before using persisted evidence.
    with tempfile.TemporaryDirectory(prefix="stp-smac-") as temporary:
        optimiser = make_smac(name, Path(temporary))
        for index in range(8):
            trial = optimiser.ask()
            values = dict(trial.config)
            label = f"{name}_{index:02d}"
            if index < len(journal):
                if journal[index]["values"] != values:
                    raise ValueError(
                        "SMAC replay differs from saved proposal; refusing to replace it"
                    )
            else:
                journal.append(dict(values=values, loss=None))
                write(journal_path, journal)  # persist proposal BEFORE any native launch
            native = (
                root / "baseline/native" if index == 0 else folder / f"screen-{index:02d}/native"
            )
            if not native.exists():
                if index == 0:
                    raise ValueError("Missing pinned baseline")
                prepare_stage(root, native.parent, train, [controller(root, name, values, label)])
            if journal[index]["loss"] is None:
                run_native(native, resume)
            group = rows(native, label if index else f"{name}_baseline", train)
            current = average_loss(group)
            if journal[index]["loss"] is not None and journal[index]["loss"] != current:
                raise ValueError("Completed evidence changed")
            journal[index]["rows"] = group
            journal[index]["loss"] = current
            write(journal_path, journal)
            optimiser.tell(trial, TrialValue(cost=current, time=0.0))
            all_rows[index] = group
    top = rank({i: all_rows[i] for i in range(1, 8)}, all_rows[0])[:2]
    alternative_top = rank(
        {i: all_rows[i] for i in range(1, 8)}, all_rows[0], ALTERNATIVE_CAP_LOSS
    )[:2]
    confirm = folder / "confirm/native"
    if not confirm.exists():
        prepare_stage(
            root,
            confirm.parent,
            validation,
            [controller(root, name, journal[i]["values"], f"{name}_{i:02d}") for i in [0, *top]],
        )
    run_native(confirm, resume)
    combined = {i: all_rows[i] + rows(confirm, f"{name}_{i:02d}", validation) for i in [0, *top]}
    chosen = choose_finalist(combined, top)
    alternative_chosen = choose_finalist(combined, top, ALTERNATIVE_CAP_LOSS)
    selection = dict(
        finalist=chosen,
        values=journal[chosen]["values"],
        top_two=top,
        training_validation_rows=combined,
        paired={i: paired(combined[i], combined[0]) for i in top},
        safety={i: safety(combined[i], combined[0]) for i in top},
        cap_penalty=CAP_LOSS,
        alternative_penalty=ALTERNATIVE_CAP_LOSS,
        alternative_screen_top_two=alternative_top,
        alternative_shortlist_changed=set(top) != set(alternative_top),
        alternative_finalist=alternative_chosen,
        penalty_changes_selection=chosen != alternative_chosen,
        fallback="untuned baseline unless a safe candidate strictly improves matched loss",
    )
    selection_path = folder / "selection.json"
    if selection_path.exists() and read(selection_path) != json.loads(json.dumps(selection)):
        raise ValueError("Frozen selection changed")
    # Once selected, preserve both the data and its integrity record.
    if not selection_path.exists():
        write(selection_path, selection)
    pin_path = folder / "selection-pin.json"
    expected = dict(sha256=evidence.digest(selection_path))
    if pin_path.exists() and read(pin_path) != expected:
        raise ValueError("Frozen selection pin changed")
    if not pin_path.exists():
        write(pin_path, expected)
    return selection


def final_test(root, name, resume):
    # Never called until every selected controller has a frozen finalist.
    experiment = read(root / "experiment.json")
    chosen = read(root / name / "selection.json")
    if read(root / name / "selection-pin.json")["sha256"] != evidence.digest(
        root / name / "selection.json"
    ):
        raise ValueError("Frozen finalist changed")
    target = root / name / "final-test"
    if not (target / "native").exists():
        prepare_stage(
            root,
            target,
            experiment["final_test"],
            [
                controller(root, name, SPACES[name]["defaults"], f"{name}_baseline"),
                controller(root, name, chosen["values"], f"{name}_finalist"),
            ],
        )
    run_native(target / "native", resume)
    baseline = rows(target / "native", f"{name}_baseline", experiment["final_test"])
    finalist = rows(target / "native", f"{name}_finalist", experiment["final_test"])
    write(
        target / "comparison.json",
        dict(
            baseline=baseline,
            finalist=finalist,
            paired=paired(finalist, baseline),
            safety=safety(finalist, baseline),
            mean_loss=average_loss(finalist),
            baseline_mean_loss=average_loss(baseline),
            historical=historical_pairs(root, finalist, baseline),
            historical_coverage=archive_coverage(read(root / "archive.json")),
        ),
    )


def historical_pairs(root, finalist, baseline):
    current = {r["seed"]: r for r in finalist}
    untuned = {r["seed"]: r for r in baseline}
    return [
        record
        | dict(
            finalist_score=current[record["seed"]]["official_score"],
            baseline_score=untuned[record["seed"]]["official_score"],
            finalist_score_delta=current[record["seed"]]["official_score"]
            - record["official_score"]
            if record["directly_comparable"]
            and record["official_score"] is not None
            and current[record["seed"]]["official_score"] is not None
            else None,
            baseline_score_delta=untuned[record["seed"]]["official_score"]
            - record["official_score"]
            if record["directly_comparable"]
            and record["official_score"] is not None
            and untuned[record["seed"]]["official_score"] is not None
            else None,
        )
        for record in read(root / "archive.json")
    ]


def archive_coverage(records):
    """Keep attempted coverage and cohorts separate from scored subsets."""
    groups = {}
    for record in records:
        key = (record["campaign"], record["controller"])
        groups.setdefault(key, []).append(record)
    return [
        dict(
            campaign=campaign,
            controller=controller,
            attempted=len(group),
            scored=sum(r["official_score"] is not None for r in group),
            outcomes=dict(Counter(r["outcome"] for r in group)),
            compatible_complete=sum(r["directly_comparable"] for r in group),
        )
        for (campaign, controller), group in sorted(groups.items())
    ]


def archive_audit(root):
    """Conservative direct-comparison gate; never substitute historical baselines."""
    pinned = root / "baseline/native"
    fresh = read(pinned / "provenance.json")
    config = read(pinned / "campaign.json")["config"]
    output = []
    held_out = set(read(root / "experiment.json")["final_test"])
    for source in sorted((REPO / "runs").rglob("campaign.json")):
        if source.is_relative_to(root) or {"snapshot", ".venv"} & set(
            source.relative_to(REPO / "runs").parts
        ):
            continue
        if source.parent == root or not (source.parent / "provenance.json").exists():
            continue
        old = read(source)
        if not old.get("attempts"):
            continue
        provenance = read(source.parent / "provenance.json")
        reasons = []
        for key in (
            "mission_sha256",
            "scorer_sha256",
            "package_revision",
            "release",
            "binaries",
            "scheduling",
        ):
            if provenance.get(key) != fresh.get(key):
                reasons.append(key)
        for key in (
            "seed_scope",
            "limits",
            "sample_period_s",
            "diagnostic_rerun",
            "thresholds",
            "repetitions",
        ):
            if old["config"].get(key) != config.get(key):
                reasons.append(key)

        # Package paths differ between frozen copies; compare multiset of transitive
        # asset content hashes, with unresolved dependencies explicitly disqualifying.
        def deps(p):
            return sorted(v.get("sha256", "MISSING") for v in p.get("dependencies", {}).values())

        if deps(provenance) != deps(fresh) or "MISSING" in deps(provenance):
            reasons.append("transitive_assets")
        old_adapter = source.parent / "snapshot/seed_adapter.json"
        if not old_adapter.exists() or read(old_adapter) != read(
            pinned / "snapshot/seed_adapter.json"
        ):
            reasons.append("seed_adapter_radar")
        for attempt in old["attempts"]:
            if attempt["seed"] not in held_out:
                continue
            folder, retry, receipt = evidence.latest_attempt(source.parent, attempt["run_id"])
            if not receipt:
                continue
            problems = list(reasons)
            if (
                receipt.get("outcome") not in COMPLETE_OUTCOMES
                or (
                    receipt.get("outcome") in {"success", "failure"}
                    and receipt.get("official_score") is None
                )
                or receipt.get("cleanup") != "owned_process_tree_exited"
                or receipt.get("cleanup_errors")
                or (
                    receipt.get("outcome") == "exception"
                    and receipt.get("error", {}).get("role") != "controller"
                )
            ):
                problems.append("incomplete_evidence")
            manifest_path = folder / "manifest.json"
            manifest = read(manifest_path) if manifest_path.exists() else {}
            if manifest.get("effective_seeds") != {"spawner": attempt["seed"], "radar": 100}:
                problems.append("effective_seeds")
            if (
                manifest.get("limits") != config["limits"]
                or manifest.get("normal_scheduling") is not True
                or manifest.get("diagnostic_rerun") is not False
            ):
                problems.append("manifest_limits_scheduling")
            output.append(
                dict(
                    campaign=str(source.parent),
                    seed=attempt["seed"],
                    controller=attempt["controller"]["id"],
                    receipt=str(folder / "receipt.json"),
                    receipt_sha256=evidence.digest(folder / "receipt.json"),
                    manifest=str(manifest_path),
                    manifest_sha256=evidence.digest(manifest_path)
                    if manifest_path.exists()
                    else None,
                    official_score=receipt.get("official_score"),
                    outcome=receipt.get("outcome", "incomplete"),
                    retry_count=retry,
                    controller_metadata=attempt["controller"],
                    inputs=receipt.get("inputs"),
                    directly_comparable=not problems,
                    condition_mismatches=problems,
                    baseline_reuse=False,
                    baseline_reason=(
                        "No reuse: new frozen controller revision and "
                        "explicit resolved settings required"
                    ),
                )
            )
    write(root / "archive.json", output)


def prepare(output):
    root = Path(output).resolve()
    if not root.is_relative_to(REPO / "runs"):
        raise ValueError("Output must be under runs/")
    root.mkdir(parents=True, exist_ok=False)
    config = read(REPO / "docs/analytics/stp-runs/20261117T040830Z-HY/updated-candidates.json")
    for key in ("source_commit", "supersedes_execution", "seeds", "controllers", "reference"):
        config.pop(key, None)
    final_bank = read(REPO / config["seed_bank"])
    if len(final_bank["seeds"]) != 22:
        raise ValueError("Expected the existing frozen 22-seed final-test bank")
    write(root / "final-bank.json", final_bank)
    bank_id = "phobos-smac3-optimisation-v1"
    used = set(final_bank["seeds"])
    values = []
    for index in range(TRAIN_SEED_COUNT + VALIDATION_SEED_COUNT):
        value = seed_tools.appended_seed(bank_id, index, used)
        values.append(value)
        used.add(value)
    bank = dict(
        schema=seed_tools.BANK_SCHEMA,
        id=bank_id,
        scenario=final_bank["scenario"],
        extension_algorithm="sha256-index-v1",
        seeds=values,
    )
    write(root / "optimisation-bank.json", bank)
    config.update(experiment_id=root.name, execution_batch="smac3", diagnostic_rerun=False)
    sources = root / "sources"
    sources.mkdir()
    resolved = {}
    for name in ACTIVE_CONTROLLERS:
        spec = SPACES[name]
        shutil.copytree(
            REPO / spec["directory"],
            sources / name,
            ignore=shutil.ignore_patterns("__pycache__", "cam_*.png", "controller_log.txt"),
        )
        resolved[name] = dict(
            tunable_defaults=spec["defaults"],
            fixed=spec["fixed"],
            source_hashes=benchmark.hashes(sources / name),
        )
    ali_tree = ast.parse((sources / "ali/controller.py").read_text())
    default_policy = next(
        ast.literal_eval(n.value)
        for n in ali_tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "DEFAULT_POLICY" for t in n.targets)
    )
    resolved["ali"]["resolved_policy"] = default_policy | SPACES["ali"]["fixed"]
    resolved["ali"]["native_fixed"] = {"time_budget_s": 300.0, "arrival_radius_m": 50.0}
    write(
        root / "experiment.json",
        dict(
            schema="wsorb.stp.smac3.v1",
            native_config=config,
            checkout_revision=benchmark.revision(REPO),
            train=values[:TRAIN_SEED_COUNT],
            validation=values[TRAIN_SEED_COUNT:],
            final_test=final_bank["seeds"][:FINAL_TEST_SEED_COUNT],
            final_test_selection="first six seeds in frozen bank order; independent of scores",
            final_bank_source=config["seed_bank"],
            final_bank_sha256=evidence.digest(REPO / config["seed_bank"]),
            resolved_settings=resolved,
            spaces={name: SPACES[name] for name in ACTIVE_CONTROLLERS},
            controllers=list(ACTIVE_CONTROLLERS),
            cap_loss=CAP_LOSS,
            alternative_cap_loss=ALTERNATIVE_CAP_LOSS,
            damage_allowance_J=DAMAGE_ALLOWANCE_J,
            collision_early_abort=False,
            simulation_ceiling_s=300,
            smac_seed=73,
            flight_budget=len(ACTIVE_CONTROLLERS)
            * (8 * TRAIN_SEED_COUNT + 3 * VALIDATION_SEED_COUNT + 2 * FINAL_TEST_SEED_COUNT),
            baseline_archive_reuse=0,
            objective=(
                "negative mean exact native score over the assigned matched block; "
                "finite loss for unscored cap/controller exception; "
                "incomplete infrastructure evidence halts"
            ),
            extended_ceiling=(
                "optional separate 420 s matched reruns, maximum two seeds/controller; "
                "not launched by this adapter"
            ),
            python=sys.version,
            dependencies={
                d.metadata["Name"]: d.version for d in importlib.metadata.distributions()
            },
        ),
    )
    prepare_stage(
        root,
        root / "baseline",
        values[:TRAIN_SEED_COUNT],
        [
            controller(root, name, SPACES[name]["defaults"], f"{name}_baseline")
            for name in ACTIVE_CONTROLLERS
        ],
    )
    original_native = root / "baseline/native"
    original_provenance = read(original_native / "provenance.json")
    write(
        root / "runtime-pin.json",
        dict(
            provenance_sha256=evidence.digest(original_native / "provenance.json"),
            signature=runtime_signature(original_native, original_provenance),
        ),
    )
    # Future trial preparation uses this exact package snapshot, not a mutable checkout.
    experiment = read(root / "experiment.json")
    experiment["native_config"]["package"] = str(root / "baseline/native/snapshot/STU4-Sim")
    write(root / "experiment.json", experiment)
    snapshot = root / "adapter"
    snapshot.mkdir()
    for filename in ("optimise.py", "pyproject.toml", "uv.lock", "README.md"):
        shutil.copy2(HERE / filename, snapshot / filename)
    shutil.copytree(
        HARNESS, snapshot / "harness", ignore=shutil.ignore_patterns("__pycache__", "*.lock")
    )
    archive_audit(root)
    pinned_files = [p for base in (sources, snapshot) for p in base.rglob("*") if p.is_file()]
    pinned_files += [
        root / name
        for name in (
            "experiment.json",
            "final-bank.json",
            "optimisation-bank.json",
            "archive.json",
            "runtime-pin.json",
        )
    ]
    write(root / "pins.json", {str(p.relative_to(root)): evidence.digest(p) for p in pinned_files})
    print(
        json.dumps(
            dict(
                prepared=str(root),
                train=values[:TRAIN_SEED_COUNT],
                validation=values[TRAIN_SEED_COUNT:],
                final_test=experiment["final_test"],
                flights=experiment["flight_budget"],
                controllers=experiment["controllers"],
            ),
            indent=2,
        )
    )


def assert_pins(root):
    for relative, expected in read(root / "pins.json").items():
        if evidence.digest(root / relative) != expected:
            raise ValueError(f"Frozen source changed: {relative}")
    experiment = read(root / "experiment.json")
    for package in ("smac", "ConfigSpace", "numpy", "scipy", "scikit-learn"):
        if importlib.metadata.version(package) != experiment["dependencies"][package]:
            raise ValueError(f"Optimiser dependency changed: {package}")
    if set(experiment["train"] + experiment["validation"]) & set(experiment["final_test"]):
        raise ValueError("Final-test leakage")
    if seed_tools.seed_digest(experiment["final_test"]) != seed_tools.seed_digest(
        read(root / "final-bank.json")["seeds"][: len(experiment["final_test"])]
    ):
        raise ValueError("Final-test bank changed")


def main():
    parser = argparse.ArgumentParser(__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--output", required=True)
    run = sub.add_parser("run")
    run.add_argument("root", type=Path)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--stage", choices=("tune", "final-test", "all"), default="all")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.output)
        return
    root = args.root.resolve()
    assert_pins(root)
    names = read(root / "experiment.json")["controllers"]
    # Bank lock is an exclusive campaign-adapter lock; native flight locking and
    # owned process cleanup remain entirely in the existing STP harness.
    with seed_tools.bank_lock(root / "adapter-run"):
        if args.stage in {"tune", "all"}:
            for name in names:
                tune(root, name, args.resume)
        if args.stage in {"final-test", "all"}:
            if any(not (root / name / "selection.json").exists() for name in names):
                raise ValueError("Freeze all selected finalists before opening final-test evidence")
            for name in names:
                final_test(root, name, args.resume)


if __name__ == "__main__":
    main()
