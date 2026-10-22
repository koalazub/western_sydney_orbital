from __future__ import annotations

from wsorb.cli import main


def test_scenario_cli_success(capsys) -> None:
    assert main(["scenario", "show", "--json"]) == 0
    assert '"id": "lunar_sar"' in capsys.readouterr().out


def test_scenario_cli_error(capsys) -> None:
    assert main(["scenario", "show", "--scenario", "missing-scenario"]) == 2
    assert "unknown scenario" in capsys.readouterr().err
