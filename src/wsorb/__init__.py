"""Western Sydney Orbital — a Space Teams PRO campaign lab.

The package provides a deterministic, dependency-light surrogate for the
surface-search mechanics documented in the Space Teams PRO Python API. It is
designed for strategy iteration: run many simulated missions, compare policies,
record what changed, and export the winning plan for a real STP Mission Manager.
"""

from wsorb.model import (
    AssetSpec,
    AssetState,
    CampaignResult,
    Event,
    RunResult,
    Scenario,
    Strategy,
)
from wsorb.scenario import default_scenario, load_scenario
from wsorb.simulation import run_simulation

__all__ = [
    "AssetSpec",
    "AssetState",
    "CampaignResult",
    "Event",
    "RunResult",
    "Scenario",
    "Strategy",
    "default_scenario",
    "load_scenario",
    "run_simulation",
]

__version__ = "0.1.0"
