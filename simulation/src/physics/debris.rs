use super::*;
use crate::model::{DebrisDynamicsReport, ImpactEjectaSettings};

impl Simulation {
    /// Debug-only aggregate of actual bodies. Nonzero stored velocity does not
    /// mean playback is running, and reading this never advances the clock.
    pub fn debris_dynamics(&self) -> Result<DebrisDynamicsReport, String> {
        let mut count = 0;
        let mut moving_count = 0;
        let mut velocity_sum = Vector::ZERO;
        let mut spin_sum = 0.0;
        let mut total_mass_kg = 0.0;
        let mut min_speed: Option<f64> = None;
        let mut max_speed: Option<f64> = None;
        for (id, handle) in &self.handles {
            if !matches!(id, EntityId::Debris(_)) {
                continue;
            }
            let body = self
                .bodies
                .get(*handle)
                .filter(|body| body.is_dynamic())
                .ok_or(
                    "Debris dynamics unavailable: physical debris body missing. Reset the run.",
                )?;
            let velocity = body.linvel();
            let speed = norm(velocity.to_array());
            let spin = norm(body.angvel().to_array());
            let mass = body.mass();
            if !speed.is_finite() || !spin.is_finite() || !mass.is_finite() || mass <= 0.0 {
                return Err("Debris dynamics unavailable: nonfinite physical debris state.".into());
            }
            count += 1;
            moving_count += usize::from(speed > 0.0);
            velocity_sum += velocity;
            spin_sum += spin;
            total_mass_kg += mass;
            min_speed = Some(min_speed.map_or(speed, |minimum| minimum.min(speed)));
            max_speed = Some(max_speed.map_or(speed, |maximum| maximum.max(speed)));
        }
        if count
            != self
                .state
                .assets
                .iter()
                .filter(|asset| asset.kind == EntityKind::Debris)
                .count()
        {
            return Err("Debris dynamics unavailable: debris identities and physical bodies differ. Reset the run.".into());
        }
        Ok(DebrisDynamicsReport {
            source_time_s: self.state.time_s,
            frame: self.state.frame.clone(),
            physical_count: count,
            moving_count,
            mean_velocity_world_mps: if count == 0 {
                [0.0; 3]
            } else {
                (velocity_sum / count as f64).to_array()
            },
            min_speed_mps: min_speed,
            max_speed_mps: max_speed,
            mean_spin_rad_s: (count > 0).then(|| spin_sum / count as f64),
            total_mass_kg,
        })
    }
}

pub(super) fn unit_direction(direction: [f64; 3]) -> Vector {
    // Configuration admits any finite nonzero source, including huge components.
    let largest = direction.into_iter().map(f64::abs).fold(0.0, f64::max);
    Vector::from_array(direction.map(|component| component / largest)).normalize()
}

fn tangent_basis(normal: Vector) -> (Vector, Vector) {
    let pole = if normal.z.abs() < 0.9 {
        Vector::Z
    } else {
        Vector::Y
    };
    let first = pole.cross(normal).normalize();
    (first, normal.cross(first).normalize())
}

pub(super) fn source_direction(rng: &mut u64, impact: ImpactEjectaSettings) -> [f64; 3] {
    let center = unit_direction(impact.source_direction);
    let (first, second) = tangent_basis(center);
    let minimum_cosine = impact.patch_half_angle_deg.to_radians().cos();
    let cosine = 1.0 - uniform(rng) * (1.0 - minimum_cosine);
    let sine = (1.0 - cosine * cosine).max(0.0).sqrt();
    let bearing = uniform(rng) * std::f64::consts::TAU;
    (center * cosine + (first * bearing.cos() + second * bearing.sin()) * sine)
        .normalize()
        .to_array()
}

pub(super) fn initial_velocity(
    rng: &mut u64,
    cfg: DebrisExperimentSettings,
    position: [f64; 3],
) -> [f64; 3] {
    match cfg.distribution {
        DebrisDistribution::DirectedStream | DebrisDistribution::TransitStream => {
            debris_birth_velocity(rng, cfg)
        }
        DebrisDistribution::ImpactEjecta { settings: impact } => {
            let center = unit_direction(impact.source_direction);
            // Prefer the declared rotating frame's +Y orbit normal. The fallback
            // defines a finite initial plane for a polar source, not an orbit constraint.
            let pole = if center.y.abs() < 0.9 {
                Vector::Y
            } else {
                Vector::Z
            };
            let radial = unit_direction(position);
            let tangent = pole
                .cross(radial)
                .try_normalize()
                .unwrap_or_else(|| tangent_basis(radial).0);
            let spread =
                Vector::from_array(random_direction(rng)) * (uniform(rng) * cfg.speed_spread_mps);
            (Vector::from_array(impact.kick_velocity_mps)
                + tangent * impact.tangential_speed_mps
                + spread)
                .to_array()
        }
    }
}
