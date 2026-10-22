use crate::{domain::LagrangeRegion, scenario::Gravity};
use bevy::math::DVec3;
use serde::{Deserialize, Serialize};

/// Circular Mars–Phobos restricted three-body approximation in a Phobos-fixed
/// rotating frame. Axes: +X points from Phobos to Mars, +Y is orbit normal,
/// +Z completes the right-handed frame. SI units throughout.
///
/// Equations use the standard synodic CR3BP effective acceleration, including
/// differential Mars gravity, Coriolis and centrifugal terms. Parameters:
/// Mars GM uses JPL's Mars-system value (42,828.37362 km^3/s^2); Phobos GM
/// 707,500 m^3/s^2 and 9,376 km separation are the selected rounded model inputs
/// (JPL satellite physical parameters list Phobos GM 708,700 m^3/s^2 and mean
/// orbital elements give 9,400 km): https://ssd.jpl.nasa.gov/sats/phys_par/
/// https://ssd.jpl.nasa.gov/sats/elem/sep.html
/// This circular, point-mass model is not an ephemeris or irregular-gravity model.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Environment {
    pub mars_position_m: [f64; 3],
    pub mars_radius_m: f64,
    pub mars_mu_m3_s2: f64,
    pub phobos_mu_m3_s2: f64,
    pub angular_velocity_rad_s: [f64; 3],
    pub l1_position_m: [f64; 3],
    pub l2_position_m: [f64; 3],
    pub separation_m: f64,
}

pub fn environment(gravity: Gravity) -> Environment {
    let omega =
        ((gravity.mars_mu_m3_s2 + gravity.phobos_mu_m3_s2) / gravity.separation_m.powi(3)).sqrt();
    Environment {
        mars_position_m: [gravity.separation_m, 0.0, 0.0],
        mars_radius_m: gravity.mars_radius_m,
        mars_mu_m3_s2: gravity.mars_mu_m3_s2,
        phobos_mu_m3_s2: gravity.phobos_mu_m3_s2,
        angular_velocity_rad_s: [0.0, omega, 0.0],
        l1_position_m: [collinear_root(LagrangeRegion::L1, gravity), 0.0, 0.0],
        l2_position_m: [collinear_root(LagrangeRegion::L2, gravity), 0.0, 0.0],
        separation_m: gravity.separation_m,
    }
}

/// Acceleration in the rotating frame, for position and velocity relative to Phobos.
pub fn acceleration(position: DVec3, velocity: DVec3, gravity: Gravity) -> DVec3 {
    let mars = DVec3::new(gravity.separation_m, 0.0, 0.0);
    let delta = mars - position;
    let omega = DVec3::Y
        * ((gravity.mars_mu_m3_s2 + gravity.phobos_mu_m3_s2) / gravity.separation_m.powi(3)).sqrt();
    let phobos_radius = position.length();
    let mars_radius = delta.length();
    let phobos = if phobos_radius > 0.0 {
        -position * (gravity.phobos_mu_m3_s2 / phobos_radius.powi(3))
    } else {
        DVec3::ZERO
    };
    let mars_gravity = if mars_radius > 0.0 {
        delta / mars_radius.powi(3)
    } else {
        DVec3::ZERO
    };
    phobos + (mars_gravity - mars / gravity.separation_m.powi(3)) * gravity.mars_mu_m3_s2
        - omega.cross(omega.cross(position))
        - omega.cross(velocity) * 2.0
}

fn collinear_root(region: LagrangeRegion, gravity: Gravity) -> f64 {
    let epsilon = gravity.separation_m * 1.0e-9;
    let (mut lo, mut hi) = if region == LagrangeRegion::L1 {
        (epsilon, gravity.separation_m - epsilon)
    } else {
        (-gravity.separation_m * 0.1, -epsilon)
    };
    let f = |x: f64| acceleration(DVec3::X * x, DVec3::ZERO, gravity).x;
    let flo = f(lo);
    for _ in 0..100 {
        let mid = (lo + hi) * 0.5;
        if (f(mid) > 0.0) == (flo > 0.0) {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    (lo + hi) * 0.5
}
