use super::*;
use crate::model::StreamAxis;

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

fn assert_vector_close(actual: Vector, expected: Vector, tolerance: f64) {
    assert!(
        (actual - expected).length() <= tolerance,
        "actual {actual:?}, expected {expected:?}"
    );
}

fn assert_matrix_close(actual: [[f64; 3]; 3], expected: [[f64; 3]; 3]) {
    for (actual, expected) in actual
        .into_iter()
        .flatten()
        .zip(expected.into_iter().flatten())
    {
        assert!((actual - expected).abs() <= 1e-8 + expected.abs() * 1e-10);
    }
}

#[test]
fn seeded_population_covers_every_side_with_real_mixed_radii_and_clearance() {
    let mut config = ExperimentSettings::default();
    config.debris.distribution = DebrisDistribution::DirectedStream;
    for seed in [0, 1, 100, u32::MAX as u64] {
        let sim = Simulation::with_experiment(seed, config).unwrap();
        let repeated = Simulation::with_experiment(seed, config).unwrap();
        assert_eq!(
            serde_json::to_value(sim.snapshot()).unwrap(),
            serde_json::to_value(repeated.snapshot()).unwrap()
        );
        let debris: Vec<_> = sim
            .state
            .assets
            .iter()
            .filter(|entity| entity.kind == EntityKind::Debris)
            .collect();
        assert_eq!(debris.len(), 500);
        let mut octants = [0usize; 8];
        let mut small = 0;
        let mut large = 0;
        for (index, asteroid) in debris.iter().enumerate() {
            let octant = asteroid
                .position
                .iter()
                .enumerate()
                .fold(0usize, |bits, (axis, value)| {
                    bits | (usize::from(*value > 0.0) << axis)
                });
            octants[octant] += 1;
            let radial = norm(asteroid.position);
            let terrain =
                crate::terrain::surface_radius_m(bevy::math::DVec3::from_array(asteroid.position));
            let clearance = radial - terrain - asteroid.radius_m;
            assert!(clearance >= config.debris.shell_inner_m - 1e-8);
            assert!(clearance <= config.debris.shell_outer_m + 1e-8);
            assert!(
                phobos_shape()
                    .distance_to_local_point(Vector::from_array(asteroid.position), false)
                    > asteroid.radius_m + BIRTH_CLEARANCE_M
            );
            assert!(
                (config.debris.radius_min_m..=config.debris.radius_max_m)
                    .contains(&asteroid.radius_m)
            );
            assert_eq!(asteroid.mass_kg, debris_mass_kg(asteroid.radius_m));
            assert!(
                (actor(&sim, asteroid.id).mass() - asteroid.mass_kg).abs()
                    <= asteroid.mass_kg * 1e-12
            );
            assert!(actor(&sim, asteroid.id).is_dynamic());
            assert!(actor(&sim, asteroid.id).is_ccd_enabled());
            small += usize::from(asteroid.radius_m < 1.0);
            large += usize::from(asteroid.radius_m > 20.0);
            for other in &debris[..index] {
                assert!(
                    norm(sub(asteroid.position, other.position))
                        > asteroid.radius_m + other.radius_m + BIRTH_CLEARANCE_M
                );
            }
        }
        assert!(
            octants.into_iter().all(|count| count > 20),
            "octants {octants:?}"
        );
        assert!(small > 20 && large > 20, "small {small}, large {large}");
        assert!(
            debris
                .iter()
                .filter(|asteroid| asteroid.position[0] > 0.0)
                .count()
                > 150
        );
        assert!(
            debris
                .iter()
                .filter(|asteroid| asteroid.position[1].abs() > 450.0)
                .count()
                > 300
        );
        assert!(
            debris
                .iter()
                .filter(|asteroid| asteroid.position[2].abs() > 450.0)
                .count()
                > 300
        );
    }
}

#[test]
fn every_axis_and_sign_has_a_bounded_coherent_birth_velocity() {
    for axis in [StreamAxis::X, StreamAxis::Y, StreamAxis::Z] {
        for sign in [-1, 1] {
            let mut config = ExperimentSettings::default();
            config.debris.distribution = DebrisDistribution::DirectedStream;
            config.debris.count = 37;
            config.debris.stream_axis = axis;
            config.debris.stream_sign = sign;
            let sim = Simulation::with_experiment(3, config).unwrap();
            let mut common = [0.0; 3];
            common[axis.index()] = f64::from(sign) * config.debris.speed_mps;
            for asteroid in sim
                .state
                .assets
                .iter()
                .filter(|entity| entity.kind == EntityKind::Debris)
            {
                assert!(
                    norm(sub(asteroid.velocity, common)) <= config.debris.speed_spread_mps + 1e-10
                );
                assert!(
                    asteroid.velocity[axis.index()] * f64::from(sign)
                        >= config.debris.speed_mps - config.debris.speed_spread_mps
                );
                assert!(
                    asteroid
                        .position
                        .iter()
                        .all(|value| value.abs() < flow_half_extent_m(config.debris))
                );
            }
        }
    }
}

#[test]
fn interior_debris_motion_is_integrated_by_rapier_without_reapplying_the_stream_velocity() {
    let mut config = ExperimentSettings::default();
    config.debris.count = 17;
    config.debris.speed_spread_mps = 0.0;
    let mut sim = Simulation::with_experiment(31, config).unwrap();
    let id = EntityId::Debris(1);
    for asteroid in sim
        .state
        .assets
        .iter()
        .filter(|entity| entity.kind == EntityKind::Debris)
    {
        assert_eq!(asteroid.velocity, [20.0, 0.0, 0.0]);
    }
    let before = actor(&sim, id);
    let velocity = before.linvel();
    let position = before.translation();
    let gravity = crate::astrophysics::acceleration(position, velocity, settings().gravity);
    let dt = settings().integration.fixed_step_s;
    sim.advance(dt);
    let actual = actor(&sim, id);
    assert_vector_close(actual.linvel(), velocity + gravity * dt, 1e-9);
    assert_vector_close(
        actual.user_force(),
        gravity * actual.mass(),
        actual.mass() * 1e-10,
    );
    assert_ne!(actual.translation(), position);
    assert_ne!(actual.linvel(), velocity);
    let reported = sim
        .state
        .assets
        .iter()
        .find(|entity| entity.id == id)
        .unwrap();
    assert_eq!(reported.position, actual.translation().to_array());
    assert_eq!(reported.velocity, actual.linvel().to_array());
}

#[test]
fn physical_quantity_controls_real_bodies_and_public_sensor_rows_including_zero() {
    for count in [0, 1, 37, 1_000] {
        let mut config = ExperimentSettings::default();
        config.debris.count = count;
        let mut sim = Simulation::with_experiment(8, config).unwrap();
        assert_eq!(sim.state.assets.len(), count + 3);
        assert_eq!(sim.bodies.len(), count + 4);
        assert_eq!(sim.colliders.len(), count + 4);
        assert_eq!(sim.radar(usize::MAX).len(), count.min(200));
        let public = sim.observation();
        assert_eq!(public["geometry"]["debris_count"], count);
        assert_eq!(public["radar"].as_array().unwrap().len(), count.min(200));
        assert_eq!(
            public["experiment_settings"],
            serde_json::to_value(config).unwrap()
        );
        assert!(public.get("assets").is_none());
        for row in public["radar"].as_array().unwrap() {
            assert!(row.get("id").is_none());
            assert!(row.get("relative_velocity_mps").is_none());
        }
        sim.advance(settings().integration.fixed_step_s);
        validate(&sim.snapshot()).unwrap();
    }
}

#[test]
fn configured_mass_and_inertia_are_real_rapier_properties_and_drive_force_torque_and_fuel() {
    for (mass, inertia_scale) in [(100.0, 0.1), (8_123.0, 2.5), (100_000.0, 10.0)] {
        let mut config = ExperimentSettings::default();
        config.ship.mass_kg = mass;
        config.ship.inertia_scale = inertia_scale;
        config.debris.count = 0;
        let mut sim = Simulation::with_experiment(42, config).unwrap();
        let rb = actor(&sim, EntityId::Lander);
        assert!((rb.mass() - mass).abs() < mass * 1e-12);
        assert_eq!(rb.mass_properties().local_mprops.local_com, Vector::ZERO);
        assert!(rb.mass_properties().flags.is_empty());
        assert_matrix_close(sim.ship_inertia_kg_m2(), config.ship.inertia_kg_m2());
        assert_eq!(ship(&sim).max_thrust_n, local_max_thrust_for_mass_n(mass));
        let q = *rb.rotation();
        let before_velocity = rb.linvel();
        let gravity =
            crate::astrophysics::acceleration(rb.translation(), rb.linvel(), settings().gravity);
        let accel = [0.1, 0.0, 0.4];
        let angular = [0.01, -0.02, 0.03];
        sim.set_command(accel, angular).unwrap();
        assert_eq!(ship(&sim).thrust_n, scale(accel, mass));
        let inertia = config.ship.inertia_kg_m2();
        let expected_torque = [
            dot(inertia[0], angular),
            dot(inertia[1], angular),
            dot(inertia[2], angular),
        ];
        assert_eq!(ship(&sim).torque_nm, expected_torque);
        let dt = settings().integration.fixed_step_s;
        sim.advance(dt);
        let rb = actor(&sim, EntityId::Lander);
        assert_vector_close(
            rb.user_force(),
            (q * Vector::from_array(accel) + gravity) * rb.mass(),
            mass * 1e-10,
        );
        assert_vector_close(
            rb.user_torque(),
            q * Vector::from_array(expected_torque),
            mass * 1e-9,
        );
        assert_vector_close(
            (rb.linvel() - before_velocity) / dt,
            q * Vector::from_array(accel) + gravity,
            1e-7,
        );
        assert_vector_close(rb.angvel() / dt, q * Vector::from_array(angular), 1e-5);
        let expected_fuel = norm(accel) * rb.mass() * dt
            / (settings().lander.specific_impulse_s * settings().lander.standard_gravity_mps2);
        assert!((sim.state.mission.fuel_used_kg - expected_fuel).abs() < 1e-12);
        let public = sim.observation();
        assert_eq!(public["shuttle"]["inertia_frame"], "body");
        let reported: [[f64; 3]; 3] =
            serde_json::from_value(public["shuttle"]["inertia_kg_m2"].clone()).unwrap();
        assert_matrix_close(reported, sim.ship_inertia_kg_m2());
        assert_eq!(public["max_accel_mps2"], 0.5);
        assert_eq!(public["max_angular_accel_rad_s2"], 0.05);
        sim.set_command([10.0, 0.0, 0.0], [0.0, 10.0, 0.0]).unwrap();
        assert_eq!(ship(&sim).commanded_accel_mps2, [0.5, 0.0, 0.0]);
        assert_eq!(ship(&sim).commanded_angular_accel_rad_s2, [0.0, 0.05, 0.0]);
    }
}

// Establish contact geometry in the actual Rapier world, then use a real
// impulse to set the approach velocity. No contact/observation/damage outcome
// or physics report is injected by these tests.
fn place_contact_body(sim: &mut Simulation, id: EntityId, position: Vector, velocity: Vector) {
    let rb = &mut sim.bodies[sim.handles[&id]];
    rb.set_translation(position, true);
    rb.set_angvel(Vector::ZERO, true);
    rb.apply_impulse((velocity - rb.linvel()) * rb.mass(), true);
}

fn contact_experiment(count: usize) -> Simulation {
    let mut config = ExperimentSettings::default();
    config.debris.distribution = DebrisDistribution::DirectedStream;
    config.debris.count = count;
    config.debris.radius_min_m = 2.0;
    config.debris.radius_max_m = 2.0;
    Simulation::with_experiment(10, config).unwrap()
}

#[test]
fn debris_debris_contacts_change_both_actual_velocities() {
    let mut sim = contact_experiment(2);
    let a = EntityId::Debris(1);
    let b = EntityId::Debris(2);
    let center = Vector::new(crate::model::phobos_enclosing_radius_m() + 500.0, 0.0, 0.0);
    place_contact_body(&mut sim, a, center - Vector::X * 2.5, Vector::X * 5.0);
    place_contact_body(&mut sim, b, center + Vector::X * 2.5, -Vector::X * 5.0);
    let before_a = actor(&sim, a).linvel();
    let before_b = actor(&sim, b).linvel();
    let mut touched = false;
    for _ in 0..8 {
        sim.advance(settings().integration.fixed_step_s);
        touched |= sim.state.active_contacts.contains(&pair_key(a, b));
    }
    let after_a = actor(&sim, a).linvel();
    let after_b = actor(&sim, b).linvel();
    assert!(
        touched,
        "the real debris colliders must contact one another"
    );
    assert!(sim.state.collisions > 0);
    assert!(after_a.x < before_a.x - 4.0);
    assert!(after_b.x > before_b.x + 4.0);
    assert!(after_a.x < after_b.x);
    assert!(((after_a + after_b) - (before_a + before_b)).length() < 0.02);
    for id in [a, b] {
        let reported = sim
            .state
            .assets
            .iter()
            .find(|entity| entity.id == id)
            .unwrap();
        assert_eq!(reported.velocity, actor(&sim, id).linvel().to_array());
        assert_eq!(reported.position, actor(&sim, id).translation().to_array());
    }
}

#[test]
fn debris_hits_the_shared_phobos_terrain_and_receives_a_real_contact_response() {
    let mut sim = contact_experiment(1);
    let id = EntityId::Debris(1);
    let terrain_radius = crate::terrain::surface_radius_m(bevy::math::DVec3::X);
    place_contact_body(
        &mut sim,
        id,
        Vector::new(terrain_radius + 7.0, 0.0, 0.0),
        -Vector::X * 10.0,
    );
    let fixed_pose = *actor(&sim, EntityId::Phobos).position();
    let mut touched = false;
    for _ in 0..25 {
        sim.advance(settings().integration.fixed_step_s);
        touched |= sim
            .state
            .active_contacts
            .contains(&pair_key(id, EntityId::Phobos));
    }
    assert!(
        touched,
        "the same Phobos triangle mesh used for rendering must produce the contact"
    );
    assert!(actor(&sim, id).linvel().x > -1.0);
    assert_eq!(*actor(&sim, EntityId::Phobos).position(), fixed_pose);
    let position = actor(&sim, id).translation();
    assert!(
        position.length()
            > crate::terrain::surface_radius_m(bevy::math::DVec3::from_array(position.to_array()))
    );
    assert!(sim.state.collisions > 0);
}

#[test]
fn an_asteroid_can_hit_the_actual_lander_compound_collider() {
    let mut sim = contact_experiment(1);
    let id = EntityId::Debris(1);
    let rb = actor(&sim, EntityId::Lander);
    let up = *rb.rotation() * Vector::Z;
    let position = rb.translation() + up * 10.0;
    place_contact_body(&mut sim, id, position, -up * 20.0);
    let mut touched = false;
    for _ in 0..20 {
        sim.advance(settings().integration.fixed_step_s);
        touched |= sim
            .state
            .active_contacts
            .contains(&pair_key(id, EntityId::Lander));
        if touched {
            break;
        }
    }
    assert!(touched);
    assert!(sim.state.mission.damage > 0.0);
    assert!(actor(&sim, EntityId::Lander).linvel().length() > 0.0);
    assert_eq!(
        ship(&sim).velocity,
        actor(&sim, EntityId::Lander).linvel().to_array()
    );
}

#[test]
fn configure_is_an_explicit_new_run_and_reset_repeats_the_active_trial() {
    let mut config = ExperimentSettings::default();
    config.ship.mass_kg = 8_123.0;
    config.ship.inertia_scale = 2.5;
    config.debris.count = 37;
    let mut sim = Simulation::new_default();
    sim.set_manual_thrust([0.0, 0.3, 0.4]).unwrap();
    sim.advance(0.5);
    sim.debug_upright().unwrap();
    let command: PhysicsCommand = serde_json::from_value(json!({
        "op": "configure_experiment", "settings": config, "seed": 42,
    }))
    .unwrap();
    let configured = sim.execute(command).unwrap();
    assert_eq!(configured.experiment_settings, Some(config));
    assert_eq!(configured.seed, 42);
    assert_eq!(configured.time_s, 0.0);
    assert_eq!(configured.manual_interventions, 0);
    assert!(configured.model.contains("local_experiment_v1"));
    assert!(configured.model.contains("custom settings"));
    assert_eq!(actor(&sim, EntityId::Lander).user_force(), Vector::ZERO);
    assert_eq!(actor(&sim, EntityId::Lander).user_torque(), Vector::ZERO);
    assert_eq!(ship(&sim).thrust_n, [0.0; 3]);
    assert_eq!(ship(&sim).torque_nm, [0.0; 3]);
    let pristine = serde_json::to_value(configured).unwrap();
    sim.set_manual_thrust([0.0, 0.3, 0.4]).unwrap();
    sim.advance(0.5);
    sim.debug_upright().unwrap();
    let exported = serde_json::to_value(sim.snapshot()).unwrap();
    let restored: Snapshot = serde_json::from_value(exported.clone()).unwrap();
    sim.execute(PhysicsCommand::ConfigureExperiment {
        settings: ExperimentSettings::default(),
        seed: Some(7),
    })
    .unwrap();
    sim.execute(PhysicsCommand::Restore {
        state: Box::new(restored),
    })
    .unwrap();
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), exported);
    assert_matrix_close(sim.ship_inertia_kg_m2(), config.ship.inertia_kg_m2());
    sim.execute(PhysicsCommand::Reset { seed: None }).unwrap();
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), pristine);
    assert_eq!(actor(&sim, EntityId::Lander).user_force(), Vector::ZERO);
    assert_eq!(actor(&sim, EntityId::Lander).user_torque(), Vector::ZERO);
}

fn move_to_downstream_exit(sim: &mut Simulation, id: EntityId) {
    let config = sim.state.experiment_settings.unwrap().debris;
    let axis = config.stream_axis.index();
    let mut position = [0.0; 3];
    let mut velocity = [0.0; 3];
    position[axis] = f64::from(config.stream_sign) * (flow_half_extent_m(config) - 0.1);
    velocity[axis] = f64::from(config.stream_sign) * 20.0;
    place_contact_body(
        sim,
        id,
        Vector::from_array(position),
        Vector::from_array(velocity),
    );
}

#[test]
fn outflow_recycles_only_at_the_inlet_and_persists_rng_between_sensor_ticks() {
    let mut sim = contact_experiment(3);
    let id = EntityId::Debris(1);
    let previous_rng = sim.state.rng_state;
    let config = sim.state.experiment_settings.unwrap();
    move_to_downstream_exit(&mut sim, id);
    sim.advance(settings().integration.fixed_step_s);
    assert_ne!(sim.state.rng_state, previous_rng);
    assert_eq!(sim.state.rng_state, sim.rng);
    assert!(
        sim.state
            .observations
            .iter()
            .all(|row| row.measurement_time_s == 0.0)
    );
    let born = sim
        .state
        .assets
        .iter()
        .find(|entity| entity.id == id)
        .unwrap();
    let expected_plane = -f64::from(config.debris.stream_sign)
        * (flow_half_extent_m(config.debris) - born.radius_m - BIRTH_CLEARANCE_M);
    assert_eq!(
        born.position[config.debris.stream_axis.index()],
        expected_plane
    );
    assert_eq!(born.position, actor(&sim, id).translation().to_array());
    let mut common = [0.0; 3];
    common[config.debris.stream_axis.index()] =
        f64::from(config.debris.stream_sign) * config.debris.speed_mps;
    assert!(norm(sub(born.velocity, common)) <= config.debris.speed_spread_mps + 1e-10);
    let exported = serde_json::to_value(sim.snapshot()).unwrap();
    assert_eq!(exported["rng_state"], sim.rng.to_string());
    let mut restored = Simulation::new_default();
    restored
        .execute(PhysicsCommand::Restore {
            state: Box::new(serde_json::from_value(exported.clone()).unwrap()),
        })
        .unwrap();
    assert_eq!(serde_json::to_value(restored.snapshot()).unwrap(), exported);
    for world in [&mut sim, &mut restored] {
        move_to_downstream_exit(world, EntityId::Debris(2));
        world.advance(settings().integration.fixed_step_s);
    }
    assert_eq!(sim.rng, restored.rng);
    assert_eq!(sim.state.rng_state, restored.state.rng_state);
    for field in [EntityId::Debris(1), EntityId::Debris(2)] {
        let expected = sim
            .state
            .assets
            .iter()
            .find(|entity| entity.id == field)
            .unwrap();
        let actual = restored
            .state
            .assets
            .iter()
            .find(|entity| entity.id == field)
            .unwrap();
        assert_eq!(actual.position, expected.position);
        assert_eq!(actual.velocity, expected.velocity);
        assert_eq!(actual.radius_m, expected.radius_m);
        assert_eq!(actual.mass_kg, expected.mass_kg);
    }
    validate(&restored.snapshot()).unwrap();
}

#[test]
fn an_outside_population_has_bounded_recycle_work_without_losing_physical_bodies() {
    let mut sim = contact_experiment(9);
    let config = sim.state.experiment_settings.unwrap().debris;
    let extent = flow_half_extent_m(config);
    for index in 1..=9 {
        place_contact_body(
            &mut sim,
            EntityId::Debris(index),
            Vector::new(f64::from(index) * 30.0, -extent + 0.1, 0.0),
            -Vector::Y * 20.0,
        );
    }
    for expected_outside in [5, 1, 0] {
        sim.advance(settings().integration.fixed_step_s);
        let outside = sim
            .state
            .assets
            .iter()
            .filter(|entity| {
                entity.kind == EntityKind::Debris
                    && entity.position.iter().any(|value| value.abs() > extent)
            })
            .count();
        assert_eq!(outside, expected_outside);
        assert_eq!(sim.state.assets.len(), 12);
        assert_eq!(sim.bodies.len(), 13);
        assert_eq!(sim.state.rng_state, sim.rng);
        validate(&sim.snapshot()).unwrap();
    }
}

#[test]
fn bounded_configuration_rejects_invalid_values_atomically_including_real_forces() {
    let base = ExperimentSettings::default();
    let mut invalid = Vec::new();
    for value in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        for field in 0..8 {
            let mut config = base;
            match field {
                0 => config.ship.mass_kg = value,
                1 => config.ship.inertia_scale = value,
                2 => config.debris.radius_min_m = value,
                3 => config.debris.radius_max_m = value,
                4 => config.debris.speed_mps = value,
                5 => config.debris.speed_spread_mps = value,
                6 => config.debris.shell_inner_m = value,
                7 => config.debris.shell_outer_m = value,
                _ => unreachable!(),
            }
            invalid.push(config);
        }
    }
    for (field, value) in [
        (0, 99.0),
        (0, 100_001.0),
        (1, 0.099),
        (1, 10.001),
        (2, 0.049),
        (2, 41.0),
        (3, 0.3),
        (3, 100.001),
        (4, 0.0),
        (4, 200.001),
        (5, -0.1),
        (5, 10.001),
        (6, -0.1),
        (6, base.debris.shell_outer_m - 9.0),
        (7, 50_000.1),
        (7, base.debris.shell_inner_m),
    ] {
        let mut config = base;
        match field {
            0 => config.ship.mass_kg = value,
            1 => config.ship.inertia_scale = value,
            2 => config.debris.radius_min_m = value,
            3 => config.debris.radius_max_m = value,
            4 => config.debris.speed_mps = value,
            5 => config.debris.speed_spread_mps = value,
            6 => config.debris.shell_inner_m = value,
            7 => config.debris.shell_outer_m = value,
            _ => unreachable!(),
        }
        invalid.push(config);
    }
    let mut config = base;
    config.debris.count = crate::model::MAX_DEBRIS_COUNT + 1;
    invalid.push(config);
    for sign in [-2, 0, 2] {
        let mut config = base;
        config.debris.stream_sign = sign;
        invalid.push(config);
    }
    let mut sim = Simulation::new_default();
    sim.set_manual_thrust([0.0, 0.3, 0.4]).unwrap();
    sim.advance(settings().integration.fixed_step_s);
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    let force = actor(&sim, EntityId::Lander).user_force();
    let torque = actor(&sim, EntityId::Lander).user_torque();
    let pose = *actor(&sim, EntityId::Lander).position();
    for settings in invalid {
        assert!(
            sim.execute(PhysicsCommand::ConfigureExperiment {
                settings,
                seed: Some(42)
            })
            .is_err()
        );
        assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
        assert_eq!(actor(&sim, EntityId::Lander).user_force(), force);
        assert_eq!(actor(&sim, EntityId::Lander).user_torque(), torque);
        assert_eq!(*actor(&sim, EntityId::Lander).position(), pose);
    }
    for command in [
        PhysicsCommand::ConfigureExperiment {
            settings: base,
            seed: Some(u32::MAX as u64 + 1),
        },
        PhysicsCommand::Reset {
            seed: Some(u32::MAX as u64 + 1),
        },
    ] {
        assert!(sim.execute(command).is_err());
        assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
    }
}

#[test]
fn restoring_uses_the_stored_profile_and_rejects_unbounded_or_mismatched_physics_atomically() {
    let mut config = ExperimentSettings::default();
    config.ship.mass_kg = 8_123.0;
    config.ship.inertia_scale = 2.5;
    config.debris.count = 37;
    let mut sim = Simulation::with_experiment(42, config).unwrap();
    sim.set_manual_thrust([0.0, 0.3, 0.4]).unwrap();
    let valid = sim.snapshot();
    validate(&valid).unwrap();
    let before = serde_json::to_value(&valid).unwrap();
    let mut invalid_states = Vec::new();
    let mut invalid = valid.clone();
    invalid.experiment_settings.as_mut().unwrap().ship.mass_kg = f64::NAN;
    invalid_states.push(invalid);
    let mut invalid = valid.clone();
    invalid.experiment_settings.as_mut().unwrap().debris.count = crate::model::MAX_DEBRIS_COUNT + 1;
    invalid_states.push(invalid);
    let mut invalid = valid.clone();
    invalid
        .assets
        .iter_mut()
        .find(|entity| entity.id == EntityId::Lander)
        .unwrap()
        .mass_kg += 1.0;
    invalid_states.push(invalid);
    let mut invalid = valid.clone();
    invalid
        .assets
        .iter_mut()
        .find(|entity| entity.kind == EntityKind::Debris)
        .unwrap()
        .radius_m += 0.1;
    invalid_states.push(invalid);
    let mut invalid = valid.clone();
    invalid.rng_state = 0;
    invalid_states.push(invalid);
    let mut invalid = valid.clone();
    invalid.active_contacts = vec![[EntityId::Lander, EntityId::LandingPad]; 2];
    invalid_states.push(invalid);
    let mut invalid = valid;
    invalid
        .assets
        .iter_mut()
        .find(|entity| entity.id == EntityId::Lander)
        .unwrap()
        .max_thrust_n = local_max_thrust_n();
    invalid_states.push(invalid);
    for invalid in invalid_states {
        assert!(
            sim.execute(PhysicsCommand::Restore {
                state: Box::new(invalid)
            })
            .is_err()
        );
        assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
        assert_matrix_close(sim.ship_inertia_kg_m2(), config.ship.inertia_kg_m2());
    }
}

#[test]
fn legacy_none_profile_keeps_original_corridor_mass_tensor_and_rng_contract() {
    let seed = 100;
    let mut legacy = Simulation::with_profile(seed, None).unwrap();
    assert_eq!(legacy.state.model, settings().description);
    assert_eq!(legacy.state.experiment_settings, None);
    assert_eq!(ship(&legacy).mass_kg, settings().lander.mass_kg);
    assert_matrix_close(legacy.ship_inertia_kg_m2(), settings().lander.inertia_kg_m2);
    let cfg = settings().debris;
    for asteroid in legacy
        .state
        .assets
        .iter()
        .filter(|entity| entity.kind == EntityKind::Debris)
    {
        assert!((-cfg.x_min_m - cfg.x_span_m..=-cfg.x_min_m).contains(&asteroid.position[0]));
        assert!(asteroid.position[1].abs() <= cfg.y_half_width_m);
        assert!(asteroid.position[2].abs() <= cfg.z_half_width_m);
    }
    let mut expected_rng = seed.max(1);
    for _ in 0..cfg.count * 18 + cfg.radar_count * 12 {
        rand(&mut expected_rng);
    }
    assert_eq!(legacy.rng, expected_rng);
    let mut exported = serde_json::to_value(legacy.snapshot()).unwrap();
    exported
        .as_object_mut()
        .unwrap()
        .remove("experiment_settings");
    let old: Snapshot = serde_json::from_value(exported).unwrap();
    assert_eq!(old.experiment_settings, None);
    validate(&old).unwrap();
    let before = serde_json::to_value(legacy.snapshot()).unwrap();
    legacy
        .execute(PhysicsCommand::Restore {
            state: Box::new(old.clone()),
        })
        .unwrap();
    assert_eq!(serde_json::to_value(legacy.snapshot()).unwrap(), before);
    assert!(legacy.observation()["experiment_settings"].is_null());
    let mut mass_cheat = old.clone();
    mass_cheat
        .assets
        .iter_mut()
        .find(|entity| entity.id == EntityId::Lander)
        .unwrap()
        .mass_kg += 1.0;
    assert!(validate(&mass_cheat).is_err());
    let mut radius_cheat = old.clone();
    radius_cheat
        .assets
        .iter_mut()
        .find(|entity| entity.kind == EntityKind::Debris)
        .unwrap()
        .radius_m += 0.1;
    assert!(validate(&radius_cheat).is_err());
    let mut relabeled = old;
    relabeled.experiment_settings = Some(ExperimentSettings::default());
    relabeled.model = profile_description(relabeled.experiment_settings);
    assert!(validate(&relabeled).is_err());
    let mut stream_as_legacy = Simulation::new_default().snapshot();
    stream_as_legacy.experiment_settings = None;
    stream_as_legacy.model = profile_description(None);
    assert!(validate(&stream_as_legacy).is_err());
    legacy.set_manual_thrust([0.0, 0.3, 0.4]).unwrap();
    legacy.advance(0.5);
    legacy
        .execute(PhysicsCommand::Reset { seed: None })
        .unwrap();
    assert_eq!(serde_json::to_value(legacy.snapshot()).unwrap(), before);
}
