"""One bounded 26-neighbour A* computation over frozen obstacle envelopes.

Distance costs and a Euclidean heuristic guide the search in a start-to-goal
corridor. Exact-goal visibility connectors and checked greedy smoothing make
this a geometric candidate, not a shortest-path or flight-safety guarantee.
There is no scheduler, background job, physics update or command output here.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import product

import numpy as np
import numpy.typing as npt

from ._validation import Vec3, count, difference, distance, point, real_array, scalar
from .config import PlannerConfig
from .geometry import Capsule, CollisionIndex, segment_clear
from .prediction import PlanningFrame

MAX_PREVIEW_POINTS = 4096
MAX_DETAIL_CHARS = 240
_STATUSES = frozenset(
    {"candidate", "no_candidate", "planning", "waiting_for_radar", "invalid_input", "at_waypoint"}
)
type Key = tuple[int, int, int]
_NEIGHBOURS = tuple(key for key in product((-1, 0, 1), repeat=3) if key != (0, 0, 0))


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Pure geometric search result, with storage counters for research checks."""

    points_world_m: tuple[Vec3, ...]
    detail: str
    expanded_nodes: int = 0
    stored_nodes: int = 0
    peak_heap_entries: int = 0


@dataclass(frozen=True, slots=True, eq=False)
class RoutePlan:
    frame: PlanningFrame
    points_world_m: npt.NDArray[np.float64]
    status: str
    detail: str
    expanded_nodes: int = 0

    def __post_init__(self) -> None:
        points = real_array(self.points_world_m, "route points_world_m")
        if points.shape == (0,):
            points = points.reshape(0, 3)
        if points.ndim != 2 or points.shape[1] != 3:
            raise ValueError("route points_world_m must have shape (N, 3)")
        points.setflags(write=False)
        object.__setattr__(self, "points_world_m", points)

    def to_preview(self) -> dict:
        """Return only the external renderer schema, or reject malformed data.

        A long path is never truncated into an unchecked shortcut. The planner
        returns no_candidate if checked smoothing cannot fit the point limit.
        These immutable numeric points are not actuator commands.
        """
        frame = self.frame
        if not isinstance(self.status, str) or self.status not in _STATUSES:
            raise ValueError("Unknown preview status")
        if not isinstance(self.detail, str) or not 1 <= len(self.detail) <= MAX_DETAIL_CHARS:
            raise ValueError(f"Preview detail must contain 1 to {MAX_DETAIL_CHARS} characters")
        if len(self.points_world_m) > MAX_PREVIEW_POINTS:
            raise ValueError(
                f"Preview exceeds {MAX_PREVIEW_POINTS} points; no points were truncated"
            )
        points = [list(point(value, "preview point")) for value in self.points_world_m]
        center = point(frame.goal_center_m, "goal_center_m")
        radius = scalar(frame.goal_radius_m, "goal_radius_m", positive=True)
        if self.status == "candidate":
            if len(points) < 2 or distance(tuple(points[-1]), center) > radius:
                raise ValueError("Candidate requires a complete path ending inside the goal region")
        elif points:
            raise ValueError("Only candidate status may contain route points")
        now = scalar(frame.source_time_s, "source_time_s")
        oldest, newest = frame.oldest_measurement_time_s, frame.newest_measurement_time_s
        if (oldest is None) != (newest is None):
            raise ValueError("Both measurement timestamps must be present or null")
        if oldest is not None:
            oldest = scalar(oldest, "oldest_measurement_time_s")
            newest = scalar(newest, "newest_measurement_time_s")
            if not oldest <= newest <= now:
                raise ValueError("Preview measurement timestamps must share source_time_s's origin")
        return {
            "source_seed": count(frame.source_seed, "source_seed", maximum=0xFFFFFFFF),
            "source_time_s": now,
            "status": self.status,
            "detail": self.detail,
            "points_world_m": points,
            "goal_center_m": list(center),
            "goal_radius_m": radius,
            "measured_obstacles": count(frame.measured_obstacles, "measured_obstacles"),
            "unmodelled_obstacles_lower_bound": count(
                frame.unmodelled_obstacles_lower_bound, "unmodelled_obstacles_lower_bound"
            ),
            "forecast_horizon_s": scalar(frame.forecast_horizon_s, "forecast_horizon_s"),
            "max_measurement_age_s": scalar(frame.max_measurement_age_s, "max_measurement_age_s"),
            "oldest_measurement_time_s": oldest,
            "newest_measurement_time_s": newest,
            "assumed_tangential_speed_allowance_mps": scalar(
                frame.assumed_tangential_speed_allowance_mps,
                "assumed_tangential_speed_allowance_mps",
            ),
            "expanded_nodes": count(self.expanded_nodes, "expanded_nodes"),
        }


@dataclass(frozen=True, slots=True)
class _Corridor:
    start: Vec3
    forward: Vec3
    across: Vec3
    up: Vec3
    cell_size_m: float
    along_max: int
    lateral_max: int

    @classmethod
    def create(cls, start: Vec3, goal: Vec3, config: PlannerConfig) -> _Corridor:
        length = distance(start, goal)
        along_cells = length / config.cell_size_m
        lateral_cells = config.corridor_half_width_m / config.cell_size_m
        if not all(
            math.isfinite(value) and value <= 2**31 - 4 for value in (along_cells, lateral_cells)
        ):
            raise ValueError("Corridor cell indices exceed the supported integer range")
        forward = (
            np.array(difference(goal, start)) / length if length else np.array([1.0, 0.0, 0.0])
        )
        reference = np.eye(3)[int(np.argmin(abs(forward)))]
        across = np.cross(forward, reference)
        across /= math.hypot(*across)
        up = np.cross(forward, across)
        corridor = cls(
            start,
            point(forward, "forward"),
            point(across, "across"),
            point(up, "up"),
            config.cell_size_m,
            math.floor(along_cells) + 2,
            math.floor(lateral_cells),
        )
        for key in product(
            (-2, corridor.along_max),
            (-corridor.lateral_max, corridor.lateral_max),
            (-corridor.lateral_max, corridor.lateral_max),
        ):
            corner = corridor.point(key)
            distance(corner, start)
            distance(corner, goal)
        return corridor

    def contains(self, key: Key) -> bool:
        return (
            -2 <= key[0] <= self.along_max
            and abs(key[1]) <= self.lateral_max
            and abs(key[2]) <= self.lateral_max
        )

    def point(self, key: Key) -> Vec3:
        if key == (0, 0, 0):
            return self.start
        coordinates = tuple(
            origin + self.cell_size_m * (a * key[0] + b * key[1] + c * key[2])
            for origin, a, b, c in zip(self.start, self.forward, self.across, self.up, strict=True)
        )
        return point(coordinates, "corridor point")


@dataclass(slots=True)
class _Node:
    point: Vec3
    cost: float
    parent: Key | None


def _smooth_checked(raw: tuple[Vec3, ...], collisions: CollisionIndex) -> tuple[Vec3, ...]:
    """Linear greedy pulling. Every kept edge, including the last, is checked."""
    if len(raw) < 2:
        return raw
    result = [raw[0]]
    previous = raw[0]
    for candidate in raw[1:]:
        if not collisions.clear(result[-1], candidate):
            if previous == result[-1] or not collisions.clear(previous, candidate):
                raise ValueError("Path reconstruction failed a segment-clearance check")
            result.append(previous)
        previous = candidate
    if result[-1] != previous:
        result.append(previous)
    if any(
        not collisions.clear(left, right)
        for left, right in zip(result[:-1], result[1:], strict=True)
    ):
        raise ValueError("Smoothed path failed final clearance validation")
    return tuple(result)


def search_route(
    start_world_m: Vec3,
    goal_world_m: Vec3,
    obstacles: Iterable[Capsule],
    config: PlannerConfig,
) -> SearchResult:
    """Search geometry once, bounded by nodes, expansions and heap entries.

    The sparse lattice allocates no whole-corridor grid. heapq duplicates are
    counted against their own explicit storage limit. Work per expansion has at
    most 26 neighbours plus an exact-goal visibility test; obstacle work still
    depends on input size. A visible goal can finish before a globally optimal
    route is known. Budget failure never returns a partial route.
    """
    if not isinstance(config, PlannerConfig):
        raise ValueError("config must be a PlannerConfig")
    start, goal = point(start_world_m, "start_world_m"), point(goal_world_m, "goal_world_m")
    corridor = _Corridor.create(start, goal, config)
    collisions = CollisionIndex(obstacles)
    if not collisions.clear(start, start):
        return SearchResult((), "Route start is blocked by an inflated obstacle")
    if not collisions.clear(goal, goal):
        return SearchResult((), "Route goal is blocked by an inflated obstacle")
    if collisions.clear(start, goal):
        return SearchResult(
            (start,) if start == goal else (start, goal),
            "Direct segment is clear of supplied capsules",
        )

    origin = (0, 0, 0)
    nodes = {origin: _Node(start, 0.0, None)}
    heuristic = distance(start, goal)
    opened = [(heuristic, heuristic, origin, 0.0)]
    expanded, peak_heap = 0, 1

    def result(detail: str, points: tuple[Vec3, ...] = ()) -> SearchResult:
        return SearchResult(points, detail, expanded, len(nodes), peak_heap)

    while opened:
        if expanded >= config.max_expansions:
            return result("Route search reached its expansion limit")
        _, _, key, queued_cost = heapq.heappop(opened)
        current = nodes[key]
        if queued_cost != current.cost:
            continue
        expanded += 1
        if collisions.clear(current.point, goal):
            reverse = [goal]
            cursor = key
            for _ in range(len(nodes)):
                node = nodes[cursor]
                if node.point != reverse[-1]:
                    reverse.append(node.point)
                if node.parent is None:
                    if cursor != origin:
                        raise ValueError("Route parent chain does not reach the start")
                    raw = tuple(reversed(reverse))
                    if any(
                        not collisions.clear(left, right)
                        for left, right in zip(raw[:-1], raw[1:], strict=True)
                    ):
                        raise ValueError("Reconstructed route failed clearance validation")
                    return result(
                        "Candidate checked against supplied capsules",
                        _smooth_checked(raw, collisions),
                    )
                cursor = node.parent
            raise ValueError("Route parent chain exceeded the stored-node bound")

        for offset in _NEIGHBOURS:
            neighbour = tuple(a + b for a, b in zip(key, offset, strict=True))
            if not corridor.contains(neighbour):
                continue
            known = nodes.get(neighbour)
            candidate = known.point if known else corridor.point(neighbour)
            edge_cost = distance(current.point, candidate)
            tentative = current.cost + edge_cost
            if edge_cost <= 0.0 or not math.isfinite(tentative) or tentative <= current.cost:
                raise ValueError("Search edge length or cost is not representable")
            if known is not None and tentative >= known.cost:
                continue
            if not collisions.clear(current.point, candidate):
                continue
            if known is None and len(nodes) >= config.max_nodes:
                return result("Route search reached its stored-node limit")
            if len(opened) >= config.max_heap_entries:
                return result("Route search reached its heap-entry storage limit")
            heuristic = distance(candidate, goal)
            if not math.isfinite(tentative + heuristic):
                raise ValueError("Search priority is not representable")
            nodes[neighbour] = _Node(candidate, tentative, key)
            heapq.heappush(opened, (tentative + heuristic, heuristic, neighbour, tentative))
            peak_heap = max(peak_heap, len(opened))
    return result("No clear route was found within the bounded corridor")


def plan_route(frame: PlanningFrame, config: PlannerConfig) -> RoutePlan:
    """Create an advisory preview, never commands or cached/autopilot state."""
    if not isinstance(frame, PlanningFrame) or not isinstance(config, PlannerConfig):
        raise ValueError("plan_route requires a PlanningFrame and PlannerConfig")
    if frame.config != config:
        raise ValueError("Config changed after prediction; call predict_obstacles again")
    if (
        not frame.obstacles
        or not frame.departure_world_m
        or frame.departure_world_m[-1] != frame.start_world_m
    ):
        raise ValueError("Planning frame lacks known geometry or a complete departure")
    if frame.at_waypoint:
        if not segment_clear(frame.start_world_m, frame.start_world_m, frame.obstacles):
            return RoutePlan(
                frame,
                (),
                "no_candidate",
                "Inside the waypoint region, but an inflated obstacle blocks the observer",
            )
        return RoutePlan(
            frame,
            (),
            "at_waypoint",
            "Inside the waypoint arrival region; no flight command is issued",
        )

    # Validate the special prefix again. It may omit only the Phobos enclosure.
    if len(frame.departure_world_m) > 2:
        raise ValueError("Departure prefix must contain at most two points")
    if len(frame.departure_world_m) == 2:
        first, last = frame.departure_world_m
        first_radius, last_radius = math.hypot(*first), math.hypot(*last)
        if first_radius == 0.0 or last_radius <= first_radius:
            raise ValueError("Departure prefix must point radially outward")
        if not np.allclose(
            np.array(first) / first_radius, np.array(last) / last_radius, rtol=0.0, atol=1e-12
        ):
            raise ValueError("Departure prefix is not radial")
        if not frame.has_surface_departure or not segment_clear(first, last, frame.obstacles[1:]):
            raise ValueError("Departure prefix failed clearance validation")
    result = search_route(frame.start_world_m, frame.goal_world_m, frame.obstacles, config)
    if not result.points_world_m:
        return RoutePlan(frame, (), "no_candidate", result.detail, result.expanded_nodes)
    points = frame.departure_world_m[:-1] + result.points_world_m
    if len(points) > MAX_PREVIEW_POINTS:
        return RoutePlan(
            frame,
            (),
            "no_candidate",
            "Checked route exceeds the 4096-point preview limit; no path was truncated",
            result.expanded_nodes,
        )
    if distance(points[-1], frame.goal_center_m) > frame.goal_radius_m:
        raise ValueError("Completed route does not reach the waypoint region")
    detail = (
        "Geometric radar-only candidate; unobserved debris and flight dynamics remain unchecked"
    )
    if frame.has_surface_departure:
        detail += ". Outward prefix omits the Phobos enclosure, not verified terrain"
    return RoutePlan(frame, points, "candidate", detail, result.expanded_nodes)
