//! Read-only adapter for the fields present in the public radar matrices.
//! In particular, Snapshot's analysis-only debris velocities and actor IDs are
//! not a tracking/fusion input. A range rate supplies only one velocity component.
use super::geometry::{Capsule, segment_clear};
use crate::{
    model::Snapshot,
    scenario::{PathPlanning, settings},
};
use bevy::math::DVec3;
use std::fmt;
use std::hash::{DefaultHasher, Hash, Hasher};

#[derive(Clone, Copy, Debug)]
pub(super) enum InputFailure {
    MissingVehicleOrWaypoint,
    InvalidState,
    NoRadar,
    StaleRadar,
    InvalidRadar,
    DepartureBlocked,
    GoalRegionTooTight,
    NoClearGoalApproach,
    InsideTerrain,
}

impl fmt::Display for InputFailure {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::MissingVehicleOrWaypoint => "Vehicle or waypoint unavailable. Check the input state.",
            Self::InvalidState => "Invalid position, time or geometry. Check the input values and frame.",
            Self::NoRadar => "Waiting for radar measurements. Empty input is not assumed clear.",
            Self::StaleRadar => "Radar exceeded the planning freshness limit. Waiting for newer measurements.",
            Self::InvalidRadar => "Invalid or future-dated radar. Check measurement times and values.",
            Self::DepartureBlocked => "No clear outward departure. Rechecks when inputs change.",
            Self::GoalRegionTooTight => "Waypoint region cannot fit the clearance. Review planning clearance and goal geometry.",
            Self::NoClearGoalApproach => {
                "No sampled waypoint approach is clear. Rechecks when inputs change."
            }
            Self::InsideTerrain => "Observer is inside the known terrain. Check its position and frame.",
        })
    }
}

#[derive(Clone)]
pub(super) struct PlanningFrame {
    pub time_s: f64,
    pub start: DVec3,
    pub goal: DVec3,
    pub goal_center: DVec3,
    pub goal_radius_m: f64,
    pub at_waypoint: bool,
    /// First capsule is the enclosing Phobos sphere. It is omitted only for
    /// the checked, radially outward surface-departure prefix.
    pub obstacles: Vec<Capsule>,
    pub departure: Vec<DVec3>,
    pub measured: usize,
    pub unmodelled: usize,
    pub oldest_measurement_s: Option<f64>,
    pub newest_measurement_s: Option<f64>,
    pub max_age_s: f64,
}

/// Changes only in permitted inputs can invalidate a route. Repeated radar
/// rows keep their original timestamps while forecasts advance to current time.
pub(super) fn signature(state: &Snapshot) -> u64 {
    let mut key = DefaultHasher::new();
    state.seed.hash(&mut key);
    state.time_s.to_bits().hash(&mut key);
    state
        .environment
        .mars_position_m
        .map(f64::to_bits)
        .hash(&mut key);
    state.environment.mars_radius_m.to_bits().hash(&mut key);
    for id in [state.mission.shuttle_id, state.mission.target_id] {
        if let Some(actor) = state.assets.iter().find(|actor| actor.id == id) {
            actor.position.map(f64::to_bits).hash(&mut key);
            actor.velocity.map(f64::to_bits).hash(&mut key);
            actor.radius_m.to_bits().hash(&mut key);
        }
    }
    for measurement in &state.observations {
        measurement
            .relative_position_m
            .map(f64::to_bits)
            .hash(&mut key);
        measurement.range_rate_mps.to_bits().hash(&mut key);
        measurement.diameter_m.to_bits().hash(&mut key);
        measurement.measurement_time_s.to_bits().hash(&mut key);
    }
    key.finish()
}

pub(super) fn prepare(
    state: &Snapshot,
    config: PathPlanning,
    body_radius_m: f64,
) -> Result<PlanningFrame, InputFailure> {
    let ship = state
        .assets
        .iter()
        .find(|asset| asset.id == state.mission.shuttle_id)
        .ok_or(InputFailure::MissingVehicleOrWaypoint)?;
    let target = state
        .assets
        .iter()
        .find(|asset| asset.id == state.mission.target_id)
        .ok_or(InputFailure::MissingVehicleOrWaypoint)?;
    let position = DVec3::from_array(ship.position);
    let velocity = DVec3::from_array(ship.velocity);
    let center = DVec3::from_array(target.position);
    let goal_radius_m = settings().mission.arrival_radius_m;
    if !position.is_finite()
        || !velocity.is_finite()
        || !center.is_finite()
        || !state.time_s.is_finite()
        || state.time_s < 0.0
        || !ship.radius_m.is_finite()
        || ship.radius_m <= 0.0
        || !target.radius_m.is_finite()
        || target.radius_m <= 0.0
        || !body_radius_m.is_finite()
        || body_radius_m <= 0.0
    {
        return Err(InputFailure::InvalidState);
    }
    let clearance = ship.radius_m + config.clearance_m;
    if goal_radius_m <= target.radius_m + clearance + 0.01 {
        return Err(InputFailure::GoalRegionTooTight);
    }
    let at_waypoint = position.distance(center) <= goal_radius_m;
    let approach = (position - center).normalize_or(DVec3::X);
    let mut goal = center + approach * (goal_radius_m - 0.01);
    let body = Capsule {
        from: DVec3::ZERO,
        to: DVec3::ZERO,
        radius_m: body_radius_m + clearance,
    };
    let mars = DVec3::from_array(state.environment.mars_position_m);
    if !mars.is_finite()
        || !state.environment.mars_radius_m.is_finite()
        || state.environment.mars_radius_m <= 0.0
    {
        return Err(InputFailure::InvalidState);
    }
    let mut obstacles = Vec::with_capacity(state.observations.len() + 3);
    obstacles.extend([
        body,
        Capsule {
            from: mars,
            to: mars,
            radius_m: state.environment.mars_radius_m + clearance,
        },
        Capsule {
            from: center,
            to: center,
            radius_m: target.radius_m + clearance,
        },
    ]);
    let mut oldest = None::<f64>;
    let mut newest = None::<f64>;
    let mut max_age_s = 0.0_f64;
    let size_uncertainty = config.sigma_multiplier * settings().debris.diameter_noise_fraction;
    if !(0.0..1.0).contains(&size_uncertainty) {
        return Err(InputFailure::InvalidRadar);
    }
    for measurement in &state.observations {
        let delta = DVec3::from_array(measurement.relative_position_m);
        if !delta.is_finite()
            || !measurement.range_rate_mps.is_finite()
            || !measurement.diameter_m.is_finite()
            || measurement.diameter_m <= 0.0
            || !measurement.measurement_time_s.is_finite()
            || measurement.measurement_time_s < 0.0
            || measurement.measurement_time_s > state.time_s + 1e-6
        {
            return Err(InputFailure::InvalidRadar);
        }
        let age = (state.time_s - measurement.measurement_time_s).max(0.0);
        if age > config.max_measurement_age_s && !at_waypoint {
            return Err(InputFailure::StaleRadar);
        }
        oldest = Some(oldest.map_or(measurement.measurement_time_s, |t| {
            t.min(measurement.measurement_time_s)
        }));
        newest = Some(newest.map_or(measurement.measurement_time_s, |t| {
            t.max(measurement.measurement_time_s)
        }));
        max_age_s = max_age_s.max(age);
        let direction = delta.normalize_or_zero();
        // Remove the observer's radial motion. The tangential component remains
        // unknown; do not substitute the private truth velocity from Snapshot.
        let radial_velocity = direction * (measurement.range_rate_mps + velocity.dot(direction));
        let observer_at_measurement = position - velocity * age;
        let measured_center = observer_at_measurement + delta;
        let from = measured_center + radial_velocity * age;
        let to = from + radial_velocity * config.forecast_horizon_s;
        // A thrust-sized allowance for backdating the observer, not a bound
        // on gravity, contacts or future asteroid accelerations.
        let observer_accel_allowance = settings()
            .lander
            .max_accel_mps2
            .max(settings().practice.space_lift_accel_mps2);
        let uncertainty = config.sigma_multiplier * settings().sensors.range_noise_m
            + 0.5 * observer_accel_allowance * age * age
            + (config.unknown_tangential_speed_mps
                + config.sigma_multiplier * settings().sensors.range_rate_noise_mps
                + observer_accel_allowance * age)
                * (age + config.forecast_horizon_s);
        // Diameter noise is relative to the unknown true size. Invert the
        // lower measurement factor instead of adding a fraction of the reading.
        let radius =
            measurement.diameter_m * 0.5 / (1.0 - size_uncertainty) + clearance + uncertainty;
        if !from.is_finite() || !to.is_finite() || !radius.is_finite() {
            return Err(InputFailure::InvalidRadar);
        }
        obstacles.push(Capsule {
            from,
            to,
            radius_m: radius,
        });
    }
    let measured = state.observations.len();
    if measured == 0 && !at_waypoint {
        return Err(InputFailure::NoRadar);
    }
    if !at_waypoint {
        let side = approach.cross(DVec3::Y).try_normalize().unwrap_or(DVec3::Z);
        let up = approach.cross(side).normalize();
        goal = [
            approach,
            approach + side,
            approach - side,
            approach + up,
            approach - up,
            side,
            -side,
            up,
            -up,
            -approach,
        ]
        .into_iter()
        .map(|direction| center + direction.normalize() * (goal_radius_m - 0.01))
        .filter(|point| segment_clear(*point, *point, &obstacles))
        .min_by(|a, b| {
            a.distance_squared(position)
                .total_cmp(&b.distance_squared(position))
        })
        .ok_or(InputFailure::NoClearGoalApproach)?;
    }
    let mut start = position;
    let mut departure = vec![position];
    if !at_waypoint && position.length() <= body.radius_m {
        let direction = position.normalize_or_zero();
        if direction == DVec3::ZERO
            || position.length() < crate::terrain::surface_radius_m(direction)
        {
            return Err(InputFailure::InsideTerrain);
        }
        // The vehicle can start landed, inside a conservative enclosing sphere.
        // Preview an outward-only departure; this does not launch or command it.
        start = direction * (body.radius_m + config.cell_size_m);
        if !segment_clear(position, start, &obstacles[1..]) {
            return Err(InputFailure::DepartureBlocked);
        }
        departure.push(start);
    }
    Ok(PlanningFrame {
        time_s: state.time_s,
        start,
        goal,
        goal_center: center,
        goal_radius_m,
        at_waypoint,
        obstacles,
        departure,
        measured,
        unmodelled: settings().debris.count.saturating_sub(measured),
        oldest_measurement_s: oldest,
        newest_measurement_s: newest,
        max_age_s,
    })
}

/// Attach a cached candidate to the new observer position and check every edge
/// against the newest available envelopes before drawing it again.
pub(super) fn reconnect(frame: &PlanningFrame, route: &[DVec3]) -> Option<Vec<DVec3>> {
    let last = *route.last()?;
    if route.len() < 2 || last.distance(frame.goal_center) > frame.goal_radius_m + 1e-6 {
        return None;
    }
    let mut nearest = (f64::INFINITY, 1_usize);
    for (index, edge) in route.windows(2).enumerate() {
        let delta = edge[1] - edge[0];
        let t = if delta.length_squared() > 0.0 {
            ((frame.start - edge[0]).dot(delta) / delta.length_squared()).clamp(0.0, 1.0)
        } else {
            0.0
        };
        let distance = frame.start.distance_squared(edge[0] + delta * t);
        if distance < nearest.0 {
            nearest = (distance, index + 1);
        }
    }
    let mut points = frame.departure.clone();
    points.extend_from_slice(&route[nearest.1..]);
    let core_start = frame.departure.len() - 1;
    if points[core_start..]
        .windows(2)
        .any(|edge| !segment_clear(edge[0], edge[1], &frame.obstacles))
    {
        return None;
    }
    Some(points)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn body_bound() -> f64 {
        crate::model::phobos_geometry()
            .0
            .into_iter()
            .map(|point| DVec3::from_array(point).length())
            .fold(0.0, f64::max)
    }

    #[test]
    fn preparing_the_actual_radar_snapshot_does_not_mutate_the_simulation() {
        let simulation = crate::physics::Simulation::new_default();
        let before = serde_json::to_string(&simulation.snapshot()).unwrap();
        let frame = prepare(
            &simulation.snapshot(),
            settings().path_planning,
            body_bound(),
        )
        .unwrap();
        assert_eq!(frame.measured, settings().debris.radar_count);
        assert_eq!(
            frame.unmodelled,
            settings().debris.count - settings().debris.radar_count
        );
        assert!(frame.start.length() > body_bound());
        assert_eq!(
            before,
            serde_json::to_string(&simulation.snapshot()).unwrap()
        );
    }

    #[test]
    fn repeated_readings_keep_their_measurement_time_and_age() {
        let simulation = crate::physics::Simulation::new_default();
        let mut snapshot = simulation.snapshot();
        snapshot.time_s = 1.8;
        let frame = prepare(&snapshot, settings().path_planning, body_bound()).unwrap();
        assert_eq!(frame.newest_measurement_s, Some(0.0));
        assert_eq!(frame.oldest_measurement_s, Some(0.0));
        assert_eq!(frame.max_age_s, 1.8);
    }

    #[test]
    fn private_debris_state_and_truth_velocity_are_not_planner_inputs() {
        let simulation = crate::physics::Simulation::new_default();
        let snapshot = simulation.snapshot();
        let original = prepare(&snapshot, settings().path_planning, body_bound()).unwrap();
        let mut changed_private_data = snapshot.clone();
        for asset in &mut changed_private_data.assets {
            if asset.kind == crate::domain::EntityKind::Debris {
                asset.position = [1e7; 3];
                asset.velocity = [1e4; 3];
            }
        }
        for measurement in &mut changed_private_data.observations {
            measurement.relative_velocity_mps = [1e4; 3];
            measurement.id = crate::domain::EntityId::Debris(500);
        }
        let projected = prepare(
            &changed_private_data,
            settings().path_planning,
            body_bound(),
        )
        .unwrap();
        assert_eq!(signature(&snapshot), signature(&changed_private_data));
        for (a, b) in original.obstacles.iter().zip(&projected.obstacles) {
            assert_eq!(a.from, b.from);
            assert_eq!(a.to, b.to);
            assert_eq!(a.radius_m, b.radius_m);
        }
    }

    #[test]
    fn missing_radar_is_unknown_not_empty_free_space() {
        let mut snapshot = crate::physics::Simulation::new_default().snapshot();
        snapshot.observations.clear();
        assert!(matches!(
            prepare(&snapshot, settings().path_planning, body_bound()),
            Err(InputFailure::NoRadar)
        ));
    }

    #[test]
    fn invalid_or_future_radar_is_not_drawn_as_a_current_candidate() {
        let original = crate::physics::Simulation::new_default().snapshot();
        let mut snapshot = original.clone();
        snapshot.observations[0].measurement_time_s = snapshot.time_s + 0.1;
        assert!(matches!(
            prepare(&snapshot, settings().path_planning, body_bound()),
            Err(InputFailure::InvalidRadar)
        ));
        snapshot = original;
        snapshot.observations[0].relative_position_m[0] = f64::NAN;
        assert!(matches!(
            prepare(&snapshot, settings().path_planning, body_bound()),
            Err(InputFailure::InvalidRadar)
        ));
    }
}
