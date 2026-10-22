from __future__ import annotations

from dataclasses import replace

from wsorb.report import render_campaign_markdown


def test_campaign_report_uses_the_actual_command(campaign_result) -> None:
    command = "uv run --locked wsorb campaign run --scenario scenarios/custom.toml"
    report = render_campaign_markdown(replace(campaign_result, command=command))
    assert report.count(command) == 1
    assert "--scenario lunar_sar" not in report
    assert "successful runs only" in report


def test_campaign_report_does_not_invent_a_command(campaign_result) -> None:
    report = render_campaign_markdown(campaign_result)
    assert "## Recorded command" not in report
    assert "uv run" not in report
