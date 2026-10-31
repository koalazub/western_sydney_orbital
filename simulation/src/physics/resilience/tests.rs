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
fn transit_radars_select_a_near_body_beyond_the_first_200_without_dropping_physical_bodies() {
    let mut state = simulation(500).snapshot();
    let shuttle = state
        .assets
        .iter()
        .find(|entity| entity.id == EntityId::Lander)
        .unwrap()
        .clone();
    let near_position = [80.0, 60.0, 0.0];
    let near_velocity = [-8.0, -6.0, 0.0];
    let mut near_id = None;
    let mut near_radius = 0.0;
    // Controlled native world geometry, not assigned radar rows. Acquisition
    // below must produce both measurements through their genuine noise paths.
    for (index, debris) in state
        .assets
        .iter_mut()
        .filter(|entity| entity.kind == EntityKind::Debris)
        .enumerate()
    {
        let delta = if index == 400 {
            near_id = Some(debris.id);
            near_radius = debris.radius_m;
            debris.velocity =
                std::array::from_fn(|axis| shuttle.velocity[axis] + near_velocity[axis]);
            near_position
        } else {
            [10_000.0 + index as f64 * 100.0, 1_000.0, 1_000.0]
        };
        debris.position = std::array::from_fn(|axis| shuttle.position[axis] + delta[axis]);
    }
    let mut sim = Simulation::from_state(state);
    let before_assets = serde_json::to_value(&sim.state.assets).unwrap();
    let before_handles = sim.handles.clone();
    let before_colliders = sim.collider_ids.clone();
    sim.sample_radar();
    assert_eq!(sim.state.observations.first().unwrap().id, near_id.unwrap());
    assert_eq!(
        serde_json::to_value(&sim.state.assets).unwrap(),
        before_assets
    );
    assert_eq!(sim.handles, before_handles);
    assert_eq!(sim.collider_ids, before_colliders);
    assert_eq!(
        sim.state
            .assets
            .iter()
            .filter(|entity| entity.kind == EntityKind::Debris)
            .count(),
        500
    );
    assert_eq!(sim.bodies.len(), 504);
    assert_eq!(sim.colliders.len(), 504);
    let report = sim.observation();
    assert_eq!(
        report["radar_selection"]["selection"],
        "nearest_noisy_centers"
    );
    assert_eq!(report["radar_selection"]["total_physical_body_count"], 500);
    assert_eq!(
        report["radar_selection"]["unobserved_count_lower_bound"],
        300
    );
    assert_ne!(report["radar"], report["radar_backup"]);
    let cfg = settings();
    for (field, unit) in [("radar", "radar_primary"), ("radar_backup", "radar_backup")] {
        let rows: Vec<RadarRow> = serde_json::from_value(report[field].clone()).unwrap();
        assert_eq!(rows.len(), 200);
        assert!(
            rows.windows(2)
                .all(|pair| norm(pair[0][0]) <= norm(pair[1][0]))
        );
        let near = rows.first().unwrap();
        assert!(norm(sub(near[0], near_position)) < 10.0 * cfg.sensors.range_noise_m);
        assert_ne!(near[0], near_position);
        assert!((near[1][0] - (100.0 - near_radius)).abs() < 10.0 * cfg.sensors.range_noise_m);
        assert!((near[1][1] + 10.0).abs() < 10.0 * cfg.sensors.range_rate_noise_mps);
        assert_ne!(near[1][1], -10.0);
        assert!(
            rows.iter()
                .all(|row| row[2] == [sim.state.time_s, 0.0, 0.0])
        );
        assert_eq!(health(&report, unit)["delivery"], "ready");
        assert_eq!(health(&report, unit)["sample_time_s"], sim.state.time_s);
        assert!(
            health(&report, unit)["provenance"]
                .as_str()
                .unwrap()
                .contains("nearest_noisy_centers")
        );
        assert_eq!(
            health(&report, unit)["noise"]["range_noise_m"],
            cfg.sensors.range_noise_m
        );
        assert!(!report[field].to_string().contains("id"));
        assert!(!report[field].to_string().contains("velocity"));
    }
}

#[test]
fn backup_radar_keeps_exact_noise_draws_and_the_legacy_fixed_subset_for_old_distributions() {
    for distribution in [
        None,
        Some(DebrisDistribution::DirectedStream),
        Some(DebrisDistribution::impact_default()),
        Some(DebrisDistribution::TransitStream),
    ] {
        let experiment = distribution.map(|distribution| {
            let mut experiment = ExperimentSettings::default();
            experiment.debris.distribution = distribution;
            experiment
        });
        let sim = Simulation::with_profile(100, experiment).unwrap();
        let state = sim.snapshot();
        let shuttle = ship(&sim);
        let cfg = settings();
        let candidate_count = if distribution == Some(DebrisDistribution::TransitStream) {
            500
        } else {
            cfg.debris.radar_count
        };
        let mut actual_rng = state.sensor_state.as_ref().unwrap().backup_rng_state;
        let mut expected_rng = actual_rng;
        let mut expected = Vec::new();
        for debris in state
            .assets
            .iter()
            .filter(|entity| entity.kind == EntityKind::Debris)
            .take(candidate_count)
        {
            let delta = sub(debris.position, shuttle.position);
            let range = norm(delta);
            let position = std::array::from_fn(|axis| {
                delta[axis] + gaussian(&mut expected_rng) * cfg.sensors.range_noise_m
            });
            let rate = dot(sub(debris.velocity, shuttle.velocity), delta) / range
                + gaussian(&mut expected_rng) * cfg.sensors.range_rate_noise_mps;
            let closest =
                range - debris.radius_m + gaussian(&mut expected_rng) * cfg.sensors.range_noise_m;
            let diameter = 2.0
                * debris.radius_m
                * (1.0 + gaussian(&mut expected_rng) * cfg.debris.diameter_noise_fraction);
            expected.push([
                position,
                [closest, rate, diameter],
                [state.time_s, 0.0, 0.0],
            ]);
        }
        assert_eq!(expected.len(), candidate_count);
        expected.sort_by(|a, b| norm(a[0]).total_cmp(&norm(b[0])));
        expected.truncate(cfg.debris.radar_count);
        assert_eq!(sample_backup(&state, &mut actual_rng), expected);
        assert_eq!(actual_rng, expected_rng);
        if distribution != Some(DebrisDistribution::TransitStream) {
            assert_eq!(
                sim.observation()["radar_selection"]["selection"],
                "legacy_fixed_subset"
            );
            assert_eq!(
                health(&sim.observation(), "radar_primary")["provenance"],
                "native_world_radar_primary_legacy_rng"
            );
            assert_eq!(
                health(&sim.observation(), "radar_backup")["provenance"],
                "native_world_radar_backup_independent_rng_v1"
            );
        }
    }
}

fn fixed_subset_transit_snapshot() -> Snapshot {
    let mut experiment = ExperimentSettings::default();
    experiment.debris.count = 500;
    experiment.debris.distribution = DebrisDistribution::TransitStream;
    let mut sim = Simulation::from_state(initial(100, Some(experiment)).unwrap());
    let assets = serde_json::to_value(&sim.state.assets).unwrap();
    // Exercise the genuine historical acquisition path on the transit world.
    // Only the selector changes temporarily, not bodies, noise or returned rows.
    sim.state
        .experiment_settings
        .as_mut()
        .unwrap()
        .debris
        .distribution = DebrisDistribution::DirectedStream;
    sim.sample_radar();
    sim.state.experiment_settings = Some(experiment);
    sim.state.rng_state = sim.rng;
    assert_eq!(serde_json::to_value(&sim.state.assets).unwrap(), assets);
    assert_eq!(sim.state.observations.len(), 200);
    assert!(
        sim.state
            .observations
            .iter()
            .all(|row| matches!(row.id, EntityId::Debris(id) if id <= 200))
    );
    let report = sim.observation();
    assert_eq!(
        report["radar_selection"]["selection"],
        "legacy_fixed_subset"
    );
    assert_eq!(
        health(&report, "radar_primary")["provenance"],
        "native_world_radar_primary_legacy_rng"
    );
    assert_eq!(
        health(&report, "radar_backup")["provenance"],
        "native_world_radar_backup_independent_rng_v1"
    );
    assert_ne!(report["radar"], report["radar_backup"]);
    sim.snapshot()
}

fn strip_radar_selection_fields(archive: &mut Value) {
    for channel in ["primary", "backup"] {
        assert!(
            archive["sensor_state"][channel]["sample"]
                .as_object_mut()
                .unwrap()
                .remove("selection")
                .is_some()
        );
    }
}

#[test]
fn unversioned_transit_restore_keeps_original_rows_clocks_assets_and_rng_until_real_sampling() {
    let original = serde_json::to_value(fixed_subset_transit_snapshot()).unwrap();
    let period_s = 1.0 / settings().sensors.cadence_hz;
    for has_delivery_caches in [true, false] {
        let mut archive = original.clone();
        if has_delivery_caches {
            strip_radar_selection_fields(&mut archive);
        } else {
            archive.as_object_mut().unwrap().remove("sensor_state");
        }
        let archived_state: Snapshot = serde_json::from_value(archive).unwrap();
        let expected = serde_json::to_value(&archived_state).unwrap();
        let mut sim = simulation(0);
        sim.execute(PhysicsCommand::Restore {
            state: Box::new(archived_state),
        })
        .unwrap();
        // A full-state comparison includes the original noisy samples, clocks,
        // sequences, both RNG streams, contacts, assets and mission state.
        assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), expected);
        assert_eq!(sim.rng, sim.state.rng_state);
        let report = sim.observation();
        assert_eq!(
            report["radar"],
            original["sensor_state"]["primary"]["sample"]["rows"]
        );
        let expected_backup = if has_delivery_caches {
            original["sensor_state"]["backup"]["sample"]["rows"].clone()
        } else {
            json!([])
        };
        assert_eq!(report["radar_backup"], expected_backup);
        assert_eq!(report["radar_selection"]["selection"], "unknown");
        for id in ["radar_primary", "radar_backup"] {
            assert_eq!(health(&report, id)["provenance"], "unknown");
        }
        assert_eq!(sim.observation(), report);
        assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), expected);

        sim.advance(period_s * 0.25);
        let between_ticks = sim.observation();
        assert_eq!(between_ticks["radar"], report["radar"]);
        assert_eq!(between_ticks["radar_backup"], report["radar_backup"]);
        assert_eq!(between_ticks["radar_selection"]["selection"], "unknown");
        assert_eq!(
            serde_json::to_value(&sim.state.observations).unwrap(),
            original["observations"]
        );
        for id in ["radar_primary", "radar_backup"] {
            assert_eq!(health(&between_ticks, id)["provenance"], "unknown");
        }

        sim.advance(period_s);
        let fresh = sim.observation();
        assert_eq!(
            fresh["radar_selection"]["selection"],
            "nearest_noisy_centers"
        );
        assert_eq!(fresh["radar_selection"]["total_physical_body_count"], 500);
        assert_eq!(fresh["radar_selection"]["sample_limit"], 200);
        assert_eq!(
            fresh["radar_selection"]["unobserved_count_lower_bound"],
            300
        );
        assert!(!fresh["radar_selection"].to_string().contains("id"));
        assert!(!fresh["radar_selection"].to_string().contains("velocity"));
        for (field, id) in [("radar", "radar_primary"), ("radar_backup", "radar_backup")] {
            let sensor = health(&fresh, id);
            let sample_time_s = sensor["sample_time_s"].as_f64().unwrap();
            assert!(sample_time_s > 0.0);
            assert_eq!(sensor["delivery"], "ready");
            assert!(
                sensor["provenance"]
                    .as_str()
                    .unwrap()
                    .contains("nearest_noisy_centers")
            );
            let rows = fresh[field].as_array().unwrap();
            assert_eq!(rows.len(), 200);
            assert!(
                rows.iter()
                    .all(|row| row[2] == json!([sample_time_s, 0.0, 0.0]))
            );
            assert!(!fresh[field].to_string().contains("id"));
            assert!(!fresh[field].to_string().contains("velocity"));
        }
        let acquired = serde_json::to_value(sim.snapshot()).unwrap();
        for channel in ["primary", "backup"] {
            assert_eq!(
                acquired["sensor_state"][channel]["sample"]["selection"],
                "nearest_noisy_centers_v1"
            );
        }
        sim.execute(PhysicsCommand::Restore {
            state: Box::new(serde_json::from_value(acquired.clone()).unwrap()),
        })
        .unwrap();
        assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), acquired);
        let restored = sim.observation();
        for field in ["radar", "radar_backup", "radar_selection"] {
            assert_eq!(restored[field], fresh[field]);
        }
    }
}

#[test]
fn frozen_radar_selection_is_independent_of_fresh_peer_acquisition_and_survives_restore() {
    let original = serde_json::to_value(fixed_subset_transit_snapshot()).unwrap();
    let period_s = 1.0 / settings().sensors.cadence_hz;
    for versioned in [true, false] {
        for frozen_primary in [true, false] {
            let mut archive = original.clone();
            if !versioned {
                strip_radar_selection_fields(&mut archive);
            }
            let mut sim = Simulation::from_state(serde_json::from_value(archive).unwrap());
            let (target, channel, field, unit, peer_field, peer_unit) = if frozen_primary {
                (
                    SensorFaultTarget::RadarPrimary,
                    "primary",
                    "radar",
                    "radar_primary",
                    "radar_backup",
                    "radar_backup",
                )
            } else {
                (
                    SensorFaultTarget::RadarBackup,
                    "backup",
                    "radar_backup",
                    "radar_backup",
                    "radar",
                    "radar_primary",
                )
            };
            sim.set_sensor_fault(target, SensorFaultMode::Frozen, Some(1.0))
                .unwrap();
            let frozen_frame = serde_json::to_value(sim.snapshot()).unwrap()["sensor_state"]
                [channel]["sample"]
                .clone();
            sim.advance(period_s * 1.25);
            let report = sim.observation();
            let mixed = serde_json::to_value(sim.snapshot()).unwrap();
            assert_eq!(mixed["sensor_state"][channel]["sample"], frozen_frame);
            assert_eq!(report[field], frozen_frame["rows"]);
            assert_ne!(
                report[peer_field],
                original["sensor_state"][if frozen_primary { "backup" } else { "primary" }]["sample"]
                    ["rows"]
            );
            let frozen = health(&report, unit);
            assert_eq!(frozen["delivery"], "frozen");
            assert_eq!(frozen["sample_time_s"], frozen_frame["sample_time_s"]);
            assert_eq!(frozen["sequence"], frozen_frame["sequence"]);
            let frozen_provenance = if !versioned {
                "unknown"
            } else if frozen_primary {
                "native_world_radar_primary_legacy_rng"
            } else {
                "native_world_radar_backup_independent_rng_v1"
            };
            assert_eq!(frozen["provenance"], frozen_provenance);
            let peer = health(&report, peer_unit);
            assert_eq!(peer["delivery"], "ready");
            assert!(peer["sample_time_s"].as_f64().unwrap() > 0.0);
            assert!(
                peer["provenance"]
                    .as_str()
                    .unwrap()
                    .contains("nearest_noisy_centers")
            );
            let primary_selection = if !frozen_primary {
                "nearest_noisy_centers"
            } else if versioned {
                "legacy_fixed_subset"
            } else {
                "unknown"
            };
            assert_eq!(report["radar_selection"]["selection"], primary_selection);

            let mut restored = simulation(0);
            restored
                .execute(PhysicsCommand::Restore {
                    state: Box::new(serde_json::from_value(mixed.clone()).unwrap()),
                })
                .unwrap();
            assert_eq!(serde_json::to_value(restored.snapshot()).unwrap(), mixed);
            assert_eq!(restored.rng, sim.rng);
            let restored_report = restored.observation();
            for field in ["radar", "radar_backup", "radar_selection"] {
                assert_eq!(restored_report[field], report[field]);
            }
            for id in ["radar_primary", "radar_backup"] {
                assert_eq!(health(&restored_report, id), health(&report, id));
            }

            restored
                .set_sensor_fault(target, SensorFaultMode::Nominal, None)
                .unwrap();
            let waiting = restored.observation();
            assert_eq!(waiting[field], json!([]));
            assert_eq!(health(&waiting, unit)["delivery"], "waiting_for_sample");
            assert_eq!(health(&waiting, unit)["provenance"], "unknown");
            assert_eq!(waiting[peer_field], report[peer_field]);
            assert_eq!(
                waiting["radar_selection"]["selection"],
                if frozen_primary {
                    "unknown"
                } else {
                    "nearest_noisy_centers"
                }
            );
            assert_eq!(
                serde_json::to_value(restored.snapshot()).unwrap()["sensor_state"][channel]["sample"],
                frozen_frame
            );
            restored.advance(period_s * 1.25);
            let recovered = restored.observation();
            assert_eq!(
                recovered["radar_selection"]["selection"],
                "nearest_noisy_centers"
            );
            for id in ["radar_primary", "radar_backup"] {
                let sensor = health(&recovered, id);
                assert_eq!(sensor["delivery"], "ready");
                assert!(
                    sensor["provenance"]
                        .as_str()
                        .unwrap()
                        .contains("nearest_noisy_centers")
                );
                assert!(
                    sensor["sample_time_s"].as_f64().unwrap()
                        > peer["sample_time_s"].as_f64().unwrap()
                );
            }
        }
    }
}

#[test]
fn missing_and_withheld_radar_samples_do_not_claim_current_selection_coverage() {
    let mut sim =
        Simulation::from_state(initial(100, Some(ExperimentSettings::default())).unwrap());
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    let report = sim.observation();
    assert_eq!(report["radar_selection"]["selection"], "unknown");
    for id in ["radar_primary", "radar_backup"] {
        assert_eq!(health(&report, id)["delivery"], "waiting_for_sample");
        assert_eq!(health(&report, id)["provenance"], "unknown");
    }
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
    sim.set_sensor_fault(
        SensorFaultTarget::RadarPrimary,
        SensorFaultMode::Frozen,
        Some(1.0),
    )
    .unwrap();
    sim.advance(1.25 / settings().sensors.cadence_hz);
    let report = sim.observation();
    assert_eq!(report["radar"], json!([]));
    assert_eq!(report["radar_selection"]["selection"], "unknown");
    assert_eq!(health(&report, "radar_primary")["provenance"], "unknown");
    assert_eq!(
        health(&report, "radar_primary")["delivery"],
        "waiting_for_sample"
    );
    assert!(
        health(&report, "radar_backup")["provenance"]
            .as_str()
            .unwrap()
            .contains("nearest_noisy_centers")
    );
    sim.set_sensor_fault(
        SensorFaultTarget::RadarBus,
        SensorFaultMode::Unavailable,
        Some(1.0),
    )
    .unwrap();
    let report = sim.observation();
    assert_eq!(report["radar_selection"]["selection"], "unknown");
    for (field, id) in [("radar", "radar_primary"), ("radar_backup", "radar_backup")] {
        assert_eq!(report[field], json!([]));
        assert_eq!(health(&report, id)["delivery"], "unavailable");
        assert_eq!(health(&report, id)["provenance"], "unknown");
    }
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
    assert_eq!(sim.observation()["radar_selection"]["selection"], "unknown");
    for id in ["radar_primary", "radar_backup"] {
        assert_eq!(health(&sim.observation(), id)["delivery"], "ready");
        assert_eq!(health(&sim.observation(), id)["sequence"], 1);
        assert_eq!(health(&sim.observation(), id)["provenance"], "unknown");
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

fn ownership_renewal(sim: &Simulation, fence: &ControlFence, duration: f64) -> ControlRenewal {
    ControlRenewal {
        controller_fence: fence.clone(),
        source_time_s: sim.state.time_s,
        lease_sim_s: duration,
    }
}

#[test]
fn ownership_capability_is_optional_metadata_outside_the_unchanged_authority_schema() {
    let sim = simulation(0);
    let report = sim.observation();
    assert_eq!(
        report["control_capabilities"]["ownership_renewal"],
        json!({
            "schema_version": 1, "max_lease_sim_s": 0.75,
        })
    );
    assert_eq!(report["control_authority"].as_object().unwrap().len(), 6);
    assert_eq!(report["control_authority"]["status"], "idle");
    assert_eq!(report["command_watchdog"]["status"], "idle");
}

#[test]
fn minimum_ownership_renewal_uses_its_actual_new_source_without_claiming_or_installing_a_command() {
    let mut sim = simulation(0);
    let fence = claim(&mut sim, "run", "primary", 0.05);
    sim.advance(0.02);
    let source = sim.state.time_s;
    let watchdog = sim.observation()["command_watchdog"].clone();
    let receipt = sim
        .renew_control(ownership_renewal(&sim, &fence, 0.05))
        .unwrap();
    assert_eq!(receipt.source_time_s, source);
    assert_eq!(
        receipt.control_authority.expires_at_sim_time_s,
        Some(source + 0.05)
    );
    assert_eq!(receipt.control_authority.fence(), Some(fence));
    assert_eq!(sim.observation()["command_watchdog"], watchdog);
    assert_eq!(ship(&sim).commanded_accel_mps2, [0.0; 3]);
}

#[test]
fn cohorts_without_an_ownership_heartbeat_keep_the_original_direct_actuator_lease_contract() {
    let mut sim = simulation(0);
    let fence = claim(&mut sim, "legacy_run", "primary", 0.75);
    sim.set_leased_command(fenced_lease(&sim, &fence, 0.1))
        .unwrap();
    assert_eq!(sim.control_authority().expires_at_sim_time_s, Some(0.1));
    assert_eq!(
        sim.observation()["command_watchdog"]["expires_at_sim_time_s"],
        0.1
    );
    sim.advance(0.1);
    assert_eq!(
        sim.control_authority().status,
        ControlAuthorityStatus::Expired
    );
    assert_eq!(ship(&sim).commanded_accel_mps2, [0.0; 3]);
}

#[test]
fn ownership_renewal_changes_only_authority_and_the_original_command_expires_normally() {
    let mut sim = simulation(0);
    let primary = claim(&mut sim, "run", "primary", 0.2);
    sim.set_leased_command(fenced_lease(&sim, &primary, 0.2))
        .unwrap();
    sim.advance(0.05);
    let before = sim.snapshot();
    let applied = serde_json::to_value(sim.applied_physics().unwrap()).unwrap();
    let command_runtime_deadline = sim.control_runtime.command_deadline_s;
    let request = ownership_renewal(&sim, &primary, 0.75);
    let receipt = sim.renew_control(request.clone()).unwrap();
    assert_eq!(receipt.source_time_s, before.time_s);
    assert_eq!(receipt.lease_sim_s, 0.75);
    assert_eq!(receipt.sensor_revision, sim.sensor_revision());
    assert_eq!(receipt.control_authority, sim.control_authority());
    assert_eq!(receipt.control_authority.fence(), Some(primary.clone()));
    assert_eq!(
        receipt.control_authority.expires_at_sim_time_s,
        Some(before.time_s + 0.75)
    );
    assert_eq!(
        sim.control_runtime.command_deadline_s,
        command_runtime_deadline
    );
    assert_eq!(
        serde_json::to_value(sim.applied_physics().unwrap()).unwrap(),
        applied
    );
    let mut original = serde_json::to_value(before).unwrap();
    let mut renewed = serde_json::to_value(sim.snapshot()).unwrap();
    original["sensor_state"]["control_authority"].take();
    renewed["sensor_state"]["control_authority"].take();
    assert_eq!(renewed, original, "only native ownership may change");

    let archive = sim.snapshot();
    validate_sensor_state(&archive).unwrap();
    let mut invalid = archive.clone();
    invalid
        .sensor_state
        .as_mut()
        .unwrap()
        .control_authority
        .as_mut()
        .unwrap()
        .expires_at_sim_time_s = Some(0.1);
    assert!(validate_sensor_state(&invalid).is_err());
    let unchanged = serde_json::to_value(sim.snapshot()).unwrap();
    assert!(matches!(
        sim.renew_control(request),
        Err(LeaseError::StaleContext)
    ));
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), unchanged);

    sim.advance(0.2);
    assert_eq!(
        sim.control_authority().status,
        ControlAuthorityStatus::Active
    );
    assert_eq!(sim.control_authority().fence(), Some(primary.clone()));
    let watchdog = &sim.state.sensor_state.as_ref().unwrap().command_watchdog;
    assert_eq!(watchdog.status, CommandWatchdogStatus::Expired);
    assert_eq!(watchdog.source_time_s, Some(0.0));
    assert_eq!(watchdog.expires_at_sim_time_s, Some(0.2));
    assert_eq!(ship(&sim).commanded_accel_mps2, [0.0; 3]);
    assert_eq!(ship(&sim).commanded_angular_accel_rad_s2, [0.0; 3]);
    assert!(sim.state.time_s > 0.2);

    // A separately calculated fresh command still needs normal fenced admission.
    let source = sim.state.time_s;
    sim.set_leased_command(fenced_lease(&sim, &primary, 0.05))
        .unwrap();
    assert_eq!(sim.control_authority().expires_at_sim_time_s, Some(0.8));
    assert_eq!(
        sim.observation()["command_watchdog"]["source_time_s"],
        source
    );
    assert_eq!(
        sim.observation()["command_watchdog"]["expires_at_sim_time_s"],
        source + 0.05
    );
    let mut restored = simulation(0);
    restored
        .execute(PhysicsCommand::Restore {
            state: Box::new(archive),
        })
        .unwrap();
    assert_eq!(
        restored.control_authority().status,
        ControlAuthorityStatus::Revoked
    );
    assert_eq!(ship(&restored).commanded_accel_mps2, [0.0; 3]);
}

#[test]
fn ownership_renewal_rejects_stale_fences_sources_bounds_and_expired_or_revoked_owners_atomically()
{
    let mut sim = simulation(0);
    let primary = claim(&mut sim, "run", "primary", 0.2);
    sim.set_leased_command(fenced_lease(&sim, &primary, 0.2))
        .unwrap();
    sim.advance(0.05);
    let request = ownership_renewal(&sim, &primary, 0.75);
    let mut invalid = vec![
        ControlRenewal {
            lease_sim_s: 0.049,
            ..request.clone()
        },
        ControlRenewal {
            lease_sim_s: 0.750001,
            ..request.clone()
        },
        ControlRenewal {
            lease_sim_s: f64::NAN,
            ..request.clone()
        },
        ControlRenewal {
            lease_sim_s: f64::INFINITY,
            ..request.clone()
        },
        ControlRenewal {
            source_time_s: -1.0,
            ..request.clone()
        },
        ControlRenewal {
            source_time_s: sim.state.time_s + 0.01,
            ..request.clone()
        },
        ControlRenewal {
            source_time_s: f64::NAN,
            ..request.clone()
        },
        ControlRenewal {
            source_time_s: 0.0,
            lease_sim_s: 0.05,
            ..request.clone()
        },
    ];
    for fence in [
        ControlFence {
            epoch: primary.epoch + 1,
            ..primary.clone()
        },
        ControlFence {
            term: primary.term + 1,
            ..primary.clone()
        },
        ControlFence {
            run_id: "another_run".into(),
            ..primary.clone()
        },
        ControlFence {
            controller_id: "standby".into(),
            ..primary.clone()
        },
        ControlFence {
            controller_id: "bad/owner".into(),
            ..primary.clone()
        },
    ] {
        invalid.push(ControlRenewal {
            controller_fence: fence,
            ..request.clone()
        });
    }
    for request in invalid {
        let before = serde_json::to_value(sim.snapshot()).unwrap();
        let runtime = sim.control_runtime;
        assert!(sim.renew_control(request).is_err());
        assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
        assert_eq!(sim.control_runtime.deadline_s, runtime.deadline_s);
        assert_eq!(
            sim.control_runtime.command_deadline_s,
            runtime.command_deadline_s
        );
    }
    sim.advance(0.2);
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    assert!(matches!(
        sim.renew_control(ownership_renewal(&sim, &primary, 0.75)),
        Err(LeaseError::StaleContext)
    ));
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
    let peer = claim(&mut sim, "run", "standby", 0.2);
    assert_eq!(peer.term, primary.term + 1);
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    assert!(matches!(
        sim.renew_control(ownership_renewal(&sim, &primary, 0.75)),
        Err(LeaseError::StaleContext)
    ));
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
    sim.set_manual_thrust([0.0, 0.2, 0.0]).unwrap();
    let before = serde_json::to_value(sim.snapshot()).unwrap();
    assert!(matches!(
        sim.renew_control(ownership_renewal(&sim, &peer, 0.75)),
        Err(LeaseError::StaleContext)
    ));
    assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
}

#[test]
fn ownership_renewal_cannot_hold_a_leader_without_eligible_native_sensor_delivery() {
    for target in [SensorFaultTarget::Navigation, SensorFaultTarget::RadarBus] {
        for mode in [SensorFaultMode::Frozen, SensorFaultMode::Unavailable] {
            let mut sim = simulation(0);
            let primary = claim(&mut sim, "run", "primary", 0.2);
            sim.set_leased_command(fenced_lease(&sim, &primary, 0.2))
                .unwrap();
            sim.advance(0.05);
            sim.set_sensor_fault(target, mode, Some(1.0)).unwrap();
            let before = serde_json::to_value(sim.snapshot()).unwrap();
            let runtime = sim.control_runtime;
            assert!(matches!(
                sim.renew_control(ownership_renewal(&sim, &primary, 0.75)),
                Err(LeaseError::Unavailable)
            ));
            assert_eq!(serde_json::to_value(sim.snapshot()).unwrap(), before);
            assert_eq!(sim.control_runtime.deadline_s, runtime.deadline_s);
            assert_eq!(
                sim.control_runtime.command_deadline_s,
                runtime.command_deadline_s
            );
        }
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
