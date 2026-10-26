import marimo

__generated_with = "0.25.2"
app = marimo.App(width="medium")


@app.cell
def _():
    import inspect
    import json
    import math
    import time
    from contextlib import suppress
    from dataclasses import asdict
    from pathlib import Path

    import altair as alt
    import marimo as mo
    import numpy as np

    import wsorb.flight as flight_modules
    from wsorb.experiment import (
        CHECKPOINT_RUN_LIMIT_S,
        MAX_CONTROLLER_RUN_S,
        ExperimentRunner,
        FlightStateError,
        LivePolicy,
        controller_start_notice,
        flight_context,
        lookahead_target,
        next_waypoint,
        observed_setup,
    )
    from wsorb.experiment_io import (
        experiment_document,
        history_csv,
        json_bytes,
        source_manifest,
    )
    from wsorb.flight import (
        PlannerConfig,
        ResilienceConfig,
        ResilientGuidance,
        basic_command,
        plan_route,
        predict_obstacles,
    )
    from wsorb.local_api import (
        MAX_DEBRIS_COUNT,
        LocalApiError,
        LocalSimulation,
        validate_experiment_settings,
        validate_position,
    )
    from wsorb.proposals import approve_world_proposal, parse_world_proposal

    lab_source_path = Path(__file__).resolve()
    return (
        CHECKPOINT_RUN_LIMIT_S,
        ExperimentRunner,
        FlightStateError,
        LivePolicy,
        LocalApiError,
        LocalSimulation,
        MAX_DEBRIS_COUNT,
        MAX_CONTROLLER_RUN_S,
        Path,
        PlannerConfig,
        ResilienceConfig,
        ResilientGuidance,
        alt,
        asdict,
        approve_world_proposal,
        basic_command,
        controller_start_notice,
        experiment_document,
        flight_context,
        flight_modules,
        history_csv,
        inspect,
        json,
        json_bytes,
        lab_source_path,
        lookahead_target,
        math,
        mo,
        next_waypoint,
        np,
        observed_setup,
        parse_world_proposal,
        plan_route,
        predict_obstacles,
        source_manifest,
        suppress,
        time,
        validate_experiment_settings,
        validate_position,
    )


@app.cell
def _(json, lab_source_path, validate_experiment_settings):
    scenario_source_path = lab_source_path.parents[1] / "simulation/scenarios/phobos_escape.json"
    workspace_scenario = json.loads(scenario_source_path.read_text(encoding="utf-8"))
    default_world_settings = validate_experiment_settings(workspace_scenario["experiment_settings"])
    impact_defaults = dict(workspace_scenario["impact_ejecta_defaults"])
    position_limit_m = workspace_scenario["limits"]["position_m"]
    return (
        default_world_settings,
        impact_defaults,
        position_limit_m,
        scenario_source_path,
        workspace_scenario,
    )


@app.cell
def _(mo):
    proposal_upload = mo.ui.file(
        filetypes=[".json"],
        multiple=False,
        max_size=65536,
        label="Local world proposal JSON",
    )
    review_proposal = mo.ui.run_button(label="Review proposal")
    adopt_proposal = mo.ui.run_button(label="Use reviewed proposal as draft")
    discard_proposal = mo.ui.run_button(label="Discard imported draft")
    get_reviewed_proposal, set_reviewed_proposal = mo.state(None)
    get_proposal_draft, set_proposal_draft = mo.state(None)
    return (
        adopt_proposal,
        discard_proposal,
        get_proposal_draft,
        get_reviewed_proposal,
        proposal_upload,
        review_proposal,
        set_proposal_draft,
        set_reviewed_proposal,
    )


@app.cell
def _(
    action_error, mo, parse_world_proposal, proposal_upload, review_proposal, set_reviewed_proposal
):
    mo.stop(not review_proposal.value)
    try:
        _contents = proposal_upload.contents()
        if _contents is None:
            raise ValueError("Choose a proposal JSON file before Review proposal.")
        _proposal = parse_world_proposal(_contents)
        set_reviewed_proposal(_proposal)
        mo.output.replace(
            mo.md(
                f"Proposal validated. Review content digest `{_proposal.digest}`. "
                "Check the normalized settings before adopting it. "
                "No game, file or Git state changed."
            )
        )
    except (RuntimeError, ValueError, TypeError) as _error:
        set_reviewed_proposal(None)
        mo.output.replace(action_error("Review proposal", _error))
    return


@app.cell
def _(
    action_error,
    adopt_proposal,
    approve_world_proposal,
    get_reviewed_proposal,
    mo,
    parse_world_proposal,
    proposal_upload,
    set_proposal_draft,
):
    mo.stop(not adopt_proposal.value)
    try:
        _reviewed = get_reviewed_proposal()
        _contents = proposal_upload.contents()
        if _reviewed is None or _contents is None:
            raise ValueError("Review the proposal before using it as a draft.")
        _current = parse_world_proposal(_contents)
        _draft = approve_world_proposal(_current, _reviewed.digest)
        set_proposal_draft({**_draft, "digest": _current.digest})
        mo.output.replace(
            mo.md(
                "Imported draft selected. The current flight is unchanged. Regenerate world "
                "will explicitly replace the run using this approved draft."
            )
        )
    except (RuntimeError, ValueError, TypeError) as _error:
        mo.output.replace(action_error("Adopt proposal draft", _error))
    return


@app.cell
def _(discard_proposal, mo, set_proposal_draft, set_reviewed_proposal):
    mo.stop(not discard_proposal.value)
    set_proposal_draft(None)
    set_reviewed_proposal(None)
    mo.output.replace(mo.md("Imported draft discarded. Manual draft controls are active again."))
    return


@app.cell
def _(
    default_world_settings,
    get_proposal_draft,
    get_reviewed_proposal,
    json,
    mo,
    parse_world_proposal,
):
    _template = parse_world_proposal(
        json.dumps(
            {
                "schema": "wsorb.local_world_proposal",
                "schema_version": 1,
                "preset": "far_side_l2",
                "seed": None,
                "settings": default_world_settings,
            }
        )
    )
    proposal_template_download = mo.download(
        _template.to_json_bytes(),
        filename="local-world-proposal.json",
        mimetype="application/json",
        label="Download proposal template",
    )
    _reviewed, _draft = get_reviewed_proposal(), get_proposal_draft()
    proposal_status_view = mo.vstack(
        [
            mo.md(f"Active imported draft: `{_draft['digest']}`. Regenerate world uses this draft.")
            if _draft is not None
            else mo.md(
                "No imported draft selected. Regenerate world uses the manual draft controls."
            ),
            mo.json(_reviewed.to_document(), label="Validated proposal for review")
            if _reviewed is not None
            else mo.md("Upload a local JSON file, then select Review proposal."),
            mo.json(_draft, label="Approved new-world draft") if _draft is not None else mo.md(""),
        ]
    )
    return proposal_status_view, proposal_template_download


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    # Flight lab

    Connect in Flight to watch the current game without pausing, thrust or reset.
    Run controller starts the selected Python policy. A browser-backed game must
    stay visible for live rendering. The explicit native headless backend runs
    real physics without a display. This notebook does not start either server.
    """)
    return


@app.cell
def _(FlightStateError, LocalApiError, json, mo, observed_setup, validate_position):
    def lab_slider(start, stop, step, value, label, *, disabled=False):
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
            disabled=disabled,
        )

    def field_table(values, *, label):
        if values is None:
            return mo.md(f"{label} is unavailable.")
        return mo.ui.table(
            [{"field": key, "value": json.dumps(value)} for key, value in values.items()],
            label=label,
            selection=None,
            pagination=False,
            show_column_summaries=False,
        )

    def live_tuning_payload(actual, previous_widgets, edited_widgets, fields):
        """Merge only post-Connect edits into actual settings, not widget defaults."""
        changed = {
            key: edited_widgets[key]
            for key in fields
            if edited_widgets[key] != previous_widgets[key]
        }
        if not changed:
            return None
        if not isinstance(actual, dict) or not all(key in actual for key in fields):
            raise ValueError("The current engine has not reported its live settings")
        return {key: changed.get(key, actual[key]) for key in fields}

    def confirmed_control_values(session, defaults):
        """An enabled control is initialized only from confirmed engine data."""
        ready = (
            session is not None
            and isinstance(session.get("actual_ship"), dict)
            and isinstance(session.get("actual_flow"), dict)
        )
        if ready:
            return dict(session["actual_ship"]), dict(session["actual_flow"]), False
        ship = {**defaults["ship"], "thrust_scale": 1.0}
        flow = {
            key: defaults["debris"][key]
            for key in ("speed_mps", "speed_spread_mps", "stream_axis", "stream_sign")
        }
        return ship, flow, True

    def directed_flow_available(session):
        setup = session.get("actual_setup") if session is not None else None
        distribution = setup.get("debris_distribution") if isinstance(setup, dict) else None
        return isinstance(distribution, dict) and distribution.get("kind") == "directed_stream"

    def placement_seed(snapshot):
        seed = {
            "position_m": None,
            "source_time_s": None,
            "setup": observed_setup(snapshot.get("observation", {})),
        }
        physics = snapshot.get("observation", {}).get("applied_physics")
        if not isinstance(physics, dict):
            return seed
        try:
            position = validate_position(physics["position_world_m"])
        except (KeyError, TypeError, ValueError):
            return seed
        return {**seed, "position_m": position, "source_time_s": physics.get("source_time_s")}

    def confirmed_tuning_session(session, snapshot, *, family, requested=None, error=None):
        """Store actual receipts, never a requested value masquerading as readback."""
        observation = snapshot["observation"]
        updated = dict(session)
        for name, field, keys in (
            ("ship", "ship_tuning", ("mass_kg", "inertia_scale", "thrust_scale")),
            (
                "flow",
                "debris_flow",
                ("speed_mps", "speed_spread_mps", "stream_axis", "stream_sign"),
            ),
        ):
            actual = observation.get(field)
            if not isinstance(actual, dict) or not all(key in actual for key in keys):
                raise ValueError("The engine has not reported all live tuning fields")
            values = {key: actual[key] for key in keys}
            updated[f"actual_{name}"] = values
            updated[f"{name}_widgets"] = dict(values)
        actual = updated.get(f"actual_{family}")
        state = "confirmed" if requested is None or actual == requested else "diverged"
        updated.update(
            tuning_state=state,
            tuning_family=family,
            tuning_source_time_s=observation.get("time_s"),
            tuning_source_seed=observation.get("seed"),
            actual_setup=observed_setup(observation),
            tuning_revision=observation.get("tuning_revision"),
            tuning_error=error,
        )
        return updated

    def action_error_text(action, error):
        # Transport/callback text may contain private connection information.
        if isinstance(error, FlightStateError):
            return str(error)
        safe_validation = isinstance(error, ValueError) and str(error).startswith(
            (
                "duration_s must",
                "cadence_hz must",
                "mass_kg must",
                "inertia_scale must",
                "thrust_scale must",
                "speed_mps must",
                "speed_spread_mps must",
                "Ship tuning requires",
                "Flow tuning requires",
                "Flow direction needs",
                "The current engine",
                "The engine has not",
                "Experiment ",
                "Debris ",
                "Impact ejecta",
                "source_direction",
                "kick_velocity_mps",
                "tangential_speed_mps",
                "patch_half_angle_deg",
                "position_m",
                "Flight preset",
            )
        )
        detail = (
            str(error)[:320]
            if isinstance(error, LocalApiError) or safe_validation
            else type(error).__name__
        )
        return (
            f"{action} failed: {detail}. Last confirmed configuration retained. "
            "No uncertain command is retried automatically."
        )

    def action_error(action, error):
        return (
            mo.md(action_error_text(action, error))
            .callout(kind="danger")
            .style(min_width="0", max_width="100%", overflow_wrap="anywhere", line_height="1.5")
        )

    return (
        action_error,
        action_error_text,
        confirmed_control_values,
        confirmed_tuning_session,
        directed_flow_available,
        field_table,
        lab_slider,
        live_tuning_payload,
        placement_seed,
    )


@app.cell
def _(ExperimentRunner, mo):
    experiment = ExperimentRunner()
    get_session, set_session = mo.state(None)
    get_camera_report, set_camera_report = mo.state(None)
    get_placement_seed, set_placement_seed = mo.state(None)

    def get_applied_config():
        return experiment.status().get("accepted_config")

    return (
        experiment,
        get_applied_config,
        get_camera_report,
        get_placement_seed,
        get_session,
        set_camera_report,
        set_placement_seed,
        set_session,
    )


@app.cell
def _(experiment, get_session):
    def stopped_sim():
        session = get_session()
        if session is None:
            raise RuntimeError("Connect first")
        stopped = experiment.stop(timeout_s=10)
        if stopped.get("worker_active") or stopped["state"] in {"starting", "running", "stopping"}:
            raise RuntimeError("Worker cleanup is pending; no second flight writer was started")
        return session["client"]

    return (stopped_sim,)


@app.cell
def _(inspect, suppress):
    def policy_sources(predict, search, decide):
        sources = {}
        for name, function in (
            ("predict_scene", predict),
            ("find_path", search),
            ("decide_command", decide),
        ):
            with suppress(OSError, TypeError):
                sources[f"{name}, inspectable current function"] = inspect.getsource(function)
        return sources

    return (policy_sources,)


@app.cell
def _(CHECKPOINT_RUN_LIMIT_S, MAX_CONTROLLER_RUN_S, lab_slider, mo):
    connection_file = mo.ui.text(
        value=".devenv/wsorb-local.json",
        label="Private connection file",
        full_width=True,
    )
    connect_refresh = mo.ui.run_button(label="Connect / Refresh")
    controller_mode = mo.ui.dropdown(
        options={
            "Resilient route with radar failover": "resilient_waypoint_pd",
            "Planned waypoint PD + public radar": "planned_waypoint_pd",
            "Basic PD + public radar": "basic_pd_radar",
            "Coast, zero command": "coast",
        },
        value="Resilient route with radar failover",
        allow_select_none=False,
        label="Python controller",
        full_width=True,
    )
    run_controller = mo.ui.run_button(label="Run controller", kind="warn")
    stop_run = mo.ui.run_button(label="Stop Python control", kind="danger")
    duration_s = lab_slider(
        0.1, MAX_CONTROLLER_RUN_S, 0.1, CHECKPOINT_RUN_LIMIT_S, "Flight limit, simulation s"
    )
    cadence_hz = lab_slider(0.1, 10, 0.1, 8, "Requested controller cadence, Hz")
    experiment_refresh = mo.ui.refresh(
        options=["1s", "2s", "5s"],
        default_interval="1s",
        label="Notebook display refresh, read-only",
    )
    return (
        cadence_hz,
        connect_refresh,
        connection_file,
        controller_mode,
        duration_s,
        experiment_refresh,
        run_controller,
        stop_run,
    )


@app.cell
def _(mo):
    def lab_stack(items):
        return mo.vstack(items, align="stretch", gap=1).style(
            min_width="0", max_width="100%", width="100%"
        )

    lab_styles = mo.Html("""
    <style>
    .wsorb-lab {
        min-inline-size: 0;
        inline-size: 100%;
        max-inline-size: 100%;
    }
    .wsorb-lab :where(marimo-ui-element, marimo-tabs, [role="tabpanel"],
        [data-kind="tab"], .mo-label, [part="label"]) {
        min-inline-size: 0;
        max-inline-size: 100%;
    }
    .wsorb-lab marimo-tabs [role="tablist"] {
        display: flex;
        flex-wrap: wrap;
        justify-content: flex-start;
        gap: 0.25rem;
        inline-size: 100%;
        block-size: auto;
        max-block-size: none;
        overflow: visible;
    }
    .wsorb-lab marimo-tabs [role="tab"] {
        flex: 0 0 auto;
        min-block-size: 2.5rem;
        white-space: nowrap;
    }
    .wsorb-lab :where(marimo-number, marimo-slider, marimo-dropdown, marimo-text) {
        display: block;
        min-inline-size: 0;
        inline-size: 100%;
        max-inline-size: 100%;
    }
    .wsorb-lab :where(p, li, label, [part="label"]) {
        max-inline-size: 72ch;
        line-height: 1.5;
        overflow-wrap: anywhere;
    }
    .wsorb-lab :where(p code, li code, pre) {
        white-space: pre-wrap;
        overflow-wrap: anywhere;
    }
    .wsorb-lab marimo-button button {
        max-inline-size: 100%;
        min-block-size: 2.5rem;
        white-space: normal;
    }
    .wsorb-lab marimo-accordion button {
        min-inline-size: 0;
        max-inline-size: 100%;
        white-space: normal;
        text-align: start;
        line-height: 1.5;
    }
    .wsorb-lab :where(marimo-number, marimo-slider, marimo-text) input {
        min-inline-size: 0;
        max-inline-size: 100%;
        font-size: 1rem;
        font-variant-numeric: tabular-nums;
    }
    .wsorb-lab .marimo-json-output :where(span, code) {
        white-space: normal;
        overflow-wrap: anywhere;
    }
    .wsorb-lab .marimo-json-output .w-rjv-line {
        flex-wrap: wrap;
        min-inline-size: 0;
    }
    </style>
    """)
    return lab_stack, lab_styles


@app.cell
def _(
    confirmed_control_values,
    default_world_settings,
    directed_flow_available,
    get_session,
    lab_slider,
    mo,
):
    _ship, _flow, _disabled = confirmed_control_values(get_session(), default_world_settings)
    _flow_disabled = _disabled or not directed_flow_available(get_session())
    ship_controls = mo.ui.dictionary(
        {
            "mass_kg": lab_slider(
                100, 100000, 0.1, _ship["mass_kg"], "Mass, kg", disabled=_disabled
            ),
            "inertia_scale": lab_slider(
                0.1, 10, 0.1, _ship["inertia_scale"], "Inertia multiplier", disabled=_disabled
            ),
            "thrust_scale": lab_slider(
                0.1, 10, 0.1, _ship["thrust_scale"], "Thrust multiplier", disabled=_disabled
            ),
        }
    )
    flow_controls = mo.ui.dictionary(
        {
            "speed_mps": lab_slider(
                0.1, 200, 0.1, _flow["speed_mps"], "Stream speed, m/s", disabled=_flow_disabled
            ),
            "speed_spread_mps": lab_slider(
                0, 100, 0.1, _flow["speed_spread_mps"], "Birth spread, m/s", disabled=_flow_disabled
            ),
            "stream_axis": mo.ui.dropdown(
                options=["x", "y", "z"],
                value=_flow["stream_axis"],
                allow_select_none=False,
                label="Stream axis, observation frame",
                full_width=True,
                disabled=_flow_disabled,
            ),
            "stream_sign": mo.ui.dropdown(
                options={"Negative direction": -1, "Positive direction": 1},
                value="Negative direction" if _flow["stream_sign"] == -1 else "Positive direction",
                allow_select_none=False,
                label="Stream direction",
                full_width=True,
                disabled=_flow_disabled,
            ),
        }
    )
    return flow_controls, ship_controls


@app.cell
def _(ResilienceConfig, lab_slider, mo):
    _forecast_s = ResilienceConfig().planner.forecast_horizon_s
    planner_controls = mo.ui.dictionary(
        {
            "cell_size_m": lab_slider(5, 200, 5, 40, "A* grid cell size, m"),
            "clearance_m": lab_slider(0, 100, 1, 8, "Extra obstacle clearance, m"),
            "forecast_horizon_s": lab_slider(
                0.1, 15, 0.1, _forecast_s, "Forecast horizon, simulation s"
            ),
            "max_measurement_age_s": lab_slider(0, 30, 0.1, 5.4, "Maximum radar age, s"),
            "unknown_tangential_speed_mps": lab_slider(0, 100, 1, 12, "Sideways allowance, m/s"),
            "sigma_multiplier": lab_slider(0, 8, 0.1, 3, "Sensor uncertainty multiplier"),
        }
    )
    gain_controls = mo.ui.dictionary(
        {
            "position_gain_s2": lab_slider(0, 0.02, 0.0001, 0.0012, "Position gain, s⁻²"),
            "velocity_gain_s": lab_slider(0, 1, 0.005, 0.07, "Velocity damping, s⁻¹"),
            "avoidance_range_m": lab_slider(1, 500, 1, 70, "Radar avoidance range, m"),
            "avoidance_accel_mps2": lab_slider(0, 2, 0.05, 0.7, "Avoidance request, m/s²"),
            "waypoint_arrival_m": lab_slider(0.1, 200, 0.1, 20, "Waypoint arrival distance, m"),
        }
    )
    return gain_controls, planner_controls


@app.cell
def _(PlannerConfig, gain_controls, planner_controls):
    planner_settings = PlannerConfig(
        **planner_controls.value,
        corridor_half_width_m=800.0,
        max_nodes=50_000,
        max_expansions=12_000,
    )
    controller_gains = dict(gain_controls.value)
    return controller_gains, planner_settings


@app.cell
def _(predict_obstacles):
    def predict_scene(observation, settings):
        """Forecast from public radar and original timestamps. Edit this function."""
        # A tracker/fusion model belongs here, never hidden debris or debug ray truth.
        return predict_obstacles(observation, settings)

    return (predict_scene,)


@app.cell
def _(plan_route):
    def find_path(frame, settings):
        """Run the team's real bounded Python A*. Edit this function to change search."""
        return plan_route(frame, settings)

    return (find_path,)


@app.cell
def _(basic_command, next_waypoint, np):
    def decide_command(observation, route, mode, gains):
        """Select body-frame SI commands in Python. Calling this sends nothing."""
        if mode == "coast":
            return np.zeros(3), np.zeros(3)
        goal_world_m = None
        if mode == "planned_waypoint_pd":
            if route is None:
                raise ValueError("Route mode requires a current Python plan")
            if route.status == "at_waypoint":
                return np.zeros(3), np.zeros(3)
            if route.status != "candidate":
                raise ValueError("No candidate route; no new flight command")
            # Keep this rule editable and shared with the displayed next waypoint.
            goal_world_m = next_waypoint(observation, route, gains["waypoint_arrival_m"])
            if goal_world_m is None:
                return np.zeros(3), np.zeros(3)
        elif mode != "basic_pd_radar":
            raise ValueError("Unknown controller mode")
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
def _(
    LivePolicy,
    ResilienceConfig,
    asdict,
    controller_gains,
    controller_mode,
    decide_command,
    find_path,
    planner_settings,
    policy_sources,
    predict_scene,
    source_manifest,
):
    live_policy = LivePolicy(
        predict_scene,
        find_path,
        decide_command,
        planner_settings,
        controller=controller_mode.value,
        gains=controller_gains,
        resilience=ResilienceConfig(planner=planner_settings)
        if controller_mode.value == "resilient_waypoint_pd"
        else None,
        config={
            "gains": dict(controller_gains),
            "planner": asdict(planner_settings),
            "replan_interval_sim_s": 0.5,
            "resilience": asdict(ResilienceConfig(planner=planner_settings))
            if controller_mode.value == "resilient_waypoint_pd"
            else None,
            "python_functions": source_manifest(
                policy_sources(predict_scene, find_path, decide_command)
            ),
            "source_note": "Current inspectable functions; dependencies are not standalone",
        },
    )
    return (live_policy,)


@app.cell
def _(
    LocalSimulation,
    action_error,
    confirmed_tuning_session,
    connect_refresh,
    connection_file,
    experiment,
    get_session,
    live_policy,
    mo,
    placement_seed,
    set_placement_seed,
    set_session,
):
    mo.stop(not connect_refresh.value)
    try:
        _session = get_session()
        _status = experiment.status()
        if _status.get("worker_active"):
            if _session is None or _session["path"] != connection_file.value:
                raise RuntimeError("Stop before choosing another connection")
            _sim = _session["client"]
        else:
            _sim = LocalSimulation.from_file(connection_file.value)
        _backend_status = _sim.status()
        with _sim.control_transaction():
            _snapshot = _sim.snapshot()
            experiment.record_snapshot(_snapshot, source="connect_refresh")
        if not _status.get("worker_active"):
            experiment.watch(_sim, live_policy, poll_hz=10, replan_s=0.5)
        # Seed controls from the confirmed game values, not unbound defaults.
        set_session(
            confirmed_tuning_session(
                {"client": _sim, "path": connection_file.value},
                _snapshot,
                family="connection",
            )
        )
        set_placement_seed(placement_seed(_snapshot))
        mo.output.replace(
            mo.md(
                f"[Open native headless server help]({_sim.url}). "
                "Actual physics snapshot received; watch requested."
                if _backend_status.get("backend") == "native_headless"
                else f"[Open the actual local game]({_sim.url}). "
                "Snapshot received; watch requested."
            )
        )
    except (RuntimeError, ValueError) as _error:
        mo.output.replace(action_error("Connect", _error))
    return


@app.cell
def _(action_error, experiment, get_session, live_policy, mo):
    # Re-evaluates after edits to any actual function, planner setting or gain.
    # No reconnect, play, pause or flight command occurs in this cell.
    mo.stop(get_session() is None)
    if experiment.status().get("worker_active"):
        try:
            _requested_revision = experiment.update_policy(live_policy)
        except (RuntimeError, ValueError, TypeError) as _error:
            mo.output.replace(action_error("Policy update", _error))
    return


@app.cell
def _(action_error, cadence_hz, duration_s, experiment, get_session, mo):
    # This is a data update on the existing worker, not Run consent.
    mo.stop(get_session() is None)
    if experiment.status().get("worker_active"):
        try:
            _requested_limits_revision = experiment.update_run_limits(
                duration_s=duration_s.value, cadence_hz=cadence_hz.value
            )
        except (RuntimeError, ValueError, TypeError) as _error:
            mo.output.replace(action_error("Run-limit update", _error))
    return


@app.cell
def _(
    action_error,
    action_error_text,
    confirmed_tuning_session,
    experiment,
    get_session,
    live_tuning_payload,
    mo,
    set_session,
    ship_controls,
    suppress,
):
    _session = get_session()
    mo.stop(_session is None)
    _edited = dict(ship_controls.value)
    mo.stop(_edited == _session["ship_widgets"])
    _sim, _before, _payload = _session["client"], None, None
    _phase = "reading current calibration"
    try:
        with (
            mo.status.spinner(title="Applying ship calibration; flight is not restarted"),
            _sim.control_transaction(),
        ):
            _before = _sim.snapshot()
            _payload = live_tuning_payload(
                _before["observation"].get("ship_tuning"),
                _session["ship_widgets"],
                _edited,
                ("mass_kg", "inertia_scale", "thrust_scale"),
            )
            _phase = "applying and verifying ship calibration"
            _after = _sim.tune_ship(_payload)
            experiment.record_snapshot(_after, source="ship_live_tune")
        set_session(confirmed_tuning_session(_session, _after, family="ship", requested=_payload))
        mo.output.replace(
            mo.md(
                f"Ship calibration confirmed at tuning revision "
                f"{_after['observation']['tuning_revision']}."
            )
        )
    except (RuntimeError, ValueError) as _error:
        # Restore the last confirmed values. Never silently present a failed edit
        # as applied, or retry an uncertain mutation after unrelated UI changes.
        _message = action_error_text(f"Ship tuning / {_phase}", _error)
        _updated = {
            **_session,
            "tuning_state": "rejected" if isinstance(_error, ValueError) else "unconfirmed",
            "tuning_family": "ship",
            "tuning_error": _message,
        }
        if _payload is not None and not isinstance(_error, ValueError):
            # Reconcile once with a real read, never another tuning command.
            with suppress(RuntimeError, ValueError):
                with _sim.control_transaction():
                    _readback = _sim.snapshot()
                    experiment.record_snapshot(_readback, source="ship_tune_readback")
                _updated = confirmed_tuning_session(
                    _updated, _readback, family="ship", requested=_payload, error=_message
                )
        set_session(_updated)
        mo.output.replace(action_error(f"Ship tuning / {_phase}", _error))
    return


@app.cell
def _(
    action_error,
    action_error_text,
    confirmed_tuning_session,
    directed_flow_available,
    experiment,
    flow_controls,
    get_session,
    live_tuning_payload,
    mo,
    set_session,
    suppress,
):
    _session = get_session()
    mo.stop(_session is None)
    mo.stop(not directed_flow_available(_session))
    _edited = dict(flow_controls.value)
    mo.stop(_edited == _session["flow_widgets"])
    _sim, _payload = _session["client"], None
    _phase = "reading current flow"
    try:
        with (
            mo.status.spinner(title="Applying debris flow without restarting flight"),
            _sim.control_transaction(),
        ):
            _before = _sim.snapshot()
            _payload = live_tuning_payload(
                _before["observation"].get("debris_flow"),
                _session["flow_widgets"],
                _edited,
                ("speed_mps", "speed_spread_mps", "stream_axis", "stream_sign"),
            )
            _phase = "applying and verifying debris flow"
            _after = _sim.tune_debris_flow(_payload)
            experiment.record_snapshot(_after, source="flow_live_tune")
        set_session(confirmed_tuning_session(_session, _after, family="flow", requested=_payload))
        mo.output.replace(
            mo.md(
                f"Flow configuration confirmed at tuning revision "
                f"{_after['observation']['tuning_revision']}."
            )
        )
    except (RuntimeError, ValueError) as _error:
        _message = action_error_text(f"Flow tuning / {_phase}", _error)
        _updated = {
            **_session,
            "tuning_state": "rejected" if isinstance(_error, ValueError) else "unconfirmed",
            "tuning_family": "flow",
            "tuning_error": _message,
        }
        if _payload is not None and not isinstance(_error, ValueError):
            with suppress(RuntimeError, ValueError):
                with _sim.control_transaction():
                    _readback = _sim.snapshot()
                    experiment.record_snapshot(_readback, source="flow_tune_readback")
                _updated = confirmed_tuning_session(
                    _updated, _readback, family="flow", requested=_payload, error=_message
                )
        set_session(_updated)
        mo.output.replace(action_error(f"Flow tuning / {_phase}", _error))
    return


@app.cell
def _(
    FlightStateError,
    action_error,
    cadence_hz,
    duration_s,
    experiment,
    get_session,
    live_policy,
    mo,
    run_controller,
):
    mo.stop(not run_controller.value)
    try:
        _session = get_session()
        if _session is None:
            raise FlightStateError("Connect / Refresh before Run controller.")
        with _session["client"].control_transaction():
            _current = _session["client"].snapshot()
            experiment.record_snapshot(_current, source="run_request_readback")
        experiment.update_policy(live_policy)
        experiment.run_controller(duration_s=duration_s.value, cadence_hz=cadence_hz.value)
        mo.output.replace(
            mo.md("Run request queued. Live status reports acceptance, commands or rejection.")
        )
    except (RuntimeError, ValueError, TypeError) as _error:
        mo.output.replace(action_error("Run controller", _error))
    return


@app.cell
def _(action_error, experiment, mo, stop_run):
    mo.stop(not stop_run.value)
    try:
        _stopped = experiment.stop(timeout_s=10)
        if _stopped.get("worker_active") or _stopped["state"] == "stopping":
            mo.output.replace(mo.md("Stop cleanup is pending. No second writer was started."))
        elif _stopped["error"]:
            mo.output.replace(
                mo.md(
                    _stopped["error"] + " Inspect actual flight and watchdog state in the game."
                ).callout(kind="danger")
            )
        else:
            mo.output.replace(
                mo.md(
                    "Python control stopped. Connect / Refresh to resume read-only guidance. "
                    + (_stopped.get("notice") or "")
                )
            )
    except (RuntimeError, ValueError) as _error:
        mo.output.replace(action_error("Stop Python control", _error))
    return


@app.cell
def _(get_session, lab_slider, mo):
    _disabled = get_session() is None
    sensor_fault_target = mo.ui.dropdown(
        options={
            "Primary radar": "radar_primary",
            "Backup radar": "radar_backup",
            "Top camera": "camera_top",
            "Bottom camera": "camera_bottom",
            "Shared radar bus": "radar_bus",
            "Navigation telemetry": "navigation",
            "All avionics": "all_avionics",
        },
        value="Primary radar",
        allow_select_none=False,
        label="Failure domain",
        full_width=True,
        disabled=_disabled,
    )
    sensor_fault_mode = mo.ui.dropdown(
        options={"Unavailable": "unavailable", "Frozen delivery": "frozen", "Nominal": "nominal"},
        value="Unavailable",
        allow_select_none=False,
        label="Delivery failure",
        full_width=True,
        disabled=_disabled,
    )
    sensor_fault_duration = lab_slider(
        0.1, 300, 0.1, 5, "Failure duration, simulation s", disabled=_disabled
    )
    apply_sensor_fault = mo.ui.run_button(
        label="Apply sensor failure", kind="warn", disabled=_disabled
    )
    clear_sensor_faults = mo.ui.run_button(label="Restore all sensor delivery", disabled=_disabled)
    pause_physics = mo.ui.run_button(label="Pause physics", kind="warn", disabled=_disabled)
    primary_outage_duration = lab_slider(
        0.1, 300, 0.1, 5, "Primary decision outage, simulation s", disabled=_disabled
    )
    interrupt_primary = mo.ui.run_button(
        label="Interrupt primary decisions", kind="warn", disabled=_disabled
    )
    return (
        apply_sensor_fault,
        clear_sensor_faults,
        pause_physics,
        primary_outage_duration,
        interrupt_primary,
        sensor_fault_duration,
        sensor_fault_mode,
        sensor_fault_target,
    )


@app.cell
def _(action_error, experiment, interrupt_primary, mo, primary_outage_duration):
    mo.stop(not interrupt_primary.value)
    try:
        experiment.inject_primary_outage(duration_sim_s=primary_outage_duration.value)
        mo.output.replace(
            mo.md(
                "Primary decisions and renewals interrupted. Native ownership determines "
                "whether the standby takes over after expiry; no leader result is assumed."
            )
        )
    except (RuntimeError, ValueError) as _error:
        mo.output.replace(action_error("Interrupt primary decisions", _error))
    return


@app.cell
def _(action_error, experiment, get_session, mo, pause_physics):
    mo.stop(not pause_physics.value)
    try:
        _session = get_session()
        if _session is None:
            raise RuntimeError("Connect / Refresh before pausing physics.")
        _sim = _session["client"]
        _sim.pause()
        experiment.record_snapshot(_sim.snapshot(), source="explicit_pause")
        mo.output.replace(
            mo.md("Actual physics paused. Run controller is required to grant control again.")
        )
    except (RuntimeError, ValueError) as _error:
        mo.output.replace(action_error("Pause physics", _error))
    return


@app.cell
def _(
    action_error,
    apply_sensor_fault,
    clear_sensor_faults,
    experiment,
    get_session,
    mo,
    sensor_fault_duration,
    sensor_fault_mode,
    sensor_fault_target,
):
    mo.stop(not (apply_sensor_fault.value or clear_sensor_faults.value))
    try:
        _session = get_session()
        if _session is None:
            raise RuntimeError("Connect / Refresh before applying a sensor failure.")
        _sim = _session["client"]
        if clear_sensor_faults.value:
            _snapshot = _sim.clear_sensor_faults()
        else:
            _snapshot = _sim.set_sensor_fault(
                sensor_fault_target.value,
                sensor_fault_mode.value,
                duration_sim_s=sensor_fault_duration.value,
            )
        experiment.record_snapshot(_snapshot, source="sensor_fault_control")
        mo.output.replace(
            mo.md(
                "Native sensor delivery state received. Recovery waits for genuine new samples. "
                "The world and controller were not reset."
            )
        )
    except (RuntimeError, ValueError) as _error:
        mo.output.replace(action_error("Sensor failure control", _error))
    return


@app.cell
def _(experiment, experiment_refresh, mo):
    _ = experiment_refresh.value
    _status = experiment.status()
    _latest = _status.get("latest") or {}
    _observation = _latest.get("observation") or {}
    _health = _observation.get("sensor_health")
    _guidance = _status.get("resilience")
    resilience_status_view = mo.vstack(
        [
            mo.md(
                f"Python guidance: **{_guidance.get('mode', 'unavailable')}**. "
                f"{_guidance.get('reason', '')}"
            )
            if _guidance is not None
            else mo.md(
                "Connect with the resilient route controller to inspect estimates and failover."
            ),
            mo.json(_health, label="Native sensor delivery and shared failure domains")
            if _health is not None
            else mo.md("Sensor health is unavailable. Build the current engine and reconnect."),
            mo.json(_guidance, label="Python state estimate, uncertainty and route progress")
            if _guidance is not None
            else mo.md("No Python estimate has been produced."),
            mo.json(_observation.get("command_watchdog"), label="Native command watchdog")
            if _observation.get("command_watchdog") is not None
            else mo.md("No native command-watchdog report is available."),
            mo.json(
                _observation.get("control_authority"), label="Actual native controller ownership"
            )
            if _observation.get("control_authority") is not None
            else mo.md("No native controller-ownership report is available."),
            mo.json(_status.get("standby"), label="Separate-process standby status")
            if _status.get("standby") is not None
            else mo.md("No standby process report received."),
        ]
    )
    return (resilience_status_view,)


@app.cell
def _(mo):
    get_step_guidance, set_step_guidance = mo.state(None)
    return get_step_guidance, set_step_guidance


@app.cell
def _(
    apply_camera_aim,
    cadence_hz,
    capture_images,
    confirmed_tuning_session,
    connect_refresh,
    duration_s,
    experiment,
    experiment_refresh,
    field_table,
    flight_context,
    get_session,
    live_policy,
    mo,
    observed_setup,
    place_lander,
    pause_physics,
    prepare_export,
    regenerate_world,
    reset_run,
    run_controller,
    run_policy_step,
    set_session,
    stop_run,
    suppress,
    upright,
):
    _tick = experiment_refresh.value
    _status = experiment.status()
    _snapshot = _status["latest"]
    _session = get_session()
    if _session is None:
        live_status_view = mo.md("Disconnected. Connect to start read-only Python guidance.")
        ship_readback_view = mo.md("Connect in Flight to read the ship and enable live edits.")
        debris_readback_view = mo.md("Connect in Flight to read the debris mode and live flow.")
        launch_readback_view = mo.md("Connect in Flight to read the current launch setup.")
        policy_status_view = mo.md("No Python policy accepted yet. Connect in Flight to watch.")
        flight_diagnostics_view = mo.md("No flight readback yet.")
    else:
        _observation = _snapshot.get("observation", {}) if _snapshot else {}
        _source_time = _observation.get("time_s")
        _previous_time = _session.get("tuning_source_time_s")
        _action_busy = any(
            button.value
            for button in (
                connect_refresh,
                run_controller,
                stop_run,
                reset_run,
                regenerate_world,
                upright,
                run_policy_step,
                apply_camera_aim,
                capture_images,
                prepare_export,
                place_lander,
                pause_physics,
            )
        )
        _calibration_changed = (
            _observation.get("ship_tuning") != _session.get("actual_ship")
            or _observation.get("debris_flow") != _session.get("actual_flow")
            or _observation.get("seed") != _session.get("tuning_source_seed")
            or observed_setup(_observation) != _session.get("actual_setup")
            or (
                type(_source_time) in (int, float)
                and type(_previous_time) in (int, float)
                and _source_time < _previous_time
            )
        )
        if _snapshot is not None and _calibration_changed and not _action_busy:
            # Reconcile already-received public data after a native reset/tune.
            # No new RPC, mutation, worker or synthesized measurement is involved.
            with suppress(KeyError, TypeError, ValueError):
                set_session(confirmed_tuning_session(_session, _snapshot, family="observed"))
        _requested = _status.get("requested_policy_revision")
        _accepted = _status.get("accepted_policy_revision")
        _current_config = {**live_policy.config, "controller": live_policy.controller}
        _policy_pending = (
            _requested != _accepted or _status.get("accepted_config") != _current_config
        )
        _pending = " · Python edits not yet accepted" if _policy_pending else ""
        _ship = _observation.get("ship_tuning") or {}
        _mission = _observation.get("mission") or {}
        _flow = _observation.get("debris_flow") or {}
        _context = flight_context(_snapshot)
        _physics = _context["applied_physics"]
        _application = _physics.get("last_actuator_application") if _physics is not None else None
        _application_views = []
        if _physics is None:
            _body_summary = mo.md(
                "Rapier body readback unavailable; calibration is not a substitute."
            )
        else:
            _body_summary = mo.md(
                f"Measured Rapier mass {_physics.get('mass_kg', 'unavailable')} kg · "
                f"actuation {_physics.get('actuation_state', 'unavailable')} · "
                f"body source {_physics.get('source_time_s', 'unavailable')} s / "
                f"revision {_physics.get('tuning_revision', 'unavailable')}"
            )
            if _application is None:
                _application_views.append(
                    mo.md(
                        "No actual actuator application record yet. Requested or stored wrench "
                        "is not shown as an applied measurement."
                    )
                )
            else:
                _application_views.append(
                    mo.md(
                        "Last actual actuator application "
                        f"{_application.get('application_time_s')} s / "
                        f"revision {_application.get('tuning_revision')} · "
                        f"gain {_application.get('actuator_gain')}× · "
                        f"body force {_application.get('force_body_n')} N · "
                        f"body torque {_application.get('torque_body_nm')} N m"
                    )
                )
            if _physics.get("actuation_state") == "awaiting_application":
                _application_views.append(
                    mo.md(
                        "The current request awaits application. The last application above "
                        "can belong to an earlier request; its time and revision have not advanced."
                    )
                )
        _limits = _status.get("run_limits")
        _limit_revision = _status.get("accepted_run_limits_revision")
        _current_limits = {
            "duration_s": duration_s.value,
            "requested_cadence_hz": cadence_hz.value,
        }
        _limit_pending = (
            _status.get("requested_run_limits_revision") != _limit_revision
            or _limits != _current_limits
        )
        _limits_scope = (
            f"from Run origin {_status.get('run_origin_time_s')} s"
            if _status.get("controller_running")
            else "for the next explicit Run"
        )
        _limits_text = (
            f"Accepted run limits {_limit_revision}: {_limits['duration_s']:g} simulation s "
            f"{_limits_scope}, {_limits['requested_cadence_hz']:g} Hz"
            if _limits is not None
            else "Run limits awaiting worker acceptance"
        )
        if _limit_pending:
            _limits_text += " · timing edit pending"
        live_status_view = mo.vstack(
            [
                mo.md(
                    f"**{_status['state']}** · simulation "
                    f"{_observation.get('time_s', 'unavailable')} s"
                    f" · game {_context['playback']}"
                ),
                mo.md(
                    f"Mission {_context['mission_status'] or 'unavailable'} · "
                    f"phase {_context['mission_phase'] or 'unavailable'} · "
                    f"controller {_status.get('run_request_state', 'idle')}"
                ),
                field_table(
                    {
                        "Range to checkpoint, m": _mission.get("target_distance_m"),
                        "Arrival radius, m": (_observation.get("geometry") or {}).get(
                            "arrival_radius_m"
                        ),
                        "Relative speed, m/s": _mission.get("relative_speed_mps"),
                        "Damage estimate, J": _mission.get("damage"),
                        "Navigation source time, s": _observation.get("navigation_sample_time_s"),
                    },
                    label="Native checkpoint measurements, not route predictions",
                ),
                mo.md(
                    f"Received {_status['sample_count']} samples. "
                    "Open History for measured values and source clocks."
                ),
                mo.md(_context["terminal_notice"]).callout(kind="info")
                if _context["terminal_notice"]
                else mo.md(""),
                mo.md(
                    "Requested acceleration and angular acceleration are zero. "
                    "Tuning alone does not start thrust."
                )
                if _context["command_state"] == "zero"
                else mo.md(""),
                mo.md(
                    "Guidance is stopped. Connect / Refresh to watch "
                    "and accept policy/timing edits."
                )
                if not _status.get("worker_active")
                else mo.md(""),
                mo.md(_status["notice"]).callout(kind="info")
                if _status.get("notice") and _status["notice"] != _context["terminal_notice"]
                else mo.md(""),
                mo.md(_status["error"]).callout(kind="danger") if _status["error"] else mo.md(""),
                mo.md(_context["applied_physics_error"]).callout(kind="warn")
                if _context["applied_physics_error"]
                else mo.md(""),
                mo.md(_status["raycast_error"]).callout(kind="warn")
                if _status.get("raycast_error")
                else mo.md(""),
            ]
        )
        ship_readback_view = mo.vstack(
            [
                mo.md(
                    f"Confirmed calibration {_ship.get('mass_kg', 'unavailable')} kg · "
                    f"inertia {_ship.get('inertia_scale', 'unavailable')}× · "
                    f"thrust {_ship.get('thrust_scale', 'unavailable')}× · "
                    f"source {_observation.get('time_s', 'unavailable')} s · "
                    f"revision {_observation.get('tuning_revision', 'unavailable')}"
                ),
                _body_summary,
                mo.accordion({"Last actual actuator application": mo.vstack(_application_views)})
                if _application_views
                else mo.md(""),
            ]
        )
        _distribution_kind = (_context.get("debris_distribution") or {}).get("kind")
        _mode_label = {
            "directed_stream": "Directed stream",
            "impact_ejecta": "Impact ejecta",
        }.get(_distribution_kind, "Unavailable")
        _direction_label = {-1: "negative", 1: "positive"}.get(
            _flow.get("stream_sign"), "unavailable"
        )
        debris_readback_view = mo.vstack(
            [
                mo.md(f"Actual debris mode: **{_mode_label}**."),
                mo.md(
                    f"Confirmed stream speed {_flow.get('speed_mps', 'unavailable')} m/s · "
                    f"birth spread {_flow.get('speed_spread_mps', 'unavailable')} m/s · "
                    f"{_direction_label} {str(_flow.get('stream_axis', '?')).upper()} · "
                    f"source {_observation.get('time_s', 'unavailable')} s · "
                    f"revision {_observation.get('tuning_revision', 'unavailable')}"
                )
                if _distribution_kind == "directed_stream"
                else mo.md(
                    "Live stream edits are unavailable in this world. New-world drafts below."
                ),
            ]
        )
        _site_label = (
            "Custom launch site recorded"
            if _observation.get("launch_site_direction") is not None
            else "Preset launch site"
            if "launch_site_direction" in _observation
            else "Launch-site readback unavailable"
        )
        launch_readback_view = mo.md(
            f"Target {_mission.get('target_region', 'unavailable')} · "
            f"trial {_observation.get('trial_revision', 'unavailable')}. {_site_label}."
        )
        policy_status_view = mo.vstack(
            [
                mo.md(f"Accepted Python policy {_accepted}{_pending}."),
                mo.md(_limits_text),
            ]
        )
        flight_diagnostics_view = mo.md(
            f"Observation source {_observation.get('time_s', 'unavailable')} s · "
            f"tuning {_observation.get('tuning_revision', 'unavailable')} · "
            f"trial {_observation.get('trial_revision', 'unavailable')} · "
            f"accepted Python policy {_accepted}{_pending}"
        )
    if _snapshot is None:
        raw_snapshot_view = mo.md("No public snapshot received.")
    else:
        raw_snapshot_view = mo.accordion(
            {
                "Confirmed calibration, not a rigid-body measurement": field_table(
                    {
                        key: _snapshot["observation"].get(key)
                        for key in (
                            "ship_tuning",
                            "debris_flow",
                            "tuning_revision",
                            "experiment_settings",
                        )
                    },
                    label="Verified calibration and unchanged baseline reset settings",
                ),
                "Requested commands, stored actuator report and Rapier getter": mo.json(
                    flight_context(_snapshot)
                ),
                "Actual Rapier body getter, with its own source clock": field_table(
                    _snapshot["observation"].get("applied_physics"),
                    label="Rapier getter report, unavailable fields stay unavailable",
                ),
                "Physics readback scope": mo.md(
                    "Total external world force includes gravity and rotating-frame terms, "
                    "not contact impulses. External torque is the Rapier accumulator readback. "
                    "The last actuator record is the force-accumulator "
                    "delta before gravity. It may be absent after startup/restore, or older than "
                    "the current request. Public shuttle thrust_n/torque_nm retain their "
                    "command-derived wrench_provenance and are not that application record."
                ),
                "Public observation and radar": mo.json(_snapshot["observation"]),
                "Telemetry with original source clock": mo.json(
                    {"telemetry": _snapshot.get("telemetry")}
                ),
                "Python path and native debug trace": mo.json(
                    {"navigation": _snapshot.get("navigation")}
                ),
                "Actual worker state and debug query result": mo.json(
                    {key: value for key, value in _status.items() if key != "latest"}
                ),
                "Runtime and loaded engine": mo.json(
                    {key: _snapshot.get(key) for key in ("runtime", "engine_build")}
                ),
                "Recorded launch-site direction, separate from the target preset": mo.json(
                    {
                        key: _snapshot["observation"].get(key)
                        for key in ("launch_site_direction", "launchpad", "time_s")
                    }
                ),
                "Accepted policy and observed tuning changes": mo.json(
                    experiment.configuration_changes()
                ),
            }
        )
    return (
        debris_readback_view,
        flight_diagnostics_view,
        launch_readback_view,
        live_status_view,
        policy_status_view,
        raw_snapshot_view,
        ship_readback_view,
    )


@app.cell
def _(mo):
    history_metric_labels = {
        "Speed, m/s": "speed_mps",
        "Radial climb, m/s": "radial_climb_mps",
        "Terrain-centre clearance, m": "terrain_center_clearance_m",
        "Target centre range, m": "target_center_range_m",
        "Target closing speed, m/s": "target_closing_mps",
        "Telemetry actuator acceleration, m/s²": "applied_thrust_accel_mps2",
        "Telemetry actuator force, N": "thrust_force_n",
        "Telemetry actuator torque, N m": "torque_nm",
        "Spin, rad/s": "angular_speed_rad_s",
        "Estimated fuel used, kg": "fuel_used_kg_estimate",
        "Estimated contact damage, J": "damage_j_estimate",
        "Rapier body mass, kg": "rapier_mass_kg",
        "Actual lander world X, m": "rapier_position_world_x_m",
        "Actual lander world Y, m": "rapier_position_world_y_m",
        "Actual lander world Z, m": "rapier_position_world_z_m",
        "Actual lander world velocity X, m/s": "rapier_velocity_world_x_mps",
        "Actual lander world velocity Y, m/s": "rapier_velocity_world_y_mps",
        "Actual lander world velocity Z, m/s": "rapier_velocity_world_z_mps",
        "Rapier body inertia xx, kg m²": "rapier_inertia_xx_kg_m2",
        "Rapier external force world x, N": "external_force_world_x_n",
        "Rapier external force world y, N": "external_force_world_y_n",
        "Rapier external force world z, N": "external_force_world_z_n",
        "Last applied actuator force body x, N": "actuator_force_body_x_n",
        "Last applied actuator force body y, N": "actuator_force_body_y_n",
        "Last applied actuator force body z, N": "actuator_force_body_z_n",
        "Last applied actuator torque body x, N m": "actuator_torque_body_x_nm",
        "Last applied actuator torque body y, N m": "actuator_torque_body_y_nm",
        "Last applied actuator torque body z, N m": "actuator_torque_body_z_nm",
        "Last actual propellant force, N": "actuator_propellant_force_n",
        "Public shuttle mass, kg": "ship_mass_kg",
        "Public shuttle inertia xx, kg m²": "ship_inertia_xx_kg_m2",
        "Reference command cap, m/s²": "reference_accel_limit_mps2",
        "Requested command x, body m/s²": "command_accel_body_x_mps2",
        "Requested command y, body m/s²": "command_accel_body_y_mps2",
        "Requested command z, body m/s²": "command_accel_body_z_mps2",
    }
    history_metric = mo.ui.dropdown(
        options=history_metric_labels,
        value="Speed, m/s",
        allow_select_none=False,
        label="Received measurement history",
        full_width=True,
    )
    return history_metric, history_metric_labels


@app.cell
def _(alt):
    def history_plot(rows, metric, label, clock):
        """Keep real source times. Do not join missing values or observed boundaries."""
        points = []
        gap = 0
        seen_applications = set()
        for row in rows:
            if (
                clock == "telemetry_time_s"
                and row.get("telemetry_held")
                and (
                    row.get("tuning_changed")
                    or row.get("configuration_changed")
                    or row.get("setup_changed")
                    or row.get("recorded_placement")
                )
            ):
                # An actual tuning read is useful for hardware plots, but its
                # held telemetry is not another new flight-metric measurement.
                gap += 1
                continue
            if row.get(metric) is None or row.get(clock) is None:
                gap += 1
                continue
            if clock == "actuator_application_time_s":
                application = (
                    row.get("observation_seed"),
                    row.get("telemetry_epoch"),
                    row[clock],
                    row.get("actuator_application_tuning_revision"),
                    row[metric],
                )
                if application in seen_applications:
                    gap += 1
                    continue
                seen_applications.add(application)
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
                    "tuning_revision:Q",
                    "trial_revision:Q",
                    "flight_preset:N",
                    "recorded_placement:N",
                    "accepted_policy_revision:Q",
                    "accepted_run_limits_revision:Q",
                    "applied_physics_time_s:Q",
                    "actuator_application_time_s:Q",
                    "actuator_application_tuning_revision:Q",
                    "actuation_state:N",
                    "external_force_provenance:N",
                    "telemetry_held:N",
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
    _tick = experiment_refresh.value
    _rows = experiment.history()
    _metric = history_metric.value
    _label = next(label for label, field in history_metric_labels.items() if field == _metric)
    if _metric.startswith(("rapier_", "external_force_", "external_torque_")):
        _clock = "applied_physics_time_s"
    elif _metric.startswith("actuator_"):
        _clock = "actuator_application_time_s"
    elif _metric.startswith(("ship_", "reference_", "command_")):
        _clock = "observation_time_s"
    else:
        _clock = "telemetry_time_s"
    flight_history_chart = None
    if any(row.get(_metric) is not None and row.get(_clock) is not None for row in _rows):
        flight_history_chart = mo.ui.altair_chart(
            history_plot(_rows, _metric, _label, _clock),
            chart_selection=False,
            legend_selection=False,
        )
    history_view = mo.vstack(
        [
            history_metric,
            flight_history_chart
            if flight_history_chart is not None
            else mo.md("No measured values yet. Connect to watch the current flight."),
            mo.accordion(
                {
                    "History clocks and gaps": mo.md(
                        "At most 2000 received rows. Telemetry and observation clocks "
                        "stay separate. "
                        "Tuning, accepted edits, resets and manual changes split lines. "
                        "Held telemetry is labelled, not interpolated. The Rapier getter keeps "
                        "its own clock, not a value rebuilt from calibration or stored state."
                    )
                }
            ),
            mo.accordion(
                {
                    "Exact received rows": mo.ui.table(
                        _rows, selection=None, page_size=12, show_column_summaries=False
                    )
                }
            )
            if _rows
            else mo.md(""),
        ]
    )
    return (history_view,)


@app.cell
def _(lab_slider, mo):
    camera_controls = mo.ui.dictionary(
        {
            "top_yaw": lab_slider(-89, 89, 1, 0, "Top yaw, degrees"),
            "top_pitch": lab_slider(-50, 0, 1, 0, "Top pitch, degrees"),
            "bottom_yaw": lab_slider(-89, 89, 1, 0, "Bottom yaw, degrees"),
            "bottom_pitch": lab_slider(0, 50, 1, 0, "Bottom pitch, degrees"),
        }
    )
    apply_camera_aim = mo.ui.run_button(label="Apply camera aim")
    capture_camera = mo.ui.dropdown(
        options={"Top camera": "top", "Bottom camera": "bottom", "Both cameras": "both"},
        value="Top camera",
        allow_select_none=False,
        label="Manual capture source",
        full_width=True,
    )
    capture_images = mo.ui.run_button(label="Capture ship cameras")
    return apply_camera_aim, camera_controls, capture_camera, capture_images


@app.cell
def _(camera_controls):
    camera_aim_draft = dict(camera_controls.value)
    return (camera_aim_draft,)


@app.cell
def _(
    action_error,
    apply_camera_aim,
    camera_aim_draft,
    get_session,
    mo,
    set_camera_report,
):
    mo.stop(not apply_camera_aim.value)
    try:
        _session = get_session()
        if _session is None:
            raise RuntimeError("Connect first")
        _sim = _session["client"]
        _sim.PanTiltCamera(
            _sim.topCamera, camera_aim_draft["top_yaw"], camera_aim_draft["top_pitch"]
        )
        _sim.PanTiltCamera(
            _sim.bottomCamera, camera_aim_draft["bottom_yaw"], camera_aim_draft["bottom_pitch"]
        )
        _report = _sim.camera_status()
        set_camera_report(_report)
        mo.output.replace(mo.json(_report, label="Actual manual camera aim"))
    except (RuntimeError, ValueError) as _error:
        mo.output.replace(action_error("Camera aim; one aim may already have applied", _error))
    return


@app.cell
def _(
    action_error,
    capture_camera,
    capture_images,
    get_session,
    mo,
    set_camera_report,
):
    mo.stop(not capture_images.value)
    _views = []
    try:
        _session = get_session()
        if _session is None:
            raise RuntimeError("Connect first")
        _sim = _session["client"]
        _report = _sim.camera_status()
        set_camera_report(_report)
        _views.append(mo.json(_report, label="Actual camera metadata at capture"))
        _cameras = ("top", "bottom") if capture_camera.value == "both" else (capture_camera.value,)
        for _camera in _cameras:
            _capture = _sim.capture_image(_camera)
            _views.extend(
                [
                    mo.json(_capture["metadata"], label=f"Actual {_camera} capture source"),
                    mo.image(_capture["rgb"], alt=f"Actual {_camera} ship-camera RGB capture"),
                ]
            )
    except (RuntimeError, ValueError) as _error:
        _views.append(action_error("Manual capture; no substitute image is shown", _error))
    mo.output.replace(mo.vstack(_views))
    return


@app.cell
def _(
    MAX_DEBRIS_COUNT,
    default_world_settings,
    get_placement_seed,
    impact_defaults,
    lab_slider,
    mo,
):
    _seed = get_placement_seed()
    _setup = _seed.get("setup", {}) if _seed is not None else {}
    _baseline = _setup.get("experiment_settings") or default_world_settings
    _debris = _baseline["debris"]
    _distribution = _setup.get("debris_distribution") or {"kind": "directed_stream"}
    _impact = _distribution.get("settings", impact_defaults)
    flight_preset = mo.ui.dropdown(
        options={
            "Mars-facing +X / L1 target": "mars_facing_l1",
            "Far-side -X / L2 target": "far_side_l2",
        },
        value="Mars-facing +X / L1 target"
        if _setup.get("flight_preset") == "mars_facing_l1"
        else "Far-side -X / L2 target",
        allow_select_none=False,
        label="Target preset for new world",
        full_width=True,
    )
    debris_mode = mo.ui.dropdown(
        options={"Directed stream": "directed_stream", "Impact ejecta": "impact_ejecta"},
        value="Impact ejecta" if _distribution["kind"] == "impact_ejecta" else "Directed stream",
        allow_select_none=False,
        label="Debris mode for new world",
        full_width=True,
    )
    structure_controls = mo.ui.dictionary(
        {
            "count": mo.ui.number(
                start=0,
                stop=MAX_DEBRIS_COUNT,
                step=1,
                value=_debris["count"],
                label="New-world debris bodies, 0 to 100000",
                full_width=True,
                debounce=True,
            ),
            "radius_min_m": lab_slider(
                0.05, 100, 0.05, _debris["radius_min_m"], "Minimum debris radius, m"
            ),
            "radius_max_m": lab_slider(
                0.05, 100, 0.05, _debris["radius_max_m"], "Maximum debris radius, m"
            ),
            "shell_inner_m": lab_slider(
                0, 49990, 10, _debris["shell_inner_m"], "Shell inner offset, m"
            ),
            "shell_outer_m": lab_slider(
                10, 50000, 10, _debris["shell_outer_m"], "Shell outer offset, m"
            ),
        }
    )
    impact_controls = mo.ui.dictionary(
        {
            **{
                f"source_{axis}": mo.ui.number(
                    value=_impact["source_direction"][index],
                    label=f"Source direction {axis.upper()}, unitless",
                    full_width=True,
                    debounce=True,
                )
                for index, axis in enumerate("xyz")
            },
            **{
                f"kick_{axis}": mo.ui.number(
                    start=-200,
                    stop=200,
                    value=_impact["kick_velocity_mps"][index],
                    label=f"Initial kick {axis.upper()}, rotating-world m/s",
                    full_width=True,
                    debounce=True,
                )
                for index, axis in enumerate("xyz")
            },
            "tangential_speed_mps": mo.ui.number(
                start=0,
                stop=200,
                value=_impact["tangential_speed_mps"],
                label="Tangential birth speed, m/s",
                full_width=True,
                debounce=True,
            ),
            "patch_half_angle_deg": mo.ui.number(
                start=0,
                stop=180,
                value=_impact["patch_half_angle_deg"],
                label="Source patch half-angle, degrees; greater than zero",
                full_width=True,
                debounce=True,
            ),
        }
    )
    world_seed = mo.ui.text(
        value="", label="Reset / new-world seed, blank keeps current", full_width=True
    )
    reset_run = mo.ui.run_button(label="Reset flight", kind="danger")
    regenerate_world = mo.ui.run_button(label="Regenerate world", kind="danger")
    upright = mo.ui.run_button(label="Upright / clear spin", kind="warn")
    step_seconds = lab_slider(0.001, 1, 0.001, 0.05, "Paused step, simulation s")
    schedule_slot = mo.ui.number(
        start=0, step=1, value=0, label="Schedule row, zero-based", full_width=True
    )
    run_policy_step = mo.ui.run_button(label="Run one paused policy step", kind="warn")
    return (
        debris_mode,
        flight_preset,
        impact_controls,
        regenerate_world,
        reset_run,
        run_policy_step,
        schedule_slot,
        step_seconds,
        structure_controls,
        upright,
        world_seed,
    )


@app.cell
def _(get_placement_seed, mo, position_limit_m):
    _seed = get_placement_seed()
    _position = _seed.get("position_m") if _seed is not None else None
    _enabled = _position is not None
    placement_controls = mo.ui.dictionary(
        {
            axis: mo.ui.number(
                start=-position_limit_m if _enabled else None,
                stop=position_limit_m if _enabled else None,
                value=_position[index] if _enabled else None,
                label=f"Lander {axis.upper()}, world m",
                full_width=True,
                debounce=True,
                disabled=not _enabled,
            )
            for index, axis in enumerate("xyz")
        }
    )
    place_lander = mo.ui.run_button(label="Place lander only", kind="warn", disabled=not _enabled)
    return place_lander, placement_controls


@app.cell
def _(
    debris_mode,
    flow_controls,
    impact_controls,
    ship_controls,
    step_seconds,
    structure_controls,
):
    _impact = impact_controls.value
    distribution_draft = (
        {
            "kind": "impact_ejecta",
            "settings": {
                "source_direction": [_impact[f"source_{axis}"] for axis in "xyz"],
                "kick_velocity_mps": [_impact[f"kick_{axis}"] for axis in "xyz"],
                "tangential_speed_mps": _impact["tangential_speed_mps"],
                "patch_half_angle_deg": _impact["patch_half_angle_deg"],
            },
        }
        if debris_mode.value == "impact_ejecta"
        else {"kind": "directed_stream"}
    )
    world_settings_draft = {
        "ship": {key: ship_controls.value[key] for key in ("mass_kg", "inertia_scale")},
        "debris": {
            **structure_controls.value,
            **flow_controls.value,
            "distribution": distribution_draft,
        },
    }
    # Edit this real Python schedule. One click executes one selected row, no loop.
    experiment_schedule_s = (step_seconds.value,)
    return distribution_draft, experiment_schedule_s, world_settings_draft


@app.cell
def _(
    FlightStateError,
    action_error,
    distribution_draft,
    experiment,
    flight_preset,
    get_proposal_draft,
    get_session,
    mo,
    regenerate_world,
    stopped_sim,
    structure_controls,
    validate_experiment_settings,
    world_seed,
):
    mo.stop(not regenerate_world.value)
    try:
        _approved = get_proposal_draft()
        _session = get_session()
        if _session is None:
            raise FlightStateError("Connect / Refresh before Regenerate world.")
        _sim = _session["client"]
        with _sim.control_transaction():
            _before = _sim.snapshot()["observation"]
        if _approved is not None:
            _seed, _preset = _approved["seed"], _approved["preset"]
            _settings = _approved["settings"]
        else:
            _seed = int(world_seed.value) if world_seed.value.strip() else None
            _preset = flight_preset.value
            # Use actual current hardware/flow, never unedited slider defaults.
            _settings = {
                "ship": {key: _before["ship_tuning"][key] for key in ("mass_kg", "inertia_scale")},
                "debris": {
                    **_before["debris_flow"],
                    **structure_controls.value,
                    "distribution": distribution_draft,
                },
            }
        _settings = validate_experiment_settings(_settings)
        _sim = stopped_sim()
        with mo.status.spinner(title="Building world; large fields can block the engine"):
            _after = _sim.configure_flight(_settings, _preset, seed=_seed)
        experiment.record_snapshot(_after, source="regenerate_world")
        mo.output.replace(
            mo.md("World regenerated and paused. Connect / Refresh to resume guidance.")
        )
    except (RuntimeError, ValueError) as _error:
        mo.output.replace(action_error("Regenerate world", _error))
    return


@app.cell
def _(
    FlightStateError,
    action_error,
    experiment,
    get_session,
    mo,
    place_lander,
    placement_controls,
    placement_seed,
    set_placement_seed,
    validate_position,
):
    mo.stop(not place_lander.value)
    try:
        _session = get_session()
        if _session is None:
            raise FlightStateError("Connect / Refresh before local placement.")
        _position = validate_position([placement_controls.value[axis] for axis in "xyz"])
        _sim = _session["client"]
        with mo.status.spinner(title="Validating local placement and reading the actual body"):
            _after = experiment.place_lander(_sim, _position, timeout_s=10)
        set_placement_seed(placement_seed(_after))
        mo.output.replace(
            mo.md(
                "Lander-only placement confirmed by Rapier pose/motion. "
                "The launchpad did not move. "
                "The existing mission outcome is unchanged; no flight was started. "
                "Connect / Refresh to resume Python guidance."
            )
        )
    except (RuntimeError, ValueError) as _error:
        mo.output.replace(action_error("Local placement", _error))
    return


@app.cell
def _(action_error, experiment, mo, reset_run, stopped_sim, world_seed):
    mo.stop(not reset_run.value)
    try:
        _seed = int(world_seed.value) if world_seed.value.strip() else None
        _sim = stopped_sim()
        experiment.record_snapshot(_sim.reset(seed=_seed), source="reset_flight")
        mo.output.replace(
            mo.md("Flight reset; no controller started. Connect / Refresh to resume guidance.")
        )
    except (RuntimeError, ValueError) as _error:
        mo.output.replace(action_error("Reset flight", _error))
    return


@app.cell
def _(action_error, experiment, mo, stopped_sim, upright):
    mo.stop(not upright.value)
    try:
        _sim = stopped_sim()
        experiment.record_snapshot(_sim.upright(), source="upright")
        mo.output.replace(mo.md("Upright reset received. Connect / Refresh to resume guidance."))
    except (RuntimeError, ValueError) as _error:
        mo.output.replace(action_error("Upright reset", _error))
    return


@app.cell
def _(
    FlightStateError,
    ResilientGuidance,
    action_error,
    controller_start_notice,
    experiment,
    experiment_schedule_s,
    get_step_guidance,
    live_policy,
    lookahead_target,
    math,
    mo,
    run_policy_step,
    schedule_slot,
    set_step_guidance,
    stopped_sim,
    suppress,
    time,
):
    mo.stop(not run_policy_step.value)
    _sim = None
    try:
        _slot = schedule_slot.value
        if _slot is None or not math.isfinite(_slot) or _slot != int(_slot):
            raise ValueError("Choose an integer schedule row")
        if not 0 <= _slot < len(experiment_schedule_s):
            raise ValueError("Choose an existing schedule row")
        _seconds = experiment_schedule_s[int(_slot)]
        if (
            type(_seconds) not in (int, float)
            or not math.isfinite(_seconds)
            or not 0 < _seconds <= 1
        ):
            raise ValueError("Step seconds must be finite, greater than zero and at most one")
        _sim = stopped_sim()
        if live_policy.controller == "resilient_waypoint_pd":
            _sim.pause()
        _before = _sim.snapshot()
        experiment.record_snapshot(_before, source="paused_policy_input")
        _blocked = controller_start_notice(_before)
        if _blocked:
            raise FlightStateError(_blocked)
        _observation = _before["observation"]
        if live_policy.controller == "resilient_waypoint_pd":
            _cached = get_step_guidance()
            if _cached is None or _cached[0] != live_policy.resilience:
                _cached = (live_policy.resilience, ResilientGuidance(live_policy.resilience))
                set_step_guidance(_cached)
            _guidance = _cached[1]
            _ready = _guidance.update(_observation, wall_time_s=time.monotonic(), running=False)
            _route = (
                live_policy.plan(_ready.observation, guidance=_guidance)
                if _ready.can_command
                else None
            )
            _decision = live_policy.decision(_ready.observation, _route, guidance=_guidance)
            _sim.preview(_route.to_preview() if _route is not None else None)
            _sim.set_leased_command(
                _decision.accel,
                _decision.angular_accel,
                _before,
                lease_sim_s=max(0.05, min(_ready.lease_sim_s, _seconds)),
            )
            _sim.command({"op": "step", "seconds": _seconds})
            _after = _sim.snapshot()
        else:
            _route = live_policy.plan(_observation)
            _decision = live_policy.decision(_observation, _route)
            _sim.preview(_route.to_preview())
            _sim.raycast(
                lookahead_target(_observation, _route, arrival_m=live_policy.arrival_m).tolist(),
                max_distance_m=250.0,
            )
            _after = _sim.run_step(
                _decision.accel, angular_accel=_decision.angular_accel, seconds=_seconds
            )
        experiment.record_snapshot(
            _after,
            source="policy_step_leased_result"
            if live_policy.controller == "resilient_waypoint_pd"
            else "policy_step_before_cleanup",
        )
        experiment.record_snapshot(
            _sim.snapshot(),
            source="policy_step_lease_readback"
            if live_policy.controller == "resilient_waypoint_pd"
            else "policy_step_after_cleanup",
        )
        mo.output.replace(
            mo.md("One paused policy step completed. Connect / Refresh to resume guidance.")
        )
    except (RuntimeError, ValueError) as _error:
        if _sim is not None:
            with suppress(RuntimeError, ValueError):
                _sim.preview(None)
        mo.output.replace(action_error("Paused policy step", _error))
    return


@app.cell
def _(mo):
    prepare_export = mo.ui.run_button(label="Prepare downloads")
    return (prepare_export,)


@app.cell
def _(mo):
    get_selected_tab, set_selected_tab = mo.state("Flight")
    return get_selected_tab, set_selected_tab


@app.cell(hide_code=True)
def _(
    adopt_proposal,
    apply_camera_aim,
    apply_sensor_fault,
    cadence_hz,
    camera_controls,
    capture_camera,
    capture_images,
    clear_sensor_faults,
    connect_refresh,
    connection_file,
    controller_mode,
    debris_mode,
    debris_readback_view,
    duration_s,
    discard_proposal,
    experiment_refresh,
    experiment_schedule_s,
    flight_diagnostics_view,
    flow_controls,
    flight_preset,
    gain_controls,
    get_selected_tab,
    get_proposal_draft,
    get_session,
    history_view,
    impact_controls,
    interrupt_primary,
    lab_stack,
    lab_styles,
    launch_readback_view,
    live_status_view,
    mo,
    pause_physics,
    place_lander,
    placement_controls,
    planner_controls,
    policy_status_view,
    prepare_export,
    primary_outage_duration,
    proposal_status_view,
    proposal_template_download,
    proposal_upload,
    raw_snapshot_view,
    regenerate_world,
    reset_run,
    resilience_status_view,
    review_proposal,
    run_controller,
    run_policy_step,
    schedule_slot,
    sensor_fault_duration,
    sensor_fault_mode,
    sensor_fault_target,
    set_selected_tab,
    ship_controls,
    ship_readback_view,
    step_seconds,
    stop_run,
    structure_controls,
    upright,
    world_seed,
):
    _tuning_session = get_session()
    _imported_draft = get_proposal_draft()
    _tuning_feedback = mo.md("")
    if _tuning_session is not None:
        _tuning_state = _tuning_session.get("tuning_state", "confirmed")
        if _tuning_state != "confirmed" or _tuning_session.get("tuning_error"):
            _tuning_feedback = mo.vstack(
                [
                    mo.md(
                        f"Calibration readback **{_tuning_state}** · "
                        f"source {_tuning_session.get('tuning_source_time_s', 'unavailable')} s · "
                        f"revision {_tuning_session.get('tuning_revision', 'unavailable')}"
                    ),
                    mo.md(
                        "Requested values differ from the game readback. "
                        "Controls show that readback; no tuning command was retried."
                    ).callout(kind="warn")
                    if _tuning_state == "diverged"
                    else mo.md(""),
                    mo.md(_tuning_session["tuning_error"]).callout(
                        kind="warn" if _tuning_state == "confirmed" else "danger"
                    )
                    if _tuning_session.get("tuning_error")
                    else mo.md(""),
                ]
            )
    lab_tabs = mo.ui.tabs(
        {
            "Flight": lab_stack(
                [
                    connection_file,
                    mo.hstack(
                        [connect_refresh, run_controller, stop_run, pause_physics],
                        justify="start",
                        wrap=True,
                    ),
                    live_status_view,
                    mo.md(
                        "Connect only watches. Run controller flies the policy selected in "
                        "Python policy. Stop Python control ends command renewal. In resilient "
                        "mode the native lease expires without pausing physics. Use P in the "
                        "game to pause."
                    ),
                ]
            ),
            "Resilience": lab_stack(
                [
                    resilience_status_view,
                    mo.md("### Test controller failover"),
                    mo.md(
                        "An explicit resilient Run starts a separate-process standby with "
                        "checked-in Python modules and data settings. Unsaved notebook callbacks "
                        "run only in the primary. Native epoch/term ownership permits one writer; "
                        "a recovered old primary cannot overwrite a healthy standby or undo a "
                        "manual pause. Both processes still share this host."
                    ),
                    primary_outage_duration,
                    interrupt_primary,
                    mo.md("### Test a sensor failure"),
                    mo.md(
                        "These controls change real sensor delivery inside this local simulation. "
                        "They do not fail a production cloud region or replace measurements. "
                        "Select the resilient controller before testing autonomous failover."
                    ),
                    sensor_fault_target,
                    sensor_fault_mode,
                    sensor_fault_duration,
                    mo.hstack([apply_sensor_fault, clear_sensor_faults], wrap=True),
                    mo.md(
                        "A radar-bus outage removes both radars. An all-avionics outage also "
                        "removes navigation telemetry and both cameras. Loss of usable data "
                        "means zero commanded thrust, not braking or a guaranteed safe orbit. "
                        "Failures expire on simulation time; Restore all sensor delivery also "
                        "clears them. Recovery requires genuinely advancing samples."
                    ),
                    mo.accordion(
                        {
                            "What is and is not redundant": mo.md(
                                "Backup radar samples the same physical world with its own noise "
                                "stream. The radars still share a bus and physical environment. "
                                "Navigation telemetry is one unreplicated source. Camera pixels "
                                "are not a navigation backup without a vision estimator. A native "
                                "command lease limits stale thrust if Python or its connection "
                                "dies."
                            )
                        }
                    ),
                ]
            ),
            "Ship": lab_stack(
                [
                    ship_readback_view,
                    _tuning_feedback
                    if (_tuning_session or {}).get("tuning_family") == "ship"
                    else mo.md(""),
                    mo.md("Edits apply live. Pose, motion, clock and history stay unchanged."),
                    ship_controls.vstack(),
                    mo.accordion(
                        {
                            "Hardware and command model": mo.md(
                                "Calibration is separate from applied force. With zero requested "
                                "commands, a multiplier edit does not fire the engine or restart "
                                "flight. Thrust gain is local hardware tuning; reference command "
                                "caps remain 0.5 m/s² and 0.05 rad/s². SDK-style requests scale "
                                "force with mass, so mass changes impulse response rather than "
                                "requested acceleration. Details separates stored actuator fields "
                                "from the Rapier body getter."
                            )
                        }
                    ),
                ]
            ),
            "Debris": lab_stack(
                [
                    debris_readback_view,
                    _tuning_feedback
                    if (_tuning_session or {}).get("tuning_family") == "flow"
                    else mo.md(""),
                    mo.md("Stream edits apply live, without restarting the current flight."),
                    flow_controls.vstack(),
                    mo.accordion(
                        {
                            "Live stream model": mo.md(
                                "Speed and direction edits preserve existing velocity deviations. "
                                "Birth spread affects future births and must be at most half the "
                                "speed. Live flow edits require a directed-stream world."
                            ),
                            "New-world debris setup, draft only": lab_stack(
                                [
                                    mo.md(
                                        "These drafts do not change the current world. Apply them "
                                        "with Regenerate world in Launch."
                                    ),
                                    debris_mode,
                                    structure_controls.vstack(),
                                    mo.accordion(
                                        {
                                            "Impact ejecta initial conditions": (
                                                impact_controls.vstack()
                                            )
                                        }
                                    ),
                                    mo.md(
                                        "Impact ejecta starts with seeded ejection, then follows "
                                        "gravity and contacts. It is not verified fragmentation. "
                                        "There is no orbit rail or birth recycling; an escaping "
                                        "kick need not form a ring."
                                    ),
                                ]
                            ),
                        }
                    ),
                ]
            ),
            "Launch": lab_stack(
                [
                    launch_readback_view,
                    mo.md(
                        f"Imported draft `{_imported_draft['digest']}` is active. "
                        "Regenerate world uses that draft, not the manual draft controls."
                    ).callout(kind="warn")
                    if _imported_draft is not None
                    else mo.md(""),
                    mo.md("### Move the launchpad"),
                    mo.md(
                        "In the game's Launch tab, select Place lander, then drag the "
                        "launchpad on Phobos. The button pauses flight and frames the pad."
                    ),
                    mo.accordion(
                        {
                            "Launchpad controls and retained state": mo.md(
                                "Release to apply the site; press Escape to cancel. "
                                "For point placement, select Place lander, click Phobos, "
                                "then Apply in the game. Its N/S/E/W preview controls "
                                "also work with the keyboard. Cancel discards a preview. "
                                "The pad and lander move together at rest, with thrust cleared. "
                                "The clock, hardware, fuel, damage and mission outcome stay "
                                "unchanged. Reset flight retains a custom launch site; "
                                "Regenerate world clears it."
                            )
                        }
                    ),
                    mo.md("### Reset this flight"),
                    mo.md(
                        "Reset flight replaces the run, including clock, fuel, damage and outcome. "
                        "It keeps the current launch site and calibration. "
                        "It does not start flight."
                    ).callout(kind="warn"),
                    world_seed,
                    reset_run,
                    mo.accordion(
                        {
                            "Regenerate world, replace the current run": lab_stack(
                                [
                                    mo.md(
                                        "Replaces the world and run, clears any custom launch site "
                                        "and starts a paused trial. Uses the target preset below "
                                        "and the new-world drafts in Debris unless an approved "
                                        "imported draft is active."
                                    ).callout(kind="warn"),
                                    mo.accordion(
                                        {
                                            "Import proposal JSON": lab_stack(
                                                [
                                                    mo.md(
                                                        "NotebookLM can suggest JSON. "
                                                        "It cannot execute commands. "
                                                        "Download the template and "
                                                        "upload a proposal. Review it, "
                                                        "then use it as a draft. "
                                                        "This does not tune hardware "
                                                        "or write files or Git. "
                                                        "Regenerate world replaces "
                                                        "the actual run. Maximum file size: 64 KiB."
                                                    ),
                                                    proposal_template_download,
                                                    proposal_upload,
                                                    review_proposal,
                                                    proposal_status_view,
                                                    adopt_proposal,
                                                    discard_proposal,
                                                ]
                                            ),
                                        }
                                    ),
                                    flight_preset,
                                    mo.md(
                                        "L1/L2 presets select a target and default launch side. "
                                        "They are not the only launch sites. Moving the pad "
                                        "does not move the target."
                                    ),
                                    regenerate_world,
                                    mo.accordion(
                                        {
                                            "Large debris worlds": mo.md(
                                                "Default: 500 real bodies. 10000 and 100000 are "
                                                "real-body counts, not visual duplicates. Large "
                                                "builds can block the browser, take up to the "
                                                "180-second world deadline and run below real "
                                                "time. No operation is resent automatically "
                                                "after an indeterminate response."
                                            )
                                        }
                                    ),
                                ]
                            ),
                            "Separate lander placement, advanced": lab_stack(
                                [
                                    mo.md(
                                        "Moves only the lander, not the launchpad or target. "
                                        "XYZ drafts do nothing until Place lander only."
                                    ),
                                    placement_controls.vstack(),
                                    place_lander,
                                    mo.md(
                                        "Valid placement pauses the body upright at rest, "
                                        "with +Z away from Phobos. The clock, hardware, fuel, "
                                        "damage and mission outcome stay unchanged. Rejection "
                                        "leaves the Python controller attached; a valid move "
                                        "stops its worker. Missing radar does not mean empty space."
                                    ),
                                ]
                            ),
                            "Upright / clear spin": lab_stack(
                                [
                                    mo.md(
                                        "Resets the lander's attitude and spin. "
                                        "This is a local intervention, not a manoeuvre."
                                    ),
                                    upright,
                                ]
                            ),
                        }
                    ),
                ]
            ),
            "Python policy": lab_stack(
                [
                    policy_status_view,
                    controller_mode,
                    mo.md(
                        "Edit predict_scene and find_path in the cells. Resilient decisions "
                        "and persistent estimates live in src/wsorb/flight/guidance.py and "
                        "estimation.py. Baseline controllers use the editable decide_command "
                        "cell. Connected edits reach the next decision without restarting flight."
                    ),
                    mo.accordion(
                        {
                            "A* and public-data forecast": planner_controls.vstack(),
                            "Controller gains": gain_controls.vstack(),
                        }
                    ),
                    duration_s,
                    cadence_hz,
                    mo.md(
                        "Timing edits update the existing worker; they never start flight. "
                        "Shortening the limit below elapsed time ends control. Resilient mode "
                        "stops renewal and lets the native lease expire; baseline modes pause "
                        "during cleanup. Resilient cadence must be faster than its lease interval."
                    ),
                    mo.accordion(
                        {
                            "Policy and timing model": mo.md(
                                "Replans target 0.5 simulation seconds, not repeated plans on a "
                                "held clock. Timing edits apply during a run. Duration remains "
                                "relative to the original Run; a timing edit does not reset it."
                            ),
                            "Run one paused policy step": lab_stack(
                                [
                                    step_seconds,
                                    schedule_slot,
                                    mo.json(
                                        list(experiment_schedule_s),
                                        label="Editable Python step schedule",
                                    ),
                                    run_policy_step,
                                    mo.md(
                                        "Edit experiment_schedule_s in its Python cell. "
                                        "One click executes one selected row, without a loop."
                                    ),
                                ]
                            ),
                        }
                    ),
                ]
            ),
            "History": history_view,
            "Cameras": lab_stack(
                [
                    camera_controls.vstack(),
                    apply_camera_aim,
                    capture_camera,
                    capture_images,
                    mo.md("Camera edits apply only with Apply camera aim. Captures are manual."),
                    mo.accordion(
                        {
                            "Camera contract": mo.md(
                                "PanTiltCamera uses the real degree limits. Captures are actual "
                                "512×512 RGB8, 60° field of view, fixed exposure, without HUD "
                                "or debug truth. No substitute image is shown on failure."
                            )
                        }
                    ),
                ]
            ),
            "Export": lab_stack(
                [
                    prepare_export,
                    mo.md(
                        "Prepare downloads freezes the received data without a game request "
                        "or file write. Save Python edits before downloading the saved notebook."
                    ),
                    mo.accordion(
                        {
                            "Download contents": mo.md(
                                "Snapshots, measured history, accepted policy, tuning revisions, "
                                "current drafts and saved Python sources. Prepare again for "
                                "newer readings. Source hashes identify the supplied files."
                            )
                        }
                    ),
                ]
            ),
            "Details": lab_stack(
                [
                    experiment_refresh,
                    flight_diagnostics_view,
                    raw_snapshot_view,
                    mo.accordion(
                        {
                            "Contracts and source": mo.md("""
                            Python owns prediction, A*, waypoint selection and commands.
                            Bevy/Rapier owns physics and the actual debug ray intersection.
                            Debug rays never enter official radar, prediction, control or clean RGB.

                            `SetCommand`, `GetNearestDebrisLocations` and `PanTiltCamera`
                            keep the helper contracts. Local tuning, reset, playback, preview
                            and ray queries are practice tools, not SDK methods or official scores.
                            Preserve frame, quaternion convention and original clocks for STP.

                            [Flight algorithms](../src/wsorb/flight/) ·
                            [Runner](../src/wsorb/experiment.py) ·
                            [Local client](../src/wsorb/local_api.py) ·
                            [Licensed adapter](../src/wsorb/stp_api.py)

                            Keep automatic cell execution enabled for one-shot buttons.
                            Share source through the private repository, never the editor or token.
                            """)
                        }
                    ),
                ]
            ),
        },
        value=get_selected_tab(),
        label="Flight lab tasks",
        on_change=set_selected_tab,
    )
    lab_view = mo.Html(f'<div class="wsorb-lab">{lab_styles}{lab_tabs}</div>')
    mo.output.replace(lab_view)
    return lab_tabs, lab_view


@app.cell
def _(
    Path,
    action_error,
    asdict,
    cadence_hz,
    camera_aim_draft,
    controller_gains,
    controller_mode,
    decide_command,
    duration_s,
    distribution_draft,
    experiment,
    experiment_document,
    experiment_schedule_s,
    find_path,
    flight_modules,
    flight_preset,
    get_applied_config,
    get_camera_report,
    get_proposal_draft,
    history_csv,
    json_bytes,
    lab_source_path,
    mo,
    planner_settings,
    placement_controls,
    policy_sources,
    predict_scene,
    prepare_export,
    schedule_slot,
    scenario_source_path,
    ship_controls,
    world_seed,
    world_settings_draft,
):
    mo.stop(not prepare_export.value)
    try:
        _status = experiment.status()
        _rows = experiment.history()
        _flight_dir = Path(flight_modules.__file__).resolve().parent
        _saved_notebook = lab_source_path.read_bytes()
        _sources = {"notebooks/flight_lab.py, saved file at export": _saved_notebook}
        _sources["simulation/scenarios/phobos_escape.json, saved file at export"] = (
            scenario_source_path.read_bytes()
        )
        _sources.update(
            {
                f"src/wsorb/flight/{path.name}, saved file at export": path.read_bytes()
                for path in sorted(_flight_dir.glob("*.py"))
            }
        )
        for _name in (
            "experiment.py",
            "experiment_io.py",
            "proposals.py",
            "standby.py",
            "local_api.py",
        ):
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
                "world_geometry_controls": world_settings_draft,
                "approved_world_proposal": get_proposal_draft(),
                "flight_preset": flight_preset.value,
                "debris_distribution": distribution_draft,
                "placement_xyz_draft_m": dict(placement_controls.value),
                "ship_controls": dict(ship_controls.value),
                "world_seed_override_text": world_seed.value,
                "camera_aim_controls": camera_aim_draft,
            },
            selected_policy=controller_mode.value,
            schedule_s=experiment_schedule_s,
            history=_rows,
            latest_snapshot=_status["latest"],
            sources=_sources,
            camera_status=get_camera_report(),
            configuration_changes=experiment.configuration_changes(),
            execution_status={key: value for key, value in _status.items() if key != "latest"},
        )
        mo.output.replace(
            mo.vstack(
                [
                    mo.md("Downloads freeze this preparation. Prepare again for newer readings."),
                    mo.hstack(
                        [
                            mo.download(
                                json_bytes(_document),
                                filename="flight-experiment.json",
                                mimetype="application/json",
                                label="Experiment JSON",
                            ),
                            mo.download(
                                history_csv(_rows),
                                filename="flight-history.csv",
                                mimetype="text/csv",
                                label="Measured history CSV",
                            ),
                            mo.download(
                                _saved_notebook,
                                filename="flight_lab.py",
                                mimetype="text/x-python",
                                label="Saved Python notebook",
                            ),
                        ],
                        justify="start",
                        wrap=True,
                    ),
                    mo.accordion(
                        {"Exact supplied source hashes": mo.json(_document["python_sources"])}
                    ),
                ]
            )
        )
    except (OSError, TypeError, ValueError) as _error:
        mo.output.replace(
            action_error("Export preparation; save source files and prepare again", _error)
        )
    return


if __name__ == "__main__":
    app.run()
