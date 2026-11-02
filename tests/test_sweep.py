"""Sweep contracts and small repeated trials against the actual compiled engine."""

import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

from wsorb import flight_engine, sweep
from wsorb.flight import coast_command
from wsorb.sweep import _configuration, load_trial, run_sweep
from wsorb.sweep_analysis import write_report

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = json.loads((ROOT / "simulation/scenarios/phobos_escape.json").read_text())


def coast_factory(settings):
    return coast_command


def broken_factory(settings):
    def broken(observation):
        raise ValueError("Deliberate policy failure")

    return broken


def configuration():
    return {
        "schema_version": 1,
        "preset": "far_side_l2",
        "seeds": [11, 22],
        "debris_counts": [0, 2],
        "duration_s": 0.1,
        "step_s": 0.05,
        "algorithms": {"coast": {}},
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("seeds", [11, 11]),
        ("seeds", [True]),
        ("debris_counts", [-1]),
        ("step_s", 0.01),
        ("step_s", 1.05),
        ("step_s", 30),
        ("step_s", True),
        ("duration_s", 0.075),
        ("preset", "typo"),
        ("algorithms", {"missing": {}}),
        ("schema_version", 2),
        ("repetitions", True),
        ("repetitions", 0),
        ("repetitions", 11),
        ("repetitions", 2.0),
    ],
)
def test_invalid_design_rejected_before_creating_output(tmp_path, monkeypatch, field, value):
    def unexpected_launch():
        pytest.fail("Invalid sweep must not launch an isolated server")

    monkeypatch.setattr(flight_engine, "isolated_simulation", unexpected_launch)
    config = configuration()
    config[field] = value
    output = tmp_path / "invalid"
    with pytest.raises(ValueError):
        run_sweep(config, {"coast": coast_factory}, output)
    assert not output.exists()


def test_official_comparison_config_is_a_valid_competition_sweep():
    config = json.loads((ROOT / "examples/official_comparison.json").read_text())
    policies = {name: coast_factory for name in config["algorithms"]}
    validated = _configuration(config, SCENARIO, policies)
    assert validated["preset"] == "competition_reference"
    assert validated["settings"]["debris"]["distribution"] == {"kind": "official_cloud"}
    assert validated["settings"]["debris"]["count"] == 500
    assert validated["repetitions"] == 2
    assert set(validated["algorithms"]) == {
        "official_basic_beeline",
        "ali_official_profile",
        "baseline",
        "body-axis",
        "basic_pd_radar",
    }


def test_ali_official_profile_alias_is_registered_for_published_configs():
    from runpy import run_path

    policies = run_path(str(ROOT / "examples/flight_sweep.py"))["POLICIES"]
    assert "ali_official_profile" in policies
    assert "wso_official_profile" in policies
    assert policies["ali_official_profile"] is policies["wso_official_profile"]


@pytest.mark.parametrize("override", ["root", "node"])
def test_unsupported_launcher_override_rejected_before_output(tmp_path, monkeypatch, override):
    def unexpected_launch():
        pytest.fail("Unsupported override must not launch an isolated server")

    monkeypatch.setattr(flight_engine, "isolated_simulation", unexpected_launch)
    arguments = {override: tmp_path if override == "root" else "/nonexistent/wsorb-node"}
    output = tmp_path / "invalid"
    with pytest.raises(ValueError, match=f"{override} overrides are unsupported"):
        run_sweep(configuration(), {"coast": coast_factory}, output, **arguments)
    assert not output.exists()


def test_one_second_manual_step_is_supported():
    config = configuration()
    config.update(duration_s=2.0, step_s=1.0)
    assert _configuration(config, SCENARIO, {"coast": coast_factory})["step_s"] == 1.0


def test_repetitions_default_to_one_and_are_bounded():
    config = configuration()
    assert _configuration(config, SCENARIO, {"coast": coast_factory})["repetitions"] == 1
    for repetitions in (2, 10):
        config["repetitions"] = repetitions
        assert (
            _configuration(config, SCENARIO, {"coast": coast_factory})["repetitions"] == repetitions
        )


def test_pure_schedule_keeps_fresh_repetitions_consecutive_and_snapshots_peer_hashes(
    tmp_path, monkeypatch
):
    # Orchestration only: never launch physics, produce observations or claim a physical result.
    monkeypatch.setattr(sweep.HeadlessSimulation, "__enter__", lambda self: self)
    attempts = []

    def no_flight(engine, run, factory, trace):
        attempts.append((run["debris_count"], run["seed"], run["algorithm"], run["repetition"]))
        run["outcome"] = "policy_error"

    monkeypatch.setattr(sweep, "_trial", no_flight)
    config = configuration()
    config.update(repetitions=2, algorithms={"one": {}, "two": {}})
    output = tmp_path / "schedule-only"
    summaries = run_sweep(config, {"one": coast_factory, "two": coast_factory}, output)
    expected = [
        (count, seed, algorithm, repetition)
        for count in (0, 2)
        for seed in (11, 22)
        for algorithm in ("one", "two")
        for repetition in (1, 2)
    ]
    assert attempts == expected
    assert [row["repetition"] for row in summaries] == [row[-1] for row in expected]
    assert all(row["sample_count"] == 0 for row in summaries)
    evidence = json.loads((output / "python.json").read_text())
    hashes = evidence["reviewed_example_hashes"]
    assert "examples/bui/mvp/guidance.py" in hashes
    assert "examples/tom/avoidance.py" in hashes
    assert all(len(value) == 64 for value in hashes.values())


def test_settings_override_keeps_fixed_region_and_does_not_mutate_input():
    config = configuration()
    config["settings"] = {"ship": {"mass_kg": 4000}, "debris": {"radius_max_m": 2}}
    before = deepcopy(config)
    parsed = _configuration(config, SCENARIO, {"coast": coast_factory})
    assert config == before
    assert parsed["settings"]["ship"]["mass_kg"] == 4000
    assert (
        parsed["settings"]["debris"]["shell_outer_m"]
        == SCENARIO["experiment_settings"]["debris"]["shell_outer_m"]
    )


@pytest.fixture
def real_engine():
    if not shutil.which("node") or not (ROOT / "worker/public/sim/wsorb_bevy_bg.wasm").is_file():
        pytest.skip("Run just build to test the actual engine")


def test_seed_count_cartesian_runs_reproduce_and_save_public_trace(tmp_path, real_engine):
    config = configuration()
    first = run_sweep(config, {"coast": coast_factory}, tmp_path / "first")
    second = run_sweep(config, {"coast": coast_factory}, tmp_path / "second")
    assert len(first) == len(second) == 4
    assert {(row["seed"], row["debris_count"]) for row in first} == {
        (11, 0),
        (22, 0),
        (11, 2),
        (22, 2),
    }
    for left, right in zip(first, second, strict=True):
        assert left["outcome"] == right["outcome"] == "timeout"
        assert left["elapsed_sim_s"] == pytest.approx(0.1)
        a = load_trial(tmp_path / "first" / left["trial_directory"])
        b = load_trial(tmp_path / "second" / right["trial_directory"])
        assert [sample["observation"] for sample in a["samples"]] == [
            sample["observation"] for sample in b["samples"]
        ]
        assert a["engine"]["wasm_sha256"]
        assert a["engine"]["backend"] == "native_headless"
        assert a["engine"]["control_mode"] == "owned_native_API_manual_step"
        assert a["execution_kind"] == a["engine"]["execution_kind"] == "owned_native_manual_step"
        assert len(a["samples"]) == 3
        assert a["samples"][-1]["accel_command"] == [0, 0, 0]
        assert "assets" not in a["samples"][0]["observation"]
    groups = json.loads((tmp_path / "first/summary.json").read_text())
    assert len(groups) == 2
    assert all(group["runs"] == 2 for group in groups)
    report = write_report(tmp_path / "first")
    assert "Paired trace" in report.read_text()
    assert "arrival-speed limit" in report.read_text()
    assert write_report(tmp_path / "first", trial="trial-00001").is_file()
    with pytest.raises(ValueError, match="no saved observations"):
        write_report(tmp_path / "first", trial="missing")
    assert (tmp_path / "first/python.json").is_file()
    with pytest.raises(FileExistsError):
        run_sweep(config, {"coast": coast_factory}, tmp_path / "first")


def test_policy_errors_preserve_trace_and_do_not_prevent_other_trials(tmp_path, real_engine):
    config = configuration()
    config.update(seeds=[11], debris_counts=[0], algorithms={"broken": {}, "coast": {}})
    rows = run_sweep(
        config, {"broken": broken_factory, "coast": coast_factory}, tmp_path / "errors"
    )
    assert [row["outcome"] for row in rows] == ["policy_error", "timeout"]
    assert rows[0]["sample_count"] == 1
    assert "Deliberate policy failure" in rows[0]["error"]
    assert (tmp_path / "errors/trial-00000/trace.jsonl").read_text()
    assert write_report(tmp_path / "errors").is_file()


def test_factories_reset_memory_for_each_paired_trial_and_traces_flush(tmp_path, real_engine):
    config = configuration()
    config.update(debris_counts=[0], algorithms={"first": {"tag": 1}, "second": {"tag": 2}})
    output = tmp_path / "fresh"
    instances = []

    def factory(settings):
        instance = {"tag": settings["tag"], "times": []}
        instances.append(instance)
        settings["tag"] = "only the factory copy changes"

        def policy(observation):
            trace = sorted(output.glob("trial-*/trace.jsonl"))[-1]
            saved = [json.loads(line) for line in trace.read_text().splitlines()]
            assert len(saved) == len(instance["times"]) + 1
            assert saved[-1]["observation"]["time_s"] == observation["time_s"]
            instance["times"].append(observation["time_s"])
            observation["mission"]["status"] = "only the policy copy changes"
            return coast_command(observation)

        return policy

    rows = run_sweep(config, {"first": factory, "second": factory}, output)
    assert len(instances) == len(rows) == 4
    assert [instance["tag"] for instance in instances] == [1, 2, 1, 2]
    assert all(instance["times"] == [0, 0.05] for instance in instances)
    assert all(row["outcome"] == "timeout" for row in rows)
    assert {(row["algorithm"], row["seed"]) for row in rows} == {
        (algorithm, seed) for algorithm in ("first", "second") for seed in (11, 22)
    }
    assert config["algorithms"] == {"first": {"tag": 1}, "second": {"tag": 2}}
    for row in rows:
        trial = load_trial(output / row["trial_directory"])
        assert trial["algorithm_settings"] == config["algorithms"][row["algorithm"]]
        assert trial["samples"][-1]["observation"]["time_s"] == pytest.approx(0.1)
