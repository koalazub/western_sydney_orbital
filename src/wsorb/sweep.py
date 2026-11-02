"""Sequential, fixed-step experiments using the same compiled practice physics.

Policies receive public observations only. Every trial gets a fresh policy and
engine, with the same seeds reused across algorithms and debris counts.
"""

from __future__ import annotations

import csv
import hashlib
import inspect
import json
import math
import platform
import sys
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from .experiment import Decision
from .experiment_io import json_safe
from .flight_engine import EXECUTION_KIND, HeadlessSimulation
from .local_api import (
    FLIGHT_PRESETS,
    MAX_DEBRIS_COUNT,
    MAX_STEP_SECONDS,
    validate_experiment_settings,
)
from .sweep_analysis import analyze_run, summarize_runs

Policy = Callable[[dict], tuple[Any, Any] | Decision]
PolicyFactory = Callable[[dict], Policy]


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(json_safe(value), indent=2, allow_nan=False) + "\n")


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(
            {
                key: json.dumps(value) if isinstance(value, (dict, list)) else value
                for key, value in row.items()
            }
            for row in rows
        )


def _source(factory: PolicyFactory) -> dict:
    """Best-effort function evidence, not a complete dependency/environment lock."""
    try:
        source = inspect.getsource(factory)
    except (OSError, TypeError):
        source = None
    try:
        module_source = inspect.getsource(sys.modules[factory.__module__])
    except (OSError, TypeError, KeyError, AttributeError):
        module_source = None
    return {
        "module": getattr(factory, "__module__", None),
        "qualname": getattr(factory, "__qualname__", None),
        "source": source,
        "source_sha256": hashlib.sha256(source.encode()).hexdigest() if source else None,
        "module_source": module_source,
        "module_source_sha256": (
            hashlib.sha256(module_source.encode()).hexdigest() if module_source else None
        ),
        "limitation": (
            "Factory/module sources only; imported dependencies are not "
            "a full environment snapshot."
        ),
    }


def _configuration(config: Mapping, scenario: dict, policies: Mapping) -> dict:
    if (
        not isinstance(config, Mapping)
        or type(config.get("schema_version")) is not int
        or config.get("schema_version") != 1
    ):
        raise ValueError("Sweep configuration requires schema_version 1")
    allowed = {
        "schema_version",
        "seeds",
        "debris_counts",
        "preset",
        "duration_s",
        "step_s",
        "settings",
        "algorithms",
        "repetitions",
    }
    if set(config) - allowed:
        raise ValueError(f"Unknown sweep fields: {sorted(set(config) - allowed)}")
    result = deepcopy(dict(config))
    repetitions = result.get("repetitions", 1)
    if type(repetitions) is not int or not 1 <= repetitions <= 10:
        raise ValueError("repetitions must be an integer between 1 and 10")
    result["repetitions"] = repetitions
    for key, maximum in (("seeds", 0xFFFFFFFF), ("debris_counts", MAX_DEBRIS_COUNT)):
        values = result.get(key)
        if (
            not isinstance(values, (list, tuple))
            or not values
            or any(type(value) is not int or not 0 <= value <= maximum for value in values)
            or len(set(values)) != len(values)
        ):
            raise ValueError(f"{key} must contain unique integers between 0 and {maximum}")
        result[key] = list(values)
    if result.get("preset") not in FLIGHT_PRESETS:
        raise ValueError(f"Choose preset from {', '.join(FLIGHT_PRESETS)}")
    fixed = scenario["integration"]["fixed_step_s"]
    for key in ("duration_s", "step_s"):
        value = result.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be a positive finite number")
        if not math.isclose(value / fixed, round(value / fixed), abs_tol=1e-8):
            raise ValueError(f"{key} must be a multiple of the engine's {fixed}s physics step")
    if result["step_s"] > min(MAX_STEP_SECONDS, scenario["integration"]["maximum_command_step_s"]):
        raise ValueError(f"step_s must be at most {MAX_STEP_SECONDS}s for native manual steps")
    if not math.isclose(
        result["duration_s"] / result["step_s"],
        round(result["duration_s"] / result["step_s"]),
        abs_tol=1e-8,
    ):
        raise ValueError("duration_s must be a multiple of step_s")
    algorithms = result.get("algorithms")
    if not isinstance(algorithms, Mapping) or not algorithms:
        raise ValueError("algorithms must map policy names to settings")
    for name, settings in algorithms.items():
        if (
            name not in policies
            or not callable(policies[name])
            or not isinstance(settings, Mapping)
        ):
            raise ValueError(f"Unknown policy or invalid settings for {name!r}")
    settings = deepcopy(scenario["experiment_settings"])
    overrides = result.get("settings", {})
    if not isinstance(overrides, Mapping) or set(overrides) - {"ship", "debris"}:
        raise ValueError("settings may override ship and debris fields")
    for family, changes in overrides.items():
        if not isinstance(changes, Mapping):
            raise ValueError(f"settings.{family} must be a mapping")
        settings[family].update(changes)
    result["settings"] = validate_experiment_settings(settings)
    # Validate every population before starting any expensive or irreversible run.
    for count in result["debris_counts"]:
        trial = deepcopy(result["settings"])
        trial["debris"]["count"] = count
        validate_experiment_settings(trial)
    json.dumps(result, allow_nan=False)
    return result


def _trial(engine, run: dict, factory: PolicyFactory, trace) -> None:
    def record(observation, decision=None, policy_wall_s=None):
        sample = {
            "observation": observation,
            "accel_command": decision.accel.tolist() if decision else None,
            "angular_command": decision.angular_accel.tolist() if decision else None,
            "policy_wall_s": policy_wall_s,
        }
        run["samples"].append(sample)
        trace.write(json.dumps(json_safe(sample), allow_nan=False) + "\n")
        trace.flush()

    phase = "engine"
    try:
        observation = engine.configure_flight(run["settings"], run["preset"], run["seed"])
        record(observation)
        phase = "policy"
        policy = factory(deepcopy(run["algorithm_settings"]))
        if not callable(policy):
            raise TypeError("Policy factory must return a callable")
        for _ in range(round(run["duration_s"] / run["step_s"])):
            status = observation["mission"]["status"]
            if status != "running":
                run["outcome"] = {"success": "success", "failed_damage": "failed_damage"}[status]
                break
            phase = "policy"
            started = time.perf_counter()
            command = policy(deepcopy(observation))
            decision = command if isinstance(command, Decision) else Decision(*command)
            elapsed = time.perf_counter() - started
            phase = "engine"
            previous_time = observation["time_s"]
            observation = engine.step(decision.accel, decision.angular_accel, run["step_s"])
            if observation["time_s"] <= previous_time:
                raise RuntimeError("Physics clock did not advance")
            record(observation, decision, elapsed)
        else:
            run["outcome"] = {
                "running": "timeout",
                "success": "success",
                "failed_damage": "failed_damage",
            }[observation["mission"]["status"]]
    except Exception as error:
        run["outcome"] = f"{phase}_error"
        run["error"] = f"{type(error).__name__}: {error}"


def run_sweep(
    config: Mapping,
    policies: Mapping[str, PolicyFactory],
    output: str | Path,
    *,
    root: str | Path | None = None,
    node: str = "node",
    progress: Callable[[dict], None] | None = None,
) -> list[dict]:
    """Run paired seeds at each debris count; return flat per-trial summaries.

    ``policies`` maps names to factories accepting algorithm settings. Each
    factory returns ``policy(observation) -> (accel, angular_accel)`` or Decision.
    Use a factory/closure for stateful algorithms so memory resets every trial.
    Optional ``repetitions`` defaults to one. Repeats get fresh contexts and stay
    consecutive within each count/seed/algorithm, rather than adding seed draws.
    The output directory must be new. Traces flush after each sample, and errors
    are retained as experiment outcomes rather than silently dropped.
    """
    preflight = HeadlessSimulation(root=root, node=node)
    root = preflight.root
    scenario = preflight.scenario
    config = _configuration(config, scenario, policies)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    _write_json(output / "configuration.json", config)
    _write_json(
        output / "policies.json", {name: _source(policies[name]) for name in config["algorithms"]}
    )
    _write_json(
        output / "python.json",
        {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "uv_lock_sha256": hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest(),
            "reviewed_example_hashes": {
                str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for family in ("bui/mvp", "tom")
                for path in sorted((root / "examples" / family).glob("*.py"))
            },
            "analytics_definitions": (
                json.loads((root / "simulation/scenarios/flight_analytics.json").read_text())
                if (root / "simulation/scenarios/flight_analytics.json").is_file()
                else None
            ),
            "data_source_hashes": {
                str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (
                    root / "simulation/scenarios/phobos_escape.json",
                    root / "simulation/scenarios/flight_analytics.json",
                    root / "src/wsorb/controllers/catalog.json",
                )
                if path.is_file()
            },
            "package_sources": {
                str(path.relative_to(root)): {
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "source": path.read_text(),
                }
                for path in sorted((root / "src/wsorb").rglob("*.py"))
            },
        },
    )
    summaries = []
    # Keep only one trial's observations in memory. Each trial has its own process.
    for count in config["debris_counts"]:
        for seed in config["seeds"]:
            for algorithm, algorithm_settings in config["algorithms"].items():
                for repetition in range(1, config["repetitions"] + 1):
                    trial_dir = output / f"trial-{len(summaries):05d}"
                    trial_dir.mkdir()
                    settings = deepcopy(config["settings"])
                    settings["debris"]["count"] = count
                    run = {
                        "schema_version": 1,
                        "execution_kind": EXECUTION_KIND,
                        "algorithm": algorithm,
                        "algorithm_settings": algorithm_settings,
                        "repetition": repetition,
                        "seed": seed,
                        "debris_count": count,
                        "preset": config["preset"],
                        "duration_s": config["duration_s"],
                        "step_s": config["step_s"],
                        "settings": settings,
                        "outcome": "engine_error",
                        "error": None,
                        "samples": [],
                        "engine": None,
                        "success_criterion": (
                            "Local engine arrival-radius entry; no arrival-speed gate"
                        ),
                    }
                    with (trial_dir / "trace.jsonl").open("w") as trace:
                        try:
                            with HeadlessSimulation(root=root, node=node) as engine:
                                run["engine"] = engine.metadata
                                _trial(engine, run, policies[algorithm], trace)
                        except KeyboardInterrupt:
                            run["outcome"] = "interrupted"
                            _write_json(
                                trial_dir / "run.json",
                                {k: v for k, v in run.items() if k != "samples"},
                            )
                            raise
                        except Exception as error:
                            run["outcome"] = "engine_error"
                            run["error"] = f"{type(error).__name__}: {error}"
                    _write_json(
                        trial_dir / "run.json", {k: v for k, v in run.items() if k != "samples"}
                    )
                    summary = analyze_run(run)
                    summary["trial_directory"] = trial_dir.name
                    summaries.append(summary)
                    _write_csv(output / "runs.csv", summaries)
                    # Analysis consumes observations; grouping only needs flat summaries.
                    groups = summarize_runs(summaries)
                    _write_json(output / "summary.json", groups)
                    _write_csv(output / "summary.csv", groups)
                    if progress:
                        progress(summary)
    return summaries


def load_trial(path: str | Path) -> dict:
    """Load a saved trial for notebook analysis without connecting to a simulator."""
    path = Path(path)
    run = json.loads((path / "run.json").read_text())
    run["samples"] = [json.loads(line) for line in (path / "trace.jsonl").read_text().splitlines()]
    return run
