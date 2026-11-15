"""Observational launch adapter for original STP Python systems (Python 3.12).

The original sources/argv, SDK calls, loop pacing and commands are retained.
Monitoring is local to the original system's code objects; imported plotting and
numeric libraries are not profiled. Overhead is declared in every manifest.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import runpy
import socket
import sys
import time
import traceback
from pathlib import Path
from types import CodeType


def plain(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if hasattr(value, "tolist"):
        return plain(value.tolist())
    return str(value)


def run(spec_path):
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    source = str(Path(spec["source"]).resolve())
    role = spec["role"]
    connection = socket.create_connection(("127.0.0.1", spec["port"]), timeout=5)
    connection.settimeout(2)
    connection.sendall((json.dumps(dict(token=spec["token"], role=role)) + "\n").encode())
    sequence = 0

    def emit(kind, **data):
        nonlocal sequence
        record = dict(
            kind=kind,
            role=role,
            process_id=os.getpid(),
            process_seq=sequence,
            utc=datetime.datetime.now(datetime.UTC).isoformat(),
            monotonic_s=time.monotonic(),
            **data,
        )
        sequence += 1
        connection.sendall((json.dumps(plain(record), allow_nan=False) + "\n").encode())

    emit("process_started", source=source, argv=sys.argv)
    latest_sensor = None
    sensor_acquired = None
    radar_origin = None
    decision_start = None
    last_sample = -math.inf
    original_file = os.path.normcase(source)
    profile_errors = []
    score_pending = None
    score_result = None
    actuator_read = {}
    monitoring = sys.monitoring
    tool_id = 3
    monitoring.use_tool_id(tool_id, "wsorb-stp-evidence")
    monitored_codes = []

    def sample(namespace):
        st = sys.modules["spaceteams"]
        shuttle = namespace.get("shuttle")
        if shuttle is None:
            return
        phobos = namespace["phobos"].GetBodyFixedFrame()
        target = namespace["target"]
        state = shuttle.getLocVelAcc()
        position = state.loc_WRT_ExprIn(phobos)
        velocity = state.vel_WRT_ExprIn(phobos)
        target_state = target.getLocVelAcc()
        target_position = target_state.loc_WRT_ExprIn(phobos)
        target_velocity = target_state.vel_WRT_ExprIn(phobos)
        np = sys.modules["numpy"]
        sim_now = st.SimGlobals.SimClock.GetTimeNow().as_datetime()
        start = (
            st.SimGlobals.GetSimEntity()
            .GetParam(st.VarType.timestamp, ["Time", "StartDateTAI"])
            .as_datetime()
        )
        sim_s = (sim_now - start).total_seconds()
        sensor_times = [float(d[2][0]) for d in latest_sensor] if latest_sensor else []
        sensor_age = None
        if radar_origin is not None and sensor_times:
            sensor_age = max((sim_now - radar_origin).total_seconds() - t for t in sensor_times)
        mode = namespace.get("tel", {}).get("mode")
        governor = namespace.get("governor")
        if governor is not None:
            mode = "burn" if governor.firing else "coast"
        emit(
            "telemetry",
            sim_s=sim_s,
            sim_tai=sim_now.isoformat(),
            position_phobos_m=position,
            velocity_phobos_m_s=velocity,
            target_position_phobos_m=target_position,
            relative_speed_m_s=float(np.linalg.norm(velocity - target_velocity)),
            range_m=float(np.linalg.norm(target_position - position)),
            attitude_xyzw_phobos=shuttle.getRotation().Quat_WRT(phobos),
            angular_velocity_phobos_rad_s=shuttle.getAngVelocity().WRT_ExprIn(phobos),
            fuel_kg=shuttle.GetParam(st.VarType.double, ["Stats", "FuelUsed"]),
            damage_J=shuttle.GetParam(st.VarType.double, ["Stats", "Damage"]),
            radar_measurements=latest_sensor,
            sensor_read_completed_monotonic_s=sensor_acquired,
            sensor_timestamps_since_radar_origin_s=sensor_times,
            sensor_age_s=sensor_age,
            radar_origin_tai=radar_origin.isoformat() if radar_origin else None,
            mode=mode,
            avoidance=getattr(namespace.get("avoider"), "last_info", None),
            missing_data=["Native contact events/identities", "Fuel by maneuver"],
            reads_are_atomic=False,
        )

    def observe(frame, event, arg):
        nonlocal latest_sensor, sensor_acquired, radar_origin, decision_start
        nonlocal last_sample, score_pending, score_result
        # Only inspect the original system's own calls, not NumPy/Matplotlib internals.
        if os.path.normcase(frame.f_code.co_filename) != original_file:
            return
        name = getattr(arg, "__name__", "") if event.startswith("c_") else frame.f_code.co_name
        namespace = frame.f_globals
        if event == "c_return" and name == "connect_to_sim":
            st = sys.modules["spaceteams"]
            emit(
                "connected",
                native_run_uuid=str(st.SimGlobals.GetRunUUID()),
                native_logs=str(st.SimGlobals.GetRunLogsDir()),
                native_output=str(st.SimGlobals.GetRunOutputDir()),
                sim_state=str(st.SimGlobals.GetSimState()),
            )
        if event == "c_return" and name == "sleep":
            decision_start = time.perf_counter()
        if role == "radar" and event == "c_call" and name == "sleep" and radar_origin is None:
            radar_origin = namespace.get("t0")
            emit("radar_clock", origin_tai=radar_origin.isoformat() if radar_origin else None)
        if event == "return" and name == "GetNearestDebrisLocations":
            latest_sensor = plain(arg)
            sensor_acquired = time.monotonic()
        if role == "actuator" and event == "return" and name == "IfHas_GetParam":
            key = frame.f_locals.get("key")
            if key == ["JetSelection", "CommandedAccel"]:
                actuator_read["linear_body_m_s2"] = plain(arg)
                actuator_read["linear_read_monotonic_s"] = time.monotonic()
            elif key == ["JetSelection", "CommandedAngAccel"]:
                actuator_read["angular_body_rad_s2"] = plain(arg)
                actuator_read["angular_read_monotonic_s"] = time.monotonic()
        if role == "actuator" and event == "c_call" and name == "sleep":
            now = time.monotonic()
            if actuator_read and now - last_sample >= spec["sample_period_s"]:
                en = namespace["en"]
                resident_frame = namespace["resFrame"]
                emit(
                    "actuator_applied",
                    received=actuator_read.copy(),
                    clamped_linear_body_m_s2=namespace.get("accCommand"),
                    clamped_angular_body_rad_s2=namespace.get("angAccCommand"),
                    linear_impulse_body_kg_m_s=namespace.get("linearImpulse"),
                    angular_impulse_body_kg_m2_s=namespace.get("angularImpulse"),
                    impulse_counter=namespace.get("impulseCounter"),
                    step_fuel_kg=namespace.get("fuelUsage"),
                    velocity_resident_m_s=en.getLocVelAcc().vel_WRT_ExprIn(resident_frame),
                    position_resident_m=en.getLocVelAcc().loc_WRT_ExprIn(resident_frame),
                    effect_calls_returned=True,
                    reads_are_atomic=False,
                )
                last_sample = now
        if event == "c_call" and name == "CaptureImageBlocking":
            emit("camera_requested")
        if event == "c_return" and name == "CaptureImageBlocking":
            emit(
                "camera_returned",
                pixels_available=None,
                note="C profiling omits the return value; native logs/images are evidence",
            )
        if role == "controller" and event == "call" and name == "SetCommand":
            args = frame.f_locals
            emit(
                "command",
                linear_body_m_s2=args.get("accelCommand"),
                angular_body_rad_s2=args.get("angAccelCommand"),
                decision_duration_s=time.perf_counter() - decision_start
                if decision_start is not None
                else None,
                decision_duration_definition=(
                    "Loop sleep return to SetCommand call; includes SDK/camera/UI"
                ),
            )
        if role == "controller" and event == "return" and name == "SetCommand":
            emit(
                "command_written",
                linear_body_m_s2=frame.f_locals.get("accelCommand"),
                angular_body_rad_s2=frame.f_locals.get("angAccelCommand"),
            )
            now = time.monotonic()
            if now - last_sample >= spec["sample_period_s"]:
                # Radar's origin is a separate native process; parent supplies its clock record
                # during analysis. Preserve raw times here; do not guess an offset.
                try:
                    sample(namespace)
                except Exception:
                    emit("sensor_error", error=traceback.format_exc())
                last_sample = now
        if role == "scorer" and event == "call" and name in {"YouWon", "YouLost"}:
            score_pending = dict(
                outcome="success" if name == "YouWon" else "failure",
                inputs=plain(dict(frame.f_locals)),
                scorer_time_s=namespace.get("sim_elapsed_time"),
                range_m=plain(namespace.get("distanceToShip")),
                initial_range_m=plain(namespace.get("initial_distanceToShip")),
            )
            emit("scoring_inputs", **score_pending)
        if role == "scorer" and event == "return" and name in {"FullScoreWin", "FullScoreLoss"}:
            score_result = plain(arg)
            emit("score_calculated_native", value=score_result)
        if role == "scorer" and event == "return" and name in {"YouWon", "YouLost"}:
            emit(
                "official_receipt",
                **score_pending,
                official_score=score_result,
                official_display_score=float(f"{score_result:.1f}"),
                source="Original scorer return after native OnScreenAlert",
                termination_reason="official_arrival"
                if name == "YouWon"
                else "official_damage_failure",
            )
        if role == "scorer" and event == "c_call" and name == "sleep":
            emit("scorer_progress", scorer_time_s=namespace.get("sim_elapsed_time"))

    def disable():
        monitoring.set_events(tool_id, 0)
        for code in monitored_codes:
            monitoring.set_local_events(tool_id, code, 0)

    def observed(frame, event, arg):
        try:
            observe(frame, event, arg)
        except Exception:
            # Do not replace an original control exception with a diagnostic failure.
            # Preserve this failure on stderr; parent halts on the dropped evidence stream.
            if not profile_errors:
                profile_errors.append(traceback.format_exc())
                print(profile_errors[-1], file=sys.stderr, flush=True)
                try:
                    emit("hook_error", error=profile_errors[-1])
                except OSError:
                    connection.close()
            disable()

    target_functions = {
        "SetCommand",
        "GetNearestDebrisLocations",
        "IfHas_GetParam",
        "YouWon",
        "YouLost",
        "FullScoreWin",
        "FullScoreLoss",
    }

    def register_code(code):
        if code.co_name == "<module>":
            monitoring.set_local_events(tool_id, code, monitoring.events.CALL)
            monitored_codes.append(code)
        elif code.co_name in target_functions:
            monitoring.set_local_events(
                tool_id, code, monitoring.events.PY_START | monitoring.events.PY_RETURN
            )
            monitored_codes.append(code)
        for constant in code.co_consts:
            if isinstance(constant, CodeType):
                register_code(constant)

    def on_start(code, offset):
        frame = sys._getframe(1)
        if os.path.normcase(code.co_filename) != original_file:
            return monitoring.DISABLE
        if code.co_name == "<module>":
            register_code(code)
            # Discovery ends at the original module. Imported code receives no global events.
            monitoring.set_events(tool_id, 0)
        else:
            observed(frame, "call", None)

    def on_return(code, offset, result):
        observed(sys._getframe(1), "return", result)

    native_calls = {"connect_to_sim", "sleep", "CaptureImageBlocking"}

    def on_call(code, offset, function, first_arg):
        if getattr(function, "__name__", "") not in native_calls:
            return monitoring.DISABLE
        observed(sys._getframe(1), "c_call", function)

    def on_c_return(code, offset, function, first_arg):
        if getattr(function, "__name__", "") in native_calls:
            observed(sys._getframe(1), "c_return", function)

    sys.path.insert(0, str(Path(source).parent))
    sys.argv[0] = source
    monitoring.register_callback(tool_id, monitoring.events.PY_START, on_start)
    monitoring.register_callback(tool_id, monitoring.events.PY_RETURN, on_return)
    monitoring.register_callback(tool_id, monitoring.events.CALL, on_call)
    monitoring.register_callback(tool_id, monitoring.events.C_RETURN, on_c_return)
    monitoring.set_events(tool_id, monitoring.events.PY_START)
    try:
        runpy.run_path(source, run_name="__main__")
    except BaseException:
        disable()
        emit("exception", error=traceback.format_exc())
        raise
    finally:
        disable()
        for event in (
            monitoring.events.PY_START,
            monitoring.events.PY_RETURN,
            monitoring.events.CALL,
            monitoring.events.C_RETURN,
        ):
            monitoring.register_callback(tool_id, event, None)
        monitoring.free_tool_id(tool_id)
        emit("process_finished")
        connection.close()
