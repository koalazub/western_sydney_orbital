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
