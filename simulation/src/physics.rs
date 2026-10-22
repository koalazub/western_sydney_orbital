use crate::model::{EntityState, Event, MissionState, Observation, PrimaryBody, Snapshot};
use rapier3d_f64::prelude::*;
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};

const MAX_STEP: f64 = 30.0;
const DT: f64 = 0.05;
const EVENT_LIMIT: usize = 200;
const MAX_ACCEL: f64 = 0.5;
const MAX_ANG_ACCEL: f64 = 0.05;
const KEEP_OUT_M: f64 = 50.0;
const DAMAGE_THRESHOLD_J: f64 = 209_200.0;
const NOMINAL_FUEL_KG: f64 = 1.15;
const NOMINAL_TIME_S: f64 = 120.0;
const INITIAL_RANGE_M: f64 = 1_305.82;
const PHOBOS_PROXY_RADII: [f64; 3] = crate::model::PHOBOS_RADII_M;
const SHUTTLE_MASS_KG: f64 = 3388.7;
// JPL Mars GM, with a fixed Mars direction for this short-run frame approximation.
const MARS_MU: f64 = 4.282837e13;
const MARS_POSITION_M: [f64; 3] = [9_376_000.0, 0.0, 0.0];

fn body() -> PrimaryBody {
    PrimaryBody {
        name: "Phobos".into(),
        radii_m: PHOBOS_PROXY_RADII,
        mu_m3_s2: 707_500.0,
        rotation_period_s: 27_540.0,
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
    handles: HashMap<String, RigidBodyHandle>,
    collider_ids: HashMap<ColliderHandle, String>,
    touching: HashSet<String>,
    rng: u64,
}

impl Simulation {
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
        let touching = state.active_contacts.iter().cloned().collect();
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
        sim.integration.dt = DT;
        for e in &sim.state.assets {
            let p = Vector::new(e.position[0], e.position[1], e.position[2]);
            let q = Rotation::from_xyzw(e.rotation[0], e.rotation[1], e.rotation[2], e.rotation[3]);
            let mut builder = if e.id == sim.state.mission.target_id {
                RigidBodyBuilder::kinematic_position_based()
            } else {
                RigidBodyBuilder::dynamic()
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
            let shape = SharedShape::ball(e.radius_m.max(0.1));
            let mut collider = ColliderBuilder::new(shape)
                .friction(0.8)
                .restitution(0.05)
                .active_events(ActiveEvents::COLLISION_EVENTS);
            if e.id == "Shuttle" {
                let inertia = Matrix::from_cols(
                    Vector::new(3025.8, -171.7, 461.7),
                    Vector::new(-171.7, 5927.5, 313.6),
                    Vector::new(461.7, 313.6, 6756.0),
                );
                collider = collider.mass_properties(MassProperties::with_inertia_matrix(
                    Vector::ZERO,
                    3388.7,
                    inertia,
                ));
            } else if e.id != sim.state.mission.target_id {
                collider = collider.mass(e.mass_kg);
            }
            let c = sim
                .colliders
                .insert_with_parent(collider.build(), h, &mut sim.bodies);
            sim.handles.insert(e.id.clone(), h);
            sim.collider_ids.insert(c, e.id.clone());
        }
        // The renderer and collider use the same independently generated terrain mesh.
        let phobos = sim.bodies.insert(RigidBodyBuilder::fixed().build());
        let collider = sim.colliders.insert_with_parent(
            ColliderBuilder::new(phobos_shape())
                .friction(0.8)
                .restitution(0.05)
                .active_events(ActiveEvents::COLLISION_EVENTS)
                .build(),
            phobos,
            &mut sim.bodies,
        );
        sim.handles.insert("Phobos".into(), phobos);
        sim.collider_ids.insert(collider, "Phobos".into());
        sim
    }
    pub fn snapshot(&self) -> Snapshot {
        self.state.clone()
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
            .radar(200)
            .iter()
            .map(|o| {
                json!([
                    o.relative_position_m,
                    [o.range_m, o.range_rate_mps, o.diameter_m],
                    [o.measurement_time_s, 0.0, 0.0],
                ])
            })
            .collect();
        json!({"time_s":self.state.time_s,"seed":self.state.seed,"frame":self.state.frame,"shuttle":shuttle,"target":target,"mission":self.state.mission,"radar":radar,"fuel_unlimited":true,"max_accel_mps2":MAX_ACCEL,"max_angular_accel_rad_s2":MAX_ANG_ACCEL})
    }
    pub fn radar(&self, n: usize) -> Vec<Observation> {
        self.state
            .observations
            .iter()
            .take(n.min(200))
            .cloned()
            .collect()
    }
    pub fn set_command(&mut self, accel: [f64; 3], angular_accel: [f64; 3]) -> Result<(), String> {
        if !accel
            .iter()
            .chain(angular_accel.iter())
            .all(|x| x.is_finite())
        {
            return Err("command values must be finite".into());
        }
        let a = clamp_vec(accel, MAX_ACCEL);
        let aa = clamp_vec(angular_accel, MAX_ANG_ACCEL);
        if self.state.mission.status != "running" && (norm(a) > 0.0 || norm(aa) > 0.0) {
            return Err("Mission finished. Reset before commanding another flight.".into());
        }
        let e = self
            .state
            .assets
            .iter_mut()
            .find(|e| e.id == "Shuttle")
            .ok_or("controlled Shuttle missing")?;
        e.commanded_accel_mps2 = a;
        e.commanded_angular_accel_rad_s2 = aa;
        e.thrust_n = scale(a, SHUTTLE_MASS_KG);
        e.torque_nm = [
            3025.8 * aa[0] - 171.7 * aa[1] + 461.7 * aa[2],
            -171.7 * aa[0] + 5927.5 * aa[1] + 313.6 * aa[2],
            461.7 * aa[0] + 313.6 * aa[1] + 6756.0 * aa[2],
        ];
        e.status = if norm(a) > 0.0 {
            "thrusting"
        } else if norm(aa) > 0.0 {
            "rotating"
        } else {
            "coasting"
        }
        .into();
        Ok(())
    }
    pub fn execute(&mut self, req: Value) -> Result<Snapshot, String> {
        let op = req.get("op").and_then(Value::as_str).ok_or("missing op")?;
        match op {
            "reset" => {
                let seed = match req.get("seed") {
                    Some(value) => value
                        .as_u64()
                        .filter(|seed| *seed <= u32::MAX as u64)
                        .ok_or("seed must be a whole number from 0 to 4294967295")?,
                    None => 100,
                };
                *self = Self::new(seed);
            }
            "observe" => {}
            "restore" => {
                let s: Snapshot =
                    serde_json::from_value(req.get("state").cloned().ok_or("missing state")?)
                        .map_err(|e| e.to_string())?;
                validate(&s)?;
                *self = Self::from_state(s);
            }
            "step" => {
                let seconds = num(&req, "seconds")?;
                if !(0.0..=MAX_STEP).contains(&seconds) {
                    return Err(format!("seconds must be between 0 and {MAX_STEP}"));
                }
                self.advance(seconds)
            }
            "set_command" => {
                let a = vec_arg(&req, "accel")?;
                let aa = vec_arg(&req, "angular_accel")?;
                self.set_command(a, aa)?;
            }
            "stop" => {
                self.set_command([0.0; 3], [0.0; 3])?;
            }
            _ => return Err(format!("unknown operation: {op}")),
        }
        Ok(self.snapshot())
    }
    fn advance(&mut self, seconds: f64) {
        if seconds == 0.0 {
            return;
        }
        let n = (seconds / DT).ceil().max(1.0) as usize;
        let h = seconds / n as f64;
        self.integration.dt = h;
        for _ in 0..n {
            if self.state.mission.status == "success"
                || self.state.mission.status == "failed_damage"
            {
                break;
            }
            for e in &self.state.assets {
                if let Some(handle) = self.handles.get(&e.id) {
                    if let Some(rb) = self.bodies.get_mut(*handle) {
                        // Rapier keeps user forces across steps. Replacing them avoids
                        // accumulating gravity and commanded acceleration every substep.
                        rb.reset_forces(false);
                        rb.reset_torques(false);
                        if e.id == "Shuttle" {
                            let q = *rb.rotation();
                            let accel = Vector::new(
                                e.commanded_accel_mps2[0],
                                e.commanded_accel_mps2[1],
                                e.commanded_accel_mps2[2],
                            );
                            let a = q * accel;
                            rb.add_force(a * rb.mass(), true);
                            let x = e.commanded_angular_accel_rad_s2;
                            let torque = q * Vector::new(
                                3025.8 * x[0] - 171.7 * x[1] + 461.7 * x[2],
                                -171.7 * x[0] + 5927.5 * x[1] + 313.6 * x[2],
                                461.7 * x[0] + 313.6 * x[1] + 6756.0 * x[2],
                            );
                            rb.add_torque(torque, true);
                            self.state.mission.fuel_used_kg +=
                                accel.length() * SHUTTLE_MASS_KG * h / (80.0 * 9.8);
                        }
                        if e.id != self.state.mission.target_id {
                            let p = rb.translation();
                            let r = p.length().max(1.0);
                            let mars = Vector::from_array(MARS_POSITION_M);
                            let delta = mars - p;
                            let gravity = p * (-707500.0 / (r * r * r))
                                + delta * (MARS_MU / delta.length().powi(3))
                                - mars * (MARS_MU / mars.length().powi(3));
                            rb.add_force(gravity * rb.mass(), true);
                        }
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
                if pair.has_any_active_contact() {
                    let a = self
                        .collider_ids
                        .get(&pair.collider1)
                        .cloned()
                        .unwrap_or_default();
                    let b = self
                        .collider_ids
                        .get(&pair.collider2)
                        .cloned()
                        .unwrap_or_default();
                    if !a.is_empty() && !b.is_empty() {
                        current.insert(pair_key(&a, &b));
                        if !self.touching.contains(&pair_key(&a, &b)) {
                            self.state.collisions = self.state.collisions.saturating_add(1);
                            event(
                                &mut self.state,
                                format!("{a} / {b}"),
                                "Rapier contact (practice closing-energy damage approximation)",
                            );
                            if a == "Shuttle" || b == "Shuttle" {
                                let other = if a == "Shuttle" { &b } else { &a };
                                let ship_v = self
                                    .state
                                    .assets
                                    .iter()
                                    .find(|e| e.id == "Shuttle")
                                    .map(|e| {
                                        Vector::new(e.velocity[0], e.velocity[1], e.velocity[2])
                                    })
                                    .unwrap_or(Vector::ZERO);
                                let other_entity =
                                    self.state.assets.iter().find(|e| e.id == *other);
                                let other_v = other_entity
                                    .map(|e| {
                                        Vector::new(e.velocity[0], e.velocity[1], e.velocity[2])
                                    })
                                    .unwrap_or(Vector::ZERO);
                                let m2 = other_entity.map(|e| e.mass_kg).unwrap_or(f64::INFINITY);
                                let reduced = if m2.is_finite() {
                                    3388.7 * m2 / (3388.7 + m2)
                                } else {
                                    3388.7
                                };
                                let relative = if a == "Shuttle" {
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
            }
            self.touching = current;
            self.state.active_contacts = self.touching.iter().cloned().collect();
            self.state.active_contacts.sort();
            for e in &mut self.state.assets {
                if let Some(hd) = self.handles.get(&e.id) {
                    if let Some(rb) = self.bodies.get(*hd) {
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
            }
            let before = self.state.time_s;
            self.state.time_s += h;
            self.update_mission();
            if (self.state.time_s * 10.0).floor() > (before * 10.0).floor() {
                self.sample_radar();
                self.state.rng_state = self.rng;
            }
        }
    }
    fn update_mission(&mut self) {
        let shuttle = self
            .state
            .assets
            .iter()
            .find(|e| e.id == "Shuttle")
            .unwrap();
        let target = self.state.assets.iter().find(|e| e.id == "Target").unwrap();
        let distance = norm(sub(shuttle.position, target.position));
        let speed = norm(sub(shuttle.velocity, target.velocity));
        self.state.mission.target_distance_m = distance;
        self.state.mission.relative_speed_mps = speed;
        let previous = self.state.mission.status.clone();
        if self.state.mission.damage > DAMAGE_THRESHOLD_J {
            self.state.mission.status = "failed_damage".into();
        } else if distance <= KEEP_OUT_M {
            self.state.mission.status = "success".into();
        }
        if self.state.mission.status == "running" {
            return;
        }
        let term = if self.state.mission.status == "success" {
            NOMINAL_TIME_S / self.state.time_s.max(DT)
        } else {
            -distance / INITIAL_RANGE_M
        };
        self.state.mission.score = Some(
            (term
                - 2.0 * self.state.mission.damage / DAMAGE_THRESHOLD_J
                - (self.state.mission.fuel_used_kg / NOMINAL_FUEL_KG - 1.0).max(0.0)
                + 0.5)
                * 100.0,
        );
        if previous != self.state.mission.status {
            let message = if self.state.mission.status == "success" {
                "Shuttle entered the 50 m target arrival radius. Practice run finished."
            } else {
                "Estimated impact energy exceeded 209,200 J. Practice run ended in failure."
            };
            event(&mut self.state, "Shuttle".into(), message);
        }
    }

    fn sample_radar(&mut self) {
        let Some(shuttle) = self.state.assets.iter().find(|e| e.id == "Shuttle") else {
            return;
        };
        let mut rows = Vec::with_capacity(200);
        // The checked-in Radar system scans the first 200 of the 500 spawned
        // bodies. Preserve that published configuration discrepancy explicitly.
        for debris in self
            .state
            .assets
            .iter()
            .filter(|e| e.kind == "asteroid")
            .take(200)
        {
            let delta = sub(debris.position, shuttle.position);
            let range = norm(delta);
            let relative_velocity = sub(debris.velocity, shuttle.velocity);
            let position = std::array::from_fn(|i| delta[i] + gaussian(&mut self.rng) * 2.0);
            let rate =
                dot(relative_velocity, delta) / range.max(1e-9) + gaussian(&mut self.rng) * 0.02;
            rows.push(Observation {
                id: debris.id.clone(),
                kind: debris.kind.clone(),
                relative_position_m: position,
                // Analysis-only truth; observation() never exposes this field.
                relative_velocity_mps: relative_velocity,
                range_m: (range - debris.radius_m + gaussian(&mut self.rng) * 2.0).max(0.0),
                range_rate_mps: rate,
                diameter_m: (debris.radius_m * 2.0 * (1.0 + gaussian(&mut self.rng) * 0.05))
                    .max(0.01),
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
    let p = [-651.02285, 95.19903, -8239.66800];
    let mut ship = entity("Shuttle", "ship", p, [0.0; 3], 1.0, 3388.7);
    ship.name = "SpaceBuggy".into();
    ship.rotation = [0.9848615, 0.1685563, -0.0397780, 0.00737254];
    assets.push(ship);
    assets.push(entity(
        "Target",
        "target",
        [-850.0, 120.0, -9530.0],
        [0.0; 3],
        1.0,
        4.0e6,
    ));
    let up = scale(p, 1.0 / norm(p));
    for i in 0..500 {
        let x = (rand(&mut rng) % 6001) as f64 / 10000.0 - 0.3;
        let y = (rand(&mut rng) % 6001) as f64 / 10000.0 - 0.3;
        let z = (rand(&mut rng) % 10001) as f64 / 10000.0 - 0.5;
        let center = [
            p[0] + up[0] * 500.0,
            p[1] + up[1] * 500.0,
            p[2] + up[2] * 500.0,
        ];
        let pos = [
            center[0] + x * 1000.0,
            center[1] + y * 1000.0,
            center[2] + z * 1000.0,
        ];
        let tangent = Vector::new(gaussian(&mut rng), gaussian(&mut rng), gaussian(&mut rng));
        let radial = Vector::new(pos[0], pos[1], pos[2]).normalize_or_zero();
        let tangent = (tangent - radial * tangent.dot(radial)).normalize_or_zero();
        let speed = (707_500.0 / norm(pos)).sqrt() * (0.05 + uniform(&mut rng) * 0.05);
        let vel = [tangent.x * speed, tangent.y * speed, tangent.z * speed];
        let roll = rand(&mut rng) % 100;
        let (label, diameter, mass) = if roll < 45 {
            let s = 10.0 + (rand(&mut rng) % 391) as f64;
            ("Rock1", 0.08 * s, 0.63 * s.powi(3))
        } else if roll < 80 {
            let s = 20.0 + (rand(&mut rng) % 781) as f64;
            ("Rock2", 0.05 * s, 0.3 * s.powi(3))
        } else if roll < 95 {
            let s = 40.0 + (rand(&mut rng) % 961) as f64;
            ("Rock3", 0.01 * s, 0.2 * s.powi(3))
        } else {
            ("Tank", 17.0, 700.0)
        };
        let mut e = entity(
            &format!("Debris{}", i + 1),
            "asteroid",
            pos,
            vel,
            diameter * 0.5,
            mass,
        );
        e.name = format!("{label} {}", i + 1);
        let axis = Vector::new(gaussian(&mut rng), gaussian(&mut rng), gaussian(&mut rng))
            .normalize_or_zero();
        let angular_velocity = axis * uniform(&mut rng) * 0.2;
        e.angular_velocity_rad_s = angular_velocity.to_array();
        assets.push(e);
    }
    Snapshot {
        model: concat!(
            "Rapier practice approximation. 500 physical asteroids; radar scans the first 200, sorted nearest-first, ",
            "at 10 Hz rather than STP's configured 120 Hz, using the published noise standard deviations. ",
            "Phobos point-mass gravity and fixed-direction Mars differential gravity. Synthetic Phobos terrain ",
            "is shared by rendering and collision detection. Ship/rock contacts use spherical proxies; spacecraft artwork is enlarged. ",
            "Ideal acceleration replaces the 42-jet allocator. Fuel estimates integrate thrust impulse with Isp 80 s and g0 9.8 m/s²; ",
            "they do not reproduce the upstream actuator's per-update jet-activity fuel counter. ",
            "Impact damage is a closing-energy estimate, not the proprietary STP model. Debris is seeded independently, ",
            "not identical to the STP random field. Restore preserves body states and active contacts but rebuilds solver caches. ",
            "The published score formula uses 120 s nominal time, 1.15 kg nominal fuel and 209,200 J damage threshold, ",
            "with a 50 m arrival radius. Applying it to our estimated fuel/damage gives a practice score, not an official score. ",
            "Mission settings from STU4-Sim c1e3fc8f9ee3b189bba64d8bd87818a8003639cc."
        ).into(),
        time_s: 0.0, seed, frame: "Phobos-fixed frame treated as inertial over short practice runs; Cartesian SI units".into(),
        body: body(), assets, events: vec![], collisions: 0,
        mission: MissionState {
            shuttle_id: "Shuttle".into(), target_id: "Target".into(),
            target_distance_m: norm(sub(p, [-850.0, 120.0, -9530.0])), relative_speed_mps: 0.0,
            fuel_used_kg: 0.0, damage: 0.0, status: "running".into(), score: None,
            score_label: "Published score formula with approximate fuel and impact energy; not an official STP score".into(),
        },
        observations: vec![], rng_state: rng, active_contacts: vec![],
    }
}
fn entity(id: &str, kind: &str, p: [f64; 3], v: [f64; 3], r: f64, m: f64) -> EntityState {
    EntityState {
        id: id.into(),
        name: id.into(),
        kind: kind.into(),
        position: p,
        velocity: v,
        rotation: [0.0, 0.0, 0.0, 1.0],
        radius_m: r,
        mass_kg: m,
        fuel_kg: 0.0,
        max_thrust_n: if id == "Shuttle" {
            SHUTTLE_MASS_KG * MAX_ACCEL
        } else {
            0.0
        },
        thrust_n: [0.0; 3],
        status: "coasting".into(),
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
        || s.assets.len() != 502
        || s.events.len() > EVENT_LIMIT
        || s.observations.len() > 200
        || s.active_contacts.len() > 4096
        || s.body.name != "Phobos"
        || s.body.radii_m != PHOBOS_PROXY_RADII
        || s.body.mu_m3_s2 != 707_500.0
        || s.body.rotation_period_s != 27_540.0
        || s.frame
            != "Phobos-fixed frame treated as inertial over short practice runs; Cartesian SI units"
        || s.mission.shuttle_id != "Shuttle"
        || s.mission.target_id != "Target"
        || ![
            s.mission.target_distance_m,
            s.mission.relative_speed_mps,
            s.mission.fuel_used_kg,
            s.mission.damage,
        ]
        .iter()
        .all(|v| v.is_finite())
        || s.mission.fuel_used_kg < 0.0
        || s.mission.damage < 0.0
        || !["running", "success", "failed_damage"].contains(&s.mission.status.as_str())
        || s.mission.score.is_some_and(|v| !v.is_finite())
    {
        return Err("invalid or client-modified fixed configuration".into());
    }
    let mut ids = HashSet::new();
    let fixed = initial(s.seed);
    let expected: HashMap<&str, &EntityState> =
        fixed.assets.iter().map(|e| (e.id.as_str(), e)).collect();
    let (mut shuttle, mut target, mut debris) = (0, 0, 0);
    for e in &s.assets {
        let qn = e.rotation.iter().map(|v| v * v).sum::<f64>().sqrt();
        if e.id.is_empty()
            || !ids.insert(&e.id)
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
            || e.position.iter().any(|v| v.abs() > 1.0e7)
            || e.velocity.iter().any(|v| v.abs() > 1.0e4)
            || !e.mass_kg.is_finite()
            || e.mass_kg <= 0.0
            || e.mass_kg > 1.0e15
            || !e.radius_m.is_finite()
            || e.radius_m <= 0.0
            || e.radius_m > 1.0e5
            || !(0.99..=1.01).contains(&qn)
            || norm(e.commanded_accel_mps2) > MAX_ACCEL + 1e-9
            || norm(e.commanded_angular_accel_rad_s2) > MAX_ANG_ACCEL + 1e-9
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
            .get(e.id.as_str())
            .ok_or_else(|| format!("unknown scene entity {}", e.id))?;
        if fixed.mass_kg != e.mass_kg
            || fixed.radius_m != e.radius_m
            || fixed.kind != e.kind
            || fixed.max_thrust_n != e.max_thrust_n
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
        match e.id.as_str() {
            "Shuttle" if e.kind == "ship" && e.mass_kg == 3388.7 => shuttle += 1,
            "Target" if e.kind == "target" && e.mass_kg == 4.0e6 => target += 1,
            _ if e.kind == "asteroid" && e.id.starts_with("Debris") => debris += 1,
            _ => return Err("entity violates fixed scene configuration".into()),
        }
    }
    if shuttle != 1
        || target != 1
        || debris != 500
        || !ids.contains(&s.mission.shuttle_id)
        || !ids.contains(&s.mission.target_id)
    {
        return Err("mission entity ids or counts invalid".into());
    }
    if s.active_contacts.iter().any(|pair| {
        pair.split_once('|').is_none_or(|(a, b)| {
            !(ids.contains(&a.to_owned()) || a == "Phobos")
                || !(ids.contains(&b.to_owned()) || b == "Phobos")
        })
    }) {
        return Err("invalid active contact state".into());
    }
    if s.events
        .iter()
        .any(|e| !e.t_s.is_finite() || e.t_s < 0.0 || e.t_s > s.time_s + DT)
        || s.observations.iter().any(|o| {
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
                || o.measurement_time_s < 0.0
                || o.measurement_time_s > s.time_s + DT
        })
    {
        return Err("invalid event or radar state".into());
    }
    Ok(())
}
fn pair_key(a: &str, b: &str) -> String {
    if a < b {
        format!("{a}|{b}")
    } else {
        format!("{b}|{a}")
    }
}
fn phobos_shape() -> SharedShape {
    let (vertices, indices) = crate::model::phobos_geometry();
    SharedShape::trimesh(
        vertices.into_iter().map(Vector::from_array).collect(),
        indices,
    )
    .expect("generated Phobos terrain is a valid triangle mesh")
}
fn event(s: &mut Snapshot, asset: String, message: &str) {
    if s.events.len() == EVENT_LIMIT {
        s.events.remove(0);
    }
    s.events.push(Event {
        t_s: s.time_s,
        asset,
        message: message.into(),
    })
}
fn num(v: &Value, k: &str) -> Result<f64, String> {
    let x = v
        .get(k)
        .and_then(Value::as_f64)
        .ok_or_else(|| format!("missing or invalid {k}"))?;
    if x.is_finite() {
        Ok(x)
    } else {
        Err(format!("{k} must be finite"))
    }
}
fn vec_arg(v: &Value, k: &str) -> Result<[f64; 3], String> {
    let a = v
        .get(k)
        .and_then(Value::as_array)
        .ok_or_else(|| format!("missing {k}"))?;
    if a.len() != 3 {
        return Err(format!("{k} must have 3 values"));
    }
    let mut out = [0.0; 3];
    for i in 0..3 {
        out[i] = a[i].as_f64().ok_or("invalid vector value")?;
        if !out[i].is_finite() {
            return Err("vector values must be finite".into());
        }
    }
    Ok(out)
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
