"""Capture a portable, receipt-checked analysis snapshot; never launches STP."""

import csv
import hashlib
import json
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
CAMPAIGN = ROOT / "runs/20261118T073111Z-phobos-harry-mppi-viability"
SOURCES = [
    ROOT / "runs/stp-20261117T040830Z-HY-final-001",
    ROOT / "runs/stp-20261117T040830Z-HY-updated-candidates-001",
]
ARMS = {
    "ali_mppi": "Broad sweep: MPPI original",
    "tom_polyfit": "Broad sweep: Polyfit original",
    "tom_polyfit_v2": "Broad sweep: Polyfit cone/timing corrected",
    "ali_mppi_v2": "Broad sweep: recent MPPI v2",
}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def relative(path):
    return Path(path).resolve().relative_to(ROOT).as_posix()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def loss(row, penalty=1000):
    if row["official_score"] is not None:
        return -row["official_score"]
    if row["outcome"] == "simulation_limit":
        return penalty
    raise ValueError(f"Incomplete/unscored evidence: {row}")


def summary(rows):
    scored = [r["official_score"] for r in rows if r["official_score"] is not None]
    result = {
        "n": len(rows),
        "arrivals": sum(r["outcome"] == "success" for r in rows),
        "caps": sum(r["outcome"] == "simulation_limit" for r in rows),
        "scored": len(scored),
        "mean_native_score_scored_only": statistics.mean(scored) if scored else None,
        "mean_loss": statistics.mean(loss(r) for r in rows),
        "mean_loss_penalty_500": statistics.mean(loss(r, 500) for r in rows),
    }
    for field in ("damage_J", "fuel_kg", "arrival_time_s", "arrival_speed_m_s", "wall_s"):
        values = [r[field] for r in rows if r.get(field) is not None]
        result[f"mean_{field}"] = statistics.mean(values) if values else None
        result[f"n_{field}"] = len(values)
    return result


def canonical_inputs(provenance):
    def key(path):
        path = path.replace("\\", "/")
        if "/snapshot/STU4-Sim/" in path:
            return "package/" + path.split("/snapshot/STU4-Sim/", 1)[1]
        if "/SPT-Sim/" in path:
            return "install/" + path.split("/SPT-Sim/", 1)[1]
        if "/STPro/" in path:
            return "STPro/" + path.split("/STPro/", 1)[1]
        raise ValueError(f"Unrecognised dependency path {path}")

    return {
        "package_revision": provenance["package_revision"],
        "mission_sha256": provenance["mission_sha256"],
        "scorer_sha256": provenance["scorer_sha256"],
        "release": provenance["release"],
        "binaries": {key(k): v for k, v in provenance["binaries"].items()},
        "dependencies": {key(k): v for k, v in provenance["dependencies"].items()},
        "package_files": {
            k: v for k, v in provenance["snapshot_hashes"].items() if k.startswith("STU4-Sim/")
        },
        "limits": provenance["configuration"]["limits"],
        "sample_period_s": provenance["configuration"]["sample_period_s"],
        "diagnostic_rerun": provenance["configuration"]["diagnostic_rerun"],
        "scheduling": provenance["scheduling"],
        "seed_scenario": provenance["seed_selection"]["scenario"],
    }


def manifest_conditions(manifest):
    return {
        k: manifest[k]
        for k in (
            "effective_seeds",
            "native_seed_adapter",
            "seed_scope",
            "limits",
            "normal_scheduling",
            "diagnostic_rerun",
        )
    } | {
        "seeded_spawner": [c["seeded_sha256"] for c in manifest["changes"] if "seeded_sha256" in c]
    }


def main():
    experiment = read(CAMPAIGN / "experiment.json")
    screen = read(CAMPAIGN / "ali/screen.json")
    selection = read(CAMPAIGN / "ali/selection.json")
    final = read(CAMPAIGN / "ali/final-test/comparison.json")
    provenance_path = CAMPAIGN / "ali/final-test/native/provenance.json"
    reference = read(provenance_path)
    evidence = {}
    versions = {}
    conditions = {}

    def record(path):
        evidence[relative(path)] = digest(path)

    def checked(row, arm, expected=None):
        row = dict(row)
        rp, mp = Path(row["receipt"]), Path(row["manifest"])
        receipt, manifest = read(rp), read(mp)
        if (
            receipt["outcome"] != row["outcome"]
            or receipt["official_score"] != row["official_score"]
        ):
            raise ValueError(f"Stored summary disagrees with receipt: {rp}")
        if manifest["attempt"]["seed"] != row["seed"]:
            raise ValueError(f"Wrong seed: {mp}")
        if receipt.get("cleanup") != "owned_process_tree_exited":
            raise ValueError(f"Unverified cleanup: {rp}")
        if expected is not None:
            settings = json.loads(manifest["settings"]["WSORB_STP_TUNING"])
            if settings != expected:
                raise ValueError(f"Wrong settings: {mp}")
        conditions[(arm, row["seed"])] = manifest_conditions(manifest)
        row.update(
            arm=arm,
            receipt=relative(rp),
            manifest=relative(mp),
            termination_reason=receipt.get("termination_reason"),
        )
        for path in (rp, mp):
            record(path)
        native = rp.parent.parent
        vp = native / "controller_versions.json"
        if vp.exists():
            identity = manifest["attempt"]["controller"]["id"]
            versions[arm] = read(vp)[identity]
            record(vp)
        pp = native / "provenance.json"
        if pp.exists():
            prov = read(pp)
            if canonical_inputs(prov) != canonical_inputs(reference):
                raise ValueError(f"Native conditions differ: {pp}")
            record(pp)
        row["metric_source"] = (
            "Native receipt; speed is last telemetry sample, not atomic with scoring. "
            "When receipt inputs are absent, metrics use the saved last-telemetry summary."
        )
        return row

    rows = []
    trials = []
    for index, trial in enumerate(screen):
        arm = f"Trial {index}"
        expected = trial["values"] | {"avoidance": "mppi", "plan_steering": True}
        group = [checked(r, arm, expected) | {"stage": "Training"} for r in trial["rows"]]
        if [r["seed"] for r in group] != experiment["train"]:
            raise ValueError("Training coverage differs")
        trials.append({"trial": index, **trial["values"], **summary(group)})
        if abs(trials[-1]["mean_loss"] - trial["loss"]) > 1e-9:
            raise ValueError("Loss disagrees with optimiser")
        rows.extend(group)
    for index, group in selection["training_validation_rows"].items():
        index = int(index)
        extra = [r for r in group if r["seed"] in experiment["validation"]]
        if [r["seed"] for r in extra] != experiment["validation"]:
            raise ValueError("Confirmation coverage differs")
        expected = screen[index]["values"] | {"avoidance": "mppi", "plan_steering": True}
        rows.extend(
            checked(r, f"Trial {index}", expected) | {"stage": "Confirmation"} for r in extra
        )
    for name, index in (("baseline", 0), ("finalist", selection["finalist"])):
        expected = screen[index]["values"] | {"avoidance": "mppi", "plan_steering": True}
        if [r["seed"] for r in final[name]] != experiment["final_test"]:
            raise ValueError("Held-out coverage differs")
        rows.extend(
            checked(r, f"Trial {index}", expected) | {"stage": "Held-out test"} for r in final[name]
        )
    if len(rows) != 88 or len({r["receipt"] for r in rows}) != 88:
        raise ValueError("Expected 88 distinct logical seed evaluations")
    if set(experiment["train"]) & set(experiment["validation"]):
        raise ValueError("Training and confirmation overlap")
    bank = read(CAMPAIGN / "final-bank.json")
    # The original 22-seed bank, not just the six selected tests, is excluded from tuning.
    frozen_seeds = reference["configuration"].get("seeds", experiment["final_test"])
    bank_seeds = bank.get("seeds", frozen_seeds)
    if set(experiment["train"] + experiment["validation"]) & set(bank_seeds):
        raise ValueError("Frozen test bank leaked into tuning")

    archive = []
    compatibility = []
    for source in SOURCES:
        prov = read(source / "provenance.json")
        left, right = canonical_inputs(reference), canonical_inputs(prov)
        checks = {k: left[k] == right[k] for k in left}
        compatibility.append(
            {
                "source": relative(source),
                "checks": checks,
                "compatible": all(checks.values()),
                "observation_description_matches": reference["observation"] == prov["observation"],
                "adapter_hashes": {
                    k: v
                    for k, v in prov["snapshot_hashes"].items()
                    if k in ("benchmark.py", "capture.py", "evidence.py", "seeds.py")
                },
            }
        )
        if not all(checks.values()):
            raise ValueError(f"Archive is incompatible: {compatibility[-1]}")
        record(source / "provenance.json")
        record(source / "attempts.csv")
        with (source / "attempts.csv").open(encoding="utf-8", newline="") as stream:
            for r in csv.DictReader(stream):
                if r["controller"] not in ARMS:
                    continue
                if json.loads(r["physical_attempt_ids"])[-1] != r["physical_run_id"]:
                    raise ValueError("Archive does not use latest physical attempt")
                rp = source / r["physical_run_id"] / "receipt.json"
                row = {
                    "seed": int(r["seed"]),
                    "outcome": r["outcome"],
                    "official_score": float(r["official_score"]) if r["official_score"] else None,
                    "receipt": str(rp),
                    "manifest": str(rp.with_name("manifest.json")),
                    "retry_count": int(r["retry_count"] or 0),
                }
                for target, field in {
                    "damage_J": "damage_J",
                    "fuel_kg": "fuel_kg",
                    "arrival_time_s": "arrival_time_s",
                    "arrival_speed_m_s": "last_relative_speed_m_s",
                    "final_distance_m": "range_m",
                    "wall_s": "wall_s",
                }.items():
                    row[target] = float(r[field]) if r.get(field) else None
                archive.append(checked(row, ARMS[r["controller"]]) | {"stage": "Broad sweep"})
    for arm in ARMS.values():
        group = [r for r in archive if r["arm"] == arm]
        if len(group) != 22 or len({r["seed"] for r in group}) != 22:
            raise ValueError(f"Archive arm lacks 22 unique seeds: {arm}")
        for seed in experiment["final_test"]:
            if conditions[(arm, seed)] != conditions[("Trial 0", seed)]:
                raise ValueError(f"Archived seed inputs/limits differ: {arm} {seed}")

    for path in (
        CAMPAIGN / "experiment.json",
        CAMPAIGN / "ali/screen.json",
        CAMPAIGN / "ali/selection.json",
        CAMPAIGN / "ali/final-test/comparison.json",
        CAMPAIGN / "final-bank.json",
    ):
        record(path)
    snapshot = {
        "campaign": relative(CAMPAIGN),
        "experiment": experiment,
        "trials": trials,
        "rows": rows,
        "archive": archive,
        "selection": {k: v for k, v in selection.items() if k != "training_validation_rows"},
        "final_safety": final["safety"],
        "versions": versions,
        "compatibility": compatibility,
        "runtime": canonical_inputs(reference),
        "adapter_hashes": {
            k: v
            for k, v in reference["snapshot_hashes"].items()
            if k in ("benchmark.py", "capture.py", "evidence.py", "seeds.py")
        },
        "source_sha256": evidence,
    }
    (HERE / "results.json").write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    print(f"Captured {len(rows)} experiment and {len(archive)} broad-sweep evaluations")
    print("Held-out safety:", final["safety"])


if __name__ == "__main__":
    main()
