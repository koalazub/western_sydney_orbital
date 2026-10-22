"""Shared fixtures for the wsorb test-suite."""

from __future__ import annotations

from dataclasses import replace

import pytest

from wsorb.campaign import run_campaign
from wsorb.model import CampaignResult, Scenario, Strategy
from wsorb.scenario import load_scenario
from wsorb.simulation import build_scenario_terrain
from wsorb.terrain import Terrain


@pytest.fixture(scope="session")
def scenario() -> Scenario:
    return load_scenario("lunar_sar")


@pytest.fixture(scope="session")
def terrain(scenario: Scenario) -> Terrain:
    return build_scenario_terrain(scenario, seed=7)


@pytest.fixture(scope="session")
def campaign_result(scenario: Scenario) -> CampaignResult:
    return run_campaign(
        replace(scenario, horizon_s=15),
        [Strategy("test")],
        replicates=1,
        campaign_id="test-campaign",
    )
