"""Shared fixtures for the wsorb test-suite."""

from __future__ import annotations

import pytest

from wsorb.model import Scenario
from wsorb.scenario import load_scenario
from wsorb.simulation import build_scenario_terrain
from wsorb.terrain import Terrain


@pytest.fixture(scope="session")
def scenario() -> Scenario:
    return load_scenario("lunar_sar")


@pytest.fixture(scope="session")
def terrain(scenario: Scenario) -> Terrain:
    return build_scenario_terrain(scenario, seed=7)
