"""Bounded gated measurement-to-track association with an explicit lifecycle.

This module is a pure NumPy baseline: stable greedy one-to-one assignment with a
hard distance gate, followed by per-track confirmation, coasting and deletion.
It is not optimal multi-hypothesis tracking. No alternative association
hypotheses are retained, and an accepted assignment is not claimed to be
globally best.

Each track is a local identity hypothesis. Track IDs are bookkeeping labels, not
physical identity; crossings, reacquisition and confirmation are not proof that
the same object was observed. A confirmed track never certifies clearance for a
manoeuvre. The caller owns frames, sensor models, update scheduling and every
safety decision.

Inputs must be finite. A scan with more measurements than ``max_measurements``
or more track/measurement pairs than ``max_candidate_pairs`` raises ValueError
before the associator changes. The pair cap also bounds the O(N*M) distance
work. Active tracks never exceed ``max_tracks``; extra births are dropped and
reported rather than growing the state without bound.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

import numpy as np
import numpy.typing as npt

from ._validation import Vec3, count, real_array, scalar

__all__ = [
    "Association",
    "AssociationConfig",
    "AssociationResult",
    "Deletion",
    "MultiTargetAssociator",
    "Track",
    "TrackStatus",
]


class TrackStatus(StrEnum):
    """Lifecycle state of one local track hypothesis.

    ``COASTING`` means no accepted measurement for longer than the caller's
    coast threshold, whether or not confirmation was reached. ``DELETED`` is a
    removal event reported by :class:`Deletion`; active tracks never store it.
    """

    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"
    COASTING = "coasting"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class AssociationConfig:
    """Caller-supplied model thresholds and hard per-update budgets.

    ``gate_radius_m`` is a distance gate, not a probability or covariance gate.
    ``confirm_hits`` counts accepted measurements, including ambiguous ones.
    ``coast_after_s`` and ``delete_after_s`` are measured from each track's own
    last accepted observation, with strict ``>`` comparisons. The defaults are
    readable baseline values, not tuned competition parameters.
    """

    gate_radius_m: float = 5.0
    confirm_hits: int = 3
    coast_after_s: float = 2.0
    delete_after_s: float = 10.0
    max_tracks: int = 512
    max_measurements: int = 512
    max_candidate_pairs: int = 131_072

    def __post_init__(self) -> None:
        for name in ("gate_radius_m", "coast_after_s", "delete_after_s"):
            object.__setattr__(self, name, scalar(getattr(self, name), name))
        limits = {
            "confirm_hits": (2, 100),
            "max_tracks": (1, 4096),
            "max_measurements": (1, 4096),
            "max_candidate_pairs": (1, 1_000_000),
        }
        for name, (minimum, maximum) in limits.items():
            object.__setattr__(
                self,
                name,
                count(getattr(self, name), name, minimum=minimum, maximum=maximum),
            )
        if self.delete_after_s <= self.coast_after_s:
            raise ValueError("delete_after_s must exceed coast_after_s")


@dataclass(frozen=True, slots=True)
class Track:
    """One local identity hypothesis and its lifecycle state.

    ``track_id`` is local bookkeeping, not proof of physical identity.
    ``velocity_mps`` is a finite-difference estimate, or ``None`` before the
    second accepted measurement. A confirmed status means the caller's hit
    count was reached; it does not certify identity or clearance.
    """

    track_id: int
    position_m: Vec3
    velocity_mps: Vec3 | None
    status: TrackStatus
    hits: int
    missed_observations: int
    ambiguous_hits: int
    first_seen_s: float
    last_seen_s: float
    confirmed_at_s: float | None


@dataclass(frozen=True, slots=True)
class Association:
    """One accepted gated pair in a single scan.

    ``distance_m`` is measured from the track's predicted gate centre.
    ``ambiguous`` means another gated rival existed for the track or for the
    measurement; the pair is retained as one local hypothesis.
    """

    track_id: int
    measurement_index: int
    distance_m: float
    ambiguous: bool


@dataclass(frozen=True, slots=True)
class Deletion:
    """A track removed at the start of an update because it was too old."""

    track_id: int
    age_s: float
    was_confirmed: bool

    @property
    def status(self) -> TrackStatus:
        """Lifecycle marker for a removed track, never stored on an active one."""
        return TrackStatus.DELETED


@dataclass(frozen=True, slots=True)
class AssociationResult:
    """Detached snapshot after one scan; identity is only a hypothesis.

    No field certifies clearance. ``candidate_pairs`` is the examined
    track/measurement product, bounded before allocation. ``gated_pairs`` is the
    subset inside the distance gate. ``unmatched_measurements`` includes both
    ungated measurements (which may birth tracks) and gated measurements already
    claimed by another track (which do not duplicate a track in this scan).
    """

    time_s: float
    tracks: tuple[Track, ...]
    associations: tuple[Association, ...]
    deletions: tuple[Deletion, ...]
    birth_track_ids: tuple[int, ...]
    unmatched_measurements: tuple[int, ...]
    dropped_births: int
    ambiguous_measurements: int
    ambiguous_associations: int
    gated_pairs: int
    candidate_pairs: int


class MultiTargetAssociator:
    """Deterministic greedy association state machine, not a certified tracker.

    For every update:

    1. Tracks older than ``delete_after_s`` since their own last accepted
       measurement are deleted.
    2. Remaining tracks are predicted with their last measured position and a
       finite-difference velocity. Before any velocity is available the gate is
       centred on the last position.
    3. All track/measurement distances are computed in one NumPy operation and
       compared with the hard radial gate.
    4. Gated pairs are sorted by distance, then track ID, then measurement index,
       and accepted greedily one-to-one. Equal distances therefore have a stable
       and documented order.
    5. An accepted pair with another gated rival is marked ambiguous. The
       assignment is still used as one local hypothesis; no alternatives are
       stored.
    6. Ungated measurements start tentative tracks while capacity remains. A
       measurement gated only to already claimed tracks does not birth a
       duplicate track in the same scan.
    7. Unmatched tracks age from their own last observation. A miss for one
       target neither advances another track's age nor adds to its miss count.

    Track identity is a hypothesis and clearance is not certified. This object
    is stateful and is not intended for concurrent calls from multiple threads.
    """

    def __init__(self, config: AssociationConfig | None = None) -> None:
        if config is not None and not isinstance(config, AssociationConfig):
            raise ValueError("config must be AssociationConfig or None")
        self.config = AssociationConfig() if config is None else config
        self._tracks: tuple[Track, ...] = ()
        self._last_time_s: float | None = None
        self._next_track_id = 1

    @property
    def tracks(self) -> tuple[Track, ...]:
        """Current detached track tuple, ordered by track ID."""
        return self._tracks

    @property
    def last_time_s(self) -> float | None:
        """Time of the last accepted update, or ``None`` before the first."""
        return self._last_time_s

    @staticmethod
    def _measurements(value: npt.ArrayLike) -> npt.NDArray[np.float64]:
        array = real_array(value, "measurements")
        if array.size == 0:
            if array.shape in ((0,), (0, 3)):
                return np.empty((0, 3), dtype=np.float64)
            raise ValueError("measurements must have shape (N, 3) or be empty")
        if array.ndim != 2 or array.shape[1] != 3:
            raise ValueError("measurements must have shape (N, 3)")
        return array

    @staticmethod
    def _predict(
        tracks: tuple[Track, ...],
        now: float,
    ) -> npt.NDArray[np.float64]:
        predicted = np.empty((len(tracks), 3), dtype=np.float64)
        for index, track in enumerate(tracks):
            position = np.asarray(track.position_m, dtype=np.float64)
            if track.velocity_mps is not None:
                with np.errstate(over="ignore", invalid="ignore"):
                    position = position + np.asarray(track.velocity_mps, dtype=np.float64) * (
                        now - track.last_seen_s
                    )
            predicted[index] = position
        if not np.isfinite(predicted).all():
            raise ValueError("track prediction is not representable")
        return predicted

    def _gated(
        self,
        tracks: list[Track],
        points: npt.NDArray[np.float64],
        now: float,
    ) -> tuple[
        npt.NDArray[np.float64],
        npt.NDArray[np.bool_],
        npt.NDArray[np.intp],
        npt.NDArray[np.intp],
    ]:
        distances = np.empty((len(tracks), len(points)), dtype=np.float64)
        gated = np.zeros((len(tracks), len(points)), dtype=bool)
        if tracks and len(points):
            predicted = self._predict(tuple(tracks), now)
            difference = predicted[:, None, :] - points[None, :, :]
            with np.errstate(over="ignore", invalid="ignore"):
                squared = np.einsum("ijk,ijk->ij", difference, difference)
                distances = np.sqrt(squared)
            if not np.isfinite(distances).all():
                raise ValueError("track/measurement distance is not representable")
            gated = distances <= self.config.gate_radius_m
        track_candidates = gated.sum(axis=1, dtype=np.intp)
        measurement_candidates = gated.sum(axis=0, dtype=np.intp)
        return distances, gated, track_candidates, measurement_candidates

    @staticmethod
    def _greedy(
        tracks: list[Track],
        distances: npt.NDArray[np.float64],
        gated: npt.NDArray[np.bool_],
        track_candidates: npt.NDArray[np.intp],
        measurement_candidates: npt.NDArray[np.intp],
    ) -> tuple[list[Association], dict[int, tuple[int, bool]], set[int]]:
        associations: list[Association] = []
        matches: dict[int, tuple[int, bool]] = {}
        assigned_tracks: set[int] = set()
        assigned_measurements: set[int] = set()
        track_indices, measurement_indices = np.nonzero(gated)
        if track_indices.size == 0:
            return associations, matches, assigned_measurements
        pair_distances = distances[track_indices, measurement_indices]
        # Last key is primary: distance first, then track ID, then measurement.
        order = np.lexsort((measurement_indices, track_indices, pair_distances))
        for position in order:
            track_index = int(track_indices[position])
            measurement_index = int(measurement_indices[position])
            if track_index in assigned_tracks or measurement_index in assigned_measurements:
                continue
            assigned_tracks.add(track_index)
            assigned_measurements.add(measurement_index)
            ambiguous = bool(
                track_candidates[track_index] > 1 or measurement_candidates[measurement_index] > 1
            )
            matches[track_index] = (measurement_index, ambiguous)
            associations.append(
                Association(
                    track_id=tracks[track_index].track_id,
                    measurement_index=measurement_index,
                    distance_m=float(pair_distances[position]),
                    ambiguous=ambiguous,
                )
            )
        return associations, matches, assigned_measurements

    def update(self, time_s: float, measurements: npt.ArrayLike) -> AssociationResult:
        """Consume one scan and return a detached lifecycle snapshot.

        Rejected input leaves the associator unchanged. ``measurements`` must be
        a finite real array with shape ``(M, 3)``; an empty list or an array with
        shape ``(0,)`` or ``(0, 3)`` is accepted. A scan budget overrun is
        raised before any state change. Identity remains a hypothesis and no
        returned state certifies clearance.
        """
        now = scalar(time_s, "time_s")
        if self._last_time_s is not None and now <= self._last_time_s:
            raise ValueError("time_s must increase strictly between association updates")
        points = self._measurements(measurements)
        if len(points) > self.config.max_measurements:
            raise ValueError(f"measurements exceed max_measurements={self.config.max_measurements}")
        active: list[Track] = []
        deletions: list[Deletion] = []
        for track in self._tracks:
            age = now - track.last_seen_s
            if age > self.config.delete_after_s:
                deletions.append(
                    Deletion(
                        track_id=track.track_id,
                        age_s=age,
                        was_confirmed=track.confirmed_at_s is not None,
                    )
                )
            else:
                active.append(track)
        candidate_pairs = len(active) * len(points)
        if candidate_pairs > self.config.max_candidate_pairs:
            raise ValueError(
                f"candidate pairs {candidate_pairs} exceed "
                f"max_candidate_pairs={self.config.max_candidate_pairs}"
            )
        distances, gated, track_candidates, measurement_candidates = self._gated(
            active,
            points,
            now,
        )
        associations, matches, assigned_measurements = self._greedy(
            active,
            distances,
            gated,
            track_candidates,
            measurement_candidates,
        )
        updated: list[Track] = []
        for index, track in enumerate(active):
            if index in matches:
                measurement_index, ambiguous = matches[index]
                point = points[measurement_index]
                with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
                    velocity = (
                        np.asarray(point, dtype=np.float64)
                        - np.asarray(track.position_m, dtype=np.float64)
                    ) / (now - track.last_seen_s)
                if not np.isfinite(velocity).all():
                    raise ValueError("track velocity is not representable")
                hits = track.hits + 1
                confirmed_at = track.confirmed_at_s
                if confirmed_at is None and hits >= self.config.confirm_hits:
                    confirmed_at = now
                updated.append(
                    replace(
                        track,
                        position_m=tuple(float(value) for value in point),
                        velocity_mps=tuple(float(value) for value in velocity),
                        status=(
                            TrackStatus.CONFIRMED
                            if confirmed_at is not None
                            else TrackStatus.TENTATIVE
                        ),
                        hits=hits,
                        missed_observations=0,
                        ambiguous_hits=track.ambiguous_hits + int(ambiguous),
                        last_seen_s=now,
                        confirmed_at_s=confirmed_at,
                    )
                )
                continue
            age = now - track.last_seen_s
            if age > self.config.coast_after_s:
                status = TrackStatus.COASTING
            elif track.confirmed_at_s is not None:
                status = TrackStatus.CONFIRMED
            else:
                status = TrackStatus.TENTATIVE
            updated.append(
                replace(
                    track,
                    status=status,
                    missed_observations=track.missed_observations + 1,
                )
            )
        unmatched = tuple(
            index for index in range(len(points)) if index not in assigned_measurements
        )
        birth_ids: list[int] = []
        dropped_births = 0
        slots = self.config.max_tracks - len(active)
        next_track_id = self._next_track_id
        for measurement_index in unmatched:
            if measurement_candidates[measurement_index] != 0:
                continue
            if slots <= 0:
                dropped_births += 1
                continue
            point = points[measurement_index]
            updated.append(
                Track(
                    track_id=next_track_id,
                    position_m=tuple(float(value) for value in point),
                    velocity_mps=None,
                    status=TrackStatus.TENTATIVE,
                    hits=1,
                    missed_observations=0,
                    ambiguous_hits=0,
                    first_seen_s=now,
                    last_seen_s=now,
                    confirmed_at_s=None,
                )
            )
            birth_ids.append(next_track_id)
            next_track_id += 1
            slots -= 1
        associations.sort(key=lambda item: (item.track_id, item.measurement_index))
        self._tracks = tuple(sorted(updated, key=lambda track: track.track_id))
        self._last_time_s = now
        self._next_track_id = next_track_id
        return AssociationResult(
            time_s=now,
            tracks=self._tracks,
            associations=tuple(associations),
            deletions=tuple(deletions),
            birth_track_ids=tuple(birth_ids),
            unmatched_measurements=unmatched,
            dropped_births=dropped_births,
            ambiguous_measurements=int(np.count_nonzero(measurement_candidates > 1)),
            ambiguous_associations=sum(1 for item in associations if item.ambiguous),
            gated_pairs=int(gated.sum()),
            candidate_pairs=candidate_pairs,
        )
