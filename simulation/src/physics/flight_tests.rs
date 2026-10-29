use super::*;

fn small_experiment(count: usize) -> ExperimentSettings {
    let mut settings = ExperimentSettings::default();
    settings.debris.count = count;
    settings
}

fn asset(sim: &Simulation, id: EntityId) -> &EntityState {
    sim.state
        .assets
        .iter()
        .find(|asset| asset.id == id)
        .unwrap()
}

fn actual(sim: &Simulation, id: EntityId) -> &RigidBody {
    &sim.bodies[sim.handles[&id]]
}

#[test]
fn both_presets_use_real_opposite_pads_and_the_matching_lagrange_target() {
    let mut sim = Simulation::with_experiment(1, small_experiment(3)).unwrap();
    for preset in [FlightPreset::MarsFacingL1, FlightPreset::FarSideL2] {
        sim.configure_flight(small_experiment(3), Some(42), preset)
            .unwrap();
        let state = sim.snapshot();
        let definition = settings().flight_preset(preset);
        let expected_target = match preset {
            FlightPreset::MarsFacingL1 => state.environment.l1_position_m,
            FlightPreset::FarSideL2 => state.environment.l2_position_m,
        };
        let sign = match preset {
            FlightPreset::MarsFacingL1 => 1.0,
            FlightPreset::FarSideL2 => -1.0,
        };
        assert_eq!(state.flight_preset, preset);
        assert_eq!(state.mission.target_region, preset.target_region());
        assert_eq!(
            asset(&sim, EntityId::ReturnShip).name,
            definition.target_name
        );
        assert_eq!(asset(&sim, EntityId::ReturnShip).position, expected_target);
        assert_eq!(
            actual(&sim, EntityId::ReturnShip).translation().to_array(),
            expected_target
        );
        assert!(asset(&sim, EntityId::Lander).position[0] * sign > 0.0);
        assert!(asset(&sim, EntityId::LandingPad).position[0] * sign > 0.0);
        assert!(expected_target[0] * sign > 0.0);
        let body = actual(&sim, EntityId::Lander);
        assert!((*body.rotation() * Vector::Z).dot(body.translation().normalize()) > 0.95);
        assert_eq!(sim.observation()["flight_preset"], json!(preset));
        validate(&state).unwrap();
        let archive = serde_json::to_value(&state).unwrap();
        if preset.is_default() {
            assert!(archive.get("flight_preset").is_none());
        }
        sim.execute(PhysicsCommand::Restore {
            state: Box::new(serde_json::from_value(archive.clone()).unwrap()),
        })
        .unwrap();
        assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), archive);
        let mut mismatched = state;
        mismatched.flight_preset = match preset {
            FlightPreset::MarsFacingL1 => FlightPreset::FarSideL2,
            FlightPreset::FarSideL2 => FlightPreset::MarsFacingL1,
        };
        assert!(
            sim.execute(PhysicsCommand::Restore {
                state: Box::new(mismatched)
            })
            .is_err()
        );
        assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), archive);
    }
}

#[test]
fn reset_retains_selected_preset_and_live_hardware_but_not_local_placement() {
    let config = small_experiment(7);
    let mut sim = Simulation::with_experiment(1, config).unwrap();
    sim.configure_flight(config, Some(42), FlightPreset::MarsFacingL1)
        .unwrap();
    let initial = sim.snapshot();
    let tuning = ShipTuning {
        mass_kg: 8_123.0,
        inertia_scale: 2.5,
        thrust_scale: 3.2,
    };
    sim.tune_ship(tuning).unwrap();
    sim.place_lander([16_000.0, 8_000.0, 9_000.0]).unwrap();
    sim.reset(42).unwrap();
    assert_eq!(sim.state.flight_preset, FlightPreset::MarsFacingL1);
    assert_eq!(
        asset(&sim, EntityId::Lander).position,
        initial
            .assets
            .iter()
            .find(|asset| asset.id == EntityId::Lander)
            .unwrap()
            .position
    );
    assert_eq!(sim.ship_tuning(), tuning);
    assert_eq!(sim.state.tuning_revision, 1);
    assert_eq!(sim.state.manual_interventions, 0);
    assert_eq!(sim.state.time_s, 0.0);
    assert_eq!(sim.state.rng_state, initial.rng_state);
    validate(&sim.snapshot()).unwrap();
    sim.execute(PhysicsCommand::ConfigureExperiment {
        settings: config,
        seed: Some(42),
    })
    .unwrap();
    assert_eq!(sim.state.flight_preset, FlightPreset::MarsFacingL1);
    assert_eq!(sim.state.tuning_revision, 0);
    assert_eq!(sim.state.ship_tuning_override, None);
    assert_eq!(
        serde_json::to_value(sim.snapshot()).unwrap(),
        serde_json::to_value(initial).unwrap()
    );
}

#[test]
fn legacy_corridor_cannot_be_relabelled_as_a_mars_facing_world() {
    let mut sim = Simulation::with_profile(100, None).unwrap();
    let expected = serde_json::to_value(sim.snapshot()).unwrap();
    assert!(expected.get("flight_preset").is_none());
    let mut invalid = sim.snapshot();
    invalid.flight_preset = FlightPreset::MarsFacingL1;
    assert!(
        sim.execute(PhysicsCommand::Restore {
            state: Box::new(invalid)
        })
        .is_err()
    );
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), expected);
    let old: Snapshot = serde_json::from_value(expected.clone()).unwrap();
    sim.execute(PhysicsCommand::Restore {
        state: Box::new(old),
    })
    .unwrap();
    sim.reset(100).unwrap();
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), expected);
}

#[test]
fn placement_is_real_in_place_upright_at_rest_and_invalidates_radar_without_rng() {
    let mut sim = Simulation::with_experiment(42, small_experiment(3)).unwrap();
    sim.set_manual_thrust([0.0, 0.3, 0.4]).unwrap();
    sim.advance(settings().integration.fixed_step_s);
    sim.tune_ship(ShipTuning {
        mass_kg: 8_123.0,
        inertia_scale: 2.5,
        thrust_scale: 3.2,
    })
    .unwrap();
    let before = sim.snapshot();
    assert!(!before.observations.is_empty());
    let rng = sim.rng;
    let handles = sim.handles.clone();
    let collider_ids = sim.collider_ids.clone();
    let body_mass = actual(&sim, EntityId::Lander)
        .mass_properties()
        .local_mprops;
    let position = [16_000.0, 8_000.0, 9_000.0];
    sim.place_lander(position).unwrap();
    let body = actual(&sim, EntityId::Lander);
    assert_eq!(body.translation().to_array(), position);
    assert!(
        (*body.rotation() * Vector::Z - Vector::from_array(position).normalize()).length() < 1e-12
    );
    assert_eq!(body.linvel(), Vector::ZERO);
    assert_eq!(body.angvel(), Vector::ZERO);
    assert_eq!(body.user_force(), Vector::ZERO);
    assert_eq!(body.user_torque(), Vector::ZERO);
    assert_eq!(body.mass_properties().local_mprops, body_mass);
    assert_eq!(sim.handles, handles);
    assert_eq!(sim.collider_ids, collider_ids);
    assert_eq!(asset(&sim, EntityId::Lander).position, position);
    assert_eq!(sim.last_actuator_application, None);
    assert_eq!(
        sim.state.manual_interventions,
        before.manual_interventions + 1
    );
    assert_eq!(sim.state.time_s, before.time_s);
    assert_eq!(sim.state.seed, before.seed);
    assert_eq!(sim.rng, rng);
    assert_eq!(sim.state.rng_state, before.rng_state);
    assert!(sim.state.observations.is_empty());
    assert_eq!(sim.state.mission.status, before.mission.status);
    assert_eq!(sim.state.mission.score, before.mission.score);
    assert_eq!(sim.state.mission.damage, before.mission.damage);
    assert_eq!(sim.state.mission.fuel_used_kg, before.mission.fuel_used_kg);
    for old in before
        .assets
        .iter()
        .filter(|asset| asset.id != EntityId::Lander)
    {
        assert_eq!(
            serde_json::to_value(asset(&sim, old.id)).unwrap(),
            serde_json::to_value(old).unwrap()
        );
    }
    assert!(
        sim.state
            .events
            .last()
            .unwrap()
            .message
            .contains("contact cache and radar invalidated")
    );
    validate(&sim.snapshot()).unwrap();
    sim.advance(settings().integration.fixed_step_s);
    assert!(!sim.state.observations.is_empty());
    assert!(
        sim.state
            .observations
            .iter()
            .all(|observation| observation.measurement_time_s > before.time_s)
    );
    assert_ne!(sim.rng, rng);
}

#[test]
fn placement_rejects_actual_world_shapes_bounds_missing_sources_and_counter_overflow_atomically() {
    let mut sim = Simulation::with_experiment(42, small_experiment(2)).unwrap();
    sim.set_command([0.2, 0.0, 0.0], [0.0, 0.01, 0.0]).unwrap();
    sim.advance(settings().integration.fixed_step_s);
    let mut invalid = vec![
        [0.0; 3],
        [f64::NAN, 0.0, 0.0],
        [settings().limits.position_m + 1.0, 0.0, 0.0],
        sim.state.environment.mars_position_m,
    ];
    for id in [
        EntityId::LandingPad,
        EntityId::ReturnShip,
        EntityId::Debris(1),
    ] {
        invalid.push(actual(&sim, id).translation().to_array());
    }
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    let applied = sim.applied_physics().unwrap();
    let pose = *actual(&sim, EntityId::Lander).position();
    for position in invalid {
        assert!(sim.place_lander(position).is_err(), "accepted {position:?}");
        assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
        assert_eq!(sim.applied_physics().unwrap(), applied);
        assert_eq!(*actual(&sim, EntityId::Lander).position(), pose);
    }
    sim.state.manual_interventions = u32::MAX;
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    assert!(sim.place_lander([16_000.0, 8_000.0, 9_000.0]).is_err());
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
    sim.state.manual_interventions = 0;
    let handle = sim.handles.remove(&EntityId::Lander).unwrap();
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    assert!(sim.place_lander([16_000.0, 8_000.0, 9_000.0]).is_err());
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
    assert_eq!(*sim.bodies[handle].position(), pose);
    sim.handles.insert(EntityId::Lander, handle);
    let collider = sim.bodies[handle].colliders()[0];
    sim.collider_ids.remove(&collider);
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    assert!(sim.place_lander([16_000.0, 8_000.0, 9_000.0]).is_err());
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
    assert_eq!(*sim.bodies[handle].position(), pose);
}

#[test]
fn placement_invalidates_lander_contacts_but_preserves_unrelated_real_warmstarts() {
    let mut config = small_experiment(2);
    config.debris.radius_min_m = 2.0;
    config.debris.radius_max_m = 2.0;
    let mut sim = Simulation::with_experiment(10, config).unwrap();
    let center = Vector::new(crate::model::phobos_enclosing_radius_m() + 500.0, 0.0, 0.0);
    let first = EntityId::Debris(1);
    let second = EntityId::Debris(2);
    for (id, offset, velocity) in [(first, -2.5, 5.0), (second, 2.5, -5.0)] {
        let body = &mut sim.bodies[sim.handles[&id]];
        body.set_translation(center + Vector::X * offset, true);
        body.set_linvel(Vector::X * velocity, true);
    }
    for _ in 0..12 {
        sim.advance(settings().integration.fixed_step_s);
        if sim.state.active_contacts.contains(&pair_key(first, second)) {
            break;
        }
    }
    assert!(sim.state.active_contacts.contains(&pair_key(first, second)));
    let warmstarts = unrelated_warmstarts(&sim);
    assert!(warmstarts.iter().any(|contact| contact.0 > 0.0));
    let before_first = *actual(&sim, first).position();
    let before_second = *actual(&sim, second).position();
    sim.place_lander([16_000.0, 8_000.0, 9_000.0]).unwrap();
    assert_eq!(unrelated_warmstarts(&sim), warmstarts);
    assert_eq!(*actual(&sim, first).position(), before_first);
    assert_eq!(*actual(&sim, second).position(), before_second);
    assert!(sim.state.active_contacts.contains(&pair_key(first, second)));
    assert!(
        sim.state
            .active_contacts
            .iter()
            .all(|pair| !pair.contains(&EntityId::Lander))
    );
    sim.advance(settings().integration.fixed_step_s);
    validate(&sim.snapshot()).unwrap();
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
fn placement_cannot_revive_or_rescore_a_real_terminal_collision() {
    let mut sim = Simulation::with_experiment(100, small_experiment(0)).unwrap();
    sim.set_command([0.0, 0.0, 0.5], [0.0; 3]).unwrap();
    sim.advance(8.0);
    let handle = sim.handles[&EntityId::Lander];
    let up = *actual(&sim, EntityId::LandingPad).rotation() * Vector::Z;
    let mass = sim.bodies[handle].mass();
    sim.bodies[handle].apply_impulse(-up * mass * 60.0, true);
    sim.advance(2.0);
    assert_eq!(sim.state.mission.status, MissionStatus::FailedDamage);
    let before = sim.snapshot();
    assert!(before.mission.damage > settings().mission.impact_threshold_j);
    assert!(before.mission.score.is_none());
    assert!(before.mission.score_breakdown.is_none());
    sim.place_lander([16_000.0, 8_000.0, 9_000.0]).unwrap();
    assert_eq!(sim.state.mission.status, before.mission.status);
    assert_eq!(sim.state.mission.phase, before.mission.phase);
    assert_eq!(sim.state.mission.score, before.mission.score);
    assert_eq!(
        sim.state.mission.score_breakdown,
        before.mission.score_breakdown
    );
    assert_eq!(sim.state.mission.damage, before.mission.damage);
    assert_eq!(sim.state.mission.fuel_used_kg, before.mission.fuel_used_kg);
    assert_eq!(sim.state.time_s, before.time_s);
    let placed = serde_json::to_value(sim.snapshot()).unwrap();
    sim.advance(1.0);
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), placed);
}
