"""Checks that the calibrated SVG and playback data come from committed rows."""

from __future__ import annotations

import json
from pathlib import Path

from wsorb.calibrated_comparison_chart import load_trials, render_svg, summarize

ROOT = Path(__file__).resolve().parents[1]
PUBLICATION = ROOT / "docs/analytics/official-comparison"
TRIALS = PUBLICATION / "calibrated-trials.csv"


def test_calibrated_trials_cover_every_profile_and_seed() -> None:
    rows = load_trials(TRIALS)
    assert len(rows) == 20
    summary = summarize(rows)
    assert summary["ali_mppi"]["arrivals"] == 5
    assert summary["tom_polyfit_mvp"]["arrivals"] == 5
    assert summary["bui_alpha_beta_mvp"]["arrivals"] == 3
    assert summary["basic_pd_radar"]["arrivals"] == 3
    assert summary["bui_alpha_beta_mvp"]["damage_runs"] == 2
    assert summary["basic_pd_radar"]["damage_runs"] == 1


def test_committed_svg_is_the_current_render() -> None:
    rendered = render_svg(load_trials(TRIALS))
    assert (PUBLICATION / "calibrated-performance.svg").read_text() == rendered
    for expected in ("Calibrated v0.56 local benchmark", "61.6", "60.9", "5/5", "3/5"):
        assert expected in rendered
    assert "not STP" in rendered


def test_calibrated_trials_are_machine_readable() -> None:
    rows = load_trials(TRIALS)
    assert {row["controller"] for row in rows} == {
        "ali_mppi",
        "tom_polyfit_mvp",
        "bui_alpha_beta_mvp",
        "basic_pd_radar",
    }
    assert json.loads(json.dumps(rows)) == [dict(row) for row in rows]
