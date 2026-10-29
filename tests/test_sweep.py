"""Sweep contracts and small repeated trials against the actual compiled engine."""

import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

from wsorb import flight_engine
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
