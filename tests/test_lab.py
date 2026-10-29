"""Execute the real notebook's safe default graph, without a browser backend."""

from __future__ import annotations

import re
import runpy
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("marimo")
pytest.importorskip("altair")

LAB_PATH = Path(__file__).resolve().parents[1] / "notebooks" / "flight_lab.py"


@pytest.fixture
def lab_defs():
    namespace = runpy.run_path(str(LAB_PATH), run_name="wsorb_flight_lab_test")
    _outputs, definitions = namespace["app"].run()
    return definitions


@pytest.fixture
def lab_panels(lab_defs):
    tabs = lab_defs["lab_tabs"]
    return dict(zip(tabs._tab_keys, tabs._children, strict=True))


def _layout_items(item):
    """Walk genuine Marimo containers, without building a frontend substitute."""
    yield item
    children = getattr(item, "_live_children", getattr(item, "_children", ()))
    for child in children:
        yield from _layout_items(child)


def test_lab_default_graph_stays_disconnected_and_idle(lab_defs) -> None:
    required = {"planner_settings", "predict_scene", "find_path", "decide_command", "experiment"}
    assert required.issubset(lab_defs)
    assert all(
        callable(lab_defs[name]) for name in ("predict_scene", "find_path", "decide_command")
    )
    assert lab_defs["controller_mode"].value == "resilient_waypoint_pd"
    assert lab_defs["live_policy"].resilience is not None
    assert lab_defs["planner_settings"] == lab_defs["live_policy"].resilience.planner
    assert lab_defs["planner_settings"].forecast_horizon_s == 6.0
    assert lab_defs["duration_s"].value == lab_defs["CHECKPOINT_RUN_LIMIT_S"]
    assert lab_defs["duration_s"].stop == lab_defs["MAX_CONTROLLER_RUN_S"]
    assert lab_defs["cadence_hz"].value == 8
    assert lab_defs["get_session"]() is None
    assert lab_defs["get_placement_seed"]() is None
    assert lab_defs["get_reviewed_proposal"]() is None
    assert lab_defs["get_proposal_draft"]() is None
    assert not lab_defs["proposal_upload"].value
    assert lab_defs["flight_preset"].value == "far_side_l2"
    assert lab_defs["debris_mode"].value == "transit_stream"
    assert lab_defs["trajectory_adjustment"].value == "off"
    assert lab_defs["placement_controls"].value == {"x": None, "y": None, "z": None}
    assert lab_defs["place_lander"]._component_args["disabled"] is True
    assert all(
        control._component_args["disabled"]
        for control in lab_defs["ship_controls"].elements.values()
    )
    assert all(
        control._component_args["disabled"]
        for control in lab_defs["flow_controls"].elements.values()
    )
    assert not {
        "policy_armed",
        "reset_confirmed",
        "world_reset_confirmed",
        "world_settings_form",
    }.intersection(lab_defs)
    for name in (
        "connect_refresh",
        "run_controller",
        "run_policy_step",
        "stop_run",
        "upright",
        "reset_run",
        "regenerate_world",
        "apply_camera_aim",
        "capture_images",
        "prepare_export",
        "place_lander",
        "apply_sensor_fault",
        "clear_sensor_faults",
        "pause_physics",
        "review_proposal",
        "adopt_proposal",
        "discard_proposal",
        "interrupt_primary",
    ):
        assert lab_defs[name].value is False
    assert lab_defs["get_applied_config"]() is None
    assert lab_defs["get_camera_report"]() is None
    assert lab_defs["experiment"].history() == []
    assert lab_defs["experiment"].configuration_changes() == []
    assert lab_defs["experiment"]._worker is None
    assert lab_defs["flight_history_chart"] is None
    assert lab_defs["lab_source_path"] == LAB_PATH
    status = lab_defs["experiment"].status()
    assert (status["state"], status["sample_count"], status["elapsed_sim_s"]) == ("idle", 0, 0.0)
    assert status["latest"] is None
    assert status["error"] is None
    policy = lab_defs["live_policy"]
    assert policy.predict_scene is lab_defs["predict_scene"]
    assert policy.find_path is lab_defs["find_path"]
    assert policy.decide_command is lab_defs["decide_command"]


def test_lab_task_tabs_are_real_marimo_tabs_with_flight_selected(lab_defs, lab_panels):
    import marimo as mo

    tabs = lab_defs["lab_tabs"]
    assert isinstance(tabs, mo.ui.tabs)
    assert list(lab_panels) == [
        "Flight",
        "Resilience",
        "Ship",
        "Debris",
        "Launch",
        "Python policy",
        "History",
        "Cameras",
        "Export",
        "Details",
    ]
    assert tabs.value == lab_defs["get_selected_tab"]() == "Flight"
    assert tabs._component_args["orientation"] == "horizontal"
    assert tabs._args.label == "Flight lab tasks"
    assert tabs._on_change is lab_defs["set_selected_tab"]


def test_resilience_controls_are_explicit_and_disabled_without_a_connection(lab_defs, lab_panels):
    """Inspect the real default notebook graph, not a simulated sensor or browser."""
    assert lab_defs["sensor_fault_target"].value == "radar_primary"
    assert lab_defs["sensor_fault_mode"].value == "unavailable"
    assert lab_defs["sensor_fault_duration"].value == 5
    assert lab_defs["primary_outage_duration"].value == 5
    assert lab_defs["get_step_guidance"]() is None
    for name in (
        "sensor_fault_target",
        "sensor_fault_mode",
        "sensor_fault_duration",
        "apply_sensor_fault",
        "clear_sensor_faults",
        "pause_physics",
        "primary_outage_duration",
        "interrupt_primary",
    ):
        assert lab_defs[name]._component_args["disabled"] is True
    assert "do not fail a production cloud region" in lab_panels["Resilience"].text
    assert "Navigation telemetry is one unreplicated source" in lab_panels["Resilience"].text
    assert lab_defs["experiment"]._worker is None
    assert lab_defs["experiment"].history() == []


def test_each_existing_widget_appears_once_in_its_task_tab(lab_defs, lab_panels):
    """Rendered component inventory, not browser interaction or GPU evidence."""
    import marimo as mo

    inventory = {
        "Flight": (
            "connection_file",
            "connect_refresh",
            "run_controller",
            "stop_run",
            "pause_physics",
        ),
        "Resilience": (
            "primary_outage_duration",
            "interrupt_primary",
            "sensor_fault_target",
            "sensor_fault_mode",
            "sensor_fault_duration",
            "apply_sensor_fault",
            "clear_sensor_faults",
        ),
        "Ship": ("ship_controls",),
        "Debris": ("flow_controls", "debris_mode", "structure_controls", "impact_controls"),
        "Launch": (
            "world_seed",
            "reset_run",
            "flight_preset",
            "regenerate_world",
            "placement_controls",
            "place_lander",
            "upright",
            "proposal_upload",
            "review_proposal",
            "adopt_proposal",
            "discard_proposal",
        ),
        "Python policy": (
            "controller_mode",
            "planner_controls",
            "gain_controls",
            "duration_s",
            "cadence_hz",
            "step_seconds",
            "schedule_slot",
            "run_policy_step",
        ),
        "History": ("history_metric",),
        "Cameras": ("camera_controls", "apply_camera_aim", "capture_camera", "capture_images"),
        "Export": ("prepare_export",),
        "Details": ("experiment_refresh",),
    }
    for tab, names in inventory.items():
        for name in names:
            widget = lab_defs[name]
            children = (
                widget.elements.values() if isinstance(widget, mo.ui.dictionary) else (widget,)
            )
            for child in children:
                marker = f"object-id='{child._id}'"
                assert lab_panels[tab].text.count(marker) == 1, (tab, name)
                assert lab_defs["lab_tabs"].text.count(marker) == 1, (tab, name)


def test_native_tab_callbacks_change_only_the_selected_task(lab_defs, lab_panels):
    """Real widget callback/state contract only; this does not drive a browser."""
    import marimo as mo

    tabs = lab_defs["lab_tabs"]
    for index, name in enumerate(lab_panels):
        tabs._update(str(index))
        assert tabs.value == lab_defs["get_selected_tab"]() == name
        assert lab_defs["get_session"]() is None
        assert lab_defs["get_placement_seed"]() is None
        assert lab_defs["experiment"]._worker is None
        assert lab_defs["experiment"].history() == []
        assert lab_defs["experiment"].configuration_changes() == []
        assert all(
            not value.value for value in lab_defs.values() if isinstance(value, mo.ui.run_button)
        )


def test_new_world_and_placement_controls_stay_behind_closed_native_disclosures(
    lab_defs, lab_panels
):
    import marimo as mo

    for name in ("Ship", "Debris", "Launch", "Python policy", "History", "Cameras", "Export"):
        disclosures = [
            item for item in _layout_items(lab_panels[name]) if isinstance(item, mo.accordion)
        ]
        assert disclosures, name
        assert all(item._expanded == [] for item in disclosures), name

    for tab, name in (
        ("Debris", "debris_mode"),
        ("Debris", "structure_controls"),
        ("Debris", "impact_controls"),
        ("Launch", "flight_preset"),
        ("Launch", "regenerate_world"),
        ("Launch", "placement_controls"),
        ("Launch", "place_lander"),
        ("Launch", "upright"),
        ("Python policy", "run_policy_step"),
    ):
        widget = lab_defs[name]
        children = widget.elements.values() if isinstance(widget, mo.ui.dictionary) else (widget,)
        disclosures = [
            item for item in _layout_items(lab_panels[tab]) if isinstance(item, mo.accordion)
        ]
        assert all(
            any(f"object-id='{child._id}'" in item.text for item in disclosures)
            for child in children
        ), (tab, name)


def test_launch_copy_separates_native_pad_movement_from_advanced_lander_xyz(lab_defs, lab_panels):
    launch = lab_panels["Launch"].text
    assert "select Place lander, then drag the launchpad on Phobos." in launch
    assert "The button pauses flight and frames the pad." in launch
    assert "select Place lander, click Phobos, then Apply" in launch
    assert "Choose surface point" not in launch
    assert "Escape to cancel" in launch
    assert "N/S/E/W" in launch
    assert "Reset flight retains a custom launch site" in launch
    assert "Regenerate world clears it" in launch
    assert "They are not the only launch sites" in launch
    assert "Separate lander placement, advanced" in launch
    assert "Moves only the lander, not the launchpad or target" in launch
    assert lab_defs["place_lander"]._args.label == "Place lander only"
    assert lab_defs["flight_preset"]._args.label == "Target preset for new world"
    assert lab_defs["debris_mode"]._args.label == "Debris mode for new world"
    assert "New-world debris setup, draft only" in lab_panels["Debris"].text
    assert "with Regenerate world in Launch" in lab_panels["Debris"].text


def test_notebook_wrapping_rules_are_scoped_to_its_native_task_components(lab_defs, lab_panels):
    """Declared layout contract only. Reflow, focus and zoom still need a browser."""
    css = lab_defs["lab_styles"].text
    stylesheet = css.strip().removeprefix("<style>").removesuffix("</style>")
    rules = {
        selector.strip(): body for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", stylesheet)
    }
    assert rules
    assert all(selector.startswith(".wsorb-lab") for selector in rules)
    tablist = rules['.wsorb-lab marimo-tabs [role="tablist"]']
    assert "flex-wrap: wrap" in tablist
    assert "block-size: auto" in tablist
    assert "max-block-size: none" in tablist
    assert "overflow: visible" in tablist
    assert "white-space: nowrap" in rules['.wsorb-lab marimo-tabs [role="tab"]']
    assert "overflow-wrap: anywhere" in css
    assert "max-inline-size: 72ch" in css
    assert "line-height: 1.5" in css
    assert "font-size: 1rem" in css
    assert "font-variant-numeric: tabular-nums" in css
    assert "overflow: hidden" not in css
    assert not any(selector in css for selector in (".cm-", "canvas", "iframe", "img"))
    assert '<div class="wsorb-lab">' in lab_defs["lab_view"].text
    assert f"object-id='{lab_defs['lab_tabs']._id}'" in lab_defs["lab_view"].text
    flight = re.sub(r"\s+", "", lab_panels["Flight"].text)
    assert "flex-wrap:wrap" in flight
    assert "align-items:stretch" in flight
    assert "min-width:0" in flight
    assert "max-width:100%" in flight


def test_numeric_controls_keep_full_width_visible_labels_and_real_dictionary_lenses(lab_defs):
    for name in (
        "ship_controls",
        "flow_controls",
        "planner_controls",
        "gain_controls",
        "structure_controls",
        "impact_controls",
        "placement_controls",
        "camera_controls",
    ):
        controls = lab_defs[name]
        for child in controls.elements.values():
            assert child._args.label, name
            assert child._component_args["full-width"] is True, name
            assert child._lens.parent_id == controls._id, name
    for name in ("world_seed", "schedule_slot", "duration_s", "cadence_hz", "step_seconds"):
        assert lab_defs[name]._args.label, name
        assert lab_defs[name]._component_args["full-width"] is True, name


def test_lab_coast_policy_returns_zero_three_vectors(lab_defs) -> None:
    accel, angular_accel = lab_defs["decide_command"](None, None, "coast", {})
    assert isinstance(accel, np.ndarray)
    assert isinstance(angular_accel, np.ndarray)
    assert accel.shape == angular_accel.shape == (3,)
    np.testing.assert_array_equal(accel, np.zeros(3))
    np.testing.assert_array_equal(angular_accel, np.zeros(3))


def test_lab_uses_real_native_sliders_with_linked_numeric_inputs(lab_defs) -> None:
    import marimo as mo

    slider = lab_defs["lab_slider"](0, 10, 0.5, 2.5, "Numeric UI contract")
    assert isinstance(slider, mo.ui.slider)
    assert (slider.start, slider.stop, slider.step, slider.value) == (0, 10, 0.5, 2.5)
    assert "include-input" in slider.text
    assert "show-value" in slider.text
    assert isinstance(lab_defs["duration_s"], mo.ui.slider)
    assert lab_defs["world_settings_draft"] == lab_defs["default_world_settings"]
    count = lab_defs["structure_controls"]["count"]
    assert isinstance(count, mo.ui.number)
    assert (count.start, count.stop, count.value) == (0, 100_000, 500)


def test_live_tuning_merges_only_edits_made_after_connect_into_real_current_settings(lab_defs):
    """Pure numeric widget-merge contract, no network or simulation substitute."""
    merge = lab_defs["live_tuning_payload"]
    baseline = {"mass_kg": 3388.7, "inertia_scale": 1.0, "thrust_scale": 1.0}
    actual = {"mass_kg": 4000.0, "inertia_scale": 2.0, "thrust_scale": 3.0}
    assert merge(actual, baseline, baseline, tuple(actual)) is None
    assert merge(None, baseline, baseline, tuple(actual)) is None
    edited = {**baseline, "mass_kg": 5000.0}
    assert merge(actual, baseline, edited, tuple(actual)) == {
        "mass_kg": 5000.0,
        "inertia_scale": 2.0,
        "thrust_scale": 3.0,
    }
    assert actual == {"mass_kg": 4000.0, "inertia_scale": 2.0, "thrust_scale": 3.0}
    with pytest.raises(ValueError, match="not reported"):
        merge(None, baseline, edited, tuple(actual))


def test_live_controls_seed_from_confirmed_values_not_stale_requested_defaults(lab_defs):
    """UI-state contract only. No client, network or physics response is substituted."""
    values = lab_defs["confirmed_control_values"]
    defaults = lab_defs["default_world_settings"]
    ship = {"mass_kg": 14225.3, "inertia_scale": 1.5, "thrust_scale": 2.0}
    flow = {"speed_mps": 37.0, "speed_spread_mps": 4.0, "stream_axis": "z", "stream_sign": 1}
    assert values(
        {"actual_ship": ship, "actual_flow": flow, "ship_widgets": defaults["ship"]}, defaults
    ) == (ship, flow, False)
    assert values(None, defaults)[2] is True
    assert values({"ship_widgets": ship}, defaults)[2] is True


def test_impact_mode_disables_only_live_stream_controls_and_uses_versioned_initial_conditions(
    lab_defs,
):
    available = lab_defs["directed_flow_available"]
    assert available({"actual_setup": {"debris_distribution": {"kind": "directed_stream"}}})
    assert not available({"actual_setup": {"debris_distribution": {"kind": "impact_ejecta"}}})
    assert not available(None)
    source = lab_defs["workspace_scenario"]["impact_ejecta_defaults"]
    controls = lab_defs["impact_controls"].value
    assert [controls[f"source_{axis}"] for axis in "xyz"] == source["source_direction"]
    assert [controls[f"kick_{axis}"] for axis in "xyz"] == source["kick_velocity_mps"]
    assert controls["tangential_speed_mps"] == source["tangential_speed_mps"]
    assert controls["patch_half_angle_deg"] == source["patch_half_angle_deg"]


def test_placement_prefill_uses_only_reported_rapier_pose_not_shuttle_or_requested_xyz(lab_defs):
    seed = lab_defs["placement_seed"]
    assert seed({"observation": {"shuttle": {"position": [1, 2, 3]}}})["position_m"] is None
    data = {
        "observation": {
            "flight_preset": "mars_facing_l1",
            "trial_revision": 5,
            "shuttle": {"position": [1, 2, 3]},
            "applied_physics": {"position_world_m": [14_000.0, -20.0, 30.0], "source_time_s": 7.5},
        }
    }
    result = seed(data)
    assert result["position_m"] == [14_000.0, -20.0, 30.0]
    assert result["source_time_s"] == 7.5
    assert result["setup"]["trial_revision"] == 5


@pytest.mark.parametrize(
    ("field", "value"),
    [("mass_kg", 14225.3), ("inertia_scale", 1.8), ("thrust_scale", 3.2)],
)
def test_each_hardware_field_updates_the_real_marimo_dictionary_without_starting_flight(
    lab_defs,
    field,
    value,
):
    """Actual widget conversion only, not frontend/GPU or transport evidence."""
    controls = lab_defs["ship_controls"]
    before = dict(controls.value)
    child_id = controls[field]._id
    assert controls[field]._lens.parent_id == controls._id
    controls._update({field: value})
    assert controls.value == {**before, field: value}
    assert controls[field]._id == child_id
    assert controls[field].value == value
    assert lab_defs["get_session"]() is None
    assert lab_defs["experiment"]._worker is None


@pytest.mark.parametrize(
    ("field", "value"),
    [("speed_mps", 76.7), ("speed_spread_mps", 7.0), ("stream_axis", "z")],
)
def test_stream_fields_use_real_dictionary_lenses_and_leave_other_fields_unchanged(
    lab_defs,
    field,
    value,
):
    controls = lab_defs["flow_controls"]
    before = dict(controls.value)
    controls._update({field: [value] if field == "stream_axis" else value})
    assert controls.value == {**before, field: value}
    assert lab_defs["experiment"]._worker is None


def test_stream_sign_conversion_uses_the_real_dropdown_frontend_key(lab_defs):
    controls = lab_defs["flow_controls"]
    controls._update({"stream_sign": ["Negative direction"]})
    assert controls.value["stream_sign"] == -1
    assert controls.value["stream_axis"] == "x"


def test_confirmed_tuning_state_restores_actual_receipts_instead_of_echoing_requests(lab_defs):
    """Numeric UI receipt projection. No LocalSimulation response is replaced."""
    accept = lab_defs["confirmed_tuning_session"]
    ship = {"mass_kg": 100.0, "inertia_scale": 1.0, "thrust_scale": 3.2}
    flow = {"speed_mps": 76.7, "speed_spread_mps": 5.0, "stream_axis": "y", "stream_sign": -1}
    data = {
        "observation": {
            "time_s": 7.5,
            "tuning_revision": 4,
            "ship_tuning": ship,
            "debris_flow": flow,
        }
    }
    requested = {**ship, "mass_kg": 14225.3}
    restored = accept({}, data, family="ship", requested=requested, error="Readback differs")
    assert restored["tuning_state"] == "diverged"
    assert restored["actual_ship"] == restored["ship_widgets"] == ship
    assert restored["actual_flow"] == restored["flow_widgets"] == flow
    assert restored["tuning_source_time_s"] == 7.5
    assert restored["tuning_revision"] == 4
    assert restored["tuning_error"] == "Readback differs"
    confirmed = accept(restored, data, family="ship", requested=ship)
    assert confirmed["tuning_state"] == "confirmed"
    assert confirmed["tuning_error"] is None
    assert requested["mass_kg"] == 14225.3


def test_tuning_receipt_never_enables_missing_confirmed_hardware_or_flow(lab_defs):
    with pytest.raises(ValueError, match="not reported all"):
        lab_defs["confirmed_tuning_session"](
            {}, {"observation": {"ship_tuning": {"mass_kg": 100.0}}}, family="ship"
        )


def test_terminal_and_validation_feedback_remains_actionable_without_exposing_arbitrary_errors(
    lab_defs,
):
    text = lab_defs["action_error_text"]
    error = lab_defs["FlightStateError"]("Mission completed. Use Reset flight to fly again.")
    assert text("Run controller", error) == str(error)
    assert "speed_spread_mps must" in text(
        "Flow validation", ValueError("speed_spread_mps must be a finite number between 0 and 10")
    )
    assert "private callback text" not in text(
        "Unknown callback", ValueError("private callback text")
    )


@pytest.fixture
def numeric_lab_observation():
    """Constructed mathematical policy input, not a simulated sensor/physics response."""
    return {
        "time_s": 5.0,
        "seed": 10,
        "frame": "numeric-policy-world",
        "rotation_convention": "active_xyzw",
        "environment": {"mars_position_m": [0, 1e6, 0], "mars_radius_m": 1000.0},
        "geometry": {
            "phobos_enclosing_radius_m": 100.0,
            "arrival_radius_m": 50.0,
            "debris_count": 100,
        },
        "shuttle": {
            "position": [1000.0, 0.0, 0.0],
            "velocity": [0.01, 0.0, 0.0],
            "rotation": [0.0, 0.0, 0.0, 1.0],
            "radius_m": 4.0,
        },
        "target": {"position": [1120.0, 0.0, 0.0], "radius_m": 20.0},
        "radar": [[[0.0, 300.0, 0.0], [299.0, -1.0, 2.0], [5.0, 0.0, 0.0]]],
        "max_accel_mps2": 0.5,
        "max_angular_accel_rad_s2": 0.05,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("position_gain_s2", 0.0015),
        ("velocity_gain_s", 0.2),
        ("avoidance_range_m", 80.0),
        ("avoidance_accel_mps2", 0.9),
    ],
)
def test_controller_gain_edits_reach_the_actual_editable_notebook_function(
    lab_defs,
    numeric_lab_observation,
    field,
    value,
):
    observation = numeric_lab_observation
    observation["radar"] = [[[0.0, 60.0, 0.0], [60.0, -1.0, 2.0], [5.0, 0.0, 0.0]]]
    controls = lab_defs["gain_controls"]
    before = dict(controls.value)
    decide = lab_defs["decide_command"]
    first, _ = decide(observation, None, "basic_pd_radar", before)
    controls._update({field: value})
    second, angular = decide(observation, None, "basic_pd_radar", controls.value)
    assert not np.array_equal(first, second)
    np.testing.assert_array_equal(angular, np.zeros(3))
    assert lab_defs["experiment"]._worker is None


def test_waypoint_arrival_rule_and_mode_are_used_by_the_current_python_policy(
    lab_defs,
    numeric_lab_observation,
):
    settings = lab_defs["planner_settings"]
    frame = lab_defs["predict_scene"](numeric_lab_observation, settings)
    route = lab_defs["find_path"](frame, settings)
    assert route.status == "candidate"
    gains = dict(lab_defs["gain_controls"].value)
    moving, _ = lab_defs["decide_command"](
        numeric_lab_observation, route, "planned_waypoint_pd", gains
    )
    assert np.linalg.norm(moving) > 0
    gains["waypoint_arrival_m"] = 100.0
    arrived, _ = lab_defs["decide_command"](
        numeric_lab_observation, route, "planned_waypoint_pd", gains
    )
    np.testing.assert_array_equal(arrived, np.zeros(3))
    lab_defs["controller_mode"]._update(["Coast, zero command"])
    coast, _ = lab_defs["decide_command"](
        numeric_lab_observation, route, lab_defs["controller_mode"].value, gains
    )
    np.testing.assert_array_equal(coast, np.zeros(3))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cell_size_m", 50.0),
        ("clearance_m", 9.0),
        ("forecast_horizon_s", 4.1),
        ("max_measurement_age_s", 6.0),
        ("unknown_tangential_speed_mps", 13.0),
        ("sigma_multiplier", 4.0),
    ],
)
def test_each_planner_control_reaches_the_real_forecast_and_search_configuration(
    lab_defs,
    numeric_lab_observation,
    field,
    value,
):
    controls = lab_defs["planner_controls"]
    controls._update({field: value})
    settings = lab_defs["PlannerConfig"](**controls.value)
    assert getattr(settings, field) == value
    frame = lab_defs["predict_scene"](numeric_lab_observation, settings)
    route = lab_defs["find_path"](frame, settings)
    assert frame.config == settings
    assert route.frame.config == settings
    assert route.to_preview()["points_world_m"] == route.points_world_m.tolist()
    assert lab_defs["experiment"]._worker is None


def test_real_altair_history_plot_splits_missing_values_and_observed_segments(lab_defs) -> None:
    """Numeric plotting contract only, not flight measurements or a backend mock."""
    import marimo as mo

    rows = [
        {"segment": 1, "telemetry_time_s": 1, "speed_mps": 2.0},
        {"segment": 1, "telemetry_time_s": 2, "speed_mps": None},
        {"segment": 1, "telemetry_time_s": 3, "speed_mps": 4.0},
        {"segment": 2, "telemetry_time_s": 0, "speed_mps": 1.0},
    ]
    chart = lab_defs["history_plot"](rows, "speed_mps", "Numeric contract", "telemetry_time_s")
    spec = chart.to_dict()
    points = spec["data"].get("values")
    if points is None:
        points = spec["datasets"][spec["data"]["name"]]
    assert [point["speed_mps"] for point in points] == [2.0, 4.0, 1.0]
    assert len({point["plot_segment"] for point in points}) == 3
    assert spec["encoding"]["x"]["field"] == "telemetry_time_s"
    assert spec["encoding"]["detail"]["field"] == "plot_segment"
    assert spec["params"]
    widget = mo.ui.altair_chart(chart, chart_selection=False, legend_selection=False)
    assert isinstance(widget, mo.ui.altair_chart)


def test_history_plot_does_not_turn_held_tuning_rows_into_new_metric_readings(lab_defs):
    rows = [
        {"segment": 1, "telemetry_time_s": 2.0, "observation_time_s": 2.0, "speed_mps": 3.0},
        {
            "segment": 2,
            "telemetry_time_s": 2.0,
            "observation_time_s": 2.5,
            "speed_mps": 3.0,
            "telemetry_held": True,
            "tuning_changed": True,
        },
        {"segment": 2, "telemetry_time_s": 3.0, "observation_time_s": 3.0, "speed_mps": 3.1},
    ]
    chart = lab_defs["history_plot"](rows, "speed_mps", "Numeric contract", "telemetry_time_s")
    spec = chart.to_dict()
    points = spec["data"].get("values")
    if points is None:
        points = spec["datasets"][spec["data"]["name"]]
    assert [point["telemetry_time_s"] for point in points] == [2.0, 3.0]
    assert len({point["plot_segment"] for point in points}) == 2


def test_history_plot_separates_held_hot_timing_edits_without_new_metric_points(lab_defs):
    rows = [
        {"segment": 1, "telemetry_time_s": 2.0, "speed_mps": 3.0},
        {
            "segment": 2,
            "telemetry_time_s": 2.0,
            "speed_mps": 3.0,
            "telemetry_held": True,
            "configuration_changed": True,
        },
        {"segment": 2, "telemetry_time_s": 3.0, "speed_mps": 3.1},
    ]
    chart = lab_defs["history_plot"](rows, "speed_mps", "Numeric contract", "telemetry_time_s")
    spec = chart.to_dict()
    points = spec["data"].get("values")
    if points is None:
        points = spec["datasets"][spec["data"]["name"]]
    assert [point["telemetry_time_s"] for point in points] == [2.0, 3.0]


def test_application_plot_uses_last_actual_application_clock_and_skips_held_duplicates(lab_defs):
    rows = [
        {
            "segment": 1,
            "telemetry_time_s": 10.0,
            "actuator_application_time_s": 1.5,
            "actuator_application_tuning_revision": 2,
            "actuator_force_body_x_n": 2.0,
        },
        {
            "segment": 1,
            "telemetry_time_s": 11.0,
            "actuator_application_time_s": 1.5,
            "actuator_application_tuning_revision": 2,
            "actuator_force_body_x_n": 2.0,
        },
        {
            "segment": 2,
            "telemetry_time_s": 12.0,
            "actuator_application_time_s": 11.5,
            "actuator_application_tuning_revision": 3,
            "actuator_force_body_x_n": 0.2,
        },
    ]
    chart = lab_defs["history_plot"](
        rows,
        "actuator_force_body_x_n",
        "Numeric application contract",
        "actuator_application_time_s",
    )
    spec = chart.to_dict()
    points = spec["data"].get("values")
    if points is None:
        points = spec["datasets"][spec["data"]["name"]]
    assert [point["actuator_application_time_s"] for point in points] == [1.5, 11.5]
    assert [point["actuator_force_body_x_n"] for point in points] == [2.0, 0.2]
