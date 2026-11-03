"""Reviewed controller IDs shared by native runs, sweeps and the browser selector.

The catalog imports no controller math, SDK, graphics, transport or runner.
Only explicit controller construction loads the selected implementation.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from importlib.resources import files
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..experiment import Decision

_CATALOG = json.loads(files(__package__).joinpath("catalog.json").read_text(encoding="utf-8"))
PROFILE_IDS: tuple[str, ...] = tuple(entry["id"] for entry in _CATALOG)

__all__ = [
    "PROFILE_IDS",
    "catalog",
    "create_controller",
    "policy_factory",
    "sample_from_observation",
]


def catalog() -> list[dict]:
    """Detached, public-safe metadata from the same JSON used by TypeScript."""
    return deepcopy(_CATALOG)


def create_controller(controller_id: str, settings: dict | None = None):
    """Create fresh per-trial state, accepting only reviewed IDs and data settings."""
    if not isinstance(controller_id, str) or controller_id not in PROFILE_IDS:
        raise ValueError("Unknown reviewed controller ID")
    if settings is None:
        settings = {}
    if not isinstance(settings, Mapping) or any(not isinstance(key, str) for key in settings):
        raise ValueError("Controller settings must be a mapping with string keys")
    settings = deepcopy(dict(settings))
    allowed = (
        {"position_gain_s2", "velocity_gain_s", "avoidance_range_m", "avoidance_accel_mps2"}
        if controller_id == "basic_pd_radar"
        else set()
    )
    if set(settings) - allowed:
        raise ValueError("Unsupported settings for this fixed reviewed controller profile")
    if controller_id in ("baseline", "body-axis"):
        # Checkpoint also imports this catalog. Defer this import until its
        # module is loaded, and return the original class/report unwrapped.
        from ..checkpoint import policy_config
        from ..stp_policy import STPCheckpointController

        return STPCheckpointController(policy_config(controller_id))
    metadata = next(entry for entry in _CATALOG if entry["id"] == controller_id)
    if controller_id == "bui_alpha_beta_mvp":
        from ._bui import BuiController

        return BuiController(metadata)
    if controller_id == "tom_kalman_velocity_mvp":
        from ._tom import TomController

        return TomController(metadata)
    if controller_id in ("ali_official_profile", "official_basic_beeline"):
        from ._official import OfficialController

        return OfficialController(metadata)
    from ..control_contract import scalar
    from ._simple import SimpleController

    for key, value in settings.items():
        settings[key] = scalar(value, key, positive=key == "avoidance_range_m")
    if controller_id == "userapi_body_z":
        from ._sources import require_template_source

        require_template_source()
    return SimpleController(metadata, settings)


def sample_from_observation(observation, *, source_kind, retain_observation=False):
    """Copy actual ready native public samples; see the producer's full contract."""
    from .observation import sample_from_observation as produce

    return produce(observation, source_kind=source_kind, retain_observation=retain_observation)


class _ObservationPolicy:
    def __init__(self, controller_id, settings):
        self.controller = create_controller(controller_id, settings)
        self._retain_observation = controller_id == "ali_official_profile"
        self._context = None
        self._last_time = None
        self._last_sample_time = None
        self._last_radar_time = None
        self._invalidated = False

    @property
    def report(self):
        return self.controller.report

    def __call__(self, observation) -> Decision:
        from ..experiment import Decision

        sample = sample_from_observation(
            observation,
            source_kind="native_manual_public_observation",
            retain_observation=self._retain_observation,
        )
        context = (sample.frame, sample.rotation_convention, sample.public_context)
        now = observation["time_s"]
        if (
            self._invalidated
            or (self._context is not None and context != self._context)
            or (self._last_time is not None and now < self._last_time)
            or (
                self._last_sample_time is not None
                and sample.elapsed_time_s < self._last_sample_time
            )
            or (
                self._last_radar_time is not None
                and sample.radar_sample_time_s < self._last_radar_time
            )
        ):
            self._invalidated = True
            raise ValueError("Public trial context/clock changed; construct a fresh policy")
        self._context, self._last_time = context, now
        self._last_sample_time = sample.elapsed_time_s
        self._last_radar_time = sample.radar_sample_time_s
        accel, angular = self.controller.command(
            sample, observation["radar"], now_elapsed_time_s=now
        )
        report = self.controller.report
        if "eligible" in report:
            eligible = report["eligible"] is True
        else:
            from ..checkpoint import policy_decision_eligible

            eligible = policy_decision_eligible(report, accel, angular, now)
        if not eligible:
            raise ValueError("The selected controller has no eligible decision for this sample")
        return Decision(accel, angular)


def policy_factory(controller_id: str, settings: dict | None = None):
    """Return one observation-to-Decision policy. Call anew for every sweep trial.

    For a sweep registry use ``lambda settings: policy_factory(id, settings)``.
    This uses native manual public observations and never checkpoint.run_trial.
    """
    return _ObservationPolicy(controller_id, settings)
