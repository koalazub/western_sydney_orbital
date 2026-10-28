"""Opt-in checkpoint attempts through authenticated local or hosted public APIs.

``python -m wsorb.checkpoint --isolated`` inspects one fresh, paused real WASM
world. Add ``--run`` to claim control and fly. ``--policy compare --run`` uses
two separate owned servers, never a reset. An explicit ``--connection-file``
selects one existing local flight. ``--room-file`` selects a hosted room's live
public RPC instead. Existing flights are never configured, reset or tuned here.

The numeric controller is the same STPCheckpointController used by the licensed
example. This transport is not spaceteams, SDK emulation or physics-parity
evidence. The official ControllerSystem entry point remains separate.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import weakref
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from . import diagnostics
from .local_api import (
    CommandExpiredError,
    LocalApiError,
    LocalBusyError,
    LocalSimulation,
    StaleContextError,
    control_context,
    controller_fence,
    validate_experiment_settings,
)
from .room_api import RoomSimulation

if TYPE_CHECKING:
    from .control_contract import ControlSample
    from .stp_policy import STPPolicyConfig

ROOT = Path(__file__).resolve().parents[2]
LEASE_SIM_S = 0.75
MAX_OUTPUT_BYTES = 64 * 1024
_KNOWN_UNAPPLIED = (LocalBusyError, StaleContextError, CommandExpiredError)
_COHORT_FIELDS = ("trial_revision", "tuning_revision", "manual_interventions", "seed")
_OWNED_CLIENTS: weakref.WeakSet[LocalSimulation] = weakref.WeakSet()
_LOCAL_SOURCE = "local_public_snapshot"
_ROOM_SOURCE = "hosted_room_public_snapshot"
_WORLD_FIELDS = (
    "flight_preset",
    "launch_site_direction",
    "experiment_settings",
    "ship_tuning",
    "debris_flow",
    "frame",
    "rotation_convention",
    "radar_clock",
    "max_accel_mps2",
    "max_angular_accel_rad_s2",
)


class CheckpointError(RuntimeError):
    """Fixed, credential-free diagnostics, never raw transport exception text."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CheckpointOptions:
    """Validated invocation data. Construction performs no I/O or actuation."""

    isolated: bool = False
    connection_file: Path | None = None
    room_file: Path | None = None
    run: bool = False
    policy: str = "baseline"
    duration_s: float = 1800.0
    wall_budget_s: float = 3600.0
    cadence_hz: float = 10.0
    playback_rate: int | None = None
    progress: bool = False
    diagnostics: bool = False
    wait_for_owner_s: float = 0.0

    def __post_init__(self) -> None:
        for name in ("isolated", "run", "progress", "diagnostics"):
            if type(getattr(self, name)) is not bool:
                raise CheckpointError(
                    "invalid_options", "Consent and scope flags must be booleans."
                )
        for name in ("connection_file", "room_file"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, (str, Path)) or not str(value):
                    raise CheckpointError(
                        "invalid_options", "An explicit private file is required."
                    )
                object.__setattr__(self, name, Path(value))
        if sum((self.isolated, self.connection_file is not None, self.room_file is not None)) != 1:
            raise CheckpointError(
                "invalid_options",
                "Select exactly one of --isolated, --connection-file or --room-file.",
            )
        if self.policy not in ("baseline", "body-axis", "compare"):
            raise CheckpointError("invalid_options", "Unknown checkpoint policy.")
        if self.policy == "compare" and (not self.isolated or not self.run):
            raise CheckpointError("invalid_options", "Comparison requires --isolated and --run.")
        for name, low, high in (
            ("duration_s", 0.0, 1800.0),
            ("wall_budget_s", 0.0, 3600.0),
            ("cadence_hz", 10.0, 50.0),
        ):
            value = _number(getattr(self, name))
            if value is None or not low <= value <= high or value <= 0:
                raise CheckpointError("invalid_options", f"Invalid bounded {name}.")
            object.__setattr__(self, name, float(value))
        rate = self.playback_rate
        if rate is None:
            rate = 1
        if type(rate) is not int or rate not in (1, 5) or (not self.isolated and rate != 1):
            raise CheckpointError(
                "invalid_options", "Playback rate 5 is allowed only on an owned isolated server."
            )
        object.__setattr__(self, "playback_rate", rate)
        wait = _number(self.wait_for_owner_s)
        if wait is None or not 0 <= wait <= 3600 or (self.isolated and wait > 0):
            raise CheckpointError(
                "invalid_options", "Owner waiting requires an external scope and 0..3600 seconds."
            )
        object.__setattr__(self, "wait_for_owner_s", float(wait))


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse otherwise echoes unknown argv and arbitrary credential-like values.
        raise CheckpointError("invalid_arguments", "Invalid checkpoint arguments. Use --help.")


def parse_args(argv: Sequence[str] | None = None) -> CheckpointOptions:
    """Parse the module/CLI arguments without connecting or starting a process."""
    parser = _Parser(
        prog="python -m wsorb.checkpoint",
        description="Inspect by default; --run explicitly consents to native API actuation.",
        allow_abbrev=False,
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--isolated", action="store_true", help="own a fresh headless WASM server")
    scope.add_argument(
        "--connection-file", type=Path, help="inspect or fly one existing local trial"
    )
    scope.add_argument("--room-file", type=Path, help="inspect or fly one connected hosted room")
    parser.add_argument("--run", action="store_true", help="explicitly claim control and fly")
    parser.add_argument(
        "--policy", choices=("baseline", "body-axis", "compare"), default="baseline"
    )
    parser.add_argument(
        "--duration-s", type=float, default=1800, help="simulation budget, at most 1800"
    )
    parser.add_argument(
        "--wall-budget-s", type=float, default=3600, help="total wall budget, max 3600"
    )
    parser.add_argument(
        "--cadence-hz", type=float, default=10, help="wall-clock decisions, 10..50 Hz"
    )
    parser.add_argument(
        "--playback-rate", type=int, choices=(1, 5), help="default 1; rate 5 requires owned scope"
    )
    parser.add_argument("--progress", action="store_true", help="bounded progress JSON on stderr")
    parser.add_argument(
        "--diagnostics", action="store_true", help="opt in to bounded, allowlisted JSONL on stderr"
    )
    parser.add_argument(
        "--wait-for-owner-s",
        type=float,
        default=0,
        help="read-only wait for an external engine, within the total wall budget; at most 3600 s",
    )
    return CheckpointOptions(**vars(parser.parse_args(argv)))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _summary(value: Any, depth: int = 0) -> Any:
    """Bound detached public numeric reports; never serialize clients or exceptions."""
    if value is None or type(value) is bool:
        return value
    if isinstance(value, (int, float)):
        return value if _number(value) is not None else None
    if isinstance(value, str):
        return value[:256]
    if depth >= 8:
        return {"summary_truncated": True}
    if isinstance(value, Mapping):
        items = list(value.items())
        result = {
            key: _summary(item, depth + 1)
            for key, item in items[:48]
            if isinstance(key, str) and len(key) <= 100
        }
        if len(items) > 48:
            result["summary_truncated"] = True
        return result
    if isinstance(value, (list, tuple)):
        result = [_summary(item, depth + 1) for item in value[:8]]
        if len(value) > 8:
            result.append({"omitted_items": len(value) - 8})
        return result
    return None


def _error(error: BaseException, phase: str) -> dict[str, str]:
    if isinstance(error, CheckpointError):
        return {"code": error.code, "message": str(error)}
    if isinstance(error, KeyboardInterrupt):
        return {"code": "interrupted", "message": "Interrupted; no actuation was retried."}
    return {
        "code": f"{phase}_failed",
        "message": f"{phase} failed. Exception text omitted; no unknown actuation was retried.",
    }


def _observation(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    observation = snapshot.get("observation")
    if not isinstance(observation, Mapping):
        raise CheckpointError("invalid_snapshot", "Actual public observation is unavailable.")
    return observation


def world_metadata(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the actual cohort/setup metadata separate from portable control math."""
    observation = _observation(snapshot)
    context = control_context(snapshot)
    geometry = observation.get("geometry", {})
    return {
        **{name: context[name] for name in _COHORT_FIELDS},
        **{name: observation.get(name) for name in _WORLD_FIELDS},
        "debris_count": geometry.get("debris_count"),
        "arrival_radius_m": geometry.get("arrival_radius_m"),
        "engine_build": snapshot.get("engine_build"),
    }


def native_measurement(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Read mission measurements, never calculate a substitute final range/score."""
    observation = _observation(snapshot)
    mission = observation.get("mission")
    mission = mission if isinstance(mission, Mapping) else {}
    return {
        "time_s": _number(observation.get("time_s")),
        "navigation_sample_time_s": _number(observation.get("navigation_sample_time_s")),
        "status": mission.get("status"),
        "phase": mission.get("phase"),
        "range_m": _number(mission.get("target_distance_m")),
        "relative_speed_mps": _number(mission.get("relative_speed_mps")),
        "damage_j": _number(mission.get("damage")),
        "fuel_used_kg": _number(mission.get("fuel_used_kg")),
        "score": _number(mission.get("score")),
        "score_label": mission.get("score_label"),
    }


def _stock_world(snapshot: Mapping[str, Any]) -> bool:
    observation = _observation(snapshot)
    try:
        source = json.loads((ROOT / "simulation/scenarios/phobos_escape.json").read_text())
        expected = validate_experiment_settings(source["experiment_settings"])
        actual = validate_experiment_settings(observation["experiment_settings"])
        return (
            source["default_seed"] == observation["seed"] == 100
            and actual == expected
            and actual["debris"]["count"] == observation["geometry"]["debris_count"] == 500
            and observation["flight_preset"] == "far_side_l2"
            and observation["launch_site_direction"] is None
            and observation["ship_tuning"] == {**expected["ship"], "thrust_scale": 1.0}
            and observation["debris_flow"]
            == {
                name: expected["debris"][name]
                for name in ("speed_mps", "speed_spread_mps", "stream_axis", "stream_sign")
            }
            and observation["max_accel_mps2"] == source["lander"]["max_accel_mps2"] == 0.5
            and observation["max_angular_accel_rad_s2"]
            == source["lander"]["max_angular_accel_rad_s2"]
            == 0.05
            and observation["geometry"]["arrival_radius_m"]
            == source["mission"]["arrival_radius_m"]
            == 50.0
            and source["mission"]["impact_threshold_j"] == 209200.0
        )
    except (KeyError, TypeError, ValueError, OSError):
        return False


def native_success(snapshot: Mapping[str, Any]) -> bool:
    """Require the native mission outcome AND its actual stock arrival/damage limits."""
    values = native_measurement(snapshot)
    radius = _observation(snapshot).get("geometry", {}).get("arrival_radius_m")
    return (
        values["status"] == "success"
        and _number(radius) == 50.0
        and values["range_m"] is not None
        and 0 <= values["range_m"] <= radius
        and values["damage_j"] is not None
        and 0 <= values["damage_j"] <= 209200.0
    )


def sample_from_snapshot(
    snapshot: Mapping[str, Any], *, source_kind: str = _LOCAL_SOURCE
) -> ControlSample:
    """Copy genuine public navigation, preserving its native active/frame provenance.

    No missing navigation is filled from other reports. Only mass and inertia
    come from the actual public applied-physics report. Health/lease metadata
    stays here, outside the shared numeric controller.
    """
    from .control_contract import ControlSample, checked_sample, radar_rows, scalar

    if source_kind not in (_LOCAL_SOURCE, _ROOM_SOURCE):
        raise CheckpointError("source_contract", "Unsupported public API source label.")
    observation = _observation(snapshot)
    if (
        observation.get("rotation_convention") != "active_xyzw"
        or observation.get("radar_clock") != "simulation_elapsed_seconds"
    ):
        raise CheckpointError("source_contract", "The native frame/clock contract is unsupported.")
    try:
        health = observation["sensor_health"]
        if type(health["schema_version"]) is not int or health["schema_version"] != 1:
            raise ValueError("Unknown health schema")
        if type(health["revision"]) is not int or health["revision"] < 0:
            raise ValueError("Missing sensor revision")
        units = {unit["id"]: unit for unit in health["units"]}
        for name in ("navigation", "radar_primary"):
            unit = units[name]
            if unit["delivery"] != "ready" or unit["active_faults"]:
                raise ValueError("Native delivery is not ready")
            scalar(unit["sample_time_s"], "native sample time")
            if type(unit["sequence"]) is not int or unit["sequence"] < 0:
                raise ValueError("Native source sequence is missing")
        elapsed = scalar(observation["navigation_sample_time_s"], "navigation time")
        now = scalar(observation["time_s"], "world time")
        if elapsed != units["navigation"]["sample_time_s"] or elapsed > now:
            raise ValueError("Native navigation clocks disagree")
        rows = radar_rows(observation["radar"])
        if not len(rows) or np.any(rows[:, 2, 0] != units["radar_primary"]["sample_time_s"]):
            raise ValueError("Native radar source metadata disagrees")
        ship, target, physics = (
            observation["shuttle"],
            observation["target"],
            observation["applied_physics"],
        )
        if physics["source_time_s"] != elapsed or physics["frame"] != observation["frame"]:
            raise ValueError("Actual mass/inertia report has another source context")
        sample = ControlSample(
            elapsed_time_s=elapsed,
            position_world_m=ship["position"],
            velocity_world_mps=ship["velocity"],
            quaternion_xyzw=ship["rotation"],
            rotation_convention="active_xyzw",
            angular_velocity_world_rad_s=ship["angular_velocity_rad_s"],
            target_position_world_m=target["position"],
            mass_kg=physics["mass_kg"],
            inertia_body_kg_m2=physics["inertia_body_kg_m2"],
            fuel_used_kg=observation["mission"]["fuel_used_kg"],
            damage_j=observation["mission"]["damage"],
            frame=observation["frame"],
            source_kind=source_kind,
        )
        return checked_sample(sample)
    except (KeyError, TypeError, ValueError, OverflowError, np.linalg.LinAlgError) as error:
        raise CheckpointError(
            "native_input_unavailable", "No eligible actual navigation/radar source sample."
        ) from error


def policy_config(policy: str) -> STPPolicyConfig:
    """Build matched shared configs; only body_axis_preference differs."""
    if policy not in ("baseline", "body-axis"):
        raise CheckpointError("invalid_policy", "Select one policy for a single trial.")
    try:
        from .stp_policy import STPCheckpointController, STPPolicyConfig
    except ImportError as error:
        raise CheckpointError(
            "shared_policy_unavailable", "The shared portable checkpoint controller is missing."
        ) from error
    if not callable(STPCheckpointController):
        raise CheckpointError("shared_policy_unavailable", "The shared controller is unavailable.")
    try:
        return STPPolicyConfig(
            enabled=True,
            radar_clock_offset_s=0.0,
            policy="checkpoint_velocity",
            body_axis_preference=policy == "body-axis",
            tracking_diagnostics=True,
            max_speed_mps=8.0,
            checkpoint_radius_m=50.0,
            max_radar_age_s=2.5,
        )
    except (TypeError, ValueError) as error:
        raise CheckpointError(
            "shared_policy_contract",
            "The shared checkpoint configuration contract is incompatible.",
        ) from error


def _temporary_parent() -> Path:
    parent = Path(tempfile.gettempdir())
    # OpenCode pre-creates this approved directory on developer machines.
    preferred = parent / "opencode"
    return preferred if preferred.is_dir() else parent


@contextmanager
def isolated_simulation(*, deadline: float | None = None) -> Iterator[LocalSimulation]:
    """Start exactly one owned real Node/WASM server, with no build or world writes."""
    node = shutil.which("node")
    if node is None:
        raise CheckpointError("node_missing", "Node is required for the real headless WASM server.")
    required = (
        "worker/scripts/local.mjs",
        "worker/public/index.html",
        "worker/public/sim/manifest.json",
        "worker/public/sim/wsorb_bevy.js",
        "worker/public/sim/wsorb_bevy_bg.wasm",
    )
    if any(not (ROOT / path).is_file() for path in required):
        raise CheckpointError(
            "build_missing",
            "Built native WASM assets are missing. Ask the owner to run just build.",
        )
    with tempfile.TemporaryDirectory(prefix="wsorb-checkpoint-", dir=_temporary_parent()) as temp:
        private = Path(temp)
        private.chmod(0o700)
        connection = private / "connection.json"
        log_fd = os.open(private / "server.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(log_fd, "wb") as log:
            process = subprocess.Popen(
                [node, "worker/scripts/local.mjs"],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PORT": "0",
                    "WSORB_LAB": "1",
                    "WSORB_HEADLESS": "1",
                    "WSORB_LOCAL_CONNECTION": str(connection),
                    "WSORB_LAB_TOKEN_FILE": str(private / "unused-notebook.token"),
                    "TMPDIR": str(private),
                },
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
            )
            try:
                startup_deadline = min(deadline or math.inf, time.monotonic() + 30.0)
                while not connection.is_file():
                    if process.poll() is not None:
                        raise CheckpointError(
                            "headless_start_failed",
                            "Real headless startup failed. Check Node and the existing WASM build; "
                            "no browser or substitute engine was started.",
                        )
                    if time.monotonic() >= startup_deadline:
                        raise CheckpointError("startup_timeout", "Owned native startup timed out.")
                    time.sleep(0.025)
                client = LocalSimulation.from_file(connection, timeout_s=2.0)
                status = client.status()
                if (
                    status.get("backend") != "native_headless"
                    or status.get("physics_ready") is not True
                    or status.get("physics_status_cached") is not False
                    or status.get("headless_error") is not None
                ):
                    raise CheckpointError(
                        "headless_not_ready", "Actual native physics is not ready."
                    )
                _OWNED_CLIENTS.add(client)
                try:
                    yield client
                finally:
                    _OWNED_CLIENTS.discard(client)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


def _source_kind(client: LocalSimulation) -> str:
    return _ROOM_SOURCE if isinstance(client, RoomSimulation) else _LOCAL_SOURCE


def _engine_build_matches_expected(snapshot: Mapping[str, Any]) -> bool:
    """Match the owner RPC's build receipt to this checkout's expected manifest."""
    try:
        expected = json.loads((ROOT / "worker/public/sim/manifest.json").read_text())
        return (
            isinstance(expected, dict)
            and isinstance(expected.get("sha256"), str)
            and len(expected["sha256"]) == 64
            and all(char in "0123456789abcdef" for char in expected["sha256"])
            and type(expected.get("bytes")) is int
            and expected["bytes"] > 0
            and snapshot.get("engine_build") == expected
        )
    except (OSError, ValueError):
        return False


def _validation_scope(client: LocalSimulation, *, owned: bool = False) -> str:
    if owned:
        return "fresh_owned_stock_trial"
    if isinstance(client, RoomSimulation):
        return "existing_hosted_room_trial_not_pristine_replay"
    return "existing_external_local_trial_not_pristine_replay"


def inspect_trial(client: LocalSimulation, *, policy: str = "baseline") -> dict[str, Any]:
    """Read-only inspection. Never claim, command, Play, configure, reset or tune."""
    snapshot = client.snapshot()
    eligible = True
    try:
        sample_from_snapshot(snapshot, source_kind=_source_kind(client))
    except CheckpointError:
        eligible = False
    return {
        "ok": True,
        "state": "read_only",
        "policy": policy,
        "actuation_permitted": False,
        "source_kind": _source_kind(client),
        "validation_scope": _validation_scope(client),
        "engine_build_matches_expected": _engine_build_matches_expected(snapshot),
        "native_input_eligible": eligible,
        "stock_world": _stock_world(snapshot),
        "measurement": native_measurement(snapshot),
        "world": world_metadata(snapshot),
        "running": snapshot["runtime"]["running"],
        "playback_rate": snapshot["runtime"].get("rate"),
    }


def _same_fence(snapshot: Mapping[str, Any], fence: Mapping[str, Any]) -> bool:
    authority = _observation(snapshot).get("control_authority", {})
    return authority.get("status") in ("active", "expired") and all(
        authority.get(name) == value for name, value in fence.items()
    )


def _guard(
    snapshot: Mapping[str, Any],
    original_world: Mapping[str, Any],
    fence: Mapping[str, Any],
    *,
    last_time_s: float,
    expect_running: bool,
) -> None:
    if world_metadata(snapshot) != original_world or not _same_fence(snapshot, fence):
        raise CheckpointError(
            "consent_ended", "Original flight/cohort changed; no new epoch adopted."
        )
    if snapshot["runtime"].get("keyboard_thrust") is True or (
        expect_running and snapshot["runtime"]["running"] is not True
    ):
        raise CheckpointError("consent_ended", "Manual input or pause ended the original run.")
    now = _number(_observation(snapshot).get("time_s"))
    if now is None or now < last_time_s:
        raise CheckpointError(
            "consent_ended", "The original native world clock is unavailable/rewound."
        )


def _usage() -> dict[str, Any]:
    return {
        "acknowledged_command_samples": 0,
        "zero_translation_samples": 0,
        "single_axis_translation_samples": 0,
        "multi_axis_translation_samples": 0,
        "active_body_axis_samples_xyz": [0, 0, 0],
        "sum_sampled_l1_accel_mps2": 0.0,
        "sum_sampled_l2_accel_mps2": 0.0,
        "max_linear_norm_mps2": 0.0,
        "max_angular_norm_rad_s2": 0.0,
        "interpretation": "Sampled requested body commands, not integrated thrust or fuel.",
        "l1_label": "body-axis allocation effort proxy, not native fuel consumption",
        "actual_fuel_model": "native center-of-mass L2 force model; not STP jet allocation",
    }


def _record_command(usage: dict[str, Any], accel: np.ndarray, angular: np.ndarray) -> None:
    active = np.abs(accel) > 1e-9
    count = int(np.count_nonzero(active))
    field = (
        "zero_translation_samples"
        if count == 0
        else "single_axis_translation_samples"
        if count == 1
        else "multi_axis_translation_samples"
    )
    usage["acknowledged_command_samples"] += 1
    usage[field] += 1
    for axis in range(3):
        usage["active_body_axis_samples_xyz"][axis] += int(active[axis])
    usage["sum_sampled_l1_accel_mps2"] += float(np.sum(np.abs(accel)))
    usage["sum_sampled_l2_accel_mps2"] += math.hypot(*accel)
    usage["max_linear_norm_mps2"] = max(usage["max_linear_norm_mps2"], math.hypot(*accel))
    usage["max_angular_norm_rad_s2"] = max(usage["max_angular_norm_rad_s2"], math.hypot(*angular))


def decision_lease(report: Mapping[str, Any], source_time_s: float) -> float:
    """Never let a native lease outlast the shared policy's reported validity."""
    until = _number(report.get("decision_valid_until_elapsed_s"))
    source = _number(source_time_s)
    if until is None or source is None or source < 0 or until - source < 0.05:
        raise CheckpointError(
            "decision_expired", "No eligible policy validity remains for a lease."
        )
    return min(LEASE_SIM_S, until - source)


def policy_decision_eligible(
    report: Mapping[str, Any], accel: np.ndarray, angular: np.ndarray, source_time_s: float
) -> bool:
    """Require the exact selected, checked candidate in the shared policy report.

    The released report has no can_command flag. Its selected candidate and
    empty top-level failure list distinguish eligible zero from invalid coast.
    These are the policy's results, not an independent native safety judgment.
    """
    from .stp_policy import decision_is_current

    try:
        if report["failure_reasons"] != [] or not decision_is_current(
            report, now_elapsed_time_s=source_time_s
        ):
            return False
        choice, command = report["candidate_choice"], report["command"]
        kind = choice["selected_kind"]
        if (
            kind is None
            or kind != command["kind"]
            or command["written_to_any_api"] is not False
            or not np.array_equal(accel, command["accel_body_mps2"])
            or not np.array_equal(angular, command["angular_accel_body_rad_s2"])
        ):
            return False
        return any(
            candidate["kind"] == kind
            and candidate["axis_index"] == choice["selected_axis_index"]
            and candidate["accepted"] is True
            and candidate["eligible_for_maneuver_policy"] is True
            and np.array_equal(accel, candidate["accel_body_mps2"])
            for candidate in choice["candidates"]
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _release(
    client: LocalSimulation, original_world: Mapping[str, Any], fence: Mapping[str, Any]
) -> tuple[str, Mapping[str, Any] | None]:
    try:
        current = client.snapshot()
        if world_metadata(current) != original_world or not _same_fence(current, fence):
            return "skipped_after_takeover_no_fallback", current
        client.release_control(fence["run_id"], fence["epoch"], current)
        return "acknowledged", client.snapshot()
    except _KNOWN_UNAPPLIED:
        return "rejected_unapplied_no_fallback", None
    except Exception:
        return "unconfirmed_native_watchdog_no_fallback", None


def run_trial(
    client: LocalSimulation,
    options: CheckpointOptions,
    *,
    policy: str,
    owned: bool = False,
    deadline: float | None = None,
    expected_world: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fly one explicit-consent trial with original-source leases and one actual grant.

    Native known-unapplied command rejections discard that decision. Any other
    actuation failure ends the trial without retry. Finally releases only the
    original cohort, never an unchecked Stop/Pause or another controller's run.
    """
    if not isinstance(options, CheckpointOptions) or options.run is not True:
        raise CheckpointError("run_required", "Actuation requires explicit --run consent.")
    if owned and (not options.isolated or client not in _OWNED_CLIENTS):
        raise CheckpointError("invalid_scope", "Owned rate changes require an isolated invocation.")
    from .control_contract import vector

    config = policy_config(policy)
    from .stp_policy import STPCheckpointController

    controller = STPCheckpointController(config)
    origin_wall = time.monotonic()
    deadline = min(deadline or math.inf, origin_wall + options.wall_budget_s)
    initial = client.snapshot()
    world = world_metadata(initial)
    if isinstance(client, RoomSimulation) and not _engine_build_matches_expected(initial):
        raise CheckpointError(
            "engine_build_mismatch",
            "Hosted engine receipt does not match this checkout's expected build manifest; "
            "no control was claimed.",
        )
    if expected_world is not None and world != expected_world:
        raise CheckpointError("comparison_mismatch", "Fresh trial metadata/build does not match.")
    if not _stock_world(initial):
        raise CheckpointError(
            "stock_world_required", "Checkpoint actuation requires unchanged stock setup."
        )
    if owned and (initial["runtime"]["running"] or _observation(initial)["time_s"] != 0):
        raise CheckpointError("fresh_trial_required", "Owned trials must start fresh and paused.")
    if not owned and initial["runtime"].get("rate") != 1:
        raise CheckpointError(
            "external_rate", "External runs require actual rate 1; no rate was changed."
        )
    observation = _observation(initial)
    if observation["mission"]["status"] != "running":
        raise CheckpointError("mission_not_running", "The native mission is already terminal.")
    if initial["runtime"].get("keyboard_thrust") is True:
        raise CheckpointError(
            "manual_control", "Native manual input is active; no control was claimed."
        )
    origin_sim = _number(observation.get("time_s"))
    if origin_sim is None:
        raise CheckpointError("source_contract", "The actual native clock is unavailable.")
    result: dict[str, Any] = {
        "ok": False,
        "state": "starting",
        "policy": policy,
        "policy_config": asdict(config),
        "source_kind": _source_kind(client),
        "frame": observation.get("frame"),
        "rotation_convention": "active_xyzw",
        "radar_clock_offset_basis": "native radar_clock=simulation_elapsed_seconds contract",
        "initial": native_measurement(initial),
        "initial_world": world,
        "stock_world": True,
        "fresh_owned_trial": owned,
        "validation_scope": _validation_scope(client, owned=owned),
        "lease_sim_s": LEASE_SIM_S,
        "cadence_hz": options.cadence_hz,
        "duration_limit_s": options.duration_s,
        "command_usage": _usage(),
        "decision_count": 0,
        "decision_timing": {
            "samples": 0,
            "max_policy_compute_wall_s": 0.0,
            "max_report_copy_wall_s": 0.0,
            "max_compute_and_report_wall_s": 0.0,
            "nominal_lease_wall_budget_at_requested_rate_s": LEASE_SIM_S / options.playback_rate,
            "budget_kind": "configured native lease/rate, not a substituted source clock",
        },
        "known_unapplied_operations": 0,
        "release_state": "not_claimed",
        "native_validation_limits": {"arrival_radius_m": 50.0, "maximum_damage_j": 209200.0},
        "initial_playback_rate": initial["runtime"].get("rate"),
        "actuation_outcome": "not_attempted",
        "final_measurement_kind": "actual_last_public_receipt_not_a_command_ack",
        "diagnostics_source": "same shared policy report; anonymous associations, not physical IDs",
        "damage_limit_basis": "versioned stock acceptance limit, not a live SDK parameter",
    }
    fence = None
    current = initial
    last_time = origin_sim
    expect_running = initial["runtime"]["running"]
    awaiting_play = False
    play_deadline = math.inf
    rate_requested = False
    next_progress = origin_wall
    phase = "decision"
    try:
        while True:
            tick = time.monotonic()
            observation = _observation(current)
            now = observation["time_s"]
            terminal = observation["mission"]["status"] != "running"
            if fence is not None:
                _guard(
                    current,
                    world,
                    fence,
                    last_time_s=last_time,
                    expect_running=expect_running and not terminal,
                )
            if terminal:
                result["state"] = "native_terminal"
                break
            if now - origin_sim >= options.duration_s or tick >= deadline:
                result["state"] = "duration_limit" if tick < deadline else "wall_budget_limit"
                break
            if fence is not None and observation["control_authority"]["status"] != "active":
                raise CheckpointError("lease_expired", "The original native control lease expired.")
            if awaiting_play and not current["runtime"]["running"]:
                if tick >= play_deadline:
                    raise CheckpointError("play_timeout", "Checked Play did not actually start.")
                time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))
                current = client.snapshot()
                continue
            if current["runtime"]["running"]:
                expect_running, awaiting_play = True, False
            if not owned and current["runtime"].get("rate") != 1:
                raise CheckpointError("external_rate", "External playback rate changed; yielding.")
            sample = sample_from_snapshot(current, source_kind=_source_kind(client))
            compute_started = time.monotonic()
            accel, angular = controller.command(
                sample, observation["radar"], now_elapsed_time_s=now
            )
            compute_finished = time.monotonic()
            result["decision_count"] += 1
            policy_report = controller.report
            report_finished = time.monotonic()
            timings = result["decision_timing"]
            timings["samples"] += 1
            timings["max_policy_compute_wall_s"] = max(
                timings["max_policy_compute_wall_s"], compute_finished - compute_started
            )
            timings["max_report_copy_wall_s"] = max(
                timings["max_report_copy_wall_s"], report_finished - compute_finished
            )
            timings["max_compute_and_report_wall_s"] = max(
                timings["max_compute_and_report_wall_s"], report_finished - compute_started
            )
            # Already detached and bounded by the shared controller. Summarize
            # only once at stdout, not on the time-critical command path.
            result["policy_report"] = policy_report
            accel = vector(accel, "body acceleration")
            angular = vector(angular, "body angular acceleration")
            if math.hypot(*accel) > 0.5 or math.hypot(*angular) > 0.05:
                raise CheckpointError(
                    "policy_command_bounds", "Shared policy exceeded native command caps."
                )
            # The policy owns its eligibility judgment. A valid zero command is
            # distinct from invalid-data coast, particularly before first Play.
            if not policy_decision_eligible(policy_report, accel, angular, now):
                raise CheckpointError(
                    "policy_ineligible", "Shared policy did not approve an eligible decision."
                )
            lease = decision_lease(policy_report, now)
            if time.monotonic() >= deadline:
                result["state"] = "wall_budget_limit"
                break
            if fence is None:
                run_id, identity = f"checkpoint_{uuid.uuid4().hex}", f"api_{uuid.uuid4().hex}"
                phase = "claim"
                result["actuation_outcome"] = "claim_unconfirmed_native_watchdog"
                try:
                    grant = client.claim_control(run_id, identity, current, lease_sim_s=LEASE_SIM_S)
                except _KNOWN_UNAPPLIED as error:
                    result["actuation_outcome"] = "claim_known_unapplied"
                    result["known_unapplied_operations"] += 1
                    raise CheckpointError(
                        "claim_unapplied", "Native claim was rejected before application."
                    ) from error
                fence = controller_fence(grant)
                if fence["run_id"] != run_id or fence["controller_id"] != identity:
                    raise CheckpointError(
                        "claim_mismatch", "Actual grant belongs to another controller."
                    )
                result["granted_fence"] = dict(fence)
                _guard(grant, world, fence, last_time_s=now, expect_running=expect_running)
                # Keep current, not grant, as the decision's original source.
            try:
                phase = "command"
                result["actuation_outcome"] = "unknown_do_not_retry"
                client.set_leased_command(
                    accel, angular, current, lease_sim_s=lease, controller_fence=fence
                )
                result["actuation_outcome"] = "command_acknowledged_not_application_proof"
                _record_command(result["command_usage"], accel, angular)
                if not current["runtime"]["running"]:
                    phase = "play"
                    result["actuation_outcome"] = "unknown_do_not_retry"
                    client.play_checked(current)
                    result["actuation_outcome"] = "play_acknowledged_not_running_proof"
                    awaiting_play = True
                    play_deadline = min(deadline, time.monotonic() + 2.0)
                elif owned and options.playback_rate == 5 and not rate_requested:
                    # Only after an actual running readback, using its actual context.
                    phase = "rate"
                    result["actuation_outcome"] = "unknown_do_not_retry"
                    client.command(
                        {"op": "play", "rate": 5, "expected_context": control_context(current)}
                    )
                    result["actuation_outcome"] = "rate_acknowledged_not_running_proof"
                    rate_requested = True
            except _KNOWN_UNAPPLIED:
                result["known_unapplied_operations"] += 1
                result["actuation_outcome"] = f"{phase}_known_unapplied_discarded"
                # Do not resend this command, relabel its clock or reclaim a new term.
            last_time = now
            result["state"] = "running"
            if options.progress and tick >= next_progress:
                progress = {"policy": policy, **native_measurement(current)}
                progress.pop("score_label")
                print(json.dumps(_summary(progress), allow_nan=False), file=sys.stderr)
                next_progress = tick + 10.0
            time.sleep(max(0.0, min(tick + 1 / options.cadence_hz, deadline) - time.monotonic()))
            phase = "observation"
            current = client.snapshot()
            phase = "decision"
    except (Exception, KeyboardInterrupt) as error:
        result["state"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "error"
        result["error"] = _error(error, phase)
    finally:
        if fence is not None:
            release_state, after = _release(client, world, fence)
            result["release_state"] = release_state
            if after is not None:
                current = after
        result["final"] = native_measurement(current)
        result["final_playback_rate"] = current["runtime"].get("rate")
        result["final_world"] = world_metadata(current)
        result["world_metadata_unchanged"] = result["final_world"] == world
        result["wall_elapsed_s"] = time.monotonic() - origin_wall
        result["native_success"] = native_success(current)
        result["ok"] = (
            result["native_success"]
            and result["world_metadata_unchanged"]
            and "error" not in result
            and result["release_state"] == "acknowledged"
        )
    return result


def run_checkpoint(options: CheckpointOptions) -> dict[str, Any]:
    """Inspect or run once. Comparison creates two independent matching owned worlds."""
    if not isinstance(options, CheckpointOptions):
        raise CheckpointError("invalid_options", "Validated CheckpointOptions are required.")
    with diagnostics.diagnostics_context() if options.diagnostics else nullcontext():
        return _run_checkpoint(options)


def wait_for_owner(client: LocalSimulation, options: CheckpointOptions, *, deadline: float) -> None:
    """Wait using reads only. Never claim, Play, adopt a fence or retry actuation."""
    from .diagnostics import Diagnostics, _error_fields

    end = min(deadline, time.monotonic() + options.wait_for_owner_s)
    original_timeout = client.timeout_s
    read_timeout = (
        max(original_timeout, 8.0) if isinstance(client, RoomSimulation) else original_timeout
    )
    wait_log = Diagnostics(max_events=128, max_events_per_second=2) if options.diagnostics else None
    previous_error = None
    previous_frame = None
    next_progress = 0.0
    try:
        while time.monotonic() < end:
            now = time.monotonic()
            if options.progress and now >= next_progress:
                print(
                    json.dumps({"state": "waiting_for_owner", "actuation_permitted": False}),
                    file=sys.stderr,
                    flush=True,
                )
                next_progress = now + 30
            client.timeout_s = min(read_timeout, max(0.001, end - now))
            try:
                native_ready = False
                may_read = True
                if not isinstance(client, RoomSimulation):
                    status = client.status()
                    native_ready = (
                        status.get("backend") == "native_headless"
                        and status.get("physics_ready") is True
                        and status.get("physics_status_cached") is False
                    )
                    may_read = status.get("busy") is False and (
                        native_ready or status.get("connected") is True
                    )
                if may_read and time.monotonic() < end:
                    client.timeout_s = min(read_timeout, max(0.001, end - time.monotonic()))
                    snapshot = client.snapshot()
                    authority = _observation(snapshot).get("control_authority", {})
                    if options.run and authority.get("status") in ("active", "expired"):
                        raise CheckpointError(
                            "owner_in_use", "An existing controller owns this flight; no takeover."
                        )
                    renderer = snapshot.get("renderer", {})
                    frames = renderer.get("rendered_frames")
                    if native_ready:
                        return
                    if renderer.get("ready") is True and type(frames) is int:
                        if previous_frame is not None and frames > previous_frame:
                            return
                        previous_frame = frames
            except LocalBusyError:
                pass  # Actual pre-dispatch read contention, not a flight retry.
            except LocalApiError as error:
                fields = _error_fields(error)
                if wait_log is not None and (
                    fields["code"] != "owner_unavailable" or previous_error != fields["code"]
                ):
                    status = getattr(error, "http_status", None)
                    wait_log.emit(
                        "error",
                        stage="collector",
                        transport="room" if isinstance(client, RoomSimulation) else "local",
                        **fields,
                        **({"http_status": status} if type(status) is int else {}),
                    )
                previous_error = fields["code"]
                if fields["code"] not in ("owner_unavailable", "transport_unavailable"):
                    raise CheckpointError(
                        "owner_wait_failed", "Engine readiness read failed; no command was sent."
                    ) from error
            time.sleep(min(0.5, max(0.0, end - time.monotonic())))
        raise CheckpointError(
            "owner_wait_timeout", "No ready engine was observed before the owner wait ended."
        )
    finally:
        client.timeout_s = original_timeout


def _run_checkpoint(options: CheckpointOptions) -> dict[str, Any]:
    if os.name != "posix":
        raise CheckpointError(
            "private_backend_platform",
            "Run this command and any private local backend inside WSL2 on its Linux filesystem. "
            "Windows file modes are not verified NTFS ACL protection. "
            "The prebuilt browser game and platform probe do not require this private backend.",
        )
    result: dict[str, Any] = {
        "schema": "wsorb.checkpoint",
        "schema_version": 1,
        "ok": False,
        "mode": "run" if options.run else "read_only",
        "policy": options.policy,
        "isolated": options.isolated,
        "transport": "hosted_room_public_API" if options.room_file else "local_private_API",
        "official_server_execution": "not_performed_here",
        "execution_kind": (
            "authenticated_hosted_room_public_API_not_SDK_emulation_or_physics_parity"
            if options.room_file
            else "authenticated_local_API_validation_not_SDK_emulation_or_physics_parity"
        ),
        "automatic_report_files": False,
        "trials": [],
    }
    deadline = time.monotonic() + options.wall_budget_s
    policies = ("baseline", "body-axis") if options.policy == "compare" else (options.policy,)
    expected_world = None
    for policy in policies:
        try:
            if time.monotonic() >= deadline:
                raise CheckpointError(
                    "wall_budget_limit", "The total invocation wall budget ended."
                )
            if options.isolated:
                with isolated_simulation(deadline=deadline) as client:
                    trial = (
                        run_trial(
                            client,
                            options,
                            policy=policy,
                            owned=True,
                            deadline=deadline,
                            expected_world=expected_world,
                        )
                        if options.run
                        else inspect_trial(client, policy=policy)
                    )
            else:
                # No implicit default descriptor or environment-selected endpoint.
                client = (
                    RoomSimulation.from_file(options.room_file, timeout_s=2.0)
                    if options.room_file is not None
                    else LocalSimulation.from_file(options.connection_file, timeout_s=2.0)
                )
                if options.wait_for_owner_s > 0:
                    from .diagnostics import diagnostics_context

                    # Waiting must not consume the bounded flight trace before
                    # a user has opened the game. Progress remains explicit.
                    with diagnostics_context(enabled=False):
                        wait_for_owner(client, options, deadline=deadline)
                trial = (
                    run_trial(client, options, policy=policy, deadline=deadline)
                    if options.run
                    else inspect_trial(client, policy=policy)
                )
            if options.policy == "compare" and expected_world is None:
                expected_world = trial.get("initial_world")
            result["trials"].append(trial)
        except (Exception, KeyboardInterrupt) as error:
            result["trials"].append(
                {"ok": False, "policy": policy, "error": _error(error, "setup")}
            )
            break
    result["ok"] = len(result["trials"]) == len(policies) and all(
        trial.get("ok") is True for trial in result["trials"]
    )
    if options.policy == "compare":
        configs = [trial.get("policy_config") for trial in result["trials"]]
        matched_configs = len(configs) == 2 and all(isinstance(item, dict) for item in configs)
        if matched_configs:
            differences = {
                name for name in configs[0] if configs[0].get(name) != configs[1].get(name)
            }
            matched_configs = differences == {"body_axis_preference"}
        result["comparison"] = {
            "matching_initial_worlds": len(result["trials"]) == 2
            and result["trials"][0].get("initial_world") is not None
            and result["trials"][0].get("initial_world")
            == result["trials"][1].get("initial_world"),
            "policy_configs_matched": matched_configs,
            "only_policy_difference": "body_axis_preference",
            "fuel_claim": "Measured native L2 fuel only; no 41-percent or allocation-fuel claim.",
        }
        result["ok"] = result["ok"] and matched_configs
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Module and existing wsorb CLI entry point; stdout is one bounded JSON object."""
    try:
        result = run_checkpoint(parse_args(argv))
    except (Exception, KeyboardInterrupt) as error:
        result = {"schema": "wsorb.checkpoint", "ok": False, "error": _error(error, "invocation")}
    output = json.dumps(_summary(result), allow_nan=False, separators=(",", ":"))
    if len(output.encode("utf-8")) > MAX_OUTPUT_BYTES:
        output = json.dumps(
            {"schema": "wsorb.checkpoint", "ok": False, "error": {"code": "output_limit"}}
        )
        result["ok"] = False
    print(output)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
