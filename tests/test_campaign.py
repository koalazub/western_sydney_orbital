from __future__ import annotations

import json
from dataclasses import replace

import pytest

from wsorb.campaign import load_campaign, resolve_strategies, run_campaign, write_campaign
from wsorb.model import Strategy
from wsorb.scenario import default_scenario


def test_campaign_pairs_draws_and_serializes_scenario(tmp_path) -> None:
    scenario = replace(default_scenario(), horizon_s=15)
    strategies = [Strategy("sector", "sector"), Strategy("sweep", "sweep")]
    result = run_campaign(scenario, strategies, replicates=1, seed=9, campaign_id="paired")
    assert len(result.runs) == 2
    assert result.runs[0].target == result.runs[1].target
    assert result.runs[0].terrain_seed == result.runs[1].terrain_seed
    payload = result.to_dict()
    assert payload["scenario"]["world_margin_m"] == scenario.world_margin_m
    written = write_campaign(result, tmp_path)
    text = (written / "campaign.json").read_text()
    serialized = json.loads(text)
    expected_target = [round(result.runs[0].target.x, 2), round(result.runs[0].target.y, 2)]
    assert [run["target"] for run in serialized["runs"]] == [expected_target] * 2


def test_existing_campaign_is_not_overwritten(tmp_path, campaign_result) -> None:
    written = write_campaign(campaign_result, tmp_path)
    original = {path.name: path.read_bytes() for path in written.iterdir()}
    with pytest.raises(FileExistsError):
        write_campaign(replace(campaign_result, command="changed"), tmp_path)
    assert {path.name: path.read_bytes() for path in written.iterdir()} == original


@pytest.mark.parametrize(
    "campaign_id", ["../outside", "/absolute", r"..\outside", "", ".", "C:other"]
)
def test_campaign_id_cannot_escape_output_directory(tmp_path, campaign_result, campaign_id) -> None:
    with pytest.raises(ValueError, match="single filename"):
        write_campaign(replace(campaign_result, campaign_id=campaign_id), tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "names", [["sector-baseline"] * 2, ["all", "bad"], ["all", "sector-baseline"]]
)
def test_ambiguous_strategy_selection_is_rejected(names) -> None:
    with pytest.raises(ValueError):
        resolve_strategies(names)


def test_duplicate_strategy_names_cannot_inflate_campaign_runs(scenario) -> None:
    with pytest.raises(ValueError, match="unique"):
        run_campaign(scenario, [Strategy("same"), Strategy("same", pattern="sweep")], replicates=1)


@pytest.mark.parametrize("payload", [None, 42, "summaries runs", [], {"summaries": [], "runs": []}])
def test_malformed_campaign_is_rejected(tmp_path, payload) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="valid campaign"):
        load_campaign(path)


@pytest.mark.parametrize(
    "field,value",
    [("runs", "24"), ("successes", True), ("mean_coverage", 2), ("median_report_s", "soon")],
)
def test_malformed_summary_is_rejected(tmp_path, campaign_result, field, value) -> None:
    payload = campaign_result.to_dict()
    payload["summaries"][0][field] = value
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="valid campaign"):
        load_campaign(path)


def test_saved_campaign_round_trips(tmp_path, campaign_result) -> None:
    directory = write_campaign(campaign_result, tmp_path)
    assert load_campaign(directory / "campaign.json") == json.loads(
        json.dumps(campaign_result.to_dict())
    )
