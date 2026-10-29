use super::*;
use crate::terrain::{landing_site_at_direction, normalized_surface_direction};
use rapier3d_f64::parry::query::intersection_test;

fn experiment(count: usize) -> ExperimentSettings {
    let mut experiment = ExperimentSettings::default();
    experiment.debris.count = count;
    experiment
}

fn asset(sim: &Simulation, id: EntityId) -> &EntityState {
    sim.state
        .assets
        .iter()
        .find(|entity| entity.id == id)
        .unwrap()
}

fn actual(sim: &Simulation, id: EntityId) -> &RigidBody {
    &sim.bodies[sim.handles[&id]]
}

fn actual_bits(sim: &Simulation) -> Vec<(RigidBodyHandle, Vec<u64>)> {
    sim.bodies
        .iter()
        .map(|(handle, body)| {
            let bits = body
                .translation()
                .to_array()
                .into_iter()
                .chain(body.rotation().to_array())
                .chain(body.linvel().to_array())
                .chain(body.angvel().to_array())
                .chain(body.user_force().to_array())
                .chain(body.user_torque().to_array())
                .map(f64::to_bits)
                .collect();
            (handle, bits)
        })
        .collect()
}

fn assert_atomic_rejection(sim: &mut Simulation, direction: [f64; 3]) -> String {
    let snapshot = serde_json::to_value(sim.snapshot()).unwrap();
    let bodies = actual_bits(sim);
    let colliders: Vec<_> = sim
        .colliders
        .iter()
        .map(|(handle, collider)| {
            (
                handle,
                *collider.position(),
                collider.shared_shape().clone(),
            )
        })
        .collect();
    let handles = sim.handles.clone();
    let collider_ids = sim.collider_ids.clone();
    let application = sim.last_actuator_application;
    let rng = sim.rng;
    let error = sim.move_launchpad(direction).unwrap_err();
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), snapshot);
    assert_eq!(actual_bits(sim), bodies);
    assert_eq!(sim.handles, handles);
    assert_eq!(sim.collider_ids, collider_ids);
    assert_eq!(sim.last_actuator_application, application);
    assert_eq!(sim.rng, rng);
    for (handle, pose, shape) in colliders {
        assert_eq!(*sim.colliders[handle].position(), pose);
        assert!(std::ptr::eq(sim.colliders[handle].shape(), shape.as_ref()));
    }
    error
}

#[test]
fn moving_launch_site_moves_the_same_two_real_bodies_and_keeps_every_other_actor() {
    let config = experiment(4);
    for (preset, direction) in [
        (FlightPreset::FarSideL2, [1.0, 0.4, -0.2]),
        (FlightPreset::MarsFacingL1, [-1.0, -0.3, 0.4]),
    ] {
        let mut sim = Simulation::with_experiment(42, config).unwrap();
        sim.configure_flight(config, Some(42), preset).unwrap();
        sim.set_manual_thrust([0.0, 0.2, 0.3]).unwrap();
        sim.advance(settings().integration.fixed_step_s);
        sim.tune_ship(ShipTuning {
            mass_kg: 8_123.0,
            inertia_scale: 2.5,
            thrust_scale: 3.2,
        })
        .unwrap();
        let before = sim.snapshot();
        let handles = sim.handles.clone();
        let collider_ids = sim.collider_ids.clone();
        let inertia = sim.ship_inertia_kg_m2();
        let mass = actual(&sim, EntityId::Lander)
            .mass_properties()
            .local_mprops;
        let others: Vec<_> = sim
            .handles
            .iter()
            .filter(|(id, _)| !matches!(id, EntityId::LandingPad | EntityId::Lander))
            .map(|(&id, &handle)| {
                (
                    id,
                    *sim.bodies[handle].position(),
                    sim.bodies[handle].linvel(),
                    sim.bodies[handle].angvel(),
                    sim.bodies[handle].user_force(),
                    sim.bodies[handle].user_torque(),
                )
            })
            .collect();
        let site = landing_site_at_direction(direction).unwrap();
        let rng = sim.rng;
        let after = sim
            .execute(PhysicsCommand::MoveLaunchpad {
                surface_direction: direction,
            })
            .unwrap();
        assert_eq!(sim.handles, handles);
        assert_eq!(sim.collider_ids, collider_ids);
        for (id, position) in [
            (EntityId::LandingPad, site.pad_position_m),
            (EntityId::Lander, site.lander_position_m),
        ] {
            let body = actual(&sim, id);
            assert_eq!(body.translation().to_array(), position);
            assert_eq!(body.rotation().to_array(), site.rotation);
            assert_eq!(asset(&sim, id).position, position);
            assert_eq!(asset(&sim, id).rotation, body.rotation().to_array());
            assert_eq!(body.linvel(), Vector::ZERO);
            assert_eq!(body.angvel(), Vector::ZERO);
            assert_eq!(body.user_force(), Vector::ZERO);
            assert_eq!(body.user_torque(), Vector::ZERO);
            assert!(
                (*body.rotation() * Vector::Z - Vector::from_array(site.normal)).length() < 1.0e-12
            );
            let collider = &sim.colliders[body.colliders()[0]];
            assert_eq!(
                *collider.position(),
                *body.position() * *collider.position_wrt_parent().unwrap()
            );
        }
        assert_eq!(
            actual(&sim, EntityId::Lander)
                .mass_properties()
                .local_mprops,
            mass
        );
        assert_eq!(sim.ship_inertia_kg_m2(), inertia);
        let ship = asset(&sim, EntityId::Lander);
        let old_ship = before
            .assets
            .iter()
            .find(|entity| entity.id == EntityId::Lander)
            .unwrap();
        assert_eq!(ship.fuel_kg, old_ship.fuel_kg);
        assert_eq!(ship.mass_kg, old_ship.mass_kg);
        assert_eq!(ship.max_thrust_n, old_ship.max_thrust_n);
        assert_eq!(ship.actuator_gain, old_ship.actuator_gain);
        assert_eq!(ship.commanded_accel_mps2, [0.0; 3]);
        assert_eq!(ship.commanded_angular_accel_rad_s2, [0.0; 3]);
        assert_eq!(ship.thrust_n, [0.0; 3]);
        assert_eq!(ship.torque_nm, [0.0; 3]);
        assert_eq!(ship.status, EntityStatus::Landed);
        assert_eq!(sim.last_actuator_application, None);
        assert_eq!(after.time_s, before.time_s);
        assert_eq!(after.seed, before.seed);
        assert_eq!(sim.rng, rng);
        assert_eq!(after.rng_state, before.rng_state);
        assert_eq!(after.collisions, before.collisions);
        assert_eq!(after.tuning_revision, before.tuning_revision);
        assert_eq!(after.experiment_settings, before.experiment_settings);
        assert_eq!(after.ship_tuning_override, before.ship_tuning_override);
        assert_eq!(after.debris_flow_override, before.debris_flow_override);
        assert_eq!(after.flight_preset, preset);
        assert_eq!(after.mission.target_region, preset.target_region());
        assert_eq!(
            after.mission.initial_distance_m,
            before.mission.initial_distance_m
        );
        assert_eq!(after.mission.status, before.mission.status);
        assert_eq!(after.mission.score, before.mission.score);
        assert_eq!(after.mission.damage, before.mission.damage);
        assert_eq!(after.mission.fuel_used_kg, before.mission.fuel_used_kg);
        assert_eq!(after.manual_interventions, before.manual_interventions + 1);
        assert_eq!(
            after.launch_site_direction,
            Some(normalized_surface_direction(direction).unwrap())
        );
        assert_eq!(after.trial_start_launch_site_direction, None);
        assert!(after.observations.is_empty());
        for old in before
            .assets
            .iter()
            .filter(|entity| !matches!(entity.id, EntityId::Lander | EntityId::LandingPad))
        {
            assert_eq!(
                serde_json::to_value(asset(&sim, old.id)).unwrap(),
                serde_json::to_value(old).unwrap()
            );
        }
        for (id, pose, velocity, spin, force, torque) in others {
            let body = actual(&sim, id);
            assert_eq!(*body.position(), pose);
            assert_eq!(body.linvel(), velocity);
            assert_eq!(body.angvel(), spin);
            assert_eq!(body.user_force(), force);
            assert_eq!(body.user_torque(), torque);
        }
        let receipt = after.events.last().unwrap();
        assert_eq!(receipt.asset, EntityId::LandingPad);
        assert_eq!(receipt.t_s, before.time_s);
        assert!(
            receipt.message.contains("from position")
                && receipt.message.contains("to position")
                && receipt.message.contains("rotation")
        );
        validate(&after).unwrap();
        let report = sim.observation();
        assert_eq!(
            report["launch_site_direction"],
            json!(after.launch_site_direction)
        );
        assert_eq!(
            report["launchpad"]["position"],
            json!(actual(&sim, EntityId::LandingPad).translation().to_array())
        );
        assert_eq!(
            report["launchpad"]["rotation"],
            json!(actual(&sim, EntityId::LandingPad).rotation().to_array())
        );
        assert_eq!(report["launchpad"]["source_time_s"], after.time_s);
        assert!(report.get("assets").is_none());
        assert!(report.get("debris").is_none());
        assert_eq!(
            sim.applied_physics().unwrap().last_actuator_application,
            None
        );
    }
}

#[test]
fn launchpad_public_pose_comes_from_the_actual_body_even_before_cache_propagation() {
    let mut sim = Simulation::with_experiment(3, experiment(0)).unwrap();
    let handle = sim.handles[&EntityId::LandingPad];
    let position = sim.bodies[handle].translation() + Vector::Y * 0.25;
    let rotation = Rotation::from_rotation_x(0.2) * *sim.bodies[handle].rotation();
    sim.bodies[handle].set_position(Pose::from_parts(position, rotation), true);
    let report = sim.observation();
    assert_eq!(report["launchpad"]["position"], json!(position.to_array()));
    assert_eq!(report["launchpad"]["rotation"], json!(rotation.to_array()));
    assert_eq!(report["launch_site_direction"], Value::Null);
    assert_ne!(
        asset(&sim, EntityId::LandingPad).position,
        position.to_array()
    );
}

#[test]
fn relocated_real_lander_can_lift_from_mesh_fitted_pad_with_body_up_thrust() {
    let mut sim = Simulation::with_experiment(100, experiment(0)).unwrap();
    for direction in [[0.0, 1.0, 0.0], [0.3, 0.5, -0.8], [-0.4, -0.8, 0.2]] {
        sim.move_launchpad(direction).unwrap();
        sim.advance(8.0);
        assert!(
            sim.state
                .active_contacts
                .contains(&pair_key(EntityId::Lander, EntityId::LandingPad))
        );
        let pad = *actual(&sim, EntityId::LandingPad).position();
        let start = actual(&sim, EntityId::Lander).translation();
        let up = *actual(&sim, EntityId::Lander).rotation() * Vector::Z;
        sim.set_command([0.0, 0.0, settings().lander.max_accel_mps2], [0.0; 3])
            .unwrap();
        sim.advance(4.0);
        let body = actual(&sim, EntityId::Lander);
        assert!(body.linvel().dot(up) > 1.0);
        assert!((body.translation() - start).dot(up) > 2.0);
        assert_eq!(*actual(&sim, EntityId::LandingPad).position(), pad);
        assert!(sim.state.mission.fuel_used_kg > 0.0);
        assert_eq!(sim.mission_status(), MissionStatus::Running);
        validate(&sim.snapshot()).unwrap();
    }
}

#[test]
fn rejected_surface_moves_leave_commands_counters_rng_bodies_and_colliders_untouched() {
    let direction = [0.3, 0.5, 0.8];
    let site = landing_site_at_direction(direction).unwrap();
    for invalid in 0..17 {
        let mut sim = Simulation::with_experiment(42, experiment(1)).unwrap();
        sim.set_command([0.1, 0.2, 0.3], [0.01, 0.02, 0.03])
            .unwrap();
        sim.advance(settings().integration.fixed_step_s);
        let request = match invalid {
            0 => [0.0; 3],
            1 => [f64::NAN, 0.0, 1.0],
            2 => [1.0, f64::INFINITY, 0.0],
            3 => [0.0, 1.0, f64::NEG_INFINITY],
            _ => direction,
        };
        match invalid {
            4 => sim.state.manual_interventions = u32::MAX,
            5 => {
                sim.handles.remove(&EntityId::LandingPad);
            }
            6 => {
                sim.handles.remove(&EntityId::Lander);
            }
            7 => {
                sim.state
                    .assets
                    .retain(|entity| entity.id != EntityId::LandingPad);
            }
            8 => {
                let handle = actual(&sim, EntityId::LandingPad).colliders()[0];
                sim.collider_ids.remove(&handle);
            }
            9 => {
                // Leave both the EntityState and collider's cached world pose
                // stale. The real moved rock must still block the request.
                let handle = sim.handles[&EntityId::Debris(1)];
                sim.bodies[handle].set_translation(Vector::from_array(site.pad_position_m), true);
            }
            10 => {
                let handle = sim.handles[&EntityId::ReturnShip];
                sim.bodies[handle]
                    .set_translation(Vector::from_array(site.lander_position_m), true);
            }
            11 | 12 => {
                let id = if invalid == 11 {
                    EntityId::LandingPad
                } else {
                    EntityId::Lander
                };
                let collider = actual(&sim, id).colliders()[0];
                // Shift the actual collision shape below its candidate seat.
                sim.colliders[collider]
                    .set_position_wrt_parent(Pose::translation(0.0, 0.0, -100.0));
            }
            13 => {
                let handle = sim.handles[&EntityId::Debris(1)];
                sim.bodies[handle].set_translation(
                    Vector::new(settings().limits.position_m + 1.0, 0.0, 0.0),
                    true,
                );
            }
            14 => {
                let handle = sim.handles[&EntityId::Lander];
                let collider = sim.bodies[handle].colliders()[0];
                let candidate = Pose::from_parts(
                    Vector::from_array(site.lander_position_m),
                    Rotation::from_array(site.rotation),
                );
                let mars = Vector::from_array(sim.state.environment.mars_position_m);
                // Keep the real configured Mars and the real lander shape.
                // Its local transform makes the proposed shape overlap Mars.
                sim.colliders[collider].set_position_wrt_parent(Pose::from_translation(
                    candidate.inverse_transform_point(mars),
                ));
            }
            15 => {
                let handle = sim.handles[&EntityId::Debris(1)];
                sim.bodies[handle].set_translation(Vector::new(f64::NAN, 0.0, 0.0), true);
            }
            16 => {
                let handle = sim.handles[&EntityId::Phobos];
                sim.bodies[handle].set_translation(Vector::X, true);
            }
            _ => {}
        }
        let error = assert_atomic_rejection(&mut sim, request);
        if invalid == 9 {
            assert!(error.contains(EntityId::Debris(1).to_string().as_str()));
        }
        if invalid == 10 {
            assert!(error.contains(EntityId::ReturnShip.to_string().as_str()));
        }
        if invalid == 11 || invalid == 12 {
            assert!(error.contains("Phobos") || error.contains("phobos"));
        }
        if invalid == 14 {
            assert!(error.contains("Mars"));
        }
    }
}

#[test]
fn custom_reset_restore_and_later_moves_keep_canonical_birth_ids_radii_rng_and_initial_range() {
    let config = experiment(12);
    let mut sim = Simulation::with_experiment(42, config).unwrap();
    sim.set_command([0.0, 0.0, 0.4], [0.0; 3]).unwrap();
    sim.advance(0.3);
    sim.move_launchpad([0.4, 0.6, -0.7]).unwrap();
    let original_range = sim.state.mission.initial_distance_m;
    let moved = sim.snapshot();
    assert_eq!(moved.trial_start_launch_site_direction, None);
    sim.execute(PhysicsCommand::Restore {
        state: Box::new(moved.clone()),
    })
    .unwrap();
    assert_eq!(
        serde_json::to_value(sim.snapshot()).unwrap(),
        serde_json::to_value(moved).unwrap()
    );
    sim.reset(43).unwrap();
    let baseline = Simulation::with_experiment(43, config).unwrap();
    let start = sim.snapshot();
    assert_eq!(
        start.launch_site_direction,
        start.trial_start_launch_site_direction
    );
    let direction = start.launch_site_direction.unwrap();
    let site = landing_site_at_direction(direction).unwrap();
    let target = asset(&sim, EntityId::ReturnShip).position;
    assert_eq!(start.time_s, 0.0);
    assert_eq!(start.manual_interventions, 0);
    assert_eq!(
        start.mission.initial_distance_m,
        norm(sub(site.lander_position_m, target))
    );
    assert_ne!(start.mission.initial_distance_m, original_range);
    assert_eq!(sim.rng, baseline.rng);
    assert_eq!(start.rng_state, baseline.state.rng_state);
    for rock in start
        .assets
        .iter()
        .filter(|entity| entity.kind == EntityKind::Debris)
    {
        assert_eq!(
            serde_json::to_value(rock).unwrap(),
            serde_json::to_value(asset(&baseline, rock.id)).unwrap()
        );
    }
    validate(&start).unwrap();
    sim.move_launchpad([-0.6, 0.7, 0.4]).unwrap();
    assert_eq!(
        sim.state.trial_start_launch_site_direction,
        start.trial_start_launch_site_direction
    );
    assert_eq!(
        sim.state.mission.initial_distance_m,
        start.mission.initial_distance_m
    );
    let archived = serde_json::to_value(sim.snapshot()).unwrap();
    sim.execute(PhysicsCommand::Restore {
        state: Box::new(serde_json::from_value(archived.clone()).unwrap()),
    })
    .unwrap();
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), archived);
    for invalid in 0..5 {
        let mut state = sim.snapshot();
        match invalid {
            0 => state.mission.initial_distance_m += 1.0,
            1 => state.launch_site_direction = Some([2.0, 0.0, 0.0]),
            2 => state.trial_start_launch_site_direction = Some([0.0; 3]),
            3 => {
                state
                    .assets
                    .iter_mut()
                    .find(|entity| entity.id == EntityId::LandingPad)
                    .unwrap()
                    .position[1] += 1.0
            }
            4 => {
                state
                    .assets
                    .iter_mut()
                    .find(|entity| entity.id == EntityId::Debris(1))
                    .unwrap()
                    .radius_m += 0.01
            }
            _ => unreachable!(),
        }
        assert!(
            sim.execute(PhysicsCommand::Restore {
                state: Box::new(state)
            })
            .is_err()
        );
        assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), archived);
    }
    sim.configure_flight(config, Some(43), FlightPreset::MarsFacingL1)
        .unwrap();
    assert_eq!(sim.state.launch_site_direction, None);
    assert_eq!(sim.state.trial_start_launch_site_direction, None);
    sim.move_launchpad([0.6, -0.7, 0.2]).unwrap();
    sim.execute(PhysicsCommand::ConfigureExperiment {
        settings: config,
        seed: Some(43),
    })
    .unwrap();
    assert_eq!(sim.state.flight_preset, FlightPreset::MarsFacingL1);
    assert_eq!(sim.state.launch_site_direction, None);
    assert_eq!(sim.state.trial_start_launch_site_direction, None);
}

#[test]
fn reset_rejects_a_new_seed_rock_at_the_custom_site_before_replacing_the_active_world() {
    let mut config = experiment(1);
    config.debris.distribution = DebrisDistribution::DirectedStream;
    config.debris.radius_min_m = 0.2;
    config.debris.radius_max_m = 0.2;
    config.debris.shell_inner_m = 0.0;
    config.debris.shell_outer_m = 10.0;
    // Find a real canonical seeded rock that would occupy a different custom
    // seat. No birth rules, geometry, observations or replacement RNG are faked.
    let (blocked_seed, direction) = (1..=128)
        .find_map(|seed| {
            let trial = Simulation::with_experiment(seed, config).unwrap();
            let rock = actual(&trial, EntityId::Debris(1));
            let direction = rock.translation().to_array();
            let site = landing_site_at_direction(direction).ok()?;
            let collider = &trial.colliders[rock.colliders()[0]];
            [
                (EntityId::LandingPad, site.pad_position_m),
                (EntityId::Lander, site.lander_position_m),
            ]
            .into_iter()
            .any(|(id, position)| {
                let shape = trial.colliders[actual(&trial, id).colliders()[0]].shape();
                intersection_test(
                    &Pose::from_parts(
                        Vector::from_array(position),
                        Rotation::from_array(site.rotation),
                    ),
                    shape,
                    rock.position(),
                    collider.shape(),
                )
                .unwrap()
                .intersecting
            })
            .then_some((seed, direction))
        })
        .expect("bounded real seeded population must include a rock near a proposed seat");
    let mut sim = Simulation::with_experiment(200, config).unwrap();
    let handle = sim.handles[&EntityId::Debris(1)];
    let clear_position = [30_000.0; 3];
    sim.bodies[handle].set_translation(Vector::from_array(clear_position), true);
    sim.state
        .assets
        .iter_mut()
        .find(|entity| entity.id == EntityId::Debris(1))
        .unwrap()
        .position = clear_position;
    sim.move_launchpad(direction).unwrap();
    sim.set_command([0.0, 0.0, 0.4], [0.0; 3]).unwrap();
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    let actual = actual_bits(&sim);
    let rng = sim.rng;
    let error = sim.reset(blocked_seed).unwrap_err();
    assert!(
        error.contains("custom launch site")
            && error.contains(EntityId::Debris(1).to_string().as_str())
    );
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
    assert_eq!(actual_bits(&sim), actual);
    assert_eq!(sim.rng, rng);
}

#[test]
fn old_none_corridor_archives_keep_omitted_anchor_fields_and_support_custom_site_reset() {
    let mut sim = Simulation::with_profile(100, None).unwrap();
    let old = serde_json::to_value(sim.snapshot()).unwrap();
    assert!(old.get("launch_site_direction").is_none());
    assert!(old.get("trial_start_launch_site_direction").is_none());
    sim.execute(PhysicsCommand::Restore {
        state: Box::new(serde_json::from_value(old.clone()).unwrap()),
    })
    .unwrap();
    sim.reset(100).unwrap();
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), old);
    let mut altered_range = sim.snapshot();
    altered_range.mission.initial_distance_m += 1.0;
    assert!(validate(&altered_range).is_err());
    sim.move_launchpad([0.0, 1.0, 0.0]).unwrap();
    sim.reset(100).unwrap();
    let custom = sim.snapshot();
    assert_eq!(custom.experiment_settings, None);
    assert_eq!(custom.flight_preset, FlightPreset::FarSideL2);
    assert_eq!(
        custom.launch_site_direction,
        custom.trial_start_launch_site_direction
    );
    for rock in custom
        .assets
        .iter()
        .filter(|entity| entity.kind == EntityKind::Debris)
    {
        let original = old["assets"]
            .as_array()
            .unwrap()
            .iter()
            .find(|entity| entity["id"] == json!(rock.id))
            .unwrap();
        assert_eq!(serde_json::to_value(rock).unwrap(), *original);
    }
    validate(&custom).unwrap();
    let encoded = serde_json::to_value(custom).unwrap();
    sim.execute(PhysicsCommand::Restore {
        state: Box::new(serde_json::from_value(encoded.clone()).unwrap()),
    })
    .unwrap();
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), encoded);
}

fn unrelated_warmstarts(sim: &Simulation) -> Vec<(f64, f64, Vector, f64)> {
    let a = actual(sim, EntityId::Debris(1)).colliders()[0];
    let b = actual(sim, EntityId::Debris(2)).colliders()[0];
    sim.narrow
        .contact_pair(a, b)
        .unwrap()
        .solver_manifolds()
        .iter()
        .flat_map(|manifold| {
            manifold.points.iter().map(|point| {
                (
                    point.data.impulse,
                    point.data.warmstart_impulse,
                    point.data.warmstart_tangent_world,
                    point.data.warmstart_twist_impulse,
                )
            })
        })
        .collect()
}

#[test]
fn relocating_pad_invalidates_only_old_pad_and_lander_contacts_not_other_warmstarts() {
    let mut config = experiment(2);
    config.debris.distribution = DebrisDistribution::DirectedStream;
    config.debris.radius_min_m = 2.0;
    config.debris.radius_max_m = 2.0;
    let mut sim = Simulation::with_experiment(10, config).unwrap();
    let center = Vector::new(crate::model::phobos_enclosing_radius_m() + 500.0, 0.0, 0.0);
    let a = EntityId::Debris(1);
    let b = EntityId::Debris(2);
    for (id, offset, speed) in [(a, -2.5, 5.0), (b, 2.5, -5.0)] {
        let body = &mut sim.bodies[sim.handles[&id]];
        body.set_translation(center + Vector::X * offset, true);
        body.set_linvel(Vector::X * speed, true);
    }
    for _ in 0..12 {
        sim.advance(settings().integration.fixed_step_s);
        if sim.state.active_contacts.contains(&pair_key(a, b)) {
            break;
        }
    }
    assert!(sim.state.active_contacts.contains(&pair_key(a, b)));
    let warmstarts = unrelated_warmstarts(&sim);
    assert!(warmstarts.iter().any(|contact| contact.0 > 0.0));
    let rocks = [
        (a, *actual(&sim, a).position(), actual(&sim, a).linvel()),
        (b, *actual(&sim, b).position(), actual(&sim, b).linvel()),
    ];
    sim.move_launchpad([0.3, 0.5, 0.8]).unwrap();
    assert_eq!(unrelated_warmstarts(&sim), warmstarts);
    for (id, pose, velocity) in rocks {
        assert_eq!(*actual(&sim, id).position(), pose);
        assert_eq!(actual(&sim, id).linvel(), velocity);
    }
    assert!(sim.state.active_contacts.contains(&pair_key(a, b)));
    assert!(
        sim.state
            .active_contacts
            .iter()
            .all(|pair| !pair.contains(&EntityId::Lander) && !pair.contains(&EntityId::LandingPad))
    );
    let saved = sim.snapshot();
    sim.advance(settings().integration.fixed_step_s);
    validate(&sim.snapshot()).unwrap();
    sim.execute(PhysicsCommand::Restore {
        state: Box::new(saved),
    })
    .unwrap();
    let before = sim.snapshot();
    sim.move_launchpad([-0.2, 0.8, 0.5]).unwrap();
    assert!(sim.state.active_contacts.contains(&pair_key(a, b)));
    assert_eq!(sim.state.collisions, before.collisions);
    assert_eq!(sim.state.time_s, before.time_s);
    assert_eq!(sim.state.rng_state, before.rng_state);
    validate(&sim.snapshot()).unwrap();
}

#[test]
fn relocating_a_launch_site_cannot_revive_or_rescore_a_real_terminal_impact() {
    let mut sim = Simulation::with_experiment(100, experiment(0)).unwrap();
    sim.set_command([0.0, 0.0, 0.5], [0.0; 3]).unwrap();
    sim.advance(8.0);
    let handle = sim.handles[&EntityId::Lander];
    let up = *actual(&sim, EntityId::LandingPad).rotation() * Vector::Z;
    let mass = sim.bodies[handle].mass();
    sim.bodies[handle].apply_impulse(-up * mass * 60.0, true);
    sim.advance(2.0);
    assert_eq!(sim.mission_status(), MissionStatus::FailedDamage);
    let before = sim.snapshot();
    sim.move_launchpad([0.0, -1.0, 0.0]).unwrap();
    assert_eq!(sim.state.mission.status, before.mission.status);
    assert_eq!(sim.state.mission.phase, before.mission.phase);
    assert_eq!(sim.state.mission.score, before.mission.score);
    assert_eq!(
        sim.state.mission.initial_distance_m,
        before.mission.initial_distance_m
    );
    assert_eq!(sim.state.mission.damage, before.mission.damage);
    assert_eq!(sim.state.mission.fuel_used_kg, before.mission.fuel_used_kg);
    assert_eq!(sim.state.time_s, before.time_s);
    assert_eq!(sim.state.collisions, before.collisions);
    assert!(sim.set_command([0.0, 0.0, 0.5], [0.0; 3]).is_err());
    let stopped = serde_json::to_value(sim.snapshot()).unwrap();
    sim.advance(1.0);
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), stopped);
    validate(&sim.snapshot()).unwrap();
}
