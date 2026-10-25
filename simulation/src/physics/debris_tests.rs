use super::*;
use crate::model::ImpactEjectaSettings;

fn impact_config(count: usize) -> ExperimentSettings {
    let mut experiment = ExperimentSettings::default();
    experiment.debris.count = count;
    experiment.debris.radius_min_m = 0.5;
    experiment.debris.radius_max_m = 1.5;
    experiment.debris.speed_spread_mps = 0.1;
    experiment.debris.distribution = DebrisDistribution::impact_default();
    experiment
}

fn body(sim: &Simulation, id: EntityId) -> &RigidBody {
    &sim.bodies[sim.handles[&id]]
}

#[test]
fn impact_initial_conditions_are_a_real_seeded_patch_with_angular_momentum_not_a_forced_orbit() {
    let config = impact_config(37);
    let DebrisDistribution::ImpactEjecta { settings: impact } = config.debris.distribution else {
        unreachable!()
    };
    let mut sim = Simulation::with_experiment(100, config).unwrap();
    let repeated = Simulation::with_experiment(100, config).unwrap();
    assert_eq!(
        serde_json::to_value(sim.snapshot()).unwrap(),
        serde_json::to_value(repeated.snapshot()).unwrap()
    );
    let center = debris::unit_direction(impact.source_direction);
    let initial = sim.snapshot();
    for asteroid in sim
        .state
        .assets
        .iter()
        .filter(|asset| asset.kind == EntityKind::Debris)
    {
        let radial = Vector::from_array(asteroid.position).normalize();
        assert!(radial.dot(center) >= impact.patch_half_angle_deg.to_radians().cos() - 1e-12);
        let altitude = norm(asteroid.position)
            - crate::terrain::surface_radius_m(bevy::math::DVec3::from_array(asteroid.position))
            - asteroid.radius_m;
        assert!(altitude >= config.debris.shell_inner_m - 1e-8);
        assert!(altitude <= config.debris.shell_outer_m + 1e-8);
        let actual = body(&sim, asteroid.id);
        assert!(actual.is_dynamic() && actual.is_ccd_enabled());
        assert!((actual.mass() - asteroid.mass_kg).abs() <= asteroid.mass_kg * 1e-12);
        assert_eq!(actual.linvel().to_array(), asteroid.velocity);
        assert!(actual.translation().cross(actual.linvel()).length() > 10_000.0);
        assert!(actual.angvel().length() > 0.0);
    }
    let id = EntityId::Debris(1);
    let before_position = body(&sim, id).translation();
    let before_velocity = body(&sim, id).linvel();
    let before_rotation = *body(&sim, id).rotation();
    let gravity =
        crate::astrophysics::acceleration(before_position, before_velocity, settings().gravity);
    let dt = settings().integration.fixed_step_s;
    sim.advance(dt);
    assert!((body(&sim, id).linvel() - before_velocity - gravity * dt).length() < 1e-9);
    assert_ne!(body(&sim, id).translation(), before_position);
    assert_ne!(*body(&sim, id).rotation(), before_rotation);
    assert_ne!(body(&sim, id).linvel(), before_velocity);
    assert_eq!(
        sim.state.rng_state, initial.rng_state,
        "no impact inlet births or off-cadence sensor draws"
    );
    let public = sim.observation();
    assert_eq!(
        public["debris_distribution"],
        json!(config.debris.distribution)
    );
    assert_eq!(public["debris_births_enabled"], false);
    assert_eq!(public["debris_flow_active"], false);
    assert!(public.get("debris_dynamics").is_none());
    validate(&sim.snapshot()).unwrap();
}

#[test]
fn impact_tuning_rejection_is_atomic_ship_tuning_still_works_and_escaping_rocks_are_not_recycled() {
    let mut sim = Simulation::with_experiment(100, impact_config(3)).unwrap();
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    let rng = sim.rng;
    let handles = sim.handles.clone();
    assert!(
        sim.tune_debris_flow(DebrisFlowTuning {
            speed_mps: 76.7,
            ..sim.debris_flow()
        })
        .is_err()
    );
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
    assert_eq!(sim.rng, rng);
    assert_eq!(sim.handles, handles);
    sim.tune_ship(ShipTuning {
        mass_kg: 100.0,
        inertia_scale: 2.0,
        thrust_scale: 3.2,
    })
    .unwrap();
    assert!((body(&sim, EntityId::Lander).mass() - 100.0).abs() < 1e-10);
    let id = EntityId::Debris(1);
    let handle = sim.handles[&id];
    let config = sim.state.experiment_settings.unwrap().debris;
    let position = Vector::new(flow_half_extent_m(config) + 1_000.0, 2_000.0, 1_000.0);
    sim.bodies[handle].set_translation(position, true);
    sim.bodies[handle].set_linvel(Vector::X * 20.0, true);
    let rng = sim.rng;
    sim.advance(settings().integration.fixed_step_s);
    assert!(body(&sim, id).translation().x > position.x);
    assert_eq!(sim.rng, rng);
    assert_eq!(
        sim.state
            .assets
            .iter()
            .filter(|asset| asset.kind == EntityKind::Debris)
            .count(),
        3
    );
    assert_eq!(sim.handles, handles);
}

#[test]
fn impact_archives_resume_their_real_dynamics_and_reject_stream_overrides() {
    let mut original = Simulation::with_experiment(100, impact_config(3)).unwrap();
    original
        .tune_ship(ShipTuning {
            thrust_scale: 3.2,
            ..original.ship_tuning()
        })
        .unwrap();
    let exported = serde_json::to_value(original.snapshot()).unwrap();
    let mut restored = Simulation::with_experiment(1, impact_config(0)).unwrap();
    restored
        .execute(PhysicsCommand::Restore {
            state: Box::new(serde_json::from_value(exported.clone()).unwrap()),
        })
        .unwrap();
    assert_eq!(serde_json::to_value(restored.snapshot()).unwrap(), exported);
    original.advance(settings().integration.fixed_step_s);
    restored.advance(settings().integration.fixed_step_s);
    assert_eq!(
        serde_json::to_value(restored.snapshot()).unwrap(),
        serde_json::to_value(original.snapshot()).unwrap()
    );
    let mut invalid = restored.snapshot();
    invalid.debris_flow_override = Some(restored.debris_flow());
    let before = serde_json::to_value(restored.snapshot()).unwrap();
    assert!(
        restored
            .execute(PhysicsCommand::Restore {
                state: Box::new(invalid)
            })
            .is_err()
    );
    assert_eq!(serde_json::to_value(restored.snapshot()).unwrap(), before);
    restored.reset(100).unwrap();
    assert_eq!(
        restored
            .state
            .experiment_settings
            .unwrap()
            .debris
            .distribution,
        impact_config(3).debris.distribution
    );
}

#[test]
fn impact_initial_conditions_can_hit_actual_terrain_without_injected_contact_or_per_frame_steering()
{
    let mut config = impact_config(7);
    config.debris.distribution = DebrisDistribution::ImpactEjecta {
        settings: ImpactEjectaSettings {
            source_direction: [-1.0, 0.0, 0.0],
            kick_velocity_mps: [100.0, 0.0, 0.0],
            tangential_speed_mps: 7.0,
            patch_half_angle_deg: 1.0,
        },
    };
    let mut sim = Simulation::with_experiment(100, impact_config(0)).unwrap();
    // The positive-X lander is out of this negative-X source patch. Everything
    // else is the configured seeded cloud, ordinary gravity and real contacts.
    sim.configure_flight(config, Some(100), FlightPreset::MarsFacingL1)
        .unwrap();
    let dt = settings().integration.fixed_step_s;
    let mut touched = false;
    for _ in 0..(8.0 / dt).ceil() as usize {
        sim.advance(dt);
        touched = sim.narrow.contact_pairs().any(|pair| {
            let a = sim.collider_ids[&pair.collider1];
            let b = sim.collider_ids[&pair.collider2];
            matches!(
                (a, b),
                (EntityId::Phobos, EntityId::Debris(_)) | (EntityId::Debris(_), EntityId::Phobos)
            ) && pair.has_any_active_contact()
                && pair.total_impulse_magnitude() > 0.0
        });
        if touched {
            break;
        }
    }
    assert!(
        touched,
        "the actual configured ejecta must receive a Rapier terrain impulse"
    );
    assert!(sim.state.collisions > 0);
    assert_eq!(sim.debris_dynamics().unwrap().physical_count, 7);
}

#[test]
fn debug_dynamics_uses_actual_bodies_and_reads_do_not_advance_the_clock() {
    let mut config = ExperimentSettings::default();
    config.debris.count = 12;
    let mut sim = Simulation::with_experiment(100, config).unwrap();
    let before = sim.snapshot();
    let before_json = serde_json::to_value(&before).unwrap();
    let report = sim.debris_dynamics().unwrap();
    assert_eq!(report.physical_count, 12);
    assert_eq!(
        report.moving_count, 12,
        "stored nonzero velocity is not a playback-state claim"
    );
    assert!(report.min_speed_mps.unwrap() >= 16.0 - 1e-10);
    assert!(report.max_speed_mps.unwrap() <= 24.0 + 1e-10);
    assert!(report.mean_spin_rad_s.unwrap() > 0.0);
    let rng = sim.rng;
    for _ in 0..3 {
        sim.debris_dynamics().unwrap();
        sim.observation();
    }
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before_json);
    assert_eq!(sim.rng, rng);
    assert_eq!(sim.debris_dynamics().unwrap().source_time_s, 0.0);
    let id = EntityId::Debris(1);
    let handle = sim.handles[&id];
    let before_velocity = sim.bodies[handle].linvel();
    let mass = sim.bodies[handle].mass();
    sim.bodies[handle].apply_impulse(Vector::Y * mass * 12.0, true);
    let changed = sim.debris_dynamics().unwrap();
    assert!(
        (changed.mean_velocity_world_mps[1] - report.mean_velocity_world_mps[1] - 1.0).abs()
            < 1e-10
    );
    assert_eq!(
        sim.state
            .assets
            .iter()
            .find(|asset| asset.id == id)
            .unwrap()
            .velocity,
        before_velocity.to_array()
    );
    sim.advance(settings().integration.fixed_step_s);
    assert!(sim.debris_dynamics().unwrap().source_time_s > 0.0);
    assert_ne!(
        body(&sim, id).translation().to_array(),
        before
            .assets
            .iter()
            .find(|asset| asset.id == id)
            .unwrap()
            .position
    );
    let mut empty_config = config;
    empty_config.debris.count = 0;
    let empty = Simulation::with_experiment(100, empty_config)
        .unwrap()
        .debris_dynamics()
        .unwrap();
    assert_eq!(empty.physical_count, 0);
    assert_eq!(empty.moving_count, 0);
    assert_eq!(empty.mean_velocity_world_mps, [0.0; 3]);
    assert_eq!(empty.min_speed_mps, None);
    assert_eq!(empty.max_speed_mps, None);
    assert_eq!(empty.mean_spin_rad_s, None);
    assert_eq!(empty.total_mass_kg, 0.0);
}

#[test]
fn ten_thousand_then_one_hundred_thousand_real_bodies_construct_and_take_one_ordinary_step() {
    assert_eq!(crate::model::MAX_DEBRIS_COUNT, 100_000);
    // One heavy test, sequential cases. No huge snapshot/JSON clones or parallel
    // worlds, no render-only counts, and no frame-rate claim.
    for count in [10_000, 100_000] {
        let mut config = ExperimentSettings::default();
        config.debris.count = count;
        config.debris.radius_min_m = 0.05;
        config.debris.radius_max_m = 0.08;
        let mut sim = Simulation::with_experiment(100, config).unwrap();
        assert_eq!(sim.state.assets.len(), count + 3);
        assert_eq!(sim.bodies.len(), count + 4);
        assert_eq!(sim.colliders.len(), count + 4);
        let ids: HashSet<_> = sim
            .state
            .assets
            .iter()
            .filter(|asset| asset.kind == EntityKind::Debris)
            .map(|asset| asset.id)
            .collect();
        assert_eq!(ids.len(), count);
        let mapped_bodies: HashSet<_> = ids.iter().map(|id| sim.handles[id]).collect();
        assert_eq!(mapped_bodies.len(), count);
        assert_eq!(
            sim.bodies
                .iter()
                .filter(|(_, body)| body.is_dynamic())
                .count(),
            count + 1
        );
        for id in &ids {
            let body = body(&sim, *id);
            assert!(body.is_dynamic() && body.is_ccd_enabled());
            assert!(body.mass() > 0.0);
            assert_eq!(body.colliders().len(), 1);
        }
        let first = EntityId::Debris(1);
        let last = EntityId::Debris(count as u32);
        let first_position = body(&sim, first).translation();
        let last_rotation = *body(&sim, last).rotation();
        let last_handle = sim.handles[&last];
        sim.advance(settings().integration.fixed_step_s);
        assert_eq!(sim.handles[&last], last_handle);
        assert_ne!(body(&sim, first).translation(), first_position);
        assert_ne!(*body(&sim, last).rotation(), last_rotation);
        let report = sim.debris_dynamics().unwrap();
        assert_eq!(report.physical_count, count);
        assert_eq!(report.moving_count, count);
        assert_eq!(report.source_time_s, settings().integration.fixed_step_s);
        assert!(report.total_mass_kg > 0.0);
        let body = &mut sim.bodies[last_handle];
        let velocity = body.linvel();
        let spin = body.angvel();
        let rotation = *body.rotation();
        let inertia = body
            .mass_properties()
            .local_mprops
            .reconstruct_inertia_matrix();
        let mass = body.mass();
        let angular_impulse = Vector::Z * 0.0001;
        let expected_spin_change =
            rotation * (inertia.inverse() * (rotation.inverse() * angular_impulse));
        body.apply_impulse(Vector::X, true);
        body.apply_torque_impulse(angular_impulse, true);
        assert!((body.linvel() - velocity - Vector::X / mass).length() < 1e-9);
        assert!((body.angvel() - spin - expected_spin_change).length() < 1e-8);
        assert_eq!(sim.state.assets.len(), count + 3);
        assert_eq!(sim.bodies.len(), count + 4);
    }
}

#[test]
fn finite_huge_source_directions_are_normalized_without_nonfinite_ejecta() {
    let mut config = impact_config(3);
    config.debris.distribution = DebrisDistribution::ImpactEjecta {
        settings: ImpactEjectaSettings {
            source_direction: [f64::MAX, f64::MAX, 0.0],
            ..settings().impact_ejecta_defaults
        },
    };
    let sim = Simulation::with_experiment(100, config).unwrap();
    assert!(sim.state.assets.iter().all(|asset| {
        asset
            .position
            .into_iter()
            .chain(asset.velocity)
            .all(f64::is_finite)
    }));
    validate(&sim.snapshot()).unwrap();
}
