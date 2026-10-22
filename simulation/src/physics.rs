use crate::domain::{
    EntityId, EntityKind, EntityStatus, LagrangeRegion, MissionPhase, MissionStatus,
};
use crate::mission::{self, FlightMetrics};
use crate::model::{EntityState, Event, MissionState, Observation, PrimaryBody, Snapshot};
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
    fn from_state(state: Snapshot) -> Self {
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
        if self.state.mission.status.is_terminal() && (norm(a) > 0.0 || norm(aa) > 0.0) {
            return Err("Mission finished. Reset before commanding another flight.".into());
        }
        let e = self
            .state
            .assets
            .iter_mut()
            .find(|e| e.id == EntityId::Lander)
            .ok_or("controlled Shuttle missing")?;
        e.commanded_accel_mps2 = a;
        e.commanded_angular_accel_rad_s2 = aa;
        e.thrust_n = scale(a, settings().lander.mass_kg);
        let i = cfg.inertia_kg_m2;
        e.torque_nm = [dot(i[0], aa), dot(i[1], aa), dot(i[2], aa)];
        e.status = if norm(a) > 0.0 {
            EntityStatus::Thrusting
        } else if norm(aa) > 0.0 {
            EntityStatus::Rotating
        } else {
            EntityStatus::Coasting
        };
        Ok(())
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
                        let q = *rb.rotation();
                        let accel = Vector::new(
                            e.commanded_accel_mps2[0],
                            e.commanded_accel_mps2[1],
                            e.commanded_accel_mps2[2],
                        );
                        let a = q * accel;
                        rb.add_force(a * rb.mass(), true);
                        let x = e.commanded_angular_accel_rad_s2;
                        let i = settings().lander.inertia_kg_m2;
                        let torque = q * Vector::new(dot(i[0], x), dot(i[1], x), dot(i[2], x));
                        rb.add_torque(torque, true);
                        self.state.mission.fuel_used_kg +=
                            accel.length() * settings().lander.mass_kg * h
                                / (settings().lander.specific_impulse_s
                                    * settings().lander.standard_gravity_mps2);
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
            settings().lander.mass_kg * settings().lander.max_accel_mps2
        } else {
            0.0
        },
        thrust_n: [0.0; 3],
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
            || norm(e.commanded_accel_mps2) > settings().lander.max_accel_mps2 + 1e-9
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
            || fixed.max_thrust_n != e.max_thrust_n
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
