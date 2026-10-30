"""Bounded kinodynamic beam advice, never executable-motion clearance.

The caller must clear every root that enters the beam against its actual fresh
whole lease, complete active stop and all original growing uncertainty envelopes,
either beforehand or through the external root checker. This search cannot
replace or relax that check. Its obstacle velocities
are explicit nominal world-frame hypotheses, not measured debris velocities.

The search keeps at most eight paths, not every reachable continuation. Root
pruning, the finite action lattice and conservative chord bounds make it incomplete.
Neither a returned path nor failure establishes feasibility or optimality.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import product
from typing import Literal

import numpy as np
import numpy.typing as npt

from ._validation import count, point, real_array, scalar
from .control import _bounded
from .temporal import TemporalObstacles

__all__ = ["NominalMotionAdvice", "NominalMotionResult", "search_nominal_motion"]

_MAX_ROOTS = 200
_BEAM_WIDTH = 8
_FUTURE_PIECES = 8
_FUTURE_CHOICES = 81
_MAX_PAIR_INTERVALS = 640_000
_PAIR_BATCH = 16_384
_GOAL_ROOT_STEPS = 32
_ABS_PADDING_M = 4e-8
_REL_PADDING = 1024.0 * float(np.finfo(np.float64).eps)
_KIND = "nominal_kinodynamic_beam_advice_only"
_DIRECTIONS = np.asarray(
    [
        np.asarray(value, dtype=float) / math.hypot(*value)
        for value in product((-1, 0, 1), repeat=3)
        if value != (0, 0, 0)
    ]
)

type FloatArray = npt.NDArray[np.float64]


def _readonly(value: FloatArray) -> FloatArray:
    return np.frombuffer(value.tobytes(), dtype=value.dtype).reshape(value.shape)


def _array(value, name: str) -> FloatArray:
    if not isinstance(value, np.ndarray):
        raw = np.asarray(value, dtype=object)
        if any(isinstance(item, (bool, np.bool_)) for item in raw.flat):
            raise ValueError(f"{name} must contain real numbers, not booleans")
    return real_array(value, name)


def _finite(*values) -> None:
    if not all(np.isfinite(value).all() for value in values):
        raise FloatingPointError("Unrepresentable nominal motion calculation")


def _dot(left: FloatArray, right: FloatArray) -> FloatArray:
    value = np.einsum("...i,...i->...", left, right)
    _finite(value)
    return value


@dataclass(frozen=True, slots=True, eq=False)
class NominalMotionAdvice:
    """One selected root and its calculated, read-only nominal path.

    ``cost`` is lexicographically minimized: combined nominal path/contingency
    collision penalty in m, trapezoidal running goal-distance error in m*s,
    terminal distance outside the goal tolerance in m, then the original root
    reference-command norm.
    Root input index and stable future-action order break exact ties.

    The scored path ends at first MODEL goal-region entry, if any. Completed
    paths retain that cost unchanged, with zero further goal error, while other
    beams advance. No post-entry collision or terminal cost is added. This is
    a one-time-entry objective, not a hypothetical stop or station-keeping claim.
    A receding path without entry is scored through the full horizon.

    ``reference_controls_mps2[i]`` applies continuously between path nodes i and
    i+1, multiplied by the supplied gain. First model entry may truncate even
    the scored first piece. The separate ``first_lease_*`` fields ALWAYS contain
    the endpoint of the exact, full actual first lease, without clipping speed
    or extending authority. The external root proof must cover that full lease
    and stop even when the advisory objective ends earlier.
    """

    control_index: int
    cost: FloatArray
    minimum_gap_lower_bound_m: float
    minimum_path_gap_lower_bound_m: float
    minimum_contingency_gap_lower_bound_m: float | None
    goal_entry_time_s: float | None
    times_s: FloatArray
    positions_m: FloatArray
    velocities_mps: FloatArray
    reference_controls_mps2: FloatArray
    first_lease_position_m: FloatArray
    first_lease_velocity_mps: FloatArray
    safety_clearance: bool = field(default=False, init=False)
    source_freshness: bool = field(default=False, init=False)
    kind: str = field(default=_KIND, init=False)

    def __post_init__(self) -> None:
        for name in (
            "cost",
            "times_s",
            "positions_m",
            "velocities_mps",
            "reference_controls_mps2",
            "first_lease_position_m",
            "first_lease_velocity_mps",
        ):
            object.__setattr__(self, name, _readonly(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class NominalMotionResult:
    advice: NominalMotionAdvice | None
    status: Literal["calculated", "budget_exhausted", "numeric_failure", "no_admissible_motion"]
    detail: str
    root_count: int
    supplied_obstacles: int
    required_pair_intervals: int
    evaluated_pair_intervals: int
    evaluated_path_pair_intervals: int
    evaluated_contingency_pair_intervals: int
    evaluated_candidates: int
    beam_width: int
    planned_pair_intervals: int
    completed_steps: int
    peak_beam_size: int
    peak_candidates: int
    elapsed_s: float
    safety_clearance: bool = field(default=False, init=False)
    source_freshness: bool = field(default=False, init=False)
    kind: str = field(default=_KIND, init=False)


class _BudgetReached(Exception):
    pass


@dataclass(slots=True)
class _Work:
    rows: int
    limit: int
    required: int = 0
    evaluated: int = 0
    path_pairs: int = 0
    contingency_pairs: int = 0
    candidates: int = 0
    beam_width: int = 0
    planned: int = 0
    steps: int = 0
    peak_beam: int = 0
    peak_candidates: int = 0

    def reserve(self, candidates: int, contingencies: int) -> None:
        self.required += (candidates + contingencies) * self.rows
        self.peak_candidates = max(self.peak_candidates, candidates)
        if self.required > self.limit:
            raise _BudgetReached


@dataclass(frozen=True, slots=True, eq=False)
class _Node:
    root: int
    root_norm: float
    position: FloatArray
    velocity: FloatArray
    elapsed: float
    gap: float
    path_gap: float
    contingency_gap: float
    integral: float
    terminal_error: float
    entry: float | None
    command: FloatArray
    parent: _Node | None

    def priority(self):
        return (
            max(0.0, -self.gap),
            self.integral,
            self.terminal_error,
            self.root_norm,
            self.root,
        )


@dataclass(frozen=True, slots=True)
class _Evaluation:
    positions: FloatArray
    velocities: FloatArray
    elapsed: FloatArray
    gaps: FloatArray
    path_gaps: FloatArray
    contingency_gaps: FloatArray
    integrals: FloatArray
    terminal_errors: FloatArray
    entered: npt.NDArray[np.bool_]


def _first_goal_entry(positions, velocities, accelerations, durations, target, tolerance):
    """First entry on each exact quadratic piece, not an endpoint-only test."""
    r = positions - target
    w = velocities * durations[:, None]
    z = accelerations * (0.5 * durations**2)[:, None]
    # Half the derivative of squared distance is A*u**3+B*u**2+C*u+D.
    a, b = 2.0 * _dot(z, z), 3.0 * _dot(w, z)
    c, d = _dot(w, w) + 2.0 * _dot(r, z), _dot(r, w)
    discriminant = b * b - 3.0 * a * c
    q = -b - np.copysign(np.sqrt(np.maximum(0.0, discriminant)), b)
    extrema = (a > 0) & (discriminant > 0)
    root1 = np.divide(q, 3.0 * a, out=np.zeros_like(a), where=extrema)
    root2 = np.divide(c, q, out=np.zeros_like(a), where=extrema & (q != 0))
    low = np.clip(np.minimum(root1, root2), 0.0, 1.0)
    high = np.clip(np.maximum(root1, root2), 0.0, 1.0)
    boundaries = np.stack((np.zeros_like(a), low, high, np.ones_like(a)), axis=1)
    left, right = boundaries[:, :-1].copy(), boundaries[:, 1:].copy()

    def derivative(u):
        return ((a[:, None] * u + b[:, None]) * u + c[:, None]) * u + d[:, None]

    left_value, right_value = derivative(left), derivative(right)
    bracket = (a > 0)[:, None] & ((left_value < 0) != (right_value < 0))
    if np.any(bracket):
        for _ in range(_GOAL_ROOT_STEPS):
            middle = left + 0.5 * (right - left)
            value = derivative(middle)
            same = (value < 0) == (left_value < 0)
            left = np.where(same, middle, left)
            left_value = np.where(same, value, left_value)
            right = np.where(same, right, middle)
            right_value = np.where(same, right_value, value)
    fraction = np.divide(
        -left_value,
        right_value - left_value,
        out=np.full_like(left_value, 0.5),
        where=right_value != left_value,
    )
    stationary = np.where(bracket, left + np.clip(fraction, 0.0, 1.0) * (right - left), 0.0)
    linear = np.clip(np.divide(-d, c, out=np.zeros_like(d), where=(a == 0) & (c > 0)), 0.0, 1.0)
    candidates = np.sort(np.column_stack((boundaries, stationary, linear)), axis=1)
    relative = r[:, None] + w[:, None] * candidates[:, :, None]
    relative += z[:, None] * (candidates**2)[:, :, None]
    distances = np.hypot.reduce(relative, axis=2)
    _finite(distances)
    inside = distances <= tolerance
    has_entry = np.any(inside, axis=1)
    first_inside = np.argmax(inside, axis=1)
    indices = np.arange(len(positions))
    left = candidates[indices, np.maximum(0, first_inside - 1)]
    right = candidates[indices, first_inside]
    if np.any(has_entry & (first_inside > 0)):
        for _ in range(_GOAL_ROOT_STEPS):
            middle = left + 0.5 * (right - left)
            relative = r + w * middle[:, None] + z * (middle**2)[:, None]
            outside = np.hypot.reduce(relative, axis=1) > tolerance
            left = np.where(outside, middle, left)
            right = np.where(outside, right, middle)
    return np.where(has_entry, right * durations, np.inf)


def _chord_gaps(
    positions,
    displacement,
    elapsed,
    durations,
    own_scale,
    curvature,
    obstacles,
    forecasts,
    work,
    *,
    contingency=False,
    command_padding=0.0,
):
    """One candidate/row pair encloses one whole nominal path or contingency."""
    _finite(own_scale, curvature, displacement, durations)
    end_time = float(np.max(elapsed + durations))
    gaps = np.full(len(positions), np.inf)
    batch_size = max(1, _PAIR_BATCH // len(positions))
    for first in range(0, work.rows, batch_size):
        last = min(work.rows, first + batch_size)
        centers, velocities = obstacles.centers_m[first:last], forecasts[first:last]
        radii = obstacles.radii_m[None, first:last] + command_padding
        if contingency:
            radii = radii + durations[:, None] * obstacles.growth_mps[first:last]
            radii += (durations**2)[:, None] * obstacles.growth_mps2[first:last]
        obstacle_scale = np.max(abs(centers), axis=1)
        obstacle_scale += np.max(abs(velocities), axis=1) * end_time
        padding = _ABS_PADDING_M + _REL_PADDING * (own_scale[:, None] + obstacle_scale + radii)
        relative = positions[:, None] - centers - elapsed[:, None, None] * velocities
        chord = displacement[:, None] - durations[:, None, None] * velocities
        squared = _dot(chord, chord)
        along = np.divide(
            -_dot(relative, chord), squared, out=np.zeros_like(squared), where=squared > 0
        )
        np.clip(along, 0.0, 1.0, out=along)
        distances = np.hypot.reduce(relative + along[:, :, None] * chord, axis=2)
        distances -= radii + padding + curvature[:, None]
        _finite(distances)
        gaps = np.minimum(gaps, np.min(distances, axis=1))
        pairs = len(positions) * (last - first)
        work.evaluated += pairs
        if contingency:
            work.contingency_pairs += pairs
        else:
            work.path_pairs += pairs
    _finite(gaps)
    return gaps


def _gap_bounds(positions, ends, accelerations, elapsed, durations, obstacles, forecasts, work):
    displacement = ends - positions
    own_scale = np.max(abs(positions), axis=1) + np.max(abs(displacement), axis=1)
    # |v|*dt <= |displacement| + |a|*dt**2/2, including cancellation.
    own_scale += np.max(abs(accelerations), axis=1) * durations**2
    curvature = np.hypot.reduce(accelerations, axis=1) * durations**2 / 8.0
    return _chord_gaps(
        positions,
        displacement,
        elapsed,
        durations,
        own_scale,
        curvature,
        obstacles,
        forecasts,
        work,
    )


def _contingency_gaps(
    positions, velocities, elapsed, obstacles, forecasts, braking, allowance, padding, work
):
    """Whole brake then stationary error-reserve enclosure, not endpoint sampling."""
    speeds = np.hypot.reduce(velocities, axis=1)
    durations = (speeds + allowance) / braking
    displacement = velocities * (speeds / (2.0 * braking))[:, None]
    own_scale = np.max(abs(positions), axis=1)
    own_scale += np.max(abs(velocities), axis=1) * durations
    own_scale += 0.5 * braking * durations**2
    # The C1 brake/reserve path has globally bounded |p''| <= braking.
    curvature = np.where(speeds > 0, braking * durations**2 / 8.0, 0.0)
    return _chord_gaps(
        positions,
        displacement,
        elapsed,
        durations,
        own_scale,
        curvature,
        obstacles,
        forecasts,
        work,
        contingency=True,
        command_padding=padding,
    )


def _evaluate(
    positions,
    velocities,
    commands,
    elapsed,
    duration,
    gain,
    target,
    tolerance,
    obstacles,
    forecasts,
    braking,
    allowance,
    padding,
    work,
):
    durations = np.full(len(commands), duration)
    accelerations = commands * gain
    entries = _first_goal_entry(positions, velocities, accelerations, durations, target, tolerance)
    entered = np.isfinite(entries)
    unfinished = ~entered
    work.reserve(len(commands), int(np.count_nonzero(unfinished)))
    durations = np.where(entered, entries, durations)
    ends = positions + velocities * durations[:, None]
    ends += accelerations * (0.5 * durations**2)[:, None]
    end_velocities = velocities + accelerations * durations[:, None]
    start_errors = np.maximum(0.0, np.hypot.reduce(positions - target, axis=1) - tolerance)
    end_errors = np.maximum(0.0, np.hypot.reduce(ends - target, axis=1) - tolerance)
    end_errors = np.where(entered, 0.0, end_errors)
    # Explicit trapezoidal quadrature of outside-tolerance distance, in m*s.
    integrals = 0.5 * (start_errors + end_errors) * durations
    _finite(ends, end_velocities, integrals, end_errors)
    path_gaps = _gap_bounds(
        positions, ends, accelerations, elapsed, durations, obstacles, forecasts, work
    )
    # Entry ends the objective. Do not invent a post-entry brake or its penalty.
    contingency_gaps = np.full(len(commands), np.inf)
    if np.any(unfinished):
        contingency_gaps[unfinished] = _contingency_gaps(
            ends[unfinished],
            end_velocities[unfinished],
            (elapsed + durations)[unfinished],
            obstacles,
            forecasts,
            braking,
            allowance,
            padding,
            work,
        )
    gaps = np.minimum(path_gaps, contingency_gaps)
    work.candidates += len(commands)
    return _Evaluation(
        ends,
        end_velocities,
        elapsed + durations,
        gaps,
        path_gaps,
        contingency_gaps,
        integrals,
        end_errors,
        entered,
    )


def _select(completed, parents, roots, norms, commands, evaluation, eligible, width):
    pool = list(completed)
    for i in np.flatnonzero(eligible):
        parent = parents[i]
        path_gap = float(evaluation.path_gaps[i])
        contingency_gap = float(evaluation.contingency_gaps[i])
        integral = float(evaluation.integrals[i])
        if parent is not None:
            path_gap = min(path_gap, parent.path_gap)
            contingency_gap = min(contingency_gap, parent.contingency_gap)
            integral += parent.integral
        gap = min(path_gap, contingency_gap)
        if not math.isfinite(integral):
            raise FloatingPointError("Unrepresentable accumulated goal-distance integral")
        pool.append(
            _Node(
                int(roots[i]),
                float(norms[i]),
                evaluation.positions[i].copy(),
                evaluation.velocities[i].copy(),
                float(evaluation.elapsed[i]),
                gap,
                path_gap,
                contingency_gap,
                integral,
                float(evaluation.terminal_errors[i]),
                float(evaluation.elapsed[i]) if evaluation.entered[i] else None,
                commands[i].copy(),
                parent,
            )
        )
    # Python's stable sort preserves parent/action input order after exact ties.
    pool.sort(key=_Node.priority)
    return pool[:width]


def _lattice_controls(maximum):
    return np.asarray(
        [
            _bounded(direction * maximum * scale, maximum)
            for scale in (1.0, 0.5, 0.25)
            for direction in _DIRECTIONS
        ]
    )


def _future_controls(nodes, target, speed, maximum, gain, duration, tolerance, lattice):
    response = gain * duration
    if not math.isfinite(response) or response <= 0:
        raise FloatingPointError("Future control interval is not representable")
    choices = np.empty((len(nodes), _FUTURE_CHOICES, 3))
    choices[:, :78] = lattice
    choices[:, 78] = 0.0
    actual_accel = maximum * gain
    for i, node in enumerate(nodes):
        delta = target - node.position
        length = math.hypot(*delta)
        remaining = max(0.0, length - tolerance)
        desired_speed = min(speed, math.sqrt(2.0 * actual_accel * remaining))
        desired = delta * (desired_speed / length) if length > 0 else np.zeros(3)
        choices[i, 79] = _bounded((desired - node.velocity) / response, maximum)
        choices[i, 80] = _bounded(-node.velocity / response, maximum)
    return choices.reshape(-1, 3)


def _advice(node, position, velocity, first_positions, first_velocities):
    reverse = []
    cursor = node
    while cursor is not None:
        reverse.append(cursor)
        cursor = cursor.parent
    times, positions, velocities, commands = [0.0], [position], [velocity], []
    for current in reversed(reverse):
        if current.elapsed > times[-1]:
            times.append(current.elapsed)
            positions.append(current.position)
            velocities.append(current.velocity)
            commands.append(current.command)
    return NominalMotionAdvice(
        node.root,
        np.asarray(node.priority()[:4]),
        node.gap,
        node.path_gap,
        node.contingency_gap if math.isfinite(node.contingency_gap) else None,
        node.entry,
        np.asarray(times),
        np.asarray(positions),
        np.asarray(velocities),
        np.asarray(commands, dtype=float).reshape(-1, 3),
        first_positions[node.root],
        first_velocities[node.root],
    )


def search_nominal_motion(
    position,
    velocity,
    controls,
    obstacles: TemporalObstacles,
    forecast_velocities,
    target,
    speed_cap,
    maximum,
    gain,
    lease,
    horizon=40.0,
    goal_tolerance=4.0,
    max_pair_intervals=_MAX_PAIR_INTERVALS,
    *,
    braking_accel_mps2,
    velocity_allowance_mps,
    command_padding_m,
    root_is_admissible: Callable[[int], bool] | None = None,
    seed_plan: NominalMotionAdvice | None = None,
    seed_age_s: float = 0.0,
    seed_control_index: int | None = None,
) -> NominalMotionResult:
    """Choose among 1..200 bounded roots using externally checked beam advice.

    The first reference command is applied to the actual supplied p/v for the
    exact supplied lease. Its full endpoint is retained even if the scored goal
    prefix ends earlier. Eight future intervals equally divide horizon-lease.
    Each active beam branches into 26 normalized {-1,0,1} lattice controls at
    1, 1/2 and 1/4 maximum, zero, bounded goal-velocity feedback and bounded braking.
    Feedback uses min(cap, sqrt(2*maximum*gain*outside_goal_distance)) and reference
    acceleration bound((desired_velocity-velocity)/(gain*dt), maximum).

    No velocity is reset or projected onto the speed sphere. Each modeled piece
    must end at speed <=max(its starting speed, cap). The actual first lease uses
    its FULL endpoint for this check, even if its scored prefix reaches the goal.
    Convexity of the norm bounds every intermediate constant-acceleration speed.

    All costs cover the same modeled prefix. Only first model goal entry ends a
    path early, never closest approach or a receding endpoint. Goal entry uses
    quadratic stationary points and bracketed bisection, with normalized-time
    isolation to 2**-32 plus floating arithmetic error. Distance integration is
    explicitly one trapezoid per scored piece, not an exact integral or bound.

    Every candidate piece is checked against EVERY row with its unchanged current
    radius. Its relative chord minimum minus ||a||*dt**2/8 and arithmetic padding
    bounds its continuous nominal path gap. Every unfinished endpoint also gets
    one whole nominal contingency bound. At absolute future time T, speed s,
    supplied braking b and velocity allowance e, h=(s+e)/b and
    p_stop=p+v*s/(2*b). The relative chord runs from p-(c+u*T) to
    p_stop-(c+u*(T+h)). Subtract R+g*h+q*h**2+command_padding_m and b*h**2/8
    when s>0, otherwise zero curvature. This encloses the C1 brake-then-stationary
    reserve, not just its endpoints. All original R,g,q remain. T advances only
    the explicit nominal world-center hypothesis; no future observation, source
    clock or actual uncertainty proof is manufactured or reset.

    The worst path OR contingency gap supplies the first cost column. Contingency
    costs accumulated at pre-entry nodes remain part of that prefix's cost; no
    additional contingency is evaluated at or after first model entry. These
    conservative nominal bounds need not be physical collisions. They never
    accept a root or replace the caller's complete growing-radius stop proof.

    Before pruning, every root gets a complete bounded coast preview through the
    remaining horizon. Pruning on its first lease alone would discard a turn
    before the later encounter that makes that turn necessary. Preview costs
    choose beam seeds only; they are not added to the returned path's cost.
    With C roots, N rows and eight future pieces, beam width is
    min(8, floor((budget-4*C*N)/(2*8*81*N))). Width below one returns no advice.
    An optional seed reserves another 2*8*N pairs inside that same budget.
    A horizon equal to the lease needs only the 2*C*N root allowance. No row is
    dropped. At most width*81 future candidates exist; row work is batched into
    <=16,384 candidate/row pairs. Counts include candidates rejected for speed.
    Completed goal paths need no contingency pair or future expansion. The
    planned pair count conservatively reserves both pairs for every candidate;
    evaluated/required counts omit goal-terminal contingencies. On an unfundable
    search, required reports the minimum whole plan needed. Any budget/numeric
    failure discards ALL advice, not partial costs.
    Invalid inputs raise ValueError. There is no wall-time or optimality promise.

    An external root checker may be supplied instead of prechecking every root.
    It is called in full-preview order before a root enters the beam. Only roots
    it accepts can be returned. This avoids spending the current lease proving
    lower-ranked roots that cannot enter the bounded beam. The caller retains
    responsibility for the complete current motion/stop and native dispatch.

    A previous calculated plan may seed one complete continuation. Only its
    reference velocities are reused: its path is resimulated from the CURRENT
    supplied position/velocity against CURRENT obstacle hypotheses and margins.
    Its first control still passes the external current-stop checker. An old
    plan supplies neither observations, freshness, clearance nor authority.
    """
    started = time.perf_counter()
    position, velocity, target = (
        np.asarray(point(_array(value, name), name))
        for value, name in ((position, "position"), (velocity, "velocity"), (target, "target"))
    )
    controls = _array(controls, "controls")
    if controls.ndim != 2 or controls.shape[1:] != (3,) or not 1 <= len(controls) <= _MAX_ROOTS:
        raise ValueError("controls must have shape (C, 3), 1 <= C <= 200")
    if not isinstance(obstacles, TemporalObstacles):
        raise ValueError("obstacles must be TemporalObstacles")
    forecasts = _array(forecast_velocities, "forecast_velocities")
    if forecasts.shape != obstacles.centers_m.shape:
        raise ValueError("forecast_velocities must include every obstacle row, with shape (N, 3)")
    speed = scalar(speed_cap, "speed_cap")
    maximum = scalar(maximum, "maximum", positive=True)
    gain = scalar(gain, "gain", positive=True)
    lease = scalar(lease, "lease", positive=True)
    horizon = scalar(horizon, "horizon", positive=True)
    tolerance = scalar(goal_tolerance, "goal_tolerance")
    braking = scalar(braking_accel_mps2, "braking_accel_mps2", positive=True)
    allowance = scalar(velocity_allowance_mps, "velocity_allowance_mps")
    padding = scalar(command_padding_m, "command_padding_m")
    if speed > 8.0 or maximum > 0.5 or lease > 0.75 or not lease <= horizon <= 40.0:
        raise ValueError("Nominal motion exceeds speed, acceleration, lease or horizon limits")
    norms = np.asarray([math.hypot(*command) for command in controls])
    if np.any(norms > maximum):
        raise ValueError("Every root must already satisfy the reference acceleration bound")
    if braking > maximum * gain:
        raise ValueError("Braking exceeds the supplied actual acceleration authority")
    limit = count(max_pair_intervals, "max_pair_intervals", maximum=_MAX_PAIR_INTERVALS)
    work = _Work(len(obstacles.radii_m), limit)
    if seed_plan is not None:
        if not isinstance(seed_plan, NominalMotionAdvice):
            raise ValueError("seed_plan must be calculated nominal motion advice")
        seed_age_s = scalar(seed_age_s, "seed_age_s")
        seed_control_index = count(
            seed_control_index, "seed_control_index", maximum=len(controls) - 1
        )
        if (
            seed_plan.times_s.ndim != 1
            or len(seed_plan.times_s) < 2
            or seed_plan.velocities_mps.shape != (len(seed_plan.times_s), 3)
            or not np.isfinite(seed_plan.times_s).all()
            or not np.isfinite(seed_plan.velocities_mps).all()
            or np.any(np.diff(seed_plan.times_s) <= 0.0)
        ):
            raise ValueError("Seed plan must contain finite ordered model states")

    def result(advice, status, detail):
        return NominalMotionResult(
            advice,
            status,
            detail,
            len(controls),
            work.rows,
            work.required,
            work.evaluated,
            work.path_pairs,
            work.contingency_pairs,
            work.candidates,
            work.beam_width,
            work.planned,
            work.steps,
            work.peak_beam,
            work.peak_candidates,
            time.perf_counter() - started,
        )

    root_pairs = (4 if horizon > lease else 2) * len(controls) * work.rows
    if seed_plan is not None and horizon > lease:
        root_pairs += 2 * _FUTURE_PIECES * work.rows
    if horizon > lease:
        width_pairs = 2 * _FUTURE_PIECES * _FUTURE_CHOICES * work.rows
        work.beam_width = min(_BEAM_WIDTH, max(0, (limit - root_pairs) // width_pairs))
        work.planned = root_pairs + max(1, work.beam_width) * width_pairs
    else:
        work.beam_width = _BEAM_WIDTH if root_pairs <= limit else 0
        work.planned = root_pairs
    if work.beam_width < 1:
        work.required = work.planned
        return result(None, "budget_exhausted", "Cannot fund the whole minimum-width beam search")

    try:
        with np.errstate(over="raise", invalid="raise", divide="raise", under="ignore"):
            actual_accel = maximum * gain
            if not math.isfinite(actual_accel) or actual_accel <= 0:
                raise FloatingPointError("Actual acceleration is not representable")
            first_positions = position + velocity * lease + controls * (0.5 * gain * lease**2)
            first_velocities = velocity + controls * (gain * lease)
            _finite(first_positions, first_velocities)
            root_evaluation = _evaluate(
                np.broadcast_to(position, controls.shape),
                np.broadcast_to(velocity, controls.shape),
                controls,
                np.zeros(len(controls)),
                lease,
                gain,
                target,
                tolerance,
                obstacles,
                forecasts,
                braking,
                allowance,
                padding,
                work,
            )
            speed_limit = max(math.hypot(*velocity), speed)
            eligible = np.hypot.reduce(first_velocities, axis=1) <= speed_limit
            roots = _select(
                [],
                [None] * len(controls),
                np.arange(len(controls)),
                norms,
                controls,
                root_evaluation,
                eligible,
                len(controls),
            )
            if horizon > lease:
                unfinished = [node for node in roots if node.entry is None]
                preview_keys = {}
                if unfinished:
                    preview = _evaluate(
                        np.asarray([node.position for node in unfinished]),
                        np.asarray([node.velocity for node in unfinished]),
                        np.zeros((len(unfinished), 3)),
                        np.asarray([node.elapsed for node in unfinished]),
                        horizon - lease,
                        gain,
                        target,
                        tolerance,
                        obstacles,
                        forecasts,
                        braking,
                        allowance,
                        padding,
                        work,
                    )
                    for i, node in enumerate(unfinished):
                        preview_keys[node.root] = (
                            max(0.0, -min(node.gap, float(preview.gaps[i]))),
                            node.integral + float(preview.integrals[i]),
                            float(preview.terminal_errors[i]),
                            node.root_norm,
                            node.root,
                        )
                roots.sort(key=lambda node: preview_keys.get(node.root, node.priority()))
            nodes = []
            for node in roots:
                if root_is_admissible is None or root_is_admissible(node.root):
                    nodes.append(node)
                    if len(nodes) >= work.beam_width:
                        break
            seeded = None
            if seed_plan is not None:
                seeded = next((node for node in roots if node.root == seed_control_index), None)
                if seeded is not None and root_is_admissible is not None:
                    if not root_is_admissible(seeded.root):
                        seeded = None
            work.peak_beam = len(nodes)
            if not nodes:
                return result(
                    None, "no_admissible_motion", "No root satisfies the model speed bound"
                )
            if horizon > lease:
                schedule = np.linspace(lease, horizon, _FUTURE_PIECES + 1)
                durations = np.diff(schedule)
                if np.any(durations <= 0):
                    raise FloatingPointError("Eight future intervals are not representable")
                lattice = _lattice_controls(maximum)
                if seeded is not None:
                    for duration in durations:
                        if seeded.entry is not None:
                            break
                        query = seed_age_s + seeded.elapsed + float(duration)
                        if query <= seed_plan.times_s[-1]:
                            desired = np.asarray(
                                [
                                    np.interp(
                                        query, seed_plan.times_s, seed_plan.velocities_mps[:, axis]
                                    )
                                    for axis in range(3)
                                ]
                            )
                            command = _bounded(
                                (desired - seeded.velocity) / (gain * float(duration)),
                                maximum,
                            )
                        else:
                            command = _future_controls(
                                [seeded],
                                target,
                                speed,
                                maximum,
                                gain,
                                float(duration),
                                tolerance,
                                lattice,
                            )[79]
                        evaluation = _evaluate(
                            seeded.position[None, :],
                            seeded.velocity[None, :],
                            command[None, :],
                            np.asarray([seeded.elapsed]),
                            float(duration),
                            gain,
                            target,
                            tolerance,
                            obstacles,
                            forecasts,
                            braking,
                            allowance,
                            padding,
                            work,
                        )
                        if math.hypot(*evaluation.velocities[0]) > max(
                            math.hypot(*seeded.velocity),
                            speed,
                        ):
                            seeded = None
                            break
                        seeded = _select(
                            [],
                            [seeded],
                            np.asarray([seeded.root]),
                            np.asarray([seeded.root_norm]),
                            command[None, :],
                            evaluation,
                            np.asarray([True]),
                            1,
                        )[0]
                for duration in durations:
                    active = [node for node in nodes if node.entry is None]
                    if not active:
                        break
                    commands = _future_controls(
                        active, target, speed, maximum, gain, float(duration), tolerance, lattice
                    )
                    parents = [node for node in active for _ in range(_FUTURE_CHOICES)]
                    positions = np.asarray([node.position for node in parents])
                    velocities = np.asarray([node.velocity for node in parents])
                    elapsed = np.asarray([node.elapsed for node in parents])
                    evaluation = _evaluate(
                        positions,
                        velocities,
                        commands,
                        elapsed,
                        float(duration),
                        gain,
                        target,
                        tolerance,
                        obstacles,
                        forecasts,
                        braking,
                        allowance,
                        padding,
                        work,
                    )
                    eligible = np.hypot.reduce(evaluation.velocities, axis=1) <= np.maximum(
                        np.hypot.reduce(velocities, axis=1), speed
                    )
                    nodes = _select(
                        [node for node in nodes if node.entry is not None],
                        parents,
                        np.asarray([node.root for node in parents]),
                        np.asarray([node.root_norm for node in parents]),
                        commands,
                        evaluation,
                        eligible,
                        work.beam_width,
                    )
                    work.steps += 1
                    work.peak_beam = max(work.peak_beam, len(nodes))
                    if not nodes:
                        return result(
                            None, "no_admissible_motion", "No modeled continuation retained"
                        )
            best = min([*nodes, seeded] if seeded is not None else nodes, key=_Node.priority)
            advice = _advice(best, position, velocity, first_positions, first_velocities)
            return result(
                advice, "calculated", "Bounded nominal beam advice; no clearance or freshness"
            )
    except _BudgetReached:
        return result(None, "budget_exhausted", "Whole candidate/row batch exceeds the pair budget")
    except (ArithmeticError, ValueError):
        return result(None, "numeric_failure", "Unrepresentable nominal model; no partial advice")
