"""Faithful native port of Tom's 8 m/s polyfit controller for local comparison.

The numerical core is copied verbatim from
``controllers/tom/polyfit/Controller.py`` (Rock/Tracker/Avoider/BurnGovernor/
GoalPlanner at the commit that added the native polyfit reference). Only the
STP transport is replaced: radar rows and shuttle state come from the native
observation contract, and the world-frame acceleration is rotated into the
body frame by the shared adapter. Tom's source file is not modified.
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
        times = np.array([t for t, p in self.history])
        points = np.array([p for t, p in self.history])
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
        cone_angle = np.radians(30.0)
        core_bubble = 50.0
        target_vec = target_position - shuttle_position

        def debris_in_cone(debris) -> bool:
            debris_pos = debris[0]
            if debris[1][0] < core_bubble:
                return True
            dot = np.dot(target_vec, debris_pos)
            angle = np.arccos(
                np.clip(
                    dot / (np.linalg.norm(debris_pos) * np.linalg.norm(target_vec)),
                    -1.0,
                    1.0,
                )
            )
            return dot > 0.0 and angle < cone_angle

        for item in radar_items:
            if not debris_in_cone(item):
                continue
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
    t = -(rel_pos * rel_vel).sum(axis=1) / speed_squared
    t = np.clip(t, 0.0, horizon)
    closest = np.linalg.norm(rel_pos + t[:, None] * rel_vel, axis=1)
    return (closest - keep_out).min()


def two_sideways_directions(direction):
    helper = np.array([0.0, 0.0, 1.0]) if abs(direction[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    side1 = np.cross(direction, helper)
    side1 /= np.linalg.norm(side1)
    side2 = np.cross(direction, side1)
    return side1, side2


class Avoider:
    def __init__(self, margin=6.0, unknown_extra=4.0, horizon=50.0):
        self.margin = margin
        self.unknown_extra = unknown_extra
        self.horizon = horizon
        self.last_dodge = None

    def choose(self, wanted_velocity, rocks, shuttle_position, now):
        wanted_velocity = np.asarray(wanted_velocity, float)
        speed = np.linalg.norm(wanted_velocity)
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

        def score(velocity):
            return clearance(velocity, rel_pos, rock_vel, keep_out, self.horizon)

        if score(wanted_velocity) > 0:
            self.last_dodge = None
            return wanted_velocity

        if self.last_dodge is not None and score(self.last_dodge) > 0:
            return self.last_dodge

        side1, side2 = two_sideways_directions(wanted_velocity / speed)
        tried = [wanted_velocity]
        for size in (0.05, 0.1, 0.2, 0.35, 0.5, 0.75):
            for angle in np.linspace(0, 2 * np.pi, 8, endpoint=False):
                sideways = np.cos(angle) * side1 + np.sin(angle) * side2
                candidate = wanted_velocity + size * speed * sideways
                if score(candidate) > 0:
                    self.last_dodge = candidate
                    return candidate
                tried.append(candidate)

        for fraction in (0.5, 0.25, 0.0):
            candidate = wanted_velocity * fraction
            if score(candidate) > 0:
                self.last_dodge = candidate
                return candidate
            tried.append(candidate)

        self.last_dodge = max(tried, key=score)
        return self.last_dodge


class BurnGovernor:
    def __init__(self, gain=50.0, start=0.3, stop=0.1):
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
    def __init__(self, cruise_speed, goal_radius=15.0, start_fraction=0.7, stop_fraction=0.3):
        self.cruise_speed = cruise_speed
        self.goal_radius = goal_radius
        self.start = start_fraction * goal_radius
        self.stop = stop_fraction * goal_radius
        self.correcting = True

    def miss_distance(self, shuttle_position, shuttle_velocity, target_position):
        speed = np.linalg.norm(shuttle_velocity)
        if speed < 0.98 * self.cruise_speed:
            return np.inf
        heading = shuttle_velocity / speed
        to_target = target_position - shuttle_position
        along = to_target @ heading
        if along <= 0:
            return np.inf
        return np.linalg.norm(to_target - along * heading)

    def wanted_velocity(self, shuttle_position, shuttle_velocity, target_position):
        miss = self.miss_distance(shuttle_position, shuttle_velocity, target_position)
        if self.correcting and miss < self.stop:
            self.correcting = False
        elif not self.correcting and miss > self.start:
            self.correcting = True
        if not self.correcting:
            return np.asarray(shuttle_velocity, float)
        to_target = target_position - shuttle_position
        return self.cruise_speed * to_target / np.linalg.norm(to_target)


class TomPolyfitController(ExperimentalController):
    def __init__(self, metadata):
        super().__init__(metadata)
        self.tracker = Tracker()
        self.avoider = Avoider()
        self.governor = BurnGovernor(gain=50.0)
        self.planner = GoalPlanner(cruise_speed=8.0)
        self.target_position = None

    def _ingest(self, epoch, rows):
        if self.target_position is None:
            return
        position, _ = self.history.at(epoch)
        self.tracker.update(rows, position, epoch, self.target_position)

    def _expire(self, now):
        self.tracker.rocks = [
            rock for rock in self.tracker.rocks if now - rock.last_seen < self.tracker.max_age
        ]

    def _calculate(self, sample, rows, now):
        self.target_position = np.asarray(sample.target_position_world_m, float)
        if not self._track_rows(rows, limit=500, now=now):
            self.tracker.update(
                rows,
                np.asarray(sample.position_world_m, float),
                now,
                self.target_position,
            )
        position = np.asarray(sample.position_world_m, float)
        velocity = np.asarray(sample.velocity_world_mps, float)
        goal = self.planner.wanted_velocity(position, velocity, self.target_position)
        safe = self.avoider.choose(goal, self.tracker.rocks, position, now)
        accel_world = self.governor.acceleration(safe, velocity)
        accel_body = reference_to_body(
            bounded(accel_world, 0.5 - 4.0 * np.spacing(0.5)),
            sample.quaternion_xyzw,
            sample.rotation_convention,
        )
        self._report["guidance"] = {
            "track_count": len(self.tracker.rocks),
            "cruise_speed_mps": self.planner.cruise_speed,
            "governor_gain_s": self.governor.gain,
            "accel_norm_cap_mps2": 0.5,
            "goal_correcting": self.planner.correcting,
            "goal_velocity_mps": safe.tolist(),
            "dodge_active": self.avoider.last_dodge is not None,
            "model": (
                "Native port of Tom's 8 m/s polyfit tracker, velocity avoider and goal planner"
            ),
        }
        return accel_body, np.zeros(3)
