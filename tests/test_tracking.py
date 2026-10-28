"""Numeric contracts only: no SDK, network, native wrapper or flight-safety test."""

import ast
import json
import sys
from dataclasses import FrozenInstanceError, replace
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import numpy as np
import pytest

# Load the sibling file itself. Importing wsorb or flight is not needed by the
# licensed Python 3.10+ bundle, even though this repository's test shell is 3.13.
TRACKING_PATH = Path(__file__).parents[1] / "src" / "wsorb" / "tracking.py"
_SPEC = spec_from_file_location("portable_tracking_contract", TRACKING_PATH)
tracking = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = tracking
_SPEC.loader.exec_module(tracking)
TrackerConfig = tracking.TrackerConfig
RadarTracker = tracking.RadarTracker


def row(time=0.0, position=(100.0, 0.0, 0.0), rate=0.0, closest=99.0, diameter=2.0):
    return np.array([position, [closest, rate, diameter], [time, 0.0, 0.0]], dtype=float)


def update(tracker, now, *rows, position=(0.0, 0.0, 0.0), velocity=(0.0, 0.0, 0.0), **kwargs):
    return tracker.update(now, position, velocity, list(rows), frame="Phobos", **kwargs)


def only(report):
    assert report["counts"]["active"] == 1
    return report["tracks"][0]


def covariance(report):
    return np.array(only(report)["covariance"])


def assert_psd(matrix):
    matrix = np.array(matrix)
    np.testing.assert_allclose(matrix, matrix.T, atol=1e-9)
    assert np.linalg.eigvalsh(matrix)[0] >= -1e-8


def test_portable_module_has_python310_syntax_and_only_numpy_or_standard_imports():
    tree = ast.parse(TRACKING_PATH.read_text(), feature_version=(3, 10))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            roots.add(node.module.split(".")[0])
    assert roots <= {
        "__future__",
        "collections",
        "copy",
        "dataclasses",
        "itertools",
        "numbers",
        "typing",
        "numpy",
    }
    assert RadarTracker.__module__ == "portable_tracking_contract"


def test_configuration_is_frozen_and_noise_floors_have_revision_specific_provenance():
    config = TrackerConfig()
    with pytest.raises(FrozenInstanceError):
        config.acceleration_psd_m2_s3 = 0.0
    report = RadarTracker(config).coast(0.0, frame="Phobos")
    assert "c1e3fc8" in report["model"]["noise_provenance"]
    assert "not universal" in report["model"]["noise_provenance"]
    assert report["model"]["configuration"]["position_noise_std_m"] == 2.0
    assert report["diagnostic_only"]
    for key in (
        "clearance_granted",
        "physical_identity_proven",
        "calibrated_probabilities",
        "raw_obstacle_envelopes_modified",
    ):
        assert report[key] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"position_noise_std_m": 1.0},
        {"range_rate_noise_std_mps": 0.01},
        {"diameter_noise_fraction": 0.01},
        {"acceleration_psd_m2_s3": -1.0},
        {"ego_position_std_m": True},
        {"initial_velocity_std_mps": 0.0},
        {"confirm_hits": 1},
        {"max_tracks": True},
        {"max_tracks": 257},
        {"report_limit": 17},
        {"max_fingerprints": 1.0},
        {"association_nis_gate": np.inf},
        {"delete_after_s": 1.0},
        {"fingerprint_history_s": 1.0},
        {"max_input_rows": 2},
    ],
)
def test_invalid_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        TrackerConfig(**changes)


def test_continuous_white_acceleration_Q_and_F_compose_including_cross_covariance():
    transition = tracking._transition
    process = tracking._process_covariance
    a, b, psd = 0.3, 1.7, 0.8
    np.testing.assert_allclose(transition(a + b), transition(b) @ transition(a))
    np.testing.assert_allclose(
        process(a + b, psd),
        transition(b) @ process(a, psd) @ transition(b).T + process(b, psd),
        atol=1e-14,
    )
    np.testing.assert_allclose(process(2.0, psd)[:3, 3:], np.eye(3) * psd * 2.0)
    assert_psd(process(a, psd))


def test_repeated_poll_is_idempotent_and_does_not_use_new_ownship_pose_as_old_measurement():
    tracker = RadarTracker()
    raw = row(rate=1.0)
    first = update(tracker, 0.0, raw)
    repeated = update(tracker, 0.0, raw, position=(10.0, 20.0, 30.0))
    assert first["tracks"] == repeated["tracks"]
    assert repeated["event"]["discarded"]["repeated_raw_row"] == 1
    expected_coast = tracker.coast(0.5, frame="Phobos")
    later_poll = update(tracker, 0.5, raw, position=(900.0, 800.0, 700.0))
    assert expected_coast["tracks"] == later_poll["tracks"]
    assert only(later_poll)["genuinely_new_timestamp_hits"] == 1
    assert not only(later_poll)["confirmed"]


def test_changed_row_at_same_source_time_is_not_an_independent_hit_or_correction():
    tracker = RadarTracker()
    update(tracker, 0.0, row())
    expected = tracker.coast(0.1, frame="Phobos")
    result = update(tracker, 0.1, row(position=(100.01, 0.0, 0.0)))
    assert result["tracks"] == expected["tracks"]
    assert result["event"]["discarded"]["same_source_time"] == 1


def test_mixed_publication_times_only_update_the_target_with_new_evidence():
    tracker = RadarTracker()
    a = row(10.0, rate=1.0)
    b = row(9.5, position=(1000.0, 0.0, 0.0))
    first = update(tracker, 10.0, a, b)
    b_id = next(t["id"] for t in first["tracks"] if t["last_raw_source_time_s"] == 9.5)
    before = tracker.coast(10.5, frame="Phobos")
    second = update(tracker, 10.5, b, row(10.5, position=(100.5, 0.0, 0.0), rate=1.0), a)
    before_b = next(t for t in before["tracks"] if t["id"] == b_id)
    after_b = next(t for t in second["tracks"] if t["id"] == b_id)
    assert after_b == before_b
    assert sorted(t["genuinely_new_timestamp_hits"] for t in second["tracks"]) == [1, 2]
    assert second["event"]["associated_rows"] == 1
    third = update(tracker, 11.0, b, row(11.0, position=(101.0, 0.0, 0.0), rate=1.0))
    assert third["counts"]["confirmed"] == 1
    assert next(t for t in third["tracks"] if t["id"] == b_id)["source_age_s"] == 1.5


def test_a_newer_other_target_or_report_does_not_block_an_asynchronous_row():
    tracker = RadarTracker()
    update(tracker, 0.0, row(), row(position=(1000.0, 0.0, 0.0)))
    update(tracker, 3.0, row(3.0), row(1.0, position=(1000.0, 0.0, 0.0)))
    result = update(tracker, 3.2, row(2.0, position=(1000.0, 0.0, 0.0)))
    assert result["event"]["associated_rows"] == 1
    assert sorted(t["last_aligned_source_time_s"] for t in result["tracks"]) == [2.0, 3.0]
    assert result["counts"]["confirmed"] == 1


def test_source_order_and_row_permutation_do_not_change_posteriors_or_local_ids():
    rows = [
        row(0.0),
        row(0.0, position=(1000.0, 0.0, 0.0)),
        row(0.2, position=(100.2, 0.0, 0.0), rate=1.0),
        row(0.4, position=(100.4, 0.0, 0.0), rate=1.0),
    ]
    first = update(RadarTracker(), 0.4, *rows)
    second = update(RadarTracker(), 0.4, *reversed(rows))
    assert first == second
    assert sorted(t["genuinely_new_timestamp_hits"] for t in first["tracks"]) == [1, 3]


def test_missing_rows_coast_with_full_covariance_growth_and_poll_frequency_independence():
    one_poll, many_polls = RadarTracker(), RadarTracker()
    first = update(one_poll, 0.0, row())
    update(many_polls, 0.0, row())
    prior = covariance(first)
    for time in (0.1, 0.3, 0.8, 1.4):
        many_polls.coast(time, frame="Phobos")
    result = one_poll.coast(2.0, frame="Phobos")
    other = update(many_polls, 2.0)
    assert result["tracks"] == other["tracks"]
    transition = tracking._transition(2.0)
    expected = transition @ prior @ transition.T + tracking._process_covariance(2.0, 0.25)
    np.testing.assert_allclose(covariance(result), expected, atol=1e-9)
    assert np.all(np.diag(covariance(result)) > np.diag(prior))
    assert np.max(np.abs(covariance(result)[:3, 3:])) > 100.0
    assert only(result)["status"] == "coasting"
    assert only(result)["genuinely_new_timestamp_hits"] == 1
    assert_psd(covariance(result))


def test_innovation_H_R_and_Joseph_update_match_the_reported_numeric_model():
    tracker = RadarTracker()
    first = update(tracker, 0.0, row())
    result = update(tracker, 1.0, row(1.0, position=(101.0, 0.0, 0.0), rate=0.5))
    track = only(result)
    diagnostic = track["last_assimilation"]
    transition = tracking._transition(1.0)
    prior = transition @ covariance(first) @ transition.T + tracking._process_covariance(1.0, 0.25)
    h = np.array(diagnostic["H"])
    r = np.array(diagnostic["R"])
    s = h @ prior @ h.T + r
    gain = np.linalg.solve(s, h @ prior).T
    complement = np.eye(6) - gain @ h
    expected = complement @ prior @ complement.T + gain @ r @ gain.T
    np.testing.assert_allclose(covariance(result), expected, atol=1e-9)
    np.testing.assert_allclose(diagnostic["innovation"], [1.0, 0.0, 0.0, 0.5])
    np.testing.assert_allclose(diagnostic["innovation_covariance"], s, atol=1e-9)
    np.testing.assert_allclose(h[3, 3:], [1.0, 0.0, 0.0])
    assert diagnostic["innovation_nis_not_probability"] > 0.0
    assert abs(diagnostic["post_update_residual"][0]) < 1.0
    assert_psd(r)
    assert_psd(expected)


def test_source_time_ego_backdating_and_uncertainty_are_explicit_not_an_old_pose_claim():
    tracker = RadarTracker()
    relative = np.array([50.0, 40.0, 30.0])
    velocity = np.array([4.0, 2.0, -1.0])
    rate = -float(relative / np.linalg.norm(relative) @ velocity)
    raw = row(3.0, position=relative, rate=rate)
    original = raw.copy()
    result = update(tracker, 5.0, raw, position=(100.0, 30.0, 10.0), velocity=velocity)
    track = only(result)
    diagnostic = track["last_assimilation"]
    np.testing.assert_allclose(diagnostic["ownship_position_cv_estimate_world_m"], [92, 26, 12])
    np.testing.assert_allclose(track["state"][:3], [142, 66, 42], atol=1e-8)
    assert diagnostic["ownship_backdating_s"] == 2.0
    ego = np.array(diagnostic["ownship_covariance_at_source"])
    assert ego[0, 0] > 1.0
    assert ego[0, 3] < 0.0
    assert ego[3, 3] > 0.25
    h = np.array(diagnostic["H"])
    base_r = np.diag([4.0, 4.0, 4.0, 0.02**2])
    np.testing.assert_allclose(diagnostic["R"], base_r + h @ ego @ h.T)
    assert np.linalg.norm(h[3, :3]) > 0.0
    assert_psd(ego)
    np.testing.assert_array_equal(raw, original)


def test_explicit_clock_offset_keeps_raw_and_aligned_times_and_does_not_infer_one():
    raw = row(5.0)
    no_offset = update(RadarTracker(), 100.0, raw)
    assert no_offset["counts"]["active"] == 0
    assert no_offset["event"]["discarded"]["stale_source_time"] == 1
    tracker = RadarTracker()
    result = update(tracker, 100.0, raw, radar_clock_offset_s=95.0)
    track = only(result)
    assert track["last_raw_source_time_s"] == 5.0
    assert track["last_aligned_source_time_s"] == 100.0
    assert track["last_assimilation"]["raw_radar_row"][2] == [5.0, 0.0, 0.0]
    repeated = update(tracker, 100.5, raw, radar_clock_offset_s=95.0)
    assert only(repeated)["genuinely_new_timestamp_hits"] == 1
    changed = update(tracker, 101.0, raw, radar_clock_offset_s=96.0)
    assert changed["event"]["reset_reasons"] == ["radar_clock_offset_changed"]
    assert changed["generation"] == result["generation"] + 1
    assert only(changed)["id"] != track["id"]
    np.testing.assert_array_equal(raw, row(5.0))


def test_radial_rate_does_not_measure_unknown_tangential_velocity():
    tracker = RadarTracker()
    result = update(tracker, 0.0, row(rate=3.0))
    track = only(result)
    state, cov = np.array(track["state"]), covariance(result)
    assert state[3] == pytest.approx(3.0, abs=0.001)
    np.testing.assert_array_equal(state[4:], [0.0, 0.0])
    assert cov[3, 3] < 1.0
    np.testing.assert_allclose(np.diag(cov)[4:], [2500.0, 2500.0])
    assert "prior_mean_not_observation" in track["velocity_information"]
    assert "not_fused" in result["model"]


def test_closest_range_and_diameter_are_not_fabricated_centre_or_identity_measurements():
    first = update(RadarTracker(), 0.0, row(closest=3.0, diameter=2.0))
    second = update(RadarTracker(), 0.0, row(closest=-20.0, diameter=50.0))
    np.testing.assert_array_equal(only(first)["state"], only(second)["state"])
    np.testing.assert_array_equal(covariance(first), covariance(second))
    assert only(second)["last_assimilation"]["raw_radar_row"][1] == [-20.0, 0.0, 50.0]


def test_unambiguous_associations_are_one_to_one_and_confirm_on_new_timestamps():
    tracker = RadarTracker(TrackerConfig(confirm_hits=2))
    first = update(tracker, 0.0, row(), row(position=(700.0, 0.0, 0.0)))
    result = update(
        tracker,
        0.1,
        row(0.1, position=(100.05, 0.0, 0.0)),
        row(0.1, position=(700.02, 0.0, 0.0)),
    )
    assert [t["id"] for t in first["tracks"]] == [t["id"] for t in result["tracks"]]
    assert result["event"]["associated_rows"] == 2
    assert result["counts"]["confirmed"] == 2
    assert result["event"]["ambiguous_rows"] == 0
    assert all(t["genuinely_new_timestamp_hits"] == 2 for t in result["tracks"])
    assert all(t["confirmation_age_s"] == 0.0 for t in result["tracks"])


def test_ambiguous_crossing_refuses_corrections_confirmation_and_forced_identity():
    tracker = RadarTracker(TrackerConfig(confirm_hits=2))
    initial = update(tracker, 0.0, row(), row(position=(120.0, 0.0, 0.0)))
    crossing = [row(0.1, position=(110.0, 0.0, 0.0)), row(0.1, position=(110.1, 0.0, 0.0))]
    result = update(tracker, 0.1, *crossing)
    assert result["counts"]["active"] == 2
    assert result["counts"]["association_ambiguous"] == 2
    assert result["event"]["ambiguous_rows"] == 2
    assert result["event"]["associated_rows"] == 0
    assert result["counts"]["confirmed"] == 0
    assert all(t["genuinely_new_timestamp_hits"] == 1 for t in result["tracks"])
    assert [t["id"] for t in result["tracks"]] == [t["id"] for t in initial["tracks"]]
    repeated = update(tracker, 0.1, *reversed(crossing))
    assert repeated["tracks"] == result["tracks"]
    assert repeated["event"]["discarded"]["repeated_raw_row"] == 2
    assert not result["physical_identity_proven"]


def test_late_row_is_reported_and_never_rewinds_or_creates_an_extra_gated_track():
    tracker = RadarTracker()
    update(tracker, 0.0, row(rate=1.0))
    update(tracker, 1.0, row(1.0, position=(101.0, 0.0, 0.0), rate=1.0))
    before = tracker.coast(1.2, frame="Phobos")
    result = update(tracker, 1.2, row(0.5, position=(100.5, 0.0, 0.0), rate=1.0))
    assert result["tracks"] == before["tracks"]
    assert result["event"]["discarded"]["late_source_time"] == 1
    assert only(result)["last_aligned_source_time_s"] == 1.0


def test_deadline_deletion_uses_source_age_and_never_reuses_deleted_ids():
    tracker = RadarTracker()
    initial = update(tracker, 4.0, row())
    old_id = only(initial)["id"]
    assert only(initial)["deletion_deadline_s"] == 10.0
    assert tracker.coast(9.999, frame="Phobos")["counts"]["active"] == 1
    deleted = tracker.coast(10.0, frame="Phobos")
    assert deleted["counts"]["active"] == 0
    assert deleted["event"]["deleted_ids"] == [old_id]
    reappeared = update(tracker, 10.0, row(10.0))
    assert only(reappeared)["id"] != old_id
    assert only(reappeared)["genuinely_new_timestamp_hits"] == 1
    assert not reappeared["physical_identity_proven"]


def test_frame_change_and_clock_rewind_reset_history_generation_and_confirmation():
    tracker = RadarTracker()
    first = update(tracker, 1.0, row(1.0))
    changed = tracker.coast(1.0, frame="other-Phobos-frame")
    assert changed["event"]["reset_reasons"] == ["frame_changed"]
    assert changed["event"]["reset_track_count"] == 1
    assert changed["counts"]["active"] == 0
    second = tracker.update(1.0, [0, 0, 0], [0, 0, 0], [row(1.0)], frame="other-Phobos-frame")
    assert only(second)["id"] != only(first)["id"]
    rewind = tracker.coast(0.5, frame="other-Phobos-frame")
    assert rewind["event"]["reset_reasons"] == ["mission_clock_rewind"]
    assert rewind["generation"] == first["generation"] + 2
    assert rewind["counts"]["fingerprints"] == 0
    assert rewind["counts"]["active"] == 0


def test_future_stale_and_invalid_rows_are_discarded_without_rewriting_raw_policy_input():
    tracker = RadarTracker()
    invalid = [row(10.0) for _ in range(7)]
    invalid[0][0, 0] = np.nan
    invalid[1][1, 1] = np.inf
    invalid[2][1, 2] = 0.0
    invalid[3][2, 0] = -1.0
    invalid[4][2, 1] = 1.0
    invalid[5][0] = 0.0
    invalid[6][0, 0] = 1e30
    raw = np.array([row(11.0), row(3.0), row(10.0), *invalid])
    original = raw.copy()
    result = tracker.update(10.0, [0, 0, 0], [0, 0, 0], raw, frame="Phobos")
    assert result["event"]["discarded"]["invalid_row"] == 7
    assert result["event"]["discarded"]["invalid_or_future_source_time"] == 1
    assert result["event"]["discarded"]["stale_source_time"] == 1
    assert result["counts"]["active"] == 1
    np.testing.assert_array_equal(raw, original)
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        True,
        np.ones((3, 3)),
        np.ones((1, 2, 3)),
        np.ones((1, 3, 3), dtype=bool),
        [[[True, 0, 0], [99, 0, 2], [0, 0, 0]]],
        np.ones((1, 3, 3), dtype=complex),
        np.ones((1, 3, 3), dtype=str),
    ],
)
def test_malformed_and_boolean_radar_is_rejected(raw):
    with pytest.raises(ValueError):
        RadarTracker().update(0.0, [0, 0, 0], [0, 0, 0], raw, frame="Phobos")


@pytest.mark.parametrize("now", [True, np.bool_(False), -1.0, np.nan, np.inf, "0", 1e30])
def test_invalid_clock_scalar_is_rejected(now):
    with pytest.raises(ValueError):
        RadarTracker().coast(now, frame="Phobos")


@pytest.mark.parametrize(
    "changes",
    [
        {"position_world_m": [True, 0, 0]},
        {"position_world_m": [np.inf, 0, 0]},
        {"velocity_world_mps": [0, 0]},
        {"velocity_world_mps": [0, 0, 1j]},
        {"radar_clock_offset_s": True},
        {"radar_clock_offset_s": np.nan},
        {"frame": ""},
        {"frame": "x" * 129},
    ],
)
def test_invalid_arguments_do_not_reset_or_mutate_an_existing_tracker(changes):
    tracker = RadarTracker()
    before = update(tracker, 0.0, row())
    arguments = {
        "now_s": 0.0,
        "position_world_m": [0, 0, 0],
        "velocity_world_mps": [0, 0, 0],
        "radar": [row()],
        "frame": "Phobos",
    }
    arguments.update(changes)
    with pytest.raises(ValueError):
        tracker.update(**arguments)
    assert tracker.coast(0.0, frame="Phobos")["tracks"] == before["tracks"]


def test_bounded_candidate_work_track_count_history_and_report_count():
    config = TrackerConfig(
        max_tracks=3,
        max_input_rows=8,
        max_rows_per_update=4,
        max_candidate_pairs=3,
        max_fingerprints=4,
        report_limit=2,
    )
    tracker = RadarTracker(config)
    rows = [row(position=(100.0 + i * 1000, 0, 0)) for i in range(4)]
    first = update(tracker, 0.0, *rows)
    assert first["counts"]["active"] == 3
    assert first["counts"]["reported"] == 2
    assert first["counts"]["unreported"] == 1
    assert first["event"]["discarded"]["track_budget"] == 1
    next_rows = [row(0.1, position=(100.0 + i * 1000, 0, 0)) for i in range(4)]
    second = update(tracker, 0.1, *next_rows)
    assert second["event"]["candidate_pairs"] <= 3
    assert second["event"]["discarded"]["candidate_budget"] == 4
    assert second["event"]["associated_rows"] == 0
    assert second["event"]["association_search_limited"]
    assert second["counts"]["fingerprints"] <= 4
    assert second["counts"]["active"] <= 3
    with pytest.raises(ValueError, match="max_input_rows"):
        update(tracker, 0.1, *([row(0.1)] * 9))


def test_fingerprint_capacity_eviction_cannot_turn_repeated_evidence_into_new_hits():
    config = TrackerConfig(max_fingerprints=1)
    tracker = RadarTracker(config)
    a, b = row(), row(position=(1000.0, 0.0, 0.0))
    first = update(tracker, 0.0, a, b)
    repeated = update(tracker, 0.0, b, a)
    assert first["tracks"] == repeated["tracks"]
    assert repeated["event"]["discarded"]["history_closed_source_time"] == 1
    assert repeated["event"]["discarded"]["repeated_raw_row"] == 1
    assert repeated["counts"]["fingerprints"] == 1
    unseen_old = update(tracker, 0.0, row(position=(2000.0, 0.0, 0.0)))
    assert unseen_old["event"]["discarded"]["history_closed_source_time"] == 1
    assert unseen_old["counts"]["active"] == 2


def test_row_budget_is_deterministic_and_discarded_rows_cannot_be_repolled_as_fresh():
    config = TrackerConfig(max_input_rows=4, max_rows_per_update=2)
    rows = [row(i / 10, position=(100.0 + i * 1000, 0, 0)) for i in range(4)]
    tracker = RadarTracker(config)
    first = update(tracker, 0.3, *rows)
    other = update(RadarTracker(config), 0.3, *reversed(rows))
    assert first == other
    assert first["event"]["discarded"]["row_budget"] == 2
    assert sorted(t["last_raw_source_time_s"] for t in first["tracks"]) == [0.2, 0.3]
    repeated = update(tracker, 0.3, *rows)
    assert repeated["tracks"] == first["tracks"]
    assert repeated["event"]["discarded"]["repeated_raw_row"] == 4


def test_full_track_budget_has_bounded_JSON_output_below_private_packet_limit():
    raw = [row(position=(100.0 + i * 1000, 0.0, 0.0)) for i in range(256)]
    result = update(RadarTracker(), 0.0, *raw)
    assert result["counts"]["active"] == 256
    assert result["counts"]["reported"] == 16
    assert result["counts"]["unreported"] == 240
    assert len(json.dumps(result, allow_nan=False).encode()) < 128 * 1024
    assert result["maxima"]["velocity_std_mps"] >= 50.0


def test_row_budget_does_not_force_a_match_by_hiding_a_same_timestamp_rival():
    config = TrackerConfig(max_input_rows=2, max_rows_per_update=1)
    tracker = RadarTracker(config)
    update(tracker, 0.0, row())
    before = tracker.coast(0.1, frame="Phobos")
    result = update(
        tracker,
        0.1,
        row(0.1, position=(100.05, 0.0, 0.0)),
        row(0.1, position=(100.06, 0.0, 0.0)),
    )
    assert result["tracks"] == before["tracks"]
    assert result["event"]["discarded"]["row_budget"] == 2
    assert result["event"]["association_search_limited"]


def test_inputs_and_returned_nested_data_are_detached_from_tracker_state():
    tracker = RadarTracker()
    raw = np.array([row()])
    position, velocity = np.zeros(3), np.zeros(3)
    report = tracker.update(0.0, position, velocity, raw, frame="Phobos")
    original = json.loads(json.dumps(report))
    raw[:] = 500
    position[:] = 100
    velocity[:] = 100
    report["tracks"][0]["state"][0] = -10000
    report["tracks"][0]["covariance"][0][0] = -10000
    report["tracks"][0]["last_assimilation"]["raw_radar_row"][2][0] = 12345
    report["tracks"][0]["last_assimilation"]["H"][0][0] = 99
    report["model"]["configuration"]["position_noise_std_m"] = 10000
    later = tracker.coast(0.0, frame="Phobos")
    assert later["tracks"] == original["tracks"]
    assert later["model"] == original["model"]


def test_larger_configured_sensor_uncertainty_is_used_without_random_noise_injection():
    config = replace(TrackerConfig(), position_noise_std_m=4.0, range_rate_noise_std_mps=0.2)
    larger = update(RadarTracker(config), 0.0, row())
    default = update(RadarTracker(), 0.0, row())
    assert covariance(larger)[0, 0] > covariance(default)[0, 0]
    assert covariance(larger)[3, 3] > covariance(default)[3, 3]
    repeat = update(RadarTracker(config), 0.0, row())
    assert larger == repeat
