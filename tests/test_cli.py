from __future__ import annotations

import json
import shlex
import sys

import pytest

from wsorb.cli import main


def test_scenario_cli_success(capsys) -> None:
    assert main(["scenario", "show", "--json"]) == 0
    assert '"id": "lunar_sar"' in capsys.readouterr().out


def test_scenario_cli_error(capsys) -> None:
    assert main(["scenario", "show", "--scenario", "missing-scenario"]) == 2
    assert "unknown scenario" in capsys.readouterr().err


@pytest.mark.parametrize("explicit_argv", [True, False])
def test_campaign_records_a_portable_uv_command(tmp_path, monkeypatch, explicit_argv) -> None:
    argv = [
        "campaign",
        "run",
        "--replicates",
        "1",
        "--strategies",
        "sweep-baseline",
        "--out",
        str(tmp_path / "shared results"),
    ]
    monkeypatch.setattr(sys, "argv", [str(tmp_path / ".venv/bin/wsorb"), *argv])
    assert main(argv if explicit_argv else None) == 0
    path = next((tmp_path / "shared results").glob("*/campaign.json"))
    command = json.loads(path.read_text())["command"]
    assert shlex.split(command) == ["uv", "run", "--locked", "wsorb", *argv]
    assert ".venv" not in command


def test_json_stays_parseable_when_writing_simulation_files(
    tmp_path, monkeypatch, campaign_result, capsys
) -> None:
    monkeypatch.setattr("wsorb.cli.run_simulation", lambda *args, **kwargs: campaign_result.runs[0])
    main(
        [
            "sim",
            "run",
            "--json",
            "--summary-out",
            str(tmp_path / "summary.md"),
            "--plan-out",
            str(tmp_path / "plan.json"),
        ]
    )
    output = capsys.readouterr()
    assert json.loads(output.out)["scenario_id"] == campaign_result.scenario.id
    assert "report:" in output.err and "plan:" in output.err


def test_json_stays_parseable_with_notebook_output(
    tmp_path, monkeypatch, campaign_result, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("wsorb.cli.run_campaign", lambda *args, **kwargs: campaign_result)
    assert main(["campaign", "run", "--json", "--notebook"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["campaign_id"] == campaign_result.campaign_id
    assert "notebook:" in output.err


def test_partial_target_returns_a_usage_error(capsys) -> None:
    assert main(["sim", "run", "--target-x", "10"]) == 2
    assert "provide both" in capsys.readouterr().err


def test_output_errors_do_not_raise_a_traceback(
    tmp_path, monkeypatch, campaign_result, capsys
) -> None:
    monkeypatch.setattr("wsorb.cli.run_simulation", lambda *args, **kwargs: campaign_result.runs[0])
    assert main(["sim", "run", "--summary-out", str(tmp_path)]) == 2
    assert "wsorb:" in capsys.readouterr().err
