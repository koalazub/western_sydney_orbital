"""Balanced scenario contrasts must preserve censoring and separate additive effects."""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "stp_analysis", Path(__file__).resolve().parents[1] / "docs/controllers/stp_analysis.py"
)
_analysis = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_analysis)
seed_sensitivity = _analysis.seed_sensitivity


def flight(controller, seed, score, repetition=0):
    return dict(
        controller=controller,
        seed=seed,
        repetition=repetition,
        official_score=score,
        outcome="success" if score is not None else "simulation_limit",
        arrival_time_s=120 if score is not None else None,
    )


def test_additive_difficulty_is_removed_and_repeats_and_incomplete_seeds_excluded():
    rows = [
        flight("a", 1, 10),
        flight("b", 1, 20),
        flight("a", 2, 30),
        flight("b", 2, 40),
        flight("a", 3, -999),
        flight("a", 1, -999, repetition=1),
    ]
    summary, effects = seed_sensitivity(rows, ["a", "b"])
    assert all(r["interaction_points"] == 0 for r in effects)
    assert all(r["common_seeds"] == 2 for r in summary)
    assert summary[0]["score_iqr"] == 10
    assert summary[0]["score_sd"] == pytest.approx(2**0.5 * 10)


def test_crossing_response_and_missing_scores():
    rows = [
        flight("a", 1, 10),
        flight("b", 1, 30),
        flight("a", 2, 30),
        flight("b", 2, 10),
        flight("a", 3, None),
        flight("b", 3, 50),
    ]
    summary, effects = seed_sensitivity(rows, ["a", "b"])
    assert [r["interaction_points"] for r in effects] == [-10, 10, 10, -10]
    assert all(r["jointly_scored_seeds"] == 2 for r in summary)
    assert summary[0]["timeouts"] == 1
    assert summary[0]["scored_seeds"] == 2
    assert summary[1]["scored_seeds"] == 3
    assert summary[0]["interaction_sd"] == pytest.approx(2**0.5 * 10)


def test_empty_singleton_and_duplicate_cells():
    summary, effects = seed_sensitivity([], ["a", "b"])
    assert effects == []
    assert all(r["score_sd"] is None for r in summary)
    rows = [flight("a", 1, None), flight("b", 1, 10)]
    summary, effects = seed_sensitivity(rows, ["a", "b"])
    assert effects == []
    assert summary[1]["score_sd"] is None
    with pytest.raises(ValueError, match="one first-pass flight"):
        seed_sensitivity([*rows, rows[0]], ["a", "b"])
