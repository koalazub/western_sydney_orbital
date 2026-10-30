"""Constructed math only, not observations, root certificates or native flight proof.

No engine, source clocks, sensor delivery or actual clearance is fabricated here.
Even calculated advice with zero nominal cost grants no command eligibility.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from wsorb.flight import nominal_motion
from wsorb.flight.local_motion import stopping_motion
from wsorb.flight.nominal_motion import search_nominal_motion
from wsorb.flight.temporal import TemporalObstacles, motion_clear


def numeric_obstacles(centers=((1e6, 1e6, 0.0),), *, radii=None, growth=None, quadratic=None):
    centers = np.asarray(centers, dtype=float)
    rows = len(centers)
    return TemporalObstacles(
        centers,
        np.zeros((rows, 3)),
        np.ones(rows) if radii is None else np.asarray(radii, dtype=float),
        np.zeros(rows) if growth is None else np.asarray(growth, dtype=float),
        np.zeros(rows) if quadratic is None else np.asarray(quadratic, dtype=float),
    )


def numeric_search(controls, *, obstacles=None, forecasts=None, **changes):
    obstacles = numeric_obstacles() if obstacles is None else obstacles
    settings = {
        "position": (0.0, 0.0, 0.0),
        "velocity": (0.0, 0.0, 0.0),
        "controls": controls,
        "target": (100.0, 0.0, 0.0),
        "speed_cap": 8.0,
        "maximum": 0.5,
        "gain": 1.0,
        "lease": 0.75,
        "horizon": 40.0,
        "goal_tolerance": 4.0,
        "braking_accel_mps2": 0.49,
        "velocity_allowance_mps": 0.05,
        "command_padding_m": 0.43,
    }
    settings.update(changes)
    return search_nominal_motion(
        obstacles=obstacles,
        forecast_velocities=np.zeros_like(obstacles.centers_m) if forecasts is None else forecasts,
        **settings,
    )


def test_free_one_dimensional_advance_beats_wait_with_explicit_trapezoidal_cost():
    result = numeric_search([[0.5, 0.0, 0.0], [0.0, 0.0, 0.0]], horizon=0.75)
    wait = numeric_search([[0.0, 0.0, 0.0]], horizon=0.75)
    assert result.status == wait.status == "calculated"
    advice, waiting = result.advice, wait.advice
    assert advice is not None and waiting is not None
    assert advice.control_index == 0
    # p(0.75)=0.5*0.5*0.75**2=0.140625. Outside-tolerance errors are
    # 96 and 95.859375, not a claim that the trapezoid is an exact integral.
    assert advice.cost[1] == pytest.approx(0.5 * (96.0 + 95.859375) * 0.75)
    assert waiting.cost[1] == pytest.approx(72.0)
    assert advice.cost[1] < waiting.cost[1]
    assert advice.cost[2] == pytest.approx(95.859375)
    assert advice.cost[3] == pytest.approx(0.5)
    assert advice.cost[0] == waiting.cost[0] == 0.0
    assert not result.safety_clearance and not result.source_freshness
    assert not advice.safety_clearance and not advice.source_freshness


def test_root_pruning_sees_the_later_encounter_before_discarding_a_turn():
    result = numeric_search(
        ((0.5, 0.0, 0.0), (0.0, 0.5, 0.0)),
        velocity=(1.0, 0.0, 0.0),
        obstacles=numeric_obstacles(((10.0, 0.0, 0.0),), radii=(0.5,)),
        max_pair_intervals=1304,
    )
    assert result.status == "calculated" and result.advice is not None
    assert result.beam_width == 1
    # Both first leases miss the sphere. The forward root's coast reaches it;
    # the turning root's coast passes it at more than three metres sideways.
    # A first-lease-only pruning cost used to throw away the turn immediately.
    assert result.advice.control_index == 1
    np.testing.assert_array_equal(result.advice.reference_controls_mps2[0], (0.0, 0.5, 0.0))
    assert not result.safety_clearance and not result.source_freshness


def test_external_gate_keeps_an_unsafe_current_stop_out_of_every_future_beam():
    obstacles = numeric_obstacles(((5.0, 0.0, 0.0),), quadratic=(0.25,))
    padded = TemporalObstacles(
        obstacles.centers_m,
        obstacles.velocities_mps,
        obstacles.radii_m + 0.43,
        obstacles.growth_mps,
        obstacles.growth_mps2,
    )
    controls = np.array(((0.5, 0.0, 0.0), (-0.5, 0.0, 0.0)))
    checks = {}

    def current_stop_is_clear(index):
        segments = stopping_motion(
            np.zeros(3),
            np.array((1.0, 0.0, 0.0)),
            controls[index],
            0.75,
            0.49,
            0.05,
        )
        checks[index] = motion_clear(segments, padded)
        return checks[index]

    old_plan = numeric_search(((0.5, 0.0, 0.0),), velocity=(1.0, 0.0, 0.0)).advice
    assert old_plan is not None
    result = numeric_search(
        controls,
        velocity=(1.0, 0.0, 0.0),
        obstacles=obstacles,
        root_is_admissible=current_stop_is_clear,
        seed_plan=old_plan,
        seed_age_s=0.0,
        seed_control_index=0,
    )
    assert result.advice is not None
    assert checks == {0: False, 1: True}
    assert result.advice.control_index == 1
    assert not result.safety_clearance and not result.source_freshness


def test_seeded_continuation_starts_from_current_state_not_the_old_model_position():
    old = numeric_search(((0.5, 0.0, 0.0),), target=(10000.0, 0.0, 0.0)).advice
    assert old is not None
    current_position = np.array((10.0, 3.0, -2.0))
    current_velocity = np.array((0.125, 0.0, 0.0))
    result = numeric_search(
        ((0.5, 0.0, 0.0),),
        position=current_position,
        velocity=current_velocity,
        target=(10000.0, 0.0, 0.0),
        seed_plan=old,
        seed_age_s=0.25,
        seed_control_index=0,
    )
    assert result.advice is not None
    np.testing.assert_array_equal(result.advice.positions_m[0], current_position)
    np.testing.assert_array_equal(result.advice.velocities_mps[0], current_velocity)
    np.testing.assert_allclose(
        result.advice.first_lease_position_m,
        current_position + current_velocity * 0.75 + np.array((0.25 * 0.75**2, 0.0, 0.0)),
    )
    assert result.evaluated_pair_intervals <= 640000
    assert not result.safety_clearance and not result.source_freshness


def test_first_lease_and_every_future_turn_remain_continuous_and_acceleration_bounded():
    position = np.array((20.0, 3.0, -2.0))
    velocity = np.array((1.0, 0.0, 0.0))
    controls = np.array(((0.0, 0.25, 0.0),))
    before = position.copy(), velocity.copy(), controls.copy()
    result = numeric_search(
        controls,
        position=position,
        velocity=velocity,
        target=(20.0, 10000.0, -2.0),
        gain=2.0,
    )
    assert result.status == "calculated" and result.advice is not None
    advice = result.advice
    first_accel = controls[0] * 2.0
    expected_position = position + velocity * 0.75 + 0.5 * first_accel * 0.75**2
    expected_velocity = velocity + first_accel * 0.75
    assert advice.times_s[1] == 0.75
    np.testing.assert_allclose(advice.first_lease_position_m, expected_position)
    np.testing.assert_allclose(advice.first_lease_velocity_mps, expected_velocity)
    np.testing.assert_allclose(advice.positions_m[1], expected_position)
    np.testing.assert_allclose(advice.velocities_mps[1], expected_velocity)
    np.testing.assert_allclose(advice.times_s[1:], np.linspace(0.75, 40.0, 9))
    np.testing.assert_array_equal(advice.reference_controls_mps2[0], controls[0])
    assert result.completed_steps == 8 and advice.goal_entry_time_s is None
    assert advice.positions_m[-1, 1] > advice.positions_m[1, 1]
    assert np.all(np.hypot.reduce(advice.reference_controls_mps2, axis=1) <= 0.5)
    for i, duration in enumerate(np.diff(advice.times_s)):
        acceleration = advice.reference_controls_mps2[i] * 2.0
        np.testing.assert_allclose(
            advice.positions_m[i + 1],
            advice.positions_m[i]
            + advice.velocities_mps[i] * duration
            + 0.5 * acceleration * duration**2,
            atol=1e-10,
        )
        np.testing.assert_allclose(
            advice.velocities_mps[i + 1],
            advice.velocities_mps[i] + acceleration * duration,
            atol=1e-12,
        )
        assert (
            math.hypot(*advice.velocities_mps[i + 1])
            <= max(math.hypot(*advice.velocities_mps[i]), 8.0) + 1e-12
        )
    for original, unchanged in zip((position, velocity, controls), before, strict=True):
        np.testing.assert_array_equal(original, unchanged)


def test_future_actions_include_all_three_existing_lattice_magnitudes_and_actual_gain_feedback():
    lattice = nominal_motion._lattice_controls(0.5)
    assert lattice.shape == (78, 3)
    for block, magnitude in enumerate((0.5, 0.25, 0.125)):
        commands = lattice[26 * block : 26 * (block + 1)]
        np.testing.assert_allclose(np.hypot.reduce(commands, axis=1), magnitude, atol=1e-15)
        np.testing.assert_allclose(commands / magnitude, lattice[:26] / 0.5, atol=1e-15)
    node = nominal_motion._Node(
        root=0,
        root_norm=0.0,
        position=np.zeros(3),
        velocity=np.array((1.0, 0.0, 0.0)),
        elapsed=0.75,
        gap=1000.0,
        path_gap=1000.0,
        contingency_gap=1000.0,
        integral=0.0,
        terminal_error=4.0,
        entry=None,
        command=np.zeros(3),
        parent=None,
    )
    choices = nominal_motion._future_controls(
        [node], np.array((5.0, 0.0, 0.0)), 8.0, 0.5, 2.0, 4.0, 1.0, lattice
    )
    assert choices.shape == (81, 3)
    np.testing.assert_array_equal(choices[:78], lattice)
    np.testing.assert_array_equal(choices[78], np.zeros(3))
    # Actual acceleration ceiling is 0.5*2=1, remaining distance is 5-1=4.
    desired_speed = math.sqrt(2.0 * 1.0 * 4.0)
    np.testing.assert_allclose(choices[79], ((desired_speed - 1.0) / 8.0, 0.0, 0.0))
    np.testing.assert_allclose(choices[80], (-0.125, 0.0, 0.0))
    np.testing.assert_allclose(node.velocity + choices[79] * 2.0 * 4.0, (desired_speed, 0, 0))


def test_relative_chord_catches_a_moving_body_between_clear_piece_endpoints():
    obstacles = numeric_obstacles(((-1.0, 0.0, 0.0),), radii=(0.1,))
    result = numeric_search(
        [[0.0, 0.0, 0.0]],
        obstacles=obstacles,
        forecasts=((8.0 / 3.0, 0.0, 0.0),),
        horizon=0.75,
    )
    assert result.status == "calculated" and result.advice is not None
    assert math.dist((0, 0, 0), (-1, 0, 0)) > 0.1
    assert math.dist((0, 0, 0), (1, 0, 0)) > 0.1
    assert result.advice.minimum_path_gap_lower_bound_m < -0.1
    assert result.advice.cost[0] >= 0.1


def test_last_fast_row_is_not_dropped_or_replaced_by_obstacle_velocity_fields():
    centers = np.tile((1e6, 1e6, 0.0), (203, 1))
    centers[-1] = (-7.5, 0.0, 0.0)
    obstacles = numeric_obstacles(centers, radii=np.full(203, 0.25))
    forecasts = np.zeros((203, 3))
    forecasts[-1] = (20.0, 0.0, 0.0)
    result = numeric_search(
        [[0.0, 0.0, 0.0]], obstacles=obstacles, forecasts=forecasts, horizon=0.75
    )
    assert result.status == "calculated" and result.advice is not None
    assert result.supplied_obstacles == 203
    assert result.evaluated_path_pair_intervals == 203
    assert result.evaluated_contingency_pair_intervals == 203
    assert result.evaluated_pair_intervals == 406
    assert result.advice.minimum_path_gap_lower_bound_m < -0.25
    assert result.advice.cost[0] >= 0.25
    np.testing.assert_array_equal(obstacles.velocities_mps, np.zeros((203, 3)))


def test_whole_contingency_catches_twenty_metre_per_second_crossing_before_clear_stop_endpoint():
    obstacles = numeric_obstacles(((-25.0, 0.0, 0.0),), radii=(0.2,))
    result = numeric_search(
        [[0.0, 0.0, 0.0]],
        velocity=(1.0, 0.0, 0.0),
        obstacles=obstacles,
        forecasts=((20.0, 0.0, 0.0),),
        braking_accel_mps2=0.5,
        velocity_allowance_mps=0.0,
        command_padding_m=0.0,
        horizon=0.75,
    )
    assert result.status == "calculated" and result.advice is not None
    # At T=.75, p=.75, speed=1. Brake h=2, p_stop=1.75. The body moves
    # from -10 to 30 during that brake. Both relative endpoints miss the body.
    assert abs(0.75 - (-25.0 + 20.0 * 0.75)) > 0.2
    assert abs(1.75 - (-25.0 + 20.0 * 2.75)) > 0.2
    assert result.advice.minimum_path_gap_lower_bound_m > 0.0
    # The relative chord crosses zero. R=.2 and curvature=.5*2**2/8=.25.
    assert result.advice.minimum_contingency_gap_lower_bound_m == pytest.approx(-0.45, abs=1e-6)
    assert result.advice.cost[0] == pytest.approx(0.45, abs=1e-6)
    assert result.evaluated_pair_intervals == 2
    assert result.evaluated_contingency_pair_intervals == 1
    assert not result.safety_clearance and not result.source_freshness


def test_contingency_retains_R_g_q_command_padding_and_full_velocity_error_reserve():
    obstacles = numeric_obstacles(
        ((10.0, 0.0, 0.0),), radii=(1.0,), growth=(2.0,), quadratic=(0.5,)
    )
    result = numeric_search(
        [[0.0, 0.0, 0.0]],
        velocity=(1.0, 0.0, 0.0),
        obstacles=obstacles,
        horizon=0.75,
        braking_accel_mps2=0.5,
        velocity_allowance_mps=0.5,
        command_padding_m=0.25,
    )
    assert result.status == "calculated" and result.advice is not None
    # h=(1+.5)/.5=3, p_stop=.75+1/(2*.5)=1.75, chord distance=8.25.
    # R_end=1+2*3+.5*3**2+.25=11.75; curvature=.5*3**2/8=.5625.
    assert result.advice.minimum_contingency_gap_lower_bound_m == pytest.approx(
        8.25 - 11.75 - 0.5625, abs=1e-6
    )
    assert result.advice.minimum_gap_lower_bound_m == pytest.approx(-4.0625, abs=1e-6)
    assert result.advice.cost[0] == pytest.approx(4.0625, abs=1e-6)
    assert result.evaluated_pair_intervals == 2
    np.testing.assert_array_equal(obstacles.radii_m, (1.0,))
    np.testing.assert_array_equal(obstacles.growth_mps, (2.0,))
    np.testing.assert_array_equal(obstacles.growth_mps2, (0.5,))


def test_stationary_error_reserve_has_no_fictitious_braking_curvature():
    result = numeric_search(
        [[0.0, 0.0, 0.0]],
        obstacles=numeric_obstacles(((5.0, 0.0, 0.0),)),
        horizon=0.75,
        braking_accel_mps2=0.5,
        velocity_allowance_mps=0.5,
        command_padding_m=0.25,
    )
    assert result.status == "calculated" and result.advice is not None
    assert result.advice.minimum_contingency_gap_lower_bound_m == pytest.approx(3.75, abs=1e-6)
    assert result.evaluated_pair_intervals == 2


def test_stopping_room_deficit_precedes_goal_progress_without_an_extra_speed_policy():
    obstacles = numeric_obstacles(((5.0, 0.0, 0.0),), quadratic=(0.25,))
    result = numeric_search(
        [[0.5, 0.0, 0.0], [-0.5, 0.0, 0.0]],
        velocity=(1.0, 0.0, 0.0),
        obstacles=obstacles,
        horizon=0.75,
        braking_accel_mps2=0.5,
    )
    assert result.status == "calculated" and result.advice is not None
    # The faster node reaches x=.890625 at v=1.375, then its contingency ends
    # at x=2.78125 with h=2.85. Its retained growth/curvature causes a deficit.
    # The slower node reaches x=.609375 at v=.625 and has positive stop room.
    assert result.advice.control_index == 1
    assert result.advice.minimum_contingency_gap_lower_bound_m > 0.0
    assert result.advice.cost[0] == 0.0


def test_first_model_entry_has_no_post_goal_path_or_contingency_penalty():
    obstacles = numeric_obstacles(
        ((1.4, 0.0, 0.0),), radii=(0.05,), growth=(5.0,), quadratic=(2.0,)
    )
    result = numeric_search(
        [[0.0, 0.0, 0.0]],
        velocity=(2.0, 0.0, 0.0),
        target=(1.0, 0.0, 0.0),
        obstacles=obstacles,
        goal_tolerance=0.1,
    )
    assert result.status == "calculated" and result.advice is not None
    advice = result.advice
    assert advice.goal_entry_time_s == pytest.approx(0.45, abs=1e-8)
    assert advice.times_s[-1] == advice.goal_entry_time_s
    np.testing.assert_allclose(advice.positions_m[-1], (0.9, 0.0, 0.0), atol=1e-8)
    assert advice.cost[0] == advice.cost[2] == 0.0
    assert advice.minimum_contingency_gap_lower_bound_m is None
    assert result.completed_steps == 0 and result.evaluated_candidates == 1
    assert result.evaluated_path_pair_intervals == 1
    assert result.evaluated_contingency_pair_intervals == 0
    # The actual root lease is NOT shortened or certified by this model endpoint.
    np.testing.assert_allclose(advice.first_lease_position_m, (1.5, 0.0, 0.0))
    np.testing.assert_allclose(advice.first_lease_velocity_mps, (2.0, 0.0, 0.0))
    assert not advice.safety_clearance and not advice.source_freshness


def test_goal_entry_between_outside_quadratic_endpoints_is_not_missed():
    result = numeric_search(
        [[-0.5, 0.0, 0.0]],
        velocity=(4.0, 0.0, 0.0),
        target=(1.5, 0.0, 0.0),
        goal_tolerance=0.1,
        horizon=0.75,
    )
    assert result.status == "calculated" and result.advice is not None
    advice = result.advice
    assert math.dist((0.0, 0.0, 0.0), (1.5, 0.0, 0.0)) > 0.1
    assert math.dist(advice.first_lease_position_m, (1.5, 0.0, 0.0)) > 0.1
    assert 0.0 < advice.goal_entry_time_s < 0.75
    np.testing.assert_allclose(advice.positions_m[-1], (1.4, 0.0, 0.0), atol=1e-8)
    assert advice.cost[2] == 0.0


def test_receding_without_entry_keeps_the_full_prefix_not_its_initial_closest_point():
    result = numeric_search([[0.0, 0.0, 0.0]], velocity=(-1.0, 0.0, 0.0), horizon=0.75)
    assert result.status == "calculated" and result.advice is not None
    advice = result.advice
    assert advice.goal_entry_time_s is None
    assert advice.times_s[-1] == 0.75
    assert advice.cost[1] == pytest.approx(0.5 * (96.0 + 96.75) * 0.75)
    assert advice.cost[2] == pytest.approx(96.75)


def test_already_inside_the_model_goal_has_no_zero_duration_control_division():
    result = numeric_search([[0.0, 0.0, 0.0]], target=(0.0, 0.0, 0.0))
    assert result.status == "calculated" and result.advice is not None
    advice = result.advice
    assert advice.goal_entry_time_s == 0.0
    np.testing.assert_array_equal(advice.times_s, (0.0,))
    np.testing.assert_array_equal(advice.cost, np.zeros(4))
    assert advice.reference_controls_mps2.shape == (0, 3)
    assert result.evaluated_pair_intervals == 1 and result.completed_steps == 0
    assert not advice.safety_clearance and not advice.source_freshness


def test_root_pruning_and_all_eighty_one_future_choices_have_bounded_exact_pair_counts():
    result = numeric_search(np.zeros((20, 3)), target=(10000.0, 0.0, 0.0), horizon=8.75)
    assert result.status == "calculated" and result.advice is not None
    assert result.advice.control_index == 0
    assert result.beam_width == result.peak_beam_size == 8
    assert result.peak_candidates == 8 * 81
    assert result.completed_steps == 8
    assert result.evaluated_candidates == 40 + 8 * 81 * 8
    assert result.evaluated_path_pair_intervals == result.evaluated_candidates
    assert result.evaluated_contingency_pair_intervals == result.evaluated_candidates
    assert result.evaluated_pair_intervals == result.required_pair_intervals == 10448
    assert result.planned_pair_intervals == 10448
    assert result.elapsed_s >= 0.0


def test_full_root_previews_remain_inside_the_original_pair_budget():
    obstacles = numeric_obstacles(np.tile((1e6, 1e6, 0.0), (203, 1)))
    result = numeric_search(np.zeros((156, 3)), obstacles=obstacles, target=(10000.0, 0.0, 0.0))
    assert result.status == "calculated" and result.advice is not None
    assert result.beam_width == result.peak_beam_size == 1
    assert result.planned_pair_intervals == 4 * 156 * 203 + 2 * 8 * 81 * 203
    assert result.planned_pair_intervals == 389760
    assert result.evaluated_pair_intervals == result.required_pair_intervals == 389760
    assert result.evaluated_pair_intervals <= 640000
    assert result.peak_candidates == 156


@pytest.mark.parametrize("budget", [0, 1299])
def test_unfundable_minimum_width_returns_no_advice_not_partial_or_zero_costs(budget):
    result = numeric_search([[0.0, 0.0, 0.0]], max_pair_intervals=budget)
    assert result.status == "budget_exhausted" and result.advice is None
    assert result.beam_width == 0
    assert result.required_pair_intervals == result.planned_pair_intervals == 1300
    assert result.evaluated_pair_intervals == result.evaluated_candidates == 0
    assert not result.safety_clearance and not result.source_freshness


def test_dense_input_exhausts_the_budget_without_dropping_obstacle_rows():
    obstacles = numeric_obstacles(np.tile((1e6, 1e6, 0.0), (4099, 1)))
    result = numeric_search(np.zeros((156, 3)), obstacles=obstacles)
    assert result.status == "budget_exhausted" and result.advice is None
    assert result.supplied_obstacles == 4099 and result.root_count == 156
    assert result.required_pair_intervals > 640000
    assert result.evaluated_pair_intervals == 0


def test_numeric_failure_discards_advice_instead_of_returning_uncomputed_zero_costs():
    result = numeric_search(
        [[0.0, 0.0, 0.0]], position=(1e308, 0.0, 0.0), target=(-1e308, 0.0, 0.0)
    )
    assert result.status == "numeric_failure" and result.advice is None
    assert not result.safety_clearance and not result.source_freshness


def test_first_lease_overspeed_is_rejected_without_resetting_velocity():
    result = numeric_search([[0.5, 0.0, 0.0]], velocity=(8.0, 0.0, 0.0), horizon=0.75)
    assert result.status == "no_admissible_motion" and result.advice is None
    coast = numeric_search([[0.0, 0.0, 0.0]], velocity=(9.0, 0.0, 0.0), horizon=0.75)
    assert coast.status == "calculated" and coast.advice is not None
    np.testing.assert_array_equal(coast.advice.first_lease_velocity_mps, (9.0, 0.0, 0.0))


def test_selected_calculations_have_immutable_independent_arrays():
    result = numeric_search([[0.0, 0.0, 0.0]], horizon=0.75)
    assert result.advice is not None
    for name in (
        "cost",
        "times_s",
        "positions_m",
        "velocities_mps",
        "reference_controls_mps2",
        "first_lease_position_m",
        "first_lease_velocity_mps",
    ):
        array = getattr(result.advice, name)
        assert not array.flags.writeable
        with pytest.raises(ValueError):
            array.setflags(write=True)


@pytest.mark.parametrize(
    "change",
    [
        {"maximum": 0.51},
        {"speed_cap": 8.01},
        {"lease": 0.76},
        {"horizon": 40.01},
        {"braking_accel_mps2": 0.0},
        {"braking_accel_mps2": 0.51},
        {"velocity_allowance_mps": -0.01},
        {"command_padding_m": -0.01},
        {"max_pair_intervals": 640001},
        {"max_pair_intervals": True},
    ],
)
def test_invalid_numeric_contracts_are_rejected_without_claiming_any_source_eligibility(change):
    with pytest.raises(ValueError):
        numeric_search([[0.0, 0.0, 0.0]], **change)


def test_forecasts_must_cover_every_row_and_mixed_boolean_controls_are_not_numbers():
    with pytest.raises(ValueError, match="every obstacle row"):
        numeric_search([[0.0, 0.0, 0.0]], forecasts=np.zeros((0, 3)))
    with pytest.raises(ValueError, match="booleans"):
        numeric_search([[True, 0.0, 0.0]])
