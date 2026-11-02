"""Pure checks for bounded association and per-track lifecycle hypotheses.

Identity is a local hypothesis in these tests, not a physical-identity claim.
No test measures wall-clock time or grants clearance.
"""

from __future__ import annotations

import numpy as np
import pytest

from wsorb.flight.association import AssociationConfig, MultiTargetAssociator, TrackStatus


def _config(**overrides: int | float) -> AssociationConfig:
    defaults: dict[str, int | float] = {
        "gate_radius_m": 2.0,
        "confirm_hits": 2,
        "coast_after_s": 1.0,
        "delete_after_s": 4.0,
        "max_tracks": 512,
        "max_measurements": 512,
        "max_candidate_pairs": 100_000,
    }
    defaults.update(overrides)
    return AssociationConfig(**defaults)


def test_gate_rejects_a_measurement_beyond_the_radius() -> None:
    tracker = MultiTargetAssociator(_config(gate_radius_m=2.0))
    first = tracker.update(0.0, [[0.0, 0.0, 0.0]])
    assert len(first.tracks) == 1
    assert first.tracks[0].status is TrackStatus.TENTATIVE

    second = tracker.update(1.0, [[10.0, 0.0, 0.0]])
    assert second.associations == ()
    assert second.gated_pairs == 0
    assert second.birth_track_ids == (2,)
    assert [track.track_id for track in second.tracks] == [1, 2]
    original, fresh = second.tracks
    assert original.missed_observations == 1
    assert original.status is TrackStatus.TENTATIVE
    assert fresh.position_m == (10.0, 0.0, 0.0)
    assert fresh.velocity_mps is None


def test_equal_distances_resolve_by_track_id_then_measurement_index() -> None:
    tracker = MultiTargetAssociator(
        _config(gate_radius_m=5.0, coast_after_s=10.0, delete_after_s=20.0)
    )
    tracker.update(0.0, [[-1.0, 0.0, 0.0], [1.0, 0.0, 0.0]])

    result = tracker.update(1.0, [[0.0, 0.0, 0.0]])
    assert len(result.associations) == 1
    association = result.associations[0]
    assert association.track_id == 1
    assert association.measurement_index == 0
    assert association.distance_m == pytest.approx(1.0)
    assert association.ambiguous is True
    assert result.tracks[0].hits == 2
    assert result.tracks[0].ambiguous_hits == 1
    assert result.tracks[1].missed_observations == 1

    single = MultiTargetAssociator(
        _config(gate_radius_m=5.0, coast_after_s=10.0, delete_after_s=20.0)
    )
    single.update(0.0, [[0.0, 0.0, 0.0]])
    tied = single.update(1.0, [[-0.5, 0.0, 0.0], [0.5, 0.0, 0.0]])
    assert len(tied.associations) == 1
    assert tied.associations[0].measurement_index == 0
    assert tied.associations[0].distance_m == pytest.approx(0.5)
    assert tied.unmatched_measurements == (1,)
    assert tied.birth_track_ids == ()


def test_crossing_targets_keep_deterministic_local_hypotheses() -> None:
    tracker = MultiTargetAssociator(
        _config(
            gate_radius_m=1.5,
            confirm_hits=3,
            coast_after_s=10.0,
            delete_after_s=20.0,
        )
    )
    initial = tracker.update(0.0, [[-10.0, 0.0, 0.0], [10.0, 0.0, 0.0]])
    assert [track.track_id for track in initial.tracks] == [1, 2]

    for step in range(1, 10):
        result = tracker.update(
            float(step),
            [[-10.0 + step, 0.0, 0.0], [10.0 - step, 0.0, 0.0]],
        )
        assert result.birth_track_ids == ()
        assert [(item.track_id, item.measurement_index) for item in result.associations] == [
            (1, 0),
            (2, 1),
        ]
        assert len(result.tracks) == 2

    crossing = tracker.update(10.0, [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]])
    assert [(item.track_id, item.measurement_index) for item in crossing.associations] == [
        (1, 0),
        (2, 1),
    ]
    assert all(item.ambiguous for item in crossing.associations)

    after = tracker.update(11.0, [[1.0, 0.0, 0.0], [-1.0, 0.0, 0.0]])
    assert [(item.track_id, item.measurement_index) for item in after.associations] == [
        (1, 0),
        (2, 1),
    ]
    assert all(not item.ambiguous for item in after.associations)
    assert [track.position_m for track in after.tracks] == [
        (1.0, 0.0, 0.0),
        (-1.0, 0.0, 0.0),
    ]
    assert all(track.status is TrackStatus.CONFIRMED for track in after.tracks)


def test_a_missing_target_does_not_age_the_observed_target() -> None:
    tracker = MultiTargetAssociator(
        _config(
            gate_radius_m=1.5,
            confirm_hits=2,
            coast_after_s=1.0,
            delete_after_s=5.0,
        )
    )
    tracker.update(0.0, [[-2.0, 0.0, 0.0], [2.0, 0.0, 0.0]])
    tracker.update(1.0, [[-1.0, 0.0, 0.0]])

    result = tracker.update(2.0, [[0.0, 0.0, 0.0]])
    observed, missing = result.tracks
    assert observed.track_id == 1
    assert observed.status is TrackStatus.CONFIRMED
    assert observed.missed_observations == 0
    assert observed.last_seen_s == 2.0
    assert observed.hits == 3
    assert missing.track_id == 2
    assert missing.status is TrackStatus.COASTING
    assert missing.missed_observations == 2
    assert missing.last_seen_s == 0.0


def test_confirmation_coasting_and_deletion_follow_caller_thresholds() -> None:
    tracker = MultiTargetAssociator(
        _config(
            gate_radius_m=5.0,
            confirm_hits=2,
            coast_after_s=1.0,
            delete_after_s=3.0,
        )
    )
    first = tracker.update(0.0, [[0.0, 0.0, 0.0]])
    assert first.tracks[0].status is TrackStatus.TENTATIVE

    second = tracker.update(0.5, [[0.0, 0.0, 0.0]])
    assert second.tracks[0].status is TrackStatus.CONFIRMED
    assert second.tracks[0].confirmed_at_s == 0.5

    third = tracker.update(1.0, [])
    assert third.tracks[0].status is TrackStatus.CONFIRMED
    assert third.tracks[0].missed_observations == 1

    fourth = tracker.update(1.6, [])
    assert fourth.tracks[0].status is TrackStatus.COASTING
    assert fourth.tracks[0].missed_observations == 2

    final = tracker.update(3.6, [])
    assert final.tracks == ()
    assert len(final.deletions) == 1
    deletion = final.deletions[0]
    assert deletion.track_id == 1
    assert deletion.age_s == pytest.approx(3.1)
    assert deletion.was_confirmed is True
    assert deletion.status is TrackStatus.DELETED


def test_empty_measurements_are_accepted_without_births() -> None:
    tracker = MultiTargetAssociator(_config(coast_after_s=5.0, delete_after_s=10.0))
    empty = tracker.update(0.0, np.empty((0, 3)))
    assert empty.tracks == ()
    assert empty.associations == ()
    assert empty.birth_track_ids == ()
    assert empty.candidate_pairs == 0

    tracker.update(1.0, [[0.0, 0.0, 0.0]])
    aged = tracker.update(2.0, [])
    assert len(aged.tracks) == 1
    assert aged.associations == ()
    assert aged.birth_track_ids == ()
    assert aged.tracks[0].missed_observations == 1


@pytest.mark.parametrize(
    "measurements",
    [
        np.array([[np.nan, 0.0, 0.0]]),
        np.array([[np.inf, 0.0, 0.0]]),
        np.array([[0.0, 0.0]]),
        np.array([0.0, 0.0, 0.0]),
        np.array([[[0.0, 0.0, 0.0]]]),
        np.array([[True, False, False]]),
        np.empty((0, 2)),
    ],
)
def test_nonfinite_and_mismatched_measurements_are_rejected(measurements: np.ndarray) -> None:
    tracker = MultiTargetAssociator(_config())
    with pytest.raises(ValueError):
        tracker.update(0.0, measurements)
    assert tracker.tracks == ()


def test_invalid_times_and_non_monotonic_updates_are_rejected() -> None:
    tracker = MultiTargetAssociator(_config())
    for value in (np.nan, np.inf, -1.0):
        with pytest.raises(ValueError):
            tracker.update(value, [])
    assert tracker.update(1.0, []).time_s == 1.0
    with pytest.raises(ValueError):
        tracker.update(1.0, [])
    with pytest.raises(ValueError):
        tracker.update(0.5, [])


def test_configuration_rejects_negative_or_inconsistent_thresholds() -> None:
    with pytest.raises(ValueError):
        AssociationConfig(gate_radius_m=-1.0)
    with pytest.raises(ValueError):
        AssociationConfig(coast_after_s=-1.0)
    with pytest.raises(ValueError):
        AssociationConfig(delete_after_s=-1.0)
    with pytest.raises(ValueError):
        AssociationConfig(gate_radius_m=float("nan"))
    with pytest.raises(ValueError):
        AssociationConfig(coast_after_s=float("inf"))
    with pytest.raises(ValueError):
        AssociationConfig(gate_radius_m=True)
    with pytest.raises(ValueError):
        AssociationConfig(confirm_hits=1)
    with pytest.raises(ValueError):
        AssociationConfig(max_tracks=0)
    with pytest.raises(ValueError):
        AssociationConfig(max_measurements=0)
    with pytest.raises(ValueError):
        AssociationConfig(max_candidate_pairs=0)
    with pytest.raises(ValueError):
        AssociationConfig(coast_after_s=5.0, delete_after_s=5.0)


def test_measurement_and_candidate_budgets_are_rejected_without_mutation() -> None:
    limited = MultiTargetAssociator(_config(max_measurements=2, max_candidate_pairs=4))
    with pytest.raises(ValueError):
        limited.update(0.0, np.zeros((3, 3)))
    assert limited.tracks == ()

    pair_limited = MultiTargetAssociator(_config(max_measurements=4, max_candidate_pairs=3))
    pair_limited.update(0.0, np.zeros((2, 3)))
    with pytest.raises(ValueError):
        pair_limited.update(1.0, np.zeros((2, 3)))
    assert [track.track_id for track in pair_limited.tracks] == [1, 2]

    retry = pair_limited.update(1.0, np.zeros((1, 3)))
    assert retry.candidate_pairs == 2
    assert len(retry.associations) == 1


def test_500_track_200_measurement_scan_stays_within_the_candidate_cap() -> None:
    tracker = MultiTargetAssociator(
        AssociationConfig(
            gate_radius_m=1.0,
            confirm_hits=3,
            coast_after_s=10.0,
            delete_after_s=20.0,
            max_tracks=512,
            max_measurements=512,
            max_candidate_pairs=100_000,
        )
    )
    positions = np.arange(500, dtype=np.float64) * 10.0
    initial = np.column_stack(
        (positions, np.zeros(500, dtype=np.float64), np.zeros(500, dtype=np.float64))
    )
    born = tracker.update(0.0, initial)
    assert len(born.tracks) == 500

    result = tracker.update(1.0, initial[:200])
    assert result.candidate_pairs == 500 * 200
    assert result.gated_pairs == 200
    assert len(result.tracks) == 500
    assert len(result.associations) == 200
    assert result.birth_track_ids == ()
    assert result.dropped_births == 0
    assert sum(track.missed_observations for track in result.tracks) == 300
