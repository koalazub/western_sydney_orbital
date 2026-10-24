"""Shared fixtures for the wsorb test-suite."""

from __future__ import annotations

from dataclasses import replace

import pytest

from wsorb.campaign import run_campaign
from wsorb.model import CampaignResult, Scenario, Strategy
from wsorb.scenario import load_scenario


@pytest.fixture(scope="session")
def scenario() -> Scenario:
    return load_scenario("lunar_sar")


@pytest.fixture(scope="session")
def campaign_result(scenario: Scenario) -> CampaignResult:
    return run_campaign(
        replace(scenario, horizon_s=15),
        [Strategy("test")],
        replicates=1,
        campaign_id="test-campaign",
    )
