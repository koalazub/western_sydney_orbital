use super::*;

impl Simulation {
    /// Change hardware on the same body and collider, without stepping the world.
    pub fn tune_ship(&mut self, settings: ShipTuning) -> Result<(), String> {
        settings.validate()?;
        let index = self
            .state
            .assets
            .iter()
            .position(|entity| entity.id == EntityId::Lander && entity.kind == EntityKind::Lander)
            .ok_or("Ship tuning unavailable: controlled lander missing. Reset the run.")?;
        let handle = *self
            .handles
            .get(&EntityId::Lander)
            .ok_or("Ship tuning unavailable: lander body missing. Reset the run.")?;
        let body = self
            .bodies
            .get(handle)
            .filter(|body| body.is_dynamic())
            .ok_or("Ship tuning unavailable: lander body missing. Reset the run.")?;
        let [collider_handle] = body.colliders() else {
            return Err("Ship tuning unavailable: lander collider missing. Reset the run.".into());
        };
        let collider_handle = *collider_handle;
        self.colliders
            .get(collider_handle)
            .filter(|collider| {
                collider.parent() == Some(handle)
                    && self.collider_ids.get(&collider_handle) == Some(&EntityId::Lander)
            })
            .ok_or("Ship tuning unavailable: lander collider missing. Reset the run.")?;
        if settings == self.ship_tuning() {
            return Ok(());
        }
        let revision = self.next_tuning_revision()?;
        let inertia = settings.inertia_kg_m2();
        let mass_properties = MassProperties::with_inertia_matrix(
            Vector::ZERO,
            settings.mass_kg,
            inertia_matrix(inertia),
        );
        let mut lander = self.state.assets[index].clone();
        lander.mass_kg = settings.mass_kg;
        lander.actuator_gain = settings.thrust_scale;
        lander.max_thrust_n = local_max_thrust_for_mass_n(settings.mass_kg) * settings.thrust_scale;
        reconcile_ship_wrench(&mut lander, inertia);

        // All fallible checks finish before touching Rapier or the snapshot.
        // Changing collider mass does not replace its shape or clear manifolds,
        // contact impulses, velocities, pose, solver state or world handles.
        self.colliders[collider_handle].set_mass_properties(mass_properties);
        let body = &mut self.bodies[handle];
        body.recompute_mass_properties_from_colliders(&self.colliders);
        body.reset_forces(false);
        body.reset_torques(false);
        let application = apply_actuator_forces(body, &lander, self.state.time_s, revision);
        apply_gravity(body);
        self.last_actuator_application = Some(application);
        self.state.assets[index] = lander;
        self.state.ship_tuning_override = Some(settings);
        self.state.tuning_revision = revision;
        event(
            &mut self.state,
            EntityId::Lander,
            format!("Local live ship tuning, revision {revision}"),
        );
        Ok(())
    }

    /// Add only the common-stream delta to actual bodies. No birth or RNG draw.
    pub fn tune_debris_flow(&mut self, settings: DebrisFlowTuning) -> Result<(), String> {
        if self.state.experiment_settings.is_some_and(|experiment| {
            matches!(
                experiment.debris.distribution,
                DebrisDistribution::ImpactEjecta { .. } | DebrisDistribution::OfficialCloud
            )
        }) {
            return Err("Live stream tuning is unavailable for impact ejecta or the official cloud. Configure a new flight to change these settings.".into());
        }
        settings.validate()?;
        let delta = Vector::from_array(sub(
            settings.common_velocity_mps(),
            self.debris_flow().common_velocity_mps(),
        ));
        let debris_ids: HashSet<_> = self
            .state
            .assets
            .iter()
            .filter(|entity| entity.kind == EntityKind::Debris)
            .map(|entity| entity.id)
            .collect();
        for id in self.handles.keys() {
            if matches!(id, EntityId::Debris(_)) && !debris_ids.contains(id) {
                return Err(
                    "Debris flow tuning unavailable: debris entity missing. Reset the run.".into(),
                );
            }
        }
        let mut changes = Vec::new();
        for (index, entity) in self.state.assets.iter().enumerate() {
            if entity.kind != EntityKind::Debris {
                continue;
            }
            let handle = *self
                .handles
                .get(&entity.id)
                .ok_or("Debris flow tuning unavailable: debris body missing. Reset the run.")?;
            let body = self
                .bodies
                .get(handle)
                .filter(|body| body.is_dynamic())
                .ok_or("Debris flow tuning unavailable: debris body missing. Reset the run.")?;
            let velocity = body.linvel() + delta;
            if !velocity.is_finite()
                || velocity
                    .to_array()
                    .iter()
                    .any(|value| value.abs() > crate::scenario::settings().limits.velocity_mps)
            {
                return Err("Debris flow tuning would exceed the world velocity limit".into());
            }
            changes.push((index, handle, velocity));
        }
        if settings == self.debris_flow() {
            return Ok(());
        }
        let revision = self.next_tuning_revision()?;
        for (index, handle, velocity) in changes {
            if delta != Vector::ZERO {
                self.bodies[handle].set_linvel(velocity, true);
            }
            self.state.assets[index].velocity = velocity.to_array();
        }
        self.state.debris_flow_override = Some(settings);
        self.state.tuning_revision = revision;
        event(
            &mut self.state,
            EntityId::Phobos,
            format!(
                "Local live debris flow tuning, revision {revision}; spread applies to future births only"
            ),
        );
        Ok(())
    }

    fn next_tuning_revision(&self) -> Result<u32, String> {
        self.state.tuning_revision.checked_add(1).ok_or_else(|| {
            "Local tuning revision exhausted. Configure a new experiment before tuning again."
                .into()
        })
    }
}

pub(super) fn inertia_matrix(inertia: [[f64; 3]; 3]) -> Matrix {
    Matrix::from_cols(
        Vector::new(inertia[0][0], inertia[1][0], inertia[2][0]),
        Vector::new(inertia[0][1], inertia[1][1], inertia[2][1]),
        Vector::new(inertia[0][2], inertia[1][2], inertia[2][2]),
    )
}

pub(super) fn reconcile_ship_wrench(lander: &mut EntityState, inertia: [[f64; 3]; 3]) {
    lander.thrust_n = scale(
        lander.commanded_accel_mps2,
        lander.mass_kg * lander.actuator_gain,
    );
    lander.torque_nm = match lander.thrust_application {
        ThrustApplication::CenterOfMass => {
            let angular = lander.commanded_angular_accel_rad_s2;
            inertia.map(|row| dot(row, angular) * lander.actuator_gain)
        }
        ThrustApplication::ManualNozzles | ThrustApplication::PracticeBoost => {
            applied_wrench(&applied_jets(lander)).torque_body_nm
        }
    };
}

/// Record the contribution of this exact application, without estimating gravity
/// from a later state or treating solver contact impulses as external user force.
pub(super) fn apply_actuator_forces(
    body: &mut RigidBody,
    lander: &EntityState,
    application_time_s: f64,
    tuning_revision: u32,
) -> AppliedActuatorWrench {
    let rotation = *body.rotation();
    let before_force = body.user_force();
    let before_torque = body.user_torque();
    let propellant_force_n = match lander.thrust_application {
        ThrustApplication::CenterOfMass => {
            // Keep the gain-one integration and fuel arithmetic of archived
            // center commands, using the real body's mass rather than a cap.
            let accel = Vector::from_array(lander.commanded_accel_mps2);
            body.add_force(rotation * accel * body.mass() * lander.actuator_gain, true);
            body.add_torque(rotation * Vector::from_array(lander.torque_nm), true);
            accel.length() * body.mass() * lander.actuator_gain
        }
        ThrustApplication::ManualNozzles | ThrustApplication::PracticeBoost => {
            let jets = applied_jets(lander);
            for jet in &jets {
                let force = rotation * Vector::from_array(jet.force_body_n);
                let point = body
                    .position()
                    .transform_point(Vector::from_array(jet.point_body_m));
                // This supplies r × F exactly once, with no direct angular jet.
                body.add_force_at_point(force, point, true);
            }
            applied_wrench(&jets).propellant_force_n
        }
    };
    let force_world = body.user_force() - before_force;
    let torque_world = body.user_torque() - before_torque;
    AppliedActuatorWrench {
        application_time_s,
        tuning_revision,
        requested: crate::model::ActuationRequest::from(lander),
        actuator_gain: lander.actuator_gain,
        rotation_body_to_world_xyzw: [rotation.x, rotation.y, rotation.z, rotation.w],
        force_body_n: (rotation.inverse() * force_world).to_array(),
        torque_body_nm: (rotation.inverse() * torque_world).to_array(),
        force_world_n: force_world.to_array(),
        torque_world_nm: torque_world.to_array(),
        propellant_force_n,
    }
}

fn apply_gravity(body: &mut RigidBody) {
    let gravity =
        crate::astrophysics::acceleration(body.translation(), body.linvel(), settings().gravity);
    body.add_force(gravity * body.mass(), true);
}
