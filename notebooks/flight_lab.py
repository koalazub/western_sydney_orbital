import marimo

__generated_with = "0.25.2"
app = marimo.App(width="medium")


@app.cell
def _():
    import json
    import math
    from dataclasses import asdict

    import marimo as mo
    import numpy as np

    from wsorb.local_api import LocalApiError, LocalSimulation

    return LocalApiError, LocalSimulation, asdict, json, math, mo, np


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    # Flight lab

    Prediction, A* planning, waypoint selection and controllers live in
    **editable Python**. Rust, Bevy and Rapier run the actual browser physics
    and rendering in WASM. Edit the functions and settings below, inspect a
    route, then click **Apply and run** to watch the current policy fly in the
    actual game. Paused single-step analysis is also available below.

    Start the local game with its private lab bridge, then open it in a
    WebGPU-capable browser. This notebook starts neither process. Opening the
    notebook, editing the policy and refreshing observations do not command
    the shuttle. The live monitor shows the worker's latest actual snapshot;
    other reports are point-in-time reads.

    Keep these algorithms independent of clients so the team can reuse them
    with the licensed Space Teams PRO adapter. The local physics differs.
    Ship-camera images remain genuine 512×512 RGB8 captures.

    `SetCommand(accelCommand, angAccelCommand)` and
    `GetNearestDebrisLocations(N)` share the official method names. They use
    body-frame SI 3-vectors and actual public 3×3 radar rows, respectively.
    `snapshot`, `preview`, `pause`, `step`, `run_step` and `upright` are **local
    experiment tools**, not STP methods. Preserve clock and rotation metadata when
    adapting observations between backends; do not assume identical epochs
    or quaternion conventions.
    """)
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    Keep **automatic cell execution** enabled when using these controls.
    Marimo resets a run button after its downstream cells finish. Lazy mode
    does not reset run buttons, so do not use it for command cells.
    """).callout(kind="warn")
    return


@app.cell
def _(json, mo):
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
                        key: value
                        for key, value in observation.items()
                        if key not in {"shuttle", "target", "mission", "radar"}
                    },
                    label="Public observation metadata",
                ),
                mo.accordion(
                    {
                        "Own shuttle": field_table(observation["shuttle"], label="Shuttle state"),
                        "Mission target": field_table(observation["target"], label="Target state"),
                        "Mission": field_table(observation["mission"], label="Mission state"),
                        "Public radar": mo.vstack(
                            [
                                mo.md("""
                                Rows are range-sorted measurements, not persistent object IDs.
                                Relative positions use the observation's rotating-frame axes,
                                not command body axes. Preserve measurement times. Range rate
                                is not a full relative-velocity vector. Radar is incomplete.
                                Check `radar_clock` before comparing timestamps across backends.
                                """),
                                radar_view,
                                mo.json(observation["radar"], label="Raw public 3×3 radar rows"),
                            ]
                        ),
                        "Flight telemetry": mo.vstack(
                            [
                                field_table(telemetry["metrics"], label="Measured flight metrics"),
                                mo.md("""
                                Telemetry has its own source time. Python previews keep their
                                planning input time until explicitly replaced.
                                """),
                                mo.json(telemetry, label="Telemetry status, units and caveats"),
                            ]
                        ),
                        "Last published Python preview": mo.vstack(
                            [
                                mo.md("""
                                This is the last Python report sent to the browser, not a live
                                planner. Check its source time. A preview does not command flight.
                                """),
                                mo.json(snapshot["navigation"], label="Current navigation status"),
                            ]
                        ),
                        "Runtime and engine build": mo.vstack(
                            [
                                mo.json(snapshot["runtime"], label="Actual browser runtime"),
                                mo.json(snapshot["engine_build"], label="Loaded engine build"),
                            ]
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
                mo.ui.table(
                    [
                        {
                            "source_time_s": observation["time_s"],
                            "public_radar_rows": len(observation["radar"]),
                            "status": plan.status,
                            "detail": plan.detail,
                            "route_points": len(points),
                        }
                    ],
                    label="Python result from this observation",
                    selection=None,
                ),
                field_table(
                    {
                        "measured_obstacles": frame.measured_obstacles,
                        "unmodelled_obstacles_lower_bound": frame.unmodelled_obstacles_lower_bound,
                        "predicted_envelopes_including_known_bodies": len(frame.obstacles),
                        "oldest_measurement_time_s": frame.oldest_measurement_time_s,
                        "newest_measurement_time_s": frame.newest_measurement_time_s,
                        "maximum_observed_age_s": frame.max_measurement_age_s,
                        "forecast_horizon_s": frame.forecast_horizon_s,
                        "tangential_allowance_mps": frame.assumed_tangential_speed_allowance_mps,
                    },
                    label="Public coverage and prediction uncertainty",
                ),
                mo.ui.table(
                    points, label="Computed route in observation world axes", selection=None
                )
                if points
                else mo.md("This plan returned no route points."),
                mo.json(
                    plan.to_preview(),
                    label="Prediction/search report, coverage counts and uncertainty assumptions",
                ),
            ]
        )

    return show_plan, show_snapshot


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
def _(LocalApiError, LocalSimulation, connect_refresh, connection_file, mo, show_snapshot):
    mo.stop(
        not connect_refresh.value,
        mo.md("Press Connect / Refresh to read the local game's status and public state."),
    )
    _views = []
    try:
        _sim = LocalSimulation.from_file(connection_file.value)
        _views.append(mo.md(f"[Open the actual local game in a separate window]({_sim.url})"))
        _views.append(mo.json(_sim.status(), label="Local bridge status"))
        _views.append(show_snapshot(_sim.snapshot()))
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
def _(ExperimentRunner):
    experiment = ExperimentRunner()
    return (experiment,)


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
def _(mo):
    planner_controls = mo.ui.dictionary(
        {
            "cell_size_m": mo.ui.number(
                start=5, stop=200, step=5, value=40, label="Grid cell size, m"
            ),
            "clearance_m": mo.ui.number(
                start=0, stop=100, step=1, value=8, label="Extra obstacle clearance, m"
            ),
            "forecast_horizon_s": mo.ui.number(
                start=0.1, stop=15, step=0.1, value=3.6, label="Forecast horizon, simulation s"
            ),
            "max_measurement_age_s": mo.ui.number(
                start=0, stop=30, step=0.1, value=5.4, label="Maximum radar measurement age, s"
            ),
            "unknown_tangential_speed_mps": mo.ui.number(
                start=0,
                stop=100,
                step=1,
                value=12,
                label="Unmeasured tangential-speed allowance, m/s",
            ),
            "sigma_multiplier": mo.ui.number(
                start=0, stop=8, step=0.1, value=3, label="Sensor uncertainty multiplier, sigma"
            ),
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
def _(mo):
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
            "position_gain_s2": mo.ui.number(
                start=0, stop=0.02, step=0.0001, value=0.0012, label="Position gain, s⁻²"
            ),
            "velocity_gain_s": mo.ui.number(
                start=0, stop=1, step=0.005, value=0.07, label="Velocity damping gain, s⁻¹"
            ),
            "avoidance_range_m": mo.ui.number(
                start=1, stop=500, step=1, value=70, label="Radar avoidance range, m"
            ),
            "avoidance_accel_mps2": mo.ui.number(
                start=0,
                stop=2,
                step=0.05,
                value=0.7,
                label="Avoidance request before limiting, m/s²",
            ),
            "waypoint_arrival_m": mo.ui.number(
                start=0.1, stop=200, step=0.1, value=20, label="Waypoint arrival distance, m"
            ),
        }
    )
    mo.vstack([mo.md("## Controller configuration"), controller_mode, gain_controls.vstack()])
    return controller_mode, gain_controls


@app.cell
def _(gain_controls, mo):
    controller_gains = {
        "position_gain_s2": gain_controls.value["position_gain_s2"],
        "velocity_gain_s": gain_controls.value["velocity_gain_s"],
        "avoidance_range_m": gain_controls.value["avoidance_range_m"],
        "avoidance_accel_mps2": gain_controls.value["avoidance_accel_mps2"],
        "waypoint_arrival_m": gain_controls.value["waypoint_arrival_m"],
    }
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
def _(mo):
    duration_s = mo.ui.number(
        start=0.1, stop=300, step=0.1, value=30, label="Live run duration, simulation s"
    )
    cadence_hz = mo.ui.number(
        start=0.1, stop=5, step=0.1, value=1, label="Requested Python controller cadence, Hz"
    )
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
    LocalApiError,
    LocalSimulation,
    apply_and_run,
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
    predict_scene,
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
        _sim = LocalSimulation.from_file(connection_file.value)
        _stopped = experiment.stop(timeout_s=10)
        if _stopped["state"] in {"starting", "running", "stopping"}:
            raise LocalApiError("Previous worker is still stopping. No new run was started.")
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
        _apply_view = mo.vstack(
            [
                mo.md(f"[Watch the actual local game]({_sim.url})"),
                mo.md("Apply requested. The monitor below reports the actual worker state."),
                mo.json(
                    {
                        "captured_controller": controller_mode.value,
                        "captured_gains": dict(controller_gains),
                        "duration_s": _duration,
                        "requested_cadence_hz": _cadence,
                    },
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
    step_seconds = mo.ui.number(
        start=0.001,
        stop=1.0,
        step=0.001,
        value=0.05,
        label="Base experiment interval, simulation s",
    )
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
    LocalApiError,
    LocalSimulation,
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
    predict_scene,
    run_policy_step,
    schedule_slot,
    show_plan,
    show_snapshot,
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
        _stopped = experiment.stop(timeout_s=10)
        if _stopped["state"] in {"starting", "running", "stopping"}:
            raise LocalApiError("Live worker is still stopping. No single step was requested.")
        _sim = LocalSimulation.from_file(connection_file.value)
        _before = _sim.snapshot()
        _observation = _before["observation"]
        _mode = controller_mode.value
        _route = None
        if _mode == "planned_waypoint_pd":
            _frame = predict_scene(_observation, planner_settings)
            _route = find_path(_frame, planner_settings)
        _accel, _angular_accel = decide_command(_observation, _route, _mode, controller_gains)
        _after = _sim.run_step(_accel, angular_accel=_angular_accel, seconds=_seconds)
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
def _(LocalApiError, LocalSimulation, connection_file, experiment, mo, show_snapshot, stop_forces):
    mo.stop(not stop_forces.value)
    try:
        _stopped = experiment.stop(timeout_s=10)
        if _stopped["state"] in {"starting", "running", "stopping"}:
            raise LocalApiError("Live worker is still stopping. No second command writer was used.")
        _sim = LocalSimulation.from_file(connection_file.value)
        _sim.stop()
        _stop_view = show_snapshot(_sim.snapshot())
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
def _(LocalApiError, LocalSimulation, connection_file, experiment, mo, show_snapshot, upright):
    mo.stop(not upright.value)
    try:
        _stopped = experiment.stop(timeout_s=10)
        if _stopped["state"] in {"starting", "running", "stopping"}:
            raise LocalApiError("Live worker is still stopping. Upright was not requested.")
        _sim = LocalSimulation.from_file(connection_file.value)
        _upright_view = show_snapshot(_sim.upright())
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
    LocalApiError,
    LocalSimulation,
    connection_file,
    experiment,
    mo,
    reset_confirmed,
    reset_run,
    show_snapshot,
):
    mo.stop(not reset_run.value)
    mo.stop(
        not reset_confirmed.value,
        mo.md("Confirm replacement, then click Reset local run.").callout(kind="warn"),
    )
    try:
        _stopped = experiment.stop(timeout_s=10)
        if _stopped["state"] in {"starting", "running", "stopping"}:
            raise LocalApiError("Live worker is still stopping. Reset was not requested.")
        _sim = LocalSimulation.from_file(connection_file.value)
        _sim.reset()
        _reset_view = show_snapshot(_sim.snapshot())
    except (RuntimeError, ValueError) as _error:
        _reset_view = mo.md(f"Reset report unavailable. {_error}").callout(kind="danger")
    mo.output.replace(_reset_view)
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
    Prediction, planning, controllers and schedules can change in these cells
    or pure Python modules without a WASM rebuild. Changes to Rust simulation
    or rendering still need a rebuild and browser reload. Compare the loaded
    engine hash, Python source and settings when comparing experiments.

    Teammates should clone the private repository and run their own local
    game/notebook pair. Do not publish this editor, its connection file, a
    Molab copy or a public editor URL. Share reviewed Python source through
    the private repository. The game opens separately; it is not embedded here.
    """)
    return


if __name__ == "__main__":
    app.run()
