import marimo

__generated_with = "0.25.2"
app = marimo.App(width="medium")


@app.cell
def _():
    import inspect
    import json
    import math
    from contextlib import suppress
    from dataclasses import asdict
    from pathlib import Path

    import altair as alt
    import marimo as mo
    import numpy as np

    import wsorb.flight as flight_modules
    from wsorb.experiment_io import (
        experiment_document,
        history_csv,
        json_bytes,
        source_manifest,
    )
    from wsorb.local_api import LocalApiError, LocalSimulation

    lab_source_path = Path(__file__).resolve()
    return (
        LocalApiError,
        LocalSimulation,
        Path,
        alt,
        asdict,
        experiment_document,
        flight_modules,
        history_csv,
        inspect,
        json,
        json_bytes,
        lab_source_path,
        math,
        mo,
        np,
        source_manifest,
        suppress,
    )


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    # Flight lab

    Edit prediction, A*, waypoint selection and controllers in Python. Bevy/Rapier
    runs the real local physics. Start the private lab/game processes and keep the
    game visible in a WebGPU browser. This notebook starts neither process.

    Opening the notebook, changing widgets and refreshing charts never starts
    flight. Inspect a preview, then arm **Apply and run** or a paused single step.
    World/ship settings require a separate confirmed reset. Export measurements
    and saved Python after experimenting.

    `SetCommand`, `GetNearestDebrisLocations` and `PanTiltCamera` share the official
    helper contracts. Local pause, stepping, reset, preview and capture transport
    are not SDK methods. Keep frame, quaternion and clock metadata when adapting
    a policy to licensed STP. Local results are not official scores or physics parity.
    """)
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    Keep automatic cell execution enabled. Marimo resets run buttons after their
    action finishes; lazy mode does not. Use one flight-command writer.
    """).callout(kind="warn")
    return


@app.cell
def _(json, mo):
    def lab_slider(start, stop, step, value, label):
        return mo.ui.slider(
            start=start,
            stop=stop,
            step=step,
            value=value,
            label=label,
            show_value=True,
            include_input=True,
            debounce=True,
            full_width=True,
        )

    def field_table(values, *, label):
        if values is None:
            return mo.md(f"{label} is unavailable in this snapshot.").callout(kind="warn")
        return mo.ui.table(
            [{"field": key, "value": json.dumps(value)} for key, value in values.items()],
            label=label,
            selection=None,
            pagination=False,
            show_column_summaries=False,
        )

    def show_snapshot(snapshot):
        observation = snapshot["observation"]
        telemetry = snapshot["telemetry"]
        ship = observation.get("shuttle") or {}
        radar_rows = [
            {
                "row": index,
                "relative_position_m": json.dumps(matrix[0]),
                "range_m": matrix[1][0],
                "range_rate_mps": matrix[1][1],
                "diameter_m": matrix[1][2],
                "measurement_time_s": matrix[2][0],
            }
            for index, matrix in enumerate(observation["radar"])
        ]
        radar_view = (
            mo.ui.table(radar_rows, label="Public radar", selection=None, page_size=10)
            if radar_rows
            else mo.md("No radar rows returned. Unobserved space is not clear space.").callout(
                kind="warn"
            )
        )
        return mo.vstack(
            [
                field_table(
                    {
                        "observation source time, s": observation.get("time_s"),
                        "telemetry source time, s": telemetry.get("source_time_s"),
                        "world/ship reset settings": observation.get("experiment_settings"),
                        "actual ship mass, kg": ship.get("mass_kg"),
                        "actual ship inertia, kg m²": ship.get("inertia_kg_m2"),
                        "reference linear cap, m/s²": observation.get("max_accel_mps2"),
                        "reference angular cap, rad/s²": observation.get(
                            "max_angular_accel_rad_s2"
                        ),
                    },
                    label="APPLIED world/ship and read-only limits, not widget drafts",
                ),
                field_table(telemetry.get("metrics"), label="Measured flight metrics"),
                mo.accordion(
                    {
                        "Public observation metadata and sensor model": mo.json(
                            {
                                key: value
                                for key, value in observation.items()
                                if key
                                not in {
                                    "shuttle",
                                    "target",
                                    "mission",
                                    "radar",
                                    "experiment_settings",
                                }
                            }
                        ),
                        "Own shuttle, target and mission": mo.json(
                            {key: observation.get(key) for key in ("shuttle", "target", "mission")}
                        ),
                        "Public radar, partial and range-sorted": mo.vstack(
                            [
                                mo.md(
                                    "World axes, not body axes. Rows have no persistent IDs. "
                                    "Range rate is not full velocity. Keep measurement times "
                                    "and `radar_clock`."
                                ),
                                radar_view,
                                mo.json(observation["radar"], label="Raw public 3×3 radar rows"),
                            ]
                        ),
                        "Telemetry epoch, units and caveats": mo.json(telemetry),
                        "Last Python advisory, check its source time": mo.json(
                            snapshot["navigation"]
                        ),
                        "Actual runtime and loaded engine build": mo.json(
                            {key: snapshot[key] for key in ("runtime", "engine_build")}
                        ),
                    },
                    multiple=True,
                ),
            ]
        )

    def show_plan(plan, observation, frame):
        points = [
            {"waypoint": index, "x_m": point[0], "y_m": point[1], "z_m": point[2]}
            for index, point in enumerate(plan.points_world_m)
        ]
        return mo.vstack(
            [
                field_table(
                    {
                        "source_time_s": observation["time_s"],
                        "status": plan.status,
                        "detail": plan.detail,
                        "measured_obstacles": frame.measured_obstacles,
                        "unmodelled_obstacles_lower_bound": frame.unmodelled_obstacles_lower_bound,
                        "forecast_horizon_s": frame.forecast_horizon_s,
                        "maximum_observed_age_s": frame.max_measurement_age_s,
                    },
                    label="Python result and public coverage, not a collision guarantee",
                ),
                mo.ui.table(
                    points, label="Computed route in observation world axes", selection=None
                )
                if points
                else mo.md("This plan returned no route points."),
                mo.accordion(
                    {
                        "Full prediction/search report and uncertainty assumptions": mo.json(
                            plan.to_preview()
                        )
                    }
                ),
            ]
        )

    return field_table, lab_slider, show_plan, show_snapshot


@app.cell
def _(mo):
    connection_file = mo.ui.text(
        value=".devenv/wsorb-local.json",
        label="Private local connection file, relative to the repository root",
        full_width=True,
    )
    connect_refresh = mo.ui.run_button(label="Connect / Refresh")
    mo.vstack(
        [
            mo.md("## Inspect the local game"),
            connection_file,
            connect_refresh,
            mo.md("Only the file path is shown. Keep the connection file and its token private."),
        ]
    )
    return connect_refresh, connection_file


@app.cell
def _(
    LocalApiError, LocalSimulation, connect_refresh, connection_file, experiment, mo, show_snapshot
):
    mo.stop(
        not connect_refresh.value,
        mo.md("Press Connect / Refresh to read the local game's status and public state."),
    )
    _views = []
    try:
        _sim = LocalSimulation.from_file(connection_file.value)
        _views.append(mo.md(f"[Open the actual local game in a separate window]({_sim.url})"))
        _views.append(mo.json(_sim.status(), label="Local bridge status"))
        _snapshot = _sim.snapshot()
        if experiment.status()["state"] not in {"starting", "running", "stopping"}:
            experiment.record_snapshot(_snapshot, source="inspect")
        _views.append(show_snapshot(_snapshot))
    except (LocalApiError, ValueError) as _error:
        _views.append(
            mo.md(f"""
            Cannot read the local game. {_error}

            Keep the local server running and open the game link in a
            WebGPU-capable browser, then press Connect / Refresh again.
            """).callout(kind="warn")
        )
    mo.vstack(_views)
    return


@app.cell
def _():
    from wsorb.flight import PlannerConfig, basic_command, plan_route, predict_obstacles

    return PlannerConfig, basic_command, plan_route, predict_obstacles


@app.cell
def _():
    from wsorb.experiment import Decision, ExperimentRunner

    return Decision, ExperimentRunner


@app.cell
def _(ExperimentRunner, mo):
    experiment = ExperimentRunner()
    get_applied_config, set_applied_config = mo.state(None)
    get_camera_report, set_camera_report = mo.state(None)
    return experiment, get_applied_config, get_camera_report, set_applied_config, set_camera_report


@app.cell
def _(LocalApiError, LocalSimulation, experiment, inspect, suppress):
    def stopped_sim(connection_path):
        stopped = experiment.stop(timeout_s=10)
        if stopped["state"] in {"starting", "running", "stopping"}:
            raise LocalApiError("Worker is still stopping. No second command writer was used.")
        return LocalSimulation.from_file(connection_path)

    def policy_sources(predict, search, decide):
        sources = {}
        for name, function in (
            ("predict_scene", predict),
            ("find_path", search),
            ("decide_command", decide),
        ):
            with suppress(OSError, TypeError):
                sources[f"{name}, inspectable function at action"] = inspect.getsource(function)
        return sources

    return policy_sources, stopped_sim


@app.cell
def _(lab_slider, mo):
    default_world_settings = {
        "ship": {"mass_kg": 3388.7, "inertia_scale": 1.0},
        "debris": {
            "count": 500,
            "radius_min_m": 0.4,
            "radius_max_m": 40.4,
            "speed_mps": 20.0,
            "speed_spread_mps": 5.0,
            "shell_inner_m": 200.0,
            "shell_outer_m": 3000.0,
            "stream_axis": "y",
            "stream_sign": -1,
        },
    }
    world_settings_form = (
        mo.md("""
    ### Ship draft
    {mass_kg}

    {inertia_scale}

    ### Directional debris-flow draft
    {count}

    {radius_min_m}

    {radius_max_m}

    {speed_mps}

    {speed_spread_mps}

    {shell_inner_m}

    {shell_outer_m}

    {stream_axis}

    {stream_sign}

    {use_seed}

    {seed}
    """)
        .batch(
            mass_kg=lab_slider(100, 100000, 0.1, 3388.7, "Ship mass, kg"),
            inertia_scale=lab_slider(0.1, 10.0, 0.1, 1.0, "Inertia tensor scale"),
            count=lab_slider(0, 1000, 1, 500, "Actual debris rigid-body count"),
            radius_min_m=lab_slider(0.05, 100.0, 0.05, 0.4, "Minimum debris radius, m"),
            radius_max_m=lab_slider(0.05, 100.0, 0.05, 40.4, "Maximum debris radius, m"),
            speed_mps=lab_slider(0.1, 200.0, 0.1, 20.0, "Stream speed, m/s"),
            speed_spread_mps=lab_slider(
                0.0, 100.0, 0.1, 5.0, "Stream speed spread, m/s; at most half the speed"
            ),
            shell_inner_m=lab_slider(
                0.0, 49990.0, 10.0, 200.0, "Shell inner offset from Phobos, m"
            ),
            shell_outer_m=lab_slider(
                10.0,
                50000.0,
                10.0,
                3000.0,
                "Shell outer offset from Phobos, m; at least 10 m beyond inner",
            ),
            stream_axis=mo.ui.dropdown(
                options=["x", "y", "z"],
                value="y",
                allow_select_none=False,
                label="Stream axis in the observation frame",
            ),
            stream_sign=mo.ui.dropdown(
                options={"Negative direction": -1, "Positive direction": 1},
                value="Negative direction",
                allow_select_none=False,
                label="Stream direction",
            ),
            use_seed=mo.ui.checkbox(value=False, label="Replace the current seed with this value"),
            seed=mo.ui.number(
                start=0, stop=4294967295, step=1, value=0, label="Optional reset seed"
            ),
        )
        .form(submit_button_label="Stage world/ship draft", label="World and ship draft")
    )
    world_reset_confirmed = mo.ui.checkbox(
        value=False, label="I understand this replaces the current local run and its physical state"
    )
    apply_world_settings = mo.ui.run_button(
        label="Apply world/ship settings and reset", kind="danger"
    )
    mo.vstack(
        [
            mo.md("## World and ship, local reset only"),
            mo.md(
                "Stage a draft, then confirm and apply it. This stops the live worker and "
                "replaces the world. Export first. Controller Apply never resets these settings. "
                "Mass/inertia change local hardware only on reset; gains and forecasts do not. "
                "Reference command caps remain 0.5 m/s² and 0.05 rad/s²."
            ).callout(kind="warn"),
            world_settings_form,
            world_reset_confirmed,
            apply_world_settings,
        ]
    )
    return (
        apply_world_settings,
        default_world_settings,
        world_reset_confirmed,
        world_settings_form,
    )


@app.cell
def _(default_world_settings, mo, world_settings_form):
    _staged = world_settings_form.value
    world_settings_draft = (
        {
            "ship": {key: _staged[key] for key in default_world_settings["ship"]},
            "debris": {key: _staged[key] for key in default_world_settings["debris"]},
        }
        if _staged is not None
        else default_world_settings
    )
    world_seed_draft = _staged["seed"] if _staged is not None and _staged["use_seed"] else None
    mo.json(
        {"settings": world_settings_draft, "seed_override": world_seed_draft},
        label="DRAFT world/ship, last staged form values; not applied to the game",
    )
    return world_seed_draft, world_settings_draft


@app.cell
def _(
    apply_world_settings,
    connection_file,
    experiment,
    mo,
    show_snapshot,
    stopped_sim,
    world_reset_confirmed,
    world_seed_draft,
    world_settings_draft,
):
    mo.stop(not apply_world_settings.value)
    mo.stop(
        not world_reset_confirmed.value,
        mo.md("Confirm replacement, then click Apply world/ship settings and reset.").callout(
            kind="warn"
        ),
    )
    try:
        _sim = stopped_sim(connection_file.value)
        _snapshot = _sim.configure_experiment(world_settings_draft, seed=world_seed_draft)
        experiment.record_snapshot(_snapshot, source="world_reset")
        _world_view = show_snapshot(_snapshot)
    except (RuntimeError, ValueError) as _error:
        _world_view = mo.md(
            f"World/ship apply failed. {_error} Check radius order, speed spread and shell bounds; "
            "read the game's actual state before retrying an uncertain reset."
        ).callout(kind="danger")
    mo.output.replace(_world_view)
    return


@app.cell
def _(lab_slider, mo):
    camera_aim_form = (
        mo.md("""
    {top_yaw}

    {top_pitch}

    {bottom_yaw}

    {bottom_pitch}
    """)
        .batch(
            top_yaw=lab_slider(-89, 89, 1, 0, "Top camera yaw, degrees"),
            top_pitch=lab_slider(-50, 0, 1, 0, "Top camera pitch, degrees"),
            bottom_yaw=lab_slider(-89, 89, 1, 0, "Bottom camera yaw, degrees"),
            bottom_pitch=lab_slider(0, 50, 1, 0, "Bottom camera pitch, degrees"),
        )
        .form(submit_button_label="Stage camera draft", label="Ship-camera aim draft")
    )
    apply_camera_aim = mo.ui.run_button(label="Apply camera aim")
    capture_camera = mo.ui.dropdown(
        options={"Top camera": "top", "Bottom camera": "bottom", "Both cameras": "both"},
        value="Top camera",
        allow_select_none=False,
        label="Manual capture source",
    )
    capture_images = mo.ui.run_button(label="Capture selected ship cameras")
    mo.vstack(
        [
            mo.md("## Mounted ship cameras"),
            mo.md(
                "Aim uses the source-bounded PanTiltCamera helper. The read-only sensor contract "
                "is 60° FOV, 512×512 RGB8 per channel and fixed exposure. These are not viewer "
                "zoom controls. Drafts send nothing; captures occur only on a click."
            ),
            camera_aim_form,
            apply_camera_aim,
            capture_camera,
            capture_images,
        ]
    )
    return apply_camera_aim, camera_aim_form, capture_camera, capture_images


@app.cell
def _(camera_aim_form):
    camera_aim_draft = camera_aim_form.value or {
        "top_yaw": 0,
        "top_pitch": 0,
        "bottom_yaw": 0,
        "bottom_pitch": 0,
    }
    return (camera_aim_draft,)


@app.cell
def _(LocalSimulation, apply_camera_aim, camera_aim_draft, connection_file, mo, set_camera_report):
    mo.stop(not apply_camera_aim.value)
    try:
        _sim = LocalSimulation.from_file(connection_file.value)
        _sim.PanTiltCamera(
            _sim.topCamera, camera_aim_draft["top_yaw"], camera_aim_draft["top_pitch"]
        )
        _sim.PanTiltCamera(
            _sim.bottomCamera, camera_aim_draft["bottom_yaw"], camera_aim_draft["bottom_pitch"]
        )
        _camera_report = _sim.camera_status()
        set_camera_report(_camera_report)
        _camera_view = mo.json(_camera_report, label="Actual reported camera aim and optics")
    except (RuntimeError, ValueError) as _error:
        _camera_view = mo.md(
            f"Camera apply failed. {_error} One aim may already have applied. "
            "Reconnect and read camera status with a manual capture before retrying."
        ).callout(kind="warn")
    mo.output.replace(_camera_view)
    return


@app.cell
def _(LocalSimulation, capture_camera, capture_images, connection_file, mo, set_camera_report):
    mo.stop(not capture_images.value, mo.md("No camera data yet. Capture is manual, never polled."))
    _capture_views = []
    try:
        _sim = LocalSimulation.from_file(connection_file.value)
        _camera_report = _sim.camera_status()
        set_camera_report(_camera_report)
        _capture_views.append(mo.json(_camera_report, label="Actual camera status at read"))
        _cameras = ("top", "bottom") if capture_camera.value == "both" else (capture_camera.value,)
        for _camera in _cameras:
            _capture = _sim.capture_image(_camera)
            if _capture is None:
                _capture_views.append(
                    mo.md(
                        f"No {_camera} capture returned. Check GPU readiness and capture again."
                    ).callout(kind="warn")
                )
                continue
            _capture_views.extend(
                [
                    mo.md(f"### Actual {_camera} camera capture"),
                    mo.json(_capture["metadata"], label="Capture source and format metadata"),
                    mo.image(_capture["rgb"], alt=f"Actual {_camera} ship-camera RGB capture"),
                ]
            )
    except (RuntimeError, ValueError) as _error:
        _capture_views.append(
            mo.md(
                f"Capture unavailable. {_error} Keep the game visible with its GPU renderer ready, "
                "then click Capture again. No substitute image is shown."
            ).callout(kind="warn")
        )
    mo.output.replace(mo.vstack(_capture_views))
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    ## Prediction and planning

    These cells own the Python algorithm choices. Edit `predict_scene` to try
    a tracker or sensor-fusion model, and `find_path` to change the search.
    Both are pure functions. They receive public data and settings, never a
    simulation client. Widget changes only update Python configuration.

    The default forecast covers **3.6 simulation seconds**, not the whole
    route's travel time. Radar is partial. Tangential speed and sigma inflation
    are uncertainty assumptions, not collision guarantees.
    """)
    return


@app.cell
def _(lab_slider, mo):
    planner_controls = mo.ui.dictionary(
        {
            "cell_size_m": lab_slider(5, 200, 5, 40, "Grid cell size, m"),
            "clearance_m": lab_slider(0, 100, 1, 8, "Extra obstacle clearance, m"),
            "forecast_horizon_s": lab_slider(0.1, 15, 0.1, 3.6, "Forecast horizon, simulation s"),
            "max_measurement_age_s": lab_slider(
                0, 30, 0.1, 5.4, "Maximum radar measurement age, s"
            ),
            "unknown_tangential_speed_mps": lab_slider(
                0, 100, 1, 12, "Unmeasured tangential-speed allowance, m/s"
            ),
            "sigma_multiplier": lab_slider(0, 8, 0.1, 3, "Sensor uncertainty multiplier, sigma"),
        }
    )
    planner_controls.vstack()
    return (planner_controls,)


@app.cell
def _(PlannerConfig, asdict, mo, planner_controls):
    planner_settings = PlannerConfig(
        cell_size_m=planner_controls.value["cell_size_m"],
        corridor_half_width_m=800.0,
        max_nodes=50_000,
        max_expansions=12_000,
        clearance_m=planner_controls.value["clearance_m"],
        forecast_horizon_s=planner_controls.value["forecast_horizon_s"],
        max_measurement_age_s=planner_controls.value["max_measurement_age_s"],
        unknown_tangential_speed_mps=planner_controls.value["unknown_tangential_speed_mps"],
        sigma_multiplier=planner_controls.value["sigma_multiplier"],
    )
    mo.json(
        asdict(planner_settings), label="Editable Python planner configuration, not applied yet"
    )
    return (planner_settings,)


@app.cell
def _(predict_obstacles):
    def predict_scene(observation, settings):
        """Predict obstacle envelopes from the public radar and its original times."""
        # Replace this with a reviewed Kalman/fusion model, not hidden debris truth.
        # Geometry, sensor noise, clock and rotation metadata come from observation.
        return predict_obstacles(observation, settings)

    return (predict_scene,)


@app.cell
def _(plan_route):
    def find_path(frame, settings):
        """Run the team's Python A* search on this prediction frame."""
        return plan_route(frame, settings)

    return (find_path,)


@app.cell
def _(mo):
    plan_and_show = mo.ui.run_button(label="Plan and show route")
    mo.vstack(
        [
            plan_and_show,
            mo.md("""
            This button reads fresh sensors, predicts and plans in Python, then
            replaces only the browser's visual preview. It sends no flight
            command and does not pause, step or start the simulation.
            """),
        ]
    )
    return (plan_and_show,)


@app.cell
def _(
    LocalApiError,
    LocalSimulation,
    connection_file,
    find_path,
    mo,
    plan_and_show,
    planner_settings,
    predict_scene,
    show_plan,
    show_snapshot,
):
    mo.stop(not plan_and_show.value)
    _plan_views = []
    _sim = None
    try:
        _sim = LocalSimulation.from_file(connection_file.value)
        _source = _sim.snapshot()
        _observation = _source["observation"]
        _frame = predict_scene(_observation, planner_settings)
        _route = find_path(_frame, planner_settings)
        _plan_views.append(show_plan(_route, _observation, _frame))
        _plan_views.append(
            mo.accordion({"Actual sensor snapshot used by Python": show_snapshot(_source)})
        )
        _sim.preview(_route.to_preview())
        _plan_views.append(
            mo.json(_sim.snapshot()["navigation"], label="Actual browser preview after publishing")
        )
    except (LocalApiError, ValueError) as _error:
        _plan_views.append(
            mo.md(f"""
            Planning or preview update failed. {_error}

            An earlier preview is not a new result. If still connected, this
            action attempts to remove it; check the actual report below.
            A policy step never reuses this cell's route.
            """).callout(kind="warn")
        )
        if _sim is not None:
            try:
                _sim.preview(None)
            except (LocalApiError, ValueError) as _clear_error:
                _plan_views.append(
                    mo.md(f"Old preview removal failed. {_clear_error}").callout(kind="warn")
                )
            try:
                _plan_views.append(
                    mo.json(
                        _sim.snapshot()["navigation"],
                        label="Browser preview after removal attempt; inspect its source time",
                    )
                )
            except (LocalApiError, ValueError) as _read_error:
                _plan_views.append(
                    mo.md(f"Cannot read the remaining preview. {_read_error}").callout(kind="warn")
                )
    mo.vstack(_plan_views)
    return


@app.cell
def _(lab_slider, mo):
    controller_mode = mo.ui.dropdown(
        options={
            "Coast, zero command": "coast",
            "Basic PD + public radar": "basic_pd_radar",
            "Planned waypoint PD + public radar": "planned_waypoint_pd",
        },
        value="Coast, zero command",
        allow_select_none=False,
        label="Controller applied only by an armed run or single-step click",
        full_width=True,
    )
    gain_controls = mo.ui.dictionary(
        {
            "position_gain_s2": lab_slider(0, 0.02, 0.0001, 0.0012, "Position gain, s⁻²"),
            "velocity_gain_s": lab_slider(0, 1, 0.005, 0.07, "Velocity damping gain, s⁻¹"),
            "avoidance_range_m": lab_slider(1, 500, 1, 70, "Radar avoidance range, m"),
            "avoidance_accel_mps2": lab_slider(
                0, 2, 0.05, 0.7, "Avoidance request before limiting, m/s²"
            ),
            "waypoint_arrival_m": lab_slider(0.1, 200, 0.1, 20, "Waypoint arrival distance, m"),
        }
    )
    mo.vstack([mo.md("## Controller configuration"), controller_mode, gain_controls.vstack()])
    return controller_mode, gain_controls


@app.cell
def _(gain_controls, mo):
    controller_gains = dict(gain_controls.value)
    mo.json(controller_gains, label="Editable Python controller gains, not applied yet")
    return (controller_gains,)


@app.cell
def _(basic_command, np):
    def decide_command(observation, route, mode, gains):
        """Select finite body-frame SI 3-vectors without calling any client."""
        if mode == "coast":
            return np.zeros(3), np.zeros(3)

        goal_world_m = None
        if mode == "planned_waypoint_pd":
            if route is None:
                raise ValueError("Route mode requires a plan from this step's fresh observation.")
            if route.status == "at_waypoint":
                return np.zeros(3), np.zeros(3)
            if route.status != "candidate":
                raise ValueError(f"No candidate route; no flight command sent. {route.detail}")
            position = np.asarray(observation["shuttle"]["position"], dtype=float)
            points = np.asarray(route.points_world_m, dtype=float)
            arrival = gains["waypoint_arrival_m"]
            if not np.isfinite(arrival) or arrival <= 0:
                raise ValueError("Waypoint arrival distance must be finite and positive.")
            if position.shape != (3,) or not np.isfinite(position).all():
                raise ValueError("Own position must be a finite world-frame 3-vector.")
            if points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(points).all():
                raise ValueError("Candidate route must contain finite world-frame 3-vectors.")
            # The waypoint rule is Python policy, not browser or Rust control logic.
            goal_world_m = next(
                (point for point in points if np.linalg.norm(point - position) > arrival), None
            )
            if goal_world_m is None:
                return np.zeros(3), np.zeros(3)
        elif mode != "basic_pd_radar":
            raise ValueError(f"Unknown controller mode: {mode}")

        # basic_command uses explicit rotation metadata and reference vector limits.
        return basic_command(
            observation,
            goal_world_m=goal_world_m,
            position_gain_s2=gains["position_gain_s2"],
            velocity_gain_s=gains["velocity_gain_s"],
            avoidance_range_m=gains["avoidance_range_m"],
            avoidance_accel_mps2=gains["avoidance_accel_mps2"],
        )

    return (decide_command,)


@app.cell
def _(lab_slider, mo):
    duration_s = lab_slider(0.1, 300, 0.1, 30, "Live run duration, simulation s")
    cadence_hz = lab_slider(0.1, 5, 0.1, 1, "Requested Python controller cadence, Hz")
    policy_armed = mo.ui.checkbox(
        value=False,
        label="Allow Apply and run or single-step clicks to command my local shuttle",
    )
    apply_and_run = mo.ui.run_button(label="Apply and run", kind="warn")
    stop_run = mo.ui.run_button(label="Stop run", kind="danger")
    mo.vstack(
        [
            mo.md("## Apply attributes and watch the flight"),
            duration_s,
            cadence_hz,
            policy_armed,
            mo.hstack([apply_and_run, stop_run], justify="start", wrap=True),
            mo.md("""
            Apply captures the current Python functions, controller and settings,
            then runs the actual game at 1×. Editing them alone does not change
            an active run. Apply again to stop it and start with the new values.
            Coast is the default; zero thrust does not brake the shuttle.

            Bridge round-trips and planning can reduce the requested decision
            cadence. Stop run requests zero forces and pause. Native manual
            intervention, pause or reset also interrupts the worker.
            """),
        ]
    )
    return apply_and_run, cadence_hz, duration_s, policy_armed, stop_run


@app.cell
def _(
    Decision,
    apply_and_run,
    asdict,
    cadence_hz,
    connection_file,
    controller_gains,
    controller_mode,
    decide_command,
    duration_s,
    experiment,
    find_path,
    math,
    mo,
    planner_settings,
    policy_armed,
    policy_sources,
    predict_scene,
    set_applied_config,
    source_manifest,
    stopped_sim,
):
    mo.stop(not apply_and_run.value)
    mo.stop(
        not policy_armed.value,
        mo.md("Check the arming box, then click Apply and run.").callout(kind="warn"),
    )

    def _capture_policy(predict, search, decide, settings, mode, gains, decision_type):
        # A real closure keeps this run independent of later reactive cell edits.
        def policy(observation):
            route = None
            if mode == "planned_waypoint_pd":
                frame = predict(observation, settings)
                route = search(frame, settings)
            accel, angular_accel = decide(observation, route, mode, gains)
            return decision_type(
                accel,
                angular_accel,
                preview=route.to_preview() if route is not None else None,
            )

        return policy

    try:
        _duration = duration_s.value
        _cadence = cadence_hz.value
        if _duration is None or not math.isfinite(_duration) or not 0 < _duration <= 300:
            raise ValueError("Live duration must be finite and greater than 0, up to 300 seconds.")
        if _cadence is None or not math.isfinite(_cadence) or not 0 < _cadence <= 5:
            raise ValueError("Requested cadence must be finite and greater than 0, up to 5 Hz.")
        _sim = stopped_sim(connection_file.value)
        _policy = _capture_policy(
            predict_scene,
            find_path,
            decide_command,
            planner_settings,
            controller_mode.value,
            dict(controller_gains),
            Decision,
        )
        experiment.start(_sim, _policy, duration_s=_duration, cadence_hz=_cadence)
        _captured = {
            "action": "live_run",
            "controller": controller_mode.value,
            "gains": dict(controller_gains),
            "planner": asdict(planner_settings),
            "duration_s": _duration,
            "requested_cadence_hz": _cadence,
            "python_functions": source_manifest(
                policy_sources(predict_scene, find_path, decide_command)
            ),
            "source_note": "Inspectable function text only; dependencies are not standalone.",
        }
        set_applied_config(_captured)
        _apply_view = mo.vstack(
            [
                mo.md(f"[Watch the actual local game]({_sim.url})"),
                mo.md("Apply requested. The monitor below reports the actual worker state."),
                mo.json(
                    _captured,
                    label="Settings captured by this Apply click",
                ),
            ]
        )
    except (RuntimeError, ValueError) as _error:
        _apply_view = mo.md(f"""
        Apply failed. {_error}

        Check the live status below. If cleanup or the connection failed, use
        the game HUD to check pause and requested forces before trying again.
        """).callout(kind="danger")
    mo.output.replace(_apply_view)
    return


@app.cell
def _(experiment, mo, stop_run):
    mo.stop(not stop_run.value)
    try:
        _stopped = experiment.stop(timeout_s=10)
        _stop_run_views = [
            mo.json(
                {key: value for key, value in _stopped.items() if key != "latest"},
                label="Actual Stop run result",
            )
        ]
        if _stopped["state"] == "stopping":
            _stop_run_views.append(
                mo.md("Worker cleanup is still pending. Do not start another writer.").callout(
                    kind="warn"
                )
            )
        _stop_run_view = mo.vstack(_stop_run_views)
    except (RuntimeError, ValueError) as _error:
        _stop_run_view = mo.md(f"Stop run failed. {_error}").callout(kind="danger")
    mo.output.replace(_stop_run_view)
    return


@app.cell
def _(mo):
    experiment_refresh = mo.ui.refresh(
        options=["1s", "2s", "5s"],
        default_interval="1s",
        label="Read-only live monitor refresh",
    )
    mo.vstack([mo.md("## Live experiment status"), experiment_refresh])
    return (experiment_refresh,)


@app.cell
def _(experiment, experiment_refresh, mo, show_snapshot):
    _refresh_tick = experiment_refresh.value
    _status = experiment.status()
    _monitor_views = [
        mo.json(
            {key: value for key, value in _status.items() if key != "latest"},
            label="Actual worker state and received sample count",
        )
    ]
    if _status["error"]:
        _monitor_views.append(mo.md(_status["error"]).callout(kind="danger"))
    if _status["latest"] is not None:
        _monitor_views.append(show_snapshot(_status["latest"]))
    else:
        _monitor_views.append(mo.md("No experiment samples yet. Apply and run is not automatic."))
    mo.output.replace(mo.vstack(_monitor_views))
    return


@app.cell
def _(mo):
    history_metric_labels = {
        "Speed, rotating frame, m/s": "speed_mps",
        "Radial climb, m/s": "radial_climb_mps",
        "Terrain-centre clearance, m": "terrain_center_clearance_m",
        "Target centre range, m": "target_center_range_m",
        "Target closing speed, m/s": "target_closing_mps",
        "Applied thrust acceleration, m/s²": "applied_thrust_accel_mps2",
        "Applied thrust force, N": "thrust_force_n",
        "Applied torque, N m": "torque_nm",
        "Spin, rad/s": "angular_speed_rad_s",
        "Estimated fuel used, kg": "fuel_used_kg_estimate",
        "Estimated contact damage, J": "damage_j_estimate",
        "Actual ship mass, kg": "ship_mass_kg",
        "Actual ship inertia xx, kg m²": "ship_inertia_xx_kg_m2",
        "Reference command cap, m/s²": "reference_accel_limit_mps2",
        "Requested command x, body m/s²": "command_accel_body_x_mps2",
        "Requested command y, body m/s²": "command_accel_body_y_mps2",
        "Requested command z, body m/s²": "command_accel_body_z_mps2",
    }
    history_metric = mo.ui.dropdown(
        options=history_metric_labels,
        value="Speed, rotating frame, m/s",
        allow_select_none=False,
        label="Measured history metric, read-only",
        full_width=True,
    )
    mo.vstack(
        [
            mo.md("## Measured history"),
            mo.md(
                "Pan or zoom the chart; inspect exact values in the table. The rolling buffer "
                "holds at most 2000 received telemetry samples, not all physics substeps. "
                "Held telemetry replaces its previous row. Gaps, runs, reset epochs and manual "
                "interventions split lines. Missing readings are never plotted as zero. "
                "Fuel and contact damage are local estimates, not official scores."
            ),
            history_metric,
        ]
    )
    return history_metric, history_metric_labels


@app.cell
def _(alt):
    def history_plot(rows, metric, label, clock):
        """Plot supplied measured rows only, without joining missing readings or epochs."""
        points = []
        gap = 0
        for row in rows:
            if row.get(metric) is None or row.get(clock) is None:
                gap += 1
                continue
            points.append({**row, "plot_segment": f"{row['segment']}:{gap}"})
        return (
            alt.Chart(alt.Data(values=points))
            .mark_line(point=True)
            .encode(
                x=alt.X(f"{clock}:Q", title=clock.replace("_", " ")),
                y=alt.Y(f"{metric}:Q", title=label, scale=alt.Scale(zero=False)),
                detail="plot_segment:N",
                color=alt.Color("segment:N", title="Observed segment"),
                order=alt.Order(f"{clock}:Q"),
                tooltip=[
                    alt.Tooltip(f"{clock}:Q"),
                    alt.Tooltip(f"{metric}:Q"),
                    "telemetry_epoch:Q",
                    "observation_time_s:Q",
                    "manual_interventions:Q",
                    "sample_source:N",
                ],
            )
            .properties(width="container", height=280)
            .interactive()
        )

    return (history_plot,)


@app.cell
def _(
    experiment,
    experiment_refresh,
    history_metric,
    history_metric_labels,
    history_plot,
    mo,
):
    _refresh_tick = experiment_refresh.value
    _rows = experiment.history()
    _metric = history_metric.value
    _label = next(label for label, field in history_metric_labels.items() if field == _metric)
    _clock = (
        "observation_time_s"
        if _metric.startswith(("ship_", "reference_", "command_"))
        else "telemetry_time_s"
    )
    flight_history_chart = None
    _history_views = []
    if any(row.get(_metric) is not None and row.get(_clock) is not None for row in _rows):
        flight_history_chart = mo.ui.altair_chart(
            history_plot(_rows, _metric, _label, _clock),
            chart_selection=False,
            legend_selection=False,
            label=f"Received measurements: {_label}",
        )
        _history_views.append(flight_history_chart)
    else:
        _history_views.append(
            mo.md(
                "No measured values for this metric yet. Connect / Refresh, or explicitly run an "
                "armed experiment. This view never reads sensors or sends flight commands."
            )
        )
    if _rows:
        _history_views.append(
            mo.accordion(
                {
                    "Measured rows, telemetry and observation source clocks": mo.ui.table(
                        _rows,
                        label="Rolling measured history",
                        selection=None,
                        page_size=12,
                        show_column_summaries=False,
                    ),
                    "Observed manual interventions": mo.ui.table(
                        [row for row in _rows if row["manual_intervention"]],
                        label="Counter changes actually observed in received snapshots",
                        selection=None,
                        page_size=8,
                        show_column_summaries=False,
                    ),
                }
            )
        )
    mo.output.replace(mo.vstack(_history_views))
    return (flight_history_chart,)


@app.cell
def _(lab_slider, mo):
    step_seconds = lab_slider(0.001, 1.0, 0.001, 0.05, "Base experiment interval, simulation s")
    schedule_slot = mo.ui.number(
        start=0, step=1, value=0, label="Schedule row to execute on the next click, zero-based"
    )
    mo.vstack(
        [
            mo.md("## Optional paused-step analysis"),
            step_seconds,
            schedule_slot,
            mo.md(
                "Edit the schedule cell below. Each click runs only the chosen row, never a loop."
            ),
        ]
    )
    return schedule_slot, step_seconds


@app.cell
def _(mo, step_seconds):
    # Add or vary durations here. All entries must satisfy 0 < seconds <= 1.
    # The policy-step button does not advance the selected row automatically.
    experiment_schedule_s = (step_seconds.value,)
    mo.ui.table(
        [
            {"row": index, "simulation_seconds": dt}
            for index, dt in enumerate(experiment_schedule_s)
        ],
        label="Configured experiment intervals, not simulation results",
        selection=None,
    )
    return (experiment_schedule_s,)


@app.cell
def _(mo):
    run_policy_step = mo.ui.run_button(label="Run one policy step", kind="warn")
    mo.vstack(
        [
            run_policy_step,
            mo.md("""
            Uses the arming checkbox above and stops the live worker first.
            A fresh observation feeds the selected Python policy. `run_step`
            pauses, applies one reference-limited command, advances only the
            chosen interval and attempts a final zero command. Route mode
            replans from this input, never from a stored preview.
            """),
        ]
    )
    return (run_policy_step,)


@app.cell
def _(
    asdict,
    connection_file,
    controller_gains,
    controller_mode,
    decide_command,
    experiment,
    experiment_schedule_s,
    find_path,
    math,
    mo,
    np,
    planner_settings,
    policy_armed,
    policy_sources,
    predict_scene,
    run_policy_step,
    schedule_slot,
    set_applied_config,
    show_plan,
    show_snapshot,
    source_manifest,
    stopped_sim,
):
    mo.stop(not run_policy_step.value)
    mo.stop(
        not policy_armed.value,
        mo.md("Check the arming box, then click Run one policy step.").callout(kind="warn"),
    )
    try:
        _slot = schedule_slot.value
        if _slot is None or not math.isfinite(_slot) or _slot != int(_slot):
            raise ValueError("Schedule row must be a finite integer.")
        if not 0 <= _slot < len(experiment_schedule_s):
            raise ValueError("Choose an existing schedule row.")
        _seconds = experiment_schedule_s[int(_slot)]
        if (
            isinstance(_seconds, bool)
            or not isinstance(_seconds, (int, float))
            or not math.isfinite(_seconds)
            or not 0 < _seconds <= 1.0
        ):
            raise ValueError("Step duration must be finite and greater than 0, up to 1 second.")
        _sim = stopped_sim(connection_file.value)
        _before = _sim.snapshot()
        experiment.record_snapshot(_before, source="policy_input")
        _observation = _before["observation"]
        _mode = controller_mode.value
        _route = None
        if _mode == "planned_waypoint_pd":
            _frame = predict_scene(_observation, planner_settings)
            _route = find_path(_frame, planner_settings)
        _accel, _angular_accel = decide_command(_observation, _route, _mode, controller_gains)
        _after = _sim.run_step(_accel, angular_accel=_angular_accel, seconds=_seconds)
        experiment.record_snapshot(_after, source="policy_step_before_cleanup")
        set_applied_config(
            {
                "action": "paused_policy_step",
                "controller": _mode,
                "gains": dict(controller_gains),
                "planner": asdict(planner_settings),
                "schedule_row": int(_slot),
                "requested_seconds": _seconds,
                "policy_input_time_s": _observation["time_s"],
                "python_functions": source_manifest(
                    policy_sources(predict_scene, find_path, decide_command)
                ),
                "source_note": "Inspectable function text only; dependencies are not standalone.",
            }
        )
        _step_views = [
            mo.md("""
            The policy step returned. This snapshot was captured before the
            client's final best-effort stop. Press Connect / Refresh to check
            current runtime and requested forces after cleanup.
            """).callout(kind="info"),
            mo.json(
                {
                    "controller": _mode,
                    "policy_input_time_s": _observation["time_s"],
                    "schedule_row": int(_slot),
                    "requested_seconds": _seconds,
                    "accel_mps2_body": np.asarray(_accel).tolist(),
                    "angular_accel_rad_s2_body": np.asarray(_angular_accel).tolist(),
                },
                label="Python policy request, subject to reference vector-norm limits",
            ),
            show_snapshot(_after),
        ]
        if _route is not None:
            _step_views.append(
                mo.accordion(
                    {
                        "Fresh Python route used for this step": show_plan(
                            _route, _observation, _frame
                        )
                    }
                )
            )
        _step_view = mo.vstack(_step_views)
    except (RuntimeError, ValueError) as _error:
        _step_view = mo.md(f"""
        Policy-step error. {_error}

        A command may already have reached the game. If the connection was lost,
        use the game HUD to check pause and thrust. A stop request cannot be
        guaranteed over a failed connection.
        """).callout(kind="danger")
    mo.output.replace(_step_view)
    return


@app.cell
def _(mo):
    stop_forces = mo.ui.run_button(label="Stop requested forces", kind="danger")
    mo.vstack(
        [
            mo.md("## Stop requested thrust and torque"),
            mo.md("Stops the live worker first, then zeros forces. It does not cancel momentum."),
            stop_forces,
        ]
    )
    return (stop_forces,)


@app.cell
def _(connection_file, experiment, mo, show_snapshot, stop_forces, stopped_sim):
    mo.stop(not stop_forces.value)
    try:
        _sim = stopped_sim(connection_file.value)
        _sim.stop()
        _snapshot = _sim.snapshot()
        experiment.record_snapshot(_snapshot, source="stop_forces")
        _stop_view = show_snapshot(_snapshot)
    except (RuntimeError, ValueError) as _error:
        _stop_view = mo.md(f"""
        Stop report unavailable. {_error}

        Check the game HUD directly. A failed connection cannot guarantee that
        zero requested forces reached the shuttle.
        """).callout(kind="danger")
    mo.output.replace(_stop_view)
    return


@app.cell
def _(mo):
    upright = mo.ui.run_button(label="Upright, local test reset", kind="warn")
    mo.vstack(
        [
            upright,
            mo.md("""
            Local test reset, not a flight manoeuvre or STP method. Stops the live
            worker, pauses the actual game, aligns the shuttle parallel to the
            physical pad and clears spin. It changes the current physical state.
            """).callout(kind="warn"),
        ]
    )
    return (upright,)


@app.cell
def _(connection_file, experiment, mo, show_snapshot, stopped_sim, upright):
    mo.stop(not upright.value)
    try:
        _sim = stopped_sim(connection_file.value)
        _snapshot = _sim.upright()
        experiment.record_snapshot(_snapshot, source="upright")
        _upright_view = show_snapshot(_snapshot)
    except (RuntimeError, ValueError) as _error:
        _upright_view = mo.md(f"Upright report unavailable. {_error}").callout(kind="danger")
    mo.output.replace(_upright_view)
    return


@app.cell
def _(mo):
    reset_confirmed = mo.ui.checkbox(
        value=False,
        label="I understand reset replaces the current local run",
    )
    reset_run = mo.ui.run_button(label="Reset local run", kind="danger")
    mo.vstack(
        [
            mo.md("## Replace the current run"),
            mo.md("""
            Stops the live worker, then pauses and replaces the local run.
            Unsaved current state is lost. Export anything you need in the
            game before resetting. Reset does not run the policy.
            """).callout(kind="warn"),
            reset_confirmed,
            reset_run,
        ]
    )
    return reset_confirmed, reset_run


@app.cell
def _(
    connection_file,
    experiment,
    mo,
    reset_confirmed,
    reset_run,
    show_snapshot,
    stopped_sim,
):
    mo.stop(not reset_run.value)
    mo.stop(
        not reset_confirmed.value,
        mo.md("Confirm replacement, then click Reset local run.").callout(kind="warn"),
    )
    try:
        _sim = stopped_sim(connection_file.value)
        _snapshot = _sim.reset()
        experiment.record_snapshot(_snapshot, source="reset")
        _reset_view = show_snapshot(_snapshot)
    except (RuntimeError, ValueError) as _error:
        _reset_view = mo.md(f"Reset report unavailable. {_error}").callout(kind="danger")
    mo.output.replace(_reset_view)
    return


@app.cell
def _(mo):
    prepare_export = mo.ui.run_button(label="Prepare export snapshot")
    mo.vstack(
        [
            mo.md("## Export this experiment"),
            mo.md(
                "Prepare freezes the latest received public snapshot, rolling measurements and "
                "current Python drafts for download. It sends no game request and writes no file. "
                "Stop first for a final run record. Save notebook edits before exporting Python; "
                "that download is the actual saved notebook, not a standalone SDK controller. "
                "Unsubmitted form edits remain in the browser and are not exported."
            ),
            prepare_export,
        ]
    )
    return (prepare_export,)


@app.cell
def _(
    Path,
    asdict,
    cadence_hz,
    camera_aim_draft,
    controller_gains,
    controller_mode,
    decide_command,
    duration_s,
    experiment,
    experiment_document,
    experiment_schedule_s,
    find_path,
    flight_modules,
    get_applied_config,
    get_camera_report,
    history_csv,
    json_bytes,
    lab_source_path,
    mo,
    planner_settings,
    policy_sources,
    predict_scene,
    prepare_export,
    schedule_slot,
    world_seed_draft,
    world_settings_draft,
):
    mo.stop(not prepare_export.value)
    try:
        _status = experiment.status()
        _rows = experiment.history()
        _flight_dir = Path(flight_modules.__file__).resolve().parent
        _saved_notebook = lab_source_path.read_bytes()
        _sources = {"notebooks/flight_lab.py, saved file at export": _saved_notebook}
        _sources.update(
            {
                f"src/wsorb/flight/{path.name}, saved file at export": path.read_bytes()
                for path in sorted(_flight_dir.glob("*.py"))
            }
        )
        for _name in ("experiment.py", "experiment_io.py"):
            _sources[f"src/wsorb/{_name}, saved file at export"] = (
                _flight_dir.parent / _name
            ).read_bytes()
        _sources.update(policy_sources(predict_scene, find_path, decide_command))
        _document = experiment_document(
            applied_config=get_applied_config(),
            draft_config={
                "controller": controller_mode.value,
                "gains": dict(controller_gains),
                "planner": asdict(planner_settings),
                "live_duration_s": duration_s.value,
                "requested_cadence_hz": cadence_hz.value,
                "selected_schedule_row": schedule_slot.value,
                "world_ship_last_staged": world_settings_draft,
                "world_seed_override": world_seed_draft,
                "camera_aim_last_staged": camera_aim_draft,
            },
            selected_policy=controller_mode.value,
            schedule_s=experiment_schedule_s,
            history=_rows,
            latest_snapshot=_status["latest"],
            sources=_sources,
            camera_status=get_camera_report(),
        )
        _export_views = [
            mo.md("Downloads freeze this prepared snapshot. Prepare again for newer data."),
            mo.hstack(
                [
                    mo.download(
                        json_bytes(_document),
                        filename="flight-experiment.json",
                        mimetype="application/json",
                        label="Download experiment JSON",
                    ),
                    mo.download(
                        history_csv(_rows),
                        filename="flight-history.csv",
                        mimetype="text/csv",
                        label="Download measured history CSV",
                    ),
                    mo.download(
                        _saved_notebook,
                        filename="flight_lab.py",
                        mimetype="text/x-python",
                        label="Download saved Python notebook",
                    ),
                ],
                justify="start",
                wrap=True,
            ),
            mo.json(
                _document["python_sources"], label="Actual supplied Python source SHA-256 hashes"
            ),
        ]
        if _status["state"] in {"starting", "running", "stopping"}:
            _export_views.append(
                mo.md(
                    "The worker was active when prepared. These are received samples so far, "
                    "not a completed run. Stop and prepare again for the final record."
                ).callout(kind="warn")
            )
        _export_view = mo.vstack(_export_views)
    except (OSError, TypeError, ValueError):
        _export_view = mo.md(
            "Export preparation failed. Save the notebook, check that its source files remain "
            "available, then prepare again. No file was written and no game command was sent."
        ).callout(kind="warn")
    mo.output.replace(_export_view)
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    ## Source and sharing

    Inspect the [Python flight algorithms](../src/wsorb/flight/),
    [live experiment runner](../src/wsorb/experiment.py),
    [local experiment client](../src/wsorb/local_api.py),
    [simulation and rendering](../simulation/src/) and
    [licensed SDK adapter](../src/wsorb/stp_api.py).
    Python edits need no WASM rebuild. Rust changes still need a rebuild and
    browser reload. Compare source hashes, loaded engine build and applied settings.

    Share source through the private repository. Teammates run their own game/lab
    pair. Never publish this executable editor, its connection file or its token.
    """)
    return


if __name__ == "__main__":
    app.run()
