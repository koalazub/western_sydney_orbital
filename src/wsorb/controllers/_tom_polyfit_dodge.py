"""Native review port of Tom's ``controllers/tom/polyfit_dodge`` entrypoint.

The numeric policy is copied from the current source on ``main``. Only the STP
transport changes: radar rows and ownship state arrive through the native public
observation contract, and the reference-frame acceleration is rotated into the
body frame by the shared adapter. Tom's source file is not modified.

Two source behaviours are retained deliberately because they affect the review:

* tracked rock positions use the shuttle position from the current decision for
  every radar row, as the source does, rather than an interpolated pose at the
  row's acquisition time;
* the goal planner's early-acceleration gate uses the source expression
  ``|target| - |shuttle| > 1100`` and the live local fuel counter.

The only additions are numeric guards for zero speed and clipped cone cosines,
plus a guidance report used by the review harness.
"""

from __future__ import annotations

from collections import deque

import numpy as np

from ..control_contract import bounded, reference_to_body
from ._core import ExperimentalController


class Rock:
    def __init__(self, position, diameter, time):
        self.position = position
        self.diameter = diameter
        self.velocity = None
        self.last_seen = time
        self.history = deque(maxlen=30)
        self.history.append((time, position))

    def add_sighting(self, position, diameter, time):
        if time <= self.last_seen:
            return
        self.position = position
        self.diameter = 0.8 * self.diameter + 0.2 * diameter
        self.last_seen = time
        self.history.append((time, position))
        self.update_velocity()

    def update_velocity(self):
        times = np.array([time for time, _ in self.history])
        points = np.array([position for _, position in self.history])
        if len(times) < 3 or times[-1] - times[0] < 1.0:
            return
        self.velocity = np.polyfit(times - times[0], points, 1)[0]

    def predicted_position(self, time):
        if self.velocity is None:
            return self.position
        return self.position + self.velocity * (time - self.last_seen)


class Tracker:
    def __init__(self, gate=15.0, max_age=5.0):
        self.rocks = []
        self.gate = gate
        self.max_age = max_age

    def update(self, radar_items, shuttle_position, now, target_position):
        target_vec = target_position - shuttle_position
        target_dist = np.linalg.norm(target_vec)
        cone_cos = np.cos(np.radians(90.0))

        def debris_in_cone(rel_pos) -> bool:
            distance = np.linalg.norm(rel_pos)
            if distance < 1e-9 or target_dist < 1e-9:
                return True
            cosine = (rel_pos @ target_vec) / (distance * target_dist)
            return bool(np.clip(cosine, -1.0, 1.0) > cone_cos)

        filtered = [item for item in radar_items if debris_in_cone(item[0])]
        for item in filtered:
            position = shuttle_position + item[0, :]
            diameter, time = item[1, 2], item[2, 0]
            rock = self.find_match(position, diameter, time)
            if rock is None:
                self.rocks.append(Rock(position, diameter, time))
            else:
                rock.add_sighting(position, diameter, time)
        self.rocks = [rock for rock in self.rocks if now - rock.last_seen < self.max_age]

    def find_match(self, position, diameter, time):
        best, best_distance = None, self.gate
        for rock in self.rocks:
            if abs(rock.diameter - diameter) > 0.25 * rock.diameter:
                continue
            distance = np.linalg.norm(rock.predicted_position(time) - position)
            if distance < best_distance:
                best, best_distance = rock, distance
        return best


def clearance(my_velocity, rel_pos, rock_vel, keep_out, horizon=50.0):
    rel_vel = rock_vel - my_velocity
    speed_squared = (rel_vel**2).sum(axis=1) + 1e-9
    time = -(rel_pos * rel_vel).sum(axis=1) / speed_squared
    time = np.clip(time, 0.0, horizon)
    closest = np.linalg.norm(rel_pos + time[:, None] * rel_vel, axis=1)
    return (closest - keep_out).min()


def two_sideways_directions(direction):
    helper = np.array([0.0, 0.0, 1.0]) if abs(direction[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    side1 = np.cross(direction, helper)
    side1 /= np.linalg.norm(side1)
    side2 = np.cross(direction, side1)
    return side1, side2


def time_to_collision(my_velocity, rel_pos, rock_vel, keep_out, horizon=20.0):
    rel_vel = rock_vel - my_velocity
    a = (rel_vel**2).sum(axis=1) + 1e-9
    b = (rel_pos * rel_vel).sum(axis=1)
    c = (rel_pos**2).sum(axis=1) - keep_out**2
    discriminant = b**2 - a * c
    hit_time = np.full(len(rel_pos), np.inf)
    inside = c <= 0.0
    hit_time[inside] = 0.0
    approaching = (~inside) & (discriminant >= 0.0) & (b < 0.0)
    hit_time[approaching] = (-b[approaching] - np.sqrt(discriminant[approaching])) / a[approaching]
    hit_time[hit_time > horizon] = np.inf
    index = int(np.argmin(hit_time))
    if not np.isfinite(hit_time[index]):
        return None, None
    return float(hit_time[index]), index


class Avoider:
    def __init__(self, margin=6.0, unknown_extra=4.0, horizon=50.0, buffer=5.0):
        self.margin = margin
        self.unknown_extra = unknown_extra
        self.horizon = horizon
        self.buffer = buffer
        self.last_info = {}

    def choose(self, wanted_velocity, rocks, shuttle_position, now, current_velocity):
        wanted_velocity = np.asarray(wanted_velocity, float)
        current_velocity = np.asarray(current_velocity, float)
        speed = np.linalg.norm(wanted_velocity)
        self.last_info = {}
        if speed < 1e-3 or len(rocks) == 0:
            return wanted_velocity

        rel_pos = np.array([rock.predicted_position(now) - shuttle_position for rock in rocks])
        rock_vel = np.array(
            [rock.velocity if rock.velocity is not None else np.zeros(3) for rock in rocks]
        )
        keep_out = np.array(
            [
                rock.diameter / 2
                + self.margin
                + (self.unknown_extra if rock.velocity is None else 0.0)
                for rock in rocks
            ]
        )

        hit_time, hit_index = time_to_collision(
            wanted_velocity, rel_pos, rock_vel, keep_out, self.horizon
        )

        def score(velocity):
            return clearance(velocity, rel_pos, rock_vel, keep_out, self.horizon)

        wanted_clearance = float(score(wanted_velocity))
        info = {
            "track_count": len(rocks),
            "unknown_velocity_count": sum(rock.velocity is None for rock in rocks),
            "wanted_clearance_m": wanted_clearance,
            "next_collision_s": hit_time,
            "next_rock_diameter_m": float(rocks[hit_index].diameter)
            if hit_index is not None
            else None,
            "candidate_count": 0,
            "safe_candidate_count": 0,
            "dodge_active": False,
            "chosen_clearance_m": wanted_clearance,
            "delta_v_from_current_mps": 0.0,
        }
        self.last_info = info

        if wanted_clearance > 0:
            return wanted_velocity

        forward = wanted_velocity / speed
        side1, side2 = two_sideways_directions(forward)
        candidates = []
        for size in (0.05, 0.1, 0.2, 0.35, 0.5, 0.75):
            for angle in np.linspace(0, 2 * np.pi, 12, endpoint=False):
                sideways = np.cos(angle) * side1 + np.sin(angle) * side2
                for along in (-0.3, 0.0, 0.3):
                    candidates.append(wanted_velocity + speed * (size * sideways + along * forward))
        for fraction in (0.5, 0.25, 0.0):
            candidates.append(wanted_velocity * fraction)

        scored = [(candidate, float(score(candidate))) for candidate in candidates]
        safe = [candidate for candidate, value in scored if value > self.buffer]
        if safe:
            best = min(safe, key=lambda candidate: np.linalg.norm(candidate - current_velocity))
        else:
            best = max(scored, key=lambda item: item[1])[0]

        info.update(
            candidate_count=len(candidates),
            safe_candidate_count=len(safe),
            dodge_active=True,
            chosen_clearance_m=float(score(best)),
            delta_v_from_current_mps=float(np.linalg.norm(best - current_velocity)),
        )
        return best


class BurnGovernor:
    def __init__(self, gain=5.0, start=0.3, stop=0.1):
        self.gain = gain
        self.start = start
        self.stop = stop
        self.burning = False

    def acceleration(self, wanted_velocity, current_velocity):
        error = np.asarray(wanted_velocity, float) - np.asarray(current_velocity, float)
        size = np.linalg.norm(error)
        if self.burning and size < self.stop:
            self.burning = False
        elif not self.burning and size > self.start:
            self.burning = True
        return self.gain * error if self.burning else np.zeros(3)


class GoalPlanner:
    def __init__(self, cruise_speed, goal_radius=40.0, start_fraction=0.7, stop_fraction=0.3):
        self.cruise_speed = cruise_speed
        self.goal_radius = goal_radius
        self.start = start_fraction * goal_radius
        self.stop = stop_fraction * goal_radius
        self.correcting = True

    def miss_distance(self, shuttle_position, shuttle_velocity, target_position, fuel_used):
        speed = np.linalg.norm(shuttle_velocity)
        if speed <= 1e-9:
            return np.inf
        if (
            speed < 0.95 * self.cruise_speed
            and np.linalg.norm(target_position) - np.linalg.norm(shuttle_position) > 1100.0
            and fuel_used < 35.0
        ):
            return np.inf
        heading = shuttle_velocity / speed
        to_target = target_position - shuttle_position
        along = to_target @ heading
        if along <= 0:
            return np.inf
        return float(np.linalg.norm(to_target - along * heading))

    def wanted_velocity(self, shuttle_position, shuttle_velocity, target_position, fuel_used):
        miss = self.miss_distance(shuttle_position, shuttle_velocity, target_position, fuel_used)
        if self.correcting and miss < self.stop:
            self.correcting = False
        elif not self.correcting and miss > self.start:
            self.correcting = True
        if not self.correcting:
            return np.asarray(shuttle_velocity, float)
        to_target = target_position - shuttle_position
        distance = np.linalg.norm(to_target)
        if distance <= 1e-9:
            return np.zeros(3)
        return self.cruise_speed * to_target / distance


class TomPolyfitDodgeController(ExperimentalController):
    def __init__(self, metadata, settings=None):
        super().__init__(metadata)
        settings = settings if isinstance(settings, dict) else {}
        self.tracker = Tracker()
        self.avoider = Avoider()
        self.governor = BurnGovernor(gain=5.0)
        self.planner = GoalPlanner(cruise_speed=float(settings.get("cruise_speed_mps", 6.0)))
        self.target_position = None
        self.current_position = np.zeros(3)
        self.fuel_used_kg = 0.0

    def _ingest(self, epoch, rows):
        if self.target_position is None:
            return
        # Retain the source's current-pose rebase for every radar row. This is
        # why the review also reports radar age separately.
        self.tracker.update(rows, self.current_position, epoch, self.target_position)

    def _expire(self, now):
        self.tracker.rocks = [
            rock for rock in self.tracker.rocks if now - rock.last_seen < self.tracker.max_age
        ]

    def _calculate(self, sample, rows, now):
        self.target_position = np.asarray(sample.target_position_world_m, float)
        self.current_position = np.asarray(sample.position_world_m, float)
        self.fuel_used_kg = float(sample.fuel_used_kg)
        if not self._track_rows(rows, limit=150, now=now):
            self.tracker.update(rows[:150], self.current_position, now, self.target_position)
        velocity = np.asarray(sample.velocity_world_mps, float)
        goal = self.planner.wanted_velocity(
            self.current_position, velocity, self.target_position, self.fuel_used_kg
        )
        safe = self.avoider.choose(goal, self.tracker.rocks, self.current_position, now, velocity)
        accel_world = self.governor.acceleration(safe, velocity)
        accel_body = reference_to_body(
            bounded(accel_world, 0.5 - 4.0 * np.spacing(0.5)),
            sample.quaternion_xyzw,
            sample.rotation_convention,
        )
        row_epochs = rows[:, 2, 0] if len(rows) else np.array([now])
        self._report["guidance"] = {
            **self.avoider.last_info,
            "radar_rows": len(rows),
            "radar_oldest_row_age_s": float(now - np.min(row_epochs)),
            "radar_newest_row_age_s": float(now - np.max(row_epochs)),
            "cruise_speed_mps": self.planner.cruise_speed,
            "governor_gain_s": self.governor.gain,
            "goal_radius_m": self.planner.goal_radius,
            "goal_correcting": self.planner.correcting,
            "goal_velocity_mps": goal.tolist(),
            "safe_velocity_mps": safe.tolist(),
            "fuel_used_kg": self.fuel_used_kg,
            "model": (
                "Native review port of Tom's polyfit_dodge tracker, time-to-collision "
                "report, velocity avoider and goal planner"
            ),
        }
        return accel_body, np.zeros(3)
