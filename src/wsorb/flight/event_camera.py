"""Offline event-camera and pinhole-calibration reference prototype.

This module is a pure Python/NumPy study of the sensing themes raised on
20 October: asynchronous polarity events from log-intensity contrast, simple
deterministic event clustering, and distortion-free pinhole projection. It is
not integrated with the local renderer or the Bevy simulation, and it cannot
certify event-sensor or vision performance. It performs no astronomical
catalogue matching and no STP vision processing.

The implementation uses synthetic luminance frames, not hardware event data.
It emits at most one event per pixel per frame pair and models no sensor noise,
hot pixels, latency, refractory period or repeated threshold crossings. It has
no GPU, SDK, thread or renderer dependency. Callers own scheduling, sensor noise
models and any integration with flight software.

Conventions
-----------
- Luminance frames are ``(height, width)`` arrays indexed ``[y, x]``.
- Image coordinates have their origin at the top-left, ``+x`` to the right
  (column) and ``+y`` down (row). Integer coordinates denote pixel centres.
- Event timestamps are seconds on the newer frame's clock. All events from a
  frame pair receive that frame's timestamp; there is no intra-frame timing
  model, so these are not hardware event timestamps.
- Polarity ``+1`` means log intensity increased and ``-1`` means it decreased.
- The camera frame is right-handed with ``+x`` right, ``+y`` down and ``+z``
  along the optical axis away from the camera, matching the image convention.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from numbers import Integral
from typing import Literal

import numpy as np
import numpy.typing as npt

from ._validation import Vec3, count, real_array, scalar, vector

__all__ = [
    "CAMERA_CONVENTION",
    "EVENT_CONVENTION",
    "IMAGE_CONVENTION",
    "MAX_CLUSTER_EVENTS",
    "MAX_EVENTS_PER_PAIR",
    "MAX_FRAME_PIXELS",
    "NEGATIVE_POLARITY",
    "POSITIVE_POLARITY",
    "ClusterConfig",
    "Event",
    "EventBatch",
    "EventCluster",
    "LuminanceFrame",
    "PinholeIntrinsics",
    "cluster_events",
    "detect_events",
]

IMAGE_CONVENTION = (
    "image: origin top-left; +x right (column), +y down (row); integer "
    "coordinates are pixel centres; no distortion or lens model"
)
CAMERA_CONVENTION = "camera: right-handed; +x right, +y down, +z forward along the optical axis"
EVENT_CONVENTION = (
    "event: pixel (x=column, y=row) at a timestamp in seconds on the newer "
    "frame clock; polarity +1 for increasing log intensity, -1 for decreasing"
)

POSITIVE_POLARITY = 1
NEGATIVE_POLARITY = -1

# Hard caps bound every public input and every frame-difference output.
MAX_FRAME_PIXELS = 4_194_304
MAX_EVENTS_PER_PAIR = 65_536
MAX_CLUSTER_EVENTS = 65_536
_MAX_FRAME_DIMENSION = 8192
_MAX_PIXEL_INDEX = 1_000_000
_MAX_ACTIVE_CLUSTERS = 1024
_MAX_IMAGE_DIMENSION = 32_768


@dataclass(frozen=True, slots=True, eq=False)
class LuminanceFrame:
    """One synthetic luminance frame and its source timestamp.

    ``luminance`` must be a finite, nonnegative ``(height, width)`` array within
    the hard pixel cap. The array is copied and made read-only. This is an
    offline reference input: it is not integrated with the local renderer and
    cannot certify event-sensor or vision performance.
    """

    time_s: float
    luminance: npt.NDArray[np.float64]

    def __post_init__(self) -> None:
        object.__setattr__(self, "time_s", scalar(self.time_s, "frame.time_s"))
        values = real_array(self.luminance, "frame.luminance")
        height, width = values.shape if values.ndim == 2 else (0, 0)
        if (
            values.ndim != 2
            or not 1 <= height <= _MAX_FRAME_DIMENSION
            or not 1 <= width <= _MAX_FRAME_DIMENSION
            or values.size > MAX_FRAME_PIXELS
        ):
            raise ValueError(
                "frame.luminance must have shape (height, width) within the hard pixel cap"
            )
        if np.any(values < 0.0):
            raise ValueError("frame.luminance must be nonnegative")
        # Immutable backing also prevents re-enabling writes with setflags.
        immutable = np.frombuffer(values.tobytes(), dtype=np.float64).reshape(values.shape)
        object.__setattr__(self, "luminance", immutable)


@dataclass(frozen=True, slots=True)
class Event:
    """One asynchronous polarity event on a source-clock timestamp.

    ``x_px`` and ``y_px`` follow the image convention: column and row indices
    with an origin at the top-left. Polarity is exactly ``+1`` or ``-1``. This
    is a synthetic event record, not integrated hardware telemetry. It is not
    integrated with the local renderer and cannot certify event-sensor or
    vision performance.
    """

    timestamp_s: float
    x_px: int
    y_px: int
    polarity: Literal[1, -1]

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp_s", scalar(self.timestamp_s, "event.timestamp_s"))
        object.__setattr__(self, "x_px", count(self.x_px, "event.x_px", maximum=_MAX_PIXEL_INDEX))
        object.__setattr__(self, "y_px", count(self.y_px, "event.y_px", maximum=_MAX_PIXEL_INDEX))
        if (
            isinstance(self.polarity, (bool, np.bool_))
            or not isinstance(self.polarity, Integral)
            or self.polarity not in (POSITIVE_POLARITY, NEGATIVE_POLARITY)
        ):
            raise ValueError("event.polarity must be the integer +1 or -1")
        object.__setattr__(self, "polarity", int(self.polarity))


@dataclass(frozen=True, slots=True)
class EventBatch:
    """A bounded synthetic event batch from one frame pair.

    ``dropped_events`` counts threshold crossings omitted by ``max_events``;
    kept events are the first row-major crossings. All events carry
    ``timestamp_s``, the newer frame's timestamp. This is an offline reference
    output, not integrated sensor data. It is not integrated with the local
    renderer and cannot certify event-sensor or vision performance.
    """

    events: tuple[Event, ...]
    timestamp_s: float
    contrast_threshold: float
    dropped_events: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.events, tuple) or not all(
            isinstance(event, Event) for event in self.events
        ):
            raise ValueError("batch.events must be a tuple of Event values")
        if len(self.events) > MAX_EVENTS_PER_PAIR:
            raise ValueError(f"batch.events must contain at most {MAX_EVENTS_PER_PAIR} events")
        object.__setattr__(self, "timestamp_s", scalar(self.timestamp_s, "batch.timestamp_s"))
        object.__setattr__(
            self,
            "contrast_threshold",
            scalar(self.contrast_threshold, "batch.contrast_threshold", positive=True),
        )
        object.__setattr__(
            self,
            "dropped_events",
            count(
                self.dropped_events,
                "batch.dropped_events",
                maximum=MAX_FRAME_PIXELS,
            ),
        )


def detect_events(
    previous: LuminanceFrame,
    current: LuminanceFrame,
    *,
    contrast_threshold: float,
    max_events: int = MAX_EVENTS_PER_PAIR,
) -> EventBatch:
    """Convert two synthetic frames into polarity events by log contrast.

    The differencing is vectorized: ``log1p(current) - log1p(previous)`` is
    compared with the positive threshold, and one event is emitted per pixel
    whose absolute change reaches it. Using ``log1p`` keeps black (zero)
    luminance finite; it is an encoding convention, not a sensor calibration.
    Events are returned in row-major order and clipped deterministically to
    ``max_events``, with the omitted count retained in the batch.

    Frame timestamps must be finite, nonnegative and strictly increasing. The
    newer frame supplies every event timestamp. This prototype is not
    integrated with the local renderer and cannot certify event-sensor or
    vision performance.
    """
    if not isinstance(previous, LuminanceFrame) or not isinstance(current, LuminanceFrame):
        raise ValueError("previous and current must be LuminanceFrame values")
    threshold = scalar(contrast_threshold, "contrast_threshold", positive=True)
    limit = count(max_events, "max_events", minimum=1, maximum=MAX_EVENTS_PER_PAIR)
    if previous.luminance.shape != current.luminance.shape:
        raise ValueError("previous and current frames must have the same shape")
    if current.time_s <= previous.time_s:
        raise ValueError("frame timestamps must be strictly increasing")

    delta = np.log1p(current.luminance) - np.log1p(previous.luminance)
    crossings = np.flatnonzero((np.abs(delta) >= threshold).ravel())
    kept = crossings[:limit]
    height, width = current.luminance.shape
    rows, columns = np.divmod(kept, width)
    signs = np.where(delta.ravel()[kept] > 0.0, POSITIVE_POLARITY, NEGATIVE_POLARITY)
    events = tuple(
        Event(current.time_s, int(column), int(row), int(sign))
        for row, column, sign in zip(rows, columns, signs, strict=True)
    )
    return EventBatch(
        events=events,
        timestamp_s=current.time_s,
        contrast_threshold=threshold,
        dropped_events=int(crossings.size - kept.size),
    )


@dataclass(frozen=True, slots=True)
class ClusterConfig:
    """Spatial and temporal gates for deterministic event clustering.

    Distances are measured in pixels from the running cluster centroid.
    Timestamps are measured in seconds from the latest event in the cluster.
    Both gates must be finite and positive. These are offline proximity choices,
    not calibrated sensor or tracker gates. This prototype is not integrated
    with the local renderer and cannot certify event-sensor or vision
    performance.
    """

    spatial_gate_px: float
    temporal_gate_s: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "spatial_gate_px",
            scalar(self.spatial_gate_px, "cluster.spatial_gate_px", positive=True),
        )
        object.__setattr__(
            self,
            "temporal_gate_s",
            scalar(self.temporal_gate_s, "cluster.temporal_gate_s", positive=True),
        )


@dataclass(frozen=True, slots=True)
class EventCluster:
    """A deterministic centroid estimate from a bounded group of events.

    ``timestamp_s`` is the arithmetic mean event time, while
    ``first_timestamp_s`` and ``last_timestamp_s`` retain the gate span.
    Positive and negative counts preserve explicit event polarity. This is a
    proximity summary, not a target identity or a calibrated measurement. It is
    not integrated with the local renderer and cannot certify event-sensor or
    vision performance.
    """

    centroid_x_px: float
    centroid_y_px: float
    timestamp_s: float
    first_timestamp_s: float
    last_timestamp_s: float
    count: int
    positive_count: int
    negative_count: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "centroid_x_px", scalar(self.centroid_x_px, "cluster.centroid_x_px")
        )
        object.__setattr__(
            self, "centroid_y_px", scalar(self.centroid_y_px, "cluster.centroid_y_px")
        )
        object.__setattr__(self, "timestamp_s", scalar(self.timestamp_s, "cluster.timestamp_s"))
        object.__setattr__(
            self,
            "first_timestamp_s",
            scalar(self.first_timestamp_s, "cluster.first_timestamp_s"),
        )
        object.__setattr__(
            self,
            "last_timestamp_s",
            scalar(self.last_timestamp_s, "cluster.last_timestamp_s"),
        )
        n_events = count(self.count, "cluster.count", minimum=1, maximum=MAX_CLUSTER_EVENTS)
        positive = count(self.positive_count, "cluster.positive_count", maximum=MAX_CLUSTER_EVENTS)
        negative = count(self.negative_count, "cluster.negative_count", maximum=MAX_CLUSTER_EVENTS)
        if positive + negative != n_events:
            raise ValueError("cluster positive_count and negative_count must sum to count")
        if not self.first_timestamp_s <= self.timestamp_s <= self.last_timestamp_s:
            raise ValueError("cluster timestamps must satisfy first <= mean <= last")


@dataclass(slots=True)
class _ActiveCluster:
    """Mutable accumulator used only inside ``cluster_events``."""

    index: int
    sum_x: float
    sum_y: float
    sum_time: float
    count: int
    positive_count: int
    negative_count: int
    first_timestamp_s: float
    last_timestamp_s: float

    @classmethod
    def start(cls, event: Event, index: int) -> _ActiveCluster:
        positive = int(event.polarity == POSITIVE_POLARITY)
        return cls(
            index,
            float(event.x_px),
            float(event.y_px),
            event.timestamp_s,
            1,
            positive,
            1 - positive,
            event.timestamp_s,
            event.timestamp_s,
        )

    @property
    def centroid_x_px(self) -> float:
        return self.sum_x / self.count

    @property
    def centroid_y_px(self) -> float:
        return self.sum_y / self.count

    def add(self, event: Event) -> None:
        self.sum_x += event.x_px
        self.sum_y += event.y_px
        self.sum_time += event.timestamp_s
        self.count += 1
        if event.polarity == POSITIVE_POLARITY:
            self.positive_count += 1
        else:
            self.negative_count += 1
        self.last_timestamp_s = event.timestamp_s

    def snapshot(self) -> EventCluster:
        mean_time = self.sum_time / self.count
        # Repeated addition can round a mean just outside the input span, so
        # keep the derived measurement time within its own first/last bounds.
        mean_time = min(max(mean_time, self.first_timestamp_s), self.last_timestamp_s)
        return EventCluster(
            centroid_x_px=self.centroid_x_px,
            centroid_y_px=self.centroid_y_px,
            timestamp_s=mean_time,
            first_timestamp_s=self.first_timestamp_s,
            last_timestamp_s=self.last_timestamp_s,
            count=self.count,
            positive_count=self.positive_count,
            negative_count=self.negative_count,
        )


def cluster_events(
    events: Sequence[Event],
    config: ClusterConfig,
) -> tuple[EventCluster, ...]:
    """Cluster timestamp-ordered events into deterministic centroid estimates.

    This is a single greedy pass, never a pairwise expansion over all events.
    With hard event and active-cluster caps, each event is compared with only
    the bounded active window. An event joins the nearest eligible cluster
    centroid (ties go to the earlier cluster); otherwise it starts a cluster.
    Clusters whose latest event is older than the temporal gate stop accepting
    events but remain in the returned creation-order tuple.

    Input must already be in nondecreasing timestamp order and within the hard
    event cap. This offline prototype is not integrated with the local renderer
    and cannot certify event-sensor or vision performance.
    """
    if not isinstance(config, ClusterConfig):
        raise ValueError("config must be a ClusterConfig")
    if isinstance(events, (str, bytes, bytearray)) or not isinstance(events, Sequence):
        raise ValueError("events must be a bounded sequence of Event values")
    if len(events) > MAX_CLUSTER_EVENTS:
        raise ValueError(f"events must contain at most {MAX_CLUSTER_EVENTS} values")
    if not events:
        return ()

    clusters: list[_ActiveCluster] = []
    active: list[_ActiveCluster] = []
    spatial_gate_sq = config.spatial_gate_px * config.spatial_gate_px
    previous_timestamp = -math.inf
    for event in events:
        if not isinstance(event, Event):
            raise ValueError("events must contain Event values")
        if event.timestamp_s < previous_timestamp:
            raise ValueError("events must be in nondecreasing timestamp order")
        previous_timestamp = event.timestamp_s

        active[:] = [
            cluster
            for cluster in active
            if event.timestamp_s - cluster.last_timestamp_s <= config.temporal_gate_s
        ]
        best: _ActiveCluster | None = None
        best_key: tuple[float, int] | None = None
        for cluster in active:
            dx = cluster.centroid_x_px - event.x_px
            dy = cluster.centroid_y_px - event.y_px
            distance_sq = dx * dx + dy * dy
            if distance_sq <= spatial_gate_sq:
                key = (distance_sq, cluster.index)
                if best_key is None or key < best_key:
                    best = cluster
                    best_key = key

        if best is None:
            if len(active) >= _MAX_ACTIVE_CLUSTERS:
                raise ValueError(
                    f"active event clusters exceed the hard cap of {_MAX_ACTIVE_CLUSTERS}"
                )
            best = _ActiveCluster.start(event, len(clusters))
            clusters.append(best)
            active.append(best)
        else:
            best.add(event)

    return tuple(cluster.snapshot() for cluster in clusters)


@dataclass(frozen=True, slots=True)
class PinholeIntrinsics:
    """Distortion-free pinhole intrinsics with explicit frame conventions.

    The intrinsic matrix maps camera-frame points ``(x, y, z)`` with ``z > 0``
    to image pixels ``(u, v)`` using ``u = fx*x/z + cx`` and
    ``v = fy*y/z + cy``. ``cx_px`` and ``cy_px`` may lie on the inclusive
    ``[0, width_px]`` / ``[0, height_px]`` boundary, which permits a principal
    point between the central pixels. No distortion, rolling shutter or lens
    model is applied.

    This calibration utility is an offline reference only. It is not integrated
    with the local renderer and cannot certify event-sensor or vision
    performance.
    """

    width_px: int
    height_px: int
    fx_px: float
    fy_px: float
    cx_px: float
    cy_px: float

    def __post_init__(self) -> None:
        width = count(self.width_px, "intrinsics.width_px", minimum=1, maximum=_MAX_IMAGE_DIMENSION)
        height = count(
            self.height_px, "intrinsics.height_px", minimum=1, maximum=_MAX_IMAGE_DIMENSION
        )
        fx = scalar(self.fx_px, "intrinsics.fx_px", positive=True)
        fy = scalar(self.fy_px, "intrinsics.fy_px", positive=True)
        cx = scalar(self.cx_px, "intrinsics.cx_px")
        cy = scalar(self.cy_px, "intrinsics.cy_px")
        if not 0.0 <= cx <= width:
            raise ValueError("intrinsics.cx_px must lie within [0, width_px]")
        if not 0.0 <= cy <= height:
            raise ValueError("intrinsics.cy_px must lie within [0, height_px]")
        object.__setattr__(self, "width_px", width)
        object.__setattr__(self, "height_px", height)
        object.__setattr__(self, "fx_px", fx)
        object.__setattr__(self, "fy_px", fy)
        object.__setattr__(self, "cx_px", cx)
        object.__setattr__(self, "cy_px", cy)

    @property
    def intrinsic_matrix(self) -> npt.NDArray[np.float64]:
        """Return a read-only 3x3 camera matrix in the stated convention."""
        matrix = np.array(
            [
                [self.fx_px, 0.0, self.cx_px],
                [0.0, self.fy_px, self.cy_px],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        matrix.setflags(write=False)
        return matrix

    def project(self, point_camera_m: npt.ArrayLike) -> tuple[float, float]:
        """Project a camera-frame point to a distortion-free pixel.

        The point must be finite with ``z > 0`` (in front of the camera).
        Projection is not clipped to the image bounds. The returned pixel
        ``(u_px, v_px)`` follows the top-left image convention. This offline
        reference is not integrated with the local renderer and cannot certify
        event-sensor or vision performance.
        """
        x, y, z = vector(point_camera_m, "point_camera_m")
        if z <= 0.0:
            raise ValueError("point_camera_m must have z > 0 in the camera frame")
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            u_px = self.fx_px * x / z + self.cx_px
            v_px = self.fy_px * y / z + self.cy_px
        if not math.isfinite(float(u_px)) or not math.isfinite(float(v_px)):
            raise ValueError("projected pixel is not representable")
        return float(u_px), float(v_px)

    def pixel_to_bearing(self, pixel_xy: npt.ArrayLike) -> Vec3:
        """Convert an image pixel to a unit bearing in the camera frame.

        The input is ``(u_px, v_px)`` in the top-left image convention. The
        result is a unit vector with ``+x`` right, ``+y`` down and ``+z``
        forward. Pixels outside the nominal image are accepted as mathematical
        rays; the caller decides whether they are valid observations. This is
        an offline reference only, not integrated with the local renderer, and
        it cannot certify event-sensor or vision performance.
        """
        u_px, v_px = vector(pixel_xy, "pixel_xy", length=2)
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            ray_x = (u_px - self.cx_px) / self.fx_px
            ray_y = (v_px - self.cy_px) / self.fy_px
        norm = math.hypot(float(ray_x), float(ray_y), 1.0)
        if not all(math.isfinite(value) for value in (ray_x, ray_y, norm)) or norm == 0.0:
            raise ValueError("pixel bearing is not representable")
        return (
            float(ray_x) / norm,
            float(ray_y) / norm,
            1.0 / norm,
        )
