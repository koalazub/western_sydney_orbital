use super::*;
use crate::scenario::{OfficialCloudSettings, OfficialDebrisClass};

/// Seed the official relative cloud once. The actor box remains axis-aligned in
/// the Phobos body frame like Spread_m; only the cloud centre is offset along the
/// local terrain normal (the target direction). No recycling or birth inlet runs.
pub(super) fn spawn(
    rng: &mut u64,
    assets: &mut Vec<EntityState>,
    experiment: ExperimentSettings,
    preset: FlightPreset,
) -> Result<(), String> {
    let cloud = &settings().official_cloud;
    if experiment.debris.count != cloud.count {
        return Err(format!(
            "official_cloud requires exactly {} bodies",
            cloud.count
        ));
    }
    let site = crate::model::landing_site_for(preset);
    let up = Vector::from_array(site.normal);
    let (north, west) = horizontal_basis(up);
    let center = Vector::from_array(site.lander_position_m) + up * cloud.vertical_offset_m;
    let (speed_min, speed_max) = cloud.drift_speed_range_mps();
    for index in 0..experiment.debris.count {
        let class = class_for_draw(cloud, uniform(rng));
        let scale = class.scale_min + (class.scale_max - class.scale_min) * uniform(rng);
        let position = center
            + Vector::X * ((uniform(rng) * 2.0 - 1.0) * cloud.half_extents_m[0])
            + Vector::Y * ((uniform(rng) * 2.0 - 1.0) * cloud.half_extents_m[1])
            + Vector::Z * ((uniform(rng) * 2.0 - 1.0) * cloud.half_extents_m[2]);
        let speed = speed_min + (speed_max - speed_min) * uniform(rng);
        let azimuth = uniform(rng) * std::f64::consts::TAU;
        let velocity = (north * azimuth.cos() - west * azimuth.sin()) * speed;
        let radius = class.radius_m(scale);
        let mass = class.mass_kg(scale, cloud.mass_scale_exponent);
        if !position.is_finite()
            || !velocity.is_finite()
            || !radius.is_finite()
            || !mass.is_finite()
            || radius <= 0.0
            || mass <= 0.0
            || radius > cloud.collision_radius_max_m + 1.0e-12
        {
            return Err("official debris cloud produced a nonfinite or oversized body".into());
        }
        let id = EntityId::Debris((index + 1) as u32);
        let mut asteroid = entity(
            id,
            EntityKind::Debris,
            position.to_array(),
            velocity.to_array(),
            radius,
            mass,
        );
        asteroid.name = format!("Debris {}", index + 1);
        asteroid.angular_velocity_rad_s = debris_birth_spin(rng);
        assets.push(asteroid);
    }
    Ok(())
}

/// The official velocity direction is tangential: cos(azimuth) north minus
/// sin(azimuth) west. `up` is the site normal; north is body +Y projected there.
pub(super) fn tangential_velocity(rng: &mut u64, up: Vector) -> [f64; 3] {
    let (north, west) = horizontal_basis(up);
    let cloud = &settings().official_cloud;
    let (speed_min, speed_max) = cloud.drift_speed_range_mps();
    let speed = speed_min + (speed_max - speed_min) * uniform(rng);
    let azimuth = uniform(rng) * std::f64::consts::TAU;
    (north * azimuth.cos() - west * azimuth.sin())
        .to_array()
        .map(|component| component * speed)
}

pub(super) fn horizontal_basis(up: Vector) -> (Vector, Vector) {
    // Body +Y is the orbit normal. A polar site falls back to body +X so the
    // tangent plane always stays finite; no orbit or terrain assumption changes.
    let pole = if up.y.abs() < 0.9 {
        Vector::Y
    } else {
        Vector::X
    };
    let north = (pole - up * up.dot(pole)).normalize();
    let west = up.cross(north).normalize();
    (north, west)
}

pub(super) fn class_for_draw(cloud: &OfficialCloudSettings, draw: f64) -> &OfficialDebrisClass {
    // Match Python's random.choices: bisect over cumulative weights, where a
    // draw exactly equal to a cumulative boundary selects the later class.
    let total: f64 = cloud.classes.iter().map(|class| class.weight).sum();
    let target = draw.clamp(0.0, 1.0) * total;
    let mut cumulative = 0.0;
    for class in &cloud.classes {
        cumulative += class.weight;
        if target < cumulative {
            return class;
        }
    }
    cloud
        .classes
        .last()
        .expect("validated official cloud has four classes")
}
