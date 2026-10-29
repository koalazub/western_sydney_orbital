use super::*;
use crate::model::StreamAxis;

fn experiment(count: usize) -> ExperimentSettings {
    let mut experiment = ExperimentSettings::default();
    experiment.debris.distribution = DebrisDistribution::DirectedStream;
    experiment.debris.count = count;
    experiment.debris.radius_min_m = 2.0;
    experiment.debris.radius_max_m = 2.0;
    experiment
}

fn ship(sim: &Simulation) -> &EntityState {
    sim.state
        .assets
        .iter()
        .find(|entity| entity.id == EntityId::Lander)
        .unwrap()
}

fn actor(sim: &Simulation, id: EntityId) -> &RigidBody {
    &sim.bodies[sim.handles[&id]]
}

fn snapshot_json(sim: &Simulation) -> Value {
    serde_json::to_value(sim.snapshot()).unwrap()
}

fn assert_close(actual: Vector, expected: Vector, tolerance: f64) {
    assert!(
        (actual - expected).length() <= tolerance,
        "actual {actual:?}, expected {expected:?}"
    );
}

fn assert_inertia(sim: &Simulation, tuning: ShipTuning) {
    for (actual, expected) in sim
        .ship_inertia_kg_m2()
        .into_iter()
        .flatten()
        .zip(tuning.inertia_kg_m2().into_iter().flatten())
    {
        assert!((actual - expected).abs() <= 1e-8 + expected.abs() * 1e-10);
    }
}

fn kinematics(sim: &Simulation) -> Vec<(EntityId, RigidBodyHandle, Pose, Vector, Vector)> {
    let mut result: Vec<_> = sim
        .handles
        .iter()
        .map(|(&id, &handle)| {
            let body = &sim.bodies[handle];
            (id, handle, *body.position(), body.linvel(), body.angvel())
        })
        .collect();
    result.sort_by_key(|entry| entry.0);
    result
}

fn contacts(sim: &Simulation) -> Vec<(ColliderHandle, ColliderHandle, String, String)> {
    sim.narrow
        .contact_pairs()
        .map(|pair| {
            (
                pair.collider1,
                pair.collider2,
                format!("{:?}", pair.manifolds()),
                format!("{:?}", pair.solver_manifolds()),
            )
        })
        .collect()
}

fn free_flight() -> Simulation {
    let sim = Simulation::with_experiment(42, experiment(0)).unwrap();
    let mut state = sim.snapshot();
    let ship = state
        .assets
        .iter_mut()
        .find(|entity| entity.id == EntityId::Lander)
        .unwrap();
    ship.position = [
        crate::model::phobos_enclosing_radius_m() + 1_500.0,
        0.0,
        0.0,
    ];
    ship.rotation = [0.0, 0.0, 0.0, 1.0];
    Simulation::from_state(state)
}

#[test]
fn ship_tuning_preserves_the_real_world_and_warmstart_and_replaces_forces_immediately() {
    let mut sim = Simulation::with_experiment(10, experiment(4)).unwrap();
    let handle = sim.handles[&EntityId::Lander];
    let up = *sim.bodies[handle].rotation() * Vector::Z;
    let mass = sim.bodies[handle].mass();
    // Close the launch clearance with an actual impulse, not injected contacts.
    sim.bodies[handle].apply_impulse(-up * mass, true);
    for _ in 0..20 {
        sim.advance(settings().integration.fixed_step_s);
        if sim
            .state
            .active_contacts
            .contains(&pair_key(EntityId::Lander, EntityId::LandingPad))
        {
            break;
        }
    }
    assert!(
        sim.state
            .active_contacts
            .contains(&pair_key(EntityId::Lander, EntityId::LandingPad))
    );
    assert!(
        sim.narrow
            .contact_pairs()
            .any(|pair| pair.total_impulse_magnitude() > 0.0)
    );
    sim.set_command([0.1, 0.2, 0.3], [0.01, -0.02, 0.03])
        .unwrap();
    let before = sim.snapshot();
    let before_kinematics = kinematics(&sim);
    let before_contacts = contacts(&sim);
    let before_handles = sim.handles.clone();
    let before_colliders = sim.collider_ids.clone();
    let before_rng = sim.rng;
    let collider_handle = actor(&sim, EntityId::Lander).colliders()[0];
    let shape = sim.colliders[collider_handle].shared_shape().clone();
    let tuning = ShipTuning {
        mass_kg: 8_123.0,
        inertia_scale: 2.5,
        thrust_scale: 3.0,
    };
    sim.execute(PhysicsCommand::TuneShip { settings: tuning })
        .unwrap();

    assert_eq!(kinematics(&sim), before_kinematics);
    assert_eq!(contacts(&sim), before_contacts);
    assert_eq!(sim.handles, before_handles);
    assert_eq!(sim.collider_ids, before_colliders);
    assert!(std::ptr::eq(
        sim.colliders[collider_handle].shape(),
        shape.as_ref()
    ));
    assert_eq!(sim.rng, before_rng);
    let body = actor(&sim, EntityId::Lander);
    assert!((body.mass() - tuning.mass_kg).abs() < 1e-8);
    assert!((sim.colliders[collider_handle].mass() - tuning.mass_kg).abs() < 1e-8);
    assert_eq!(body.mass_properties().local_mprops.local_com, Vector::ZERO);
    assert!(body.mass_properties().flags.is_empty());
    assert_inertia(&sim, tuning);
    let gravity =
        crate::astrophysics::acceleration(body.translation(), body.linvel(), settings().gravity);
    assert_close(
        body.user_force(),
        *body.rotation() * Vector::from_array(ship(&sim).thrust_n) + gravity * body.mass(),
        1e-8,
    );
    assert_close(
        body.user_torque(),
        *body.rotation() * Vector::from_array(ship(&sim).torque_nm),
        1e-8,
    );

    let mut expected = before;
    let lander = expected
        .assets
        .iter_mut()
        .find(|entity| entity.id == EntityId::Lander)
        .unwrap();
    lander.mass_kg = tuning.mass_kg;
    lander.actuator_gain = tuning.thrust_scale;
    lander.max_thrust_n = local_max_thrust_for_mass_n(tuning.mass_kg) * tuning.thrust_scale;
    reconcile_ship_wrench(lander, tuning.inertia_kg_m2());
    expected.ship_tuning_override = Some(tuning);
    expected.tuning_revision = 1;
    event(
        &mut expected,
        EntityId::Lander,
        "Local live ship tuning, revision 1",
    );
    assert_eq!(snapshot_json(&sim), serde_json::to_value(expected).unwrap());
    let after = snapshot_json(&sim);
    let force = body.user_force();
    let torque = body.user_torque();
    sim.tune_ship(tuning).unwrap();
    assert_eq!(snapshot_json(&sim), after);
    assert_eq!(actor(&sim, EntityId::Lander).user_force(), force);
    assert_eq!(actor(&sim, EntityId::Lander).user_torque(), torque);
    assert_eq!(contacts(&sim), before_contacts);
    let public = sim.observation();
    assert_eq!(public["ship_tuning"], json!(tuning));
    assert_eq!(public["tuning_revision"], 1);
    assert_eq!(public["manual_interventions"], 0);
    assert_eq!(public["experiment_settings"], after["experiment_settings"]);
    assert_eq!(public["shuttle"]["actuator_gain"], tuning.thrust_scale);
    let actual = sim.applied_physics().unwrap();
    assert_eq!(
        public["shuttle"]["applied_accel_mps2"],
        json!(scale(
            actual.last_actuator_application.unwrap().force_body_n,
            1.0 / actual.mass_kg,
        ))
    );
    validate(&sim.snapshot()).unwrap();
}

#[test]
fn live_gain_scales_actual_direct_acceleration_and_angular_acceleration_not_the_request() {
    let mut sim = free_flight();
    sim.set_command([0.0, 2.0, 0.0], [0.0, 0.0, 2.0]).unwrap();
    let tuning = ShipTuning {
        mass_kg: 12_000.0,
        inertia_scale: 4.0,
        thrust_scale: 5.0,
    };
    sim.tune_ship(tuning).unwrap();
    let lander = ship(&sim).clone();
    assert_eq!(lander.commanded_accel_mps2, [0.0, 0.5, 0.0]);
    assert_eq!(lander.commanded_angular_accel_rad_s2, [0.0, 0.0, 0.05]);
    assert_eq!(lander.thrust_n, [0.0, 30_000.0, 0.0]);
    assert_eq!(applied_jets(&lander).len(), 1);
    let body = actor(&sim, EntityId::Lander);
    let velocity = body.linvel();
    let gravity =
        crate::astrophysics::acceleration(body.translation(), velocity, settings().gravity);
    // Measure the initial angular response without disabling real gyroscopic forces.
    let dt = settings().integration.fixed_step_s * 0.01;
    sim.advance(dt);
    let body = actor(&sim, EntityId::Lander);
    assert_close(
        (body.linvel() - velocity) / dt,
        Vector::Y * 2.5 + gravity,
        1e-7,
    );
    assert_close(body.angvel() / dt, Vector::Z * 0.25, 1e-7);
    let expected_fuel = 30_000.0 * dt
        / (settings().lander.specific_impulse_s * settings().lander.standard_gravity_mps2);
    assert!((sim.state.mission.fuel_used_kg - expected_fuel).abs() < 1e-12);
    assert_inertia(&sim, tuning);
    validate(&sim.snapshot()).unwrap();
}

#[test]
fn live_nozzle_gain_and_inertia_drive_real_passive_rotation_without_a_second_torque() {
    let mut sim = free_flight();
    sim.set_manual_thrust([0.0, 0.5, 0.0]).unwrap();
    let request = ship(&sim).commanded_accel_mps2;
    let tuning = ShipTuning {
        mass_kg: 8_123.0,
        inertia_scale: 3.0,
        thrust_scale: 10.0,
    };
    let before = kinematics(&sim);
    sim.tune_ship(tuning).unwrap();
    assert_eq!(kinematics(&sim), before);
    let lander = ship(&sim).clone();
    assert_eq!(lander.thrust_application, ThrustApplication::ManualNozzles);
    assert_eq!(lander.commanded_accel_mps2, request);
    assert_eq!(lander.commanded_angular_accel_rad_s2, [0.0; 3]);
    let jets = applied_jets(&lander);
    assert_eq!(jets.len(), 1);
    assert_eq!(jets[0].force_body_n, lander.thrust_n);
    let wrench = applied_wrench(&jets);
    let expected_angular = inertia_matrix(tuning.inertia_kg_m2()).inverse()
        * Vector::from_array(wrench.torque_body_nm);
    assert!(expected_angular.length() > settings().lander.max_angular_accel_rad_s2);
    assert_close(
        actor(&sim, EntityId::Lander).user_torque(),
        Vector::from_array(wrench.torque_body_nm),
        1e-7,
    );
    let dt = settings().integration.fixed_step_s * 0.01;
    sim.advance(dt);
    assert_close(
        actor(&sim, EntityId::Lander).angvel() / dt,
        expected_angular,
        expected_angular.length() * 1e-4,
    );
    validate(&sim.snapshot()).unwrap();
}

#[test]
fn tuning_cannot_clear_a_genuine_terminal_collision_or_its_damage() {
    let mut sim = Simulation::with_experiment(42, experiment(1)).unwrap();
    sim.set_command([0.0, 0.0, 0.5], [0.0; 3]).unwrap();
    sim.advance(8.0);
    let handle = sim.handles[&EntityId::Lander];
    let up = *sim.bodies[sim.handles[&EntityId::LandingPad]].rotation() * Vector::Z;
    let mass = sim.bodies[handle].mass();
    sim.bodies[handle].apply_impulse(-up * mass * 60.0, true);
    sim.advance(2.0);
    assert_eq!(sim.mission_status(), MissionStatus::FailedDamage);
    assert!(sim.state.mission.damage > settings().mission.impact_threshold_j);
    let before = sim.snapshot();
    let before_kinematics = kinematics(&sim);
    let before_contacts = contacts(&sim);
    let mut tuning = sim.ship_tuning();
    tuning.mass_kg = 8_123.0;
    tuning.inertia_scale = 2.5;
    tuning.thrust_scale = 3.0;
    sim.tune_ship(tuning).unwrap();
    let flow = DebrisFlowTuning {
        speed_mps: 30.0,
        ..sim.debris_flow()
    };
    sim.tune_debris_flow(flow).unwrap();
    assert_eq!(
        serde_json::to_value(&sim.state.mission).unwrap(),
        serde_json::to_value(&before.mission).unwrap()
    );
    assert_eq!(sim.state.manual_interventions, before.manual_interventions);
    assert_eq!(sim.state.active_contacts, before.active_contacts);
    assert_eq!(sim.state.collisions, before.collisions);
    assert_eq!(contacts(&sim), before_contacts);
    let current_ship = actor(&sim, EntityId::Lander);
    let old_ship = before_kinematics
        .iter()
        .find(|entry| entry.0 == EntityId::Lander)
        .unwrap();
    assert_eq!(*current_ship.position(), old_ship.2);
    assert_eq!(current_ship.linvel(), old_ship.3);
    assert_eq!(current_ship.angvel(), old_ship.4);
    let after = snapshot_json(&sim);
    sim.advance(1.0);
    assert_eq!(snapshot_json(&sim), after);
    assert!(sim.set_command([0.0, 0.0, 0.5], [0.0; 3]).is_err());
}

#[test]
fn flow_adds_a_common_delta_to_real_post_collision_velocities_and_preserves_everything_else() {
    let mut sim = Simulation::with_experiment(10, experiment(3)).unwrap();
    let center = Vector::new(crate::model::phobos_enclosing_radius_m() + 500.0, 0.0, 0.0);
    for (id, offset, velocity) in [
        (EntityId::Debris(1), -2.5, 5.0),
        (EntityId::Debris(2), 2.5, -5.0),
    ] {
        let body = &mut sim.bodies[sim.handles[&id]];
        body.set_translation(center + Vector::X * offset, true);
        body.apply_impulse((Vector::X * velocity - body.linvel()) * body.mass(), true);
    }
    let pair = pair_key(EntityId::Debris(1), EntityId::Debris(2));
    for _ in 0..8 {
        sim.advance(settings().integration.fixed_step_s);
        if sim.state.active_contacts.contains(&pair) {
            break;
        }
    }
    assert!(sim.state.active_contacts.contains(&pair));
    assert!(actor(&sim, EntityId::Debris(1)).linvel().x < 4.0);
    let before = sim.snapshot();
    let before_kinematics = kinematics(&sim);
    let before_contacts = contacts(&sim);
    let before_handles = sim.handles.clone();
    let before_colliders = sim.collider_ids.clone();
    let before_rng = sim.rng;
    let flow = DebrisFlowTuning {
        speed_mps: 42.0,
        speed_spread_mps: 7.0,
        stream_axis: StreamAxis::X,
        stream_sign: 1,
    };
    let delta = Vector::from_array(sub(
        flow.common_velocity_mps(),
        sim.debris_flow().common_velocity_mps(),
    ));
    sim.execute(PhysicsCommand::TuneDebrisFlow { settings: flow })
        .unwrap();
    assert_eq!(sim.handles, before_handles);
    assert_eq!(sim.collider_ids, before_colliders);
    assert_eq!(contacts(&sim), before_contacts);
    assert_eq!(sim.rng, before_rng);
    for (id, handle, pose, velocity, spin) in before_kinematics {
        let body = &sim.bodies[handle];
        assert_eq!(*body.position(), pose);
        assert_eq!(body.angvel(), spin);
        let expected = match id {
            EntityId::Debris(_) => velocity + delta,
            _ => velocity,
        };
        assert_eq!(body.linvel(), expected);
    }
    let mut expected = before;
    for entity in &mut expected.assets {
        if entity.kind == EntityKind::Debris {
            entity.velocity = (Vector::from_array(entity.velocity) + delta).to_array();
        }
    }
    expected.debris_flow_override = Some(flow);
    expected.tuning_revision = 1;
    event(
        &mut expected,
        EntityId::Phobos,
        "Local live debris flow tuning, revision 1; spread applies to future births only",
    );
    assert_eq!(snapshot_json(&sim), serde_json::to_value(expected).unwrap());
    let after = snapshot_json(&sim);
    sim.tune_debris_flow(flow).unwrap();
    assert_eq!(snapshot_json(&sim), after);
    let velocity = actor(&sim, EntityId::Debris(1)).linvel();
    let spread_only = DebrisFlowTuning {
        speed_spread_mps: 0.0,
        ..flow
    };
    sim.tune_debris_flow(spread_only).unwrap();
    assert_eq!(actor(&sim, EntityId::Debris(1)).linvel(), velocity);
    assert_eq!(contacts(&sim), before_contacts);
    assert_eq!(sim.rng, before_rng);
    assert_eq!(sim.tuning_revision(), 2);
    assert_eq!(sim.observation()["debris_flow"], json!(spread_only));
    validate(&sim.snapshot()).unwrap();
}

#[test]
fn tuned_births_and_real_nozzles_resume_from_json_with_the_same_rng_and_dynamics() {
    let mut original = Simulation::with_experiment(42, experiment(3)).unwrap();
    let mut state = original.snapshot();
    state
        .assets
        .iter_mut()
        .find(|entity| entity.id == EntityId::Lander)
        .unwrap()
        .position = [
        crate::model::phobos_enclosing_radius_m() + 1_500.0,
        0.0,
        0.0,
    ];
    original = Simulation::from_state(state);
    original.set_manual_thrust([0.0, 0.3, 0.4]).unwrap();
    original
        .tune_ship(ShipTuning {
            mass_kg: 8_123.0,
            inertia_scale: 2.5,
            thrust_scale: 4.0,
        })
        .unwrap();
    let flow = DebrisFlowTuning {
        speed_mps: 60.0,
        speed_spread_mps: 0.0,
        stream_axis: StreamAxis::Z,
        stream_sign: 1,
    };
    original.tune_debris_flow(flow).unwrap();
    let exported = snapshot_json(&original);
    let mut restored = Simulation::new_default();
    restored
        .execute(PhysicsCommand::Restore {
            state: Box::new(serde_json::from_value(exported.clone()).unwrap()),
        })
        .unwrap();
    assert_eq!(snapshot_json(&restored), exported);
    assert_inertia(&restored, original.ship_tuning());
    let cfg = flow.apply_to(original.state.experiment_settings.unwrap().debris);
    let id = EntityId::Debris(1);
    for sim in [&mut original, &mut restored] {
        let body = &mut sim.bodies[sim.handles[&id]];
        body.set_translation(Vector::new(100.0, 0.0, flow_half_extent_m(cfg) - 0.1), true);
        body.set_linvel(Vector::Z * 60.0, true);
        sim.advance(settings().integration.fixed_step_s);
    }
    assert_eq!(snapshot_json(&restored), snapshot_json(&original));
    assert_eq!(original.rng, restored.rng);
    assert_eq!(original.state.rng_state, original.rng);
    assert_eq!(actor(&original, id).linvel(), Vector::Z * 60.0);
    let born = original
        .state
        .assets
        .iter()
        .find(|entity| entity.id == id)
        .unwrap();
    assert_eq!(
        born.position[2],
        -(flow_half_extent_m(cfg) - born.radius_m - BIRTH_CLEARANCE_M)
    );
    assert_eq!(born.velocity, actor(&original, id).linvel().to_array());
    assert_eq!(ship(&restored).thrust_n, ship(&original).thrust_n);
    assert_eq!(ship(&restored).torque_nm, ship(&original).torque_nm);
    assert_eq!(applied_jets(ship(&restored)).len(), 2);
    validate(&restored.snapshot()).unwrap();
}

#[test]
fn tuned_direct_commands_restore_real_hardware_and_reject_inconsistent_wrench_reports() {
    let mut original = free_flight();
    let tuning = ShipTuning {
        mass_kg: 8_123.0,
        inertia_scale: 2.5,
        thrust_scale: 4.0,
    };
    original.tune_ship(tuning).unwrap();
    original
        .set_command([0.1, 0.2, 0.3], [0.01, 0.02, 0.03])
        .unwrap();
    let valid = original.snapshot();
    let exported = serde_json::to_value(&valid).unwrap();
    let mut restored = Simulation::new_default();
    restored
        .execute(PhysicsCommand::Restore {
            state: Box::new(serde_json::from_value(exported.clone()).unwrap()),
        })
        .unwrap();
    assert_eq!(snapshot_json(&restored), exported);
    assert_inertia(&restored, tuning);
    assert!((actor(&restored, EntityId::Lander).mass() - tuning.mass_kg).abs() < 1e-8);
    let mut bad_force = valid.clone();
    bad_force.assets[0].thrust_n[0] *= 0.5;
    let mut bad_torque = valid.clone();
    bad_torque.assets[0].torque_nm[0] *= 0.5;
    let mut bad_inertia = valid;
    bad_inertia
        .ship_tuning_override
        .as_mut()
        .unwrap()
        .inertia_scale = 3.0;
    for state in [bad_force, bad_torque, bad_inertia] {
        assert!(
            restored
                .execute(PhysicsCommand::Restore {
                    state: Box::new(state)
                })
                .is_err()
        );
        assert_eq!(snapshot_json(&restored), exported);
        assert_inertia(&restored, tuning);
    }
    for sim in [&mut original, &mut restored] {
        sim.advance(settings().integration.fixed_step_s);
    }
    assert_eq!(snapshot_json(&restored), snapshot_json(&original));
    assert_close(
        actor(&restored, EntityId::Lander).user_force(),
        actor(&original, EntityId::Lander).user_force(),
        1e-9,
    );
    assert_close(
        actor(&restored, EntityId::Lander).user_torque(),
        actor(&original, EntityId::Lander).user_torque(),
        1e-9,
    );
}

#[test]
fn reset_repeats_effective_tuning_while_configure_starts_fresh() {
    let baseline = experiment(7);
    let mut sim = Simulation::with_experiment(42, baseline).unwrap();
    let original = sim.snapshot();
    let tuning = ShipTuning {
        mass_kg: 8_123.0,
        inertia_scale: 2.5,
        thrust_scale: 0.25,
    };
    let flow = DebrisFlowTuning {
        speed_mps: 50.0,
        speed_spread_mps: 2.0,
        stream_axis: StreamAxis::X,
        stream_sign: -1,
    };
    sim.tune_ship(tuning).unwrap();
    sim.tune_debris_flow(flow).unwrap();
    sim.set_manual_thrust([0.0, 0.3, 0.4]).unwrap();
    sim.advance(0.5);
    sim.debug_upright().unwrap();
    sim.reset(42).unwrap();
    assert_eq!(sim.state.experiment_settings, Some(baseline));
    assert_eq!(sim.ship_tuning(), tuning);
    assert_eq!(sim.debris_flow(), flow);
    assert_eq!(sim.tuning_revision(), 2);
    assert_eq!(sim.state.time_s, 0.0);
    assert_eq!(sim.state.manual_interventions, 0);
    assert_eq!(ship(&sim).thrust_n, [0.0; 3]);
    assert_eq!(actor(&sim, EntityId::Lander).user_force(), Vector::ZERO);
    assert_eq!(sim.state.rng_state, original.rng_state);
    assert_inertia(&sim, tuning);
    for (actual, initial) in sim.state.assets.iter().zip(&original.assets) {
        assert_eq!(actual.id, initial.id);
        assert_eq!(actual.position, initial.position);
        assert_eq!(actual.radius_m, initial.radius_m);
        if actual.kind == EntityKind::Debris {
            assert!(
                norm(sub(actual.velocity, flow.common_velocity_mps()))
                    <= flow.speed_spread_mps + 1e-10
            );
        }
    }
    validate(&sim.snapshot()).unwrap();
    let replay = snapshot_json(&sim);
    sim.reset(42).unwrap();
    assert_eq!(snapshot_json(&sim), replay);
    sim.execute(PhysicsCommand::ConfigureExperiment {
        settings: baseline,
        seed: Some(42),
    })
    .unwrap();
    assert_eq!(snapshot_json(&sim), serde_json::to_value(original).unwrap());
    assert_eq!(sim.tuning_revision(), 0);
    assert_eq!(sim.state.ship_tuning_override, None);
    assert_eq!(sim.state.debris_flow_override, None);
}

#[test]
fn none_profile_stays_a_corridor_through_live_tuning_restore_and_reset() {
    let mut sim = Simulation::with_profile(100, None).unwrap();
    let initial = sim.snapshot();
    let mut old = serde_json::to_value(&initial).unwrap();
    for key in [
        "experiment_settings",
        "ship_tuning_override",
        "debris_flow_override",
        "tuning_revision",
    ] {
        old.as_object_mut().unwrap().remove(key);
    }
    for entity in old["assets"].as_array_mut().unwrap() {
        entity.as_object_mut().unwrap().remove("actuator_gain");
    }
    sim.execute(PhysicsCommand::Restore {
        state: Box::new(serde_json::from_value(old).unwrap()),
    })
    .unwrap();
    assert_eq!(snapshot_json(&sim), serde_json::to_value(&initial).unwrap());
    let tuning = ShipTuning {
        mass_kg: 8_123.0,
        inertia_scale: 2.5,
        thrust_scale: 3.0,
    };
    let reference = sim.debris_flow();
    let flow = DebrisFlowTuning {
        speed_mps: 25.0,
        speed_spread_mps: 0.0,
        stream_axis: StreamAxis::Z,
        stream_sign: 1,
    };
    sim.set_manual_thrust([0.0, 0.3, 0.4]).unwrap();
    sim.tune_ship(tuning).unwrap();
    sim.tune_debris_flow(flow).unwrap();
    assert_eq!(sim.state.experiment_settings, None);
    assert_eq!(sim.state.model, initial.model);
    validate(&sim.snapshot()).unwrap();
    let exported = snapshot_json(&sim);
    let mut restored = Simulation::new_default();
    restored
        .execute(PhysicsCommand::Restore {
            state: Box::new(serde_json::from_value(exported.clone()).unwrap()),
        })
        .unwrap();
    assert_eq!(snapshot_json(&restored), exported);
    assert_eq!(restored.state.experiment_settings, None);
    assert_inertia(&restored, tuning);
    for world in [&mut sim, &mut restored] {
        world.advance(settings().integration.fixed_step_s);
    }
    assert_eq!(snapshot_json(&restored), snapshot_json(&sim));
    sim.reset(100).unwrap();
    assert_eq!(sim.state.experiment_settings, None);
    assert_eq!(sim.tuning_revision(), 2);
    assert_eq!(sim.state.rng_state, initial.rng_state);
    let delta = Vector::from_array(sub(
        flow.common_velocity_mps(),
        reference.common_velocity_mps(),
    ));
    for (actual, initial) in sim.state.assets.iter().zip(initial.assets) {
        assert_eq!(actual.id, initial.id);
        assert_eq!(actual.position, initial.position);
        assert_eq!(actual.radius_m, initial.radius_m);
        assert_eq!(
            actual.angular_velocity_rad_s,
            initial.angular_velocity_rad_s
        );
        if actual.kind == EntityKind::Debris {
            assert_eq!(
                actual.velocity,
                (Vector::from_array(initial.velocity) + delta).to_array()
            );
        }
    }
    validate(&sim.snapshot()).unwrap();
}

#[test]
fn invalid_tuning_missing_bodies_and_exhausted_revisions_are_atomic() {
    let mut sim = free_flight();
    sim.set_manual_thrust([0.0, 0.3, 0.4]).unwrap();
    sim.advance(settings().integration.fixed_step_s);
    let base = sim.ship_tuning();
    let flow = sim.debris_flow();
    let mut invalid = Vec::new();
    for value in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY, -1.0, 0.0] {
        invalid.push(PhysicsCommand::TuneShip {
            settings: ShipTuning {
                mass_kg: value,
                ..base
            },
        });
        invalid.push(PhysicsCommand::TuneShip {
            settings: ShipTuning {
                inertia_scale: value,
                ..base
            },
        });
        invalid.push(PhysicsCommand::TuneShip {
            settings: ShipTuning {
                thrust_scale: value,
                ..base
            },
        });
        invalid.push(PhysicsCommand::TuneDebrisFlow {
            settings: DebrisFlowTuning {
                speed_mps: value,
                ..flow
            },
        });
    }
    invalid.extend([
        PhysicsCommand::TuneShip {
            settings: ShipTuning {
                mass_kg: 100_001.0,
                ..base
            },
        },
        PhysicsCommand::TuneShip {
            settings: ShipTuning {
                inertia_scale: 10.01,
                ..base
            },
        },
        PhysicsCommand::TuneShip {
            settings: ShipTuning {
                thrust_scale: 10.01,
                ..base
            },
        },
        PhysicsCommand::TuneShip {
            settings: ShipTuning {
                thrust_scale: 0.099,
                ..base
            },
        },
        PhysicsCommand::TuneDebrisFlow {
            settings: DebrisFlowTuning {
                speed_mps: 200.01,
                ..flow
            },
        },
        PhysicsCommand::TuneDebrisFlow {
            settings: DebrisFlowTuning {
                speed_spread_mps: 10.01,
                ..flow
            },
        },
        PhysicsCommand::TuneDebrisFlow {
            settings: DebrisFlowTuning {
                speed_spread_mps: -0.01,
                ..flow
            },
        },
        PhysicsCommand::TuneDebrisFlow {
            settings: DebrisFlowTuning {
                speed_spread_mps: f64::NAN,
                ..flow
            },
        },
        PhysicsCommand::TuneDebrisFlow {
            settings: DebrisFlowTuning {
                stream_sign: 0,
                ..flow
            },
        },
    ]);
    let before = snapshot_json(&sim);
    let before_kinematics = kinematics(&sim);
    let force = actor(&sim, EntityId::Lander).user_force();
    let torque = actor(&sim, EntityId::Lander).user_torque();
    let mass = actor(&sim, EntityId::Lander).mass_properties().local_mprops;
    for command in invalid {
        assert!(sim.execute(command).is_err());
        assert_eq!(snapshot_json(&sim), before);
        assert_eq!(kinematics(&sim), before_kinematics);
        assert_eq!(actor(&sim, EntityId::Lander).user_force(), force);
        assert_eq!(actor(&sim, EntityId::Lander).user_torque(), torque);
        assert_eq!(
            actor(&sim, EntityId::Lander).mass_properties().local_mprops,
            mass
        );
    }
    let changed = ShipTuning {
        thrust_scale: 2.0,
        ..base
    };
    sim.state.tuning_revision = u32::MAX;
    let before = snapshot_json(&sim);
    assert!(sim.tune_ship(changed).is_err());
    assert!(
        sim.tune_debris_flow(DebrisFlowTuning {
            speed_mps: 30.0,
            ..flow
        })
        .is_err()
    );
    assert_eq!(snapshot_json(&sim), before);
    assert_eq!(kinematics(&sim), before_kinematics);
    sim.tune_ship(base).unwrap();
    sim.tune_debris_flow(flow).unwrap();
    assert_eq!(snapshot_json(&sim), before);

    let mut missing_collider = free_flight();
    let collider = actor(&missing_collider, EntityId::Lander).colliders()[0];
    missing_collider.collider_ids.remove(&collider);
    let before = snapshot_json(&missing_collider);
    let before_kinematics = kinematics(&missing_collider);
    assert!(missing_collider.tune_ship(changed).is_err());
    assert_eq!(snapshot_json(&missing_collider), before);
    assert_eq!(kinematics(&missing_collider), before_kinematics);
    let mut missing_body = free_flight();
    missing_body.handles.remove(&EntityId::Lander);
    let before = snapshot_json(&missing_body);
    assert!(missing_body.tune_ship(changed).is_err());
    assert_eq!(snapshot_json(&missing_body), before);
    let mut missing_entity = free_flight();
    missing_entity
        .state
        .assets
        .retain(|entity| entity.id != EntityId::Lander);
    let before = snapshot_json(&missing_entity);
    let before_kinematics = kinematics(&missing_entity);
    assert!(missing_entity.tune_ship(changed).is_err());
    assert_eq!(snapshot_json(&missing_entity), before);
    assert_eq!(kinematics(&missing_entity), before_kinematics);
    let mut missing_debris = Simulation::with_experiment(10, experiment(2)).unwrap();
    missing_debris.handles.remove(&EntityId::Debris(2));
    let before = snapshot_json(&missing_debris);
    assert!(
        missing_debris
            .tune_debris_flow(DebrisFlowTuning {
                speed_mps: 30.0,
                ..flow
            })
            .is_err()
    );
    assert_eq!(snapshot_json(&missing_debris), before);
}

#[test]
fn tuning_overrides_are_authoritative_without_bypassing_seeded_geometry_validation() {
    let mut sim = Simulation::with_experiment(42, experiment(2)).unwrap();
    sim.set_manual_thrust([0.0, 0.3, 0.4]).unwrap();
    sim.tune_ship(ShipTuning {
        mass_kg: 8_123.0,
        inertia_scale: 2.5,
        thrust_scale: 4.0,
    })
    .unwrap();
    let valid = sim.snapshot();
    validate(&valid).unwrap();
    let mut invalid = Vec::new();
    let mut bad = valid.clone();
    bad.assets[0].actuator_gain = 1.0;
    invalid.push(bad);
    let mut bad = valid.clone();
    bad.assets[0].mass_kg += 1.0;
    invalid.push(bad);
    let mut bad = valid.clone();
    bad.assets[0].max_thrust_n *= 2.0;
    invalid.push(bad);
    let mut bad = valid.clone();
    bad.assets[0].thrust_n[1] *= 0.5;
    invalid.push(bad);
    let mut bad = valid.clone();
    bad.assets[0].torque_nm[0] *= 2.0;
    invalid.push(bad);
    let mut bad = valid.clone();
    bad.assets.last_mut().unwrap().radius_m += 0.01;
    invalid.push(bad);
    let mut bad = valid.clone();
    bad.assets.last_mut().unwrap().mass_kg += 1.0;
    invalid.push(bad);
    let mut bad = valid.clone();
    bad.assets[1].actuator_gain = 2.0;
    invalid.push(bad);
    let mut bad = valid.clone();
    bad.ship_tuning_override = None;
    invalid.push(bad);
    let mut bad = valid.clone();
    bad.tuning_revision = 0;
    invalid.push(bad);
    let mut bad = valid.clone();
    bad.ship_tuning_override.as_mut().unwrap().thrust_scale = 10.01;
    invalid.push(bad);
    let before = snapshot_json(&sim);
    let before_kinematics = kinematics(&sim);
    for state in invalid {
        assert!(
            sim.execute(PhysicsCommand::Restore {
                state: Box::new(state)
            })
            .is_err()
        );
        assert_eq!(snapshot_json(&sim), before);
        assert_eq!(kinematics(&sim), before_kinematics);
        assert_inertia(&sim, valid.ship_tuning());
    }
}

#[test]
fn strict_tuning_commands_require_every_exact_dto_field() {
    let ship = json!({"mass_kg": 8123.0, "inertia_scale": 2.5, "thrust_scale": 3.0});
    let flow =
        json!({"speed_mps": 42.0, "speed_spread_mps": 7.0, "stream_axis": "x", "stream_sign": 1});
    for (op, settings) in [("tune_ship", ship), ("tune_debris_flow", flow)] {
        let request = json!({"op": op, "settings": settings});
        assert!(serde_json::from_value::<PhysicsCommand>(request.clone()).is_ok());
        for key in settings.as_object().unwrap().keys() {
            let mut missing = request.clone();
            missing["settings"].as_object_mut().unwrap().remove(key);
            assert!(serde_json::from_value::<PhysicsCommand>(missing).is_err());
        }
        for extra in ["gain", "seed", "policy", "massKg"] {
            let mut unknown = request.clone();
            unknown["settings"][extra] = json!(1);
            assert!(serde_json::from_value::<PhysicsCommand>(unknown).is_err());
        }
        let mut unknown = request;
        unknown["unexpected"] = json!(true);
        assert!(serde_json::from_value::<PhysicsCommand>(unknown).is_err());
        assert!(serde_json::from_value::<PhysicsCommand>(json!({"op": op})).is_err());
    }
}
