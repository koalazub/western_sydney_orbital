//! Native example controller using the positions, attitude and noisy radar that
//! the public observation provides. It does not use debris truth velocities.
use crate::{domain::EntityId, model::Snapshot, scenario::settings};
use bevy::math::{DQuat, DVec3};

pub fn approach(state: &Snapshot) -> Option<[f64; 3]> {
    let ship = state
        .assets
        .iter()
        .find(|entity| entity.id == EntityId::Lander)?;
    let target = state
        .assets
        .iter()
        .find(|entity| entity.id == state.mission.target_id)?;
    let config = settings().guidance;
    let position = DVec3::from_array(ship.position);
    let velocity = DVec3::from_array(ship.velocity);
    let target = DVec3::from_array(target.position);
    let mut acceleration =
        (target - position) * config.position_gain_s2 - velocity * config.velocity_gain_s;
    for measurement in state.observations.iter().take(24) {
        if measurement.range_m < config.avoidance_range_m {
            let relative = DVec3::from_array(measurement.relative_position_m);
            acceleration -= relative.normalize_or_zero()
                * config.avoidance_accel_mps2
                * (1.0 - measurement.range_m / config.avoidance_range_m).max(0.0);
        }
    }
    acceleration = acceleration.clamp_length_max(settings().lander.max_accel_mps2);
    let attitude = DQuat::from_array(ship.rotation).normalize();
    Some((attitude.inverse() * acceleration).to_array())
}
