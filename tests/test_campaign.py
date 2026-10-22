from __future__ import annotations

from wsorb.campaign import run_campaign, write_campaign
from wsorb.model import Strategy
from wsorb.scenario import default_scenario


def test_campaign_pairs_draws_and_serializes_scenario(tmp_path) -> None:
    scenario = default_scenario()
    strategies = [Strategy("sector", "sector"), Strategy("sweep", "sweep")]
    result = run_campaign(scenario, strategies, replicates=1, seed=9, campaign_id="paired")
    assert len(result.runs) == 2
    assert result.runs[0].target == result.runs[1].target
    assert result.runs[0].terrain_seed == result.runs[1].terrain_seed
    payload = result.to_dict()
    assert payload["scenario"]["world_margin_m"] == scenario.world_margin_m
    written = write_campaign(result, tmp_path)
    text = (written / "campaign.json").read_text()
    import json

    serialized = json.loads(text)
    expected_target = [round(result.runs[0].target.x, 2), round(result.runs[0].target.y, 2)]
    assert [run["target"] for run in serialized["runs"]] == [expected_target] * 2
