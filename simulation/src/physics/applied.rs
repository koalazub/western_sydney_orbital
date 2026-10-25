use super::*;
use crate::model::{ActuationRequest, ActuationState, AppliedPhysics, ExternalForceProvenance};

impl Simulation {
    /// Read actual body properties and accumulators without stepping or sampling.
    pub fn applied_physics(&self) -> Result<AppliedPhysics, String> {
        let lander = self
            .state
            .assets
            .iter()
            .find(|entity| entity.id == EntityId::Lander && entity.kind == EntityKind::Lander)
            .ok_or("Applied physics unavailable: controlled lander missing. Reset the run.")?;
        let body = self
            .handles
            .get(&EntityId::Lander)
            .and_then(|handle| self.bodies.get(*handle))
            .ok_or("Applied physics unavailable: lander body missing. Reset the run.")?;
        let requested = ActuationRequest::from(lander);
        let actuation_state = match self.last_actuator_application {
            None if requested.is_zero() => ActuationState::NotApplied,
            None => ActuationState::AwaitingApplication,
            Some(last)
                if last.requested != requested || last.actuator_gain != lander.actuator_gain =>
            {
                ActuationState::AwaitingApplication
            }
            Some(last)
                if last
                    .force_world_n
                    .into_iter()
                    .chain(last.torque_world_nm)
                    .any(|component| component != 0.0) =>
            {
                ActuationState::Actuating
            }
            Some(_) => ActuationState::Coasting,
        };
        let rotation = body.rotation();
        Ok(AppliedPhysics {
            source_time_s: self.state.time_s,
            tuning_revision: self.state.tuning_revision,
            frame: self.state.frame.clone(),
            mass_kg: body.mass(),
            inertia_body_kg_m2: self.ship_inertia_kg_m2(),
            position_world_m: body.translation().to_array(),
            rotation_body_to_world_xyzw: [rotation.x, rotation.y, rotation.z, rotation.w],
            velocity_world_mps: body.linvel().to_array(),
            angular_velocity_world_rad_s: body.angvel().to_array(),
            total_external_force_world_n: body.user_force().to_array(),
            total_external_torque_world_nm: body.user_torque().to_array(),
            external_force_provenance:
                ExternalForceProvenance::RapierUserForcesExcludingSolverImpulses,
            requested,
            actuator_gain: lander.actuator_gain,
            actuation_state,
            last_actuator_application: self.last_actuator_application,
        })
    }
}
