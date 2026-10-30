"""Bounded, persistent local geometry advice, never stopping clearance.

Inputs are calculated arrays, not observations. A short nominal sweep uses the
supplied current radii without future growth. It cannot certify uncertainty,
flight dynamics, unobserved debris, a complete station route or command authority.
The caller must retain its independent full raw time-indexed stopping checks.
There is deliberately no exception for starting inside the Phobos enclosure.
"""

from __future__ import annotations

import heapq
import math
import time
from dataclasses import dataclass, field
from itertools import product

import numpy as np

from ._validation import Vec3, difference, distance, point, scalar
from .config import PlannerConfig
from .geometry import Capsule, CollisionIndex
from .temporal import TemporalObstacles

__all__ = ["NominalRouteAdvice", "NominalRouteAdviser", "nominal_escape_waypoint"]

_EXPANSIONS_PER_CALL = 4
_MAX_EXPANSIONS = 32
_MAX_NODES = 64
_MAX_HEAP_ENTRIES = 64
_MAX_VISIBILITY_CHECKS = 24
_MAX_ACTIVE_CAPSULES = 128
_MAX_PATH_POINTS = 10
_PADDING = 2048.0 * float(np.finfo(np.float64).eps)
_KIND = "nominal_local_geometry_not_safety_clearance_or_a_complete_route"


@dataclass(frozen=True, slots=True)
class NominalRouteAdvice:
    target_world_m: Vec3 | None
    points_world_m: tuple[Vec3, ...]
    local_goal_world_m: Vec3 | None
    source_time_s: float
    planned_time_s: float | None
    status: str
    detail: str
    reused: bool = False
    expanded_nodes: int = 0
    search_expanded_nodes: int = 0
    stored_nodes: int = 0
    peak_heap_entries: int = 0
    collision_checks: int = 0
    supplied_obstacles: int = 0
    waypoint_tolerance_m: float = 0.0
    nominal_sweep_horizon_s: float = 0.0
    kind: str = field(default=_KIND, init=False)
    safety_clearance: bool = field(default=False, init=False)
    complete_route: bool = field(default=False, init=False)


def _unit(value):
    length = math.hypot(*value)
    if not math.isfinite(length) or length == 0.0:
        raise ValueError("Nominal route direction is not representable")
    return np.asarray(value, dtype=float) / length


def _perpendicular(axis, preferred):
    result = preferred - axis * np.dot(preferred, axis)
    if math.hypot(*result) < 1e-8:
        reference = np.eye(3)[int(np.argmin(abs(axis)))]
        result = reference - axis * np.dot(reference, axis)
    return _unit(result)


def nominal_escape_waypoint(
    position,
    target_goal,
    config: PlannerConfig,
    *,
    radial_tolerance_m=0.0,
) -> Vec3:
    """A deterministic 200 m tangent, 40 m outward staging point by default.

    The public goal chooses the tangent sign; a Cartesian tie-break handles a
    radial goal. This is a macro-goal suggestion, not a claim that its lane is
    clear. ``choose(primary_goal=...)`` can instead follow the caller's bypass.
    """
    if not isinstance(config, PlannerConfig):
        raise ValueError("config must be a PlannerConfig")
    position = point(position, "position")
    target_goal = point(target_goal, "target_goal")
    normal = _unit(position)
    preferred = np.asarray(difference(target_goal, position))
    transverse = preferred - normal * np.dot(preferred, normal)
    tolerance = scalar(radial_tolerance_m, "radial_tolerance_m")
    if math.hypot(*transverse) <= tolerance:
        preferred = np.eye(3)[int(np.argmin(abs(normal)))]
    tangent = _perpendicular(normal, preferred)
    side = min(200.0, config.corridor_half_width_m)
    outward = min(40.0, config.cell_size_m)
    return point(np.asarray(position) + side * tangent + outward * normal, "escape waypoint")


class _BudgetReached(Exception):
    pass


class _GeometryBudget(Exception):
    pass


@dataclass(slots=True)
class _Work:
    deadline: float | None
    checks: int = 0
    expanded: int = 0

    def check(self):
        if self.deadline is not None and time.perf_counter() >= self.deadline:
            raise _BudgetReached

    def visibility(self):
        self.check()
        if self.checks >= _MAX_VISIBILITY_CHECKS:
            raise _BudgetReached
        self.checks += 1


def _bounds(points):
    values = np.asarray(points, dtype=float)
    padding = 1e-8 + _PADDING * float(np.max(abs(values)))
    return values.min(axis=0) - padding, values.max(axis=0) + padding


class _Geometry:
    """Keep every input row; build capsules only after an all-row AABB query.

    The index covers the entire declared search box, not selected endpoints.
    Exceeding its construction budget rejects advice rather than dropping rows.
    """

    def __init__(self, obstacles, horizon, bounds):
        self.obstacles = obstacles
        self.ends = obstacles.centers_m + obstacles.velocities_mps * horizon
        magnitude = np.maximum(
            np.maximum(abs(obstacles.centers_m), abs(self.ends)).max(axis=1),
            obstacles.radii_m,
        )
        padding = obstacles.radii_m + 1e-8 + _PADDING * magnitude
        minimum = np.minimum(obstacles.centers_m, self.ends) - padding[:, None]
        maximum = np.maximum(obstacles.centers_m, self.ends) + padding[:, None]
        if not all(np.isfinite(array).all() for array in (self.ends, minimum, maximum)):
            raise ValueError("Nominal obstacle bounds are not representable")
        self.rows = np.flatnonzero(
            np.all(minimum <= bounds[1], axis=1) & np.all(bounds[0] <= maximum, axis=1)
        )
        self.index = None

    def clear(self, start, end, work):
        work.visibility()
        if self.index is None:
            if len(self.rows) > _MAX_ACTIVE_CAPSULES:
                raise _GeometryBudget
            obstacles = self.obstacles
            self.index = CollisionIndex(
                Capsule(obstacles.centers_m[i], self.ends[i], obstacles.radii_m[i])
                for i in self.rows
            )
            work.check()
        return self.index.clear(start, end)

    def blocker(self, start, goal):
        """Enclosing spheres propose portals; exact capsules check every edge."""
        obstacles = self.obstacles
        rows = self.rows
        centers = obstacles.centers_m[rows]
        displacement = self.ends[rows] - centers
        centers = centers + 0.5 * displacement
        radii = obstacles.radii_m[rows] + 0.5 * np.hypot.reduce(displacement, axis=1)
        forward = _unit(difference(goal, start))
        relative = centers - start
        along = relative @ forward
        lateral = np.hypot.reduce(relative - along[:, None] * forward, axis=1)
        clipped = np.clip(along, 0.0, distance(start, goal))
        gap = np.hypot.reduce(relative - clipped[:, None] * forward, axis=1) - radii
        eligible = np.flatnonzero(gap <= 1e-8 + _PADDING * radii)
        if not len(eligible):
            return None
        entry = along[eligible] - np.sqrt(
            np.maximum(0.0, radii[eligible] ** 2 - lateral[eligible] ** 2)
        )
        # Geometry, not anonymous row order, breaks equal-distance ties.
        order = np.lexsort(
            (
                radii[eligible],
                centers[eligible, 2],
                centers[eligible, 1],
                centers[eligible, 0],
                entry,
            )
        )
        index = int(eligible[order[0]])
        return centers[index], float(radii[index])


class _Corridor:
    def __init__(self, start, goal, config, lookahead):
        self.start = np.asarray(start)
        self.length = distance(start, goal)
        self.forward = _unit(difference(goal, start))
        self.across = _perpendicular(self.forward, _unit(start))
        self.up = np.cross(self.forward, self.across)
        self.width = min(lookahead, config.corridor_half_width_m)
        self.margin = min(config.cell_size_m, lookahead * 0.25)
        self.portal_margin = min(max(4.0, config.cell_size_m * 0.5), self.width * 0.5)
        corners = [
            point(
                self.start + along * self.forward + across * self.across + up * self.up,
                "nominal corridor corner",
            )
            for along, across, up in product(
                (-self.margin, self.length + self.margin),
                (-self.width, self.width),
                (-self.width, self.width),
            )
        ]
        self.bounds = _bounds(corners)

    def contains(self, value):
        relative = np.asarray(value) - self.start
        along = float(relative @ self.forward)
        return (
            -self.margin <= along <= self.length + self.margin
            and abs(float(relative @ self.across)) <= self.width
            and abs(float(relative @ self.up)) <= self.width
        )

    def local_endpoints(self, goal):
        """Free intermediate frontiers may replace an occupied ray endpoint."""
        candidates = [goal]
        for angle in (math.pi / 8, math.pi / 4):
            for side in (self.across, -self.across, self.up, -self.up):
                candidate = point(
                    self.start
                    + self.length * (self.forward * math.cos(angle) + side * math.sin(angle)),
                    "nominal intermediate frontier",
                )
                if self.contains(candidate):
                    candidates.append(candidate)
        candidates.append(
            point(
                self.start + self.forward * self.length * 0.5,
                "shorter nominal intermediate frontier",
            )
        )
        return tuple(candidates)

    def portals(self, start, blocker):
        if blocker is None:
            return ()
        center, radius = blocker
        separation = distance(start, point(center, "blocker center"))
        if separation <= radius:
            # A swept capsule's enclosing sphere can contain a clear start.
            # Do not invent clearance through it to make the search succeed.
            return ()
        normal = _unit(np.asarray(start) - center)
        across = _perpendicular(normal, self.across)
        up = np.cross(normal, across)
        outside = radius + self.portal_margin
        angle = min(
            math.pi * 0.5,
            0.9 * (math.acos(radius / separation) + math.acos(radius / outside)),
        )
        candidates = []
        for side in (across, -across, up, -up):
            candidate = point(
                center + outside * (normal * math.cos(angle) + side * math.sin(angle)),
                "nominal portal",
            )
            if self.contains(candidate):
                candidates.append(candidate)
        return tuple(candidates)


@dataclass(slots=True)
class _Node:
    position: Vec3
    cost: float
    parent: int | None


class _Search:
    """Incremental visibility search in one frozen nominal snapshot."""

    def __init__(self, start, goal, geometry, corridor, config, source_time_s):
        self.goal, self.geometry, self.corridor = goal, geometry, corridor
        self.config, self.source_time_s = config, source_time_s
        self.nodes = [_Node(start, 0.0, None)]
        self.known = {start: 0}
        heuristic = distance(start, goal)
        self.opened = [(heuristic, heuristic, 0)]
        self.expanded = 0
        self.peak_heap = 1
        self.active = None
        self.portals = None
        self.portal_index = 0
        self.pending = ()
        self.failure = None
        self.checked_start = False
        self.checked_goal = False
        self.goal_candidates = corridor.local_endpoints(goal)
        self.goal_cursor = 0

    def advance(self, work):
        if not self.checked_start:
            if not self.geometry.clear(self.nodes[0].position, self.nodes[0].position, work):
                self.failure = "Nominal start is blocked; radial departure belongs to the caller"
                return
            self.checked_start = True
        if not self.checked_goal:
            while self.goal_cursor < len(self.goal_candidates):
                candidate = self.goal_candidates[self.goal_cursor]
                if self.geometry.clear(candidate, candidate, work):
                    self.goal = candidate
                    self.checked_goal = True
                    heuristic = distance(self.nodes[0].position, candidate)
                    self.opened = [(heuristic, heuristic, 0)]
                    break
                self.goal_cursor += 1
            if not self.checked_goal:
                self.failure = (
                    "Preferred local endpoint and bounded intermediate frontiers are blocked"
                )
                return
        while not self.pending and self.failure is None:
            work.check()
            if self.active is None:
                if not self.opened:
                    self.failure = "No nominal path in the bounded local corridor"
                    return
                if self.expanded >= min(_MAX_EXPANSIONS, self.config.max_expansions):
                    self.failure = "Nominal search reached its total expansion limit"
                    return
                if work.expanded >= _EXPANSIONS_PER_CALL:
                    return
                _, _, self.active = heapq.heappop(self.opened)
                self.expanded += 1
                work.expanded += 1
                self.portals = None
                self.portal_index = 0
            node = self.nodes[self.active]
            if self.portals is None:
                if self.geometry.clear(node.position, self.goal, work):
                    reverse = [self.goal]
                    cursor = self.active
                    while cursor is not None and len(reverse) <= _MAX_PATH_POINTS:
                        current = self.nodes[cursor]
                        reverse.append(current.position)
                        cursor = current.parent
                    if cursor is not None or len(reverse) > _MAX_PATH_POINTS:
                        self.failure = (
                            "Nominal path exceeds the point budget; no shortcut substituted"
                        )
                        return
                    self.pending = tuple(reversed(reverse))
                    return
                self.portals = self.corridor.portals(
                    node.position, self.geometry.blocker(node.position, self.goal)
                )
            while self.portal_index < len(self.portals):
                candidate = self.portals[self.portal_index]
                if candidate in self.known:
                    self.portal_index += 1
                    continue
                if not self.geometry.clear(node.position, candidate, work):
                    self.portal_index += 1
                    continue
                if len(self.nodes) >= min(_MAX_NODES, self.config.max_nodes):
                    self.failure = "Nominal search reached its stored-node limit"
                    return
                if len(self.opened) >= min(_MAX_HEAP_ENTRIES, self.config.max_heap_entries):
                    self.failure = "Nominal search reached its heap-entry limit"
                    return
                cost = node.cost + distance(node.position, candidate)
                heuristic = distance(candidate, self.goal)
                if not math.isfinite(cost + heuristic):
                    raise ValueError("Nominal search priority is not representable")
                index = len(self.nodes)
                self.nodes.append(_Node(candidate, cost, self.active))
                self.known[candidate] = index
                heapq.heappush(self.opened, (cost + heuristic, heuristic, index))
                self.peak_heap = max(self.peak_heap, len(self.opened))
                self.portal_index += 1
            self.active = None


def _passed(position, previous, corner, tolerance):
    if distance(position, corner) <= tolerance:
        return True
    edge = np.asarray(difference(corner, previous))
    length = math.hypot(*edge)
    if length == 0.0:
        return True
    forward = edge / length
    beyond = np.asarray(difference(position, corner))
    along = float(beyond @ forward)
    cross_track = math.hypot(*(beyond - along * forward))
    return along >= 0.0 and cross_track <= tolerance


class NominalRouteAdviser:
    """Retain spatial corners until reached, passed or their connector is blocked.

    ``choose(position, target_goal, obstacles, source_time_s, config,
    primary_goal=...)`` follows a caller-owned bypass macro-goal. Without one,
    it follows the public goal. Only a <=200 m local prefix is planned. A cached
    path needs one current connector check, not a new search on every scan.

    Four expansions and 24 exact visibility queries bound each call; a search
    has at most 32 expansions, 64 nodes, 64 heap entries and ten path points.
    PlannerConfig can tighten every corresponding search budget. The default
    8 ms wall guard is checked between operations, not a hard real-time promise.
    ``work_budget_s=None`` keeps operation bounds without a wall-time guard.

    Geometry retains all supplied rows. At most 128 overlapping capsules may
    enter the local index; excess returns no advice, never a truncated map.
    Frozen incremental search grants no freshness: its complete local result
    must pass current supplied geometry before publication. Later cache reuse
    checks only the next connector. Farther cached edges can therefore be stale.
    Neither source timestamps nor command leases are advanced by this class.
    """

    def __init__(self, *, lookahead_m=200.0, sweep_horizon_s=1.5, work_budget_s=0.008):
        self.lookahead_m = scalar(lookahead_m, "lookahead_m", positive=True)
        self.sweep_horizon_s = scalar(sweep_horizon_s, "sweep_horizon_s")
        if self.lookahead_m > 200.0 or self.sweep_horizon_s > 3.0:
            raise ValueError("Nominal lookahead is limited to 200 metres and a 3 second sweep")
        self.work_budget_s = (
            None if work_budget_s is None else scalar(work_budget_s, "work_budget_s", positive=True)
        )
        self._latest_time_s = None
        self._context = None
        self.reset()

    def reset(self):
        """Discard spatial state, without resetting the source-time high-water mark."""
        self._path = ()
        self._cursor = 1
        self._planned_time_s = None
        self._search = None

    def choose(
        self,
        position,
        target_goal,
        obstacles: TemporalObstacles,
        source_time_s,
        config: PlannerConfig,
        *,
        primary_goal=None,
        context_key=None,
    ) -> NominalRouteAdvice:
        if not isinstance(config, PlannerConfig) or not isinstance(obstacles, TemporalObstacles):
            raise ValueError("Nominal advice requires PlannerConfig and TemporalObstacles")
        position = point(position, "position")
        target_goal = point(target_goal, "target_goal")
        macro_goal = target_goal if primary_goal is None else point(primary_goal, "primary_goal")
        now = scalar(source_time_s, "source_time_s")
        try:
            hash(context_key)
        except TypeError as error:
            raise ValueError("context_key must be immutable and hashable") from error
        work = _Work(
            None if self.work_budget_s is None else time.perf_counter() + self.work_budget_s
        )
        local_goal = self._path[-1] if self._path else None

        def result(status, detail, *, reused=False):
            search = self._search
            points = (position, *self._path[self._cursor :]) if status == "local_advice" else ()
            return NominalRouteAdvice(
                target_world_m=points[1] if len(points) > 1 else None,
                points_world_m=points,
                local_goal_world_m=local_goal,
                source_time_s=now,
                planned_time_s=self._planned_time_s if search is None else search.source_time_s,
                status=status,
                detail=detail,
                reused=reused,
                expanded_nodes=work.expanded,
                search_expanded_nodes=search.expanded if search is not None else 0,
                stored_nodes=len(search.nodes) if search is not None else 0,
                peak_heap_entries=search.peak_heap if search is not None else 0,
                collision_checks=work.checks,
                supplied_obstacles=len(obstacles.radii_m),
                waypoint_tolerance_m=min(4.0, 0.1 * config.cell_size_m),
                nominal_sweep_horizon_s=min(self.sweep_horizon_s, config.forecast_horizon_s),
            )

        if self._latest_time_s is not None and now < self._latest_time_s:
            return result("no_nominal_path", "Source time regressed; no clock reset or advice")
        self._latest_time_s = now
        context = (target_goal, macro_goal, config, context_key)
        if context != self._context:
            self.reset()
            self._context = context
            local_goal = None
        horizon = min(self.sweep_horizon_s, config.forecast_horizon_s)
        tolerance = min(4.0, 0.1 * config.cell_size_m)
        try:
            with np.errstate(over="raise", invalid="raise", divide="raise", under="ignore"):
                if self._path:
                    while self._cursor < len(self._path) and _passed(
                        position, self._path[self._cursor - 1], self._path[self._cursor], tolerance
                    ):
                        self._cursor += 1
                    if self._cursor < len(self._path):
                        geometry = _Geometry(
                            obstacles, horizon, _bounds((position, self._path[self._cursor]))
                        )
                        if geometry.clear(position, self._path[self._cursor], work):
                            return result(
                                "local_advice",
                                "Retained first unpassed nominal corner",
                                reused=True,
                            )
                    self.reset()
                    local_goal = None
                remaining = distance(position, macro_goal)
                if remaining <= tolerance:
                    return result(
                        "at_local_goal", "At supplied macro-goal; no native outcome inferred"
                    )
                if self._search is None:
                    local_goal = point(
                        np.asarray(position)
                        + np.asarray(difference(macro_goal, position))
                        * (min(self.lookahead_m, remaining) / remaining),
                        "near nominal goal",
                    )
                    corridor = _Corridor(position, local_goal, config, self.lookahead_m)
                    geometry = _Geometry(obstacles, horizon, corridor.bounds)
                    self._search = _Search(position, local_goal, geometry, corridor, config, now)
                search = self._search
                search.advance(work)
                local_goal = search.goal
                if search.failure is not None:
                    answer = result("no_nominal_path", search.failure)
                    self._search = None
                    return answer
                if not search.pending:
                    return result("planning", "Bounded nominal search continues on a later call")
                path = search.pending
                cursor = 1
                while cursor < len(path) - 1 and _passed(
                    position, path[cursor - 1], path[cursor], tolerance
                ):
                    cursor += 1
                current_path = (position, *path[cursor:])
                geometry = _Geometry(obstacles, horizon, _bounds(current_path))
                for left, right in zip(current_path[:-1], current_path[1:], strict=True):
                    if not geometry.clear(left, right, work):
                        answer = result(
                            "no_nominal_path",
                            "Frozen nominal candidate is blocked by current geometry",
                        )
                        self._search = None
                        return answer
                self._path = current_path
                self._cursor = 1
                self._planned_time_s = search.source_time_s
                answer = result(
                    "local_advice", "Local nominal polyline checked against supplied geometry"
                )
                self._search = None
                return answer
        except _BudgetReached:
            return result("planning", "Per-call nominal work budget reached; no unchecked target")
        except _GeometryBudget:
            answer = result(
                "no_nominal_path",
                "Nominal geometry budget exceeded; no supplied rows were discarded",
            )
            self._search = None
            return answer
        except (ValueError, OverflowError, FloatingPointError):
            self.reset()
            local_goal = None
            return result("no_nominal_path", "Nominal geometry is not representable; no target")
