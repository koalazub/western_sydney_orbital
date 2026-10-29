use super::*;

fn config(count: usize) -> ExperimentSettings {
    let mut config = ExperimentSettings::default();
    config.debris.count = count;
    config.debris.radius_min_m = 0.5;
    config.debris.radius_max_m = 3.0;
    config.debris.trajectory_adjustment = DebrisTrajectoryAdjustment::TowardLander;
    config
}

fn actual(sim: &Simulation, id: EntityId) -> &RigidBody {
    &sim.bodies[sim.handles[&id]]
}

fn cursor(sim: &Simulation) -> DebrisTrajectoryAdjustmentState {
    sim.state.debris_trajectory_adjustment_state.unwrap()
}

#[test]
fn source_constants_and_round_robin_ticks_apply_mass_scaled_real_impulses_with_gravity_and_spin() {
    let constants = &settings().debris_trajectory_adjustment;
    assert_eq!(constants.global_rate_hz, 60.0);
    assert_eq!(constants.cutoff_m, 100.0);
    assert_eq!(constants.typical_speed_mps, 2.0);
    assert_eq!(constants.delta_v_factor, 0.01);
    assert!(!constants.source_enabled_by_default);
    assert!(
        constants
            .source_url
            .contains("c1e3fc8f9ee3b189bba64d8bd87818a8003639cc/Systems/SpawnDebris.py")
    );
    assert!(
        constants
            .scheduling_note
            .contains("Local deterministic simulation-clock adaptation")
    );
    let mut sim = Simulation::with_experiment(42, config(7)).unwrap();
    let handles = sim.handles.clone();
    let dt = 1.0 / constants.global_rate_hz;
    let rng = sim.rng;
    for iteration in 0..7_u64 {
        let id = EntityId::Debris((iteration + 1) as u32);
        let body = actual(&sim, id);
        let velocity = body.linvel();
        let position = body.translation();
        let spin = body.angvel();
        let rotation = *body.rotation();
        let mass = body.mass();
        let target = actual(&sim, EntityId::Lander).translation();
        let change = (target - position).normalize() * 0.02;
        let gravity =
            crate::astrophysics::acceleration(position, velocity + change, settings().gravity);
        let unselected = EntityId::Debris(((iteration + 1) % 7 + 1) as u32);
        let other = actual(&sim, unselected);
        let other_velocity = other.linvel();
        let other_gravity = crate::astrophysics::acceleration(
            other.translation(),
            other_velocity,
            settings().gravity,
        );
        sim.advance(dt);
        let body = actual(&sim, id);
        let applied_change = body.linvel() - velocity - gravity * dt;
        assert!((applied_change - change).length() < 1e-10);
        assert!((applied_change.length() - 0.02).abs() < 1e-10);
        assert!((applied_change * mass - change * mass).length() < mass * 1e-10);
        assert!((body.user_force() - gravity * mass).length() < mass * 1e-10);
        assert_eq!(body.user_torque(), Vector::ZERO);
        assert!((body.angvel() - spin).length() < 1e-10);
        assert_ne!(*body.rotation(), rotation);
        assert_ne!(body.translation(), position);
        assert!(
            (actual(&sim, unselected).linvel() - other_velocity - other_gravity * dt).length()
                < 1e-10
        );
        assert_eq!(cursor(&sim).iteration, iteration + 1);
        assert_eq!(cursor(&sim).applied_count, iteration + 1);
        validate(&sim.snapshot()).unwrap();
    }
    assert_eq!(sim.handles, handles);
    // Radar draws may happen after 0.1 s, but guidance never consumes RNG.
    assert_ne!(sim.rng, rng);
    assert_eq!(sim.debris_dynamics().unwrap().physical_count, 7);
}

#[test]
fn a_fixed_physics_step_selects_three_rocks_globally_not_sixty_ticks_per_rock() {
    let mut sim = Simulation::with_experiment(42, config(37)).unwrap();
    let before: Vec<_> = (1..=37)
        .map(|id| {
            let body = actual(&sim, EntityId::Debris(id));
            (body.translation(), body.linvel())
        })
        .collect();
    let target = actual(&sim, EntityId::Lander).translation();
    let dt = settings().integration.fixed_step_s;
    sim.advance(dt);
    assert_eq!(cursor(&sim).iteration, 3);
    assert_eq!(cursor(&sim).applied_count, 3);
    for (index, (position, velocity)) in before.into_iter().enumerate() {
        let change = if index < 3 {
            (target - position).normalize() * 0.02
        } else {
            Vector::ZERO
        };
        let gravity =
            crate::astrophysics::acceleration(position, velocity + change, settings().gravity);
        assert!(
            (actual(&sim, EntityId::Debris(index as u32 + 1)).linvel()
                - velocity
                - change
                - gravity * dt)
                .length()
                < 1e-9
        );
    }
}

#[test]
fn guidance_uses_the_actual_moved_lander_and_strict_source_cutoff() {
    let dt = 1.0 / 60.0;
    for distance in [99.0, 100.0, 101.0] {
        let mut sim = Simulation::with_experiment(42, config(1)).unwrap();
        let id = EntityId::Debris(1);
        let position = actual(&sim, id).translation();
        sim.place_lander((position + Vector::Y * distance).to_array())
            .unwrap();
        let velocity = actual(&sim, id).linvel();
        let change = if distance > 100.0 {
            Vector::Y * 0.02
        } else {
            Vector::ZERO
        };
        let gravity =
            crate::astrophysics::acceleration(position, velocity + change, settings().gravity);
        sim.advance(dt);
        assert!((actual(&sim, id).linvel() - velocity - gravity * dt - change).length() < 1e-9);
        assert_eq!(cursor(&sim).applied_count, u64::from(distance > 100.0));
        assert_eq!(cursor(&sim).iteration, 1);
    }
    let mut sim = Simulation::with_experiment(42, config(1)).unwrap();
    sim.place_lander([16_000.0, 8_000.0, 9_000.0]).unwrap();
    let id = EntityId::Debris(1);
    let position = actual(&sim, id).translation();
    let velocity = actual(&sim, id).linvel();
    let change = (actual(&sim, EntityId::Lander).translation() - position).normalize() * 0.02;
    let gravity =
        crate::astrophysics::acceleration(position, velocity + change, settings().gravity);
    sim.advance(dt);
    assert!((actual(&sim, id).linvel() - velocity - gravity * dt - change).length() < 1e-9);
    assert!(change.y > 0.0 && change.z > 0.0);
    assert_eq!(cursor(&sim).applied_count, 1);
}

#[test]
fn adjustment_off_is_an_unchanged_archive_and_readback_only_publishes_mode_constants_and_count() {
    let mut off = config(7);
    off.debris.trajectory_adjustment = DebrisTrajectoryAdjustment::Off;
    let mut sim = Simulation::with_experiment(42, off).unwrap();
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    assert!(before.get("debris_trajectory_adjustment_state").is_none());
    assert!(
        before["experiment_settings"]["debris"]
            .get("trajectory_adjustment")
            .is_none()
    );
    let before_rng = sim.rng;
    sim.apply_debris_trajectory_adjustment(0.05);
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
    assert_eq!(sim.rng, before_rng);
    let id = EntityId::Debris(1);
    let body = actual(&sim, id);
    let velocity = body.linvel();
    let gravity =
        crate::astrophysics::acceleration(body.translation(), velocity, settings().gravity);
    sim.advance(0.05);
    assert!((actual(&sim, id).linvel() - velocity - gravity * 0.05).length() < 1e-9);
    assert_eq!(sim.state.debris_trajectory_adjustment_state, None);
    let mut on = Simulation::with_experiment(42, config(7)).unwrap();
    on.advance(0.05);
    let snapshot = serde_json::to_value(on.snapshot()).unwrap();
    let report = on.observation();
    let summary = &report["debris_trajectory_adjustment"];
    assert_eq!(summary["mode"], "toward_lander");
    assert_eq!(summary["applied_count"], 3);
    assert_eq!(summary.as_object().unwrap().len(), 3);
    assert!(summary.get("iteration").is_none());
    assert!(summary.get("positions").is_none());
    assert!(report.get("debris_trajectory_adjustment_state").is_none());
    assert!(report.get("assets").is_none());
    assert_eq!(serde_json::to_value(on.snapshot()).unwrap(), snapshot);
}

#[test]
fn cursor_archive_resume_and_same_fixed_step_partition_keep_replay_exact() {
    let config = config(7);
    let mut whole = Simulation::with_experiment(42, config).unwrap();
    whole.place_lander([16_000.0, 8_000.0, 9_000.0]).unwrap();
    let mut split = Simulation::with_experiment(42, config).unwrap();
    split.place_lander([16_000.0, 8_000.0, 9_000.0]).unwrap();
    whole.advance(0.2);
    split.advance(0.1);
    let archive = serde_json::to_value(split.snapshot()).unwrap();
    let mut restored = Simulation::with_experiment(1, config).unwrap();
    restored
        .execute(PhysicsCommand::Restore {
            state: Box::new(serde_json::from_value(archive.clone()).unwrap()),
        })
        .unwrap();
    assert_eq!(serde_json::to_value(restored.snapshot()).unwrap(), archive);
    split.advance(0.1);
    restored.advance(0.1);
    assert_eq!(
        serde_json::to_value(restored.snapshot()).unwrap(),
        serde_json::to_value(split.snapshot()).unwrap()
    );
    assert_eq!(
        serde_json::to_value(restored.snapshot()).unwrap(),
        serde_json::to_value(whole.snapshot()).unwrap()
    );
    assert_eq!(cursor(&restored).iteration, 12);
    assert_eq!(cursor(&restored).applied_count, 12);
    // A save between global ticks keeps that phase through its simulation time.
    split.advance(0.005);
    let archive = serde_json::to_value(split.snapshot()).unwrap();
    restored
        .execute(PhysicsCommand::Restore {
            state: Box::new(serde_json::from_value(archive).unwrap()),
        })
        .unwrap();
    for sim in [&mut split, &mut restored] {
        sim.advance(0.025);
    }
    assert_eq!(
        serde_json::to_value(restored.snapshot()).unwrap(),
        serde_json::to_value(split.snapshot()).unwrap()
    );
    validate(&restored.snapshot()).unwrap();
}

#[test]
fn invalid_or_missing_cursor_history_is_rejected_atomically_instead_of_enabling_an_old_archive() {
    let mut sim = Simulation::with_experiment(42, config(3)).unwrap();
    sim.advance(0.05);
    let valid = sim.snapshot();
    let before = serde_json::to_value(&valid).unwrap();
    let handles = sim.handles.clone();
    let mut invalid = Vec::new();
    let mut missing = valid.clone();
    missing.debris_trajectory_adjustment_state = None;
    invalid.push(missing);
    for cursor in [
        DebrisTrajectoryAdjustmentState {
            version: 0,
            iteration: 3,
            applied_count: 3,
        },
        DebrisTrajectoryAdjustmentState {
            version: 1,
            iteration: 2,
            applied_count: 2,
        },
        DebrisTrajectoryAdjustmentState {
            version: 1,
            iteration: 3,
            applied_count: 4,
        },
        DebrisTrajectoryAdjustmentState {
            version: 1,
            iteration: u64::MAX,
            applied_count: 0,
        },
    ] {
        let mut state = valid.clone();
        state.debris_trajectory_adjustment_state = Some(cursor);
        invalid.push(state);
    }
    let mut off = valid.clone();
    off.experiment_settings
        .as_mut()
        .unwrap()
        .debris
        .trajectory_adjustment = DebrisTrajectoryAdjustment::Off;
    invalid.push(off);
    for state in invalid {
        assert!(
            sim.execute(PhysicsCommand::Restore {
                state: Box::new(state)
            })
            .is_err()
        );
        assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
        assert_eq!(sim.handles, handles);
    }
    let mut old_config = config(3);
    old_config.debris.trajectory_adjustment = DebrisTrajectoryAdjustment::Off;
    let mut old = Simulation::with_experiment(42, old_config).unwrap();
    old.advance(0.05);
    let old_json = serde_json::to_value(old.snapshot()).unwrap();
    old.execute(PhysicsCommand::Restore {
        state: Box::new(serde_json::from_value(old_json.clone()).unwrap()),
    })
    .unwrap();
    assert_eq!(serde_json::to_value(old.snapshot()).unwrap(), old_json);
    let mut retroactive = old.snapshot();
    retroactive
        .experiment_settings
        .as_mut()
        .unwrap()
        .debris
        .trajectory_adjustment = DebrisTrajectoryAdjustment::TowardLander;
    assert!(
        old.execute(PhysicsCommand::Restore {
            state: Box::new(retroactive)
        })
        .is_err()
    );
    assert_eq!(serde_json::to_value(old.snapshot()).unwrap(), old_json);
}

#[test]
fn reset_reseed_and_live_flow_keep_explicit_mode_but_restart_only_new_trial_history() {
    let config = config(17);
    let mut sim = Simulation::with_experiment(42, config).unwrap();
    let initial = serde_json::to_value(sim.snapshot()).unwrap();
    sim.advance(0.1);
    assert!(cursor(&sim).applied_count > 0);
    sim.reset(42).unwrap();
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), initial);
    sim.reset(43).unwrap();
    let reseeded = Simulation::with_experiment(43, config).unwrap();
    assert_eq!(
        serde_json::to_value(sim.snapshot()).unwrap(),
        serde_json::to_value(reseeded.snapshot()).unwrap()
    );
    let cursor = cursor(&sim);
    sim.tune_debris_flow(DebrisFlowTuning {
        speed_mps: 30.0,
        ..sim.debris_flow()
    })
    .unwrap();
    assert_eq!(sim.state.debris_trajectory_adjustment_state, Some(cursor));
    assert_eq!(
        sim.state
            .experiment_settings
            .unwrap()
            .debris
            .trajectory_adjustment,
        DebrisTrajectoryAdjustment::TowardLander
    );
    let mut off = config;
    off.debris.trajectory_adjustment = DebrisTrajectoryAdjustment::Off;
    sim.configure_flight(off, None, sim.flight_preset())
        .unwrap();
    assert_eq!(sim.state.debris_trajectory_adjustment_state, None);
    assert_eq!(sim.debris_trajectory_adjustment().applied_count, 0);
}

#[test]
fn archive_asset_order_does_not_change_round_robin_identity_or_actual_impulses() {
    let config = config(7);
    let mut source = Simulation::with_experiment(42, config).unwrap();
    source.place_lander([16_000.0, 8_000.0, 9_000.0]).unwrap();
    source.advance(0.005);
    let mut archive = serde_json::to_value(source.snapshot()).unwrap();
    archive["assets"].as_array_mut().unwrap().reverse();
    let mut restored = Simulation::with_experiment(1, config).unwrap();
    restored
        .execute(PhysicsCommand::Restore {
            state: Box::new(serde_json::from_value(archive).unwrap()),
        })
        .unwrap();
    for sim in [&mut source, &mut restored] {
        sim.advance(0.05);
    }
    assert_eq!(cursor(&source), cursor(&restored));
    assert_eq!(cursor(&source).iteration, 3);
    for id in 1..=7 {
        let id = EntityId::Debris(id);
        assert_eq!(actual(&source, id).linvel(), actual(&restored, id).linvel());
        assert_eq!(
            actual(&source, id).translation(),
            actual(&restored, id).translation()
        );
        assert_eq!(
            *actual(&source, id).rotation(),
            *actual(&restored, id).rotation()
        );
    }
}

#[test]
fn bounded_clock_skips_zero_count_missing_lander_nonadvance_and_overflow_without_catchup() {
    let mut empty = Simulation::with_experiment(42, config(0)).unwrap();
    empty.advance(0.05);
    assert_eq!(cursor(&empty).iteration, 3);
    assert_eq!(cursor(&empty).applied_count, 0);
    validate(&empty.snapshot()).unwrap();
    let before = serde_json::to_value(empty.snapshot()).unwrap();
    for duration in [0.0, -1.0, f64::NAN, f64::INFINITY] {
        empty.advance(duration);
    }
    assert_eq!(serde_json::to_value(empty.snapshot()).unwrap(), before);
    let mut missing = Simulation::with_experiment(42, config(1)).unwrap();
    missing.handles.remove(&EntityId::Lander);
    missing.apply_debris_trajectory_adjustment(0.05);
    assert_eq!(cursor(&missing).iteration, 3);
    assert_eq!(cursor(&missing).applied_count, 0);
    assert_eq!(due_ticks(0.0, 30.0, 0), None);
    assert_eq!(due_ticks(100.0, 0.05, 0), None);
    assert_eq!(due_ticks(f64::MAX, 0.05, u64::MAX), None);
    assert_eq!(completed_ticks(f64::INFINITY), None);
    assert_eq!(completed_ticks(-1.0), None);
    assert_eq!(completed_ticks(MAX_EXACT_JSON_INTEGER as f64), None);
}

#[test]
fn actual_debris_contacts_still_exchange_momentum_under_small_guidance_impulses() {
    let mut config = config(2);
    config.debris.distribution = DebrisDistribution::DirectedStream;
    config.debris.radius_min_m = 2.0;
    config.debris.radius_max_m = 2.0;
    let mut sim = Simulation::with_experiment(10, config).unwrap();
    let center = Vector::new(crate::model::phobos_enclosing_radius_m() + 500.0, 0.0, 0.0);
    let first = EntityId::Debris(1);
    let second = EntityId::Debris(2);
    for (id, offset, velocity) in [(first, -2.5, 5.0), (second, 2.5, -5.0)] {
        let body = &mut sim.bodies[sim.handles[&id]];
        body.set_translation(center + Vector::X * offset, true);
        body.apply_impulse((Vector::X * velocity - body.linvel()) * body.mass(), true);
    }
    let mut impulse = false;
    for _ in 0..8 {
        sim.advance(0.05);
        impulse |= sim.narrow.contact_pairs().any(|pair| {
            pair.has_any_active_contact()
                && pair.total_impulse_magnitude() > 0.0
                && pair_key(
                    sim.collider_ids[&pair.collider1],
                    sim.collider_ids[&pair.collider2],
                ) == pair_key(first, second)
        });
    }
    assert!(impulse);
    assert!(actual(&sim, first).linvel().x < 1.0);
    assert!(actual(&sim, second).linvel().x > -1.0);
    assert!(sim.state.collisions > 0);
    assert!(cursor(&sim).applied_count > 0);
    assert!(actual(&sim, first).angvel().length() > 0.0);
    validate(&sim.snapshot()).unwrap();
}

#[test]
fn terminal_real_lander_contact_freezes_the_guidance_cursor_with_the_physical_world() {
    let mut sim = Simulation::with_experiment(100, config(0)).unwrap();
    sim.set_command([0.0, 0.0, 0.5], [0.0; 3]).unwrap();
    sim.advance(8.0);
    let up = *actual(&sim, EntityId::LandingPad).rotation() * Vector::Z;
    let handle = sim.handles[&EntityId::Lander];
    let mass = sim.bodies[handle].mass();
    sim.bodies[handle].apply_impulse(-up * mass * 60.0, true);
    sim.advance(2.0);
    assert_eq!(sim.mission_status(), MissionStatus::FailedDamage);
    assert!(sim.state.mission.damage > settings().mission.impact_threshold_j);
    let frozen = serde_json::to_value(sim.snapshot()).unwrap();
    sim.apply_debris_trajectory_adjustment(0.05);
    sim.advance(1.0);
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), frozen);
    assert!(sim.state.mission.score.is_none());
    assert!(sim.state.mission.score_breakdown.is_none());
    validate(&sim.snapshot()).unwrap();
}
