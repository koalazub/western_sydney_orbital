use super::*;

fn simulation(count: usize) -> Simulation {
    let mut experiment = ExperimentSettings::default();
    experiment.debris.count = count;
    Simulation::with_experiment(100, experiment).unwrap()
}

fn health(report: &Value, id: &str) -> Value {
    report["sensor_health"]["units"]
        .as_array()
        .unwrap()
        .iter()
        .find(|unit| unit["id"] == id)
        .unwrap()
        .clone()
}

fn ship(simulation: &Simulation) -> &EntityState {
    simulation
        .state
        .assets
        .iter()
        .find(|entity| entity.id == EntityId::Lander)
        .unwrap()
}

fn lease(source_time_s: f64, lease_sim_s: f64) -> LeasedCommand {
    LeasedCommand {
        accel: [0.0, 0.0, 0.5],
        angular_accel: [0.01, 0.0, 0.0],
        lease_sim_s,
        source_time_s,
        controller_fence: None,
    }
}

#[test]
fn leased_actuation_interlock_requires_navigation_and_one_ready_radar_but_accepts_zero() {
    for target in [
        SensorFaultTarget::Navigation,
        SensorFaultTarget::RadarBus,
        SensorFaultTarget::AllAvionics,
    ] {
        let mut sim = simulation(0);
        sim.set_sensor_fault(target, SensorFaultMode::Unavailable, Some(1.0))
            .unwrap();
        let before = serde_json::to_value(sim.snapshot()).unwrap();
        assert!(matches!(
            sim.set_leased_command(lease(0.0, 0.2)),
            Err(LeaseError::Unavailable)
        ));
        assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
        sim.set_leased_command(LeasedCommand {
            accel: [0.0; 3],
            angular_accel: [0.0; 3],
            ..lease(0.0, 0.2)
        })
        .unwrap();
        assert_eq!(sim.observation()["command_watchdog"]["status"], "active");
        assert_eq!(sim.observation()["command_watchdog"]["source_time_s"], 0.0);
        assert_eq!(
            sim.observation()["command_watchdog"]["expires_at_sim_time_s"],
            0.2
        );
    }
    let mut sim = simulation(0);
    sim.set_sensor_fault(
        SensorFaultTarget::RadarPrimary,
        SensorFaultMode::Unavailable,
        Some(1.0),
    )
    .unwrap();
    sim.set_leased_command(lease(0.0, 0.2)).unwrap();
    assert_eq!(sim.observation()["command_watchdog"]["status"], "active");
}

#[test]
fn backup_scans_real_bodies_with_a_separate_replayable_rng_and_no_hidden_fields() {
    let mut sim = simulation(12);
    let observation = sim.observation();
    assert_eq!(observation["radar"].as_array().unwrap().len(), 12);
    assert_eq!(observation["radar_backup"].as_array().unwrap().len(), 12);
    assert_ne!(observation["radar"], observation["radar_backup"]);
    for id in ["radar_primary", "radar_backup"] {
        assert_eq!(health(&observation, id)["delivery"], "ready");
        assert_eq!(health(&observation, id)["sample_time_s"], 0.0);
    }
    assert!(observation.get("assets").is_none());
    assert!(
        !observation["radar_backup"]
            .to_string()
            .contains("relative_velocity")
    );
    let archived = sim.snapshot();
    let before = serde_json::to_value(&archived).unwrap();
    assert_eq!(sim.observation(), observation);
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
    let mut restored = Simulation::from_state(archived);
    for _ in 0..4 {
        sim.advance(0.05);
        restored.advance(0.05);
    }
    assert_eq!(sim.observation()["radar"], restored.observation()["radar"]);
    assert_eq!(
        sim.observation()["radar_backup"],
        restored.observation()["radar_backup"]
    );
    assert_eq!(sim.state.rng_state, restored.state.rng_state);
    assert_eq!(
        sim.state.sensor_state.as_ref().unwrap().backup_rng_state,
        restored
            .state
            .sensor_state
            .as_ref()
            .unwrap()
            .backup_rng_state
    );
}

#[test]
fn delivery_faults_do_not_change_primary_noise_draws_world_dynamics_or_backup_noise() {
    let mut nominal = simulation(12);
    let mut faulted = simulation(12);
    let initial = faulted.observation()["radar"].clone();
    faulted
        .set_sensor_fault(
            SensorFaultTarget::RadarPrimary,
            SensorFaultMode::Frozen,
            Some(1.0),
        )
        .unwrap();
    for _ in 0..4 {
        nominal.advance(0.05);
        faulted.advance(0.05);
    }
    assert_eq!(faulted.observation()["radar"], initial);
    assert_eq!(
        health(&faulted.observation(), "radar_primary")["sample_time_s"],
        0.0
    );
    assert_ne!(nominal.observation()["radar"], initial);
    assert_eq!(
        serde_json::to_value(&nominal.state.observations).unwrap(),
        serde_json::to_value(&faulted.state.observations).unwrap()
    );
    assert_eq!(nominal.state.rng_state, faulted.state.rng_state);
    assert_eq!(
        serde_json::to_value(&nominal.state.assets).unwrap(),
        serde_json::to_value(&faulted.state.assets).unwrap()
    );
    assert_eq!(
        nominal.observation()["radar_backup"],
        faulted.observation()["radar_backup"]
    );
}

#[test]
fn overlap_precedence_and_recovery_wait_for_an_actual_acquisition() {
    let mut sim = simulation(3);
    sim.set_sensor_fault(
        SensorFaultTarget::RadarPrimary,
        SensorFaultMode::Frozen,
        Some(1.0),
    )
    .unwrap();
    sim.set_sensor_fault(
        SensorFaultTarget::RadarBus,
        SensorFaultMode::Unavailable,
        Some(0.05),
    )
    .unwrap();
    let revision = sim.sensor_revision();
    assert_eq!(
        health(&sim.observation(), "radar_primary")["delivery"],
        "unavailable"
    );
    assert_eq!(sim.observation()["radar_backup"], json!([]));
    sim.advance(0.05);
    assert_eq!(sim.sensor_revision(), revision + 1);
    assert_eq!(
        health(&sim.observation(), "radar_primary")["delivery"],
        "frozen"
    );
    assert_eq!(
        health(&sim.observation(), "radar_backup")["delivery"],
        "waiting_for_sample"
    );
    sim.set_sensor_fault(
        SensorFaultTarget::RadarPrimary,
        SensorFaultMode::Nominal,
        None,
    )
    .unwrap();
    assert_eq!(sim.observation()["radar"], json!([]));
    let before = sim.state.rng_state;
    let revision = sim.sensor_revision();
    sim.observation();
    assert_eq!(sim.state.rng_state, before);
    assert_eq!(sim.sensor_revision(), revision);
    sim.advance(0.05);
    assert_eq!(
        health(&sim.observation(), "radar_primary")["delivery"],
        "ready"
    );
    assert_eq!(
        health(&sim.observation(), "radar_backup")["delivery"],
        "ready"
    );
}

#[test]
fn genuine_empty_scans_are_ready_and_are_not_outages() {
    let mut sim = simulation(0);
    assert_eq!(sim.observation()["radar"], json!([]));
    assert_eq!(sim.observation()["radar_backup"], json!([]));
    for id in ["radar_primary", "radar_backup"] {
        assert_eq!(health(&sim.observation(), id)["delivery"], "ready");
        assert_eq!(health(&sim.observation(), id)["sequence"], 1);
    }
    sim.set_sensor_fault(
        SensorFaultTarget::RadarBus,
        SensorFaultMode::Unavailable,
        Some(0.2),
    )
    .unwrap();
    assert_eq!(
        health(&sim.observation(), "radar_backup")["delivery"],
        "unavailable"
    );
}

#[test]
fn all_avionics_really_withholds_navigation_and_both_radars_without_stopping_physics() {
    let mut sim = simulation(3);
    sim.set_command([0.0, 0.0, 0.5], [0.0; 3]).unwrap();
    sim.set_sensor_fault(
        SensorFaultTarget::AllAvionics,
        SensorFaultMode::Unavailable,
        Some(1.0),
    )
    .unwrap();
    sim.advance(0.15);
    let report = sim.observation();
    for field in [
        "shuttle",
        "target",
        "applied_physics",
        "navigation_sample_time_s",
    ] {
        assert!(report[field].is_null(), "{field}");
    }
    assert!(report["geometry"]["terrain_radius_at_shuttle_m"].is_null());
    assert!(report["mission"]["target_distance_m"].is_null());
    assert_eq!(report["radar"], json!([]));
    assert_eq!(report["radar_backup"], json!([]));
    for unit in report["sensor_health"]["units"].as_array().unwrap() {
        assert_eq!(unit["delivery"], "unavailable");
    }
    assert!(sim.state.time_s > 0.0);
    assert!(sim.state.mission.fuel_used_kg > 0.0);
    assert_eq!(ship(&sim).commanded_accel_mps2, [0.0, 0.0, 0.5]);
}

#[test]
fn navigation_delivery_tracks_physics_between_radar_ticks_without_read_side_effects() {
    let mut sim = simulation(3);
    let before = sim.observation();
    sim.advance(0.05);
    let after = sim.observation();
    assert_eq!(after["navigation_sample_time_s"], sim.state.time_s);
    assert_eq!(
        health(&after, "navigation")["sample_time_s"],
        sim.state.time_s
    );
    assert!(
        health(&after, "navigation")["sequence"].as_u64().unwrap()
            > health(&before, "navigation")["sequence"].as_u64().unwrap()
    );
    assert_eq!(health(&after, "radar_primary")["sample_time_s"], 0.0);
    assert_eq!(sim.observation(), after);
}

#[test]
fn frozen_navigation_keeps_authentic_pose_physics_and_original_time() {
    let mut sim = simulation(0);
    let initial = sim.observation();
    sim.set_sensor_fault(
        SensorFaultTarget::Navigation,
        SensorFaultMode::Frozen,
        Some(1.0),
    )
    .unwrap();
    sim.set_command([0.0, 0.0, 0.5], [0.0; 3]).unwrap();
    sim.advance(0.2);
    let report = sim.observation();
    assert_eq!(report["shuttle"], initial["shuttle"]);
    assert_eq!(report["target"], initial["target"]);
    assert_eq!(report["applied_physics"], initial["applied_physics"]);
    assert_eq!(report["navigation_sample_time_s"], 0.0);
    assert_eq!(health(&report, "navigation")["delivery"], "frozen");
    assert_ne!(
        serde_json::to_value(ship(&sim).velocity).unwrap(),
        initial["shuttle"]["velocity"]
    );
}

#[test]
fn frozen_navigation_cannot_conceal_a_real_terminal_collision() {
    let mut sim = simulation(0);
    sim.set_command([0.0, 0.0, 0.5], [0.0; 3]).unwrap();
    sim.advance(8.0);
    let sample_time = sim.state.time_s;
    sim.set_sensor_fault(
        SensorFaultTarget::Navigation,
        SensorFaultMode::Frozen,
        Some(30.0),
    )
    .unwrap();
    let lander = sim.handles[&EntityId::Lander];
    let pad = sim.handles[&EntityId::LandingPad];
    let up = *sim.bodies[pad].rotation() * Vector::Z;
    let mass = sim.bodies[lander].mass();
    // Apply a real impulse and let Rapier produce the collision and damage.
    // No mission result, contact response or sensor sample is assigned here.
    sim.bodies[lander].apply_impulse(-up * mass * 60.0, true);
    sim.advance(2.0);
    assert_eq!(sim.state.mission.status, MissionStatus::FailedDamage);
    let report = sim.observation();
    assert_eq!(report["mission"]["status"], "failed_damage");
    assert_eq!(report["navigation_sample_time_s"], sample_time);
    assert_eq!(health(&report, "navigation")["delivery"], "frozen");
}

#[test]
fn lease_expiry_clears_only_actuation_and_real_steps_continue() {
    let mut sim = simulation(0);
    sim.set_leased_command(lease(0.0, 0.1)).unwrap();
    sim.advance(0.1);
    let fuel = sim.state.mission.fuel_used_kg;
    let velocity = ship(&sim).velocity;
    assert!(fuel > 0.0);
    assert!(norm(velocity) > 0.0);
    assert_eq!(ship(&sim).commanded_accel_mps2, [0.0; 3]);
    assert_eq!(ship(&sim).commanded_angular_accel_rad_s2, [0.0; 3]);
    assert_eq!(sim.observation()["command_watchdog"]["status"], "expired");
    sim.advance(0.2);
    assert!(sim.state.time_s >= 0.3 - 1e-12);
    assert_eq!(sim.state.mission.fuel_used_kg, fuel);
    assert!(
        norm(ship(&sim).velocity) > 0.0,
        "expiry does not brake the real body"
    );
    assert_eq!(sim.state.manual_interventions, 0);
}

#[test]
fn old_or_future_decisions_and_invalid_faults_are_atomic() {
    let mut sim = simulation(0);
    sim.advance(0.2);
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    assert!(matches!(
        sim.set_leased_command(lease(0.0, 0.1)),
        Err(LeaseError::Expired)
    ));
    assert!(matches!(
        sim.set_leased_command(lease(1.0, 0.1)),
        Err(LeaseError::Rejected(_))
    ));
    for duration in [
        None,
        Some(f64::NAN),
        Some(f64::INFINITY),
        Some(0.0),
        Some(301.0),
    ] {
        assert!(
            sim.set_sensor_fault(
                SensorFaultTarget::AllAvionics,
                SensorFaultMode::Unavailable,
                duration
            )
            .is_err()
        );
        assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
    }
}

#[test]
fn fault_revisions_revoke_a_lease_but_later_manual_and_legacy_commands_survive_expiry() {
    for manual in [false, true] {
        let mut sim = simulation(0);
        sim.set_leased_command(lease(0.0, 1.0)).unwrap();
        sim.set_sensor_fault(
            SensorFaultTarget::RadarBackup,
            SensorFaultMode::Frozen,
            Some(0.05),
        )
        .unwrap();
        assert_eq!(ship(&sim).commanded_accel_mps2, [0.0; 3]);
        assert_eq!(
            sim.observation()["command_watchdog"]["status"],
            "sensor_changed"
        );
        assert_eq!(sim.state.time_s, 0.0);
        assert_eq!(sim.state.mission.fuel_used_kg, 0.0);
        if manual {
            sim.set_manual_thrust([0.0, 0.0, 0.2]).unwrap();
        } else {
            sim.set_command([0.0, 0.0, 0.2], [0.0; 3]).unwrap();
        }
        sim.advance(0.1);
        assert_eq!(ship(&sim).commanded_accel_mps2, [0.0, 0.0, 0.2]);
        assert_eq!(sim.observation()["command_watchdog"]["status"], "idle");
    }
}

#[test]
fn restore_keeps_faults_caches_and_rng_but_never_rearms_an_archived_lease() {
    let mut sim = simulation(3);
    sim.set_sensor_fault(
        SensorFaultTarget::RadarPrimary,
        SensorFaultMode::Frozen,
        Some(1.0),
    )
    .unwrap();
    sim.set_leased_command(lease(0.0, 1.0)).unwrap();
    let archived = sim.snapshot();
    let rng = archived.rng_state;
    let sensor = archived.sensor_state.as_ref().unwrap();
    let backup_rng = sensor.backup_rng_state;
    let faults = sensor.faults.clone();
    sim.execute(PhysicsCommand::Restore {
        state: Box::new(archived),
    })
    .unwrap();
    assert_eq!(sim.state.rng_state, rng);
    assert_eq!(
        sim.state.sensor_state.as_ref().unwrap().backup_rng_state,
        backup_rng
    );
    assert_eq!(sim.state.sensor_state.as_ref().unwrap().faults, faults);
    assert_eq!(ship(&sim).commanded_accel_mps2, [0.0; 3]);
    assert_eq!(sim.observation()["command_watchdog"]["status"], "restored");
    sim.set_command([0.0, 0.0, 0.2], [0.0; 3]).unwrap();
    let mut legacy = sim.snapshot();
    legacy.sensor_state = None;
    let primary = serde_json::to_value(&legacy.observations).unwrap();
    sim.execute(PhysicsCommand::Restore {
        state: Box::new(legacy),
    })
    .unwrap();
    assert_eq!(ship(&sim).commanded_accel_mps2, [0.0, 0.0, 0.2]);
    assert_eq!(
        serde_json::to_value(&sim.state.observations).unwrap(),
        primary
    );
    assert!(sim.state.sensor_state.is_none());
    sim.advance(0.1);
    assert_eq!(
        health(&sim.observation(), "radar_backup")["delivery"],
        "ready"
    );
}

#[test]
fn placement_invalidates_both_caches_without_changing_nominal_live_navigation() {
    let mut sim = simulation(0);
    let rng = sim.state.rng_state;
    let backup_rng = sim.state.sensor_state.as_ref().unwrap().backup_rng_state;
    let position = [crate::model::phobos_enclosing_radius_m() + 1500.0, 0.0, 0.0];
    sim.place_lander(position).unwrap();
    assert!(
        sim.state
            .sensor_state
            .as_ref()
            .unwrap()
            .backup
            .sample
            .is_none()
    );
    assert!(
        sim.state
            .sensor_state
            .as_ref()
            .unwrap()
            .navigation
            .sample
            .is_none()
    );
    assert_eq!(sim.observation()["shuttle"]["position"], json!(position));
    assert_eq!(sim.state.rng_state, rng);
    assert_eq!(
        sim.state.sensor_state.as_ref().unwrap().backup_rng_state,
        backup_rng
    );
    sim.set_sensor_fault(
        SensorFaultTarget::AllAvionics,
        SensorFaultMode::Frozen,
        Some(1.0),
    )
    .unwrap();
    assert!(sim.observation()["shuttle"].is_null());
    assert_eq!(
        health(&sim.observation(), "navigation")["delivery"],
        "waiting_for_sample"
    );
    sim.reset(100).unwrap();
    assert!(sim.state.sensor_state.as_ref().unwrap().faults.is_empty());
    assert_eq!(sim.sensor_revision(), 0);
}

fn claim(sim: &mut Simulation, run: &str, controller: &str, duration: f64) -> ControlFence {
    let authority = sim.control_authority();
    sim.claim_control(ControlClaim {
        run_id: run.into(),
        controller_id: controller.into(),
        expected_epoch: authority.epoch,
        expected_term: authority.term,
        source_time_s: sim.state.time_s,
        lease_sim_s: duration,
    })
    .unwrap();
    sim.control_authority().fence().unwrap()
}

fn fenced_lease(sim: &Simulation, fence: &ControlFence, duration: f64) -> LeasedCommand {
    LeasedCommand {
        controller_fence: Some(fence.clone()),
        ..lease(sim.state.time_s, duration)
    }
}

#[test]
fn actual_authority_excludes_a_healthy_leader_then_fences_late_primary_writes() {
    let mut sim = simulation(0);
    assert_eq!(
        sim.observation()["control_authority"],
        json!({
            "epoch": 0, "term": 0, "status": "idle", "run_id": null,
            "controller_id": null, "expires_at_sim_time_s": null,
        })
    );
    let primary = claim(&mut sim, "run_1", "primary", 0.1);
    sim.set_leased_command(fenced_lease(&sim, &primary, 0.1))
        .unwrap();
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    assert!(matches!(
        sim.claim_control(ControlClaim {
            run_id: "run_1".into(),
            controller_id: "standby".into(),
            expected_epoch: primary.epoch,
            expected_term: primary.term,
            source_time_s: 0.0,
            lease_sim_s: 0.2,
        }),
        Err(LeaseError::StaleContext)
    ));
    assert!(matches!(
        sim.set_leased_command(lease(0.0, 0.2)),
        Err(LeaseError::StaleContext)
    ));
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
    sim.advance(0.1);
    let expired = sim.control_authority();
    assert_eq!(expired.status, ControlAuthorityStatus::Expired);
    assert_eq!(expired.epoch, primary.epoch);
    assert_eq!(expired.run_id.as_deref(), Some("run_1"));
    assert_eq!(ship(&sim).commanded_accel_mps2, [0.0; 3]);
    assert!(matches!(
        sim.set_leased_command(lease(sim.state.time_s, 0.2)),
        Err(LeaseError::StaleContext)
    ));
    let standby = claim(&mut sim, "run_1", "standby", 0.2);
    assert_eq!(standby.epoch, primary.epoch);
    assert_eq!(standby.term, primary.term + 1);
    sim.set_leased_command(fenced_lease(&sim, &standby, 0.3))
        .unwrap();
    assert_eq!(
        sim.control_authority().expires_at_sim_time_s,
        Some(sim.state.time_s + 0.3)
    );
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    // A fresh source timestamp and observation do not repair an old owner's fence.
    assert!(matches!(
        sim.set_leased_command(fenced_lease(&sim, &primary, 0.3)),
        Err(LeaseError::StaleContext)
    ));
    for fence in [
        ControlFence {
            epoch: standby.epoch + 1,
            ..standby.clone()
        },
        ControlFence {
            controller_id: "primary".into(),
            ..standby.clone()
        },
        ControlFence {
            run_id: "another-run".into(),
            ..standby.clone()
        },
    ] {
        assert!(matches!(
            sim.set_leased_command(fenced_lease(&sim, &fence, 0.3)),
            Err(LeaseError::StaleContext)
        ));
    }
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
}

#[test]
fn manual_takeover_revokes_original_consent_without_late_autonomous_zeroing() {
    for manual in [false, true] {
        let mut sim = simulation(0);
        let primary = claim(&mut sim, "run", "primary", 0.1);
        sim.set_leased_command(fenced_lease(&sim, &primary, 0.1))
            .unwrap();
        if manual {
            sim.set_manual_thrust([0.0, 0.0, 0.2]).unwrap();
        } else {
            sim.set_command([0.0, 0.0, 0.2], [0.0; 3]).unwrap();
        }
        let revoked = sim.control_authority();
        assert_eq!(revoked.status, ControlAuthorityStatus::Revoked);
        assert_eq!(revoked.epoch, primary.epoch + 1);
        let before = serde_json::to_value(sim.snapshot()).unwrap();
        assert!(matches!(
            sim.claim_control(ControlClaim {
                run_id: "run".into(),
                controller_id: "standby".into(),
                expected_epoch: primary.epoch,
                expected_term: revoked.term,
                source_time_s: sim.state.time_s,
                lease_sim_s: 0.2,
            }),
            Err(LeaseError::StaleContext)
        ));
        assert!(matches!(
            sim.release_control(ControlRelease {
                run_id: "run".into(),
                expected_epoch: primary.epoch,
            }),
            Err(LeaseError::StaleContext)
        ));
        assert!(matches!(
            sim.set_leased_command(lease(sim.state.time_s, 0.2)),
            Err(LeaseError::StaleContext)
        ));
        assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
        sim.advance(0.2);
        assert_eq!(ship(&sim).commanded_accel_mps2, [0.0, 0.0, 0.2]);
        assert_eq!(sim.control_authority(), revoked);
    }
}

#[test]
fn sensor_faults_clear_force_without_revoking_consent_and_zero_renewals_remain_available() {
    let mut sim = simulation(0);
    let primary = claim(&mut sim, "run", "primary", 0.2);
    sim.set_leased_command(fenced_lease(&sim, &primary, 0.2))
        .unwrap();
    sim.set_sensor_fault(
        SensorFaultTarget::AllAvionics,
        SensorFaultMode::Unavailable,
        Some(0.1),
    )
    .unwrap();
    assert_eq!(sim.control_authority().fence(), Some(primary.clone()));
    assert_eq!(
        sim.control_authority().status,
        ControlAuthorityStatus::Active
    );
    assert_eq!(ship(&sim).commanded_accel_mps2, [0.0; 3]);
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    assert!(matches!(
        sim.set_leased_command(fenced_lease(&sim, &primary, 0.3)),
        Err(LeaseError::Unavailable)
    ));
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
    sim.set_leased_command(LeasedCommand {
        accel: [0.0; 3],
        angular_accel: [0.0; 3],
        ..fenced_lease(&sim, &primary, 0.3)
    })
    .unwrap();
    assert_eq!(sim.control_authority().expires_at_sim_time_s, Some(0.3));
    sim.advance(0.1);
    assert_eq!(sim.control_authority().fence(), Some(primary));
    assert_eq!(
        sim.control_authority().status,
        ControlAuthorityStatus::Active
    );
}

#[test]
fn cohort_release_stops_its_current_member_without_pausing_braking_or_clearing_a_new_run() {
    let mut sim = simulation(0);
    let primary = claim(&mut sim, "run", "primary", 0.1);
    sim.set_leased_command(fenced_lease(&sim, &primary, 0.1))
        .unwrap();
    sim.advance(0.1);
    let standby = claim(&mut sim, "run", "standby", 0.2);
    sim.set_leased_command(fenced_lease(&sim, &standby, 0.2))
        .unwrap();
    sim.advance(0.05);
    let before = sim.snapshot();
    let velocity = ship(&sim).velocity;
    sim.release_control(ControlRelease {
        run_id: "run".into(),
        expected_epoch: primary.epoch,
    })
    .unwrap();
    assert_eq!(ship(&sim).velocity, velocity);
    assert_eq!(ship(&sim).commanded_accel_mps2, [0.0; 3]);
    assert_eq!(sim.state.time_s, before.time_s);
    assert_eq!(sim.state.mission.fuel_used_kg, before.mission.fuel_used_kg);
    assert_eq!(sim.state.manual_interventions, before.manual_interventions);
    assert_eq!(sim.control_authority().epoch, primary.epoch + 1);
    let newer = claim(&mut sim, "new-run", "new-primary", 0.2);
    sim.set_leased_command(fenced_lease(&sim, &newer, 0.2))
        .unwrap();
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    assert!(matches!(
        sim.release_control(ControlRelease {
            run_id: "run".into(),
            expected_epoch: newer.epoch,
        }),
        Err(LeaseError::StaleContext)
    ));
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
}

#[test]
fn restore_and_reset_cannot_resurrect_archived_or_current_controller_authority() {
    let mut sim = simulation(0);
    let primary = claim(&mut sim, "run", "primary", 0.2);
    sim.set_leased_command(fenced_lease(&sim, &primary, 0.2))
        .unwrap();
    let archived = sim.snapshot();
    sim.execute(PhysicsCommand::Restore {
        state: Box::new(archived.clone()),
    })
    .unwrap();
    assert_eq!(
        sim.control_authority().status,
        ControlAuthorityStatus::Revoked
    );
    assert!(sim.control_authority().epoch > primary.epoch);
    assert_eq!(ship(&sim).commanded_accel_mps2, [0.0; 3]);
    let next = claim(&mut sim, "new-run", "primary", 0.2);
    sim.reset(100).unwrap();
    assert_eq!(sim.control_authority().epoch, next.epoch + 1);
    assert_eq!(
        sim.control_authority().status,
        ControlAuthorityStatus::Revoked
    );
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    assert!(matches!(
        sim.set_leased_command(fenced_lease(&sim, &next, 0.2)),
        Err(LeaseError::StaleContext)
    ));
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
    let mut other = simulation(0);
    other
        .execute(PhysicsCommand::Restore {
            state: Box::new(archived),
        })
        .unwrap();
    assert_eq!(
        other.control_authority().status,
        ControlAuthorityStatus::Revoked
    );
    assert_eq!(ship(&other).commanded_accel_mps2, [0.0; 3]);
}

#[test]
fn claim_source_deadlines_ids_and_counter_capacity_fail_before_any_mutation() {
    let mut sim = simulation(0);
    sim.advance(0.2);
    let request = ControlClaim {
        run_id: "run".into(),
        controller_id: "primary".into(),
        expected_epoch: 0,
        expected_term: 0,
        source_time_s: sim.state.time_s,
        lease_sim_s: 0.2,
    };
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    assert!(matches!(
        sim.claim_control(ControlClaim {
            source_time_s: 0.0,
            lease_sim_s: 0.1,
            ..request.clone()
        }),
        Err(LeaseError::Expired)
    ));
    for invalid in [
        ControlClaim {
            run_id: "".into(),
            ..request.clone()
        },
        ControlClaim {
            controller_id: "not/a/process".into(),
            ..request.clone()
        },
        ControlClaim {
            run_id: "x".repeat(65),
            ..request.clone()
        },
        ControlClaim {
            source_time_s: 1.0,
            ..request.clone()
        },
        ControlClaim {
            lease_sim_s: 1.01,
            ..request.clone()
        },
        ControlClaim {
            expected_term: 1,
            ..request.clone()
        },
    ] {
        assert!(sim.claim_control(invalid).is_err());
        assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
    }
    for (epoch, term) in [(0, u32::MAX), (u32::MAX, 0)] {
        sim.sensor_state_mut().control_authority = Some(ControlAuthority {
            epoch,
            term,
            status: ControlAuthorityStatus::Revoked,
            run_id: Some("previous-run".into()),
            controller_id: None,
            expires_at_sim_time_s: None,
        });
        let before = serde_json::to_value(sim.snapshot()).unwrap();
        assert!(
            sim.claim_control(ControlClaim {
                expected_epoch: epoch,
                expected_term: term,
                ..request.clone()
            })
            .is_err()
        );
        assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
    }
}
