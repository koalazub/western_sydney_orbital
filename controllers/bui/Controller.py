# ruff: noqa: E402, E501, E702, E731
#############################################################################################
# DON'T CHANGE ANY OF THE BELOW. NECESSARY FOR CORRECTLY JOINING THE COMPETITION SIMULATION #
#############################################################################################

import datetime
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
topCameraFrame = topCamera.GetBodyFixedFrame()
bottomCamera: st.Entity = this.GetParam(st.VarType.entityRef, "BottomCamera")
bottomCameraFrame = bottomCamera.GetBodyFixedFrame()


def GetNearestDebrisLocations(N: int) -> list[npt.NDArray]:
    allDebrisInfo: list[npt.NDArray] = shuttle.GetParamArray(
        st.VarType.doubleM3x3, ["Radar", "SortedDebrisData"]
    )
    nearest = allDebrisInfo[: min(N, len(allDebrisInfo))]
    return nearest


def PanTiltCamera(camera: st.Entity, yaw_deg: float, pitch_deg: float) -> None:
    if camera == topCamera:
        yaw_deg = np.clip(yaw_deg, -89.0, 89.0)
        pitch_deg = np.clip(pitch_deg, -50.0, 0.0)
    elif camera == bottomCamera:
        yaw_deg = np.clip(yaw_deg, -89.0, 89.0)
        pitch_deg = np.clip(pitch_deg, 0.0, 50.0)
    camera.setRotation_DCM(
        st.math.Euler321_to_DCM([np.deg2rad(yaw_deg), np.deg2rad(pitch_deg), 0.0]), shuttleFrame
    )


def SetCommand(accelCommand: npt.NDArray, angAccelCommand: npt.NDArray) -> None:
    shuttle.SetParam(st.VarType.doubleV3, ["JetSelection", "CommandedAccel"], accelCommand)
    shuttle.SetParam(st.VarType.doubleV3, ["JetSelection", "CommandedAngAccel"], angAccelCommand)


#############################################################################################
# DON'T CHANGE ANY OF THE ABOVE. NECESSARY FOR CORRECTLY JOINING THE COMPETITION SIMULATION #
#############################################################################################


#########################################################
# EXAMPLE USER CODE (NOTE: RUNNING A LOOP IS NECESSARY) #
#########################################################


###############################
# Get initial simulation time #
###############################

t0_sim: st.timestamp = st.SimGlobals.GetSimEntity().GetParam(
    st.VarType.timestamp, ["Time", "StartDateTAI"]
)
t0: datetime.datetime = t0_sim.as_datetime()

"""
Conventions:
  * Positions/velocities in the Phobos frame, metres and metres/second.
  * Radar item: item[0,:] rock centre relative to shuttle; item[1,0] surface distance;
    item[1,1] range rate; item[1,2] diameter; item[2,0] measurement time.
"""


ALPHA, BETA = 0.3, 0.1  # alpha-beta filter gains: ALPHA corrects position, BETA corrects velocity

# =====================================================================================
# 1. TRACKING - alpha-beta filter per rock (no per-track history needed)
# =====================================================================================


class Rock:
    def __init__(self, position, diameter, time):
        self.position = position
        self.diameter = diameter
        self.velocity = np.zeros(3)
        self.last_seen = time

    def add_sighting(self, position, diameter, time):
        dt = time - self.last_seen
        if dt <= 0:
            return
        residual = position - self.predicted_position(time)
        self.position = self.predicted_position(time) + ALPHA * residual
        self.velocity = self.velocity + (BETA / dt) * residual
        self.diameter = 0.8 * self.diameter + 0.2 * diameter
        self.last_seen = time

    def predicted_position(self, time):
        return self.position + self.velocity * (time - self.last_seen)


class Tracker:
    def __init__(self, gate=15.0, max_age=5.0):
        self.rocks = []
        self.gate = gate
        self.max_age = max_age

    def update(self, radar_items, shuttle_position, now):
        for item in radar_items:
            position = shuttle_position + item[0, :]
            diameter, time = item[1, 2], item[2, 0]
            rock = self.find_match(position, diameter, time)
            if rock is None:
                self.rocks.append(Rock(position, diameter, time))
            else:
                rock.add_sighting(position, diameter, time)
        self.rocks = [r for r in self.rocks if now - r.last_seen < self.max_age]

    def find_match(self, position, diameter, time):
        best, best_distance = None, self.gate
        for rock in self.rocks:
            if abs(rock.diameter - diameter) > 0.25 * rock.diameter:
                continue
            distance = np.linalg.norm(rock.predicted_position(time) - position)
            if distance < best_distance:
                best, best_distance = rock, distance
        return best


# =====================================================================================
# 2. AVOIDANCE - detect a collision course and say how long to hold before it clears
# =====================================================================================


class Avoider:
    def __init__(self, margin=6.0, horizon=50.0):
        self.margin = margin
        self.horizon = horizon

    def check(self, velocity, rocks, shuttle_position, now):
        if len(rocks) == 0:
            return None
        rel_pos = np.array([r.predicted_position(now) - shuttle_position for r in rocks])
        rock_vel = np.array([r.velocity for r in rocks])
        rock_diameter = np.array([r.diameter for r in rocks])
        rel_vel = rock_vel - velocity
        tau_star = np.clip(
            -(rel_pos * rel_vel).sum(axis=1) / ((rel_vel * rel_vel).sum(axis=1) + 1e-9),
            0.0,
            self.horizon,
        )
        gap = np.linalg.norm(rel_pos + rel_vel * tau_star[:, None], axis=1) - rock_diameter / 2
        ok = (tau_star < 20) & (gap < 10)
        if not ok.any():
            return None
        i = np.flatnonzero(ok)[np.argmin(gap[ok])]
        return tau_star[i], gap[i], i

    def gap(self, rock, velocity, shuttle_position, now):
        """Closest-approach time and gap for ONE tracked rock if we fly at `velocity`."""
        rel_pos = rock.predicted_position(now) - shuttle_position
        rel_vel = rock.velocity - velocity
        tau = np.clip(-(rel_pos @ rel_vel) / (rel_vel @ rel_vel + 1e-9), 0.0, self.horizon)
        return tau, np.linalg.norm(rel_pos + rel_vel * tau) - rock.diameter / 2


# =====================================================================================
# 3. FUEL - only fire the thrusters when the velocity error is worth a burn
# =====================================================================================


class BurnGovernor:
    def __init__(self, gain=5.0, start=0.2, stop=0.1):
        self.gain = gain
        self.start = start
        self.stop = stop
        self.burning = False

    def acceleration(self, wanted_velocity, current_velocity):
        error = np.asarray(wanted_velocity, float) - np.asarray(current_velocity, float)
        size = np.linalg.norm(error)
        if self.burning and size < self.stop:
            self.burning = False
        elif not self.burning and size > self.start:
            self.burning = True
        return self.gain * error if self.burning else np.zeros(3)


####################################
#        Main Code Start           #
####################################

tracker = Tracker()
avoider = Avoider()
governor = BurnGovernor()

CRUISE_SPEED = 7.0
A_MAX, T_TRIAL = 0.1, 3.0  # thruster limit (m/s^2) and trial burn length (s)
CLEARANCE = 10.0  # the avoided rock must pass at least this far away

to_phobos = lambda v: np.asarray(st.frames.FramedVector(v, shuttleFrame).ExprIn(phobosFrame), float)

##################
# Start the loop #
##################

iteration = 0
mode = "burn"
cruise_velocity = None
burn_done = False
hold_until = 0.0
rock = None  # the rock currently being avoided
dodge_dir = None
exit_flag: bool = False
prev_mode = None
while not exit_flag:
    time.sleep(1.0 / this.GetParam(st.VarType.double, "LoopFreqHz"))
    t_from_start: float = (st.SimGlobals.SimClock.GetTimeNow().as_datetime() - t0).total_seconds()

    locVelAcc = shuttle.getLocVelAcc()
    loc = locVelAcc.loc_WRT_ExprIn(phobosFrame)
    vel = locVelAcc.vel_WRT_ExprIn(phobosFrame)
    fuelUsed: float = shuttle.GetParam(st.VarType.double, ["Stats", "FuelUsed"])

    targetLoc: npt.NDArray = target.getLocVelAcc().loc_WRT_ExprIn(phobosFrame)
    side = np.cross(targetLoc - loc, [1.0, 0.0, 0.0])
    side /= np.linalg.norm(side)
    toward_target = targetLoc + 40.0 * side - loc
    toward_target /= np.linalg.norm(toward_target)

    tracker.update(GetNearestDebrisLocations(500), loc, t_from_start)

    # 1. THREAT - only looked for outside an avoidance, so the same rock isn't re-planned every loop
    if mode in ("burn", "coast", "hold", "correction"):
        others = [r for r in tracker.rocks if r is not rock]
        threat = avoider.check(vel, tracker.rocks, loc, t_from_start)
        if threat is not None:
            tau_star, gap, i = threat
            rock = others[i]
            # Trial burn along each body axis (+/-): keep the direction that leaves the biggest gap to this rock
            trials = []
            for axis in range(3):
                for sign in (1, -1):
                    e = np.zeros(3)
                    e[axis] = sign
                    dv = to_phobos(A_MAX * e * T_TRIAL)
                    trials.append((avoider.gap(rock, vel + dv, loc, t_from_start)[1], dv))
            dodge_dir = max(trials, key=lambda x: x[0])[1]
            dodge_dir = dodge_dir / np.linalg.norm(dodge_dir)
            mode = "avoid"
            st.OnScreenLogMessage(
                f"Avoiding rock: closest approach in {tau_star:.1f}s, gap {gap:.1f} m",
                "Status",
                st.Severity.Info,
            )

    # 2. AVOID - keep pushing along the chosen direction until the tracked rock passes at CLEARANCE or more
    if mode == "avoid":
        goalVelocity = vel + dodge_dir
        tau, gap = avoider.gap(rock, vel, loc, t_from_start)
        if rock not in tracker.rocks or gap >= CLEARANCE:
            hold_until = t_from_start + tau + 1
            mode = "hold"

    # 3. HOLD - engines off until the rock has passed (or keep finishing the initial burn along the current heading)
    elif mode == "hold":
        speed = np.linalg.norm(vel)
        heading = vel / speed if speed > 0.1 else toward_target
        goalVelocity = vel if burn_done else CRUISE_SPEED * heading
        if t_from_start >= hold_until:
            mode = "correction"

    # 4. CORRECTION - burn back toward the target
    elif mode == "correction":
        goalVelocity = CRUISE_SPEED * toward_target
        if np.linalg.norm(vel - goalVelocity) < 0.1:
            mode, rock = "burn", None

    # 5. BURN - initial burn to cruise speed (skipped straight through once done)
    if mode == "burn":
        goalVelocity = CRUISE_SPEED * toward_target
        if np.linalg.norm(vel) >= 0.99 * CRUISE_SPEED:
            cruise_velocity, burn_done, mode = vel.copy(), True, "coast"

    # 6. COAST - engines off at the captured cruise velocity
    elif mode == "coast":
        goalVelocity = cruise_velocity

    accel = governor.acceleration(goalVelocity, vel)
    accel = st.frames.FramedVector(accel, phobosFrame).ExprIn(shuttleFrame)
    SetCommand(accel, np.array([0, 0, 0]))

    if iteration % 20 == 0 or mode != prev_mode:
        st.OnScreenLogMessage(
            f"mode={mode} |vel|={np.linalg.norm(vel):.2f} fuel={fuelUsed:.2f}kg",
            "Status",
            st.Severity.Info,
        )
        prev_mode = mode

    iteration += 1

st.leave_sim()
