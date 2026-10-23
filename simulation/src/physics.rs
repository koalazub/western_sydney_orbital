use crate::domain::{
    EntityId, EntityKind, EntityStatus, LagrangeRegion, MissionPhase, MissionStatus,
};
use crate::mission::{self, FlightMetrics};
use crate::model::{
    EntityState, Event, MissionState, Observation, PrimaryBody, Snapshot, ThrustApplication,
};
use crate::propulsion::{
    accel_limit, accel_within_limits, applied_jets, applied_wrench, local_max_thrust_n,
    space_shape_is_valid,
};
use crate::scenario::settings;
use rapier3d_f64::prelude::*;
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};

pub use crate::model::PhysicsCommand;

fn body() -> PrimaryBody {
    PrimaryBody {
        name: "Phobos".into(),
        radii_m: settings().gravity.phobos_radii_m,
        mu_m3_s2: settings().gravity.phobos_mu_m3_s2,
        rotation_period_s: settings().gravity.phobos_rotation_period_s,
    }
}

pub struct Simulation {
    state: Snapshot,
    pipeline: PhysicsPipeline,
    integration: IntegrationParameters,
    islands: IslandManager,
    broad: BroadPhaseBvh,
    narrow: NarrowPhase,
    bodies: RigidBodySet,
    colliders: ColliderSet,
    impulse: ImpulseJointSet,
    multibody: MultibodyJointSet,
    soft: SoftBodySet,
    ccd: CCDSolver,
    handles: HashMap<EntityId, RigidBodyHandle>,
    collider_ids: HashMap<ColliderHandle, EntityId>,
    touching: HashSet<[EntityId; 2]>,
    rng: u64,
}

impl Simulation {
    pub fn new_default() -> Self {
        Self::new(settings().default_seed)
    }
    pub fn new(seed: u64) -> Self {
        let mut sim = Self::from_state(initial(seed));
        sim.sample_radar();
        sim.state.rng_state = sim.rng;
        sim
    }
    fn from_state(mut state: Snapshot) -> Self {
        if let Some(lander) = state.assets.iter_mut().find(|e| e.id == EntityId::Lander) {
            if has_legacy_lander_thrust_cap(lander) {
                lander.max_thrust_n = local_max_thrust_n();
            }
            if lander.thrust_application == ThrustApplication::CenterOfMass {
                // Older center snapshots can omit command/torque fields. Preserve
                // their command-driven dynamics and reconcile the reported wrench.
                let cfg = settings().lander;
                let a = lander.commanded_angular_accel_rad_s2;
                let i = cfg.inertia_kg_m2;
                lander.thrust_n = scale(lander.commanded_accel_mps2, cfg.mass_kg);
                lander.torque_nm = [dot(i[0], a), dot(i[1], a), dot(i[2], a)];
            }
        }
        let rng = if state.rng_state == 0 {
            state.seed.max(1)
        } else {
            state.rng_state
        };
        let touching = state.active_contacts.iter().copied().collect();
        let mut sim = Self {
            rng,
            state,
            pipeline: PhysicsPipeline::new(),
            integration: IntegrationParameters::default(),
            islands: IslandManager::new(),
            broad: BroadPhaseBvh::new(),
            narrow: NarrowPhase::new(),
            bodies: RigidBodySet::new(),
            colliders: ColliderSet::new(),
            impulse: ImpulseJointSet::new(),
            multibody: MultibodyJointSet::new(),
            soft: SoftBodySet::new(),
            ccd: CCDSolver::new(),
            handles: HashMap::new(),
            collider_ids: HashMap::new(),
            touching,
        };
        sim.integration.dt = settings().integration.fixed_step_s;
        for e in &sim.state.assets {
            let p = Vector::new(e.position[0], e.position[1], e.position[2]);
            let q = Rotation::from_xyzw(e.rotation[0], e.rotation[1], e.rotation[2], e.rotation[3]);
            let mut builder = match e.kind {
                EntityKind::LandingPad => RigidBodyBuilder::fixed(),
                EntityKind::ReturnShip => RigidBodyBuilder::kinematic_position_based(),
                EntityKind::Lander | EntityKind::Debris => RigidBodyBuilder::dynamic(),
            };
            builder = builder
                .pose(Pose::from_parts(p, q))
                .linvel(Vector::new(e.velocity[0], e.velocity[1], e.velocity[2]))
                .angvel(Vector::new(
                    e.angular_velocity_rad_s[0],
                    e.angular_velocity_rad_s[1],
                    e.angular_velocity_rad_s[2],
                ))
                .ccd_enabled(true);
            let h = sim.bodies.insert(builder.build());
            let shape = match e.kind {
                EntityKind::Lander => shuttle_shape(),
                EntityKind::LandingPad => {
                    let p = settings().pad.half_extents_m;
                    SharedShape::cuboid(p[0], p[1], p[2])
                }
                EntityKind::ReturnShip | EntityKind::Debris => SharedShape::ball(e.radius_m),
            };
            let mut collider = ColliderBuilder::new(shape)
                .friction(settings().contacts.friction)
                .restitution(settings().contacts.restitution)
                .active_events(ActiveEvents::COLLISION_EVENTS);
            if e.kind == EntityKind::Lander {
                let i = settings().lander.inertia_kg_m2;
                let inertia = Matrix::from_cols(
                    Vector::new(i[0][0], i[1][0], i[2][0]),
                    Vector::new(i[0][1], i[1][1], i[2][1]),
                    Vector::new(i[0][2], i[1][2], i[2][2]),
                );
                collider = collider.mass_properties(MassProperties::with_inertia_matrix(
                    Vector::ZERO,
                    settings().lander.mass_kg,
                    inertia,
                ));
            } else if matches!(e.kind, EntityKind::Debris) {
                collider = collider.mass(e.mass_kg);
            }
            let c = sim
                .colliders
                .insert_with_parent(collider.build(), h, &mut sim.bodies);
            sim.handles.insert(e.id, h);
            sim.collider_ids.insert(c, e.id);
        }
        // The renderer and collider use the same independently generated terrain mesh.
        let phobos = sim.bodies.insert(RigidBodyBuilder::fixed().build());
        let collider = sim.colliders.insert_with_parent(
            ColliderBuilder::new(phobos_shape())
                .friction(settings().contacts.friction)
                .restitution(settings().contacts.restitution)
                .active_events(ActiveEvents::COLLISION_EVENTS)
                .build(),
            phobos,
            &mut sim.bodies,
        );
        sim.handles.insert(EntityId::Phobos, phobos);
        sim.collider_ids.insert(collider, EntityId::Phobos);
        sim
    }
    pub fn snapshot(&self) -> Snapshot {
        self.state.clone()
    }
    pub fn mission_status(&self) -> MissionStatus {
        self.state.mission.status
    }
    pub fn observation(&self) -> Value {
        let shuttle = self
            .state
            .assets
            .iter()
            .find(|e| e.id == self.state.mission.shuttle_id);
        let target = self
            .state
            .assets
            .iter()
            .find(|e| e.id == self.state.mission.target_id);
        let radar: Vec<Value> = self
            .radar(settings().debris.radar_count)
            .iter()
            .map(|o| {
                json!([
                    o.relative_position_m,
                    [o.range_m, o.range_rate_mps, o.diameter_m],
                    [o.measurement_time_s, 0.0, 0.0],
                ])
            })
            .collect();
        json!({"time_s":self.state.time_s,"seed":self.state.seed,"frame":self.state.frame,"environment":self.state.environment,"shuttle":shuttle,"target":target,"mission":self.state.mission,"radar":radar,"fuel_unlimited":settings().lander.fuel_unlimited,"max_accel_mps2":settings().lander.max_accel_mps2,"max_angular_accel_rad_s2":settings().lander.max_angular_accel_rad_s2})
    }
    pub fn radar(&self, n: usize) -> Vec<Observation> {
        self.state
            .observations
            .iter()
            .take(n.min(settings().debris.radar_count))
            .cloned()
            .collect()
    }
    pub fn set_command(&mut self, accel: [f64; 3], angular_accel: [f64; 3]) -> Result<(), String> {
        let cfg = settings().lander;
        if !accel
            .iter()
            .chain(angular_accel.iter())
            .all(|x| x.is_finite())
        {
            return Err("command values must be finite".into());
        }
        let a = clamp_vec(accel, cfg.max_accel_mps2);
        let aa = clamp_vec(angular_accel, cfg.max_angular_accel_rad_s2);
        self.apply_command(a, aa, ThrustApplication::CenterOfMass)
    }
    fn apply_command(
        &mut self,
        accel: [f64; 3],
        angular_accel: [f64; 3],
        application: ThrustApplication,
    ) -> Result<(), String> {
        if self.state.mission.status.is_terminal()
            && (norm(accel) > 0.0 || norm(angular_accel) > 0.0)
        {
            return Err("Mission finished. Reset before commanding another flight.".into());
        }
        let cfg = settings().lander;
        let e = self
            .state
            .assets
            .iter_mut()
            .find(|e| e.id == EntityId::Lander)
            .ok_or("controlled Shuttle missing")?;
        e.commanded_accel_mps2 = accel;
        e.commanded_angular_accel_rad_s2 = angular_accel;
        e.thrust_application = application;
        e.thrust_n = scale(accel, cfg.mass_kg);
        e.torque_nm = match application {
            ThrustApplication::CenterOfMass => {
                let i = cfg.inertia_kg_m2;
                [
                    dot(i[0], angular_accel),
                    dot(i[1], angular_accel),
                    dot(i[2], angular_accel),
                ]
            }
            ThrustApplication::ManualNozzles | ThrustApplication::PracticeBoost => {
                applied_wrench(&applied_jets(e)).torque_body_nm
            }
        };
        e.status = if norm(accel) > 0.0 {
            EntityStatus::Thrusting
        } else if norm(angular_accel) > 0.0 {
            EntityStatus::Rotating
        } else {
            EntityStatus::Coasting
        };
        Ok(())
    }
    pub(crate) fn set_manual_thrust(&mut self, accel: [f64; 3]) -> Result<(), String> {
        // Reuse the API's finite checks, norm limit and terminal-mission guard.
        // A manual translation replaces, rather than inherits, angular control.
        self.set_command(accel, [0.0; 3])?;
        let lander = self
            .state
            .assets
            .iter_mut()
            .find(|e| e.id == EntityId::Lander)
            .ok_or("controlled Shuttle missing")?;
        lander.thrust_application = ThrustApplication::ManualNozzles;
        lander.torque_nm = applied_wrench(&applied_jets(lander)).torque_body_nm;
        Ok(())
    }
    pub(crate) fn set_space_thrust(&mut self, accel: [f64; 3]) -> Result<(), String> {
        // The input adapter has already scaled positive body-Z. This method must
        // never amplify horizontal motion or admit a boosted downward request.
        if !accel.iter().all(|v| v.is_finite()) {
            return Err("command values must be finite".into());
        }
        if !space_shape_is_valid(accel) {
            return Err(format!(
                "practice Space thrust requires nonnegative body-Z and horizontal acceleration at most {} m/s²",
                settings().lander.max_accel_mps2
            ));
        }
        self.apply_command(
            clamp_vec(accel, accel_limit(ThrustApplication::PracticeBoost)),
            [0.0; 3],
            ThrustApplication::PracticeBoost,
        )
    }
    pub fn execute(&mut self, command: PhysicsCommand) -> Result<Snapshot, String> {
        match command {
            PhysicsCommand::Reset { seed } => {
                let seed = seed.unwrap_or(settings().default_seed);
                if seed > u32::MAX as u64 {
                    return Err("seed must be a whole number from 0 to 4294967295".into());
                }
                *self = Self::new(seed);
            }
            PhysicsCommand::Observe => {}
            PhysicsCommand::Restore { state } => {
                validate(&state)?;
                *self = Self::from_state(*state);
            }
            PhysicsCommand::Step { seconds } => {
                if !(0.0..=settings().integration.maximum_command_step_s).contains(&seconds) {
                    return Err(format!(
                        "seconds must be between 0 and {}",
                        settings().integration.maximum_command_step_s
                    ));
                }
                self.advance(seconds)
            }
            PhysicsCommand::SetCommand {
                accel,
                angular_accel,
            } => self.set_command(accel, angular_accel)?,
            PhysicsCommand::Stop => {
                self.set_command([0.0; 3], [0.0; 3])?;
            }
        }
        Ok(self.snapshot())
    }
    pub(crate) fn advance(&mut self, seconds: f64) {
        if !seconds.is_finite() || seconds <= 0.0 {
            return;
        }
        let seconds = seconds.min(settings().integration.maximum_command_step_s);
        let dt = settings().integration.fixed_step_s;
        let n = (seconds / dt).ceil().max(1.0) as usize;
        let h = seconds / n as f64;
        self.integration.dt = h;
        for _ in 0..n {
            if self.state.mission.status.is_terminal() {
                break;
            }
            for e in &self.state.assets {
                if let Some(handle) = self.handles.get(&e.id)
                    && let Some(rb) = self.bodies.get_mut(*handle)
                {
                    // Rapier keeps user forces across steps. Replacing them avoids
                    // accumulating gravity and commanded acceleration every substep.
                    rb.reset_forces(false);
                    rb.reset_torques(false);
                    if e.id == EntityId::Lander {
                        let cfg = settings().lander;
                        let q = *rb.rotation();
                        let fuel_force_n = match e.thrust_application {
                            ThrustApplication::CenterOfMass => {
                                let accel = Vector::from_array(e.commanded_accel_mps2);
                                rb.add_force(q * accel * rb.mass(), true);
                                accel.length() * cfg.mass_kg
                            }
                            ThrustApplication::ManualNozzles | ThrustApplication::PracticeBoost => {
                                let jets = applied_jets(e);
                                for jet in &jets {
                                    let force_world = q * Vector::from_array(jet.force_body_n);
                                    let point_world = rb
                                        .position()
                                        .transform_point(Vector::from_array(jet.point_body_m));
                                    rb.add_force_at_point(force_world, point_world, true);
                                }
                                applied_wrench(&jets).propellant_force_n
                            }
                        };
                        // add_force_at_point already supplies every r × F moment.
                        // Only an explicit angular request contributes direct torque.
                        let x = e.commanded_angular_accel_rad_s2;
                        let i = cfg.inertia_kg_m2;
                        let torque = q * Vector::new(dot(i[0], x), dot(i[1], x), dot(i[2], x));
                        rb.add_torque(torque, true);
                        self.state.mission.fuel_used_kg +=
                            fuel_force_n * h / (cfg.specific_impulse_s * cfg.standard_gravity_mps2);
                    }
                    if matches!(e.kind, EntityKind::Lander | EntityKind::Debris) {
                        let p = rb.translation();
                        let vel = rb.linvel();
                        let gravity = crate::astrophysics::acceleration(p, vel, settings().gravity);
                        rb.add_force(gravity * rb.mass(), true);
                    }
                }
            }
            self.pipeline.step(
                Vector::ZERO,
                &self.integration,
                &mut self.islands,
                &mut self.broad,
                &mut self.narrow,
                &mut self.bodies,
                &mut self.colliders,
                &mut self.impulse,
                &mut self.multibody,
                &mut self.soft,
                &mut self.ccd,
                &(),
                &(),
            );
            let mut current = HashSet::new();
            for pair in self.narrow.contact_pairs() {
                if pair.has_any_active_contact()
                    && let (Some(&a), Some(&b)) = (
                        self.collider_ids.get(&pair.collider1),
                        self.collider_ids.get(&pair.collider2),
                    )
                {
                    let key = pair_key(a, b);
                    current.insert(key);
                    if !self.touching.contains(&key) {
                        self.state.collisions = self.state.collisions.saturating_add(1);
                        event(
                            &mut self.state,
                            a,
                            format!("{a} / {b}: practice closing-energy contact"),
                        );
                        if a == EntityId::Lander || b == EntityId::Lander {
                            let other = if a == EntityId::Lander { b } else { a };
                            let ship_v = self
                                .state
                                .assets
                                .iter()
                                .find(|e| e.id == EntityId::Lander)
                                .map(|e| Vector::new(e.velocity[0], e.velocity[1], e.velocity[2]))
                                .unwrap_or(Vector::ZERO);
                            let other_entity = self.state.assets.iter().find(|e| e.id == other);
                            let other_v = other_entity
                                .map(|e| Vector::new(e.velocity[0], e.velocity[1], e.velocity[2]))
                                .unwrap_or(Vector::ZERO);
                            let m2 = other_entity.map(|e| e.mass_kg).unwrap_or(f64::INFINITY);
                            let reduced = if m2.is_finite() {
                                settings().lander.mass_kg * m2 / (settings().lander.mass_kg + m2)
                            } else {
                                settings().lander.mass_kg
                            };
                            let relative = if a == EntityId::Lander {
                                ship_v - other_v
                            } else {
                                other_v - ship_v
                            };
                            let closing_speed = pair
                                .manifolds()
                                .iter()
                                .map(|manifold| relative.dot(manifold.data.normal).max(0.0))
                                .fold(0.0_f64, f64::max);
                            self.state.mission.damage += 0.5 * reduced * closing_speed.powi(2);
                        }
                    }
                }
            }
            self.touching = current;
            self.state.active_contacts = self.touching.iter().cloned().collect();
            self.state.active_contacts.sort();
            for e in &mut self.state.assets {
                if let Some(hd) = self.handles.get(&e.id)
                    && let Some(rb) = self.bodies.get(*hd)
                {
                    let p = rb.translation();
                    e.position = [p.x, p.y, p.z];
                    let v = rb.linvel();
                    e.velocity = [v.x, v.y, v.z];
                    let q = rb.rotation();
                    e.rotation = [q.x, q.y, q.z, q.w];
                    let w = rb.angvel();
                    e.angular_velocity_rad_s = [w.x, w.y, w.z];
                }
            }
            let before = self.state.time_s;
            self.state.time_s += h;
            self.update_mission();
            let cadence = settings().sensors.cadence_hz;
            if (self.state.time_s * cadence).floor() > (before * cadence).floor() {
                self.sample_radar();
                self.state.rng_state = self.rng;
            }
        }
    }

    pub(crate) fn guidance_command(&self) -> Option<[f64; 3]> {
        crate::guidance::approach(&self.state)
    }
    fn update_mission(&mut self) {
        let (lander_position, lander_velocity) = self
            .state
            .assets
            .iter()
            .find(|e| e.id == EntityId::Lander)
            .map(|e| (e.position, e.velocity))
            .expect("scenario has a lander");
        let (target_position, target_velocity) = self
            .state
            .assets
            .iter()
            .find(|e| e.id == EntityId::ReturnShip)
            .map(|e| (e.position, e.velocity))
            .expect("scenario has a target");
        let pad_position = self
            .state
            .assets
            .iter()
            .find(|e| e.id == EntityId::LandingPad)
            .map(|e| e.position)
            .expect("scenario has a pad");
        let distance = norm(sub(lander_position, target_position));
        let speed = norm(sub(lander_velocity, target_velocity));
        let assessment = mission::assess(
            &self.state.mission,
            FlightMetrics {
                distance_m: distance,
                lander_position_m: lander_position,
                pad_position_m: pad_position,
                time_s: self.state.time_s,
                damage_j: self.state.mission.damage,
                minimum_step_s: settings().integration.fixed_step_s,
                transit_x_m: -settings().debris.x_min_m,
                on_pad_radius_m: settings().pad.lander_clearance_m
                    - settings().pad.half_extents_m[2],
            },
            &settings().mission,
        );
        let changed = self.state.mission.status != assessment.status;
        self.state.mission.target_distance_m = distance;
        self.state.mission.relative_speed_mps = speed;
        self.state.mission.status = assessment.status;
        self.state.mission.phase = assessment.phase;
        self.state.mission.score = assessment.score;
        if assessment.phase == MissionPhase::OnPad
            && let Some(lander) = self
                .state
                .assets
                .iter_mut()
                .find(|e| e.id == EntityId::Lander)
            && norm(lander.commanded_accel_mps2) == 0.0
            && norm(lander.commanded_angular_accel_rad_s2) == 0.0
        {
            lander.status = EntityStatus::Landed;
        }
        if changed {
            let message = match assessment.status {
                MissionStatus::Success => format!(
                    "Lander entered the configured {:.0} m arrival region. Estimated practice run complete.",
                    settings().mission.arrival_radius_m
                ),
                MissionStatus::FailedDamage => format!(
                    "Estimated impact energy exceeded the configured {:.0} J threshold.",
                    settings().mission.impact_threshold_j
                ),
                MissionStatus::Running => String::new(),
            };
            event(&mut self.state, EntityId::Lander, message);
        }
    }

    fn sample_radar(&mut self) {
        let Some(shuttle) = self.state.assets.iter().find(|e| e.id == EntityId::Lander) else {
            return;
        };
        let cfg = settings();
        let mut rows = Vec::with_capacity(cfg.debris.radar_count);
        // The checked-in Radar system scans the first 200 of the 500 spawned
        // bodies. Preserve that published configuration discrepancy explicitly.
        for debris in self
            .state
            .assets
            .iter()
            .filter(|e| e.kind == EntityKind::Debris)
            .take(cfg.debris.radar_count)
        {
            let delta = sub(debris.position, shuttle.position);
            let range = norm(delta);
            let relative_velocity = sub(debris.velocity, shuttle.velocity);
            let position = std::array::from_fn(|i| {
                delta[i] + gaussian(&mut self.rng) * cfg.sensors.range_noise_m
            });
            let geometric_rate = if range > 0.0 {
                dot(relative_velocity, delta) / range
            } else {
                0.0
            };
            let rate = geometric_rate + gaussian(&mut self.rng) * cfg.sensors.range_rate_noise_mps;
            rows.push(Observation {
                id: debris.id,
                kind: debris.kind,
                relative_position_m: position,
                // Analysis-only truth; observation() never exposes this field.
                relative_velocity_mps: relative_velocity,
                range_m: (range - debris.radius_m
                    + gaussian(&mut self.rng) * cfg.sensors.range_noise_m)
                    .max(0.0),
                range_rate_mps: rate,
                diameter_m: (debris.radius_m
                    * 2.0
                    * (1.0 + gaussian(&mut self.rng) * cfg.debris.diameter_noise_fraction))
                    .max(cfg.sensors.minimum_reported_diameter_m),
                measurement_time_s: self.state.time_s,
            });
        }
        rows.sort_by(|a, b| a.range_m.total_cmp(&b.range_m));
        self.state.observations = rows;
    }
}

fn initial(seed: u64) -> Snapshot {
    let mut rng = seed.max(1);
    let mut assets = vec![];
    let site = crate::model::landing_site();
    let cfg = settings();
    let mut ship = entity(
        EntityId::Lander,
        EntityKind::Lander,
        site.lander_position_m,
        [0.0; 3],
        cfg.lander.proxy_radius_m,
        cfg.lander.mass_kg,
    );
    ship.name = "Lander".into();
    ship.rotation = site.rotation;
    ship.status = EntityStatus::Landed;
    assets.push(ship);
    let env = crate::astrophysics::environment(cfg.gravity);
    let target_position = match cfg.mission.target_region {
        LagrangeRegion::L1 => env.l1_position_m,
        LagrangeRegion::L2 => env.l2_position_m,
    };
    let mut target = entity(
        EntityId::ReturnShip,
        EntityKind::ReturnShip,
        target_position,
        [0.0; 3],
        cfg.mission.target_radius_m,
        cfg.mission.target_mass_kg,
    );
    target.name = cfg.mission.target_name.clone();
    assets.push(target);
    let mut pad = entity(
        EntityId::LandingPad,
        EntityKind::LandingPad,
        site.pad_position_m,
        [0.0; 3],
        cfg.pad.half_extents_m[0],
        cfg.pad.fixed_mass_kg,
    );
    pad.name = "Phobos landing pad".into();
    pad.rotation = site.rotation;
    assets.push(pad);
    // Seeded debris corridor connects the surface launch region to the L2 marker.
    for i in 0..cfg.debris.count {
        let u = uniform(&mut rng);
        let x = -cfg.debris.x_min_m - u * cfg.debris.x_span_m;
        let y = (uniform(&mut rng) * 2.0 - 1.0) * cfg.debris.y_half_width_m;
        let z = (uniform(&mut rng) * 2.0 - 1.0) * cfg.debris.z_half_width_m;
        let pos = [x, y, z];
        let tangent = Vector::new(gaussian(&mut rng), gaussian(&mut rng), gaussian(&mut rng))
            .normalize_or_zero();
        let speed = cfg.debris.speed_min_mps + uniform(&mut rng) * cfg.debris.speed_span_mps;
        let vel = [tangent.x * speed, tangent.y * speed, tangent.z * speed];
        let radius = cfg.debris.radius_min_m + uniform(&mut rng) * cfg.debris.radius_span_m;
        let mass = 4.0 / 3.0 * std::f64::consts::PI * radius.powi(3) * cfg.debris.density_kg_m3;
        let mut e = entity(
            EntityId::Debris((i + 1) as u16),
            EntityKind::Debris,
            pos,
            vel,
            radius,
            mass,
        );
        e.name = format!("Debris {}", i + 1);
        let axis = Vector::new(gaussian(&mut rng), gaussian(&mut rng), gaussian(&mut rng))
            .normalize_or_zero();
        let angular_velocity = axis * uniform(&mut rng) * cfg.debris.spin_max_rad_s;
        e.angular_velocity_rad_s = angular_velocity.to_array();
        assets.push(e);
    }
    let initial_distance = norm(sub(site.lander_position_m, target_position));
    Snapshot {
        model: cfg.description.clone(),
        time_s: 0.0,
        seed,
        frame: cfg.frame_description.clone(),
        body: body(),
        environment: env,
        assets,
        events: vec![],
        collisions: 0,
        mission: MissionState {
            shuttle_id: EntityId::Lander,
            target_id: EntityId::ReturnShip,
            target_distance_m: initial_distance,
            initial_distance_m: initial_distance,
            relative_speed_mps: 0.0,
            fuel_used_kg: 0.0,
            damage: 0.0,
            status: MissionStatus::Running,
            score: None,
            score_label: "Custom mission estimate; not official STP score".into(),
            phase: MissionPhase::OnPad,
            target_region: cfg.mission.target_region,
        },
        observations: vec![],
        rng_state: rng,
        active_contacts: vec![],
    }
}
fn entity(id: EntityId, kind: EntityKind, p: [f64; 3], v: [f64; 3], r: f64, m: f64) -> EntityState {
    EntityState {
        id,
        name: id.to_string(),
        kind,
        position: p,
        velocity: v,
        rotation: [0.0, 0.0, 0.0, 1.0],
        radius_m: r,
        mass_kg: m,
        fuel_kg: 0.0,
        max_thrust_n: if id == EntityId::Lander {
            local_max_thrust_n()
        } else {
            0.0
        },
        thrust_n: [0.0; 3],
        thrust_application: ThrustApplication::CenterOfMass,
        status: match kind {
            EntityKind::LandingPad => EntityStatus::Fixed,
            EntityKind::ReturnShip => EntityStatus::StationKeeping,
            _ => EntityStatus::Coasting,
        },
        torque_nm: [0.0; 3],
        angular_velocity_rad_s: [0.0; 3],
        commanded_accel_mps2: [0.0; 3],
        commanded_angular_accel_rad_s2: [0.0; 3],
    }
}
fn has_legacy_lander_thrust_cap(e: &EntityState) -> bool {
    // Only pre-assist lander snapshots used this exact cap. A boost-mode
    // snapshot or another actor cannot opt into migration with an arbitrary cap.
    e.id == EntityId::Lander
        && e.kind == EntityKind::Lander
        && matches!(
            e.thrust_application,
            ThrustApplication::CenterOfMass | ThrustApplication::ManualNozzles
        )
        && e.max_thrust_n == settings().lander.mass_kg * settings().lander.max_accel_mps2
}
fn validate(s: &Snapshot) -> Result<(), String> {
    if !s.time_s.is_finite()
        || s.time_s < 0.0
        || s.seed > u32::MAX as u64
        || s.assets.len() != settings().debris.count + 3
        || s.events.len() > settings().limits.events
        || s.observations.len() > settings().limits.observations
        || s.active_contacts.len() > settings().limits.contacts
        || s.body.name != "Phobos"
        || s.body.radii_m != settings().gravity.phobos_radii_m
        || s.body.mu_m3_s2 != settings().gravity.phobos_mu_m3_s2
        || s.body.rotation_period_s != settings().gravity.phobos_rotation_period_s
        || s.frame != settings().frame_description
        || s.model != settings().description
        || s.environment.mars_position_m
            != crate::astrophysics::environment(settings().gravity).mars_position_m
        || s.environment.mars_mu_m3_s2 != settings().gravity.mars_mu_m3_s2
        || s.environment.phobos_mu_m3_s2 != settings().gravity.phobos_mu_m3_s2
        || s.environment.mars_radius_m != settings().gravity.mars_radius_m
        || s.environment.separation_m != settings().gravity.separation_m
        || s.environment.angular_velocity_rad_s
            != crate::astrophysics::environment(settings().gravity).angular_velocity_rad_s
        || s.environment.l1_position_m
            != crate::astrophysics::environment(settings().gravity).l1_position_m
        || s.environment.l2_position_m
            != crate::astrophysics::environment(settings().gravity).l2_position_m
        || s.mission.shuttle_id != EntityId::Lander
        || s.mission.target_id != EntityId::ReturnShip
        || ![
            s.mission.target_distance_m,
            s.mission.initial_distance_m,
            s.mission.relative_speed_mps,
            s.mission.fuel_used_kg,
            s.mission.damage,
        ]
        .iter()
        .all(|v| v.is_finite())
        || s.mission.fuel_used_kg < 0.0
        || s.mission.damage < 0.0
        || s.mission.target_region != settings().mission.target_region
        || s.mission.score.is_some_and(|v| !v.is_finite())
    {
        return Err("invalid or client-modified fixed configuration".into());
    }
    let mut ids = HashSet::new();
    let fixed = initial(s.seed);
    if s.mission.initial_distance_m != fixed.mission.initial_distance_m {
        return Err("mission initial range differs from the configured launch and target".into());
    }
    let expected: HashMap<EntityId, &EntityState> =
        fixed.assets.iter().map(|e| (e.id, e)).collect();
    let (mut shuttle, mut target, mut pad, mut debris) = (0, 0, 0, 0);
    for e in &s.assets {
        let qn = e.rotation.iter().map(|v| v * v).sum::<f64>().sqrt();
        if !ids.insert(e.id)
            || !e
                .position
                .iter()
                .chain(e.velocity.iter())
                .chain(e.rotation.iter())
                .chain(e.thrust_n.iter())
                .chain(e.commanded_accel_mps2.iter())
                .chain(e.commanded_angular_accel_rad_s2.iter())
                .chain(e.angular_velocity_rad_s.iter())
                .chain(e.torque_nm.iter())
                .all(|v| v.is_finite())
            || e.position
                .iter()
                .any(|v| v.abs() > settings().limits.position_m)
            || e.velocity
                .iter()
                .any(|v| v.abs() > settings().limits.velocity_mps)
            || !e.mass_kg.is_finite()
            || e.mass_kg <= 0.0
            || e.mass_kg > settings().limits.mass_kg
            || !e.radius_m.is_finite()
            || e.radius_m <= 0.0
            || e.radius_m > settings().limits.radius_m
            || !(0.99..=1.01).contains(&qn)
            || !accel_within_limits(e.thrust_application, e.commanded_accel_mps2)
            || !accel_within_limits(
                e.thrust_application,
                scale(e.thrust_n, 1.0 / settings().lander.mass_kg),
            )
            || norm(e.commanded_angular_accel_rad_s2)
                > settings().lander.max_angular_accel_rad_s2 + 1e-9
            || !e.fuel_kg.is_finite()
            || e.fuel_kg < 0.0
            || !e.max_thrust_n.is_finite()
        {
            return Err(format!(
                "invalid finite state or command bounds for {}",
                e.id
            ));
        }
        let fixed = expected
            .get(&e.id)
            .ok_or_else(|| format!("unknown scene entity {}", e.id))?;
        if fixed.mass_kg != e.mass_kg
            || fixed.radius_m != e.radius_m
            || fixed.kind != e.kind
            || (fixed.max_thrust_n != e.max_thrust_n && !has_legacy_lander_thrust_cap(e))
            || fixed.name != e.name
        {
            return Err(format!(
                "fixed settings differ for {}: mass {:?} expected {:?}, radius {:?} expected {:?}, max thrust {:?} expected {:?}",
                e.id,
                e.mass_kg,
                fixed.mass_kg,
                e.radius_m,
                fixed.radius_m,
                e.max_thrust_n,
                fixed.max_thrust_n
            ));
        }
        match e.thrust_application {
            // Keep legacy center snapshots compatible, including the historically
            // optional fields. from_state reconciles reports with the commands.
            ThrustApplication::CenterOfMass => {}
            ThrustApplication::ManualNozzles | ThrustApplication::PracticeBoost => {
                if e.id != EntityId::Lander || e.kind != EntityKind::Lander {
                    return Err("only the lander can use manual or practice-boost nozzles".into());
                }
                if e.commanded_angular_accel_rad_s2 != [0.0; 3] {
                    return Err("nozzle modes cannot include a direct angular command".into());
                }
                let force = scale(e.commanded_accel_mps2, settings().lander.mass_kg);
                let wrench = applied_wrench(&applied_jets(e));
                if !vectors_match(e.thrust_n, force)
                    || !vectors_match(wrench.force_body_n, force)
                    || !vectors_match(e.torque_nm, wrench.torque_body_nm)
                {
                    return Err("nozzle wrench differs from its applied forces".into());
                }
            }
        }
        match (e.id, e.kind) {
            (EntityId::Lander, EntityKind::Lander) if e.mass_kg == settings().lander.mass_kg => {
                shuttle += 1
            }
            (EntityId::ReturnShip, EntityKind::ReturnShip)
                if e.mass_kg == settings().mission.target_mass_kg =>
            {
                target += 1
            }
            (EntityId::LandingPad, EntityKind::LandingPad)
                if e.mass_kg == settings().pad.fixed_mass_kg =>
            {
                pad += 1
            }
            (EntityId::Debris(_), EntityKind::Debris) => debris += 1,
            _ => return Err("entity violates fixed scene configuration".into()),
        }
        let valid_status = match e.kind {
            EntityKind::Lander => matches!(
                e.status,
                EntityStatus::Landed
                    | EntityStatus::Coasting
                    | EntityStatus::Thrusting
                    | EntityStatus::Rotating
            ),
            EntityKind::ReturnShip => e.status == EntityStatus::StationKeeping,
            EntityKind::LandingPad => e.status == EntityStatus::Fixed,
            EntityKind::Debris => e.status == EntityStatus::Coasting,
        };
        if !valid_status {
            return Err(format!(
                "status is incompatible with entity kind for {}",
                e.id
            ));
        }
    }
    if shuttle != 1
        || target != 1
        || pad != 1
        || debris != settings().debris.count
        || !ids.contains(&s.mission.shuttle_id)
        || !ids.contains(&s.mission.target_id)
    {
        return Err("mission entity ids or counts invalid".into());
    }
    if s.active_contacts.iter().any(|pair| {
        pair[0] >= pair[1]
            || pair
                .iter()
                .any(|id| !ids.contains(id) && *id != EntityId::Phobos)
    }) {
        return Err("invalid active contact state".into());
    }
    if s.events.iter().any(|e| {
        !e.t_s.is_finite()
            || e.t_s < 0.0
            || e.t_s > s.time_s + settings().integration.fixed_step_s
            || (!ids.contains(&e.asset) && e.asset != EntityId::Phobos)
    }) || s.observations.iter().any(|o| {
        ![
            o.range_m,
            o.range_rate_mps,
            o.diameter_m,
            o.measurement_time_s,
        ]
        .iter()
        .all(|v| v.is_finite())
            || !o
                .relative_position_m
                .iter()
                .chain(o.relative_velocity_mps.iter())
                .all(|v| v.is_finite())
            || o.range_m < 0.0
            || o.diameter_m <= 0.0
            || !ids.contains(&o.id)
            || !s
                .assets
                .iter()
                .any(|asset| asset.id == o.id && asset.kind == o.kind)
            || o.measurement_time_s < 0.0
            || o.measurement_time_s > s.time_s + settings().integration.fixed_step_s
    }) {
        return Err("invalid event or radar state".into());
    }
    Ok(())
}
fn pair_key(a: EntityId, b: EntityId) -> [EntityId; 2] {
    if a < b { [a, b] } else { [b, a] }
}
fn phobos_shape() -> SharedShape {
    let (vertices, indices) = crate::model::phobos_geometry();
    SharedShape::trimesh(
        vertices.into_iter().map(Vector::from_array).collect(),
        indices,
    )
    .expect("generated Phobos terrain is a valid triangle mesh")
}
fn shuttle_shape() -> SharedShape {
    let config = settings().lander;
    let mut parts = vec![(
        Pose::identity(),
        SharedShape::cuboid(
            config.hull_half_extents_m[0],
            config.hull_half_extents_m[1],
            config.hull_half_extents_m[2],
        ),
    )];
    let identity = Rotation::from_xyzw(0.0, 0.0, 0.0, 1.0);
    for leg in config.leg_positions_m {
        parts.push((
            Pose::from_parts(Vector::from_array(leg), identity),
            SharedShape::cuboid(
                config.leg_half_extents_m[0],
                config.leg_half_extents_m[1],
                config.leg_half_extents_m[2],
            ),
        ));
        let foot_z = leg[2] - config.leg_half_extents_m[2];
        parts.push((
            Pose::from_parts(Vector::new(leg[0], leg[1], foot_z), identity),
            SharedShape::ball(config.foot_radius_m),
        ));
    }
    SharedShape::compound(parts)
}
fn event(s: &mut Snapshot, asset: EntityId, message: impl Into<String>) {
    if s.events.len() >= settings().limits.events {
        s.events.remove(0);
    }
    s.events.push(Event {
        t_s: s.time_s,
        asset,
        message: message.into(),
    })
}
fn clamp_vec(v: [f64; 3], m: f64) -> [f64; 3] {
    let n = norm(v);
    if n > m { scale(v, m / n) } else { v }
}
fn rand(s: &mut u64) -> u64 {
    *s ^= *s << 13;
    *s ^= *s >> 7;
    *s ^= *s << 17;
    *s
}
fn uniform(s: &mut u64) -> f64 {
    ((rand(s) >> 11) as f64 + 0.5) / 9_007_199_254_740_992.0
}
fn gaussian(s: &mut u64) -> f64 {
    (-2.0 * uniform(s).ln()).sqrt() * (std::f64::consts::TAU * uniform(s)).cos()
}
fn norm(v: [f64; 3]) -> f64 {
    v[0].hypot(v[1]).hypot(v[2])
}
fn dot(a: [f64; 3], b: [f64; 3]) -> f64 {
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}
fn sub(a: [f64; 3], b: [f64; 3]) -> [f64; 3] {
    [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
}
fn scale(a: [f64; 3], k: f64) -> [f64; 3] {
    [a[0] * k, a[1] * k, a[2] * k]
}
fn vectors_match(a: [f64; 3], b: [f64; 3]) -> bool {
    a.into_iter()
        .zip(b)
        .all(|(a, b)| (a - b).abs() <= 1e-8 + b.abs() * 1e-10)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lander(sim: &Simulation) -> &EntityState {
        sim.state
            .assets
            .iter()
            .find(|e| e.id == EntityId::Lander)
            .unwrap()
    }

    fn lander_mut(state: &mut Snapshot) -> &mut EntityState {
        state
            .assets
            .iter_mut()
            .find(|e| e.id == EntityId::Lander)
            .unwrap()
    }

    fn rigid_body(sim: &Simulation) -> &RigidBody {
        &sim.bodies[sim.handles[&EntityId::Lander]]
    }

    fn rotation(e: &EntityState) -> Rotation {
        Rotation::from_xyzw(e.rotation[0], e.rotation[1], e.rotation[2], e.rotation[3])
    }

    fn angular_momentum(sim: &Simulation) -> Vector {
        let rb = rigid_body(sim);
        let q = *rb.rotation();
        let w = (q.inverse() * rb.angvel()).to_array();
        let i = settings().lander.inertia_kg_m2;
        q * Vector::new(dot(i[0], w), dot(i[1], w), dot(i[2], w))
    }

    fn assert_vector_close(actual: Vector, expected: Vector, tolerance: f64) {
        assert!(
            (actual - expected).length() <= tolerance,
            "actual {actual:?}, expected {expected:?}, tolerance {tolerance}"
        );
    }

    #[test]
    fn manual_force_at_point_rotates_the_actual_default_lander_without_doubled_torque() {
        let mut sim = Simulation::new_default();
        let before = lander(&sim).clone();
        let q = rotation(&before);
        let dt = settings().integration.fixed_step_s;
        sim.set_manual_thrust([0.0, settings().lander.max_accel_mps2, 0.0])
            .unwrap();
        let commanded = lander(&sim).clone();
        assert_eq!(commanded.position, before.position);
        assert_eq!(commanded.rotation, before.rotation);
        assert_eq!(commanded.velocity, before.velocity);
        assert_eq!(
            commanded.angular_velocity_rad_s,
            before.angular_velocity_rad_s
        );
        assert_eq!(commanded.commanded_angular_accel_rad_s2, [0.0; 3]);
        assert!(commanded.torque_nm[0] > 0.0);
        sim.advance(dt);

        let rb = rigid_body(&sim);
        assert_vector_close(
            rb.user_torque(),
            q * Vector::from_array(commanded.torque_nm),
            1e-6,
        );
        let gravity = crate::astrophysics::acceleration(
            Vector::from_array(before.position),
            Vector::from_array(before.velocity),
            settings().gravity,
        );
        assert_vector_close(
            rb.user_force(),
            q * Vector::from_array(commanded.thrust_n) + gravity * rb.mass(),
            1e-7,
        );
        let body_w = q.inverse() * rb.angvel();
        // Passive nozzle response is not limited by the direct angular-command cap.
        assert!(body_w.x > settings().lander.max_angular_accel_rad_s2 * dt * 10.0);
        assert!((q * Vector::Z).dot(*rb.rotation() * Vector::Z) < 1.0 - 1e-8);
        assert!((rb.mass() - before.mass_kg).abs() < 1e-9);
        assert!(rb.mass_properties().flags.is_empty());
    }

    #[test]
    fn holding_a_body_left_push_naturally_tips_over_on_the_real_default_pad() {
        let mut sim = Simulation::new_default();
        let up = rotation(lander(&sim)) * Vector::Z;
        let dt = settings().integration.fixed_step_s;
        let mut minimum_up_alignment = 1.0_f64;
        let mut peak_roll_rate = 0.0_f64;
        let mut touched_pad = false;
        sim.set_manual_thrust([0.0, settings().lander.max_accel_mps2, 0.0])
            .unwrap();

        // No pose, angular velocity, gravity, inertia, or contact changes. This is
        // the same sustained +Y force the A key requests on the configured pad.
        for _ in 0..(12.0 / dt).ceil() as usize {
            sim.advance(dt);
            let rb = rigid_body(&sim);
            minimum_up_alignment = minimum_up_alignment.min(up.dot(*rb.rotation() * Vector::Z));
            peak_roll_rate = peak_roll_rate.max((rb.rotation().inverse() * rb.angvel()).x.abs());
            touched_pad |= sim
                .state
                .active_contacts
                .contains(&pair_key(EntityId::Lander, EntityId::LandingPad));
            if touched_pad && minimum_up_alignment < 0.0 {
                break;
            }
        }

        assert!(
            touched_pad,
            "the actual foot/pad colliders must participate"
        );
        assert!(
            minimum_up_alignment < 0.0,
            "sustained manual force must tip body-up beyond 90 degrees; minimum dot {minimum_up_alignment}"
        );
        assert!(peak_roll_rate > settings().lander.max_angular_accel_rad_s2);
        assert_eq!(lander(&sim).commanded_angular_accel_rad_s2, [0.0; 3]);
    }

    #[test]
    fn manual_up_thrust_clears_stale_angular_control_and_has_no_jet_moment() {
        let cfg = settings().lander;
        let mut sim = Simulation::new_default();
        sim.set_command([0.0; 3], [cfg.max_angular_accel_rad_s2, 0.0, 0.0])
            .unwrap();
        sim.set_manual_thrust([0.0, 0.0, cfg.max_accel_mps2])
            .unwrap();
        let up = rotation(lander(&sim)) * Vector::Z;
        assert_eq!(
            lander(&sim).thrust_application,
            ThrustApplication::ManualNozzles
        );
        assert_eq!(lander(&sim).commanded_angular_accel_rad_s2, [0.0; 3]);
        assert_eq!(lander(&sim).torque_nm, [0.0; 3]);
        sim.advance(settings().integration.fixed_step_s);
        let rb = rigid_body(&sim);
        assert_vector_close(rb.user_torque(), Vector::ZERO, 1e-6);
        assert_vector_close(rb.angvel(), Vector::ZERO, 1e-9);
        assert!(rb.linvel().dot(up) > 0.0);
    }

    #[test]
    fn practice_space_lift_has_four_times_the_force_and_more_real_upward_velocity() {
        let cfg = settings().lander;
        let lift = settings().practice.space_lift_accel_mps2;
        assert_eq!(cfg.max_accel_mps2, 0.5);
        assert_eq!(lift, 2.0);
        let mut reference = Simulation::new_default();
        let mut boosted = Simulation::new_default();
        let before = lander(&boosted).clone();
        let mass_properties = rigid_body(&boosted).mass_properties().local_mprops;
        let up = rotation(&before) * Vector::Z;
        let gravity = crate::astrophysics::acceleration(
            Vector::from_array(before.position),
            Vector::from_array(before.velocity),
            settings().gravity,
        );
        reference
            .set_manual_thrust([0.0, 0.0, cfg.max_accel_mps2])
            .unwrap();
        boosted.set_space_thrust([0.0, 0.0, lift]).unwrap();
        assert_eq!(
            lander(&boosted).thrust_application,
            ThrustApplication::PracticeBoost
        );
        assert!(
            (lander(&boosted).thrust_n[2] / lander(&reference).thrust_n[2] - 4.0).abs() < 1e-12
        );
        assert_eq!(lander(&boosted).max_thrust_n, cfg.mass_kg * lift);
        assert_eq!(lander(&boosted).mass_kg, before.mass_kg);
        assert_eq!(lander(&boosted).torque_nm, [0.0; 3]);
        assert!(
            boosted
                .state
                .assets
                .iter()
                .filter(|e| e.id != EntityId::Lander)
                .all(|e| e.max_thrust_n == 0.0)
        );

        let dt = settings().integration.fixed_step_s;
        for _ in 0..4 {
            reference.advance(dt);
            boosted.advance(dt);
        }
        let elapsed = 4.0 * dt;
        let reference_speed = rigid_body(&reference).linvel().dot(up);
        let boosted_speed = rigid_body(&boosted).linvel().dot(up);
        assert!(reference_speed > 0.0);
        assert!(boosted_speed > reference_speed * 3.9);
        assert!(
            (boosted_speed - reference_speed - (lift - cfg.max_accel_mps2) * elapsed).abs() < 1e-3
        );
        assert!(boosted_speed >= (lift - gravity.length()) * elapsed - 1e-4);
        assert_eq!(
            rigid_body(&boosted).mass_properties().local_mprops,
            mass_properties
        );
        assert_eq!(
            rigid_body(&reference).mass_properties().local_mprops,
            mass_properties
        );
        assert_vector_close(rigid_body(&boosted).angvel(), Vector::ZERO, 1e-9);

        // The larger local hardware report never changes either reference path.
        for manual in [false, true] {
            if manual {
                boosted.set_manual_thrust([0.0, 0.0, 2.0]).unwrap();
            } else {
                boosted.set_command([0.0, 0.0, 2.0], [0.0; 3]).unwrap();
            }
            assert_eq!(lander(&boosted).commanded_accel_mps2, [0.0, 0.0, 0.5]);
            assert_eq!(lander(&boosted).thrust_n, [0.0, 0.0, cfg.mass_kg * 0.5]);
            assert_eq!(lander(&boosted).max_thrust_n, cfg.mass_kg * lift);
        }
    }

    #[test]
    fn mixed_space_lift_preserves_horizontal_force_and_applies_nozzle_torque_once() {
        let cfg = settings().lander;
        let lift = settings().practice.space_lift_accel_mps2;
        let component = cfg.max_accel_mps2 / 2.0_f64.sqrt();
        let mut sim = Simulation::new_default();
        sim.set_manual_thrust([component, 0.0, component]).unwrap();
        let reference = lander(&sim).clone();
        sim.set_command([0.0; 3], [cfg.max_angular_accel_rad_s2, 0.0, 0.0])
            .unwrap();
        sim.set_space_thrust([component, 0.0, lift / 2.0_f64.sqrt()])
            .unwrap();
        let commanded = lander(&sim).clone();
        let q = rotation(&commanded);
        assert_eq!(
            commanded.commanded_accel_mps2[0],
            reference.commanded_accel_mps2[0]
        );
        assert_eq!(commanded.thrust_n[0], reference.thrust_n[0]);
        assert_eq!(commanded.torque_nm, reference.torque_nm);
        assert_eq!(commanded.commanded_angular_accel_rad_s2, [0.0; 3]);
        assert_eq!(commanded.velocity, reference.velocity);
        assert_eq!(
            commanded.angular_velocity_rad_s,
            reference.angular_velocity_rad_s
        );
        assert!(norm(commanded.commanded_accel_mps2) < lift);
        assert!(norm(commanded.commanded_accel_mps2) > cfg.max_accel_mps2);
        let gravity = crate::astrophysics::acceleration(
            Vector::from_array(commanded.position),
            Vector::from_array(commanded.velocity),
            settings().gravity,
        );
        let dt = settings().integration.fixed_step_s;
        sim.advance(dt);
        assert_vector_close(
            rigid_body(&sim).user_force(),
            q * Vector::from_array(commanded.thrust_n) + gravity * rigid_body(&sim).mass(),
            1e-7,
        );
        assert_vector_close(
            rigid_body(&sim).user_torque(),
            q * Vector::from_array(commanded.torque_nm),
            1e-6,
        );
        let fuel_force_n = commanded.thrust_n.into_iter().map(f64::abs).sum::<f64>();
        assert!(
            (sim.state.mission.fuel_used_kg
                - fuel_force_n * dt / (cfg.specific_impulse_s * cfg.standard_gravity_mps2))
                .abs()
                < 1e-12
        );
    }

    #[test]
    fn space_inputs_cap_total_acceleration_and_reject_unsafe_shapes_without_mutation() {
        let cfg = settings().lander;
        let lift = settings().practice.space_lift_accel_mps2;
        let mut sim = Simulation::new_default();
        sim.set_space_thrust([0.0, 0.0, lift * 10.0]).unwrap();
        assert_eq!(lander(&sim).commanded_accel_mps2, [0.0, 0.0, lift]);
        sim.set_space_thrust([0.0, 0.0, f64::MAX]).unwrap();
        assert!((norm(lander(&sim).commanded_accel_mps2) - lift).abs() < 1e-12);
        sim.set_space_thrust([0.2, 0.0, lift * 2.0]).unwrap();
        assert!((norm(lander(&sim).commanded_accel_mps2) - lift).abs() < 1e-12);
        assert!(lander(&sim).commanded_accel_mps2[0] <= 0.2);
        let before = serde_json::to_value(sim.snapshot()).unwrap();
        for invalid in [
            [f64::NAN, 0.0, lift],
            [0.0, f64::INFINITY, lift],
            [0.0, 0.0, f64::NEG_INFINITY],
            [cfg.max_accel_mps2 + 0.01, 0.0, lift],
            [cfg.max_accel_mps2, cfg.max_accel_mps2, lift],
            [f64::MAX, 0.0, f64::MAX],
            [0.0, 0.0, -0.1],
            [0.0, 0.0, -lift],
        ] {
            assert!(
                sim.set_space_thrust(invalid).is_err(),
                "accepted {invalid:?}"
            );
            assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
        }
    }

    #[test]
    fn space_release_and_reference_handoffs_cancel_boost_without_braking() {
        let cfg = settings().lander;
        let lift = settings().practice.space_lift_accel_mps2;
        let dt = settings().integration.fixed_step_s;
        for handoff in 0..6 {
            let mut sim = Simulation::new_default();
            sim.set_space_thrust([
                0.0,
                cfg.max_accel_mps2 / 2.0_f64.sqrt(),
                lift / 2.0_f64.sqrt(),
            ])
            .unwrap();
            sim.advance(dt);
            let thrusting = lander(&sim).clone();
            let momentum = angular_momentum(&sim);
            assert!(momentum.length() > 0.0);
            let fuel = sim.state.mission.fuel_used_kg;
            match handoff {
                0 => {
                    sim.execute(PhysicsCommand::Stop).unwrap();
                }
                1 => sim.set_manual_thrust([0.0; 3]).unwrap(),
                2 => sim.set_space_thrust([0.0; 3]).unwrap(),
                3 => sim.set_command([0.0; 3], [0.0; 3]).unwrap(),
                4 => sim
                    .set_manual_thrust([0.0, 0.0, cfg.max_accel_mps2])
                    .unwrap(),
                5 => sim
                    .set_command([0.0, 0.0, cfg.max_accel_mps2], [0.0; 3])
                    .unwrap(),
                _ => unreachable!(),
            }
            let released = lander(&sim).clone();
            assert!(norm(released.commanded_accel_mps2) <= cfg.max_accel_mps2);
            assert_eq!(released.torque_nm, [0.0; 3]);
            assert_eq!(released.commanded_angular_accel_rad_s2, [0.0; 3]);
            assert_eq!(released.velocity, thrusting.velocity);
            assert_eq!(
                released.angular_velocity_rad_s,
                thrusting.angular_velocity_rad_s
            );
            assert_eq!(released.position, thrusting.position);
            assert_eq!(released.rotation, thrusting.rotation);
            assert_vector_close(angular_momentum(&sim), momentum, 1e-12);
            if handoff < 4 {
                assert_eq!(released.thrust_n, [0.0; 3]);
                assert!(applied_jets(&released).is_empty());
            }
            let rb = rigid_body(&sim);
            let expected_force = rotation(&released) * Vector::from_array(released.thrust_n)
                + crate::astrophysics::acceleration(
                    rb.translation(),
                    rb.linvel(),
                    settings().gravity,
                ) * rb.mass();
            sim.advance(dt);
            assert_vector_close(rigid_body(&sim).user_force(), expected_force, 1e-8);
            // A nonzero collinear nozzle force is transformed at kilometre-scale
            // world coordinates. Allow point-subtraction roundoff, but keep the
            // fully released, zero-force cases at the stricter zero tolerance.
            let torque_tolerance = if handoff < 4 { 1e-12 } else { 1e-8 };
            assert_vector_close(
                rigid_body(&sim).user_torque(),
                Vector::ZERO,
                torque_tolerance,
            );
            assert_vector_close(angular_momentum(&sim), momentum, momentum.length() * 1e-4);
            if handoff < 4 {
                assert_eq!(sim.state.mission.fuel_used_kg, fuel);
            }
            assert!(norm(lander(&sim).velocity) > 0.0);
        }
    }

    #[test]
    fn release_clears_force_and_moment_without_braking_or_erasing_angular_momentum() {
        for release_with_manual_zero in [false, true] {
            let mut sim = Simulation::new_default();
            let dt = settings().integration.fixed_step_s;
            sim.set_manual_thrust([0.0, settings().lander.max_accel_mps2, 0.0])
                .unwrap();
            sim.advance(dt);
            let thrusting = lander(&sim).clone();
            let fuel = sim.state.mission.fuel_used_kg;
            let momentum = angular_momentum(&sim);
            assert!(momentum.length() > 0.0);
            if release_with_manual_zero {
                sim.set_manual_thrust([0.0; 3]).unwrap();
            } else {
                sim.execute(PhysicsCommand::Stop).unwrap();
            }
            let released = lander(&sim);
            assert_eq!(released.thrust_n, [0.0; 3]);
            assert_eq!(released.torque_nm, [0.0; 3]);
            assert_eq!(released.commanded_accel_mps2, [0.0; 3]);
            assert_eq!(released.commanded_angular_accel_rad_s2, [0.0; 3]);
            assert_eq!(released.velocity, thrusting.velocity);
            assert_eq!(
                released.angular_velocity_rad_s,
                thrusting.angular_velocity_rad_s
            );
            assert_eq!(released.rotation, thrusting.rotation);
            assert!(applied_jets(released).is_empty());
            assert_vector_close(angular_momentum(&sim), momentum, 1e-12);
            let rb = rigid_body(&sim);
            let gravity_force = crate::astrophysics::acceleration(
                rb.translation(),
                rb.linvel(),
                settings().gravity,
            ) * rb.mass();

            // The next short step is still above the pad. Gravity changes linear
            // velocity, but no stale jet moment or braking torque can remain.
            sim.advance(dt);
            assert_eq!(sim.state.mission.fuel_used_kg, fuel);
            assert_vector_close(rigid_body(&sim).user_force(), gravity_force, 1e-9);
            assert_vector_close(rigid_body(&sim).user_torque(), Vector::ZERO, 1e-12);
            assert_vector_close(angular_momentum(&sim), momentum, momentum.length() * 1e-4);
            assert!(norm(lander(&sim).velocity) > 0.0);
        }
    }

    #[test]
    fn api_center_force_and_direct_angular_command_keep_their_existing_limits() {
        let cfg = settings().lander;
        let dt = settings().integration.fixed_step_s;
        let mut sim = Simulation::new_default();
        sim.set_manual_thrust([0.0, cfg.max_accel_mps2, 0.0])
            .unwrap();
        sim.set_command([1.0, -2.0, 3.0], [0.1, 0.2, -0.3]).unwrap();
        let commanded = lander(&sim).clone();
        let a = clamp_vec([1.0, -2.0, 3.0], cfg.max_accel_mps2);
        let aa = clamp_vec([0.1, 0.2, -0.3], cfg.max_angular_accel_rad_s2);
        let i = cfg.inertia_kg_m2;
        assert_eq!(
            commanded.thrust_application,
            ThrustApplication::CenterOfMass
        );
        assert_eq!(commanded.commanded_accel_mps2, a);
        assert_eq!(commanded.commanded_angular_accel_rad_s2, aa);
        assert_eq!(commanded.thrust_n, scale(a, cfg.mass_kg));
        assert_eq!(
            commanded.torque_nm,
            [dot(i[0], aa), dot(i[1], aa), dot(i[2], aa)]
        );
        sim.advance(dt);
        assert_vector_close(
            rigid_body(&sim).user_torque(),
            rotation(&commanded) * Vector::from_array(commanded.torque_nm),
            1e-8,
        );

        let mut center = Simulation::new_default();
        center
            .set_command([0.0, cfg.max_accel_mps2, 0.0], [0.0; 3])
            .unwrap();
        center.advance(dt);
        assert_eq!(rigid_body(&center).user_torque(), Vector::ZERO);
        assert_vector_close(rigid_body(&center).angvel(), Vector::ZERO, 1e-12);
    }

    #[test]
    fn nonfinite_manual_and_api_inputs_are_rejected_without_changing_state() {
        let mut sim = Simulation::new_default();
        sim.set_manual_thrust([0.0, 0.5, 0.0]).unwrap();
        let before = serde_json::to_value(sim.snapshot()).unwrap();
        for invalid in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            assert!(sim.set_manual_thrust([invalid, 0.0, 0.0]).is_err());
            assert!(sim.set_command([invalid, 0.0, 0.0], [0.0; 3]).is_err());
            assert!(sim.set_command([0.0; 3], [0.0, invalid, 0.0]).is_err());
            assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
        }
    }

    #[test]
    fn manual_fuel_uses_component_magnitudes_while_center_keeps_resultant_accounting() {
        let cfg = settings().lander;
        let dt = settings().integration.fixed_step_s;
        let mut fuel = [0.0; 2];
        for (index, manual) in [false, true].into_iter().enumerate() {
            let mut sim = Simulation::new_default();
            if manual {
                sim.set_manual_thrust([0.5, -0.5, 0.0]).unwrap();
            } else {
                sim.set_command([0.5, -0.5, 0.0], [0.0; 3]).unwrap();
            }
            let force = lander(&sim).thrust_n;
            let fuel_force = if manual {
                force.into_iter().map(f64::abs).sum::<f64>()
            } else {
                norm(force)
            };
            sim.advance(dt);
            fuel[index] = sim.state.mission.fuel_used_kg;
            assert!(
                (fuel[index]
                    - fuel_force * dt / (cfg.specific_impulse_s * cfg.standard_gravity_mps2))
                    .abs()
                    < 1e-12
            );
        }
        assert!((fuel[1] / fuel[0] - 2.0_f64.sqrt()).abs() < 1e-12);
    }

    #[test]
    fn manual_mode_and_forces_round_trip_entirely_through_the_snapshot() {
        let mut original = Simulation::new_default();
        original.set_manual_thrust([0.0, 0.3, 0.4]).unwrap();
        let exported = serde_json::to_value(original.snapshot()).unwrap();
        let state: Snapshot = serde_json::from_value(exported.clone()).unwrap();
        assert_eq!(
            state
                .assets
                .iter()
                .find(|e| e.id == EntityId::Lander)
                .unwrap()
                .thrust_application,
            ThrustApplication::ManualNozzles
        );
        let mut restored = Simulation::new_default();
        restored
            .execute(PhysicsCommand::Restore {
                state: Box::new(state),
            })
            .unwrap();
        assert_eq!(serde_json::to_value(restored.snapshot()).unwrap(), exported);

        let dt = settings().integration.fixed_step_s;
        original.advance(dt);
        restored.advance(dt);
        let expected = lander(&original);
        let actual = lander(&restored);
        assert_eq!(actual.thrust_application, ThrustApplication::ManualNozzles);
        assert_eq!(actual.thrust_n, expected.thrust_n);
        assert_eq!(actual.torque_nm, expected.torque_nm);
        assert_eq!(actual.position, expected.position);
        assert_eq!(actual.rotation, expected.rotation);
        assert_eq!(actual.velocity, expected.velocity);
        assert_eq!(
            actual.angular_velocity_rad_s,
            expected.angular_velocity_rad_s
        );
        assert_eq!(
            restored.state.mission.fuel_used_kg,
            original.state.mission.fuel_used_kg
        );
        assert_vector_close(
            rigid_body(&restored).user_torque(),
            rigid_body(&original).user_torque(),
            1e-10,
        );
    }

    #[test]
    fn practice_boost_mode_full_wrench_and_future_dynamics_round_trip_through_json() {
        let mut original = Simulation::new_default();
        original.set_space_thrust([0.2, -0.3, 1.5]).unwrap();
        let exported = serde_json::to_value(original.snapshot()).unwrap();
        let asset = exported["assets"]
            .as_array()
            .unwrap()
            .iter()
            .find(|asset| asset["id"] == "Shuttle")
            .unwrap();
        assert_eq!(asset["thrust_application"], "practice_boost");
        let state: Snapshot = serde_json::from_value(exported.clone()).unwrap();
        let mut restored = Simulation::new_default();
        restored
            .execute(PhysicsCommand::Restore {
                state: Box::new(state),
            })
            .unwrap();
        assert_eq!(serde_json::to_value(restored.snapshot()).unwrap(), exported);
        assert_eq!(
            lander(&restored).thrust_application,
            ThrustApplication::PracticeBoost
        );
        assert!(
            norm(lander(&restored).thrust_n)
                > settings().lander.mass_kg * settings().lander.max_accel_mps2
        );

        let dt = settings().integration.fixed_step_s;
        original.advance(dt);
        restored.advance(dt);
        assert_eq!(
            serde_json::to_value(restored.snapshot()).unwrap(),
            serde_json::to_value(original.snapshot()).unwrap()
        );
        assert_vector_close(
            rigid_body(&restored).user_force(),
            rigid_body(&original).user_force(),
            1e-9,
        );
        assert_vector_close(
            rigid_body(&restored).user_torque(),
            rigid_body(&original).user_torque(),
            1e-9,
        );
    }

    #[test]
    fn only_the_known_legacy_lander_cap_is_migrated_for_reference_modes() {
        let cfg = settings().lander;
        for manual in [false, true] {
            let mut original = Simulation::new_default();
            if manual {
                original.set_manual_thrust([0.0, 0.3, 0.4]).unwrap();
            } else {
                original
                    .set_command([0.0, 0.3, 0.4], [0.01, 0.0, 0.0])
                    .unwrap();
            }
            let current = serde_json::to_value(original.snapshot()).unwrap();
            let mut legacy = original.snapshot();
            lander_mut(&mut legacy).max_thrust_n = cfg.mass_kg * cfg.max_accel_mps2;
            validate(&legacy).unwrap();
            let mut restored = Simulation::new_default();
            restored
                .execute(PhysicsCommand::Restore {
                    state: Box::new(legacy),
                })
                .unwrap();
            assert_eq!(lander(&restored).max_thrust_n, local_max_thrust_n());
            assert_eq!(serde_json::to_value(restored.snapshot()).unwrap(), current);
        }

        let mut sim = Simulation::new_default();
        sim.set_space_thrust([0.0, 0.0, settings().practice.space_lift_accel_mps2])
            .unwrap();
        let boost = sim.snapshot();
        let mut invalid = boost.clone();
        lander_mut(&mut invalid).max_thrust_n = cfg.mass_kg * cfg.max_accel_mps2;
        assert!(validate(&invalid).is_err());
        for mode in [
            ThrustApplication::CenterOfMass,
            ThrustApplication::ManualNozzles,
            ThrustApplication::PracticeBoost,
        ] {
            let mut baseline = Simulation::new_default();
            match mode {
                ThrustApplication::CenterOfMass => {
                    baseline.set_command([0.0; 3], [0.0; 3]).unwrap()
                }
                ThrustApplication::ManualNozzles => baseline.set_manual_thrust([0.0; 3]).unwrap(),
                ThrustApplication::PracticeBoost => baseline.set_space_thrust([0.0; 3]).unwrap(),
            }
            for cap in [
                0.0,
                -1.0,
                cfg.mass_kg * cfg.max_accel_mps2 + 1.0,
                local_max_thrust_n() + 1.0,
                f64::NAN,
                f64::INFINITY,
            ] {
                let mut invalid = baseline.snapshot();
                lander_mut(&mut invalid).max_thrust_n = cap;
                assert!(
                    validate(&invalid).is_err(),
                    "accepted cap {cap} in {mode:?}"
                );
            }
        }
        for kind in [
            EntityKind::ReturnShip,
            EntityKind::LandingPad,
            EntityKind::Debris,
        ] {
            let mut invalid = boost.clone();
            invalid
                .assets
                .iter_mut()
                .find(|e| e.kind == kind)
                .unwrap()
                .max_thrust_n = cfg.mass_kg * cfg.max_accel_mps2;
            assert!(validate(&invalid).is_err());
        }
    }

    #[test]
    fn boost_restore_rejects_tampered_wrench_angular_control_and_component_limits() {
        let cfg = settings().lander;
        let lift = settings().practice.space_lift_accel_mps2;
        let mut sim = Simulation::new_default();
        sim.set_space_thrust([0.2, 0.3, 1.5]).unwrap();
        let valid = sim.snapshot();
        validate(&valid).unwrap();
        let before = serde_json::to_value(valid.clone()).unwrap();
        let mut invalid_states = Vec::new();
        let mut invalid = valid.clone();
        lander_mut(&mut invalid).thrust_n[0] *= 0.5;
        invalid_states.push(invalid);
        let mut invalid = valid.clone();
        lander_mut(&mut invalid).torque_nm[0] *= 2.0;
        invalid_states.push(invalid);
        let mut invalid = valid.clone();
        lander_mut(&mut invalid).commanded_angular_accel_rad_s2[0] = 0.01;
        invalid_states.push(invalid);
        for accel in [
            [0.0, 0.0, lift + 0.01],
            [cfg.max_accel_mps2 + 0.01, 0.0, 1.0],
            [cfg.max_accel_mps2, cfg.max_accel_mps2, 1.0],
            [0.0, 0.0, -0.1],
            [0.0, 0.0, -lift],
        ] {
            let mut invalid = valid.clone();
            let lander = lander_mut(&mut invalid);
            lander.commanded_accel_mps2 = accel;
            lander.thrust_n = scale(accel, cfg.mass_kg);
            invalid_states.push(invalid);
        }
        for mode in [
            ThrustApplication::CenterOfMass,
            ThrustApplication::ManualNozzles,
        ] {
            let mut invalid = valid.clone();
            lander_mut(&mut invalid).thrust_application = mode;
            invalid_states.push(invalid);
            let mut invalid = Simulation::new_default().snapshot();
            let lander = lander_mut(&mut invalid);
            lander.thrust_application = mode;
            lander.thrust_n = [0.0, 0.0, cfg.mass_kg * lift];
            invalid_states.push(invalid);
        }
        for kind in [
            EntityKind::ReturnShip,
            EntityKind::LandingPad,
            EntityKind::Debris,
        ] {
            let mut invalid = valid.clone();
            invalid
                .assets
                .iter_mut()
                .find(|e| e.kind == kind)
                .unwrap()
                .thrust_application = ThrustApplication::PracticeBoost;
            invalid_states.push(invalid);
        }
        for invalid in invalid_states {
            assert!(
                sim.execute(PhysicsCommand::Restore {
                    state: Box::new(invalid)
                })
                .is_err()
            );
            assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
        }
        let mut unknown_mode = serde_json::to_value(valid).unwrap();
        unknown_mode["assets"][0]["thrust_application"] = json!("unbounded_boost");
        assert!(serde_json::from_value::<Snapshot>(unknown_mode).is_err());
    }

    #[test]
    fn legacy_snapshot_defaults_to_center_and_reconciles_optional_wrench_reports() {
        let mut sim = Simulation::new_default();
        sim.set_command([0.0, 0.3, 0.4], [0.01, 0.0, 0.0]).unwrap();
        let mut state = sim.snapshot();
        lander_mut(&mut state).max_thrust_n =
            settings().lander.mass_kg * settings().lander.max_accel_mps2;
        let mut legacy = serde_json::to_value(state).unwrap();
        for asset in legacy["assets"].as_array_mut().unwrap() {
            asset.as_object_mut().unwrap().remove("thrust_application");
        }
        let state: Snapshot = serde_json::from_value(legacy.clone()).unwrap();
        assert!(
            state
                .assets
                .iter()
                .all(|e| e.thrust_application == ThrustApplication::CenterOfMass)
        );
        sim.execute(PhysicsCommand::Restore {
            state: Box::new(state),
        })
        .unwrap();
        assert_eq!(lander(&sim).commanded_accel_mps2, [0.0, 0.3, 0.4]);
        assert_eq!(lander(&sim).max_thrust_n, local_max_thrust_n());

        // These fields were already optional before nozzle mode existed. Their
        // absence must not leave a displayed jet that physics never applies.
        for asset in legacy["assets"].as_array_mut().unwrap() {
            let object = asset.as_object_mut().unwrap();
            object.remove("commanded_accel_mps2");
            object.remove("commanded_angular_accel_rad_s2");
            object.remove("torque_nm");
        }
        let state = serde_json::from_value(legacy).unwrap();
        sim.execute(PhysicsCommand::Restore {
            state: Box::new(state),
        })
        .unwrap();
        assert_eq!(
            lander(&sim).thrust_application,
            ThrustApplication::CenterOfMass
        );
        assert_eq!(lander(&sim).thrust_n, [0.0; 3]);
        assert_eq!(lander(&sim).torque_nm, [0.0; 3]);
        assert!(applied_jets(lander(&sim)).is_empty());
        sim.set_manual_thrust([0.0, 0.5, 0.0]).unwrap();
        sim.execute(PhysicsCommand::Reset { seed: None }).unwrap();
        assert_eq!(
            lander(&sim).thrust_application,
            ThrustApplication::CenterOfMass
        );
    }

    #[test]
    fn restore_rejects_wrong_manual_owner_inconsistent_wrench_and_direct_angular_request() {
        let mut sim = Simulation::new_default();
        sim.set_manual_thrust([0.0, 0.5, 0.0]).unwrap();
        let valid = sim.snapshot();
        validate(&valid).unwrap();
        let mut invalid = valid.clone();
        lander_mut(&mut invalid).thrust_n[1] *= 0.5;
        assert!(validate(&invalid).is_err());
        let mut invalid = valid.clone();
        lander_mut(&mut invalid).torque_nm[0] *= 2.0;
        assert!(validate(&invalid).is_err());
        let before = serde_json::to_value(sim.snapshot()).unwrap();
        assert!(
            sim.execute(PhysicsCommand::Restore {
                state: Box::new(invalid)
            })
            .is_err()
        );
        assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
        let mut invalid = valid.clone();
        lander_mut(&mut invalid).commanded_angular_accel_rad_s2[0] = 0.01;
        assert!(validate(&invalid).is_err());
        let mut invalid = valid.clone();
        lander_mut(&mut invalid).commanded_accel_mps2[1] = 1.0;
        assert!(validate(&invalid).is_err());
        let mut invalid = valid.clone();
        lander_mut(&mut invalid).torque_nm[0] = f64::NAN;
        assert!(validate(&invalid).is_err());
        for kind in [
            EntityKind::ReturnShip,
            EntityKind::LandingPad,
            EntityKind::Debris,
        ] {
            let mut invalid = valid.clone();
            invalid
                .assets
                .iter_mut()
                .find(|e| e.kind == kind)
                .unwrap()
                .thrust_application = ThrustApplication::ManualNozzles;
            assert!(validate(&invalid).is_err());
        }

        // Natural angular response is not an angular-control request or capped
        // angular velocity. Such a finite state must remain restorable.
        let mut spinning = valid;
        lander_mut(&mut spinning).angular_velocity_rad_s = [1.0, -2.0, 3.0];
        validate(&spinning).unwrap();
    }
}
