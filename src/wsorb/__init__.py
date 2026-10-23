"""Western Sydney Orbital practice simulations.

Practice simulations, paired strategy comparisons, reports and team notes.
This package does not include the Space Teams PRO runtime or SDK. The optional
stp_api adapter calls the real SDK inside a licensed ControllerSystem process;
the browser practice engine remains separate.
"""

from importlib.metadata import version

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

__version__ = version("western-sydney-orbital")
