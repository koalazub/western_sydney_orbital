"""Portable evidence and analysis; no practice simulator or SDK imports."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

SCHEMA = "wsorb.official_stp.benchmark.v1"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_events(path):
    """A killed process may leave one incomplete final JSONL line."""
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            records.append({"kind": "truncated_record", "original": line})
    return records


def order_attempts(config, attempts, execution_order=None):
    order = execution_order or config.get("execution_order", "coverage_first")
    if order not in {"coverage_first", "paired"}:
        raise ValueError(f"Unknown execution order: {order}")
    indices = {controller["id"]: i for i, controller in enumerate(config["controllers"])}

    def key(attempt):
        block, repetition = attempt["block"], attempt["repetition"]
        rotated = (indices[attempt["controller"]["id"]] - block) % len(indices)
        return (
            (repetition, block, rotated)
            if order == "coverage_first"
            else (block, rotated, repetition)
        )

    return sorted(attempts, key=key)


def repetition_count(config):
    # Older frozen campaigns predate this setting and always scheduled two passes.
    count = config.get("repetitions", 2)
    if type(count) is not int or count < 1:
        raise ValueError("repetitions must be a positive integer")
    return count


def schedule(config, mode):
    repetitions = repetition_count(config)
    controllers = config["controllers"]
    ids = [c["id"] for c in controllers]
    if not ids or len(ids) != len(set(ids)) or config["reference"] not in ids:
        raise ValueError("Controller IDs must be unique and include the reference")
    if mode == "baseline":
        blocks = [None] * 5
    elif mode == "seeded":
        blocks = config["seeds"]
        count = config.get("seed_count", 20)
        if type(count) is not int or count < 1 or len(blocks) != count:
            raise ValueError("Seeded campaigns require seed_count predeclared seeds")
        if len(set(blocks)) != len(blocks):
            raise ValueError("Seeded campaign seeds must be distinct")
        if any(type(seed) is not int or not 0 <= seed < 2**32 for seed in blocks):
            raise ValueError("Seeds must be uint32 integers")
    elif mode == "pilot":
        blocks = [None]
        controllers = [c for c in controllers if c["id"] == config["reference"]]
    else:
        raise ValueError(f"Unknown mode: {mode}")
    attempts = []
    for block, seed in enumerate(blocks):
        offset = block % len(controllers)
        for controller in controllers[offset:] + controllers[:offset]:
            for repetition in range(repetitions):
                attempts.append(
                    dict(block=block, seed=seed, repetition=repetition, controller=controller)
                )
    return order_attempts(config, attempts)


def score_components(receipt):
    """Independent transcription of pinned STU4 Scoring.py, not a replacement scorer."""
    inputs = receipt.get("inputs")
    if not inputs or receipt.get("outcome") not in {"success", "failure"}:
        return None
    required = ["damageTaken_J", "maxDamage_J", "fuelUsed_kg", "nominalFuel_kg"]
    if any(inputs.get(k) is None for k in required):
        return None
    damage = 2 * inputs["damageTaken_J"] / inputs["maxDamage_J"]
    fuel = max(inputs["fuelUsed_kg"] / inputs["nominalFuel_kg"], 0)
    if receipt["outcome"] == "success":
        if inputs.get("time_s") is None or inputs.get("nominalTime_s") is None:
            return None
        travel = inputs["nominalTime_s"] / inputs["time_s"] if inputs["time_s"] else 0
        distance = 0
    else:
        if not inputs.get("initial_distanceToShip") or inputs.get("distanceToShip") is None:
            return None
        travel = 0
        distance = inputs["distanceToShip"] / inputs["initial_distanceToShip"]
    return dict(
        offset_points=50,
        time_points=100 * travel,
        distance_points=-100 * distance,
        damage_points=-100 * damage,
        fuel_points=-100 * fuel,
        calculated_score=50 + 100 * (travel - distance - damage - fuel),
    )


def distribution(values):
    values = sorted(v for v in values if isinstance(v, (float, int)) and math.isfinite(v))
    if not values:
        return dict(n=0, min=None, median=None, max=None, mean=None)
    return dict(
        n=len(values),
        min=values[0],
        median=statistics.median(values),
        max=values[-1],
        mean=statistics.mean(values),
        values=values,
    )


def incidents(events, receipt, thresholds):
    """Threshold indicators are observed patterns, never causal diagnoses."""
    result = []
    samples = [e for e in events if e.get("kind") == "telemetry"]

    def add(kind, event, facts, missing=()):
        index = event.get("seq")
        before = [s["seq"] for s in samples if s.get("seq", 0) < (index or 0)][-20:]
        after = [s["seq"] for s in samples if s.get("seq", 0) > (index or 0)][:20]
        result.append(
            dict(
                kind=kind,
                observed=facts,
                suspected_cause=None,
                event_seq=index,
                preceding_telemetry_seq=before,
                subsequent_telemetry_seq=after,
                evidence_file="events.jsonl",
                terminal_receipt="receipt.json",
                recovery="Inspect subsequent records; recovery is not inferred",
                missing_data=list(missing),
            )
        )

    previous = None
    window = []
    issued = set()
    commands = []
    for event in events:
        kind = event.get("kind")
        if kind in {"exception", "hook_error", "sensor_error", "truncated_record"}:
            add(kind, event, event)
        if kind == "command":
            duration = event.get("decision_duration_s")
            if duration is not None and duration > thresholds["decision_overrun_s"]:
                add("decision_overrun", event, dict(duration_s=duration))
            command = event.get("linear_body_m_s2")
            if command is not None:
                commands.append((event, command))
                commands = commands[-20:]
                reversals = sum(
                    sum(a * b for a, b in zip(left[1], right[1], strict=True)) < -0.01
                    for left, right in zip(commands, commands[1:], strict=False)
                )
                if reversals >= 12 and "oscillation" not in issued:
                    add(
                        "oscillation_indicator",
                        event,
                        dict(reversals_in_20_commands=reversals),
                        ["Cause and whether reversals are necessary avoidance"],
                    )
                    issued.add("oscillation")
        if kind != "telemetry":
            continue
        if previous and event.get("damage_J") is not None:
            old = previous.get("damage_J")
            if old is not None and event["damage_J"] > old:
                add(
                    "damage_increase",
                    event,
                    dict(
                        before_J=old,
                        after_J=event["damage_J"],
                        interval_sim_s=[previous.get("sim_s"), event.get("sim_s")],
                    ),
                    ["Exact contact time", "Contact identity", "Native contact event"],
                )
        age = event.get("sensor_age_s")
        if age is not None and age > thresholds["sensor_stale_s"] and "stale" not in issued:
            add("stale_sensor", event, dict(max_observed_sensor_age_s=age))
            issued.add("stale")
        if event.get("sim_s") is not None and event.get("range_m") is not None:
            window.append(event)
            while len(window) > 1 and event["sim_s"] - window[1]["sim_s"] >= 30:
                window.pop(0)
            if (
                window
                and event["sim_s"] - window[0]["sim_s"] >= 30
                and window[0]["range_m"] - event["range_m"] < 1
                and "progress" not in issued
            ):
                add(
                    "lack_of_progress_indicator",
                    event,
                    dict(window_s=30, range_change_m=event["range_m"] - window[0]["range_m"]),
                )
                issued.add("progress")
            closest = min(s["range_m"] for s in window)
            if closest < 100 and event["range_m"] > closest + 50 and "overshoot" not in issued:
                add(
                    "overshoot_indicator",
                    event,
                    dict(recent_min_range_m=closest, range_m=event["range_m"]),
                )
                issued.add("overshoot")
        previous = event
    if receipt.get("outcome") != "success":
        add(receipt.get("outcome", "unfinished"), events[-1] if events else {}, receipt)
    return result


def csv_table(path, rows):
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def delivery_summary(events):
    """Separate writer calls, native actuator readback, effects and observed motion."""
    writes = [e for e in events if e.get("kind") == "command_written"]
    applied = [e for e in events if e.get("kind") == "actuator_applied"]

    def nonzero(vector):
        return vector is not None and any(abs(v) > 1e-9 for v in vector)

    def same(left, right):
        return (
            left is not None
            and right is not None
            and len(left) == len(right)
            and all(abs(a - b) <= 1e-8 for a, b in zip(left, right, strict=True))
        )

    matched = []
    for event in applied:
        received = event.get("received", {})
        vector = received.get("linear_body_m_s2")
        if not nonzero(vector):
            continue
        read_time = received.get("linear_read_monotonic_s", -math.inf)
        writer = next(
            (
                w
                for w in reversed(writes)
                if w.get("monotonic_s", math.inf) <= read_time
                and same(w.get("linear_body_m_s2"), vector)
            ),
            None,
        )
        if writer is not None:
            matched.append(
                dict(
                    writer_seq=writer.get("seq"),
                    actuator_seq=event.get("seq"),
                    read_after_write_s=read_time - writer["monotonic_s"],
                )
            )
    positions = [e["position_resident_m"] for e in applied if e.get("position_resident_m")]
    velocities = [e["velocity_resident_m_s"] for e in applied if e.get("velocity_resident_m_s")]

    def change(values):
        if len(values) < 2:
            return None
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(values[-1], values[0], strict=True)))

    displacement = change(positions)
    impulses = sum(nonzero(e.get("linear_impulse_body_kg_m_s")) for e in applied)
    return dict(
        command_writes=len(writes),
        native_actuator_samples=len(applied),
        matched_nonzero_readbacks=len(matched),
        matches=matched,
        native_nonzero_impulse_samples=impulses,
        command_delivery_verified=bool(matched) and impulses > 0,
        observed_displacement_m=displacement,
        observed_velocity_change_m_s=change(velocities),
        simulator_motion_observed=(displacement is not None and displacement > 0.1),
        note="Motion also includes gravity and contacts; exact thrust attribution is not inferred.",
    )


def commit_match(repo, revision, directory, snapshot, files):
    """Read committed blobs only; never infer source identity from checkout HEAD alone."""
    if not revision or len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        return "unavailable"
    try:
        relative = Path(directory)
        if relative.is_absolute():
            relative = relative.resolve().relative_to(repo.resolve())
        prefix = relative.as_posix().rstrip("/") + "/"

        def read(*args):
            return subprocess.run(
                ["git", "-C", str(repo), *args],
                check=True,
                capture_output=True,
                timeout=10,
            ).stdout

        paths = read("ls-tree", "-r", "--name-only", "-z", revision, "--", prefix)
        committed = {
            p.removeprefix(prefix)
            for p in paths.decode().split("\0")
            if p
            and "__pycache__" not in Path(p).parts
            and Path(p).name != "controller_log.txt"
            and not Path(p).name.startswith("cam_")
        }
        if committed != set(files):
            return "modified"
        status = "exact"
        for name in files:
            original = read("show", f"{revision}:{prefix}{name}")
            frozen = (snapshot / name).read_bytes()
            if original == frozen:
                continue
            if b"\0" not in original + frozen and original.replace(
                b"\r\n", b"\n"
            ) == frozen.replace(b"\r\n", b"\n"):
                status = "line_endings_only"
            else:
                return "modified"
        return status
    except (OSError, ValueError, subprocess.SubprocessError):
        return "unavailable"


def controller_versions(root, campaign):
    root = Path(root)
    provenance_path = root / "provenance.json"
    provenance = json.loads(provenance_path.read_text()) if provenance_path.exists() else {}
    labels_path = root / "dataset_labels.json"
    labels = json.loads(labels_path.read_text()) if labels_path.exists() else {}
    result = {}
    for controller in campaign["config"]["controllers"]:
        identity = controller["id"]
        prefix = f"controllers/{identity}/"
        files = {
            name.removeprefix(prefix): value
            for name, value in provenance.get("snapshot_hashes", {}).items()
            if name.startswith(prefix)
        }
        bundle = (
            hashlib.sha256(
                json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if files
            else None
        )
        metadata = labels.get(identity, controller)
        if identity in labels and (not bundle or metadata.get("source_bundle_sha256") != bundle):
            raise ValueError(f"Dataset label does not match frozen source bundle: {identity}")
        author = metadata.get("author", "Unknown author")
        method = metadata.get("method", "Unspecified method")
        snapshot = root / "snapshot" / "controllers" / identity
        intact = bool(files) and all(
            (snapshot / name).is_file() and digest(snapshot / name) == value
            for name, value in files.items()
        )
        checkout = provenance.get("repository_revision")
        match = (
            commit_match(
                Path(__file__).resolve().parents[2],
                checkout,
                controller["directory"],
                snapshot,
                files,
            )
            if intact
            else "unavailable"
        )
        source_commit = checkout if match in {"exact", "line_endings_only"} else None
        version = source_commit[:12] if source_commit else "unverified commit"
        label = f"{author} — {method} — {version}"
        if bundle:
            label += f" — source {bundle[:12]}"
        result[identity] = dict(
            controller_label=label,
            controller_author=author,
            controller_method=method,
            controller_source_commit=source_commit,
            controller_checkout_commit=checkout,
            controller_commit_match=match,
            controller_source_sha256=bundle,
        )
    return result


def analyze(root):
    root = Path(root)
    campaign = json.loads((root / "campaign.json").read_text(encoding="utf-8"))
    versions = controller_versions(root, campaign)
    selection_path = root / "seed_selection.json"
    selection = (
        json.loads(selection_path.read_text(encoding="utf-8")) if selection_path.exists() else {}
    )
    rows, timelines = [], []
    for attempt in campaign["attempts"]:
        folder = root / attempt["run_id"]
        path = folder / "receipt.json"
        receipt = json.loads(path.read_text()) if path.exists() else dict(outcome="not_started")
        events = read_events(folder / "events.jsonl")
        delivery = delivery_summary(events)
        parts = score_components(receipt)
        official = receipt.get("official_score")
        discrepancy = None
        if parts is not None and official is not None:
            discrepancy = abs(parts["calculated_score"] - official) > 1e-8
        issue = incidents(events, receipt, campaign["config"]["thresholds"])
        if folder.exists():
            write_json(folder / "incidents.json", issue)
            write_json(folder / "delivery.json", delivery)
        timelines.extend(dict(run_id=attempt["run_id"], **item) for item in issue)
        inputs = receipt.get("inputs") or {}
        samples = [e for e in events if e.get("kind") == "telemetry"]
        last = samples[-1] if samples else {}
        row = dict(
            run_id=attempt["run_id"],
            controller=attempt["controller"]["id"],
            **versions[attempt["controller"]["id"]],
            mode=campaign["mode"],
            block=attempt["block"],
            seed=attempt["seed"],
            seed_bank_id=selection.get("bank_id"),
            seed_set_sha256=selection.get("selected_seeds_sha256"),
            repetition=attempt["repetition"],
            outcome=receipt["outcome"],
            termination_reason=receipt.get("termination_reason"),
            official_score=official,
            score_discrepancy=discrepancy,
            fuel_kg=inputs.get("fuelUsed_kg", last.get("fuel_kg")),
            damage_J=inputs.get("damageTaken_J", last.get("damage_J")),
            scorer_time_s=inputs.get("time_s", receipt.get("scorer_time_s")),
            arrival_time_s=inputs.get("time_s") if receipt["outcome"] == "success" else None,
            range_m=inputs.get("distanceToShip", receipt.get("range_m", last.get("range_m"))),
            damage_increase_observed=any(i["kind"] == "damage_increase" for i in issue),
            native_collision_count=None,
            telemetry_samples=len(samples),
            wall_s=receipt.get("wall_s"),
            bytes=receipt.get("evidence_bytes"),
            measurements_source="scoring_event" if inputs else "partial_last_telemetry",
            command_delivery_verified=delivery["command_delivery_verified"],
            simulator_motion_observed=delivery["simulator_motion_observed"],
            **(parts or {}),
        )
        rows.append(row)
    groups = defaultdict(list)
    for row in rows:
        groups[row["controller"]].append(row)
    summaries = {}
    for name, group in groups.items():
        counts = dict(Counter(r["outcome"] for r in group))
        summaries[name] = dict(
            **versions[name],
            scheduled=len(group),
            outcomes=counts,
            attempted=sum(r["outcome"] != "not_started" for r in group),
            success_rate=counts.get("success", 0) / len(group),
            scored_coverage=sum(r["official_score"] is not None for r in group) / len(group),
            damage_increase_incidence=sum(r["damage_increase_observed"] for r in group)
            / len(group),
            native_collision_incidence=None,
            damage_observation_coverage=sum(r["telemetry_samples"] > 0 for r in group) / len(group),
            distributions={
                key: distribution(r[key] for r in group)
                for key in (
                    "official_score",
                    "fuel_kg",
                    "damage_J",
                    "scorer_time_s",
                    "arrival_time_s",
                    "range_m",
                )
            },
        )
    reference = campaign["config"]["reference"]
    reference_rows = {(r["block"], r["repetition"]): r for r in groups[reference]}
    comparisons = []
    for row in rows:
        if row["controller"] == reference:
            continue
        ref = reference_rows.get((row["block"], row["repetition"]))
        if ref:
            both = row["official_score"] is not None and ref["official_score"] is not None
            comparisons.append(
                dict(
                    controller=row["controller"],
                    controller_label=row["controller_label"],
                    reference=reference,
                    reference_label=versions[reference]["controller_label"],
                    block=row["block"],
                    seed=row["seed"],
                    repetition=row["repetition"],
                    run_id=row["run_id"],
                    reference_run_id=ref["run_id"],
                    both_scored=both,
                    score_delta=row["official_score"] - ref["official_score"] if both else None,
                    outcome=row["outcome"],
                    reference_outcome=ref["outcome"],
                )
            )
    # A seed is one scenario. Require every configured repetition before reducing it.
    repetitions = repetition_count(campaign["config"])
    matched_blocks = defaultdict(list)
    for comparison in comparisons:
        matched_blocks[(comparison["controller"], comparison["block"])].append(comparison)
    seed_comparisons = []
    for (name, block), group in matched_blocks.items():
        complete = {g["repetition"] for g in group} == set(range(repetitions)) and all(
            g["both_scored"] for g in group
        )
        seed_comparisons.append(
            dict(
                controller=name,
                controller_label=versions[name]["controller_label"],
                reference_label=versions[reference]["controller_label"],
                block=block,
                seed=group[0]["seed"],
                complete_pair=complete,
                mean_score_delta=statistics.mean(g["score_delta"] for g in group)
                if complete
                else None,
            )
        )
    csv_table(root / "attempts.csv", rows)
    csv_table(root / "matched.csv", comparisons)
    csv_table(root / "scenario_comparisons.csv", seed_comparisons)
    write_json(root / "summary.json", summaries)
    write_json(root / "controller_versions.json", versions)
    write_json(root / "failure_timelines.json", timelines)
    completed = [r for r in rows if r["wall_s"] is not None]
    estimate = dict(
        pilot_attempts=len(completed),
        mean_wall_s=statistics.mean(r["wall_s"] for r in completed) if completed else None,
        mean_bytes=statistics.mean(r["bytes"] for r in completed if r["bytes"] is not None)
        if any(r["bytes"] is not None for r in completed)
        else None,
        warning="Pilot cost only; failures and early stops do not estimate a completed flight.",
    )
    if estimate["mean_wall_s"] is not None:
        n = len(campaign["config"]["controllers"])
        estimate["baseline_wall_hours"] = n * 5 * repetitions * estimate["mean_wall_s"] / 3600
        seed_count = campaign["config"].get("seed_count", 20)
        estimate["seeded_scenarios"] = seed_count
        estimate["seeded_wall_hours"] = (
            n * seed_count * repetitions * estimate["mean_wall_s"] / 3600
        )
        if estimate["mean_bytes"] is not None:
            estimate["baseline_bytes"] = n * 5 * repetitions * estimate["mean_bytes"]
            estimate["seeded_bytes"] = n * seed_count * repetitions * estimate["mean_bytes"]
    write_json(root / "cost_estimate.json", estimate)
    execution_order = campaign.get(
        "execution_order", campaign["config"].get("execution_order", "paired")
    )
    lines = [
        "# Official STP campaign",
        "",
        f"Mode: {campaign['mode']}. Reference: {reference}.",
        f"Execution order: {execution_order}.",
        f"Flights per controller per block: {repetitions}.",
        "",
        "| Controller | Outcomes | Success / scheduled | Scored coverage |",
        "| --- | --- | --- | --- |",
    ]
    for name, item in summaries.items():
        lines.append(
            f"| {item['controller_label']} ({name}) | {item['outcomes']} | "
            f"{item['success_rate']:.1%} | {item['scored_coverage']:.1%} |"
        )
    lines += [
        "",
        "Raw score and component distributions: summary.json and attempts.csv.",
        "Matched flights: matched.csv; scenario score differences averaged over all "
        "configured repetitions: scenario_comparisons.csv. "
        "Missing scores are never replaced with zero.",
        "Distinct seeds are scenarios; repeats measure repeatability. Baseline blocks "
        "share one saved field and are not independent scenarios.",
        "Damage increases are observed intervals, not identified collisions. Native "
        "collision incidence is unavailable without native contact events.",
        "Timelines link event sequences to raw records and the terminal receipt.",
        "Cost estimates are in cost_estimate.json; review the pilot outcome first.",
    ]
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summaries
