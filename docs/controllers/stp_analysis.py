"""Read-only, streaming analysis of completed native STP campaign attempts."""

from __future__ import annotations

import bisect
import csv
import hashlib
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path

CACHE_VERSION = 2
COMPLETE = {"success", "failure", "simulation_limit"}


def percentile(values, fraction):
    values = sorted(v for v in values if v is not None and math.isfinite(v))
    if not values:
        return None
    index = (len(values) - 1) * fraction
    lo, hi = math.floor(index), math.ceil(index)
    return values[lo] + (values[hi] - values[lo]) * (index - lo)


def mean(values):
    values = [v for v in values if v is not None]
    return statistics.mean(values) if values else None


def vector_key(vector):
    return tuple(vector) if vector is not None else None


def nonzero(vector):
    return vector is not None and any(abs(value) > 1e-9 for value in vector)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def scan_events(path):
    """Keep scalar histories, not full radar arrays; memory stays bounded per flight."""
    counts = Counter()
    decisions, write_times, ages = [], [], []
    writes = {axis: defaultdict(list) for axis in ("linear", "angular")}
    reads = {axis: [] for axis in writes}
    telemetry, first, last = [], None, None
    closest, peak_speed, peak_damage = None, 0.0, 0.0
    damage_intervals, nonzero_impulses, malformed = 0, 0, 0
    last_bucket, last_damage = None, None
    connections = set()
    native_ids = set()
    if not path.exists():
        return {"telemetry": [], "event_counts": {}, "telemetry_samples": 0}
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            kind = event.get("kind")
            counts[kind] += 1
            if kind == "connected":
                connections.add(event.get("role"))
                native_ids.add(event.get("native_run_uuid"))
            elif kind == "command":
                duration = event.get("decision_duration_s")
                if duration is not None:
                    decisions.append(duration)
            elif kind == "command_written":
                stamp = event.get("monotonic_s")
                if stamp is not None:
                    write_times.append(stamp)
                    for axis, key in (
                        ("linear", "linear_body_m_s2"),
                        ("angular", "angular_body_rad_s2"),
                    ):
                        writes[axis][vector_key(event.get(key))].append(stamp)
            elif kind == "actuator_applied":
                received = event.get("received", {})
                for axis, key in (
                    ("linear", "linear_body_m_s2"),
                    ("angular", "angular_body_rad_s2"),
                ):
                    vector = received.get(key)
                    stamp = received.get(f"{axis}_read_monotonic_s")
                    if nonzero(vector) and stamp is not None:
                        reads[axis].append((vector_key(vector), stamp))
                nonzero_impulses += nonzero(event.get("linear_impulse_body_kg_m_s"))
            elif kind == "telemetry":
                sim = event.get("sim_s")
                if sim is None:
                    continue
                sample = {
                    key: event.get(key)
                    for key in (
                        "sim_s",
                        "monotonic_s",
                        "range_m",
                        "relative_speed_m_s",
                        "fuel_kg",
                        "damage_J",
                        "position_phobos_m",
                    )
                }
                first = sample if first is None else first
                last = sample
                distance = sample["range_m"]
                if distance is not None and (closest is None or distance < closest[0]):
                    closest = (distance, sim)
                peak_speed = max(peak_speed, sample["relative_speed_m_s"] or 0)
                damage = sample["damage_J"]
                if damage is not None:
                    peak_damage = max(peak_damage, damage)
                    if last_damage is not None and damage > last_damage + 1e-6:
                        damage_intervals += 1
                    last_damage = damage
                sensor_age = event.get("sensor_age_s")
                if isinstance(sensor_age, (int, float)) and sensor_age >= 0:
                    ages.append(sensor_age)
                bucket = int(sim / 2)
                if bucket != last_bucket:
                    telemetry.append(sample)
                    last_bucket = bucket
    if last and (not telemetry or last != telemetry[-1]):
        telemetry.append(last)
    matches = {}
    for axis in writes:
        for stamps in writes[axis].values():
            stamps.sort()
        matches[axis] = sum(
            bisect.bisect_right(writes[axis].get(vector, []), stamp) > 0
            for vector, stamp in reads[axis]
        )
    write_times.sort()
    intervals = [b - a for a, b in zip(write_times, write_times[1:], strict=False)]
    wall_span = last["monotonic_s"] - first["monotonic_s"] if last and first else 0
    return dict(
        telemetry=telemetry,
        event_counts=dict(counts),
        telemetry_samples=counts["telemetry"],
        last_sim_s=last["sim_s"] if last else None,
        last_range_m=last["range_m"] if last else None,
        last_fuel_kg=last["fuel_kg"] if last else None,
        last_damage_J=last["damage_J"] if last else None,
        closest_range_m=closest[0] if closest else None,
        closest_time_s=closest[1] if closest else None,
        peak_speed_m_s=peak_speed,
        peak_damage_J=peak_damage,
        damage_increase_intervals=damage_intervals,
        command_writes=len(write_times),
        actuator_samples=counts["actuator_applied"],
        linear_nonzero_reads=len(reads["linear"]),
        angular_nonzero_reads=len(reads["angular"]),
        linear_matched_reads=matches["linear"],
        angular_matched_reads=matches["angular"],
        nonzero_impulse_samples=nonzero_impulses,
        delivery_verified=matches["linear"] > 0 and nonzero_impulses > 0,
        decision_p50_s=percentile(decisions, 0.5),
        decision_p95_s=percentile(decisions, 0.95),
        command_interval_p50_s=percentile(intervals, 0.5),
        command_interval_p95_s=percentile(intervals, 0.95),
        radar_worst_age_p95_s=percentile(ages, 0.95),
        observed_clock_ratio=(last["sim_s"] - first["sim_s"]) / wall_span
        if wall_span > 0
        else None,
        connected_roles=sorted(role for role in connections if role),
        native_mission_ids=sorted(value for value in native_ids if value),
        malformed_event_lines=malformed,
    )


def fingerprint(folder):
    records = [CACHE_VERSION]
    for name in ("events.jsonl", "receipt.json", "manifest.json"):
        path = folder / name
        if path.exists():
            stat = path.stat()
            records.append((name, stat.st_size, stat.st_mtime_ns))
    return hashlib.sha256(json.dumps(records).encode()).hexdigest()


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def load_campaign(root):
    root = Path(root).expanduser().resolve()
    campaign = read_json(root / "campaign.json")
    if campaign["mode"] != "seeded":
        raise ValueError("Select the main seeded campaign; pilot/baseline datasets are excluded")
    cache = root / "harry_analysis"
    cache.mkdir(exist_ok=True)
    rows, histories, excluded = [], {}, Counter()
    for attempt in campaign["attempts"]:
        folder = root / attempt["run_id"]
        receipt_path = folder / "receipt.json"
        receipt = read_json(receipt_path) if receipt_path.exists() else {}
        outcome = receipt.get("outcome", "not_started")
        if outcome not in COMPLETE:
            excluded[outcome] += 1
            continue
        signature = fingerprint(folder)
        cached_path = cache / f"{attempt['run_id']}.json"
        saved = read_json(cached_path) if cached_path.exists() else {}
        if saved.get("signature") == signature:
            metrics = saved["metrics"]
        else:
            print(
                f"Extracting {attempt['controller']['id']} seed {attempt['seed']} "
                f"repeat {attempt['repetition']}",
                flush=True,
            )
            metrics = scan_events(folder / "events.jsonl")
            write_json(cached_path, dict(signature=signature, metrics=metrics))
        history = metrics["telemetry"]
        row = {key: value for key, value in metrics.items() if key != "telemetry"}
        inputs = receipt.get("inputs") or {}
        score = receipt.get("official_score")
        parts = {}
        if score is not None and inputs:
            parts = dict(
                offset_points=50,
                time_points=100 * inputs["nominalTime_s"] / inputs["time_s"]
                if outcome == "success"
                else 0,
                fuel_points=-100 * max(inputs["fuelUsed_kg"] / inputs["nominalFuel_kg"], 0),
                damage_points=-200 * inputs["damageTaken_J"] / inputs["maxDamage_J"],
                distance_points=0
                if outcome == "success"
                else -100 * inputs["distanceToShip"] / inputs["initial_distanceToShip"],
            )
        manifest = (
            read_json(folder / "manifest.json") if (folder / "manifest.json").exists() else {}
        )
        radar = next((c for c in manifest.get("changes", []) if c["role"] == "radar"), {})
        row.update(
            run_id=attempt["run_id"],
            controller=attempt["controller"]["id"],
            seed=attempt["seed"],
            repetition=attempt["repetition"],
            outcome=outcome,
            official_score=score,
            arrival_time_s=inputs.get("time_s") if outcome == "success" else None,
            fuel_kg=inputs.get("fuelUsed_kg", metrics.get("last_fuel_kg")),
            damage_J=inputs.get("damageTaken_J", metrics.get("last_damage_J")),
            wall_s=receipt.get("wall_s"),
            cleanup=receipt.get("cleanup"),
            score_error=(sum(parts.values()) - score) if parts else None,
            radar_source_sha256=radar.get("source_sha256"),
            **parts,
        )
        rows.append(row)
        histories[row["run_id"]] = history
    # A common first-pass cohort excludes an entire seed if any controller lacks a completed flight.
    controllers = [c["id"] for c in campaign["config"]["controllers"]]
    first = [row for row in rows if row["repetition"] == 0]
    seed_owners = defaultdict(set)
    for row in first:
        seed_owners[row["seed"]].add(row["controller"])
    common_seeds = [
        seed for seed in campaign["config"]["seeds"] if seed_owners[seed] == set(controllers)
    ]
    common = [row for row in first if row["seed"] in common_seeds]
    write_csv(cache / "completed_attempts.csv", rows)
    write_csv(cache / "common_first_pass.csv", common)
    return dict(
        root=str(root),
        campaign=campaign,
        rows=rows,
        histories=histories,
        controllers=controllers,
        excluded=dict(excluded),
        common=common,
        common_seeds=common_seeds,
        cache=str(cache),
    )


def write_csv(path, rows):
    keys = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def wilson(successes, total):
    if not total:
        return None, None
    z = 1.959963984540054
    rate = successes / total
    divisor = 1 + z * z / total
    midpoint = (rate + z * z / (2 * total)) / divisor
    half = z * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total)) / divisor
    return midpoint - half, midpoint + half


def overview(rows, controllers):
    result = []
    for controller in controllers:
        group = [r for r in rows if r["controller"] == controller]
        successes = sum(r["outcome"] == "success" for r in group)
        lo, hi = wilson(successes, len(group))
        scored = [r["official_score"] for r in group if r["official_score"] is not None]
        result.append(
            dict(
                controller=controller,
                completed=len(group),
                successes=successes,
                timeouts=sum(r["outcome"] == "simulation_limit" for r in group),
                arrival_rate=successes / len(group) if group else None,
                arrival_ci_low=lo,
                arrival_ci_high=hi,
                scored=len(scored),
                score_mean=mean(scored),
                score_median=percentile(scored, 0.5),
                score_p10=percentile(scored, 0.1),
                score_p90=percentile(scored, 0.9),
                arrival_mean_s=mean(r["arrival_time_s"] for r in group),
                delivery_verified=sum(r.get("delivery_verified", False) for r in group),
                damage_observed=sum((r.get("peak_damage_J") or 0) > 0 for r in group),
            )
        )
    return result


def paired_scores(rows, reference="tom"):
    reference_rows = {(r["seed"], r["repetition"]): r for r in rows if r["controller"] == reference}
    pairs = []
    for row in rows:
        if row["controller"] == reference:
            continue
        ref = reference_rows.get((row["seed"], row["repetition"]))
        if ref and row["official_score"] is not None and ref["official_score"] is not None:
            pairs.append(
                dict(
                    controller=row["controller"],
                    seed=row["seed"],
                    repetition=row["repetition"],
                    score_delta=row["official_score"] - ref["official_score"],
                )
            )
    return pairs


def seed_sensitivity(rows, controllers):
    """Descriptive first-pass spread and balanced score interactions, without imputation."""
    groups = defaultdict(dict)
    for row in rows:
        if row["repetition"] != 0 or row["controller"] not in controllers:
            continue
        group = groups[row["seed"]]
        if row["controller"] in group:
            raise ValueError("Seed sensitivity requires one first-pass flight per controller/seed")
        group[row["controller"]] = row
    common = {s: g for s, g in groups.items() if set(g) == set(controllers)}
    balanced = {
        s: g for s, g in common.items() if all(r["official_score"] is not None for r in g.values())
    }
    controller_means = {
        c: mean(g[c]["official_score"] for g in balanced.values()) for c in controllers
    }
    grand_mean = mean(controller_means.values())
    effects = []
    for seed, group in balanced.items():
        seed_mean = mean(r["official_score"] for r in group.values())
        for controller in controllers:
            score = group[controller]["official_score"]
            effects.append(
                dict(
                    controller=controller,
                    seed=seed,
                    official_score=score,
                    seed_mean_score=seed_mean,
                    interaction_points=score
                    - controller_means[controller]
                    - seed_mean
                    + grand_mean,
                )
            )
    summaries = []
    for controller in controllers:
        flights = [g[controller] for g in common.values()]
        scores = [r["official_score"] for r in flights if r["official_score"] is not None]
        arrivals = [r["arrival_time_s"] for r in flights if r["arrival_time_s"] is not None]
        interactions = [r["interaction_points"] for r in effects if r["controller"] == controller]
        summaries.append(
            dict(
                controller=controller,
                common_seeds=len(flights),
                successes=sum(r["outcome"] == "success" for r in flights),
                timeouts=sum(r["outcome"] == "simulation_limit" for r in flights),
                scored_seeds=len(scores),
                score_sd=statistics.stdev(scores) if len(scores) > 1 else None,
                score_iqr=percentile(scores, 0.75) - percentile(scores, 0.25) if scores else None,
                score_min=min(scores) if scores else None,
                score_max=max(scores) if scores else None,
                arrival_sd_s=statistics.stdev(arrivals) if len(arrivals) > 1 else None,
                jointly_scored_seeds=len(balanced),
                interaction_sd=statistics.stdev(interactions) if len(interactions) > 1 else None,
            )
        )
    return summaries, effects


def repeatability(rows):
    groups = defaultdict(dict)
    for row in rows:
        groups[(row["controller"], row["seed"])][row["repetition"]] = row
    result = []
    for (controller, seed), repeats in groups.items():
        if set(repeats) != {0, 1}:
            continue
        first, second = repeats[0], repeats[1]
        both_scored = all(r["official_score"] is not None for r in (first, second))
        result.append(
            dict(
                controller=controller,
                seed=seed,
                outcome_changed=first["outcome"] != second["outcome"],
                first_outcome=first["outcome"],
                second_outcome=second["outcome"],
                score_delta=second["official_score"] - first["official_score"]
                if both_scored
                else None,
            )
        )
    return result


def paired_summary(pairs):
    groups = defaultdict(list)
    for pair in pairs:
        groups[pair["controller"]].append(pair["score_delta"])
    result = []
    for controller, values in groups.items():
        rng = random.Random(100)
        bootstrap = [statistics.mean(rng.choices(values, k=len(values))) for _ in range(3000)]
        result.append(
            dict(
                controller=controller,
                common_scored_seeds=len(values),
                wins=sum(v > 0 for v in values),
                mean_delta=mean(values),
                median_delta=percentile(values, 0.5),
                bootstrap_low=percentile(bootstrap, 0.025),
                bootstrap_high=percentile(bootstrap, 0.975),
            )
        )
    return result
