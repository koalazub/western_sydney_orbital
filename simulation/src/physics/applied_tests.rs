use super::*;
use crate::model::{ActuationRequest, ActuationState, AppliedPhysics, ExternalForceProvenance};

fn free_flight() -> Simulation {
    let mut config = ExperimentSettings::default();
    config.debris.count = 0;
    let mut sim = Simulation::with_experiment(100, config).unwrap();
    let mut state = sim.snapshot();
    let lander = state
        .assets
        .iter_mut()
        .find(|entity| entity.id == EntityId::Lander)
        .unwrap();
    lander.position = [
        crate::model::phobos_enclosing_radius_m() + 1_500.0,
        0.0,
        0.0,
    ];
    lander.rotation = [0.0, 0.0, 0.0, 1.0];
    sim.execute(PhysicsCommand::Restore {
        state: Box::new(state),
    })
    .unwrap();
    sim
}

fn lander(sim: &Simulation) -> &RigidBody {
    &sim.bodies[sim.handles[&EntityId::Lander]]
}

fn assert_close(actual: [f64; 3], expected: Vector, tolerance: f64) {
    assert!(
        (Vector::from_array(actual) - expected).length() <= tolerance,
        "actual {actual:?}, expected {expected:?}"
    );
}

fn assert_actual_body_readback(sim: &Simulation, report: &AppliedPhysics) {
    let body = lander(sim);
    assert_eq!(report.mass_kg, body.mass());
    assert_eq!(report.inertia_body_kg_m2, sim.ship_inertia_kg_m2());
    assert_eq!(report.position_world_m, body.translation().to_array());
    assert_eq!(report.velocity_world_mps, body.linvel().to_array());
    assert_eq!(
        report.angular_velocity_world_rad_s,
        body.angvel().to_array()
    );
    assert_eq!(
        report.total_external_force_world_n,
        body.user_force().to_array()
    );
    assert_eq!(
        report.total_external_torque_world_nm,
        body.user_torque().to_array()
    );
    assert_eq!(
        report.external_force_provenance,
        ExternalForceProvenance::RapierUserForcesExcludingSolverImpulses
    );
    assert_eq!(report.source_time_s, sim.state.time_s);
    assert_eq!(report.tuning_revision, sim.state.tuning_revision);
    assert_eq!(report.frame, sim.state.frame);
}

#[test]
fn startup_pending_commands_and_zero_request_tuning_do_not_invent_thrust_or_motion() {
    let mut sim = free_flight();
    let startup = sim.applied_physics().unwrap();
    assert_actual_body_readback(&sim, &startup);
    assert_eq!(startup.actuation_state, ActuationState::NotApplied);
    assert_eq!(startup.last_actuator_application, None);
    assert_eq!(startup.total_external_force_world_n, [0.0; 3]);
    assert!(sim.observation()["shuttle"]["applied_accel_mps2"].is_null());
    let before = sim.snapshot();
    let pose = *lander(&sim).position();
    let velocity = lander(&sim).linvel();
    sim.tune_ship(ShipTuning {
        mass_kg: 100.0,
        inertia_scale: 1.0,
        thrust_scale: 3.2,
    })
    .unwrap();
    let coast = sim.applied_physics().unwrap();
    assert_actual_body_readback(&sim, &coast);
    assert!((coast.mass_kg - 100.0).abs() < 1e-10);
    assert_eq!(coast.actuator_gain, 3.2);
    assert!(coast.requested.is_zero());
    assert_eq!(coast.actuation_state, ActuationState::Coasting);
    let application = coast.last_actuator_application.unwrap();
    assert_eq!(application.force_world_n, [0.0; 3]);
    assert_eq!(application.torque_world_nm, [0.0; 3]);
    assert_eq!(application.propellant_force_n, 0.0);
    assert_eq!(*lander(&sim).position(), pose);
    assert_eq!(lander(&sim).linvel(), velocity);
    assert_eq!(sim.state.time_s, before.time_s);
    assert_eq!(sim.state.rng_state, before.rng_state);
    assert_eq!(sim.state.mission.fuel_used_kg, before.mission.fuel_used_kg);
    sim.set_command([0.0, 0.0, 2.0], [0.0; 3]).unwrap();
    let pending = sim.applied_physics().unwrap();
    assert_eq!(pending.requested.accel_body_mps2, [0.0, 0.0, 0.5]);
    assert_eq!(pending.actuation_state, ActuationState::AwaitingApplication);
    assert_eq!(
        pending.last_actuator_application,
        coast.last_actuator_application
    );
    assert_eq!(
        pending.total_external_force_world_n,
        coast.total_external_force_world_n
    );
    assert_eq!(*lander(&sim).position(), pose);
    let dt = settings().integration.fixed_step_s;
    sim.advance(dt);
    let active = sim.applied_physics().unwrap();
    assert_eq!(active.actuation_state, ActuationState::Actuating);
    assert_close(
        active.last_actuator_application.unwrap().force_body_n,
        Vector::Z * 160.0,
        1e-9,
    );
    assert_eq!(
        active.last_actuator_application.unwrap().application_time_s,
        before.time_s
    );
    assert_eq!(active.source_time_s, before.time_s + dt);
    assert_actual_body_readback(&sim, &active);
}

#[test]
fn each_force_mode_records_actual_in_place_application_and_retains_reference_requests() {
    for mode in [
        ThrustApplication::CenterOfMass,
        ThrustApplication::ManualNozzles,
        ThrustApplication::PracticeBoost,
    ] {
        let mut sim = free_flight();
        match mode {
            ThrustApplication::CenterOfMass => sim
                .set_command([0.3, 0.4, 0.0], [0.01, -0.02, 0.03])
                .unwrap(),
            ThrustApplication::ManualNozzles => sim.set_manual_thrust([0.3, 0.4, 0.0]).unwrap(),
            ThrustApplication::PracticeBoost => sim.set_space_thrust([0.2, 0.0, 1.5]).unwrap(),
        }
        sim.advance(settings().integration.fixed_step_s * 0.01);
        let handle = sim.handles[&EntityId::Lander];
        let collider = lander(&sim).colliders()[0];
        let request = sim.applied_physics().unwrap().requested;
        let pose = *lander(&sim).position();
        let velocity = lander(&sim).linvel();
        let spin = lander(&sim).angvel();
        let time = sim.state.time_s;
        let fuel = sim.state.mission.fuel_used_kg;
        let tuning = ShipTuning {
            mass_kg: 100.0,
            inertia_scale: 2.5,
            thrust_scale: 3.2,
        };
        sim.tune_ship(tuning).unwrap();
        let actual = sim.applied_physics().unwrap();
        assert_actual_body_readback(&sim, &actual);
        assert_eq!(sim.handles[&EntityId::Lander], handle);
        assert_eq!(lander(&sim).colliders()[0], collider);
        assert_eq!(*lander(&sim).position(), pose);
        assert_eq!(lander(&sim).linvel(), velocity);
        assert_eq!(lander(&sim).angvel(), spin);
        assert_eq!(sim.state.time_s, time);
        assert_eq!(sim.state.mission.fuel_used_kg, fuel);
        assert_eq!(actual.requested, request);
        assert_eq!(actual.actuation_state, ActuationState::Actuating);
        let application = actual.last_actuator_application.unwrap();
        assert_eq!(application.requested.application, mode);
        assert_eq!(application.application_time_s, time);
        assert_eq!(application.tuning_revision, actual.tuning_revision);
        assert_eq!(application.actuator_gain, tuning.thrust_scale);
        assert_close(
            application.force_body_n,
            Vector::from_array(request.accel_body_mps2) * actual.mass_kg * tuning.thrust_scale,
            1e-8,
        );
        assert_eq!(
            application.torque_world_nm,
            lander(&sim).user_torque().to_array()
        );
        let gravity =
            crate::astrophysics::acceleration(pose.translation, velocity, settings().gravity);
        assert_close(
            actual.total_external_force_world_n,
            Vector::from_array(application.force_world_n) + gravity * actual.mass_kg,
            1e-8,
        );
        let previous_revision = actual.tuning_revision;
        let previous_application = actual.last_actuator_application;
        sim.tune_debris_flow(DebrisFlowTuning {
            speed_mps: 30.0,
            ..sim.debris_flow()
        })
        .unwrap();
        let flow_changed = sim.applied_physics().unwrap();
        assert_eq!(flow_changed.tuning_revision, previous_revision + 1);
        assert_eq!(flow_changed.last_actuator_application, previous_application);
        assert_eq!(flow_changed.actuation_state, ActuationState::Actuating);
        // A later request still uses the calibrated hardware on the next real step.
        sim.set_command([2.0, 0.0, 0.0], [0.0, 2.0, 0.0]).unwrap();
        sim.advance(settings().integration.fixed_step_s * 0.01);
        let next = sim.applied_physics().unwrap();
        assert_eq!(next.requested.accel_body_mps2, [0.5, 0.0, 0.0]);
        assert_eq!(next.requested.angular_accel_body_rad_s2, [0.0, 0.05, 0.0]);
        assert_close(
            next.last_actuator_application.unwrap().force_body_n,
            Vector::X * 160.0,
            1e-8,
        );
        validate(&sim.snapshot()).unwrap();
    }
}

#[test]
fn tuned_mass_and_inertia_change_real_rapier_impulse_response_not_only_displayed_numbers() {
    let mut readbacks = Vec::new();
    for mass_kg in [100.0, 100_000.0] {
        let mut sim = free_flight();
        let handle = sim.handles[&EntityId::Lander];
        let collider = lander(&sim).colliders()[0];
        sim.tune_ship(ShipTuning {
            mass_kg,
            inertia_scale: 1.0,
            thrust_scale: 1.0,
        })
        .unwrap();
        assert_eq!(sim.handles[&EntityId::Lander], handle);
        assert_eq!(lander(&sim).colliders()[0], collider);
        // Exactly the same real impulses in N s and N m s, not mass-scaled inputs.
        sim.bodies[handle].apply_impulse(Vector::Y * 100.0, true);
        sim.bodies[handle].apply_torque_impulse(Vector::Z, true);
        let actual = sim.applied_physics().unwrap();
        assert_actual_body_readback(&sim, &actual);
        assert_eq!(actual.actuation_state, ActuationState::Coasting);
        assert_eq!(
            actual.last_actuator_application.unwrap().force_world_n,
            [0.0; 3]
        );
        assert_eq!(
            actual.last_actuator_application.unwrap().torque_world_nm,
            [0.0; 3]
        );
        assert_close(
            actual.velocity_world_mps,
            Vector::Y * (100.0 / mass_kg),
            1e-12,
        );
        readbacks.push(actual);
    }
    assert_close(
        readbacks[0].angular_velocity_world_rad_s,
        Vector::from_array(readbacks[1].angular_velocity_world_rad_s) * 1_000.0,
        1e-12,
    );
    assert!(Vector::from_array(readbacks[0].angular_velocity_world_rad_s).length() > 0.0);
}

#[test]
fn readback_is_read_only_and_uses_actual_body_motion_not_entity_cache_or_later_gravity() {
    let mut sim = free_flight();
    sim.set_command([0.0, 0.0, 0.5], [0.0; 3]).unwrap();
    sim.advance(settings().integration.fixed_step_s);
    let recorded = sim.applied_physics().unwrap().last_actuator_application;
    let handle = sim.handles[&EntityId::Lander];
    let mass = sim.bodies[handle].mass();
    sim.bodies[handle].apply_impulse(Vector::Y * mass * 2.0, true);
    let position = sim.bodies[handle].translation();
    sim.bodies[handle].set_translation(position + Vector::X * 10.0, true);
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    let rng = sim.rng;
    let actual = sim.applied_physics().unwrap();
    assert_actual_body_readback(&sim, &actual);
    assert_eq!(actual.last_actuator_application, recorded);
    let cached = sim
        .state
        .assets
        .iter()
        .find(|entity| entity.id == EntityId::Lander)
        .unwrap();
    assert_ne!(actual.position_world_m, cached.position);
    assert_ne!(actual.velocity_world_mps, cached.velocity);
    let public = sim.observation();
    assert_eq!(
        public["applied_physics"],
        serde_json::to_value(&actual).unwrap()
    );
    assert_eq!(
        serde_json::from_value::<AppliedPhysics>(public["applied_physics"].clone()).unwrap(),
        actual
    );
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
    assert_eq!(sim.rng, rng);
    assert_eq!(sim.applied_physics().unwrap(), actual);
}

#[test]
fn pending_stop_invalid_tunes_and_restore_have_honest_application_provenance() {
    let mut sim = free_flight();
    sim.set_manual_thrust([0.3, 0.4, 0.0]).unwrap();
    sim.advance(settings().integration.fixed_step_s);
    let active = sim.applied_physics().unwrap();
    let valid = sim.snapshot();
    sim.execute(PhysicsCommand::Stop).unwrap();
    let stopped_request = sim.applied_physics().unwrap();
    assert_eq!(
        stopped_request.actuation_state,
        ActuationState::AwaitingApplication
    );
    assert!(stopped_request.requested.is_zero());
    assert_eq!(
        stopped_request.last_actuator_application,
        active.last_actuator_application
    );
    assert_eq!(
        stopped_request.total_external_force_world_n,
        active.total_external_force_world_n
    );
    let tuning = ShipTuning {
        thrust_scale: 3.2,
        ..sim.ship_tuning()
    };
    sim.tune_ship(tuning).unwrap();
    let coast = sim.applied_physics().unwrap();
    assert_eq!(coast.actuation_state, ActuationState::Coasting);
    assert_eq!(
        coast.last_actuator_application.unwrap().force_world_n,
        [0.0; 3]
    );
    sim.tune_ship(tuning).unwrap();
    assert_eq!(sim.applied_physics().unwrap(), coast);
    assert!(
        sim.tune_ship(ShipTuning {
            thrust_scale: f64::NAN,
            ..tuning
        })
        .is_err()
    );
    assert_eq!(sim.applied_physics().unwrap(), coast);
    sim.state.tuning_revision = u32::MAX;
    let exhausted = sim.applied_physics().unwrap();
    assert!(
        sim.tune_ship(ShipTuning {
            thrust_scale: 2.0,
            ..tuning
        })
        .is_err()
    );
    assert_eq!(sim.applied_physics().unwrap(), exhausted);
    sim.execute(PhysicsCommand::Restore {
        state: Box::new(valid),
    })
    .unwrap();
    let restored = sim.applied_physics().unwrap();
    assert_actual_body_readback(&sim, &restored);
    assert_eq!(restored.last_actuator_application, None);
    assert_eq!(
        restored.actuation_state,
        ActuationState::AwaitingApplication
    );
    assert_eq!(restored.total_external_force_world_n, [0.0; 3]);
    let restored_time = sim.state.time_s;
    sim.advance(settings().integration.fixed_step_s);
    assert_eq!(
        sim.applied_physics()
            .unwrap()
            .last_actuator_application
            .unwrap()
            .application_time_s,
        restored_time
    );
    sim.debug_upright().unwrap();
    let cleared = sim.applied_physics().unwrap();
    assert_eq!(cleared.last_actuator_application, None);
    assert_eq!(cleared.actuation_state, ActuationState::NotApplied);
    assert_eq!(cleared.total_external_force_world_n, [0.0; 3]);
    assert_eq!(cleared.total_external_torque_world_nm, [0.0; 3]);
    assert_eq!(
        cleared.requested,
        ActuationRequest::from(
            sim.state
                .assets
                .iter()
                .find(|entity| entity.id == EntityId::Lander)
                .unwrap()
        )
    );
}

#[test]
fn real_lander_pad_impulses_change_motion_but_are_not_misreported_as_external_actuator_force() {
    let mut config = ExperimentSettings::default();
    config.debris.count = 0;
    let mut sim = Simulation::with_experiment(100, config).unwrap();
    let handle = sim.handles[&EntityId::Lander];
    let up = *sim.bodies[handle].rotation() * Vector::Z;
    let mass = sim.bodies[handle].mass();
    sim.bodies[handle].apply_impulse(-up * mass, true);
    let dt = settings().integration.fixed_step_s;
    let mut touched = false;
    for _ in 0..20 {
        let before_velocity = lander(&sim).linvel();
        sim.advance(dt);
        let actual = sim.applied_physics().unwrap();
        assert_actual_body_readback(&sim, &actual);
        let impulse = sim.narrow.contact_pairs().any(|pair| {
            let first = sim.collider_ids[&pair.collider1];
            let second = sim.collider_ids[&pair.collider2];
            pair_key(first, second) == pair_key(EntityId::Lander, EntityId::LandingPad)
                && pair.has_any_active_contact()
                && pair.total_impulse_magnitude() > 0.0
        });
        if impulse {
            let user_force_velocity_change =
                Vector::from_array(actual.total_external_force_world_n) * (dt / actual.mass_kg);
            let actual_velocity_change = lander(&sim).linvel() - before_velocity;
            assert!((actual_velocity_change - user_force_velocity_change).length() > 0.1);
            assert_eq!(actual.actuation_state, ActuationState::Coasting);
            assert_eq!(
                actual.last_actuator_application.unwrap().force_world_n,
                [0.0; 3]
            );
            assert_eq!(
                actual.last_actuator_application.unwrap().torque_world_nm,
                [0.0; 3]
            );
            touched = true;
            break;
        }
    }
    assert!(
        touched,
        "only a genuine Rapier pad contact can satisfy this test"
    );
}

#[test]
fn missing_readback_sources_are_errors_without_snapshot_or_body_mutation() {
    let mut sim = free_flight();
    let handle = sim.handles.remove(&EntityId::Lander).unwrap();
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    let pose = *sim.bodies[handle].position();
    let force = sim.bodies[handle].user_force();
    assert_eq!(
        sim.applied_physics().unwrap_err(),
        "Applied physics unavailable: lander body missing. Reset the run."
    );
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
    assert_eq!(*sim.bodies[handle].position(), pose);
    assert_eq!(sim.bodies[handle].user_force(), force);
    sim.handles.insert(EntityId::Lander, handle);
    sim.state
        .assets
        .retain(|entity| entity.id != EntityId::Lander);
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    assert_eq!(
        sim.applied_physics().unwrap_err(),
        "Applied physics unavailable: controlled lander missing. Reset the run."
    );
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
    assert_eq!(*sim.bodies[handle].position(), pose);
    assert_eq!(sim.bodies[handle].user_force(), force);
}
