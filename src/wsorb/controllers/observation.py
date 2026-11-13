"""Copy native public numeric samples without filling absent sensor reports."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..control_contract import ControlSample, checked_sample, radar_rows, scalar

SOURCE_KINDS = frozenset(
    {
        "local_public_snapshot",
        "hosted_room_public_snapshot",
        "native_manual_public_observation",
    }
)
MAX_REPORT_AGE_S = 1.0
# A 500-row rolling scan at 120 Hz needs 4.17 s to refresh; 5.0 s covers the
# full cycle with margin for the publication and decision clocks.
MAX_RADAR_AGE_S = 5.0
RADAR_CLOCK = "simulation_elapsed_seconds"
ROLLING_SELECTION = "official_rolling"
_TIME_TOLERANCE_S = 1.0e-9


@dataclass(frozen=True, slots=True)
class _PublicSample(ControlSample):
    """Numeric provenance retained even for a healthy, empty radar publication."""

    radar_clock: str
    radar_sample_time_s: float
    radar_rolling: bool
    public_context: tuple[int, ...]
    navigation_sequence: int
    radar_sequence: int
    source_observation: Mapping[str, Any] | None = None


def _counter(value: Any, name: str, maximum: int = 0xFFFFFFFF) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name} requires an actual nonnegative integer")
    return value


def _mapping(value: Any, name: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise ValueError(f"Missing actual {name} report")
    return value


def sample_from_observation(
    observation: Mapping[str, Any], *, source_kind: str, retain_observation: bool = False
) -> ControlSample:
    """Require ready navigation/radar and their actual clocks and physics context.

    These native sources declare an elapsed-seconds radar clock with offset zero.
    Empty ready radar is valid, but missing, frozen or unavailable radar is not.
    Mixed row epochs stay unchanged and must belong to the reported publication.
    The official rolling scan ages each row from its own acquisition time.
    This validates numeric inputs, not SDK equivalence or command authority.
    """
    if not isinstance(source_kind, str) or source_kind not in SOURCE_KINDS:
        raise ValueError("Unsupported public observation source_kind")
    observation = _mapping(observation, "observation")
    try:
        if (
            observation["rotation_convention"] != "active_xyzw"
            or observation["radar_clock"] != RADAR_CLOCK
        ):
            raise ValueError("Unsupported native rotation convention or radar clock")
        frame = observation["frame"]
        if not isinstance(frame, str) or not frame.strip() or len(frame) > 256:
            raise ValueError("Missing actual native reference frame")
        context = tuple(
            _counter(observation[name], name)
            for name in ("seed", "trial_revision", "tuning_revision", "manual_interventions")
        )
        health = _mapping(observation["sensor_health"], "sensor health")
        if type(health["schema_version"]) is not int or health["schema_version"] != 1:
            raise ValueError("Unsupported native sensor health schema")
        revision = _counter(health["revision"], "sensor revision")
        raw_units = health["units"]
        if not isinstance(raw_units, (list, tuple)):
            raise ValueError("Missing actual native sensor units")
        units = {}
        for raw in raw_units:
            unit = _mapping(raw, "sensor unit")
            identity = unit["id"]
            if not isinstance(identity, str) or identity in units:
                raise ValueError("Duplicate or invalid native sensor unit")
            units[identity] = unit
        for identity in ("navigation", "radar_primary"):
            unit = units[identity]
            if unit["delivery"] != "ready" or unit["active_faults"] != []:
                raise ValueError("Actual navigation and primary radar must be ready and fault-free")
            scalar(unit["sample_time_s"], f"{identity} sample time")
            _counter(unit["sequence"], f"{identity} sequence", maximum=0xFFFFFFFFFFFFFFFF)
        elapsed = scalar(observation["navigation_sample_time_s"], "navigation time")
        now = scalar(observation["time_s"], "world time")
        radar_time = scalar(units["radar_primary"]["sample_time_s"], "radar sample time")
        if elapsed != units["navigation"]["sample_time_s"]:
            raise ValueError("Navigation source clocks disagree")
        if not 0 <= now - elapsed <= MAX_REPORT_AGE_S:
            raise ValueError("Future or stale actual navigation")
        radar_age = now - radar_time
        if radar_age < -_TIME_TOLERANCE_S or radar_age > MAX_RADAR_AGE_S + _TIME_TOLERANCE_S:
            raise ValueError("Future or stale actual radar publication")
        rows = radar_rows(observation["radar"])
        times = rows[:, 2, 0]
        selection = observation.get("radar_selection")
        rolling = isinstance(selection, Mapping) and selection.get("selection") == ROLLING_SELECTION
        if len(rows):
            if rolling:
                # The official scan updates one row per 120 Hz iteration and
                # publishes every 50 rows. Each row keeps its own acquisition
                # time at or before the publication time; the oldest row can be
                # one scan older than the newest, so age each row separately.
                invalid = (
                    np.any(times > now + _TIME_TOLERANCE_S)
                    or np.any(now - times > MAX_RADAR_AGE_S + _TIME_TOLERANCE_S)
                    or np.any(times > radar_time + _TIME_TOLERANCE_S)
                )
            else:
                invalid = (
                    np.max(times) != radar_time
                    or np.any(times > now)
                    or np.any(now - times > MAX_RADAR_AGE_S)
                )
            if invalid:
                raise ValueError("Radar row epochs disagree with actual publication clocks")
        ship = _mapping(observation["shuttle"], "shuttle navigation")
        target = _mapping(observation["target"], "target navigation")
        mission = _mapping(observation["mission"], "mission navigation")
        physics = _mapping(observation["applied_physics"], "applied physics")
        if (
            observation.get("applied_physics_error") is not None
            or scalar(physics["source_time_s"], "physics source time") != elapsed
            or physics["frame"] != frame
            or _counter(physics["tuning_revision"], "physics tuning revision") != context[2]
        ):
            raise ValueError("Actual physics report has another source clock/frame/context")
        if (
            scalar(observation["max_accel_mps2"], "native acceleration cap") != 0.5
            or scalar(observation["max_angular_accel_rad_s2"], "native angular cap") != 0.05
        ):
            raise ValueError("Reviewed native profiles require the actual 0.5/0.05 actuator limits")
        return checked_sample(
            _PublicSample(
                elapsed_time_s=elapsed,
                position_world_m=ship["position"],
                velocity_world_mps=ship["velocity"],
                quaternion_xyzw=ship["rotation"],
                rotation_convention="active_xyzw",
                angular_velocity_world_rad_s=ship["angular_velocity_rad_s"],
                target_position_world_m=target["position"],
                mass_kg=physics["mass_kg"],
                inertia_body_kg_m2=physics["inertia_body_kg_m2"],
                fuel_used_kg=mission["fuel_used_kg"],
                damage_j=mission["damage"],
                frame=frame,
                source_kind=source_kind,
                radar_clock=RADAR_CLOCK,
                radar_sample_time_s=radar_time,
                radar_rolling=rolling,
                public_context=(*context, revision),
                navigation_sequence=units["navigation"]["sequence"],
                radar_sequence=units["radar_primary"]["sequence"],
                source_observation=deepcopy(dict(observation)) if retain_observation else None,
            )
        )
    except (KeyError, TypeError, OverflowError, np.linalg.LinAlgError) as error:
        raise ValueError("Actual public navigation/radar/physics context is incomplete") from error
