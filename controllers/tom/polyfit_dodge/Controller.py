# ruff: noqa: E402, E501
#############################################################################################
# DON'T CHANGE ANY OF THE BELOW. NECESSARY FOR CORRECTLY JOINING THE COMPETITION SIMULATION #
#############################################################################################

import datetime
import os
import sys
import time
import traceback
from collections import deque

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

###################
# Make a log file #
###################
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "controller_log.txt")
log_file = open(LOG_PATH, "w", buffering=1)  # noqa: SIM115  # line-buffered, fresh file each run


def status(msg: str, key: str = "Status") -> None:
    print(msg, flush=True)
    st.OnScreenLogMessage(msg, key, st.Severity.Info)
    log_file.write(msg + "\n")


"""
Get angular velocity from two quaternions representing the orientation of
an object at time (t0) and (t0 + dt). Assumes scalar-last quaternions.
"""


def AngVelFromQuats(q1: npt.NDArray, q2: npt.NDArray, dt: float):
    return (2.0 / dt) * np.array(
        [
            q1[3] * q2[0] - q1[0] * q2[3] - q1[1] * q2[2] + q1[2] * q2[1],
            q1[3] * q2[1] + q1[0] * q2[2] - q1[1] * q2[3] - q1[2] * q2[0],
            q1[3] * q2[2] - q1[0] * q2[1] + q1[1] * q2[0] - q1[2] * q2[3],
        ]
    )


####################################
# Classes and Function Definitions #
####################################

"""
Conventions used everywhere:
  * Positions and velocities are in the Phobos frame, in metres and metres/second.
  * "Radar item" = one 3x3 array from GetNearestDebrisLocations():
        item[0, :]  rock centre RELATIVE to the shuttle (m)
        item[1, 0]  distance to its surface     item[1, 1]  range rate (m/s)
        item[1, 2]  diameter (m)                item[2, 0]  time of measurement (s since sim start)
"""


# =====================================================================================
# 1. TRACKING - the radar forgets everything between readings, so we keep a list of rocks
# =====================================================================================


class Rock:
    def __init__(self, position, diameter, time):
        self.position = position  # latest ABSOLUTE position
        self.diameter = diameter
        self.velocity = None  # unknown until we have watched it for a while
        self.last_seen = time
        self.history = deque(
            maxlen=30
        )  # recent (time, position) samples; capped so memory can't grow
        self.history.append((time, position))

    def add_sighting(self, position, diameter, time):
        if time <= self.last_seen:  # the radar repeated an old reading: ignore it
            return
        self.position = position
        self.diameter = 0.8 * self.diameter + 0.2 * diameter  # smooth the noisy diameter
        self.last_seen = time
        self.history.append((time, position))
        self.update_velocity()

    def update_velocity(self):
        times = np.array([t for t, p in self.history])
        points = np.array([p for t, p in self.history])
        if len(times) < 3 or times[-1] - times[0] < 1.0:
            return  # too little data: a velocity now would be mostly noise
        # Fit a straight line through the positions. Its slope (per axis) is the velocity.
        self.velocity = np.polyfit(times - times[0], points, 1)[0]

    def predicted_position(self, time):
        if self.velocity is None:
            return self.position
        return self.position + self.velocity * (time - self.last_seen)


class Tracker:
    def __init__(self, gate=15.0, max_age=5.0):
        self.rocks = []
        self.gate = gate  # sightings closer than this (m) count as the same rock
        self.max_age = max_age  # forget rocks not seen for this long (s)

    def update(self, radar_items, shuttle_position, now, target_position):
        target_vec = target_position - shuttle_position
        target_dist = np.linalg.norm(target_vec)
        CONE_COS = np.cos(np.radians(90.0))

        def debris_in_cone(rel_pos) -> bool:
            # rel_pos is already relative to the shuttle
            d = np.linalg.norm(rel_pos)
            if d < 1e-9 or target_dist < 1e-9:
                return True  # degenerate case: keep it, safer than dropping it
            return (rel_pos @ target_vec) / (d * target_dist) > CONE_COS



        radar_items = [r for r in radar_items if debris_in_cone(r[0])]

        for item in radar_items:
            # The radar gives positions relative to the shuttle. Add the shuttle's own
            # position so that all sightings share one fixed frame.
            position = shuttle_position + item[0, :]
            diameter, time = item[1, 2], item[2, 0]
            rock = self.find_match(position, diameter, time)
            if rock is None:
                self.rocks.append(Rock(position, diameter, time))  # new rock
            else:
                rock.add_sighting(position, diameter, time)
        self.rocks = [r for r in self.rocks if now - r.last_seen < self.max_age]
        #st.OnScreenLogMessage(f"Rock info: {str(len(self.rocks))}","Rocks", st.Severity.Info)

    def find_match(self, position, diameter, time):
        """The closest existing rock that is a similar size and within the gate, else None."""
        best, best_distance = None, self.gate
        for rock in self.rocks:
            if abs(rock.diameter - diameter) > 0.25 * rock.diameter:
                continue  # sizes too different
            distance = np.linalg.norm(rock.predicted_position(time) - position)
            if distance < best_distance:
                best, best_distance = rock, distance
        return best


# =====================================================================================
# 2. AVOIDANCE - choose the velocity to fly
# =====================================================================================


def clearance(my_velocity, rel_pos, rock_vel, keep_out, horizon=50.0):
    """
    If we fly at my_velocity, how close do we get to the worst rock in the next `horizon` seconds,
    after subtracting its keep-out radius? Positive = safe, negative = collision course.
      rel_pos  (n,3)  where each rock is, relative to us
      rock_vel (n,3)  how fast each rock is moving
      keep_out (n,)   radius we want to stay outside (rock radius + safety margin)
    """
    rel_vel = rock_vel - my_velocity  # how each rock moves as seen from us
    speed_squared = (rel_vel**2).sum(axis=1) + 1e-9
    # time when the rock is closest to us (from minimising |rel_pos + t * rel_vel|)
    t = -(rel_pos * rel_vel).sum(axis=1) / speed_squared
    t = np.clip(t, 0.0, horizon)  # only look from now until the horizon
    closest = np.linalg.norm(rel_pos + t[:, None] * rel_vel, axis=1)
    
    return (closest - keep_out).min()


def two_sideways_directions(direction):
    """Two unit vectors that are perpendicular to `direction` and to each other."""
    helper = np.array([0.0, 0.0, 1.0]) if abs(direction[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    side1 = np.cross(direction, helper)
    side1 /= np.linalg.norm(side1)
    side2 = np.cross(direction, side1)
    return side1, side2

def time_to_collision(my_velocity, rel_pos, rock_vel, keep_out, horizon=20.0):
    """
    Time until we first enter the keep-out sphere of any rock, flying at my_velocity.
    Returns (time, index_of_rock), or (None, None) if no collision within the horizon.
    """
    rel_vel = rock_vel - my_velocity
    a = (rel_vel**2).sum(axis=1) + 1e-9
    b = (rel_pos * rel_vel).sum(axis=1)
    c = (rel_pos**2).sum(axis=1) - keep_out**2

    disc = b**2 - a * c
    t_hit = np.full(len(rel_pos), np.inf)

    inside = c <= 0.0                                   # already within keep-out
    t_hit[inside] = 0.0

    approaching = (~inside) & (disc >= 0.0) & (b < 0.0)  # will reach the sphere
    t_hit[approaching] = (-b[approaching] - np.sqrt(disc[approaching])) / a[approaching]

    t_hit[t_hit > horizon] = np.inf                     # beyond the horizon: ignore
    i = int(np.argmin(t_hit))
    if not np.isfinite(t_hit[i]):
        return None, None
    return float(t_hit[i]), i


class Avoider:
    def __init__(self, margin=10.0, unknown_extra=4.0, horizon=50.0, buffer=5.0):
        self.margin = margin  # metres of clearance beyond each rock's surface
        self.unknown_extra = (
            unknown_extra  # extra margin for rocks whose velocity we don't know yet
        )
        self.horizon = horizon
        self.last_dodge = None  # remembered so we don't flip-flop between dodges
        self.buffer = buffer

    def choose(self, wanted_velocity, rocks, shuttle_position, now, current_velocity):
        wanted_velocity = np.asarray(wanted_velocity, float)
        current_velocity = np.asarray(current_velocity, float)
        speed = np.linalg.norm(wanted_velocity)
        if speed < 1e-3 or len(rocks) == 0:
            return wanted_velocity

        # Describe every rock as plain arrays.
        rel_pos = np.array([r.predicted_position(now) - shuttle_position for r in rocks])
        rock_vel = np.array([r.velocity if r.velocity is not None else np.zeros(3) for r in rocks])
        keep_out = np.array(
            [
                r.diameter / 2 + self.margin + (self.unknown_extra if r.velocity is None else 0.0)
                for r in rocks
            ]
        )

        # Calculate time remaining until the predicted collision:
        t_hit, idx = time_to_collision(wanted_velocity, rel_pos, rock_vel, keep_out, self.horizon)
        if t_hit is None:
            st.OnScreenLogMessage("No collision predicted", "collision", st.Severity.Info)
        else:
            st.OnScreenLogMessage(
                f"Next collision in {t_hit:.1f}s (rock {idx}, d={rocks[idx].diameter:.1f}m)",
                "collision",
                st.Severity.Warning,
            )

        def score(velocity):
            return clearance(velocity, rel_pos, rock_vel, keep_out, self.horizon)

        if score(wanted_velocity) > 0:
            self.last_dodge = None
            return wanted_velocity

        forward = wanted_velocity / speed # Get the current unit vector of shuttle velocity
        side1, side2 = two_sideways_directions(forward)

        candidates = []
        for size in (0.05, 0.1, 0.2, 0.35, 0.5, 0.75):
            for angle in np.linspace(0, 2 * np.pi, 8, endpoint=False):
                sideways = np.cos(angle) * side1 + np.sin(angle) * side2
                for along in (-0.3, 0.0, 0.3):  # slow down / keep / speed up
                    candidates.append(wanted_velocity + speed * (size * sideways + along * forward))
        for fraction in (0.5, 0.25, 0.0):
            candidates.append(wanted_velocity * fraction)

        scored = [(c, score(c)) for c in candidates]
        safe = [c for c, s in scored if s > self.buffer]
        if safe:
            # cheapest in actual fuel: change from the velocity we have NOW
            best = min(safe, key=lambda c: np.linalg.norm(c - current_velocity))
        else:
            best = max(scored, key=lambda cs: cs[1])[0]  # least-bad

        self.last_dodge = best
        st.OnScreenLogMessage(
                f"Dodging: required delta V: {best}",
                "Dodge",
                st.Severity.Info,)
        return best


# =====================================================================================
# 3. FUEL - only fire the thrusters when the velocity error is worth a burn
# =====================================================================================


class BurnGovernor:
    def __init__(self, gain=5.0, start=0.3, stop=0.1):
        self.gain = gain  # acceleration (m/s^2) per m/s of velocity error
        self.start = start  # start burning when the error is bigger than this (m/s)
        self.stop = stop  # stop burning once the error is smaller than this (m/s)
        self.burning = False

    def acceleration(self, wanted_velocity, current_velocity):
        error = np.asarray(wanted_velocity, float) - np.asarray(current_velocity, float)
        size = np.linalg.norm(error)
        if self.burning and size < self.stop:
            self.burning = False
        elif not self.burning and size > self.start:
            self.burning = True
        return self.gain * error if self.burning else np.zeros(3)


# =====================================================================================
# 4. GOAL - the target is a SPHERE, so we only steer when our path would miss it
# =====================================================================================


class GoalPlanner:
    """
    Decides the velocity we WANT (before avoidance). Instead of constantly steering at the exact
    centre of the target, it lets us coast while our current path already passes well inside the
    goal sphere, and only steers when the path drifts towards the edge. Less steering = less fuel.
    """

    def __init__(self, cruise_speed, goal_radius=20.0, start_fraction=0.7, stop_fraction=0.3):
        self.cruise_speed = cruise_speed
        self.goal_radius = goal_radius
        self.start = (
            start_fraction * goal_radius
        )  # start steering if we would miss the centre by more than this
        self.stop = stop_fraction * goal_radius  # ...and keep steering until the miss is under this
        self.correcting = True

    def miss_distance(self, shuttle_position, shuttle_velocity, target_position):
        """How far from the target centre we would pass if we kept our current velocity."""
        speed = np.linalg.norm(shuttle_velocity)
        st.OnScreenLogMessage(f"Distance from target: {(np.linalg.norm(target_position - shuttle_position))}m", "Velocity", st.Severity.Info)
        if (speed < 0.95 * self.cruise_speed) and (np.linalg.norm(target_position) - np.linalg.norm(shuttle_position)) > 1100.0 and fuelUsed < 35.0:
            return np.inf  # not at cruise speed yet: keep accelerating
        
        heading = shuttle_velocity / speed
        to_target = target_position - shuttle_position
        along = to_target @ heading  # how far ahead the target is along our heading
        if along <= 0:
            return np.inf  # we are moving away from it
        return np.linalg.norm(to_target - along * heading)  # sideways distance at closest approach

    def wanted_velocity(self, shuttle_position, shuttle_velocity, target_position):
        miss = self.miss_distance(shuttle_position, shuttle_velocity, target_position)
        if self.correcting and miss < self.stop:
            self.correcting = False  # on a good path: coast
        elif not self.correcting and miss > self.start:
            self.correcting = True  # drifting off: steer again
        if not self.correcting:
            return np.asarray(shuttle_velocity, float)  # wanting what we have = no thruster use
        to_target = target_position - shuttle_position
        return self.cruise_speed * to_target / np.linalg.norm(to_target)


####################################
#        Main Code Start           #
####################################

tracker = Tracker()
avoider = Avoider()
governor = BurnGovernor(gain=5.0)
planner = GoalPlanner(cruise_speed=6.0)

##################
# Start the loop #
##################

iteration = 0
imageNumber = 0
exit_flag: bool = False

while not exit_flag:
    ################################
    # Wait between loop iterations #
    ################################

    time.sleep(1.0 / this.GetParam(st.VarType.double, "LoopFreqHz"))

    ###################################
    # Get elapsed time from sim start #
    ###################################

    t_now: datetime.datetime = st.SimGlobals.SimClock.GetTimeNow().as_datetime()
    t_from_start: float = (t_now - t0).total_seconds()

    #####################################
    # Example for getting vehicle state #
    #####################################

    locVelAcc = shuttle.getLocVelAcc()
    loc = locVelAcc.loc_WRT_ExprIn(phobosFrame)
    vel = locVelAcc.vel_WRT_ExprIn(phobosFrame)
    rot = shuttle.getRotation().Quat_WRT(phobosFrame)
    angvel = shuttle.getAngVelocity().WRT_ExprIn(phobosFrame)

    #####################################################
    # Example for getting additional vehicle properties #
    #####################################################

    mass = shuttle.getMass()
    inertiaBodyFrame = shuttle.getInertiaTensor(shuttleFrame)
    damage: float = shuttle.GetParam(st.VarType.double, ["Stats", "Damage"])
    fuelUsed: float = shuttle.GetParam(st.VarType.double, ["Stats", "FuelUsed"])

    ##########################################
    # Get the target's location and velocity #
    ##########################################

    targetState: st.frames.FramedLocVelAcc = target.getLocVelAcc()
    targetLoc: npt.NDArray = targetState.loc_WRT_ExprIn(phobosFrame)
    targetVel: npt.NDArray = targetState.vel_WRT_ExprIn(phobosFrame)

    ##############################
    # Calculate desired velocity #
    ##############################

    goalDirection: npt.NDArray = (targetLoc - loc) / np.linalg.norm(targetLoc - loc)
    goalSpeed_m_s: float = 6.0
    goalVelocity = planner.wanted_velocity(loc, vel, targetLoc)

    ####################################################################
    # Update tracker module with RF data and calculate a safe velocity #
    ####################################################################

    tracker.update(GetNearestDebrisLocations(150), loc, t_from_start, targetLoc)
    safe_velocity = avoider.choose(goalVelocity, tracker.rocks, loc, t_from_start, vel)
    accel = governor.acceleration(safe_velocity, vel)  # Phobos frame
    accel = st.frames.FramedVector(accel, phobosFrame).ExprIn(shuttleFrame)
    #st.OnScreenLogMessage(f"Debris info: {tracker.rocks}","tracks", st.Severity.Info)

    angularAcceleration_rad_s2 = np.array(
        [0, 0, 0]
    ) 

    SetCommand(
        accel, angularAcceleration_rad_s2
    )  # forward desired accelerations to the sim shuttle

    st.OnScreenLogMessage(
        f"Current Speed: {np.linalg.norm(vel)}\nDesired Velocity: {safe_velocity}\nFuel Used: {fuelUsed} kg",
        "Velocity",
        st.Severity.Info,
    )

    
            

    iteration += 1

st.leave_sim()
