"""SDK entrypoint for Ali's MPPI controller.

The policy lives in `mppi_core.py`; this file only reads the simulator, runs the
policy, logs telemetry and writes the command. STP does not put the script
directory on `sys.path`, so add it explicitly before importing the core. Upload
this file and `mppi_core.py` together.
"""

# ruff: noqa: E402

#############################################################################################
# DON'T CHANGE ANY OF THE BELOW. NECESSARY FOR CORRECTLY JOINING THE COMPETITION SIMULATION #
#############################################################################################

import os
import sys
import time
import traceback

import spaceteams as st


def custom_exception_handler(exctype, value, tb):
    error_message = "".join(traceback.format_exception(exctype, value, tb))
    st.logger_fatal(error_message)


sys.excepthook = custom_exception_handler
st.connect_to_sim(sys.argv)

import numpy as np
import numpy.typing as npt

this = st.GetThisSystem()
doSpawning = st.SimGlobals.GetSimEntity().GetParam(st.VarType.bool, "DoSpawning")
if doSpawning:
    while True:
        time.sleep(1.0 / this.GetParam(st.VarType.double, "LoopFreqHz"))

phobos: st.Entity = this.GetParam(st.VarType.entityRef, "Phobos")
phobosFrame = phobos.GetBodyFixedFrame()
shuttle: st.Entity = this.GetParam(st.VarType.entityRef, "Shuttle")
shuttleFrame = shuttle.GetBodyFixedFrame()
target: st.Entity = this.GetParam(st.VarType.entityRef, "Target")
topCamera: st.Entity = this.GetParam(st.VarType.entityRef, "TopCamera")
bottomCamera: st.Entity = this.GetParam(st.VarType.entityRef, "BottomCamera")


def GetNearestDebrisLocations(N: int) -> list[npt.NDArray]:
    allDebrisInfo: list[npt.NDArray] = shuttle.GetParamArray(
        st.VarType.doubleM3x3, ["Radar", "SortedDebrisData"]
    )
    return allDebrisInfo[: min(N, len(allDebrisInfo))]


def SetCommand(accelCommand: npt.NDArray, angAccelCommand: npt.NDArray) -> None:
    shuttle.SetParam(st.VarType.doubleV3, ["JetSelection", "CommandedAccel"], accelCommand)
    shuttle.SetParam(st.VarType.doubleV3, ["JetSelection", "CommandedAngAccel"], angAccelCommand)


#############################################################################################
# DON'T CHANGE ANY OF THE ABOVE. NECESSARY FOR CORRECTLY JOINING THE COMPETITION SIMULATION #
#############################################################################################

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mppi_core import (
    ATTITUDE_DEADBAND_RAD,
    RATE_DEADBAND_RAD_S,
    attitude,
    cruise_velocity,
    govern,
    plan,
    unit,
    update_belief,
)

t0 = (
    st.SimGlobals.GetSimEntity()
    .GetParam(st.VarType.timestamp, ["Time", "StartDateTAI"])
    .as_datetime()
)
state = {"rocks": [], "burning": False, "last_control": np.zeros(3)}
last_time, last_fuel, fuel_rate, last_status = None, None, 0.0, -1.0
log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "controller_mppi.csv")
log_file = open(log_path, "w", buffering=1)  # noqa: SIM115
log_file.write(
    "time_s,x,y,z,vx,vy,vz,speed,target_range,fuel,damage,"
    "accel_norm,accel_x,accel_y,accel_z,ang_norm,ang_x,ang_y,ang_z,"
    "mode,velocity_error,alignment,rocks\n"
)

while True:
    time.sleep(1.0 / this.GetParam(st.VarType.double, "LoopFreqHz"))
    now = (st.SimGlobals.SimClock.GetTimeNow().as_datetime() - t0).total_seconds()
    dt = (
        1.0 / this.GetParam(st.VarType.double, "LoopFreqHz")
        if last_time is None
        else max(1e-3, now - last_time)
    )
    last_time = now
    stopped = shuttle.getLocVelAcc()
    position = np.asarray(stopped.loc_WRT_ExprIn(phobosFrame), dtype=float)
    velocity = np.asarray(stopped.vel_WRT_ExprIn(phobosFrame), dtype=float)
    target_position = np.asarray(target.getLocVelAcc().loc_WRT_ExprIn(phobosFrame), dtype=float)
    body_x = np.asarray(
        st.frames.FramedVector(np.array([1.0, 0.0, 0.0]), shuttleFrame).ExprIn(phobosFrame),
        dtype=float,
    )
    angular_velocity = np.asarray(shuttle.getAngVelocity().WRT_ExprIn(phobosFrame), dtype=float)
    fuel = float(shuttle.GetParam(st.VarType.double, ["Stats", "FuelUsed"]))
    damage = float(shuttle.GetParam(st.VarType.double, ["Stats", "Damage"]))
    radius = float(shuttle.GetParam(st.VarType.double, ["Graphics", "VisualRadius_m"]))
    max_accel = float(shuttle.GetParam(st.VarType.double, ["Thrusters", "MaxAccel_m_s2"]))
    max_angular = float(shuttle.GetParam(st.VarType.double, ["Thrusters", "MaxAngAccel_rad_s2"]))
    rows = GetNearestDebrisLocations(80)
    state = update_belief(state, rows, position, now)
    if last_fuel is not None and now > 0.0:
        observed = max(0.0, (fuel - last_fuel) / max(dt, 1e-3))
        fuel_rate = observed if fuel_rate == 0.0 else 0.8 * fuel_rate + 0.2 * observed
    last_fuel = fuel
    nominal_velocity = cruise_velocity(position, target_position)
    desired_accel = plan(
        state,
        position,
        velocity,
        target_position,
        body_x,
        radius,
        max_accel,
        dt,
        fuel,
        damage,
        fuel_rate,
        now,
    )
    accel_world, state["burning"] = govern(
        desired_accel, nominal_velocity, velocity, state["burning"], max_accel
    )
    desired = unit(nominal_velocity)
    alignment = (
        np.arccos(float(np.clip(body_x @ desired, -1.0, 1.0)))
        if np.linalg.norm(desired) > 1e-9
        else 0.0
    )
    angular_world = (
        attitude(body_x, angular_velocity, desired, max_angular, dt)
        if state["burning"]
        or np.linalg.norm(angular_velocity) > RATE_DEADBAND_RAD_S
        or alignment > ATTITUDE_DEADBAND_RAD
        else np.zeros(3)
    )
    body_accel = st.frames.FramedVector(accel_world, phobosFrame).ExprIn(shuttleFrame)
    body_angular = st.frames.FramedVector(angular_world, phobosFrame).ExprIn(shuttleFrame)
    SetCommand(body_accel, body_angular)

    speed = np.linalg.norm(velocity)
    target_range = np.linalg.norm(target_position - position)
    velocity_error = np.linalg.norm(nominal_velocity - velocity)
    mode = "burn" if state["burning"] else "coast"
    accel_norm = np.linalg.norm(accel_world)
    angular_norm = np.linalg.norm(angular_world)
    log_file.write(
        f"{now:.3f},"
        + ",".join(
            f"{value:.5f}" for value in (*position, *velocity, speed, target_range, fuel, damage)
        )
        + f",{accel_norm:.5f},"
        + ",".join(f"{value:.5f}" for value in (*accel_world, angular_norm, *angular_world))
        + f",{mode},{velocity_error:.4f},{alignment:.4f},{len(state['rocks'])}\n"
    )
    if now - last_status >= 1.0:
        last_status = now
        message = (
            f"{mode} t={now:5.1f}s v={speed:5.2f} err={velocity_error:4.2f} "
            f"fuel={fuel:5.1f} dmg={damage:6.0f} range={target_range:6.0f} "
            f"a={accel_norm:4.2f}"
        )
        st.logger_info(message)
        st.OnScreenLogMessage(message, "MPPI", st.Severity.Info)
        st.OnScreenAlert(message, "MPPI", st.Severity.Info)
