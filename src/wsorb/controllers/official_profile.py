"""Ali's official-profile policy factory, shared by the sweep and entrypoint."""

from __future__ import annotations

from collections.abc import Mapping
from time import monotonic
from typing import Any

import numpy as np
import numpy.typing as npt

from ..flight import ResilientGuidance

POLICY_ID = "ali_official_profile"


class OfficialProfilePolicy:
    """Original callable policy with its guidance eligibility receipt exposed."""

    def __init__(self) -> None:
        self._guidance = ResilientGuidance()

    @property
    def report(self) -> dict[str, Any]:
        return self._guidance.report

    def __call__(
        self, observation: Mapping[str, object]
    ) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
        readiness = self._guidance.update(
            observation,
            wall_time_s=monotonic(),
            running=True,
        )
        if not readiness.can_command:
            return np.zeros(3), np.zeros(3)
        return self._guidance.command(readiness.observation, None, {})


def official_profile_policy(
    settings: Mapping[str, object] | None = None,
) -> OfficialProfilePolicy:
    """Return one observation-to-command policy for the official profile.

    The implementation is the same resilient official-profile controller used by
    the real-WASM arrival regression. The factory creates fresh guidance state for
    each trial. No settings are accepted: the published 10 m/s beeline and the
    existing safety caps are not tunable here.
    """
    if settings:
        raise ValueError("The official profile takes no settings")
    return OfficialProfilePolicy()
