use super::*;

/// Canonical preset route, never the mutable launchpad or lander position.
#[derive(Clone, Copy, Debug)]
pub(super) struct TransitVolume {
    origin: Vector,
    basis: [Vector; 3],
    minimum: [f64; 3],
    maximum: [f64; 3],
}

impl TransitVolume {
    pub(super) fn for_preset(preset: FlightPreset) -> Self {
        use std::sync::OnceLock;
        static L1: OnceLock<TransitVolume> = OnceLock::new();
        static L2: OnceLock<TransitVolume> = OnceLock::new();
        static COMPETITION: OnceLock<TransitVolume> = OnceLock::new();
        let cache = match preset {
            FlightPreset::MarsFacingL1 => &L1,
            FlightPreset::FarSideL2 => &L2,
            FlightPreset::CompetitionReference => &COMPETITION,
        };
        *cache.get_or_init(|| {
            let origin =
                Vector::from_array(crate::model::landing_site_for(preset).lander_position_m);
            let target = Vector::from_array(super::target_position_for(preset));
            let route = target - origin;
            let along = route.normalize();
            let across = (Vector::Y - along * along.y).normalize();
            let config = settings().transit_stream;
            Self {
                origin,
                basis: [along, across, along.cross(across).normalize()],
                minimum: [
                    -config.launch_trail_m,
                    -config.transverse_half_width_m,
                    -config.transverse_half_width_m,
                ],
                maximum: [
                    route.length() + config.target_lead_m,
                    config.transverse_half_width_m,
                    config.transverse_half_width_m,
                ],
            }
        })
    }

    fn coordinates(self, position: [f64; 3]) -> [f64; 3] {
        let relative = Vector::from_array(position) - self.origin;
        self.basis.map(|axis| relative.dot(axis))
    }

    /// Wait until the complete sphere exits. A contact inside the corridor is
    /// never erased merely because a rock no longer follows the mean flow.
    pub(super) fn has_exited(self, position: [f64; 3], radius: f64) -> bool {
        self.coordinates(position)
            .into_iter()
            .enumerate()
            .any(|(axis, value)| {
                value < self.minimum[axis] - radius || value > self.maximum[axis] + radius
            })
    }

    fn world_position(self, coordinates: [f64; 3]) -> [f64; 3] {
        (self.origin
            + self.basis[0] * coordinates[0]
            + self.basis[1] * coordinates[1]
            + self.basis[2] * coordinates[2])
            .to_array()
    }

    fn admits_birth(self, position: [f64; 3], radius: f64) -> bool {
        let inset = radius + BIRTH_CLEARANCE_M;
        self.coordinates(position)
            .into_iter()
            .enumerate()
            .all(|(axis, value)| {
                value >= self.minimum[axis] + inset - 1e-8
                    && value <= self.maximum[axis] - inset + 1e-8
            })
    }

    pub(super) fn sample_volume(self, rng: &mut u64, radius: f64) -> [f64; 3] {
        let inset = radius + BIRTH_CLEARANCE_M;
        self.world_position(std::array::from_fn(|axis| {
            let low = self.minimum[axis] + inset;
            let high = self.maximum[axis] - inset;
            low + uniform(rng) * (high - low)
        }))
    }

    pub(super) fn sample_inlet(
        self,
        rng: &mut u64,
        radius: f64,
        flow: DebrisFlowTuning,
    ) -> Option<[f64; 3]> {
        let velocity = Vector::from_array(flow.common_velocity_mps());
        // Select the upstream face most nearly perpendicular to the declared
        // world-axis flow. Y/Z flow crosses this same route box, not a new orbit.
        let projection = self.basis.map(|axis| velocity.dot(axis));
        let axis = (0..3)
            .max_by(|&a, &b| projection[a].abs().total_cmp(&projection[b].abs()))
            .expect("three route axes");
        let inset = radius + BIRTH_CLEARANCE_M;
        let mut position = self.world_position(std::array::from_fn(|component| {
            let low = self.minimum[component] + inset;
            let high = self.maximum[component] - inset;
            if component == axis {
                if projection[axis] > 0.0 { low } else { high }
            } else {
                low + uniform(rng) * (high - low)
            }
        }));
        let radial = bevy::math::DVec3::from_array(position);
        if radial.length() <= crate::terrain::surface_radius_m(radial.normalize()) {
            // An outward X flow has its nominal upstream face behind the launch
            // surface. Clip this ray to the actual mesh exit instead of spawning
            // underground or changing the corridor to follow a moved pad.
            let direction = velocity.normalize();
            let ray = Ray::new(Vector::from_array(position), direction);
            let maximum = norm(sub(self.maximum, self.minimum));
            let hit =
                phobos_shape().cast_ray_and_get_normal(&Pose::identity(), &ray, maximum, false)?;
            let incidence = hit.normal.dot(direction).abs().max(0.01);
            let distance = hit.time_of_impact + inset / incidence + 1e-6;
            position = (ray.origin + direction * distance).to_array();
        }
        self.admits_birth(position, radius).then_some(position)
    }
}

impl Simulation {
    pub(super) fn recycle_transit_debris(&mut self, cfg: DebrisExperimentSettings) {
        let volume = TransitVolume::for_preset(self.state.flight_preset);
        if !self.state.assets.iter().any(|entity| {
            entity.kind == EntityKind::Debris && volume.has_exited(entity.position, entity.radius_m)
        }) {
            return;
        }
        let mut spawn_index = spawn_index(&self.state.assets, cfg.radius_max_m);
        let count = self.state.assets.len();
        let start = (self.rng % count as u64) as usize;
        let mut budget = MAX_RECYCLES_PER_STEP;
        for offset in 0..count {
            let index = (start + offset) % count;
            let entity = &self.state.assets[index];
            if entity.kind != EntityKind::Debris
                || !volume.has_exited(entity.position, entity.radius_m)
            {
                continue;
            }
            if budget == 0 {
                break;
            }
            budget -= 1;
            let id = entity.id;
            let radius = entity.radius_m;
            let Some(position) = debris_birth_position(
                &mut self.rng,
                cfg,
                radius,
                &spawn_index,
                Some(id),
                DebrisBirth::TransitInlet(volume),
            ) else {
                // Retry later without inventing a collision-free body or
                // dropping an ID when the bounded inlet search is blocked.
                continue;
            };
            let velocity = debris_birth_velocity(&mut self.rng, cfg);
            let spin = debris_birth_spin(&mut self.rng);
            spawn_index.remove(id);
            spawn_index.insert(id, position, radius);
            let body = &mut self.bodies[self.handles[&id]];
            body.set_translation(Vector::from_array(position), true);
            body.set_linvel(Vector::from_array(velocity), true);
            body.set_angvel(Vector::from_array(spin), true);
            body.reset_forces(false);
            body.reset_torques(false);
            let entity = &mut self.state.assets[index];
            entity.position = position;
            entity.velocity = velocity;
            entity.angular_velocity_rad_s = spin;
            self.touching.retain(|pair| !pair.contains(&id));
        }
        self.state.active_contacts = self.touching.iter().copied().collect();
        self.state.active_contacts.sort();
    }
}

#[cfg(test)]
#[path = "transit_tests.rs"]
mod tests;
