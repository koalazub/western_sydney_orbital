use super::*;

fn empty_experiment() -> ExperimentSettings {
    let mut experiment = ExperimentSettings::default();
    experiment.debris.count = 0;
    experiment
}

fn free_flight() -> Simulation {
    let mut sim = Simulation::with_experiment(100, empty_experiment()).unwrap();
    let mut state = sim.snapshot();
    let lander = state
        .assets
        .iter_mut()
        .find(|asset| asset.id == EntityId::Lander)
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

fn analytics(sim: &Simulation) -> &FlightAnalytics {
    sim.state.mission.flight_analytics.as_ref().unwrap()
}

fn close(actual: f64, expected: f64) {
    assert!(
        (actual - expected).abs() <= 1e-10 * expected.abs().max(1.0),
        "{actual} != {expected}"
    );
}

#[test]
fn actual_force_torque_and_coasting_spin_have_separate_contiguous_episodes() {
    let mut sim = free_flight();
    sim.tune_ship(ShipTuning {
        mass_kg: 100.0,
        inertia_scale: 1.0,
        thrust_scale: 3.2,
    })
    .unwrap();
    assert_eq!(analytics(&sim), &FlightAnalytics::default());
    sim.set_command([0.0, 0.5, 0.0], [0.0, 0.0, 0.04]).unwrap();
    assert_eq!(
        analytics(&sim),
        &FlightAnalytics::default(),
        "a queued request is not a burn"
    );
    sim.advance(0.12);
    let burning = analytics(&sim).clone();
    let application = sim.last_actuator_application.unwrap();
    assert_eq!(burning.observed_sim_s, sim.state.time_s);
    close(burning.observed_sim_s, 0.12);
    assert_eq!(
        (
            burning.burn_count,
            burning.angular_burn_count,
            burning.turn_count
        ),
        (1, 1, 1)
    );
    close(burning.burn_duration_s, 0.12);
    close(burning.angular_burn_duration_s, 0.12);
    close(burning.turn_duration_s, 0.12);
    close(
        burning.propellant_impulse_ns,
        application.propellant_force_n * 0.12,
    );
    close(
        burning.angular_impulse_nms,
        norm(application.torque_world_nm) * 0.12,
    );
    let fuel_config = settings().lander;
    close(
        sim.state.mission.fuel_used_kg,
        burning.propellant_impulse_ns
            / (fuel_config.specific_impulse_s * fuel_config.standard_gravity_mps2),
    );
    sim.execute(PhysicsCommand::Stop).unwrap();
    assert_eq!(
        analytics(&sim),
        &burning,
        "flags describe the last step, not pending Stop"
    );
    sim.advance(0.08);
    let coast = analytics(&sim);
    assert!(!coast.burn_active && !coast.angular_burn_active && coast.turn_active);
    assert_eq!(
        (coast.burn_count, coast.angular_burn_count, coast.turn_count),
        (1, 1, 1)
    );
    close(coast.turn_duration_s, 0.20);
    close(coast.current_turn_s, 0.20);
    close(coast.longest_turn_s, 0.20);
    assert_eq!(coast.propellant_impulse_ns, burning.propellant_impulse_ns);
    assert_eq!(coast.angular_impulse_nms, burning.angular_impulse_nms);
    assert!(coast.total_turn_angle_rad > burning.total_turn_angle_rad);
    sim.set_command([0.5, 0.0, 0.0], [0.0; 3]).unwrap();
    sim.advance(0.03);
    let resumed = analytics(&sim);
    assert_eq!(
        (
            resumed.burn_count,
            resumed.angular_burn_count,
            resumed.turn_count
        ),
        (2, 1, 1)
    );
    close(resumed.burn_duration_s, 0.15);
    close(resumed.longest_burn_s, 0.12);
    close(resumed.current_burn_s, 0.03);
    close(resumed.angular_burn_duration_s, 0.12);
    close(resumed.turn_duration_s, 0.23);
    validate(&sim.snapshot()).unwrap();
}

#[test]
fn native_h_and_post_step_motion_come_from_actual_rapier_not_cached_entity_commands() {
    let mut sim = free_flight();
    sim.set_command([0.0; 3], [0.0, 0.0, 0.04]).unwrap();
    let handle = sim.handles[&EntityId::Lander];
    let h = settings().integration.fixed_step_s;
    let mut expected_angle = 0.0;
    let mut expected_peak = 0.0_f64;
    for duration in [h, h * 0.2, h * 0.7] {
        let before = *sim.bodies[handle].rotation();
        // This cached telemetry is intentionally stale; neither angular requests
        // nor EntityState motion can stand in for the actual post-step body.
        let entity = sim
            .state
            .assets
            .iter_mut()
            .find(|asset| asset.id == EntityId::Lander)
            .unwrap();
        entity.angular_velocity_rad_s = [999.0; 3];
        entity.rotation = [0.0, 1.0, 0.0, 0.0];
        sim.advance(duration);
        let body = &sim.bodies[handle];
        let delta = before.inverse() * *body.rotation();
        expected_angle += 2.0 * delta.x.hypot(delta.y).hypot(delta.z).atan2(delta.w.abs());
        expected_peak = expected_peak.max(body.angvel().length());
        let actual = analytics(&sim);
        close(actual.total_turn_angle_rad, expected_angle);
        close(actual.peak_angular_speed_rad_s, expected_peak);
        assert_eq!(actual.observed_sim_s, sim.state.time_s);
        assert_eq!(actual.burn_count, 0);
        assert_eq!(actual.propellant_impulse_ns, 0.0);
        assert_eq!(
            sim.state.mission.fuel_used_kg, 0.0,
            "preserve the local angular-only fuel model"
        );
    }
    close(analytics(&sim).angular_burn_duration_s, h * 1.9);
    assert_eq!(analytics(&sim).angular_burn_count, 1);
    assert_eq!(analytics(&sim).turn_count, 1);
    sim.execute(PhysicsCommand::Stop).unwrap();
    sim.advance(h * 0.3);
    assert!(!analytics(&sim).angular_burn_active && analytics(&sim).turn_active);
    assert_eq!(analytics(&sim).angular_burn_count, 1);
    assert_eq!(analytics(&sim).turn_count, 1);
}

#[test]
fn reads_zero_steps_and_unintegrated_tuning_never_advance_the_receipt() {
    let mut sim = free_flight();
    sim.set_manual_thrust([0.3, 0.4, 0.0]).unwrap();
    sim.advance(settings().integration.fixed_step_s);
    let before = analytics(&sim).clone();
    let application = sim.last_actuator_application.unwrap();
    assert_eq!(before.burn_count, 1);
    assert_eq!(before.angular_burn_count, 1);
    close(
        before.angular_impulse_nms,
        norm(application.torque_world_nm) * settings().integration.fixed_step_s,
    );
    for _ in 0..3 {
        assert_eq!(
            sim.observation()["mission"]["flight_analytics"],
            json!(before)
        );
        assert_eq!(
            sim.snapshot().mission.flight_analytics.as_ref(),
            Some(&before)
        );
        sim.applied_physics().unwrap();
        sim.radar(10);
        sim.execute(PhysicsCommand::Observe).unwrap();
        sim.advance(0.0);
        sim.advance(f64::NAN);
    }
    sim.tune_ship(ShipTuning {
        thrust_scale: 2.0,
        ..sim.ship_tuning()
    })
    .unwrap();
    sim.debug_upright().unwrap();
    assert_eq!(
        analytics(&sim),
        &before,
        "nonintegrating interventions do not rewrite last-step flags"
    );
    sim.advance(settings().integration.fixed_step_s);
    assert!(!analytics(&sim).burn_active && !analytics(&sim).angular_burn_active);
}

#[test]
fn real_pad_contacts_can_turn_the_lander_without_an_angular_burn() {
    let mut sim = Simulation::with_experiment(100, empty_experiment()).unwrap();
    let handle = sim.handles[&EntityId::Lander];
    let pad_rotation = *sim.bodies[sim.handles[&EntityId::LandingPad]].rotation();
    let up = pad_rotation * Vector::Z;
    let body = &mut sim.bodies[handle];
    body.set_translation(body.translation() + up * 2.0, true);
    body.set_rotation(pad_rotation * Rotation::from_rotation_y(0.2), true);
    body.set_linvel(-up * 3.0, true);
    assert_eq!(body.angvel(), Vector::ZERO);
    let mut observed_contact_turn = false;
    for _ in 0..40 {
        sim.advance(settings().integration.fixed_step_s);
        let contact_impulse = sim.narrow.contact_pairs().any(|pair| {
            pair_key(
                sim.collider_ids[&pair.collider1],
                sim.collider_ids[&pair.collider2],
            ) == pair_key(EntityId::Lander, EntityId::LandingPad)
                && pair.has_any_active_contact()
                && pair.total_impulse_magnitude() > 0.0
        });
        let receipt = analytics(&sim);
        assert_eq!(receipt.burn_count, 0);
        assert_eq!(receipt.angular_burn_count, 0);
        assert_eq!(receipt.propellant_impulse_ns, 0.0);
        assert_eq!(receipt.angular_impulse_nms, 0.0);
        if contact_impulse && receipt.turn_active {
            assert!(receipt.turn_count > 0);
            assert!(receipt.total_turn_angle_rad > 0.0);
            close(
                receipt.peak_angular_speed_rad_s,
                sim.bodies[handle].angvel().length(),
            );
            observed_contact_turn = true;
            break;
        }
        if sim.state.mission.status.is_terminal() {
            break;
        }
    }
    assert!(
        observed_contact_turn,
        "requires a real spinning Rapier pad contact"
    );
}

#[test]
fn archive_restore_continues_active_episodes_and_old_archives_remain_unavailable() {
    let mut original = free_flight();
    original
        .set_command([0.0, 0.5, 0.0], [0.0, 0.0, 0.04])
        .unwrap();
    original.advance(0.11);
    let archive = serde_json::to_value(original.snapshot()).unwrap();
    let mut restored = free_flight();
    restored
        .execute(PhysicsCommand::Restore {
            state: Box::new(serde_json::from_value(archive.clone()).unwrap()),
        })
        .unwrap();
    assert_eq!(analytics(&restored), analytics(&original));
    assert_eq!(restored.last_actuator_application, None);
    for sim in [&mut original, &mut restored] {
        sim.advance(0.06);
        assert_eq!(
            (
                analytics(sim).burn_count,
                analytics(sim).angular_burn_count,
                analytics(sim).turn_count
            ),
            (1, 1, 1)
        );
        close(analytics(sim).current_burn_s, 0.17);
        close(analytics(sim).current_angular_burn_s, 0.17);
        close(analytics(sim).current_turn_s, 0.17);
        validate(&sim.snapshot()).unwrap();
    }
    assert_eq!(analytics(&restored), analytics(&original));
    let mut old_archive = archive;
    old_archive["mission"]
        .as_object_mut()
        .unwrap()
        .remove("flight_analytics");
    let old: Snapshot = serde_json::from_value(old_archive).unwrap();
    assert_eq!(old.mission.flight_analytics, None);
    assert_eq!(MissionState::default().flight_analytics, None);
    restored
        .execute(PhysicsCommand::Restore {
            state: Box::new(old),
        })
        .unwrap();
    restored.advance(0.06);
    assert_eq!(restored.state.mission.flight_analytics, None);
    assert!(restored.observation()["mission"]["flight_analytics"].is_null());
    assert!(restored.state.mission.fuel_used_kg > 0.0);
    restored.reset(100).unwrap();
    assert_eq!(analytics(&restored), &FlightAnalytics::default());
    restored.set_manual_thrust([0.3, 0.4, 0.0]).unwrap();
    restored.advance(0.05);
    restored
        .configure_flight(empty_experiment(), Some(101), FlightPreset::MarsFacingL1)
        .unwrap();
    assert_eq!(analytics(&restored), &FlightAnalytics::default());
}

#[test]
fn terminal_partial_advance_records_only_integrated_time_and_then_freezes() {
    let mut sim = free_flight();
    let mut state = sim.snapshot();
    let target = state
        .assets
        .iter()
        .find(|asset| asset.id == EntityId::ReturnShip)
        .unwrap()
        .position;
    state
        .assets
        .iter_mut()
        .find(|asset| asset.id == EntityId::Lander)
        .unwrap()
        .position = [
        target[0],
        target[1],
        target[2] + settings().mission.arrival_radius_m * 0.5,
    ];
    sim.execute(PhysicsCommand::Restore {
        state: Box::new(state),
    })
    .unwrap();
    sim.set_command([0.1, 0.0, 0.0], [0.0, 0.0, 0.04]).unwrap();
    let request_s = 0.12;
    let h = request_s / (request_s / settings().integration.fixed_step_s).ceil();
    sim.advance(request_s);
    assert_eq!(sim.state.mission.status, MissionStatus::Success);
    close(sim.state.time_s, h);
    close(analytics(&sim).observed_sim_s, h);
    close(analytics(&sim).burn_duration_s, h);
    close(analytics(&sim).angular_burn_duration_s, h);
    let frozen = analytics(&sim).clone();
    sim.clear_terminal_command().unwrap();
    sim.advance(1.0);
    assert_eq!(analytics(&sim), &frozen);
    let archive = serde_json::to_value(sim.snapshot()).unwrap();
    sim.execute(PhysicsCommand::Restore {
        state: Box::new(serde_json::from_value(archive).unwrap()),
    })
    .unwrap();
    sim.advance(1.0);
    assert_eq!(analytics(&sim), &frozen);
}

#[test]
fn invalid_analytics_restore_is_atomic_and_never_changes_real_dynamics() {
    let mut sim = free_flight();
    sim.set_command([0.1, 0.0, 0.0], [0.0, 0.0, 0.04]).unwrap();
    sim.advance(0.05);
    let valid = sim.snapshot();
    let before = serde_json::to_value(&valid).unwrap();
    let body_before = *sim.bodies[sim.handles[&EntityId::Lander]].position();
    for kind in 0..5 {
        let mut invalid = valid.clone();
        let receipt = invalid.mission.flight_analytics.as_mut().unwrap();
        match kind {
            0 => receipt.schema_version = 2,
            1 => receipt.burn_duration_s = 1.0,
            2 => receipt.observed_sim_s = 0.0,
            3 => receipt.total_turn_angle_rad = f64::NAN,
            4 => receipt.current_turn_s = 1.0,
            _ => unreachable!(),
        }
        assert!(
            sim.execute(PhysicsCommand::Restore {
                state: Box::new(invalid)
            })
            .is_err()
        );
        assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
        assert_eq!(
            *sim.bodies[sim.handles[&EntityId::Lander]].position(),
            body_before
        );
    }
    // Invalid measurement state drops only availability on the next real step.
    let mut without_receipt = Simulation::from_state(valid.clone());
    without_receipt.state.mission.flight_analytics = None;
    sim.state
        .mission
        .flight_analytics
        .as_mut()
        .unwrap()
        .propellant_impulse_ns = f64::NAN;
    sim.advance(0.05);
    without_receipt.advance(0.05);
    assert_eq!(sim.state.mission.flight_analytics, None);
    assert_eq!(
        serde_json::to_value(sim.snapshot()).unwrap(),
        serde_json::to_value(without_receipt.snapshot()).unwrap()
    );
}

#[test]
fn public_receipt_has_exact_versioned_own_ship_fields_without_hidden_debris_truth() {
    let sim = free_flight();
    let public = sim.observation();
    let receipt = public["mission"]["flight_analytics"].as_object().unwrap();
    let expected = [
        "schema_version",
        "definition_id",
        "observed_sim_s",
        "burn_count",
        "burn_duration_s",
        "longest_burn_s",
        "angular_burn_count",
        "angular_burn_duration_s",
        "longest_angular_burn_s",
        "turn_count",
        "turn_duration_s",
        "longest_turn_s",
        "total_turn_angle_rad",
        "peak_angular_speed_rad_s",
        "propellant_impulse_ns",
        "angular_impulse_nms",
        "burn_active",
        "angular_burn_active",
        "turn_active",
        "current_burn_s",
        "current_angular_burn_s",
        "current_turn_s",
    ];
    assert_eq!(receipt.len(), expected.len());
    assert!(
        expected
            .into_iter()
            .all(|field| receipt.contains_key(field))
    );
    assert_eq!(receipt["schema_version"], 1);
    assert_eq!(
        receipt["definition_id"],
        "native_actuation_rotation_episodes_v1"
    );
    assert_eq!(public["geometry"]["debris_count"], 0);
    assert!(public.get("assets").is_none());
    assert!(public["radar"].as_array().unwrap().is_empty());
}
