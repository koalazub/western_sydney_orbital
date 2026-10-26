use crate::domain::{
    EntityId, EntityKind, EntityStatus, LagrangeRegion, MissionPhase, MissionStatus,
};
use crate::mission::{self, FlightMetrics};
use crate::model::{
    AppliedActuatorWrench, DebrisDistribution, DebrisExperimentSettings, DebrisFlowTuning,
    EntityState, Event, ExperimentSettings, FlightPreset, MissionState, Observation, PrimaryBody,
    ShipTuning, Snapshot, ThrustApplication,
};
#[cfg(test)]
use crate::propulsion::{accel_limit, space_shape_is_valid};
use crate::propulsion::{
    accel_within_limits, applied_jets, applied_wrench, local_max_thrust_for_mass_n,
    local_max_thrust_n,
};
use crate::scenario::settings;
use rapier3d_f64::prelude::*;
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};

pub use crate::model::PhysicsCommand;

mod applied;
mod debris;
mod live_tuning;
mod lookahead;
mod placement;
mod resilience;
mod spatial_index;
mod surface_placement;
use live_tuning::{apply_actuator_forces, inertia_matrix, reconcile_ship_wrench};
use spatial_index::SpawnIndex;

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
    // Runtime readback only. An archive never supplies purported measured forces.
    last_actuator_application: Option<AppliedActuatorWrench>,
    control_runtime: resilience::ControlRuntimeClock,
}

impl Simulation {
    pub fn new_default() -> Self {
        Self::new(settings().default_seed)
    }
    pub fn new(seed: u64) -> Self {
        Self::with_experiment(seed, ExperimentSettings::default())
            .expect("validated default experiment must admit its bounded initial population")
    }
    pub fn with_experiment(seed: u64, experiment: ExperimentSettings) -> Result<Self, String> {
        validate_seed(seed)?;
        experiment.validate()?;
        Self::with_profile(seed, Some(experiment))
    }
    fn with_profile(seed: u64, experiment: Option<ExperimentSettings>) -> Result<Self, String> {
        Self::with_flight_profile(seed, experiment, FlightPreset::default())
    }
    fn with_flight_profile(
        seed: u64,
        experiment: Option<ExperimentSettings>,
        preset: FlightPreset,
    ) -> Result<Self, String> {
        let state = if preset.is_default() {
            initial(seed, experiment)?
        } else {
            initial_for_flight(seed, experiment, None, None, preset)?
        };
        let mut sim = Self::from_state(state);
        sim.sample_radar();
        sim.state.rng_state = sim.rng;
        Ok(sim)
    }
    /// Construct a fresh trial first. Session playback/input changes belong to the adapter.
    pub fn configure_flight(
        &mut self,
        experiment: ExperimentSettings,
        seed: Option<u64>,
        preset: FlightPreset,
    ) -> Result<(), String> {
        let seed = seed.unwrap_or(self.state.seed);
        validate_seed(seed)?;
        experiment.validate()?;
        let authority = self.revoked_control_authority()?;
        let mut replacement = Self::with_flight_profile(seed, Some(experiment), preset)?;
        replacement.retain_control_runtime_from(self);
        replacement.install_control_authority(authority);
        *self = replacement;
        Ok(())
    }
    /// Repeat the active trial, including an archived corridor profile when None.
    /// The replacement is constructed first, so invalid input cannot clear a run.
    pub fn reset(&mut self, seed: u64) -> Result<(), String> {
        validate_seed(seed)?;
        let authority = self.revoked_control_authority()?;
        let mut state = initial_for_flight(
            seed,
            self.state.experiment_settings,
            self.state.ship_tuning_override,
            self.state.debris_flow_override,
            self.state.flight_preset,
        )?;
        state.model = self.state.model.clone();
        state.tuning_revision = self.state.tuning_revision;
        let mut replacement = Self::from_state(state);
        // Birth rejection/draw order belongs to the preset, not the mutable pad.
        // Check a custom seat against that complete population before replacement.
        if let Some(direction) = self.state.launch_site_direction {
            replacement.reset_launch_site(direction)?;
        }
        replacement.sample_radar();
        replacement.state.rng_state = replacement.rng;
        replacement.retain_control_runtime_from(self);
        replacement.install_control_authority(authority);
        *self = replacement;
        Ok(())
    }
    fn from_state(mut state: Snapshot) -> Self {
        let inertia = state.ship_tuning().inertia_kg_m2();
        if let Some(lander) = state.assets.iter_mut().find(|e| e.id == EntityId::Lander) {
            if has_legacy_lander_thrust_cap(lander) {
                lander.max_thrust_n = local_max_thrust_n();
            }
            if lander.thrust_application == ThrustApplication::CenterOfMass {
                // Older center snapshots can omit command/torque fields. Preserve
                // their command-driven dynamics and reconcile the reported wrench.
                reconcile_ship_wrench(lander, inertia);
            }
        }
        let rng = if state.rng_state == 0 {
            state.seed.max(1)
        } else {
            state.rng_state
        };
        let touching = state.active_contacts.iter().copied().collect();
        let capacity = state.assets.len() + 1;
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
            handles: HashMap::with_capacity(capacity),
            collider_ids: HashMap::with_capacity(capacity),
            touching,
            last_actuator_application: None,
            control_runtime: resilience::ControlRuntimeClock::default(),
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
                collider = collider.mass_properties(MassProperties::with_inertia_matrix(
                    Vector::ZERO,
                    e.mass_kg,
                    inertia_matrix(inertia),
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
        sim.restore_command_lease();
        sim
    }
    pub fn snapshot(&self) -> Snapshot {
        self.state.clone()
    }
    pub fn mission_status(&self) -> MissionStatus {
        self.state.mission.status
    }
    pub fn flight_preset(&self) -> FlightPreset {
        self.state.flight_preset
    }
    pub(crate) fn manual_interventions(&self) -> u32 {
        self.state.manual_interventions
    }
    pub fn ship_tuning(&self) -> ShipTuning {
        self.state.ship_tuning()
    }
    pub fn debris_flow(&self) -> DebrisFlowTuning {
        self.state.debris_flow()
    }
    pub fn tuning_revision(&self) -> u32 {
        self.state.tuning_revision
    }
    pub fn ship_inertia_kg_m2(&self) -> [[f64; 3]; 3] {
        let matrix = self.bodies[self.handles[&EntityId::Lander]]
            .mass_properties()
            .local_mprops
            .reconstruct_inertia_matrix()
            .to_cols_array_2d();
        std::array::from_fn(|row| std::array::from_fn(|column| matrix[column][row]))
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
        let config = settings();
        let surface_radius = shuttle.and_then(|ship| {
            bevy::math::DVec3::from_array(ship.position)
                .try_normalize()
                .map(crate::terrain::surface_radius_m)
        });
        let (applied_physics, applied_physics_error) = match self.applied_physics() {
            Ok(physics) => (Some(physics), None),
            Err(error) => (None, Some(error)),
        };
        let shuttle_report = shuttle.map(|ship| {
            let mut report = json!(ship);
            // Archived EntityState wrench fields remain command-derived. Do not
            // label that requested/calibrated wrench as an applied measurement.
            report["wrench_provenance"] = json!("command_derived_local_calibration");
            if let Some(actual) = &applied_physics {
                let inertia = actual.inertia_body_kg_m2;
                report["inertia_kg_m2"] = json!(inertia);
                report["inertia_frame"] = json!("body");
                report["mass_kg"] = json!(actual.mass_kg);
                report["actuation_state"] = json!(actual.actuation_state);
                let application = actual.last_actuator_application;
                report["actuator_application_time_s"] =
                    json!(application.map(|applied| applied.application_time_s));
                report["applied_accel_mps2"] = json!(
                    application
                        .map(|applied| { scale(applied.force_body_n, 1.0 / actual.mass_kg) })
                );
                report["applied_angular_accel_rad_s2"] = json!(application.map(|applied| {
                    (inertia_matrix(inertia).inverse() * Vector::from_array(applied.torque_body_nm))
                        .to_array()
                }));
            }
            report
        });
        let debris_count = self
            .state
            .assets
            .iter()
            .filter(|asset| asset.kind == EntityKind::Debris)
            .count();
        let launchpad = self
            .handles
            .get(&EntityId::LandingPad)
            .and_then(|handle| self.bodies.get(*handle))
            .map(|pad| {
                let q = pad.rotation();
                json!({
                    "position": pad.translation().to_array(),
                    "rotation": [q.x, q.y, q.z, q.w],
                    "source_time_s": self.state.time_s,
                    "source_seed": self.state.seed,
                    "normal_world": (*q * Vector::Z).to_array(),
                })
            });
        let mut report = json!({
            "time_s": self.state.time_s,
            "seed": self.state.seed,
            "manual_interventions": self.state.manual_interventions,
            "frame": self.state.frame,
            "rotation_convention": "active_xyzw",
            "radar_clock": "simulation_elapsed_seconds",
            "environment": self.state.environment,
            "experiment_settings": self.state.experiment_settings,
            "flight_preset": self.flight_preset(),
            "launch_site_direction": self.state.launch_site_direction,
            "launchpad": launchpad,
            "debris_distribution": self.state.experiment_settings.map(|experiment| experiment.debris.distribution),
            "physical_debris_limit": crate::model::MAX_DEBRIS_COUNT,
            "ship_tuning": self.ship_tuning(),
            "debris_flow": self.debris_flow(),
            "debris_flow_spread_scope": "future_births_only",
            "debris_births_enabled": self.state.experiment_settings.is_some_and(|experiment| {
                matches!(experiment.debris.distribution, DebrisDistribution::DirectedStream)
            }),
            "debris_flow_active": self.state.experiment_settings.is_some_and(|experiment| {
                matches!(experiment.debris.distribution, DebrisDistribution::DirectedStream)
            }),
            "debris_flow_tuning_available": self.state.experiment_settings.is_none_or(|experiment| {
                matches!(experiment.debris.distribution, DebrisDistribution::DirectedStream)
            }),
            "tuning_revision": self.tuning_revision(),
            "applied_physics": applied_physics,
            "applied_physics_error": applied_physics_error,
            "shuttle": shuttle_report,
            "target": target,
            "mission": self.state.mission,
            "radar": radar,
            "geometry": {
                "phobos_enclosing_radius_m": crate::model::phobos_enclosing_radius_m(),
                "terrain_radius_at_shuttle_m": surface_radius,
                "arrival_radius_m": config.mission.arrival_radius_m,
                "debris_count": debris_count,
            },
            "sensor_model": {
                "range_noise_m": config.sensors.range_noise_m,
                "range_rate_noise_mps": config.sensors.range_rate_noise_mps,
                "diameter_noise_fraction": config.debris.diameter_noise_fraction,
            },
            "fuel_unlimited": config.lander.fuel_unlimited,
            "max_accel_mps2": config.lander.max_accel_mps2,
            "max_angular_accel_rad_s2": config.lander.max_angular_accel_rad_s2,
        });
        self.project_sensor_delivery(&mut report);
        report
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
        let authority = self.revoked_control_authority()?;
        self.set_bounded_command(accel, angular_accel)?;
        self.cancel_command_lease();
        self.install_control_authority(authority);
        Ok(())
    }
    /// Shared finite/bounded actuator path. Authority and watchdog ownership are
    /// committed separately by the manual and autonomous entry points.
    fn set_bounded_command(
        &mut self,
        accel: [f64; 3],
        angular_accel: [f64; 3],
    ) -> Result<(), String> {
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
        self.apply_command(a, aa, ThrustApplication::CenterOfMass)?;
        Ok(())
    }
    pub(crate) fn clear_terminal_command(&mut self) -> Result<(), String> {
        self.set_bounded_command([0.0; 3], [0.0; 3])?;
        self.cancel_command_lease();
        Ok(())
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
        let inertia = self.state.ship_tuning().inertia_kg_m2();
        let e = self
            .state
            .assets
            .iter_mut()
            .find(|e| e.id == EntityId::Lander)
            .ok_or("controlled Shuttle missing")?;
        e.commanded_accel_mps2 = accel;
        e.commanded_angular_accel_rad_s2 = angular_accel;
        e.thrust_application = application;
        reconcile_ship_wrench(e, inertia);
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
    /// Local test intervention, never an actuator command or a new flight.
    /// Align to the actual pad body without translating or braking the lander.
    pub fn debug_upright(&mut self) -> Result<(), String> {
        let authority = self.revoked_control_authority()?;
        let interventions = self.state.manual_interventions.checked_add(1).ok_or(
            "Local test reset counter exhausted. Reset the run before using Upright lander.",
        )?;
        let lander_index = self
            .state
            .assets
            .iter()
            .position(|entity| entity.id == EntityId::Lander)
            .ok_or("Upright lander unavailable: controlled lander missing. Reset the run.")?;
        let pad_rotation = self
            .handles
            .get(&EntityId::LandingPad)
            .and_then(|handle| self.bodies.get(*handle))
            .map(|body| *body.rotation())
            .ok_or("Upright lander unavailable: landing pad body missing. Reset the run.")?;
        let lander_body = self
            .handles
            .get(&EntityId::Lander)
            .and_then(|handle| self.bodies.get_mut(*handle))
            .ok_or("Upright lander unavailable: lander body missing. Reset the run.")?;

        // Rapier retains user forces until explicitly cleared. No integration,
        // contact bookkeeping, mission assessment or sensor sampling runs here.
        lander_body.set_rotation(pad_rotation, true);
        lander_body.set_angvel(Vector::ZERO, true);
        lander_body.reset_forces(false);
        lander_body.reset_torques(false);
        self.last_actuator_application = None;
        let q = lander_body.rotation();
        let lander = &mut self.state.assets[lander_index];
        lander.rotation = [q.x, q.y, q.z, q.w];
        lander.angular_velocity_rad_s = [0.0; 3];
        lander.commanded_accel_mps2 = [0.0; 3];
        lander.commanded_angular_accel_rad_s2 = [0.0; 3];
        lander.thrust_n = [0.0; 3];
        lander.torque_nm = [0.0; 3];
        if matches!(
            lander.status,
            EntityStatus::Thrusting | EntityStatus::Rotating
        ) {
            lander.status = EntityStatus::Coasting;
        }
        self.state.manual_interventions = interventions;
        self.cancel_command_lease();
        self.install_control_authority(authority);
        event(
            &mut self.state,
            EntityId::Lander,
            "Local test: upright reset",
        );
        Ok(())
    }
    // Retained only to exercise old exported boost-mode states. Live controls
    // and notebook commands use the reference actuator limit.
    #[cfg(test)]
    pub(crate) fn set_space_thrust(&mut self, accel: [f64; 3]) -> Result<(), String> {
        let authority = self.revoked_control_authority()?;
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
        )?;
        self.cancel_command_lease();
        self.install_control_authority(authority);
        Ok(())
    }
    pub fn execute(&mut self, command: PhysicsCommand) -> Result<Snapshot, String> {
        match command {
            PhysicsCommand::Reset { seed } => {
                self.reset(seed.unwrap_or(self.state.seed))?;
            }
            PhysicsCommand::ConfigureExperiment {
                settings: experiment,
                seed,
            } => {
                self.configure_flight(experiment, seed, self.state.flight_preset)?;
            }
            PhysicsCommand::ConfigureFlight {
                settings,
                seed,
                preset,
            } => {
                self.configure_flight(settings, seed, preset)?;
            }
            PhysicsCommand::PlaceLander { position_m } => {
                let authority = self.revoked_control_authority()?;
                self.place_lander(position_m)?;
                self.install_control_authority(authority);
            }
            PhysicsCommand::MoveLaunchpad { surface_direction } => {
                let authority = self.revoked_control_authority()?;
                self.move_launchpad(surface_direction)?;
                self.install_control_authority(authority);
            }
            PhysicsCommand::TuneShip { settings } => self.tune_ship(settings)?,
            PhysicsCommand::TuneDebrisFlow { settings } => self.tune_debris_flow(settings)?,
            PhysicsCommand::Observe => {}
            PhysicsCommand::Restore { state } => {
                validate(&state)?;
                let authority = self.authority_after_restore(&state)?;
                let mut replacement = Self::from_state(*state);
                replacement.retain_control_runtime_from(self);
                replacement.install_control_authority(authority);
                *self = replacement;
            }
            PhysicsCommand::Step { seconds } => {
                if !(0.0..=settings().integration.maximum_command_step_s).contains(&seconds) {
                    return Err(format!(
                        "seconds must be between 0 and {}",
                        settings().integration.maximum_command_step_s
                    ));
                }
                self.revoke_control()?;
                self.advance(seconds)
            }
            PhysicsCommand::SetCommand {
                accel,
                angular_accel,
            } => self.set_command(accel, angular_accel)?,
            PhysicsCommand::SetLeasedCommand {
                accel,
                angular_accel,
                lease_sim_s,
                source_time_s,
                controller_fence,
            } => {
                self.set_leased_command(crate::model::LeasedCommand {
                    accel,
                    angular_accel,
                    lease_sim_s,
                    source_time_s,
                    controller_fence,
                })
                .map_err(|error| error.to_string())?;
            }
            PhysicsCommand::SetSensorFault {
                target,
                mode,
                duration_sim_s,
            } => self.set_sensor_fault(target, mode, duration_sim_s)?,
            PhysicsCommand::ClearSensorFaults => self.clear_sensor_faults()?,
            PhysicsCommand::Stop => {
                self.set_command([0.0; 3], [0.0; 3])?;
            }
            PhysicsCommand::DebugUpright => self.debug_upright()?,
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
            self.enforce_sensor_and_command_deadlines();
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
                        let application = apply_actuator_forces(
                            rb,
                            e,
                            self.state.time_s,
                            self.state.tuning_revision,
                        );
                        let fuel_force_n = application.propellant_force_n;
                        self.last_actuator_application = Some(application);
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
                            let ship_mass = self
                                .state
                                .assets
                                .iter()
                                .find(|entity| entity.id == EntityId::Lander)
                                .map(|entity| entity.mass_kg)
                                .expect("scenario has a lander");
                            let reduced = if m2.is_finite() {
                                ship_mass * m2 / (ship_mass + m2)
                            } else {
                                ship_mass
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
            self.recycle_debris();
            let before = self.state.time_s;
            self.state.time_s += h;
            self.enforce_sensor_and_command_deadlines();
            self.update_mission();
            self.sample_navigation_delivery();
            let cadence = settings().sensors.cadence_hz;
            if (self.state.time_s * cadence).floor() > (before * cadence).floor() {
                self.sample_radar();
            }
            // Birth/recycle draws also happen between sensor ticks.
            self.state.rng_state = self.rng;
        }
    }

    fn recycle_debris(&mut self) {
        let Some(experiment) = self.state.experiment_settings else {
            // Archived corridor runs never recycled bodies or consumed birth RNG.
            return;
        };
        match experiment.debris.distribution {
            DebrisDistribution::ImpactEjecta { .. } => return,
            DebrisDistribution::DirectedStream => {}
        }
        let cfg = self.state.debris_flow().apply_to(experiment.debris);
        let half_extent = flow_half_extent_m(cfg);
        if !self.state.assets.iter().any(|entity| {
            entity.kind == EntityKind::Debris
                && entity
                    .position
                    .iter()
                    .any(|value| value.abs() > half_extent)
        }) {
            return;
        }
        let mut spawn_index = spawn_index(&self.state.assets, cfg.radius_max_m);
        let count = self.state.assets.len();
        // Bound inlet work even after restoring a run with every body outside.
        // Derive the scan start from persisted RNG without drawing extra noise.
        let start = (self.rng % count as u64) as usize;
        let mut budget = MAX_RECYCLES_PER_STEP;
        for offset in 0..count {
            let index = (start + offset) % count;
            let entity = &self.state.assets[index];
            if entity.kind != EntityKind::Debris
                || entity
                    .position
                    .iter()
                    .all(|value| value.abs() <= half_extent)
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
                DebrisBirth::UpstreamInlet,
            ) else {
                // A blocked inlet retries next step. Never place an overlapping
                // body or reduce the requested physical population to make room.
                continue;
            };
            let velocity = debris_birth_velocity(&mut self.rng, cfg);
            let angular_velocity = debris_birth_spin(&mut self.rng);
            spawn_index.remove(id);
            spawn_index.insert(id, position, radius);
            let body = &mut self.bodies[self.handles[&id]];
            body.set_translation(Vector::from_array(position), true);
            body.set_linvel(Vector::from_array(velocity), true);
            body.set_angvel(Vector::from_array(angular_velocity), true);
            body.reset_forces(false);
            body.reset_torques(false);
            let entity = &mut self.state.assets[index];
            entity.position = position;
            entity.velocity = velocity;
            entity.angular_velocity_rad_s = angular_velocity;
            self.touching.retain(|pair| !pair.contains(&id));
        }
        self.state.active_contacts = self.touching.iter().copied().collect();
        self.state.active_contacts.sort();
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
        // The existing phase assessor measures progress along a negative launch
        // axis. Reflect only its phase coordinates for L1, never physical state.
        let phase_x_sign = match self.state.flight_preset {
            FlightPreset::MarsFacingL1 => -1.0,
            FlightPreset::FarSideL2 => 1.0,
        };
        let mut phase_lander_position = lander_position;
        let mut phase_pad_position = pad_position;
        phase_lander_position[0] *= phase_x_sign;
        phase_pad_position[0] *= phase_x_sign;
        let assessment = mission::assess(
            &self.state.mission,
            FlightMetrics {
                distance_m: distance,
                lander_position_m: phase_lander_position,
                pad_position_m: phase_pad_position,
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
        // Preserve the reference scan cap, without inventing rows when the
        // local experiment contains fewer physical bodies, including zero.
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
                range_m: range - debris.radius_m
                    + gaussian(&mut self.rng) * cfg.sensors.range_noise_m,
                range_rate_mps: rate,
                diameter_m: debris.radius_m
                    * 2.0
                    * (1.0 + gaussian(&mut self.rng) * cfg.debris.diameter_noise_fraction),
                measurement_time_s: self.state.time_s,
            });
        }
        // STU4-Sim c1e3fc8, Systems/Radar.py sorts noisy center position,
        // independently of its unclipped closest-range and diameter samples.
        rows.sort_by(|a, b| norm(a.relative_position_m).total_cmp(&norm(b.relative_position_m)));
        self.state.observations = rows;
        self.sample_sensor_deliveries();
    }
}

fn initial(seed: u64, experiment: Option<ExperimentSettings>) -> Result<Snapshot, String> {
    initial_with_overrides(seed, experiment, None, None)
}

fn initial_with_overrides(
    seed: u64,
    experiment: Option<ExperimentSettings>,
    ship_tuning_override: Option<ShipTuning>,
    debris_flow_override: Option<DebrisFlowTuning>,
) -> Result<Snapshot, String> {
    initial_for_flight(
        seed,
        experiment,
        ship_tuning_override,
        debris_flow_override,
        FlightPreset::default(),
    )
}

fn initial_for_flight(
    seed: u64,
    experiment: Option<ExperimentSettings>,
    ship_tuning_override: Option<ShipTuning>,
    debris_flow_override: Option<DebrisFlowTuning>,
    preset: FlightPreset,
) -> Result<Snapshot, String> {
    if experiment.is_none() && !preset.is_default() {
        return Err(
            "Archived None corridor supports only far_side_l2; configure a new flight for L1."
                .into(),
        );
    }
    if experiment.is_some_and(|experiment| {
        matches!(
            experiment.debris.distribution,
            DebrisDistribution::ImpactEjecta { .. }
        )
    }) && debris_flow_override.is_some()
    {
        return Err(
            "Impact ejecta cannot carry a live stream override; configure a new flight.".into(),
        );
    }
    let mut rng = seed.max(1);
    let capacity = experiment.map_or(settings().debris.count, |config| config.debris.count) + 3;
    let mut assets = Vec::with_capacity(capacity);
    let site = crate::model::landing_site_for(preset);
    let cfg = settings();
    let flight = cfg.flight_preset(preset);
    let mut ship = entity(
        EntityId::Lander,
        EntityKind::Lander,
        site.lander_position_m,
        [0.0; 3],
        cfg.lander.proxy_radius_m,
        experiment.map_or(cfg.lander.mass_kg, |settings| settings.ship.mass_kg),
    );
    ship.name = "Lander".into();
    ship.rotation = site.rotation;
    ship.status = EntityStatus::Landed;
    if let Some(tuning) = ship_tuning_override {
        tuning.validate()?;
        ship.mass_kg = tuning.mass_kg;
        ship.actuator_gain = tuning.thrust_scale;
        ship.max_thrust_n = local_max_thrust_for_mass_n(tuning.mass_kg) * tuning.thrust_scale;
    }
    if let Some(flow) = debris_flow_override {
        flow.validate()?;
    }
    assets.push(ship);
    let env = crate::astrophysics::environment(cfg.gravity);
    let target_position = match flight.target_region {
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
    target.name = flight.target_name.clone();
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
    if let Some(experiment) = experiment {
        let debris =
            debris_flow_override.map_or(experiment.debris, |flow| flow.apply_to(experiment.debris));
        let mut spawn_index = spawn_index(&assets, debris.radius_max_m);
        // Streams start around every side of Phobos and later recycle at the
        // upstream plane. Impact mode instead selects a source patch and never
        // recycles. Both modes use only gravity and real contacts after birth,
        // never a per-frame velocity/orbit constraint.
        for i in 0..experiment.debris.count {
            // A quadratic size sample keeps plenty of small rocks without
            // excluding the large end of the configured physical radius range.
            let size = uniform(&mut rng);
            let radius =
                debris.radius_min_m + (debris.radius_max_m - debris.radius_min_m) * size * size;
            let position = debris_birth_position(
                &mut rng, debris, radius, &spawn_index, None, DebrisBirth::InitialShell,
            ).ok_or("experiment shell cannot safely place its population within the bounded birth budget")?;
            let velocity = debris::initial_velocity(&mut rng, debris, position);
            let id = EntityId::Debris((i + 1) as u32);
            let mut asteroid = entity(
                id,
                EntityKind::Debris,
                position,
                velocity,
                radius,
                debris_mass_kg(radius),
            );
            asteroid.name = format!("Debris {}", i + 1);
            asteroid.angular_velocity_rad_s = debris_birth_spin(&mut rng);
            spawn_index.insert(id, position, radius);
            assets.push(asteroid);
        }
    } else {
        // Archive-only branch. Keep the original draw order and fixed corridor,
        // including seeded radii, so new stream defaults cannot bless altered saves.
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
                EntityId::Debris((i + 1) as u32),
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
            if let Some(flow) = debris_flow_override {
                // Keep the old draw order and isotropic corridor initializer.
                // A live calibration adds only its common-velocity delta.
                let reference = settings().legacy_corridor_flow_reference;
                let delta = sub(flow.common_velocity_mps(), reference.common_velocity_mps());
                e.velocity = std::array::from_fn(|axis| e.velocity[axis] + delta[axis]);
            }
            assets.push(e);
        }
    }
    let initial_distance = norm(sub(site.lander_position_m, target_position));
    Ok(Snapshot {
        model: profile_description_for(experiment, preset),
        time_s: 0.0,
        seed,
        flight_preset: preset,
        launch_site_direction: None,
        trial_start_launch_site_direction: None,
        frame: cfg.frame_description.clone(),
        body: body(),
        environment: env,
        experiment_settings: experiment,
        ship_tuning_override,
        debris_flow_override,
        tuning_revision: 0,
        assets,
        events: vec![],
        collisions: 0,
        manual_interventions: 0,
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
            target_region: flight.target_region,
        },
        observations: vec![],
        rng_state: rng,
        active_contacts: vec![],
        sensor_state: Some(crate::model::SensorState::new(seed)),
    })
}

const MAX_BIRTH_ATTEMPTS: usize = 64;
const MAX_RECYCLES_PER_STEP: usize = 4;
const BIRTH_CLEARANCE_M: f64 = 1.0;

#[derive(Clone, Copy)]
enum DebrisBirth {
    InitialShell,
    UpstreamInlet,
}

fn flow_half_extent_m(cfg: DebrisExperimentSettings) -> f64 {
    crate::model::phobos_enclosing_radius_m() + cfg.shell_outer_m + cfg.radius_max_m + 100.0
}

fn random_direction(rng: &mut u64) -> [f64; 3] {
    let z = uniform(rng) * 2.0 - 1.0;
    let longitude = uniform(rng) * std::f64::consts::TAU;
    let equatorial = (1.0 - z * z).sqrt();
    [
        equatorial * longitude.cos(),
        equatorial * longitude.sin(),
        z,
    ]
}

fn debris_birth_position(
    rng: &mut u64,
    cfg: DebrisExperimentSettings,
    radius: f64,
    spawn_index: &SpawnIndex,
    ignored: Option<EntityId>,
    birth: DebrisBirth,
) -> Option<[f64; 3]> {
    // At most 64 terrain queries and indexed actor-neighborhood checks. An
    // impossible arrangement is an atomic configure error, never an endless loop.
    let shape = phobos_shape();
    for _ in 0..MAX_BIRTH_ATTEMPTS {
        let position = match birth {
            DebrisBirth::InitialShell => {
                let direction = match cfg.distribution {
                    DebrisDistribution::DirectedStream => random_direction(rng),
                    DebrisDistribution::ImpactEjecta { settings } => {
                        debris::source_direction(rng, settings)
                    }
                };
                let terrain_radius =
                    crate::terrain::surface_radius_m(bevy::math::DVec3::from_array(direction));
                let margin =
                    cfg.shell_inner_m + uniform(rng) * (cfg.shell_outer_m - cfg.shell_inner_m);
                scale(direction, terrain_radius + radius + margin)
            }
            DebrisBirth::UpstreamInlet => {
                let extent = flow_half_extent_m(cfg) - radius - BIRTH_CLEARANCE_M;
                let axis = cfg.stream_axis.index();
                let mut position = [0.0; 3];
                for (index, component) in position.iter_mut().enumerate() {
                    *component = if index == axis {
                        -f64::from(cfg.stream_sign) * extent
                    } else {
                        (uniform(rng) * 2.0 - 1.0) * extent
                    };
                }
                position
            }
        };
        let point = Vector::from_array(position);
        let direction = bevy::math::DVec3::from_array(position).normalize();
        if norm(position) <= crate::terrain::surface_radius_m(direction)
            || shape.distance_to_local_point(point, false) <= radius + BIRTH_CLEARANCE_M
        {
            continue;
        }
        if spawn_index.is_clear(position, radius, BIRTH_CLEARANCE_M, ignored) {
            return Some(position);
        }
    }
    None
}

fn spawn_index(assets: &[EntityState], maximum_debris_radius_m: f64) -> SpawnIndex {
    let maximum_radius_m = maximum_debris_radius_m
        .max(norm(settings().pad.half_extents_m))
        .max(settings().lander.proxy_radius_m * 2.0)
        .max(settings().mission.target_radius_m);
    let mut index = SpawnIndex::new(maximum_radius_m);
    for asset in assets {
        let radius = match asset.kind {
            EntityKind::LandingPad => norm(settings().pad.half_extents_m),
            EntityKind::Lander => settings().lander.proxy_radius_m * 2.0,
            EntityKind::ReturnShip | EntityKind::Debris => asset.radius_m,
        };
        index.insert(asset.id, asset.position, radius);
    }
    index
}

fn debris_birth_velocity(rng: &mut u64, cfg: DebrisExperimentSettings) -> [f64; 3] {
    let mut velocity = scale(random_direction(rng), uniform(rng) * cfg.speed_spread_mps);
    velocity[cfg.stream_axis.index()] += f64::from(cfg.stream_sign) * cfg.speed_mps;
    velocity
}

fn debris_birth_spin(rng: &mut u64) -> [f64; 3] {
    scale(
        random_direction(rng),
        uniform(rng) * settings().debris.spin_max_rad_s,
    )
}

fn debris_mass_kg(radius: f64) -> f64 {
    4.0 / 3.0 * std::f64::consts::PI * radius.powi(3) * settings().debris.density_kg_m3
}

#[cfg(test)]
fn profile_description(experiment: Option<ExperimentSettings>) -> String {
    profile_description_for(experiment, FlightPreset::default())
}

fn profile_base_description(
    experiment: Option<ExperimentSettings>,
    preset: FlightPreset,
) -> String {
    match experiment {
        None => settings().description.clone(),
        Some(experiment) => match (preset, experiment.debris.distribution) {
            (FlightPreset::FarSideL2, DebrisDistribution::DirectedStream) => {
                settings().experiment_description.clone()
            }
            (_, DebrisDistribution::DirectedStream) => format!(
                "local_experiment_v1: {}; directional stream initial/inlet velocities, real rigid-body contacts and estimated fuel; not STP parity.",
                settings().flight_preset(preset).label
            ),
            (_, DebrisDistribution::ImpactEjecta { .. }) => format!(
                "local_experiment_v1: {}; post-impact cloud initial conditions with gravity and real contacts, not a fracture solver or guaranteed orbit; not STP parity.",
                settings().flight_preset(preset).label
            ),
        },
    }
}

fn profile_description_for(experiment: Option<ExperimentSettings>, preset: FlightPreset) -> String {
    let base = profile_base_description(experiment, preset);
    match experiment {
        None => base,
        Some(experiment) if experiment == settings().experiment_settings => base,
        Some(_) => format!("{base} [custom settings]"),
    }
}

fn profile_description_matches(state: &Snapshot) -> bool {
    match state.experiment_settings {
        None => state.model == settings().description,
        // These are descriptive v1 labels, not physical profile identifiers.
        // A former default becomes custom when scenario defaults change. Keep
        // both exact labels, while validating every physical setting below.
        Some(_) => {
            let base = profile_base_description(state.experiment_settings, state.flight_preset);
            state.model == base || state.model == format!("{base} [custom settings]")
        }
    }
}

fn validate_seed(seed: u64) -> Result<(), String> {
    if seed > u32::MAX as u64 {
        Err("seed must be a whole number from 0 to 4294967295".into())
    } else {
        Ok(())
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
            local_max_thrust_for_mass_n(m)
        } else {
            0.0
        },
        thrust_n: [0.0; 3],
        actuator_gain: 1.0,
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
        && e.mass_kg == settings().lander.mass_kg
        && e.actuator_gain == 1.0
        && matches!(
            e.thrust_application,
            ThrustApplication::CenterOfMass | ThrustApplication::ManualNozzles
        )
        && e.max_thrust_n == settings().lander.mass_kg * settings().lander.max_accel_mps2
}
fn validate(s: &Snapshot) -> Result<(), String> {
    resilience::validate_sensor_state(s)?;
    if let Some(experiment) = s.experiment_settings {
        experiment.validate()?;
    }
    if let Some(ship) = s.ship_tuning_override {
        ship.validate()?;
    }
    if let Some(flow) = s.debris_flow_override {
        flow.validate()?;
    }
    if s.experiment_settings.is_none() && !s.flight_preset.is_default() {
        return Err(
            "Archived None corridor supports only far_side_l2; configure a new flight for L1."
                .into(),
        );
    }
    if s.experiment_settings.is_some_and(|experiment| {
        matches!(
            experiment.debris.distribution,
            DebrisDistribution::ImpactEjecta { .. }
        )
    }) && s.debris_flow_override.is_some()
    {
        return Err(
            "Impact ejecta cannot carry a live stream override; configure a new flight.".into(),
        );
    }
    if s.tuning_revision == 0
        && (s.ship_tuning_override.is_some() || s.debris_flow_override.is_some())
    {
        return Err("live tuning overrides require a nonzero tuning_revision".into());
    }
    let ship_tuning = s.ship_tuning();
    let debris_count = s
        .experiment_settings
        .map_or(settings().debris.count, |config| config.debris.count);
    let radar_count = settings().debris.radar_count.min(debris_count);
    // Every possible unordered pair is bounded by the validated physical count.
    // Keep full contact history, rather than truncating it and double-counting
    // real contacts after restoring a dense population.
    let contact_limit = if s.experiment_settings.is_some() {
        let bodies = debris_count as u64 + 4;
        bodies * (bodies - 1) / 2
    } else {
        settings().limits.contacts as u64
    };
    if !s.time_s.is_finite()
        || s.time_s < 0.0
        || s.seed > u32::MAX as u64
        || s.assets.len() != debris_count + 3
        || s.events.len() > settings().limits.events
        || s.observations.len() > radar_count
        || s.active_contacts.len() as u64 > contact_limit
        || (s.experiment_settings.is_some() && s.rng_state == 0)
        || s.body.name != "Phobos"
        || s.body.radii_m != settings().gravity.phobos_radii_m
        || s.body.mu_m3_s2 != settings().gravity.phobos_mu_m3_s2
        || s.body.rotation_period_s != settings().gravity.phobos_rotation_period_s
        || s.frame != settings().frame_description
        || !profile_description_matches(s)
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
        || s.mission.target_region != settings().flight_preset(s.flight_preset).target_region
        || s.mission.score.is_some_and(|v| !v.is_finite())
    {
        return Err("invalid or client-modified fixed configuration".into());
    }
    let mut ids = HashSet::new();
    // Always seed against the original preset. Moving a pad must never change
    // the acceptance RNG that determines later debris IDs/radii in an archive.
    let fixed = initial_for_flight(s.seed, s.experiment_settings, None, None, s.flight_preset)?;
    let custom_site = s
        .launch_site_direction
        .map(surface_placement::archived_launch_site)
        .transpose()?;
    let start_site = s
        .trial_start_launch_site_direction
        .map(surface_placement::archived_launch_site)
        .transpose()?;
    if (start_site.is_some() && custom_site.is_none())
        || (custom_site.is_some()
            && s.manual_interventions == 0
            && s.launch_site_direction != s.trial_start_launch_site_direction)
    {
        return Err(
            "custom launch site lacks its recorded intervention or trial-start anchor".into(),
        );
    }
    let initial_distance = start_site.map_or(fixed.mission.initial_distance_m, |site| {
        let target = fixed
            .assets
            .iter()
            .find(|asset| asset.id == EntityId::ReturnShip)
            .expect("canonical preset has a target");
        norm(sub(site.lander_position_m, target.position))
    });
    if s.mission.initial_distance_m != initial_distance {
        return Err("mission initial range differs from the configured launch and target".into());
    }
    let expected: HashMap<EntityId, &EntityState> =
        fixed.assets.iter().map(|e| (e.id, e)).collect();
    // The exact population length was checked above. Unique IDs, all present in
    // this seeded map, prove every configured actor and mission ID is included.
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
            || !e.actuator_gain.is_finite()
            || !(0.1..=10.0).contains(&e.actuator_gain)
            || !e.radius_m.is_finite()
            || e.radius_m <= 0.0
            || e.radius_m > settings().limits.radius_m
            || !(0.99..=1.01).contains(&qn)
            || !accel_within_limits(e.thrust_application, e.commanded_accel_mps2)
            || !accel_within_limits(
                e.thrust_application,
                scale(e.thrust_n, 1.0 / (e.mass_kg * e.actuator_gain)),
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
        let (mass_kg, max_thrust_n, actuator_gain) = if e.id == EntityId::Lander {
            (
                ship_tuning.mass_kg,
                local_max_thrust_for_mass_n(ship_tuning.mass_kg) * ship_tuning.thrust_scale,
                ship_tuning.thrust_scale,
            )
        } else {
            (fixed.mass_kg, fixed.max_thrust_n, 1.0)
        };
        let legacy_cap = s.ship_tuning_override.is_none() && has_legacy_lander_thrust_cap(e);
        if mass_kg != e.mass_kg
            || fixed.radius_m != e.radius_m
            || fixed.kind != e.kind
            || (max_thrust_n != e.max_thrust_n && !legacy_cap)
            || actuator_gain != e.actuator_gain
            || fixed.name != e.name
        {
            return Err(format!(
                "fixed settings differ for {}: mass {:?} expected {:?}, radius {:?} expected {:?}, max thrust {:?} expected {:?}",
                e.id, e.mass_kg, mass_kg, e.radius_m, fixed.radius_m, e.max_thrust_n, max_thrust_n
            ));
        }
        match e.thrust_application {
            // Keep legacy center snapshots compatible, including the historically
            // optional fields. from_state reconciles reports with the commands.
            ThrustApplication::CenterOfMass => {
                if e.id == EntityId::Lander && s.ship_tuning_override.is_some() {
                    let mut expected = e.clone();
                    reconcile_ship_wrench(&mut expected, ship_tuning.inertia_kg_m2());
                    if !vectors_match(e.thrust_n, expected.thrust_n)
                        || !vectors_match(e.torque_nm, expected.torque_nm)
                    {
                        return Err(
                            "tuned ship wrench differs from its requested command and gain".into(),
                        );
                    }
                }
            }
            ThrustApplication::ManualNozzles | ThrustApplication::PracticeBoost => {
                if e.id != EntityId::Lander || e.kind != EntityKind::Lander {
                    return Err("only the lander can use manual or practice-boost nozzles".into());
                }
                if e.commanded_angular_accel_rad_s2 != [0.0; 3] {
                    return Err("nozzle modes cannot include a direct angular command".into());
                }
                let force = scale(e.commanded_accel_mps2, e.mass_kg * e.actuator_gain);
                let wrench = applied_wrench(&applied_jets(e));
                if !vectors_match(e.thrust_n, force)
                    || !vectors_match(wrench.force_body_n, force)
                    || !vectors_match(e.torque_nm, wrench.torque_body_nm)
                {
                    return Err("nozzle wrench differs from its applied forces".into());
                }
            }
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
        if e.id == EntityId::LandingPad
            && let Some(site) = custom_site
            && (!vectors_match(e.position, site.pad_position_m)
                || !surface_placement::same_orientation(e.rotation, site.rotation)
                || e.velocity != [0.0; 3]
                || e.angular_velocity_rad_s != [0.0; 3])
        {
            return Err("landing pad pose differs from its saved Phobos surface anchor".into());
        }
    }
    let mut contact_ids = HashSet::new();
    if s.active_contacts.iter().any(|pair| {
        pair[0] >= pair[1]
            || !contact_ids.insert(*pair)
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
        // The published radar adds Gaussian noise without clipping these samples.
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
    static SHAPE: std::sync::OnceLock<SharedShape> = std::sync::OnceLock::new();
    SHAPE
        .get_or_init(|| {
            let (vertices, indices) = crate::model::phobos_geometry();
            SharedShape::trimesh(
                vertices.into_iter().map(Vector::from_array).collect(),
                indices,
            )
            .expect("generated Phobos terrain is a valid triangle mesh")
        })
        .clone()
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
fn clamp_vec(v: [f64; 3], limit: f64) -> [f64; 3] {
    let magnitude = norm(v);
    if magnitude > limit {
        let mut bounded = if magnitude.is_finite() {
            scale(v, limit / magnitude)
        } else {
            // Finite components can still have an unrepresentable magnitude.
            // Divide components first so normalization keeps their direction.
            let largest = v.into_iter().map(f64::abs).fold(0.0, f64::max);
            let scaled = v.map(|component| component / largest);
            scale(scaled, limit / norm(scaled))
        };
        let mut bounded_magnitude = norm(bounded);
        while bounded_magnitude > limit {
            // Correct outward rounding without relaxing the actuator limit.
            // next_down also guarantees progress if the ratio rounds to one.
            bounded = scale(
                bounded,
                (limit / bounded_magnitude).min(1.0_f64.next_down()),
            );
            bounded_magnitude = norm(bounded);
        }
        bounded
    } else {
        v
    }
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
#[path = "physics/experiment_tests.rs"]
mod experiment_tests;

#[cfg(test)]
#[path = "physics/tuning_tests.rs"]
mod tuning_tests;

#[cfg(test)]
#[path = "physics/lookahead_tests.rs"]
mod lookahead_tests;

#[cfg(test)]
#[path = "physics/applied_tests.rs"]
mod applied_tests;

#[cfg(test)]
#[path = "physics/default_flow_tests.rs"]
mod default_flow_tests;

#[cfg(test)]
#[path = "physics/flight_tests.rs"]
mod flight_tests;

#[cfg(test)]
#[path = "physics/surface_placement_tests.rs"]
mod surface_placement_tests;

#[cfg(test)]
#[path = "physics/debris_tests.rs"]
mod debris_tests;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn public_observations_supply_known_geometry_without_hidden_obstacles_or_side_effects() {
        let simulation = Simulation::new_default();
        let before = serde_json::to_string(&simulation.snapshot()).unwrap();
        let observation = simulation.observation();
        assert_eq!(observation["manual_interventions"], 0);
        assert_eq!(observation["rotation_convention"], "active_xyzw");
        assert_eq!(observation["radar_clock"], "simulation_elapsed_seconds");
        assert_eq!(
            observation["geometry"]["debris_count"],
            settings().debris.count
        );
        assert_eq!(
            observation["geometry"]["arrival_radius_m"],
            settings().mission.arrival_radius_m
        );
        assert_eq!(
            observation["geometry"]["phobos_enclosing_radius_m"],
            crate::model::phobos_enclosing_radius_m()
        );
        assert!(
            observation["geometry"]["terrain_radius_at_shuttle_m"]
                .as_f64()
                .unwrap()
                > 0.0
        );
        assert!(observation.get("assets").is_none());
        let radar = observation["radar"].as_array().unwrap();
        assert_eq!(radar.len(), settings().debris.radar_count);
        let source = simulation.radar(settings().debris.radar_count);
        assert!(source.windows(2).all(|pair| {
            norm(pair[0].relative_position_m) <= norm(pair[1].relative_position_m)
        }));
        for (row, source) in radar.iter().zip(&source) {
            assert_eq!(row.as_array().unwrap().len(), 3);
            assert!(
                row.as_array()
                    .unwrap()
                    .iter()
                    .all(|part| part.as_array().unwrap().len() == 3)
            );
            assert_eq!(row[0], json!(source.relative_position_m));
            assert_eq!(
                row[1],
                json!([source.range_m, source.range_rate_mps, source.diameter_m])
            );
            assert_eq!(row[2], json!([source.measurement_time_s, 0.0, 0.0]));
            assert!(row.get("id").is_none());
            assert!(row.get("relative_velocity_mps").is_none());
        }
        assert_eq!(
            before,
            serde_json::to_string(&simulation.snapshot()).unwrap()
        );
    }

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

    fn tumble_with_real_nozzles(sim: &mut Simulation) {
        let pad_up = *sim.bodies[sim.handles[&EntityId::LandingPad]].rotation() * Vector::Z;
        let dt = settings().integration.fixed_step_s;
        sim.set_manual_thrust([0.0, settings().lander.max_accel_mps2, 0.0])
            .unwrap();
        for _ in 0..(12.0 / dt).ceil() as usize {
            sim.advance(dt);
            let rb = rigid_body(sim);
            if pad_up.dot(*rb.rotation() * Vector::Z) < 0.0 && rb.angvel().length() > 0.05 {
                return;
            }
        }
        panic!("the real manual side jets must create a tumbling lander");
    }

    #[test]
    fn debug_upright_changes_actual_rotation_and_spin_without_braking_or_restarting() {
        let mut sim = Simulation::new_default();
        tumble_with_real_nozzles(&mut sim);
        sim.set_command([0.2, 0.1, 0.0], [0.01, 0.0, 0.0]).unwrap();
        let before = sim.snapshot();
        let rb = rigid_body(&sim);
        let before_position = rb.translation();
        let before_velocity = rb.linvel();
        let before_rotation = *rb.rotation();
        let before_omega = rb.angvel();
        let mass_properties = rb.mass_properties().local_mprops;
        let pad_rotation = *sim.bodies[sim.handles[&EntityId::LandingPad]].rotation();
        assert!(before_omega.length() > 0.05);
        assert!((before_rotation * Vector::Z).dot(pad_rotation * Vector::Z) < 0.0);
        assert!(rb.user_force().length() > 0.0);
        assert!(rb.user_torque().length() > 0.0);
        assert!(before.mission.fuel_used_kg > 0.0);

        let after = sim.execute(PhysicsCommand::DebugUpright).unwrap();
        let rb = rigid_body(&sim);
        assert_eq!(*rb.rotation(), pad_rotation);
        for axis in [Vector::X, Vector::Y, Vector::Z] {
            assert_vector_close(*rb.rotation() * axis, pad_rotation * axis, 1e-12);
        }
        assert_ne!(*rb.rotation(), before_rotation);
        assert_eq!(rb.angvel(), Vector::ZERO);
        assert_eq!(rb.translation(), before_position);
        assert_eq!(rb.linvel(), before_velocity);
        assert_eq!(rb.user_force(), Vector::ZERO);
        assert_eq!(rb.user_torque(), Vector::ZERO);
        assert_eq!(rb.mass_properties().local_mprops, mass_properties);
        let ship = lander(&sim);
        assert_eq!(rotation(ship), *rb.rotation());
        assert_eq!(ship.angular_velocity_rad_s, [0.0; 3]);
        assert_eq!(ship.thrust_n, [0.0; 3]);
        assert_eq!(ship.torque_nm, [0.0; 3]);
        assert_eq!(ship.commanded_accel_mps2, [0.0; 3]);
        assert_eq!(ship.commanded_angular_accel_rad_s2, [0.0; 3]);
        assert!(applied_jets(ship).is_empty());
        assert_eq!(after.manual_interventions, before.manual_interventions + 1);
        let reset_event = after.events.last().unwrap();
        assert_eq!(reset_event.t_s, before.time_s);
        assert_eq!(reset_event.asset, EntityId::Lander);
        assert_eq!(reset_event.message, "Local test: upright reset");
        assert_eq!(sim.observation()["manual_interventions"], 1);

        // Compare everything else, including contacts, sensor readings, RNG,
        // mission outcome, hardware, mass, fuel, time and all other actors.
        let mut expected = before;
        let expected_ship = lander_mut(&mut expected);
        expected_ship.rotation = ship.rotation;
        expected_ship.angular_velocity_rad_s = [0.0; 3];
        expected_ship.thrust_n = [0.0; 3];
        expected_ship.torque_nm = [0.0; 3];
        expected_ship.commanded_accel_mps2 = [0.0; 3];
        expected_ship.commanded_angular_accel_rad_s2 = [0.0; 3];
        expected_ship.status = EntityStatus::Coasting;
        expected.manual_interventions += 1;
        event(&mut expected, EntityId::Lander, "Local test: upright reset");
        assert_eq!(
            serde_json::to_value(after).unwrap(),
            serde_json::to_value(expected).unwrap()
        );
    }

    #[test]
    fn debug_upright_uses_the_restored_pad_axes_instead_of_default_launch_axes() {
        let mut sim = Simulation::new_default();
        let mut state = sim.snapshot();
        let pad = state
            .assets
            .iter_mut()
            .find(|e| e.id == EntityId::LandingPad)
            .unwrap();
        let pad_rotation = rotation(pad) * Rotation::from_rotation_z(0.7);
        pad.rotation = [
            pad_rotation.x,
            pad_rotation.y,
            pad_rotation.z,
            pad_rotation.w,
        ];
        sim.execute(PhysicsCommand::Restore {
            state: Box::new(state),
        })
        .unwrap();
        tumble_with_real_nozzles(&mut sim);
        let actual_pad_rotation = *sim.bodies[sim.handles[&EntityId::LandingPad]].rotation();
        let default = crate::model::landing_site().rotation;
        assert_ne!(
            [
                actual_pad_rotation.x,
                actual_pad_rotation.y,
                actual_pad_rotation.z,
                actual_pad_rotation.w
            ],
            default
        );
        sim.debug_upright().unwrap();
        assert_eq!(*rigid_body(&sim).rotation(), actual_pad_rotation);
        assert_eq!(rotation(lander(&sim)), actual_pad_rotation);
        assert_eq!(rigid_body(&sim).angvel(), Vector::ZERO);
    }

    #[test]
    fn debug_upright_preserves_a_real_terminal_collision_outcome_and_cannot_revive_it() {
        let mut sim = Simulation::new_default();
        let max_accel = settings().lander.max_accel_mps2;
        let pad_up = *sim.bodies[sim.handles[&EntityId::LandingPad]].rotation() * Vector::Z;
        sim.set_command([0.0, 0.0, max_accel], [0.0; 3]).unwrap();
        sim.advance(8.0);
        sim.set_manual_thrust([0.0, max_accel, 0.0]).unwrap();
        sim.advance(1.0);
        let handle = sim.handles[&EntityId::Lander];
        let mass = sim.bodies[handle].mass();
        // A real downward impulse produces a damaging Rapier contact. No pose,
        // velocity report, damage counter or mission result is fabricated.
        sim.bodies[handle].apply_impulse(-pad_up * mass * 60.0, true);
        sim.advance(2.0);
        assert_eq!(sim.mission_status(), MissionStatus::FailedDamage);
        assert!(sim.state.mission.damage > settings().mission.impact_threshold_j);
        assert!(sim.state.collisions > 0);
        let before = sim.snapshot();
        let position = rigid_body(&sim).translation();
        let velocity = rigid_body(&sim).linvel();
        let pose = *rigid_body(&sim).position();
        let spin = rigid_body(&sim).angvel();
        let force = rigid_body(&sim).user_force();
        let torque = rigid_body(&sim).user_torque();
        let tiny = f64::from_bits(1);
        for (accel, angular_accel) in [
            ([f64::MAX, f64::MAX, 0.0], [0.0; 3]),
            ([0.0; 3], [f64::MAX, -f64::MAX, f64::MAX]),
            ([tiny, 0.0, 0.0], [0.0; 3]),
            ([0.0; 3], [0.0, tiny, 0.0]),
        ] {
            let error = sim
                .execute(PhysicsCommand::SetCommand {
                    accel,
                    angular_accel,
                })
                .unwrap_err();
            assert_eq!(
                error,
                "Mission finished. Reset before commanding another flight."
            );
            assert_eq!(
                serde_json::to_value(sim.snapshot()).unwrap(),
                serde_json::to_value(&before).unwrap()
            );
            assert_eq!(*rigid_body(&sim).position(), pose);
            assert_eq!(rigid_body(&sim).linvel(), velocity);
            assert_eq!(rigid_body(&sim).angvel(), spin);
            assert_eq!(rigid_body(&sim).user_force(), force);
            assert_eq!(rigid_body(&sim).user_torque(), torque);
        }
        let after = sim.execute(PhysicsCommand::DebugUpright).unwrap();
        assert_eq!(
            serde_json::to_value(&after.mission).unwrap(),
            serde_json::to_value(&before.mission).unwrap()
        );
        assert_eq!(rigid_body(&sim).translation(), position);
        assert_eq!(rigid_body(&sim).linvel(), velocity);
        assert_eq!(after.time_s, before.time_s);
        assert_eq!(after.collisions, before.collisions);
        assert_eq!(after.active_contacts, before.active_contacts);
        assert_eq!(after.manual_interventions, 1);
        assert!(sim.set_command([0.0, 0.0, max_accel], [0.0; 3]).is_err());
        let stopped = serde_json::to_value(sim.snapshot()).unwrap();
        sim.advance(1.0);
        assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), stopped);
    }

    #[test]
    fn debug_upright_counter_round_trips_defaults_for_old_snapshots_and_resets_with_a_new_run() {
        let mut sim = Simulation::new_default();
        sim.debug_upright().unwrap();
        sim.debug_upright().unwrap();
        assert_eq!(
            sim.manual_interventions(),
            2,
            "even an already upright reset is an intervention"
        );
        let exported = serde_json::to_value(sim.snapshot()).unwrap();
        assert_eq!(exported["manual_interventions"], 2);
        let restored = serde_json::from_value(exported.clone()).unwrap();
        sim.execute(PhysicsCommand::Reset { seed: None }).unwrap();
        assert_eq!(sim.manual_interventions(), 0);
        sim.execute(PhysicsCommand::Restore {
            state: Box::new(restored),
        })
        .unwrap();
        assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), exported);
        assert_eq!(sim.observation()["manual_interventions"], 2);

        let mut legacy = exported;
        legacy
            .as_object_mut()
            .unwrap()
            .remove("manual_interventions");
        let state = serde_json::from_value(legacy).unwrap();
        sim.execute(PhysicsCommand::Restore {
            state: Box::new(state),
        })
        .unwrap();
        assert_eq!(sim.manual_interventions(), 0);
    }

    #[test]
    fn debug_upright_missing_bodies_or_exhausted_counter_do_not_partially_mutate() {
        for invalid in 0..4 {
            let mut sim = Simulation::new_default();
            sim.set_manual_thrust([0.0, settings().lander.max_accel_mps2, 0.0])
                .unwrap();
            sim.advance(settings().integration.fixed_step_s);
            let handle = sim.handles[&EntityId::Lander];
            match invalid {
                0 => {
                    sim.handles.remove(&EntityId::LandingPad);
                }
                1 => {
                    sim.handles.remove(&EntityId::Lander);
                }
                2 => sim
                    .state
                    .assets
                    .retain(|entity| entity.id != EntityId::Lander),
                3 => sim.state.manual_interventions = u32::MAX,
                _ => unreachable!(),
            }
            let before_pose = *sim.bodies[handle].position();
            let before_omega = sim.bodies[handle].angvel();
            let before_force = sim.bodies[handle].user_force();
            let before_torque = sim.bodies[handle].user_torque();
            let before = serde_json::to_value(sim.snapshot()).unwrap();
            assert!(sim.execute(PhysicsCommand::DebugUpright).is_err());
            assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
            assert_eq!(*sim.bodies[handle].position(), before_pose);
            assert_eq!(sim.bodies[handle].angvel(), before_omega);
            assert_eq!(sim.bodies[handle].user_force(), before_force);
            assert_eq!(sim.bodies[handle].user_torque(), before_torque);
        }
    }

    #[test]
    fn radar_uses_noisy_center_sorting_and_unclipped_measurements_without_extra_rng_draws() {
        let mut state = Simulation::new_default().snapshot();
        let shuttle_position = lander_mut(&mut state).position;
        let debris = state
            .assets
            .iter_mut()
            .filter(|entity| entity.kind == EntityKind::Debris)
            .take(settings().debris.radar_count)
            .max_by(|a, b| a.radius_m.total_cmp(&b.radius_m))
            .unwrap();
        let nearby_id = debris.id;
        debris.position = [
            shuttle_position[0] + 0.5 * debris.radius_m,
            shuttle_position[1],
            shuttle_position[2],
        ];
        let mut sim = Simulation::from_state(state);
        let mut expected_rng = sim.rng;
        let shuttle = lander(&sim).clone();
        let cfg = settings();
        let mut expected = Vec::new();
        for debris in sim
            .state
            .assets
            .iter()
            .filter(|entity| entity.kind == EntityKind::Debris)
            .take(cfg.debris.radar_count)
        {
            let delta = sub(debris.position, shuttle.position);
            let range = norm(delta);
            let position = std::array::from_fn(|i| {
                delta[i] + gaussian(&mut expected_rng) * cfg.sensors.range_noise_m
            });
            let rate = dot(sub(debris.velocity, shuttle.velocity), delta) / range
                + gaussian(&mut expected_rng) * cfg.sensors.range_rate_noise_mps;
            let closest =
                range - debris.radius_m + gaussian(&mut expected_rng) * cfg.sensors.range_noise_m;
            let diameter = debris.radius_m
                * 2.0
                * (1.0 + gaussian(&mut expected_rng) * cfg.debris.diameter_noise_fraction);
            expected.push((debris.id, position, closest, rate, diameter));
        }
        expected.sort_by(|a, b| norm(a.1).total_cmp(&norm(b.1)));
        sim.sample_radar();
        assert_eq!(sim.rng, expected_rng);
        for (actual, (id, position, closest, rate, diameter)) in
            sim.state.observations.iter().zip(expected)
        {
            assert_eq!(actual.id, id);
            assert_eq!(actual.relative_position_m, position);
            assert_eq!(actual.range_m, closest);
            assert_eq!(actual.range_rate_mps, rate);
            assert_eq!(actual.diameter_m, diameter);
        }
        assert!(
            sim.state
                .observations
                .iter()
                .find(|row| row.id == nearby_id)
                .unwrap()
                .range_m
                < 0.0
        );
        assert!(
            sim.state
                .observations
                .windows(2)
                .any(|pair| pair[0].range_m > pair[1].range_m),
            "closest-range sorting must not replace noisy center sorting"
        );
    }

    #[test]
    fn restore_accepts_signed_radar_samples_but_keeps_finite_measurement_validation() {
        let mut sim = Simulation::new_default();
        let mut state = sim.snapshot();
        state.observations[0].range_m = -1.0;
        state.observations[0].diameter_m = -0.1;
        validate(&state).unwrap();
        sim.execute(PhysicsCommand::Restore {
            state: Box::new(state.clone()),
        })
        .unwrap();
        assert_eq!(sim.snapshot().observations[0].range_m, -1.0);
        assert_eq!(sim.snapshot().observations[0].diameter_m, -0.1);
        state.observations[0].diameter_m = 0.0;
        validate(&state).unwrap();
        for invalid in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            state.observations[0].range_m = invalid;
            assert!(validate(&state).is_err());
            state.observations[0].range_m = -1.0;
            state.observations[0].diameter_m = invalid;
            assert!(validate(&state).is_err());
            state.observations[0].diameter_m = -0.1;
        }
    }

    #[test]
    fn restore_rejects_seeded_identity_and_kind_status_tampering_without_mutation() {
        let mut config = ExperimentSettings::default();
        config.debris.count = 2;
        config.ship.mass_kg = 8_123.0;
        config.ship.inertia_scale = 2.5;
        let mut sim = Simulation::with_experiment(42, config).unwrap();
        sim.set_command([0.1, 0.2, 0.3], [0.01, 0.02, 0.03])
            .unwrap();
        sim.advance(settings().integration.fixed_step_s);
        let valid = sim.snapshot();
        let before = serde_json::to_value(&valid).unwrap();
        let pose = *rigid_body(&sim).position();
        let force = rigid_body(&sim).user_force();
        let torque = rigid_body(&sim).user_torque();
        let mass_properties = rigid_body(&sim).mass_properties().local_mprops;
        let mut invalid_states = Vec::new();
        let mut missing = valid.clone();
        missing.assets.pop();
        invalid_states.push(missing);
        let mut duplicate = valid.clone();
        duplicate.assets[1] = duplicate.assets[0].clone();
        invalid_states.push(duplicate);
        for unknown_id in [EntityId::Debris(3), EntityId::Phobos] {
            let mut unknown = valid.clone();
            unknown.assets.last_mut().unwrap().id = unknown_id;
            invalid_states.push(unknown);
        }
        for (index, entity) in valid.assets.iter().enumerate() {
            let mut wrong_kind = valid.clone();
            wrong_kind.assets[index].kind = match entity.kind {
                EntityKind::Lander | EntityKind::ReturnShip => EntityKind::Debris,
                EntityKind::LandingPad | EntityKind::Debris => EntityKind::Lander,
            };
            invalid_states.push(wrong_kind);
            let mut wrong_status = valid.clone();
            wrong_status.assets[index].status = match entity.kind {
                EntityKind::Lander | EntityKind::Debris => EntityStatus::StationKeeping,
                EntityKind::ReturnShip | EntityKind::LandingPad => EntityStatus::Coasting,
            };
            invalid_states.push(wrong_status);
        }
        for invalid in invalid_states {
            assert!(
                sim.execute(PhysicsCommand::Restore {
                    state: Box::new(invalid)
                })
                .is_err()
            );
            assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
            assert_eq!(*rigid_body(&sim).position(), pose);
            assert_eq!(rigid_body(&sim).user_force(), force);
            assert_eq!(rigid_body(&sim).user_torque(), torque);
            assert_eq!(
                rigid_body(&sim).mass_properties().local_mprops,
                mass_properties
            );
        }
    }

    #[test]
    fn complete_seeded_populations_round_trip_in_any_order_for_zero_small_and_legacy_profiles() {
        let mut zero = ExperimentSettings::default();
        zero.debris.count = 0;
        let mut maximum = ExperimentSettings::default();
        // The 100k construction/step test runs once without JSON archive copies.
        maximum.debris.count = 1_000;
        for profile in [Some(zero), Some(maximum), None] {
            let mut sim = Simulation::with_profile(8, profile).unwrap();
            let inertia = sim.ship_inertia_kg_m2();
            let mut expected = serde_json::to_value(sim.snapshot()).unwrap();
            expected["assets"].as_array_mut().unwrap().reverse();
            let mut archive = expected.clone();
            if profile.is_none() {
                archive
                    .as_object_mut()
                    .unwrap()
                    .remove("experiment_settings");
            }
            let state: Snapshot = serde_json::from_value(archive).unwrap();
            let restored = sim
                .execute(PhysicsCommand::Restore {
                    state: Box::new(state),
                })
                .unwrap();
            assert_eq!(restored.experiment_settings, profile);
            assert_eq!(serde_json::to_value(restored).unwrap(), expected);
            assert_eq!(sim.ship_inertia_kg_m2(), inertia);
            assert_eq!(sim.bodies.len(), sim.state.assets.len() + 1);
            assert_eq!(sim.colliders.len(), sim.state.assets.len() + 1);
        }
    }

    #[test]
    fn real_mutual_collision_archives_restore_through_both_strict_command_envelopes() {
        let mut config = ExperimentSettings::default();
        config.debris.count = 2;
        config.debris.radius_min_m = 2.0;
        config.debris.radius_max_m = 2.0;
        let mut sim = Simulation::with_experiment(10, config).unwrap();
        let center = Vector::new(crate::model::phobos_enclosing_radius_m() + 500.0, 0.0, 0.0);
        let a = EntityId::Debris(1);
        let b = EntityId::Debris(2);
        for (id, offset, velocity) in [(a, -2.5, 5.0), (b, 2.5, -5.0)] {
            let body = &mut sim.bodies[sim.handles[&id]];
            body.set_translation(center + Vector::X * offset, true);
            body.set_angvel(Vector::ZERO, true);
            body.apply_impulse((Vector::X * velocity - body.linvel()) * body.mass(), true);
        }
        let pair = pair_key(a, b);
        for _ in 0..8 {
            sim.advance(settings().integration.fixed_step_s);
            if sim.state.active_contacts.contains(&pair) {
                break;
            }
        }
        assert!(sim.state.active_contacts.contains(&pair));
        assert!(sim.state.collisions > 0);
        let before = serde_json::to_value(sim.snapshot()).unwrap();
        let request = json!({"op": "restore", "state": before});
        let command: PhysicsCommand = serde_json::from_value(request.clone()).unwrap();
        let restored = sim.execute(command).unwrap();
        assert_eq!(serde_json::to_value(restored).unwrap(), before);
        let parsed: crate::api::ApiRequest = serde_json::from_value(request).unwrap();
        let crate::api::Dispatch::Physics(command) = parsed.into_dispatch().unwrap() else {
            panic!("restore dispatched outside physics");
        };
        let restored = sim.execute(command).unwrap();
        assert_eq!(serde_json::to_value(restored).unwrap(), before);
        assert!(sim.state.active_contacts.contains(&pair));
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
    fn holding_positive_body_y_thrust_naturally_tips_over_on_the_real_default_pad() {
        let mut sim = Simulation::new_default();
        let up = rotation(lander(&sim)) * Vector::Z;
        let dt = settings().integration.fixed_step_s;
        let mut minimum_up_alignment = 1.0_f64;
        let mut peak_roll_rate = 0.0_f64;
        let mut touched_pad = false;
        sim.set_manual_thrust([0.0, settings().lander.max_accel_mps2, 0.0])
            .unwrap();

        // No pose, angular velocity, gravity, inertia, or contact changes. This is
        // a sustained positive body-Y force, not the A binding, which requests -Y.
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
    fn huge_finite_commands_preserve_direction_and_apply_bounded_nonzero_real_forces() {
        let cfg = settings().lander;
        assert_eq!(cfg.max_accel_mps2, 0.5);
        assert_eq!(cfg.max_angular_accel_rad_s2, 0.05);
        let mut sim = Simulation::new_default();
        for input in [
            [f64::MAX, f64::MAX, 0.0],
            [-f64::MAX, f64::MAX, -f64::MAX],
            [f64::MAX, f64::MAX / 2.0, -f64::MAX / 4.0],
            [1e300, -2e300, 3e300],
            [f64::MAX, 0.0, 0.0],
            [1.0, 1.0, 1.0],
            [0.9, 0.2, -0.5],
        ] {
            let before = lander(&sim).clone();
            let pose = *rigid_body(&sim).position();
            let q = *rigid_body(&sim).rotation();
            let gravity = crate::astrophysics::acceleration(
                rigid_body(&sim).translation(),
                rigid_body(&sim).linvel(),
                settings().gravity,
            );
            let largest = input.into_iter().map(f64::abs).fold(0.0, f64::max);
            let direction =
                Vector::from_array(input.map(|component| component / largest)).normalize();
            let angular_input = [input[2], -input[0], input[1]];
            let angular_direction = Vector::new(direction.z, -direction.x, direction.y);
            sim.execute(PhysicsCommand::SetCommand {
                accel: input,
                angular_accel: angular_input,
            })
            .unwrap();
            let commanded = lander(&sim).clone();
            assert!(norm(commanded.commanded_accel_mps2) > 0.0);
            assert!(norm(commanded.commanded_accel_mps2) <= cfg.max_accel_mps2);
            assert!(norm(commanded.commanded_angular_accel_rad_s2) > 0.0);
            assert!(norm(commanded.commanded_angular_accel_rad_s2) <= cfg.max_angular_accel_rad_s2);
            assert_vector_close(
                Vector::from_array(commanded.commanded_accel_mps2),
                direction * cfg.max_accel_mps2,
                1e-15,
            );
            assert_vector_close(
                Vector::from_array(commanded.commanded_angular_accel_rad_s2),
                angular_direction * cfg.max_angular_accel_rad_s2,
                1e-15,
            );
            assert_eq!(commanded.position, before.position);
            assert_eq!(commanded.rotation, before.rotation);
            assert_eq!(commanded.velocity, before.velocity);
            assert_eq!(
                commanded.angular_velocity_rad_s,
                before.angular_velocity_rad_s
            );
            assert_eq!(*rigid_body(&sim).position(), pose);
            assert_eq!(
                commanded.thrust_n,
                scale(commanded.commanded_accel_mps2, commanded.mass_kg)
            );
            let angular = commanded.commanded_angular_accel_rad_s2;
            assert_eq!(
                commanded.torque_nm,
                cfg.inertia_kg_m2.map(|row| dot(row, angular))
            );

            sim.advance(settings().integration.fixed_step_s);
            let body = rigid_body(&sim);
            assert_vector_close(
                body.user_force(),
                q * Vector::from_array(commanded.thrust_n) + gravity * body.mass(),
                1e-7,
            );
            assert_vector_close(
                body.user_torque(),
                q * Vector::from_array(commanded.torque_nm),
                1e-8,
            );
            assert!(body.user_force().length() > 0.0);
            assert!(body.user_torque().length() > 0.0);
            assert_ne!(*body.position(), pose);
        }
    }

    #[test]
    fn zero_subnormal_and_ordinary_commands_below_the_limits_remain_unchanged() {
        let tiny = f64::from_bits(1);
        let mut sim = Simulation::new_default();
        for (accel, angular) in [
            ([0.0; 3], [0.0; 3]),
            ([tiny, -tiny, 0.0], [0.0, tiny, -tiny]),
            ([f64::MIN_POSITIVE; 3], [-f64::MIN_POSITIVE; 3]),
            ([1e-100, -2e-100, 3e-100], [-3e-100, 2e-100, -1e-100]),
            ([0.1, -0.2, 0.3], [0.01, -0.02, 0.03]),
        ] {
            sim.set_command(accel, angular).unwrap();
            assert_eq!(lander(&sim).commanded_accel_mps2, accel);
            assert_eq!(lander(&sim).commanded_angular_accel_rad_s2, angular);
        }
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
