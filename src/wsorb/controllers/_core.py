"""Input eligibility and observer history for experimental numeric adapters."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping
from typing import Any

import numpy as np

from ..control_contract import (
    ControlSample,
    checked_sample,
    coast_command,
    radar_rows,
    scalar,
    vector,
)
from .observation import MAX_RADAR_AGE_S, MAX_REPORT_AGE_S, RADAR_CLOCK, SOURCE_KINDS

_TIME_TOLERANCE_S = 1.0e-9


class _Rejected(ValueError):
    pass


class ObserverHistory:
    """Interpolate actual supplied poses only, never backdate a current pose."""

    def __init__(self) -> None:
        self.samples: deque[ControlSample] = deque(maxlen=512)

    def add(self, sample: ControlSample) -> bool:
        if self.samples and sample.elapsed_time_s == self.samples[-1].elapsed_time_s:
            previous = self.samples[-1]
            if any(
                not np.array_equal(getattr(previous, name), getattr(sample, name))
                for name in (
                    "position_world_m",
                    "velocity_world_mps",
                    "quaternion_xyzw",
                    "angular_velocity_world_rad_s",
                    "target_position_world_m",
                )
            ):
                raise _Rejected("Navigation changed at the same source epoch")
            return False
        self.samples.append(sample)
        while self.samples and sample.elapsed_time_s - self.samples[0].elapsed_time_s > 15.0:
            self.samples.popleft()
        return True

    def covers(self, epoch: float) -> bool:
        return bool(
            self.samples
            and self.samples[0].elapsed_time_s - _TIME_TOLERANCE_S
            <= epoch
            <= self.samples[-1].elapsed_time_s + _TIME_TOLERANCE_S
        )

    def at(self, epoch: float) -> tuple[np.ndarray, np.ndarray]:
        if not self.covers(epoch):
            raise _Rejected("Observer history does not cover a radar row epoch")
        epoch = min(
            max(epoch, self.samples[0].elapsed_time_s), self.samples[-1].elapsed_time_s
        )
        previous = self.samples[0]
        for current in self.samples:
            if current.elapsed_time_s == epoch:
                return current.position_world_m.copy(), current.velocity_world_mps.copy()
            if current.elapsed_time_s > epoch:
                weight = (epoch - previous.elapsed_time_s) / (
                    current.elapsed_time_s - previous.elapsed_time_s
                )
                return (
                    previous.position_world_m
                    + weight * (current.position_world_m - previous.position_world_m),
                    previous.velocity_world_mps
                    + weight * (current.velocity_world_mps - previous.velocity_world_mps),
                )
            previous = current
        raise _Rejected("Observer history does not cover a radar row epoch")


class ExperimentalController:
    """Eligibility is numeric readiness, never an obstacle-clearance approval."""

    def __init__(self, metadata: Mapping[str, Any]) -> None:
        self.metadata = dict(metadata)
        self.history = ObserverHistory()
        self._source = None
        self._last_now = None
        self._last_elapsed = None
        self._last_publication = None
        self._last_radar_epoch = None
        self._last_navigation_sequence = None
        self._invalidated = None
        self._calls = 0
        self._pending: dict[float, np.ndarray] = {}
        self._ingested_epoch = -np.inf
        self._report = self._new_report()

    def _new_report(self) -> dict[str, Any]:
        return {
            "schema": "wsorb.experimental_controller_report",
            "schema_version": 1,
            "controller_id": self.metadata["id"],
            "owner": self.metadata["owner"],
            "profile": self.metadata["profile"],
            "experimental": True,
            "best_effort": True,
            "eligible": False,
            "decision_valid_until_elapsed_s": None,
            "calls": self._calls,
            "failure_reasons": [],
            "source_freshness": {
                "radar_clock": RADAR_CLOCK,
                "declared_radar_clock_offset_s": 0.0,
                "max_report_age_s": MAX_REPORT_AGE_S,
                "max_radar_age_s": MAX_RADAR_AGE_S,
            },
            "model_limits": (
                "Experimental public-sensor estimates only, not certified obstacle clearance, "
                "terrain avoidance, STP equivalence or docking. Coast does not stop motion. "
                "Native arrival is the mission's outcome, not an adapter distance test."
            ),
            "command": None,
        }

    @property
    def report(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._report, allow_nan=False))

    def _invalidate(self, reason: str) -> None:
        self._invalidated = reason
        raise _Rejected(reason)

    def _validate(self, sample, debris, now):
        checked_sample(sample)
        now = scalar(now, "now_elapsed_time_s")
        elapsed = float(sample.elapsed_time_s)
        if self._invalidated is not None:
            raise _Rejected(self._invalidated)
        if sample.source_kind not in SOURCE_KINDS or sample.rotation_convention != "active_xyzw":
            raise _Rejected("Unsupported native source or rotation convention")
        if getattr(sample, "radar_clock", None) != RADAR_CLOCK:
            raise _Rejected("Unsupported native radar clock")
        if any(
            getattr(sample, name, None) is None
            for name in (
                "public_context",
                "radar_sample_time_s",
                "navigation_sequence",
                "radar_sequence",
            )
        ):
            raise _Rejected("Missing declared native sensor source context")
        source = (
            sample.frame,
            sample.source_kind,
            sample.rotation_convention,
            getattr(sample, "public_context", None),
        )
        if self._source is not None and source != self._source:
            self._invalidate("Source frame/context changed; create a fresh controller")
        if self._last_now is not None and (now < self._last_now or elapsed < self._last_elapsed):
            self._invalidate("Source clock rewound; create a fresh controller")
        if not 0 <= now - elapsed <= MAX_REPORT_AGE_S:
            raise _Rejected("Future or stale navigation sample")
        rows = radar_rows(debris)
        radar_time = getattr(sample, "radar_sample_time_s", None)
        rolling = bool(getattr(sample, "radar_rolling", False))
        if len(rows):
            if rolling:
                # The official rolling scan stamps each row at its 120 Hz
                # acquisition time; the publication time is at or after the
                # newest row. Age every row from its own epoch.
                bad = (
                    np.any(rows[:, 2, 0] > now + _TIME_TOLERANCE_S)
                    or np.any(now - rows[:, 2, 0] > MAX_RADAR_AGE_S + _TIME_TOLERANCE_S)
                    or (
                        radar_time is not None
                        and np.any(rows[:, 2, 0] > radar_time + _TIME_TOLERANCE_S)
                    )
                )
            else:
                bad = np.any(rows[:, 2, 0] > now) or np.any(now - rows[:, 2, 0] > MAX_RADAR_AGE_S)
                if radar_time is not None:
                    bad = bad or np.max(rows[:, 2, 0]) != radar_time
            if bad:
                raise _Rejected("Future or stale radar row epoch")
        if radar_time is not None:
            age = now - radar_time
            if age < -_TIME_TOLERANCE_S or age > MAX_RADAR_AGE_S + _TIME_TOLERANCE_S:
                raise _Rejected("Future or stale radar publication")
        newest_row = float(np.max(rows[:, 2, 0])) if len(rows) else None
        if (
            newest_row is not None
            and self._last_radar_epoch is not None
            and newest_row < self._last_radar_epoch
        ):
            self._invalidate("Radar row clock rewound; create a fresh controller")
        navigation_sequence = getattr(sample, "navigation_sequence", None)
        if (
            navigation_sequence is not None
            and self._last_navigation_sequence is not None
            and navigation_sequence < self._last_navigation_sequence
        ):
            self._invalidate("Navigation source sequence rewound; create a fresh controller")
        publication = (
            getattr(sample, "radar_sequence", None),
            radar_time,
        )
        if self._last_publication is not None and publication[0] is not None:
            seq, epoch = self._last_publication
            if publication[0] < seq or publication[1] < epoch:
                self._invalidate("Radar source clock/sequence rewound; create a fresh controller")
            if publication[0] == seq and publication[1] != epoch:
                self._invalidate("Radar publication changed without a new source sequence")
        expiry = elapsed + MAX_REPORT_AGE_S
        if radar_time is not None:
            expiry = min(expiry, radar_time + MAX_RADAR_AGE_S)
        if len(rows):
            expiry = min(expiry, float(np.min(rows[:, 2, 0])) + MAX_RADAR_AGE_S)
        self._report["decision_valid_until_elapsed_s"] = expiry
        self._report["source_freshness"].update(
            sample_elapsed_time_s=elapsed,
            now_elapsed_time_s=now,
            radar_sample_time_s=radar_time,
            frame=sample.frame,
            source_kind=sample.source_kind,
            raw_radar_rows=len(rows),
        )
        self._source = source
        self._last_now, self._last_elapsed = now, elapsed
        self._last_publication = publication
        if newest_row is not None:
            self._last_radar_epoch = newest_row
        self._last_navigation_sequence = navigation_sequence
        try:
            if self.history.add(sample):
                self._observe(sample)
        except _Rejected as error:
            self._invalidate(str(error))
        return rows, now

    def _observe(self, sample: ControlSample) -> None:
        pass

    def _track_rows(self, rows: np.ndarray, *, limit: int, now: float) -> bool:
        """Split original nearest rows by epoch and wait for actual observer history.

        A row newer than navigation is buffered until history covers it. A row
        older than the first actual pose cannot be reconstructed and is omitted,
        with this decision ineligible. No clock offset is inferred from any row.
        """
        selected = rows[:limit]
        for epoch in np.unique(selected[:, 2, 0]):
            if epoch > self._ingested_epoch:
                self._pending[float(epoch)] = selected[selected[:, 2, 0] == epoch].copy()
        self._pending = {
            epoch: group
            for epoch, group in self._pending.items()
            if now - epoch <= MAX_RADAR_AGE_S
            and epoch > self._ingested_epoch
            and epoch >= self.history.samples[0].elapsed_time_s
        }
        ready = True
        ingested = 0
        for epoch, group in sorted(self._pending.items()):
            if not self.history.covers(epoch):
                ready = False
                continue
            self._ingest(epoch, group)
            self._ingested_epoch = epoch
            ingested += len(group)
        self._pending = {
            epoch: group for epoch, group in self._pending.items() if epoch > self._ingested_epoch
        }
        # Even a previously skipped old row cannot silently acquire a new pose.
        ready = ready and all(self.history.covers(float(t)) for t in selected[:, 2, 0])
        self._expire(now)
        self._report["tracking"] = {
            "selected_original_rows": len(selected),
            "ingested_rows": ingested,
            "pending_epochs": len(self._pending),
            "observer_history_covered": ready,
            "row_limit": limit,
            "identities": "tracker bookkeeping, not physical object identities",
        }
        return ready

    def _ingest(self, epoch: float, rows: np.ndarray) -> None:
        raise NotImplementedError

    def _expire(self, now: float) -> None:
        raise NotImplementedError

    def _calculate(self, sample, rows, now):
        raise NotImplementedError

    def command(self, sample: ControlSample, debris, *, now_elapsed_time_s: float):
        self._calls += 1
        self._report = self._new_report()
        try:
            rows, now = self._validate(sample, debris, now_elapsed_time_s)
            with np.errstate(over="raise", invalid="raise", divide="raise"):
                accel, angular = self._calculate(sample, rows, now)
            accel, angular = vector(accel, "body acceleration"), vector(angular, "body angular")
            # Reject a broken profile. Never approve an old prediction and then clip.
            if np.linalg.norm(accel) > 0.5 or np.linalg.norm(angular) > 0.05:
                raise _Rejected("Experimental profile exceeded its pre-assessment command bounds")
            self._report["eligible"] = True
        except _Rejected as error:
            self._report["failure_reasons"].append(str(error))
            accel, angular = coast_command()
        except (ValueError, TypeError, OverflowError, FloatingPointError, np.linalg.LinAlgError):
            self._report["failure_reasons"].append(
                "Invalid numeric source sample or profile calculation"
            )
            accel, angular = coast_command()
        self._report["command"] = {
            "accel_body_mps2": accel.tolist(),
            "angular_accel_body_rad_s2": angular.tolist(),
            "written_to_any_api": False,
        }
        return accel, angular
