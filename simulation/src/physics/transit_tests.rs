use super::*;
use crate::model::{DebrisTrajectoryAdjustment, StreamAxis};

fn config(count: usize) -> ExperimentSettings {
    let mut config = ExperimentSettings::default();
    assert_eq!(
        config.debris.distribution,
        DebrisDistribution::TransitStream
    );
    config.debris.count = count;
    config.debris.radius_min_m = 0.5;
    config.debris.radius_max_m = 1.0;
    config
}

fn actual(sim: &Simulation, id: EntityId) -> &RigidBody {
    &sim.bodies[sim.handles[&id]]
}

#[test]
fn new_default_populates_the_whole_canonical_route_and_real_cohorts_move_across_it() {
    let mut sim = Simulation::new_default();
    let config = sim.state.experiment_settings.unwrap();
    assert_eq!(sim.flight_preset(), FlightPreset::FarSideL2);
    assert_eq!(
        config.debris.distribution,
        DebrisDistribution::TransitStream
    );
    assert_eq!(config.debris.count, 500);
    assert_eq!(config.debris.stream_axis, StreamAxis::Y);
    assert_eq!(config.debris.stream_sign, -1);
    assert_eq!(config.debris.speed_mps, 20.0);
    assert_eq!(config.debris.speed_spread_mps, 4.0);
    assert_eq!(config.debris.radius_min_m, 0.4);
    assert_eq!(config.debris.radius_max_m, 40.4);
    assert!(config.debris.trajectory_adjustment.is_off());
    let volume = TransitVolume::for_preset(FlightPreset::FarSideL2);
    let length = volume.maximum[0] - settings().transit_stream.target_lead_m;
    let mut bins = [0_usize; 4];
    let mut beyond_shell = 0;
    let mut cohort = Vec::new();
    let initial_clearance = spawn_index(&sim.state.assets, config.debris.radius_max_m);
    for rock in sim
        .state
        .assets
        .iter()
        .filter(|rock| rock.kind == EntityKind::Debris)
    {
        assert!(!volume.has_exited(rock.position, rock.radius_m));
        let coordinates = volume.coordinates(rock.position);
        let along = coordinates[0];
        let bin = ((along.max(0.0) / length * 4.0) as usize).min(3);
        bins[bin] += 1;
        let radial = norm(rock.position);
        let terrain =
            crate::terrain::surface_radius_m(bevy::math::DVec3::from_array(rock.position));
        beyond_shell += usize::from(radial - terrain - rock.radius_m > config.debris.shell_outer_m);
        assert!(
            phobos_shape().distance_to_local_point(Vector::from_array(rock.position), false)
                > rock.radius_m + BIRTH_CLEARANCE_M
        );
        assert!(initial_clearance.is_clear(
            rock.position,
            rock.radius_m,
            BIRTH_CLEARANCE_M,
            Some(rock.id)
        ));
        let body = actual(&sim, rock.id);
        assert!(body.is_dynamic() && body.is_ccd_enabled());
        // Keep this cohort away from every exit, especially the -Y outflow.
        // A replenished rock must never count as ordinary crossflow motion.
        if along > length * 0.2
            && along < length * 0.8
            && coordinates[1] > volume.minimum[1] + 200.0
            && coordinates[1] < volume.maximum[1] - 50.0
            && coordinates[2] > volume.minimum[2] + 50.0
            && coordinates[2] < volume.maximum[2] - 50.0
        {
            cohort.push((rock.id, rock.radius_m, body.translation(), *body.rotation()));
        }
    }
    assert!(
        bins.into_iter().all(|count| count > 50),
        "route bins {bins:?}"
    );
    assert!(
        beyond_shell > 250,
        "the route must not end at the old shell: {beyond_shell}"
    );
    assert!(cohort.len() > 100);
    let report = sim.debris_dynamics().unwrap();
    assert_eq!(report.physical_count, 500);
    assert!(report.mean_velocity_world_mps[1] < -19.0);
    let mean = Vector::from_array(report.mean_velocity_world_mps);
    assert!(mean.dot(volume.basis[1]) < -19.0);
    assert!(mean.dot(volume.basis[0]).abs() < 1.0);
    let handles = sim.handles.clone();
    let colliders = sim.collider_ids.clone();
    let mut previous: Vec<_> = cohort.iter().map(|(_, _, position, _)| *position).collect();
    let dt = settings().integration.fixed_step_s;
    for _ in 0..100 {
        sim.advance(dt);
        for ((id, radius, _, _), previous_position) in cohort.iter().zip(&mut previous) {
            let position = actual(&sim, *id).translation();
            assert!(
                (position - *previous_position).length() < 5.0,
                "cohort rock {id:?} must move continuously, not recycle"
            );
            assert!(!volume.has_exited(position.to_array(), *radius));
            *previous_position = position;
        }
    }
    assert!((sim.state.time_s - 5.0).abs() < 1e-10);
    let moved = cohort
        .iter()
        .filter(|(id, _, position, rotation)| {
            let body = actual(&sim, *id);
            let displacement = body.translation() - *position;
            displacement.y < -65.0
                && displacement.dot(volume.basis[0]).abs() < 25.0
                && *body.rotation() != *rotation
        })
        .count();
    assert!(
        moved > cohort.len() * 9 / 10,
        "real moving cohort {moved}/{}",
        cohort.len()
    );
    assert_eq!(sim.handles, handles);
    assert_eq!(sim.collider_ids, colliders);
    assert_eq!(sim.state.assets.len(), 503);
    assert_eq!(sim.bodies.len(), 504);
    assert_eq!(sim.colliders.len(), 504);
    assert_eq!(sim.debris_dynamics().unwrap().physical_count, 500);
    assert!(sim.debris_dynamics().unwrap().mean_velocity_world_mps[1] < -15.0);
    validate(&sim.snapshot()).unwrap();
    let archive = serde_json::to_value(sim.snapshot()).unwrap();
    let mut restored = Simulation::new_default();
    restored
        .execute(PhysicsCommand::Restore {
            state: Box::new(serde_json::from_value(archive.clone()).unwrap()),
        })
        .unwrap();
    assert_eq!(serde_json::to_value(restored.snapshot()).unwrap(), archive);
    assert_eq!(restored.rng, sim.rng);
    assert_eq!(restored.debris_dynamics().unwrap().physical_count, 500);
    for rock in sim
        .state
        .assets
        .iter()
        .filter(|rock| rock.kind == EntityKind::Debris)
    {
        let original = actual(&sim, rock.id);
        let after = actual(&restored, rock.id);
        assert_eq!(after.translation(), original.translation());
        assert_eq!(after.linvel(), original.linvel());
        assert_eq!(after.angvel(), original.angvel());
        assert!((*after.rotation() - *original.rotation()).length() < 1e-12);
    }
}

#[test]
fn both_presets_and_every_axis_keep_route_volume_births_and_declared_world_flow() {
    let mut sim = Simulation::with_experiment(1, config(0)).unwrap();
    for preset in [FlightPreset::MarsFacingL1, FlightPreset::FarSideL2] {
        for axis in [StreamAxis::X, StreamAxis::Y, StreamAxis::Z] {
            for sign in [-1, 1] {
                let mut config = config(37);
                config.debris.stream_axis = axis;
                config.debris.stream_sign = sign;
                sim.configure_flight(config, Some(42), preset).unwrap();
                let volume = TransitVolume::for_preset(preset);
                let common =
                    Vector::from_array(DebrisFlowTuning::from(config.debris).common_velocity_mps());
                for rock in sim
                    .state
                    .assets
                    .iter()
                    .filter(|rock| rock.kind == EntityKind::Debris)
                {
                    assert!(!volume.has_exited(rock.position, rock.radius_m));
                    assert!(
                        (actual(&sim, rock.id).linvel() - common).length()
                            <= config.debris.speed_spread_mps + 1e-10
                    );
                    assert_eq!(
                        rock.position,
                        actual(&sim, rock.id).translation().to_array()
                    );
                }
                let index = spawn_index(&sim.state.assets, config.debris.radius_max_m);
                let mut rng = sim.rng;
                let inlet = debris_birth_position(
                    &mut rng,
                    config.debris,
                    2.0,
                    &index,
                    None,
                    DebrisBirth::TransitInlet(volume),
                )
                .unwrap();
                let coordinate = volume.coordinates(inlet);
                let projection = volume.basis.map(|basis| basis.dot(common));
                let face = (0..3)
                    .max_by(|&a, &b| projection[a].abs().total_cmp(&projection[b].abs()))
                    .unwrap();
                let expected = if projection[face] > 0.0 {
                    volume.minimum[face] + 3.0
                } else {
                    volume.maximum[face] - 3.0
                };
                let mut plane_coordinate = coordinate;
                plane_coordinate[face] = expected;
                let plane = volume.world_position(plane_coordinate);
                let radial = bevy::math::DVec3::from_array(plane);
                if radial.length() <= crate::terrain::surface_radius_m(radial.normalize()) {
                    assert!((coordinate[face] - expected) * projection[face] > 0.0);
                } else {
                    assert!((coordinate[face] - expected).abs() < 1e-9);
                }
                assert!(volume.admits_birth(inlet, 2.0));
                assert!(
                    phobos_shape().distance_to_local_point(Vector::from_array(inlet), false)
                        > 2.0 + BIRTH_CLEARANCE_M
                );
                assert!(index.is_clear(inlet, 2.0, BIRTH_CLEARANCE_M, None));
                validate(&sim.snapshot()).unwrap();
            }
        }
    }
}

#[test]
fn natural_transverse_outflow_recycles_only_after_complete_exit_and_keeps_safe_real_ids() {
    let mut config = config(9);
    config.debris.stream_axis = StreamAxis::Y;
    config.debris.stream_sign = 1;
    config.debris.speed_mps = 200.0;
    config.debris.speed_spread_mps = 0.0;
    let mut sim = Simulation::with_experiment(100, config).unwrap();
    let volume = TransitVolume::for_preset(sim.flight_preset());
    let handles = sim.handles.clone();
    let mut births = 0;
    let dt = settings().integration.fixed_step_s;
    for _ in 0..120 {
        let before: Vec<_> = sim
            .state
            .assets
            .iter()
            .filter(|rock| rock.kind == EntityKind::Debris)
            .map(|rock| {
                (
                    rock.id,
                    rock.radius_m,
                    actual(&sim, rock.id).translation(),
                    actual(&sim, rock.id).linvel(),
                )
            })
            .collect();
        sim.advance(dt);
        let mut step_births = 0;
        for (id, radius, position, velocity) in before {
            let body = actual(&sim, id);
            if (body.translation() - position).length() < 100.0 {
                continue;
            }
            step_births += 1;
            births += 1;
            let predicted = (position + velocity * dt).to_array();
            assert!(
                volume.has_exited(predicted, radius),
                "no recycling before a real exit"
            );
            let coordinate = volume.coordinates(body.translation().to_array());
            assert!((coordinate[1] - volume.minimum[1] - radius - BIRTH_CLEARANCE_M).abs() < 1e-8);
            assert!((body.linvel() - Vector::Y * 200.0).length() < 1e-10);
            assert!(
                spawn_index(&sim.state.assets, config.debris.radius_max_m).is_clear(
                    body.translation().to_array(),
                    radius,
                    BIRTH_CLEARANCE_M,
                    Some(id)
                )
            );
            assert!(
                phobos_shape().distance_to_local_point(body.translation(), false)
                    > radius + BIRTH_CLEARANCE_M
            );
        }
        assert!(step_births <= MAX_RECYCLES_PER_STEP);
        assert_eq!(sim.handles, handles);
        assert_eq!(sim.bodies.len(), 13);
        assert_eq!(sim.state.rng_state, sim.rng);
    }
    assert!(
        births >= 9,
        "all natural transverse traffic must replenish: {births}"
    );
    validate(&sim.snapshot()).unwrap();
}

#[test]
fn transit_recycling_and_partial_sensor_archives_repeat_the_same_birth_rng_and_rigid_bodies() {
    let mut config = config(7);
    config.debris.trajectory_adjustment = DebrisTrajectoryAdjustment::TowardLander;
    config.debris.stream_axis = StreamAxis::Z;
    config.debris.stream_sign = -1;
    config.debris.speed_mps = 200.0;
    let mut source = Simulation::with_experiment(42, config).unwrap();
    source.place_lander([16_000.0, 8_000.0, 9_000.0]).unwrap();
    source.advance(0.05);
    let archive = serde_json::to_value(source.snapshot()).unwrap();
    let mut restored = Simulation::with_experiment(1, config).unwrap();
    restored
        .execute(PhysicsCommand::Restore {
            state: Box::new(serde_json::from_value(archive.clone()).unwrap()),
        })
        .unwrap();
    assert_eq!(serde_json::to_value(restored.snapshot()).unwrap(), archive);
    for _ in 0..120 {
        source.advance(0.05);
        restored.advance(0.05);
        assert_eq!(
            serde_json::to_value(restored.snapshot()).unwrap(),
            serde_json::to_value(source.snapshot()).unwrap()
        );
    }
    assert_eq!(source.rng, restored.rng);
}

#[test]
fn moved_pad_and_reset_never_redefine_canonical_transit_births_or_archive_identity() {
    let config = config(17);
    let mut sim = Simulation::with_experiment(42, config).unwrap();
    let canonical = sim.snapshot();
    sim.move_launchpad([0.3, 0.5, 0.8]).unwrap();
    sim.reset(42).unwrap();
    for original in canonical
        .assets
        .iter()
        .filter(|rock| rock.kind == EntityKind::Debris)
    {
        let after = sim
            .state
            .assets
            .iter()
            .find(|rock| rock.id == original.id)
            .unwrap();
        assert_eq!(
            serde_json::to_value(after).unwrap(),
            serde_json::to_value(original).unwrap()
        );
    }
    assert_eq!(sim.rng, canonical.rng_state);
    validate(&sim.snapshot()).unwrap();
    let archive = serde_json::to_value(sim.snapshot()).unwrap();
    sim.execute(PhysicsCommand::Restore {
        state: Box::new(serde_json::from_value(archive.clone()).unwrap()),
    })
    .unwrap();
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), archive);
}

#[test]
fn blocked_transit_inlet_and_overcrowded_config_are_bounded_and_never_fake_clearance() {
    let config = config(0);
    let volume = TransitVolume::for_preset(FlightPreset::FarSideL2);
    let mut index = SpawnIndex::new(100_000.0);
    index.insert(
        EntityId::ReturnShip,
        volume.world_position([0.0; 3]),
        100_000.0,
    );
    let mut rng = 42;
    let mut expected_rng = rng;
    for _ in 0..MAX_BIRTH_ATTEMPTS * 2 {
        uniform(&mut expected_rng);
    }
    assert!(
        debris_birth_position(
            &mut rng,
            config.debris,
            2.0,
            &index,
            None,
            DebrisBirth::TransitInlet(volume)
        )
        .is_none()
    );
    assert_eq!(rng, expected_rng);
    let mut sim = Simulation::with_experiment(42, config).unwrap();
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    let handles = sim.handles.clone();
    let mut impossible = config;
    impossible.debris.count = crate::model::MAX_DEBRIS_COUNT;
    impossible.debris.radius_min_m = 100.0;
    impossible.debris.radius_max_m = 100.0;
    assert!(
        sim.configure_flight(impossible, Some(42), FlightPreset::FarSideL2)
            .is_err()
    );
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
    assert_eq!(sim.handles, handles);
}
