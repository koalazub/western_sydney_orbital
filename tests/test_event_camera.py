"""Offline checks for the event-camera and pinhole reference prototype.

These tests use synthetic frames and events only. They do not exercise the
local renderer or a hardware event sensor and do not certify vision
performance.
"""

from __future__ import annotations

import numpy as np
import pytest

from wsorb.flight.event_camera import (
    MAX_EVENTS_PER_PAIR,
    NEGATIVE_POLARITY,
    POSITIVE_POLARITY,
    ClusterConfig,
    Event,
    LuminanceFrame,
    PinholeIntrinsics,
    cluster_events,
    detect_events,
)


def frame(time_s: float, values) -> LuminanceFrame:
    return LuminanceFrame(time_s, np.asarray(values, dtype=np.float64))


def test_stationary_frame_produces_no_events() -> None:
    values = np.arange(12, dtype=np.float64).reshape(3, 4)
    batch = detect_events(
        frame(0.0, values),
        frame(0.1, values.copy()),
        contrast_threshold=1e-6,
    )
    assert batch.events == ()
    assert batch.dropped_events == 0


def test_moving_edge_produces_expected_polarity() -> None:
    previous_values = np.zeros((3, 5))
    previous_values[:, 2] = 1.0
    current_values = np.zeros((3, 5))
    current_values[:, 3] = 1.0
    batch = detect_events(
        frame(0.0, previous_values),
        frame(0.1, current_values),
        contrast_threshold=0.1,
    )

    assert len(batch.events) == 6
    assert {(event.x_px, event.polarity) for event in batch.events} == {
        (2, NEGATIVE_POLARITY),
        (3, POSITIVE_POLARITY),
    }
    assert all(event.y_px in (0, 1, 2) for event in batch.events)
    assert all(event.timestamp_s == 0.1 for event in batch.events)


def test_event_timestamps_come_from_the_newer_frame() -> None:
    batch = detect_events(
        frame(1.0, np.zeros((2, 2))),
        frame(2.5, np.ones((2, 2))),
        contrast_threshold=0.1,
    )
    assert batch.timestamp_s == 2.5
    assert len(batch.events) == 4
    assert all(event.timestamp_s == 2.5 for event in batch.events)


def test_contrast_threshold_controls_polarity_and_event_count() -> None:
    previous_values = np.array([[0.0, 0.0]])
    current_values = np.array([[np.expm1(0.05), np.expm1(0.15)]])
    above_low_threshold = detect_events(
        frame(0.0, previous_values),
        frame(0.1, current_values),
        contrast_threshold=0.1,
    )
    assert len(above_low_threshold.events) == 1
    assert above_low_threshold.events[0].x_px == 1
    assert above_low_threshold.events[0].polarity == POSITIVE_POLARITY

    below_high_threshold = detect_events(
        frame(0.0, previous_values),
        frame(0.1, current_values),
        contrast_threshold=0.2,
    )
    assert below_high_threshold.events == ()

    decreasing = detect_events(
        frame(0.0, np.array([[0.0, np.expm1(0.3)]])),
        frame(0.1, np.zeros((1, 2))),
        contrast_threshold=0.1,
    )
    assert len(decreasing.events) == 1
    assert decreasing.events[0].polarity == NEGATIVE_POLARITY


def test_event_cap_clips_deterministically_and_counts_drops() -> None:
    batch = detect_events(
        frame(0.0, np.zeros((2, 3))),
        frame(0.1, np.ones((2, 3))),
        contrast_threshold=0.5,
        max_events=2,
    )
    assert [(event.x_px, event.y_px) for event in batch.events] == [(0, 0), (1, 0)]
    assert batch.dropped_events == 4
    assert all(event.polarity == POSITIVE_POLARITY for event in batch.events)


def test_cluster_centroid_and_polarity_counts() -> None:
    events = [
        Event(0.00, 10, 10, POSITIVE_POLARITY),
        Event(0.01, 11, 10, POSITIVE_POLARITY),
        Event(0.02, 10, 11, NEGATIVE_POLARITY),
        Event(0.02, 100, 100, POSITIVE_POLARITY),
    ]
    config = ClusterConfig(spatial_gate_px=2.0, temporal_gate_s=0.05)
    clusters = cluster_events(events, config)

    assert len(clusters) == 2
    grouped, isolated = clusters
    assert grouped.count == 3
    assert grouped.positive_count == 2
    assert grouped.negative_count == 1
    assert grouped.centroid_x_px == pytest.approx(31.0 / 3.0)
    assert grouped.centroid_y_px == pytest.approx(31.0 / 3.0)
    assert grouped.timestamp_s == pytest.approx(0.01)
    assert grouped.first_timestamp_s == 0.0
    assert grouped.last_timestamp_s == 0.02

    assert isolated.count == 1
    assert isolated.centroid_x_px == 100.0
    assert isolated.centroid_y_px == 100.0

    assert cluster_events(tuple(events), config) == clusters


def test_cluster_temporal_gate_and_event_order() -> None:
    events = [Event(0.0, 5, 5, POSITIVE_POLARITY), Event(0.2, 5, 5, POSITIVE_POLARITY)]
    assert len(cluster_events(events, ClusterConfig(1.0, 0.1))) == 2
    assert len(cluster_events(events, ClusterConfig(1.0, 0.2))) == 1

    with pytest.raises(ValueError):
        cluster_events(list(reversed(events)), ClusterConfig(1.0, 1.0))
    with pytest.raises(ValueError):
        cluster_events(events, ClusterConfig(0.0, 0.1))


def test_pinhole_projection_and_bearing_round_trip() -> None:
    intrinsics = PinholeIntrinsics(
        width_px=640,
        height_px=480,
        fx_px=500.0,
        fy_px=510.0,
        cx_px=320.0,
        cy_px=240.0,
    )
    point = np.array([1.0, -0.5, 4.0])
    pixel = intrinsics.project(point)
    assert pixel == pytest.approx((445.0, 176.25))

    bearing = intrinsics.pixel_to_bearing(pixel)
    assert np.linalg.norm(bearing) == pytest.approx(1.0)
    np.testing.assert_allclose(bearing, point / np.linalg.norm(point))
    np.testing.assert_allclose(intrinsics.project(bearing), pixel)

    expected_matrix = np.array(
        [
            [500.0, 0.0, 320.0],
            [0.0, 510.0, 240.0],
            [0.0, 0.0, 1.0],
        ]
    )
    np.testing.assert_allclose(intrinsics.intrinsic_matrix, expected_matrix)
    with pytest.raises(ValueError):
        intrinsics.intrinsic_matrix[0, 0] = 1.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"width_px": 0},
        {"height_px": 0},
        {"fx_px": 0.0},
        {"fy_px": -1.0},
        {"fx_px": np.nan},
        {"cx_px": -1.0},
        {"cx_px": 641.0},
        {"cy_px": 481.0},
    ],
)
def test_invalid_intrinsics_are_rejected(overrides: dict[str, float]) -> None:
    values: dict[str, float] = {
        "width_px": 640,
        "height_px": 480,
        "fx_px": 500.0,
        "fy_px": 500.0,
        "cx_px": 320.0,
        "cy_px": 240.0,
    }
    values.update(overrides)
    with pytest.raises(ValueError):
        PinholeIntrinsics(**values)


def test_projection_rejects_points_behind_or_nonfinite() -> None:
    intrinsics = PinholeIntrinsics(640, 480, 500.0, 500.0, 320.0, 240.0)
    with pytest.raises(ValueError):
        intrinsics.project([0.0, 0.0, 0.0])
    with pytest.raises(ValueError):
        intrinsics.project([0.0, 0.0, -1.0])
    with pytest.raises(ValueError):
        intrinsics.project([np.nan, 0.0, 1.0])
    with pytest.raises(ValueError):
        intrinsics.pixel_to_bearing([np.inf, 0.0])


@pytest.mark.parametrize(
    "values",
    [
        np.array([[0.0, np.nan]]),
        np.array([[0.0, np.inf]]),
        np.array([[0.0, -1.0]]),
    ],
)
def test_invalid_luminance_frames_are_rejected(values: np.ndarray) -> None:
    with pytest.raises(ValueError):
        LuminanceFrame(0.0, values)
    with pytest.raises(ValueError):
        LuminanceFrame(0.0, np.zeros(4))
    with pytest.raises(ValueError):
        LuminanceFrame(-0.1, np.zeros((2, 2)))
    with pytest.raises(ValueError):
        LuminanceFrame(np.nan, np.zeros((2, 2)))


def test_invalid_frame_pairs_and_requests_are_rejected() -> None:
    previous = frame(0.0, np.zeros((2, 2)))
    current = frame(0.1, np.ones((2, 2)))
    with pytest.raises(ValueError):
        detect_events(previous, frame(0.1, np.ones((2, 3))), contrast_threshold=0.1)
    with pytest.raises(ValueError):
        detect_events(previous, frame(0.0, np.ones((2, 2))), contrast_threshold=0.1)
    with pytest.raises(ValueError):
        detect_events(
            frame(1.0, np.zeros((2, 2))),
            frame(0.5, np.ones((2, 2))),
            contrast_threshold=0.1,
        )
    for threshold in (0.0, -0.1, np.nan, np.inf):
        with pytest.raises(ValueError):
            detect_events(previous, current, contrast_threshold=threshold)
    for max_events in (0, 1.0, MAX_EVENTS_PER_PAIR + 1):
        with pytest.raises(ValueError):
            detect_events(previous, current, contrast_threshold=0.1, max_events=max_events)


def test_invalid_events_are_rejected() -> None:
    with pytest.raises(ValueError):
        Event(0.0, -1, 0, POSITIVE_POLARITY)
    with pytest.raises(ValueError):
        Event(0.0, 0, -1, POSITIVE_POLARITY)
    with pytest.raises(ValueError):
        Event(0.0, 0, 0, 0)
    with pytest.raises(ValueError):
        Event(0.0, 0, 0, 1.0)
    with pytest.raises(ValueError):
        Event(0.0, 0, 0, True)
    with pytest.raises(ValueError):
        Event(np.nan, 0, 0, POSITIVE_POLARITY)
