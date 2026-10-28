"""Run the checked-in probe against real prebuilt WASM, without GPU certification."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "worker/scripts/platform-probe.mjs"
ASSETS = ROOT / "worker/public/sim"


@pytest.fixture(scope="module")
def node():
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("Use the existing devenv Node.js environment to run the platform probe")
    completed = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, check=True, timeout=10
    )
    version = completed.stdout.strip()
    parts = tuple(int(part) for part in version.removeprefix("v").split("."))
    if parts[0] != 26 or parts < (26, 12, 1):
        pytest.skip("The platform probe requires the project's Node.js ^26.12.1 runtime")
    return executable, version


def _reject_constant(value):
    raise AssertionError(f"Probe emitted non-finite JSON: {value}")


def _run(node, script=PROBE, *args, cwd=ROOT):
    return subprocess.run(
        [node[0], str(script), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=40,
    )


def _report(completed):
    result = json.loads(completed.stdout, parse_constant=_reject_constant)
    assert result["schema"] == "wsorb.platform_probe.v1"
    assert result["browser_gpu_verified"] is False
    assert "CPU/WASM only" in result["measurement"]
    return result


@pytest.fixture(scope="module")
def actual_probe(node, tmp_path_factory):
    if not (ASSETS / "wsorb_bevy_bg.wasm").is_file():
        pytest.skip("Provide the complete existing prebuilt game to probe the real WASM engine")
    # A different cwd must not select assets or a live owner's connection file.
    completed = _run(node, cwd=tmp_path_factory.mktemp("probe-cwd"))
    result = _report(completed)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert result["ok"] is True
    return result


def test_actual_probe_reports_this_process_and_verified_shipped_wasm(actual_probe, node):
    result = actual_probe
    assert result["host"]["platform"] == sys.platform
    assert result["host"]["node"] == node[1]
    assert result["host"]["arch"] in {"arm64", "x64", "ppc64", "s390x", "riscv64"}
    wasm = (ASSETS / "wsorb_bevy_bg.wasm").read_bytes()
    manifest = json.loads((ASSETS / "manifest.json").read_text())
    assert result["engine_build"] == {
        "bytes": len(wasm),
        "sha256": hashlib.sha256(wasm).hexdigest(),
        "wasm_matches_manifest": True,
    }
    assert manifest["bytes"] == len(wasm)
    assert manifest["sha256"] == result["engine_build"]["sha256"]


def test_actual_checked_play_ticks_and_pause_preserve_native_clock_semantics(actual_probe):
    result = actual_probe
    initial, started = result["initial"], result["started"]
    queued, running, paused = result["play_queued"], result["running"], result["paused"]
    assert initial["physics"]["phase"] == "unstarted"
    assert initial["physics"]["started"] is initial["physics"]["owned"] is False
    assert started["physics"]["phase"] == "idle"
    assert started["physics"]["wall_elapsed_s"] == 0
    assert queued["ack"] == {"ok": True}
    assert queued["runtime"]["running"] is False
    assert queued["observation"]["time_s"] == initial["observation"]["time_s"]
    assert running["runtime"]["running"] is running["physics"]["running"] is True
    assert running["observation"]["time_s"] > initial["observation"]["time_s"]
    assert running["physics"]["time_s"] == running["observation"]["time_s"]
    assert result["pause_queued"]["ack"] == {"ok": True}
    assert result["pause_queued"]["runtime"]["running"] is True
    assert paused["runtime"]["running"] is paused["physics"]["running"] is False
    assert paused["observation"]["time_s"] == running["observation"]["time_s"]
    assert paused["physics"]["wall_elapsed_s"] > running["physics"]["wall_elapsed_s"]
    assert result["clock"]["source"] == "performance.now"
    assert len(result["clock"]["tick_deltas_s"]) >= 3
    assert all(0 < delta <= 1 for delta in result["clock"]["tick_deltas_s"])
    assert sum(result["clock"]["tick_deltas_s"]) == pytest.approx(
        result["clock"]["measured_elapsed_s"], abs=1e-9
    )
    assert paused["physics"]["wall_elapsed_s"] == pytest.approx(
        result["clock"]["measured_elapsed_s"], abs=1e-6
    )


def test_actual_probe_uses_stock_public_observations_without_graphics(actual_probe):
    for phase in ("initial", "started", "play_queued", "running", "pause_queued", "paused"):
        observation = actual_probe[phase]["observation"]
        assert observation["geometry"]["debris_count"] == 500
        assert observation["seed"] == 100
        assert observation["flight_preset"] == "far_side_l2"
        assert observation["manual_interventions"] == 0
        assert observation["shuttle"] is not None and observation["target"] is not None
        assert 0 < observation["radar_rows"] <= 500
        assert 0 < observation["radar_backup_rows"] <= 500
        assert observation["navigation_sample_time_s"] <= observation["time_s"]
    renderer = actual_probe["renderer"]
    assert renderer["started"] is renderer["ready"] is False
    assert renderer["rendered_frames"] == 0
    assert renderer["adapter"] is None
    assert actual_probe["cameras"]["configured"] is False
    assert actual_probe["cameras"]["views"] == []
    assert {item["camera"] for item in actual_probe["capture_rejections"]} == {"top", "bottom"}
    assert all(item["code"] == "sensor_no_sample" for item in actual_probe["capture_rejections"])


def test_windows_drive_and_unc_path_math_is_explicitly_not_os_emulation(actual_probe):
    paths = actual_probe["windows_path_math"]
    assert "not Windows execution" in paths["measurement"]
    assert paths["drive_url"] == (
        "file:///C:/Games/WSO%20%23100%25%20%C3%BC/worker/public/sim/wsorb_bevy.js"
    )
    assert paths["unc_url"] == (
        "file://game-host/share/WSO%20%23100%25%20%C3%BC/worker/public/sim/wsorb_bevy.js"
    )
    assert paths["contained_file"] is True
    for key in (
        "sibling_directory",
        "parent_directory",
        "other_drive",
        "other_share",
        "drive_relative_is_absolute",
    ):
        assert paths[key] is False


@pytest.fixture
def script_without_assets(tmp_path):
    script = tmp_path / "Portable WSO #100% ü/worker/scripts/platform-probe.mjs"
    script.parent.mkdir(parents=True)
    shutil.copyfile(PROBE, script)
    return script


def test_probe_help_and_import_do_not_start_an_engine_or_require_assets(
    node, script_without_assets
):
    completed = _run(node, script_without_assets, "--help")
    assert completed.returncode == 0, completed.stderr
    assert "not browser/GPU certification" in completed.stdout
    # Import the checked-in module with an empty stdin program, no inline JS.
    imported = subprocess.run(
        [node[0], "--import", script_without_assets.as_uri(), "-"],
        input="",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert imported.returncode == 0, imported.stderr
    assert imported.stdout == imported.stderr == ""


def test_actual_glue_import_handles_spaces_unicode_percent_and_hash(node, script_without_assets):
    if not (ASSETS / "wsorb_bevy_bg.wasm").is_file():
        pytest.skip("Provide the complete prebuilt game to check its actual glue import")
    worker = script_without_assets.parent.parent
    assets = worker / "public/sim"
    assets.mkdir(parents=True)
    shutil.copyfile(ROOT / "worker/package.json", worker / "package.json")
    for name in ("manifest.json", "wsorb_bevy.js", "wsorb_bevy_bg.wasm"):
        shutil.copyfile(ASSETS / name, assets / name)
    completed = _run(node, script_without_assets, cwd=worker.parent)
    result = _report(completed)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert result["ok"] is True
    assert result["host"]["platform"] == sys.platform
    assert (
        result["engine_build"]["sha256"]
        == hashlib.sha256((ASSETS / "wsorb_bevy_bg.wasm").read_bytes()).hexdigest()
    )


def test_missing_assets_fail_with_json_instead_of_claiming_compatibility(
    node, script_without_assets
):
    completed = _run(node, script_without_assets)
    result = _report(completed)
    assert completed.returncode != 0
    assert result["ok"] is False
    assert result["error"]["stage"] == "assets"
    assert result["error"]["code"] == "ENOENT"


def test_platform_override_is_rejected_not_simulated(node, script_without_assets):
    completed = _run(node, script_without_assets, "--platform", "win32")
    result = _report(completed)
    assert completed.returncode != 0
    assert result["ok"] is False
    assert result["host"]["platform"] == sys.platform
    assert "overrides are not supported" in result["error"]["message"]


@pytest.mark.parametrize("change", ["size", "sha256", "invalid_manifest"])
def test_real_artifact_integrity_failure_is_fatal_before_instantiation(
    node, script_without_assets, change
):
    if not (ASSETS / "wsorb_bevy_bg.wasm").is_file():
        pytest.skip("Provide the existing prebuilt WASM to check its real manifest")
    assets = script_without_assets.parent.parent / "public/sim"
    assets.mkdir(parents=True)
    shutil.copyfile(ASSETS / "wsorb_bevy_bg.wasm", assets / "wsorb_bevy_bg.wasm")
    manifest = json.loads((ASSETS / "manifest.json").read_text())
    if change == "size":
        manifest["bytes"] += 1
        expected = "size does not match"
    elif change == "sha256":
        manifest["sha256"] = "0" * 64
        expected = "SHA-256 does not match"
    else:
        manifest["bytes"] = True
        expected = "Invalid engine manifest"
    (assets / "manifest.json").write_text(json.dumps(manifest))
    completed = _run(node, script_without_assets)
    result = _report(completed)
    assert completed.returncode != 0
    assert result["ok"] is False
    assert result["error"]["stage"] == "assets"
    assert expected in result["error"]["message"]
