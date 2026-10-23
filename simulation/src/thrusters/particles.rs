//! Pure visual lifecycle. Particle birth poses interpolate observed snapshots;
//! after birth, each particle moves in world space without following the body.
use crate::{domain::EntityId, model::EntityState, propulsion::AppliedJet};
use bevy::math::{DQuat, DVec3};

const MAX_PARTICLES: usize = 4_096;
const MAX_JETS: usize = 6;
const MAX_EMITTED_PER_UPDATE: usize = 128;

#[derive(Clone, Copy)]
pub(super) struct Parameters {
    pub max_particles: usize,
    pub particles_per_second_per_jet: f64,
    pub particle_lifetime_s: f64,
    pub exhaust_speed_mps: f64,
    pub particle_radius_m: f64,
    pub spread_rad: f64,
}

#[derive(Clone, Copy)]
pub(super) struct Emitter {
    id: EntityId,
    position_m: DVec3,
    rotation: DQuat,
    velocity_mps: DVec3,
    // Rapier snapshots expose angular velocity in the world frame.
    angular_velocity_world_rad_s: DVec3,
}

impl Emitter {
    pub(super) fn from_entity(ship: &EntityState) -> Self {
        Self {
            id: ship.id,
            position_m: DVec3::from_array(ship.position),
            rotation: DQuat::from_array(ship.rotation).normalize(),
            velocity_mps: DVec3::from_array(ship.velocity),
            angular_velocity_world_rad_s: DVec3::from_array(ship.angular_velocity_rad_s),
        }
    }

    fn interpolate(self, next: Self, fraction: f64) -> Self {
        Self {
            id: next.id,
            position_m: self.position_m.lerp(next.position_m, fraction),
            rotation: self.rotation.slerp(next.rotation, fraction),
            velocity_mps: self.velocity_mps.lerp(next.velocity_mps, fraction),
            angular_velocity_world_rad_s: self
                .angular_velocity_world_rad_s
                .lerp(next.angular_velocity_world_rad_s, fraction),
        }
    }

    fn world_jet(self, jet: &AppliedJet) -> Option<WorldJet> {
        let force_world = self.rotation * DVec3::from_array(jet.force_body_n);
        let outward_world = (-force_world).try_normalize()?;
        let offset_world_m = self.rotation * DVec3::from_array(jet.point_body_m);
        Some(WorldJet {
            position_m: self.position_m + offset_world_m,
            inherited_velocity_mps: self.velocity_mps
                + self.angular_velocity_world_rad_s.cross(offset_world_m),
            outward_world,
        })
    }
}

struct WorldJet {
    position_m: DVec3,
    inherited_velocity_mps: DVec3,
    outward_world: DVec3,
}

impl WorldJet {
    fn particle(self, birth_s: f64, config: Parameters, seed: u64, serial: u64) -> Particle {
        let (side, up) = cross_axes(self.outward_world);
        let radius = hash_unit(seed ^ serial.wrapping_mul(2)).sqrt() * config.spread_rad.tan();
        let angle =
            hash_unit(seed ^ serial.wrapping_mul(2).wrapping_add(1)) * std::f64::consts::TAU;
        let outward_world =
            (self.outward_world + side * (radius * angle.cos()) + up * (radius * angle.sin()))
                .normalize();
        Particle {
            birth_s,
            origin_m: self.position_m,
            velocity_world_mps: self.inherited_velocity_mps
                + outward_world * config.exhaust_speed_mps,
            outward_world,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub(super) struct Particle {
    pub birth_s: f64,
    origin_m: DVec3,
    velocity_world_mps: DVec3,
    pub outward_world: DVec3,
}

impl Particle {
    pub(super) fn position_at(&self, time_s: f64) -> DVec3 {
        self.origin_m + self.velocity_world_mps * (time_s - self.birth_s)
    }
}

#[derive(Clone, Copy)]
struct Sample {
    seed: u64,
    time_s: f64,
    emitter: Option<Emitter>,
}

pub(super) struct ParticleCloud {
    parameters: Parameters,
    particles: Vec<Particle>,
    previous: Option<Sample>,
    serial: u64,
}

impl ParticleCloud {
    pub(super) fn new(mut parameters: Parameters) -> Self {
        parameters.max_particles = parameters.max_particles.min(MAX_PARTICLES);
        Self {
            particles: Vec::with_capacity(parameters.max_particles),
            parameters,
            previous: None,
            serial: 0,
        }
    }

    pub(super) fn parameters(&self) -> Parameters {
        self.parameters
    }

    pub(super) fn particles(&self) -> &[Particle] {
        &self.particles
    }

    pub(super) fn time_s(&self) -> Option<f64> {
        self.previous.map(|sample| sample.time_s)
    }

    fn clear(&mut self) {
        self.particles.clear();
        self.serial = 0;
    }

    pub(super) fn advance(
        &mut self,
        seed: u64,
        time_s: f64,
        emitter: Option<Emitter>,
        jets: &[AppliedJet],
    ) {
        if !time_s.is_finite() {
            self.clear();
            self.previous = None;
            return;
        }
        let Some(previous) = self.previous.replace(Sample {
            seed,
            time_s,
            emitter,
        }) else {
            // Establish a baseline, never invent an interval before installation.
            return;
        };
        if seed != previous.seed
            || time_s < previous.time_s
            || emitter.map(|body| body.id) != previous.emitter.map(|body| body.id)
        {
            self.clear();
            return;
        }
        if time_s == previous.time_s {
            // Pausing freezes birth positions, particle motion and fading.
            return;
        }
        let config = self.parameters;
        self.particles
            .retain(|particle| time_s - particle.birth_s < config.particle_lifetime_s);
        let budget = (config.max_particles - self.particles.len()).min(MAX_EMITTED_PER_UPDATE);
        if budget == 0
            || jets.is_empty()
            || config.particles_per_second_per_jet <= 0.0
            || config.particle_lifetime_s <= 0.0
        {
            return;
        }
        let Some((before, current)) = previous.emitter.zip(emitter) else {
            return;
        };
        let rate = config.particles_per_second_per_jet;
        let start_s = previous.time_s.max(time_s - config.particle_lifetime_s);
        let latest_tick = (time_s * rate).floor();
        // Fixed simulation-time ticks need no accumulated emission debt. Skip
        // expired births and drop excess work instead of allocating a backlog.
        let due = (latest_tick - (start_s * rate).floor()).clamp(0.0, MAX_EMITTED_PER_UPDATE as f64)
            as usize;
        let mut emitted = 0;
        'births: for tick in 0..due {
            let birth_s = ((latest_tick - tick as f64) / rate).min(time_s);
            if birth_s <= start_s {
                continue;
            }
            let fraction = (birth_s - previous.time_s) / (time_s - previous.time_s);
            let at_birth = before.interpolate(current, fraction.clamp(0.0, 1.0));
            // Round-robin across actual jets so a long update does not spend
            // its entire emission budget on the first nozzle.
            for jet in jets.iter().take(MAX_JETS) {
                if let Some(world_jet) = at_birth.world_jet(jet) {
                    self.particles
                        .push(world_jet.particle(birth_s, config, seed, self.serial));
                    self.serial = self.serial.wrapping_add(1);
                    emitted += 1;
                    if emitted == budget {
                        break 'births;
                    }
                }
            }
        }
    }
}

pub(super) fn cross_axes(direction: DVec3) -> (DVec3, DVec3) {
    let reference = if direction.z.abs() < 0.9 {
        DVec3::Z
    } else {
        DVec3::Y
    };
    let side = direction.cross(reference).normalize();
    (side, side.cross(direction).normalize())
}

fn hash_unit(mut value: u64) -> f64 {
    // Local SplitMix-style hash, never the simulation's sensor/physics RNG.
    value = value.wrapping_add(0x9e37_79b9_7f4a_7c15);
    value = (value ^ (value >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    value = (value ^ (value >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    ((value ^ (value >> 31)) >> 11) as f64 * (1.0 / ((1_u64 << 53) as f64))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parameters() -> Parameters {
        Parameters {
            max_particles: 512,
            particles_per_second_per_jet: 90.0,
            particle_lifetime_s: 0.35,
            exhaust_speed_mps: 18.0,
            particle_radius_m: 0.12,
            spread_rad: 0.12,
        }
    }

    fn emitter() -> Emitter {
        Emitter {
            id: EntityId::Lander,
            position_m: DVec3::ZERO,
            rotation: DQuat::IDENTITY,
            velocity_mps: DVec3::ZERO,
            angular_velocity_world_rad_s: DVec3::ZERO,
        }
    }

    fn jet() -> AppliedJet {
        AppliedJet {
            point_body_m: [2.0, 0.0, 0.0],
            force_body_n: [10.0, 0.0, 0.0],
        }
    }

    fn near(actual: DVec3, expected: DVec3) {
        assert!(
            actual.distance(expected) < 1.0e-10,
            "{actual:?} != {expected:?}"
        );
    }

    #[test]
    fn nozzle_and_opposite_force_rotate_but_world_angular_velocity_does_not() {
        let body = Emitter {
            position_m: DVec3::new(10.0, 20.0, 30.0),
            rotation: DQuat::from_rotation_z(std::f64::consts::FRAC_PI_2),
            velocity_mps: DVec3::new(1.0, 2.0, 3.0),
            angular_velocity_world_rad_s: DVec3::new(2.0, 0.0, 1.0),
            ..emitter()
        };
        let world = body.world_jet(&jet()).unwrap();
        near(world.position_m, DVec3::new(10.0, 22.0, 30.0));
        near(world.outward_world, -DVec3::Y);
        near(world.inherited_velocity_mps, DVec3::new(-1.0, 2.0, 7.0));
        let mut config = parameters();
        config.spread_rad = 0.0;
        let particle = world.particle(1.0, config, 42, 0);
        near(particle.velocity_world_mps, DVec3::new(-1.0, -16.0, 7.0));
        near(particle.position_at(1.1), DVec3::new(9.9, 20.4, 30.7));
    }

    #[test]
    fn spread_is_deterministic_and_keeps_flow_opposite_the_applied_force() {
        let body = emitter();
        let mut distinct = false;
        let first = body
            .world_jet(&jet())
            .unwrap()
            .particle(0.0, parameters(), 7, 0);
        for serial in 0..128 {
            let particle = body
                .world_jet(&jet())
                .unwrap()
                .particle(0.0, parameters(), 7, serial);
            let repeat = body
                .world_jet(&jet())
                .unwrap()
                .particle(0.0, parameters(), 7, serial);
            assert_eq!(particle, repeat);
            assert!(particle.outward_world.dot(DVec3::X) < -parameters().spread_rad.cos());
            distinct |= particle.outward_world != first.outward_world;
        }
        assert!(distinct);
    }

    #[test]
    fn observed_emitter_motion_sets_birth_pose_and_does_not_drag_old_particles() {
        let mut config = parameters();
        config.particles_per_second_per_jet = 20.0;
        config.spread_rad = 0.0;
        let mut cloud = ParticleCloud::new(config);
        let before = Emitter {
            velocity_mps: DVec3::X * 4.0,
            ..emitter()
        };
        let middle = Emitter {
            position_m: DVec3::X * 0.4,
            rotation: DQuat::from_rotation_z(std::f64::consts::FRAC_PI_2),
            ..before
        };
        cloud.advance(1, 0.0, Some(before), &[jet()]);
        cloud.advance(1, 0.1, Some(middle), &[jet()]);
        let old = *cloud
            .particles
            .iter()
            .find(|particle| particle.birth_s == 0.05)
            .unwrap();
        let half_angle = DQuat::from_rotation_z(std::f64::consts::FRAC_PI_4);
        near(old.origin_m, DVec3::X * 0.2 + half_angle * DVec3::X * 2.0);
        near(old.outward_world, -(half_angle * DVec3::X));
        let later = Emitter {
            position_m: DVec3::X * 0.8,
            rotation: DQuat::from_rotation_z(std::f64::consts::PI),
            ..before
        };
        cloud.advance(1, 0.2, Some(later), &[jet()]);
        assert!(cloud.particles.contains(&old));
        near(
            old.position_at(0.2) - old.position_at(0.1),
            old.velocity_world_mps * 0.1,
        );
        let newest = cloud
            .particles
            .iter()
            .find(|particle| particle.birth_s == 0.2)
            .unwrap();
        near(newest.outward_world, DVec3::X);
    }

    #[test]
    fn zero_force_emits_nothing_and_release_keeps_only_existing_particles() {
        let mut cloud = ParticleCloud::new(parameters());
        let zero = AppliedJet {
            force_body_n: [0.0; 3],
            ..jet()
        };
        cloud.advance(1, 0.0, Some(emitter()), &[]);
        cloud.advance(1, 0.1, Some(emitter()), &[zero]);
        assert!(cloud.particles.is_empty());
        assert_eq!(cloud.serial, 0);
        cloud.advance(1, 0.2, Some(emitter()), &[jet()]);
        assert!(!cloud.particles.is_empty());
        let emitted = cloud.serial;
        let existing = cloud.particles.clone();
        cloud.advance(1, 0.21, Some(emitter()), &[]);
        assert_eq!(cloud.serial, emitted);
        assert_eq!(cloud.particles, existing);
        cloud.advance(1, 0.6, Some(emitter()), &[]);
        assert!(cloud.particles.is_empty());
    }

    #[test]
    fn allocations_live_particles_and_per_update_emissions_are_bounded() {
        let mut config = parameters();
        config.max_particles = usize::MAX;
        config.particles_per_second_per_jet = 1_000_000.0;
        let mut cloud = ParticleCloud::new(config);
        let jets: [AppliedJet; 6] = std::array::from_fn(|_| jet());
        cloud.advance(1, 0.0, Some(emitter()), &jets);
        let capacity = cloud.particles.capacity();
        assert_eq!(capacity, MAX_PARTICLES);
        for frame in 1..=100 {
            let serial = cloud.serial;
            cloud.advance(1, frame as f64 * 0.001, Some(emitter()), &jets);
            assert!(cloud.serial - serial <= MAX_EMITTED_PER_UPDATE as u64);
            assert!(cloud.particles.len() <= MAX_PARTICLES);
            assert_eq!(cloud.particles.capacity(), capacity);
        }
        assert_eq!(cloud.particles.len(), MAX_PARTICLES);
        let mut small = ParticleCloud::new(Parameters {
            max_particles: 7,
            ..config
        });
        small.advance(1, 0.0, Some(emitter()), &jets);
        small.advance(1, 0.1, Some(emitter()), &jets);
        assert_eq!(small.particles.len(), 7);
        assert_eq!(small.particles.capacity(), 7);
    }

    #[test]
    fn only_the_first_six_applied_jets_can_emit() {
        let mut cloud = ParticleCloud::new(Parameters {
            particles_per_second_per_jet: 10.0,
            ..parameters()
        });
        let jets: [AppliedJet; 10] = std::array::from_fn(|index| AppliedJet {
            point_body_m: [index as f64, 0.0, 0.0],
            ..jet()
        });
        cloud.advance(1, 0.0, Some(emitter()), &jets);
        cloud.advance(1, 0.1, Some(emitter()), &jets);
        assert_eq!(cloud.particles.len(), MAX_JETS);
        for (index, particle) in cloud.particles.iter().enumerate() {
            near(particle.origin_m, DVec3::X * index as f64);
        }
    }

    #[test]
    fn frozen_time_freezes_emission_motion_and_lifetime_even_if_pose_changes() {
        let mut cloud = ParticleCloud::new(parameters());
        cloud.advance(1, 0.0, Some(emitter()), &[jet()]);
        cloud.advance(1, 0.1, Some(emitter()), &[jet()]);
        let existing = cloud.particles.clone();
        let serial = cloud.serial;
        let positions: Vec<_> = existing
            .iter()
            .map(|particle| particle.position_at(0.1))
            .collect();
        for _ in 0..10 {
            let moved = Emitter {
                position_m: DVec3::splat(1_000.0),
                ..emitter()
            };
            cloud.advance(1, 0.1, Some(moved), &[jet()]);
            assert_eq!(cloud.particles, existing);
            assert_eq!(cloud.serial, serial);
            let now = cloud.time_s().unwrap();
            assert_eq!(
                positions,
                cloud
                    .particles
                    .iter()
                    .map(|particle| particle.position_at(now))
                    .collect::<Vec<_>>()
            );
        }
    }

    #[test]
    fn rewind_reset_seed_change_and_missing_owner_clear_visual_history() {
        let mut cloud = ParticleCloud::new(parameters());
        cloud.advance(1, 0.0, Some(emitter()), &[jet()]);
        cloud.advance(1, 0.1, Some(emitter()), &[jet()]);
        let original = cloud.particles.clone();
        cloud.advance(1, 0.0, Some(emitter()), &[jet()]);
        assert!(cloud.particles.is_empty());
        assert_eq!(cloud.serial, 0);
        cloud.advance(1, 0.1, Some(emitter()), &[jet()]);
        assert_eq!(cloud.particles, original);
        cloud.advance(2, 0.1, Some(emitter()), &[jet()]);
        assert!(cloud.particles.is_empty());
        cloud.advance(2, 0.2, Some(emitter()), &[jet()]);
        assert!(!cloud.particles.is_empty());
        cloud.advance(2, 0.21, None, &[]);
        assert!(cloud.particles.is_empty());
        assert_eq!(cloud.serial, 0);
    }

    #[test]
    fn particle_expires_at_lifetime_and_large_time_jumps_do_not_catch_up_old_births() {
        let mut config = parameters();
        config.particles_per_second_per_jet = 8.0;
        config.particle_lifetime_s = 0.25;
        let mut cloud = ParticleCloud::new(config);
        cloud.advance(1, 0.0, Some(emitter()), &[jet()]);
        cloud.advance(1, 0.125, Some(emitter()), &[jet()]);
        assert_eq!(cloud.particles.len(), 1);
        cloud.advance(1, 0.375, Some(emitter()), &[]);
        assert!(cloud.particles.is_empty());
        cloud.advance(1, 1_000.0, Some(emitter()), &[jet()]);
        assert!(cloud.particles.len() <= 2);
        assert!(
            cloud
                .particles
                .iter()
                .all(|particle| 1_000.0 - particle.birth_s < 0.25)
        );
    }

    #[test]
    fn actual_snapshot_and_simulation_rng_are_read_only_inputs_to_animation() {
        let mut simulation = crate::physics::Simulation::new_default();
        simulation.set_command([0.1, 0.0, 0.0], [0.0; 3]).unwrap();
        let mut cloud = ParticleCloud::new(parameters());
        super::super::advance_snapshot(&mut cloud, &simulation.snapshot());
        simulation.advance(0.1);
        let snapshot = simulation.snapshot();
        let before = serde_json::to_string(&snapshot).unwrap();
        super::super::advance_snapshot(&mut cloud, &snapshot);
        assert!(!cloud.particles.is_empty());
        assert_eq!(before, serde_json::to_string(&snapshot).unwrap());
        assert_eq!(
            before,
            serde_json::to_string(&simulation.snapshot()).unwrap()
        );
    }
}
