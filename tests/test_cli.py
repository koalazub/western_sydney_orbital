from __future__ import annotations

import json
import shlex

import pytest

from wsorb.cli import main


def test_scenario_cli_success(capsys) -> None:
    assert main(["scenario", "show", "--json"]) == 0
    assert '"id": "lunar_sar"' in capsys.readouterr().out


def test_scenario_cli_error(capsys) -> None:
    assert main(["scenario", "show", "--scenario", "missing-scenario"]) == 2
    assert "unknown scenario" in capsys.readouterr().err


def test_campaign_records_a_portable_uv_command(tmp_path) -> None:
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
    assert main(argv) == 0
    path = next((tmp_path / "shared results").glob("*/campaign.json"))
    command = json.loads(path.read_text())["command"]
    assert shlex.split(command) == ["uv", "run", "--locked", "wsorb", *argv]
    assert ".venv" not in command


def test_partial_target_returns_a_usage_error(capsys) -> None:
    assert main(["sim", "run", "--target-x", "10"]) == 2
    assert "provide both" in capsys.readouterr().err


def test_root_help_advertises_api_checkpoint_validation(capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["--help"])
    assert stopped.value.code == 0
    assert "checkpoint" in capsys.readouterr().out


def test_checkpoint_help_preserves_arguments_and_exposes_explicit_api_consent(capsys) -> None:
    arguments = ["checkpoint", "--help"]
    with pytest.raises(SystemExit) as stopped:
        main(arguments)
    assert stopped.value.code == 0
    assert arguments == ["checkpoint", "--help"]
    output = capsys.readouterr().out
    assert all(flag in output for flag in ("--isolated", "--connection-file", "--run", "--policy"))


def test_checkpoint_cli_without_explicit_scope_fails_before_starting_a_server(capsys) -> None:
    assert main(["checkpoint"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
