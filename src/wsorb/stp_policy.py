"""Opt-in numeric baseline for the real STP adapter, not a flight-safety claim.

Uses only STPState and the public anonymous radar matrices. There is no Phobos
terrain, shuttle clearance, target velocity, vision, route search, independent
navigation sensor or SDK watchdog here. Radial repulsion cannot resolve unknown
tangential debris motion or guarantee collision avoidance. Target PD damps
velocity relative to Phobos, not relative to a moving target. There is no
arrival-region geometry or mission-complete signal in this policy.

Radar time uses a different startup origin. The operator must declare
``mission_measurement_time = source_radar_time + radar_clock_offset_s`` from
the runtime's clock configuration. No offset is estimated from observations.
Source rows and timestamps remain unchanged. State reads are sequential, and
neither their report time nor a later clock read makes them atomic.

Importing this module loads no SDK, connects nowhere and reads no configuration.
It also supports direct imports beside stp_api.py in the licensed entry point.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, fields
from numbers import Real
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

if __package__:
    from .flight._validation import real_array, scalar, vector
    from .flight.control import _bounded, coast_command, reference_to_body
    from .flight.prediction import radar_rows
    from .stp_api import STPState
else:
    # The example adds src/wsorb, not the historical practice package, to sys.path.
    from flight._validation import real_array, scalar, vector
    from flight.control import _bounded, coast_command, reference_to_body
    from flight.prediction import radar_rows
    from stp_api import STPState

CONFIG_SCHEMA = "wsorb.stp_policy"
CONFIG_SCHEMA_VERSION = 1
CONFIG_FILE_ENV = "WSORB_STP_POLICY_FILE"
CONFIG_JSON_ENV = "WSORB_STP_POLICY_JSON"
MAX_CONFIG_BYTES = 16 * 1024

# Review/input budgets for this baseline, not properties of the licensed SDK.
_BOUNDS = {
    "duration_wall_s": (0.0, 300.0),
    "max_report_age_s": (0.0, 5.4),
    "max_radar_age_s": (0.0, 5.4),
    "position_gain_s2": (0.0, 1.0),
    "velocity_gain_s": (0.0, 10.0),
    "angular_velocity_gain_s": (0.0, 10.0),
    "avoidance_range_m": (0.0, 1e6),
    "avoidance_horizon_s": (0.0, 30.0),
    "avoidance_accel_mps2": (0.0, 0.5),
    "max_accel_mps2": (0.0, 0.5),
    "max_angular_accel_rad_s2": (0.0, 0.05),
    "radar_clock_offset_s": (-1e9, 1e9),
}


def _number(value: Any, name: str) -> float:
    low, high = _BOUNDS[name]
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite real number in [{low}, {high}]")
    try:
        value = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"{name} is not a representable finite real number") from error
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be a finite real number in [{low}, {high}]")
    if name in ("duration_wall_s", "avoidance_range_m") and value == 0.0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True, slots=True)
class STPPolicyConfig:
    """Immutable, validated policy data. Construction alone never commands jets.

    Enabling requires a declared radar offset, even when that offset is zero.
    Gains and repulsion distances are operator-reviewed tuning assumptions, not
    geometry or calibrated sensor bounds. Norm limits can only lower the public
    actuator caps. duration_wall_s limits the example's wall-clock loop, not SDK
    call duration, command persistence or process-death cleanup.
    """

    enabled: bool = False
    radar_clock_offset_s: float | None = None
    duration_wall_s: float = 30.0
    max_report_age_s: float = 1.0
    max_radar_age_s: float = 1.5
    position_gain_s2: float = 0.0012
    velocity_gain_s: float = 0.07
    angular_velocity_gain_s: float = 0.1
    avoidance_range_m: float = 70.0
    avoidance_horizon_s: float = 3.0
    avoidance_accel_mps2: float = 0.5
    max_accel_mps2: float = 0.5
    max_angular_accel_rad_s2: float = 0.05

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be an explicit boolean")
        for name in _BOUNDS:
            value = getattr(self, name)
            if name == "radar_clock_offset_s" and value is None:
                if self.enabled:
                    raise ValueError("An enabled policy requires explicit radar_clock_offset_s")
                continue
            object.__setattr__(self, name, _number(value, name))

    @classmethod
    def from_mapping(cls, document: Mapping[str, Any]) -> STPPolicyConfig:
        """Parse versioned data with explicit enable, rejecting unknown fields."""
        if not isinstance(document, Mapping) or any(type(key) is not str for key in document):
            raise ValueError("STP policy configuration must be an object with string keys")
        required = {"schema", "schema_version", "enabled"}
        allowed = required | {item.name for item in fields(cls)}
        if required - document.keys() or document.keys() - allowed:
            raise ValueError("STP policy configuration has missing or unknown fields")
        if document["schema"] != CONFIG_SCHEMA:
            raise ValueError(f"schema must be {CONFIG_SCHEMA!r}")
        if (
            type(document["schema_version"]) is not int
            or document["schema_version"] != CONFIG_SCHEMA_VERSION
        ):
            raise ValueError("schema_version must be the integer 1")
        return cls(
            **{
                name: value
                for name, value in document.items()
                if name not in ("schema", "schema_version")
            }
        )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"Duplicate STP policy JSON key: {name}")
        result[name] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"STP policy JSON cannot contain {value}")


def parse_config(source: str | bytes) -> STPPolicyConfig:
    """Parse at most 16 KiB of strict UTF-8 JSON without executing any code."""
    if not isinstance(source, (str, bytes)) or len(source) > MAX_CONFIG_BYTES:
        raise ValueError("STP policy configuration must be at most 16384 UTF-8 bytes")
    try:
        data = source.encode("utf-8") if isinstance(source, str) else source
        if len(data) > MAX_CONFIG_BYTES:
            raise ValueError("STP policy configuration must be at most 16384 UTF-8 bytes")
        document = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("STP policy configuration must be a valid UTF-8 JSON object") from error
    return STPPolicyConfig.from_mapping(document)


def load_config_from_environment(environ: Mapping[str, str] | None = None) -> STPPolicyConfig:
    """Load one local file or inline JSON, never SDK bootstrap arguments.

    Select WSORB_STP_POLICY_FILE or WSORB_STP_POLICY_JSON, not both. Missing
    configuration disables the policy; present but invalid configuration raises
    before the example connects. Configuration is read once, not hot-reloaded.
    """
    environ = os.environ if environ is None else environ
    present = [name for name in (CONFIG_FILE_ENV, CONFIG_JSON_ENV) if name in environ]
    if not present:
        return STPPolicyConfig()
    if len(present) != 1:
        raise ValueError("Select only one of WSORB_STP_POLICY_FILE and WSORB_STP_POLICY_JSON")
    value = environ[present[0]]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{present[0]} must be a nonempty string")
    if present[0] == CONFIG_JSON_ENV:
        return parse_config(value)
    try:
        path = Path(value).expanduser()
        if not path.is_file():
            raise ValueError("WSORB_STP_POLICY_FILE must name a local regular JSON file")
        with path.open("rb") as handle:
            source = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError as error:
        raise ValueError("Could not read WSORB_STP_POLICY_FILE") from error
    return parse_config(source)


def _checked_state(
    state: STPState,
) -> tuple[
    float,
    npt.NDArray[np.float64],
    npt.NDArray[np.float64],
    npt.NDArray[np.float64],
    npt.NDArray[np.float64],
    npt.NDArray[np.float64],
]:
    if not isinstance(state, STPState):
        raise ValueError("state must be an actual adapter-local STPState")
    elapsed = scalar(state.elapsed_time_s, "elapsed_time_s")
    position = vector(state.position_phobos_m, "position_phobos_m")
    velocity = vector(state.velocity_phobos_m_s, "velocity_phobos_m_s")
    quaternion = vector(
        state.passive_quaternion_phobos_to_body_xyzw, "passive_quaternion", length=4
    )
    angular_velocity = vector(state.angular_velocity_phobos_rad_s, "angular_velocity_phobos_rad_s")
    target = vector(state.target_position_phobos_m, "target_position_phobos_m")
    scalar(state.mass_kg, "mass_kg", positive=True)
    scalar(state.fuel_used_kg, "fuel_used_kg")
    scalar(state.damage_j, "damage_j")
    inertia = real_array(state.inertia_body_kg_m2, "inertia_body_kg_m2")
    if inertia.shape != (3, 3):
        raise ValueError("inertia_body_kg_m2 must have shape (3, 3)")
    scale = float(np.max(np.abs(inertia)))
    if scale == 0.0:
        raise ValueError("inertia_body_kg_m2 must be positive definite")
    inertia /= scale
    if not np.allclose(inertia, inertia.T, rtol=1e-8, atol=1e-12):
        raise ValueError("inertia_body_kg_m2 must be symmetric")
    if np.linalg.eigvalsh(inertia)[0] <= 0.0:
        raise ValueError("inertia_body_kg_m2 must be positive definite")
    return elapsed, position, velocity, quaternion, angular_velocity, target


def decide_command(
    state: STPState,
    debris: npt.ArrayLike,
    config: STPPolicyConfig | None = None,
    *,
    now_elapsed_time_s: float | None = None,
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Compute bounded body-frame m/s² and rad/s² without applying commands.

    With enable and valid data, combine target PD with bounded repulsion away
    from each public radar direction. Only closing scalar range rate advances
    the closest range over sample age plus the configured radial lookahead.
    Negative noisy ranges count as zero clearance. Diameter is validated, not
    subtracted from a range that already measures the closest point. Increasing
    proximity suppresses target PD. Angular control only damps relative spin.

    A fresh, separately read adapter elapsed clock is required. Any invalid
    state, missing/empty/malformed radar, stale sample/report, future timestamp
    or unrepresentable calculation returns two actual zero vectors. Radar must
    not be future-dated even relative to the earlier state report clock. No
    future tolerance, timestamp clipping, inferred offset or backdating is used.
    These checks are data checks, not independent sensor-health evidence or
    proof that the public radar covers every hazard.
    """
    if config is None:
        return coast_command()
    if not isinstance(config, STPPolicyConfig):
        raise ValueError("config must be STPPolicyConfig")
    if not config.enabled:
        return coast_command()
    try:
        elapsed, position, velocity, quaternion, angular_velocity, target = _checked_state(state)
        now = scalar(now_elapsed_time_s, "now_elapsed_time_s")
        if now < elapsed or now - elapsed > config.max_report_age_s:
            raise ValueError("Future or stale state report")
        rows = radar_rows(debris)
        if len(rows) == 0:
            raise ValueError("Empty radar is not evidence of clear space")
        with np.errstate(over="ignore", invalid="ignore"):
            # Keep the original rows, especially row 2, unchanged.
            times = rows[:, 2, 0] + config.radar_clock_offset_s
            ages = now - times
        if (
            not np.isfinite(times).all()
            or not np.isfinite(ages).all()
            or np.any(times < 0.0)
            or np.any(times > elapsed)
            or np.any(ages > config.max_radar_age_s)
        ):
            raise ValueError("Radar timestamps are invalid, future or stale after declared offset")
        with np.errstate(over="ignore", invalid="ignore"):
            pd = (target - position) * config.position_gain_s2 - velocity * config.velocity_gain_s
            radial_ranges = rows[:, 1, 0] + np.minimum(rows[:, 1, 1], 0.0) * (
                ages + config.avoidance_horizon_s
            )
        if not np.isfinite(radial_ranges).all():
            raise ValueError("Radial lookahead is not representable")
        # Divide only inside the response range. Avoid overflowing a quotient
        # for distant rows whose repulsion weight is already known to be zero.
        weights = np.zeros(len(rows), dtype=np.float64)
        nearby = radial_ranges < config.avoidance_range_m
        weights[nearby] = 1.0 - np.maximum(radial_ranges[nearby], 0.0) / config.avoidance_range_m
        lengths = np.hypot(np.hypot(rows[:, 0, 0], rows[:, 0, 1]), rows[:, 0, 2])
        directions = rows[:, 0, :] / lengths[:, None]
        repulsion = _bounded(
            -np.sum(directions * weights[:, None], axis=0) * config.avoidance_accel_mps2,
            config.avoidance_accel_mps2,
        )
        acceleration = _bounded(
            _bounded(pd, config.max_accel_mps2) * (1.0 - float(np.max(weights))) + repulsion,
            config.max_accel_mps2,
        )
        with np.errstate(over="ignore", invalid="ignore"):
            angular_acceleration = vector(
                -angular_velocity * config.angular_velocity_gain_s, "angular acceleration"
            )
        return (
            _bounded(
                reference_to_body(acceleration, quaternion, "passive_xyzw"),
                config.max_accel_mps2,
            ),
            _bounded(
                reference_to_body(angular_acceleration, quaternion, "passive_xyzw"),
                config.max_angular_accel_rad_s2,
            ),
        )
    except (ValueError, TypeError, OverflowError, FloatingPointError, np.linalg.LinAlgError):
        return coast_command()
