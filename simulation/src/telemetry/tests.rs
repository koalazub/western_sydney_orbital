use super::{
    calculations::{magnitude, measure, waiting_report},
    data::*,
    instruments::{ActorField, FlightMetric, Instrument, InstrumentGroup, Reading},
    presentation,
    tracking::advance,
};
use crate::{
    domain::{EntityId, EntityKind},
    model::{EntityState, Snapshot, ThrustApplication},
    physics::Simulation,
    scenario::settings,
};
use bevy::{math::DVec3, prelude::App};

#[test]
fn external_telemetry_cannot_bypass_native_navigation_delivery_faults() {
    use crate::{
        api::Dispatch,
        model::{PhysicsCommand, SensorFaultMode, SensorFaultTarget},
    };
    let mut experiment = crate::model::ExperimentSettings::default();
    experiment.debris.count = 0;
    crate::session::dispatch(Dispatch::Physics(PhysicsCommand::ConfigureExperiment {
        settings: experiment,
        seed: Some(100),
    }))
    .unwrap();
    let original = crate::snapshot();
    presentation::publish(measure(&original).report);
    let initial: serde_json::Value = serde_json::from_str(&presentation::status()).unwrap();
    assert_eq!(initial["source_time_s"], 0.0);
    crate::session::dispatch(Dispatch::Physics(PhysicsCommand::SetSensorFault {
        target: SensorFaultTarget::Navigation,
        mode: SensorFaultMode::Frozen,
        duration_sim_s: Some(1.0),
    }))
    .unwrap();
    crate::session::dispatch(Dispatch::Physics(PhysicsCommand::Step { seconds: 0.1 })).unwrap();
    presentation::publish(measure(&crate::snapshot()).report);
    let frozen: serde_json::Value = serde_json::from_str(&presentation::status()).unwrap();
    assert_eq!(frozen["source_time_s"], initial["source_time_s"]);
    assert_eq!(frozen["ship"], initial["ship"]);
    assert_eq!(frozen["sensor_delivery"], "frozen");
    crate::session::dispatch(Dispatch::Physics(PhysicsCommand::SetSensorFault {
        target: SensorFaultTarget::AllAvionics,
        mode: SensorFaultMode::Unavailable,
        duration_sim_s: Some(1.0),
    }))
    .unwrap();
    let before = serde_json::to_value(crate::snapshot().as_ref()).unwrap();
    let unavailable: serde_json::Value = serde_json::from_str(&presentation::status()).unwrap();
    assert_eq!(unavailable["status"], "unavailable");
    assert!(unavailable["source_time_s"].is_null());
    assert!(unavailable["ship"]["position_m"].is_null());
    assert!(unavailable["applied_physics"].is_null());
    assert!(unavailable["metrics"]["speed_mps"].is_null());
    assert_eq!(
        serde_json::to_value(crate::snapshot().as_ref()).unwrap(),
        before
    );
    crate::session::dispatch(Dispatch::Physics(PhysicsCommand::ClearSensorFaults)).unwrap();
    crate::session::dispatch(Dispatch::Physics(PhysicsCommand::Step { seconds: 0.1 })).unwrap();
}

fn own(state: &Snapshot) -> &EntityState {
    state
        .assets
        .iter()
        .find(|actor| actor.id == state.mission.shuttle_id)
        .unwrap()
}

fn own_mut(state: &mut Snapshot) -> &mut EntityState {
    state
        .assets
        .iter_mut()
        .find(|actor| actor.id == state.mission.shuttle_id)
        .unwrap()
}

fn target_mut(state: &mut Snapshot) -> &mut EntityState {
    state
        .assets
        .iter_mut()
        .find(|actor| actor.id == state.mission.target_id)
        .unwrap()
}

fn close(actual: Option<f64>, expected: f64) {
    let actual = actual.expect("measurement should be available");
    assert!(
        (actual - expected).abs() <= 1e-10 * expected.abs().max(1.0),
        "actual {actual}, expected {expected}"
    );
}

fn finite_report(report: &Report) -> serde_json::Value {
    let m = report.metrics;
    let a = report.observed_frame_acceleration;
    let p = report.peaks;
    for value in [
        report.source_time_s,
        report.epoch.started_at_time_s,
        report.epoch.observed_duration_s,
        m.speed_mps,
        m.radial_climb_mps,
        m.terrain_center_clearance_m,
        m.target_center_range_m,
        m.target_closing_mps,
        m.applied_thrust_accel_mps2,
        m.thrust_force_n,
        m.torque_nm,
        m.angular_speed_rad_s,
        m.fuel_used_kg_estimate,
        m.damage_j_estimate,
        a.magnitude_mps2,
        a.interval_start_s,
        a.interval_end_s,
        a.interval_s,
        p.speed_mps,
        p.radial_climb_mps,
        p.terrain_center_clearance_m,
        report.caveats.reference_command_limit_mps2,
        report.caveats.practice_boost_limit_mps2,
    ]
    .into_iter()
    .flatten()
    {
        assert!(value.is_finite());
    }
    if let Some(vector) = a.vector_mps2 {
        assert!(vector.into_iter().all(f64::is_finite));
    }
    for group in InstrumentGroup::ALL {
        for &key in group.instruments() {
            match key.reading(report) {
                Reading::Scalar(value, _, _) => assert!(value.is_none_or(f64::is_finite)),
                Reading::Vector(value, _, _) => {
                    assert!(value.is_none_or(|value| value.into_iter().all(f64::is_finite)))
                }
                Reading::Quaternion(value) => {
                    assert!(value.is_none_or(|value| value.into_iter().all(f64::is_finite)))
                }
                Reading::Count(_) => {}
                Reading::Text(_) => {}
                Reading::Direction(value) => {
                    assert!(value.is_none_or(|(_, sign)| [-1, 1].contains(&sign)))
                }
            }
        }
    }
    let json = serde_json::to_string(report).unwrap();
    assert!(!json.contains("NaN"));
    assert!(!json.contains("Infinity"));
    serde_json::from_str(&json).unwrap()
}

#[test]
fn actual_live_tuning_reaches_instruments_without_rewinding_measurement_time() {
    let mut settings = crate::model::ExperimentSettings::default();
    settings.debris.count = 3;
    let mut simulation = Simulation::with_experiment(100, settings).unwrap();
    simulation.set_command([0.1, 0.0, 0.0], [0.0; 3]).unwrap();
    simulation.advance(0.05);
    let mut tracker = Tracker::default();
    let before = advance(&mut tracker, &simulation.snapshot());
    simulation
        .tune_ship(crate::model::ShipTuning {
            mass_kg: 4500.0,
            inertia_scale: 1.5,
            thrust_scale: 2.0,
        })
        .unwrap();
    simulation
        .tune_debris_flow(crate::model::DebrisFlowTuning {
            speed_mps: 33.0,
            speed_spread_mps: 6.0,
            stream_axis: crate::model::StreamAxis::Z,
            stream_sign: 1,
        })
        .unwrap();
    let after = advance(&mut tracker, &simulation.snapshot());
    assert_eq!(before.source_time_s, after.source_time_s);
    assert_eq!(before.epoch.index, after.epoch.index);
    assert_eq!(
        reading(&after, Instrument::Ship(ActorField::Mass)),
        "4500.00 kg"
    );
    assert_eq!(
        reading(&after, Instrument::Ship(ActorField::ActuatorGain)),
        "2.00 ×"
    );
    assert_eq!(reading(&after, Instrument::TuningRevision), "2");
    assert_eq!(reading(&after, Instrument::StreamSpeed), "33.00 m/s");
    assert_eq!(reading(&after, Instrument::BirthSpread), "6.00 m/s");
    assert_eq!(reading(&after, Instrument::StreamDirection), "+Z");
    close(after.metrics.applied_thrust_accel_mps2, 0.2);
    close(after.metrics.thrust_force_n, 900.0);
    finite_report(&after);
}

#[test]
fn native_live_adapter_distinguishes_pending_requests_actual_applications_and_paused_motion() {
    let mut config = crate::model::ExperimentSettings::default();
    config.debris.count = 0;
    let mut simulation = Simulation::with_experiment(100, config).unwrap();
    simulation.set_command([0.1, 0.0, 0.0], [0.0; 3]).unwrap();
    let mut tracker = Tracker::default();
    let mut pending = advance(&mut tracker, &simulation.snapshot());
    super::systems::attach_physics(
        &mut pending,
        simulation.applied_physics(),
        PlaybackContext::Paused,
    );
    assert_eq!(reading(&pending, Instrument::LastForce), "-- N");
    assert_eq!(
        reading(&pending, Instrument::ActuationStatus),
        "request awaiting application"
    );
    assert!(pending.metrics.thrust_force_n.is_none());
    assert!(
        presentation::group_description(InstrumentGroup::Actuation, &pending).contains("Paused")
    );
    simulation.advance(0.05);
    let before = simulation.snapshot();
    let mut active = advance(&mut tracker, &before);
    super::systems::attach_physics(
        &mut active,
        simulation.applied_physics(),
        PlaybackContext::Running,
    );
    close(active.metrics.applied_thrust_accel_mps2, 0.1);
    close(active.metrics.thrust_force_n, config.ship.mass_kg * 0.1);
    assert_eq!(
        active.ship_inertia_body_kg_m2,
        Some(simulation.applied_physics().unwrap().inertia_body_kg_m2)
    );
    simulation
        .tune_ship(crate::model::ShipTuning {
            mass_kg: 100.0,
            inertia_scale: 2.0,
            thrust_scale: 3.2,
        })
        .unwrap();
    let mut tuned = advance(&mut tracker, &simulation.snapshot());
    super::systems::attach_physics(
        &mut tuned,
        simulation.applied_physics(),
        PlaybackContext::Paused,
    );
    assert_eq!(tuned.source_time_s, active.source_time_s);
    assert_eq!(reading(&tuned, Instrument::BodyMass), "100.00 kg");
    assert_eq!(reading(&tuned, Instrument::LastApplicationTime), "0.050 s");
    close(tuned.metrics.applied_thrust_accel_mps2, 0.32);
    close(tuned.metrics.thrust_force_n, 32.0);
    assert_eq!(own(&simulation.snapshot()).velocity, own(&before).velocity);
    let mut mismatched = advance(&mut tracker, &before);
    let mut physics = simulation.applied_physics().unwrap();
    physics.source_time_s += 1.0;
    super::systems::attach_physics(&mut mismatched, Ok(physics), PlaybackContext::Paused);
    assert!(mismatched.applied_physics.is_none());
    assert!(mismatched.applied_physics_error.is_some());
    assert_eq!(reading(&mismatched, Instrument::LastForce), "-- N");
    assert!(mismatched.metrics.thrust_force_n.is_none());
    finite_report(&pending);
    finite_report(&active);
    finite_report(&tuned);
    finite_report(&mismatched);
}

fn known_state() -> Snapshot {
    let mut state = Simulation::new_default().snapshot();
    state.time_s = 2.0;
    let ship = own_mut(&mut state);
    ship.position = [20_000.0, 0.0, 0.0];
    ship.velocity = [3.0, 4.0, 0.0];
    ship.rotation = [0.0, 0.0, 0.0, 1.0];
    ship.mass_kg = 1_000.0;
    ship.thrust_n = [3_000.0, 4_000.0, 0.0];
    ship.torque_nm = [0.0, 0.0, -12.0];
    ship.angular_velocity_rad_s = [0.0, 0.0, 0.5];
    let target = target_mut(&mut state);
    target.position = [20_010.0, 0.0, 0.0];
    target.velocity = [1.0, 4.0, 0.0];
    state.mission.fuel_used_kg = 1.25;
    state.mission.damage = 40.0;
    state
}

#[test]
fn known_vectors_use_actual_si_state_not_configuration_maxima_or_cached_range() {
    let mut state = known_state();
    state.mission.target_distance_m = 999.0;
    state.mission.relative_speed_mps = 999.0;
    own_mut(&mut state).max_thrust_n = f64::NAN;
    own_mut(&mut state).commanded_accel_mps2 = [999.0; 3];
    let report = measure(&state).report;
    assert_eq!(report.status, ReportStatus::Ready);
    close(report.metrics.speed_mps, 5.0);
    close(report.metrics.radial_climb_mps, 3.0);
    close(
        report.metrics.terrain_center_clearance_m,
        20_000.0 - crate::terrain::surface_radius_m(DVec3::X),
    );
    close(report.metrics.target_center_range_m, 10.0);
    close(report.metrics.target_closing_mps, 2.0);
    close(report.metrics.thrust_force_n, 5_000.0);
    close(report.metrics.applied_thrust_accel_mps2, 5.0);
    close(report.metrics.torque_nm, 12.0);
    close(report.metrics.angular_speed_rad_s, 0.5);
    close(report.metrics.fuel_used_kg_estimate, 1.25);
    close(report.metrics.damage_j_estimate, 40.0);
    assert_eq!(
        report.provenance.own_ship_id,
        Some(state.mission.shuttle_id)
    );
    assert_eq!(report.provenance.target_id, Some(state.mission.target_id));
    assert!(!report.provenance.hidden_debris_truth_used);
    assert!(!report.provenance.camera_state_used);
    let json = finite_report(&report);
    assert_eq!(json["units"]["angular_speed"], "rad/s");
    assert_eq!(json["caveats"]["stp_parity"], false);
    assert_eq!(
        reading(&report, Instrument::Flight(FlightMetric::Speed)),
        "5.00 m/s"
    );
    assert_eq!(
        reading(&report, Instrument::Flight(FlightMetric::Spin)),
        "0.5000 rad/s"
    );
    assert_eq!(
        reading(&report, Instrument::Flight(FlightMetric::TargetRange)),
        "10.0 m"
    );
}

#[test]
fn target_closing_sign_is_positive_toward_target_and_undefined_at_zero_range() {
    let mut state = known_state();
    own_mut(&mut state).velocity = [3.0, 0.0, 0.0];
    for (target_velocity, expected) in [
        ([-2.0, 0.0, 0.0], 5.0),
        ([4.0, 0.0, 0.0], -1.0),
        ([3.0, 8.0, 0.0], 0.0),
    ] {
        target_mut(&mut state).velocity = target_velocity;
        close(measure(&state).report.metrics.target_closing_mps, expected);
    }
    target_mut(&mut state).position = own(&state).position;
    let report = measure(&state).report;
    close(report.metrics.target_center_range_m, 0.0);
    assert!(report.metrics.target_closing_mps.is_none());
    assert!(
        report
            .input_issues
            .contains(&InputIssue::UndefinedClosingDirection)
    );
    finite_report(&report);
}

#[test]
fn zero_radius_is_not_a_radial_direction_or_a_fake_clearance() {
    let mut state = known_state();
    own_mut(&mut state).position = [0.0; 3];
    let report = measure(&state).report;
    close(report.metrics.speed_mps, 5.0);
    assert!(report.metrics.radial_climb_mps.is_none());
    assert!(report.metrics.terrain_center_clearance_m.is_none());
    assert!(
        report
            .input_issues
            .contains(&InputIssue::UndefinedRadialDirection)
    );
    assert_eq!(
        reading(&report, Instrument::Flight(FlightMetric::TerrainClearance)),
        "-- m"
    );
    finite_report(&report);
}

#[test]
fn observed_acceleration_is_delta_velocity_over_the_actual_sample_interval() {
    let mut state = known_state();
    own_mut(&mut state).velocity = [1.0, 2.0, 3.0];
    let mut tracker = Tracker::default();
    let first = advance(&mut tracker, &state);
    assert_eq!(
        first.observed_frame_acceleration.status,
        AccelerationStatus::WaitingForNextSample
    );
    assert!(first.observed_frame_acceleration.vector_mps2.is_none());
    state.time_s = 4.0;
    own_mut(&mut state).position[0] += 4.0;
    own_mut(&mut state).velocity = [5.0, -4.0, 9.0];
    let report = advance(&mut tracker, &state);
    let a = report.observed_frame_acceleration;
    assert_eq!(a.status, AccelerationStatus::FreshInterval);
    assert_eq!(a.vector_mps2, Some([2.0, -3.0, 3.0]));
    close(a.magnitude_mps2, 22.0_f64.sqrt());
    close(a.interval_start_s, 2.0);
    close(a.interval_end_s, 4.0);
    close(a.interval_s, 2.0);
    close(report.epoch.observed_duration_s, 2.0);
    finite_report(&report);
}

#[test]
fn same_time_force_and_actual_mode_update_without_inventing_a_new_derivative() {
    let mut state = known_state();
    let mut tracker = Tracker::default();
    advance(&mut tracker, &state);
    state.time_s = 3.0;
    own_mut(&mut state).velocity = [4.0, 4.0, 0.0];
    let timed = advance(&mut tracker, &state);
    let ship = own_mut(&mut state);
    ship.mass_kg = 2_000.0;
    ship.thrust_n = [0.0, 0.0, 2.0 * ship.mass_kg];
    ship.thrust_application = ThrustApplication::PracticeBoost;
    let paused = advance(&mut tracker, &state);
    assert_eq!(paused.epoch.index, timed.epoch.index);
    assert_eq!(paused.peaks.speed_mps, timed.peaks.speed_mps);
    assert_eq!(
        paused.actuation.thrust_application,
        Some(ThrustApplication::PracticeBoost)
    );
    assert_eq!(paused.actuation.practice_boost_active, Some(true));
    close(paused.metrics.applied_thrust_accel_mps2, 2.0);
    assert_eq!(paused.ship.mass_kg, Some(2_000.0));
    assert_eq!(paused.ship.applied_force_body_n, Some([0.0, 0.0, 4_000.0]));
    assert_eq!(
        paused.observed_frame_acceleration.status,
        AccelerationStatus::HeldWithoutTimeAdvance
    );
    assert_eq!(
        paused.observed_frame_acceleration.vector_mps2,
        timed.observed_frame_acceleration.vector_mps2
    );
    assert_eq!(paused.observed_frame_acceleration.interval_end_s, Some(3.0));
    assert_eq!(
        finite_report(&paused)["actuation"]["thrust_application"],
        "practice_boost"
    );
    assert_eq!(
        presentation::group_description(InstrumentGroup::Acceleration, &paused),
        "Held; no time advance"
    );
}

#[test]
fn same_time_own_kinematic_restores_invalidate_the_derivative_and_sampled_peaks() {
    enum Change {
        Position,
        Velocity,
        Rotation,
        Spin,
    }
    let template = known_state();
    for change in [
        Change::Position,
        Change::Velocity,
        Change::Rotation,
        Change::Spin,
    ] {
        let mut before = template.clone();
        own_mut(&mut before).position = [30_000.0, 0.0, 0.0];
        own_mut(&mut before).velocity = [100.0, 0.0, 0.0];
        let mut tracker = Tracker::default();
        advance(&mut tracker, &before);
        let mut current = template.clone();
        current.time_s = before.time_s + 1.0;
        own_mut(&mut current).velocity = [2.0, 0.0, 0.0];
        let timed = advance(&mut tracker, &current);
        close(timed.peaks.speed_mps, 100.0);
        let ship = own_mut(&mut current);
        match change {
            Change::Position => ship.position[0] -= 100.0,
            Change::Velocity => ship.velocity[0] = 1.0,
            Change::Rotation => ship.rotation = [0.0, 0.0, 1.0, 0.0],
            Change::Spin => ship.angular_velocity_rad_s = [0.0, 0.0, 0.25],
        }
        let restored = advance(&mut tracker, &current);
        assert_eq!(restored.epoch.index, timed.epoch.index + 1);
        assert_eq!(
            restored.epoch.last_reset,
            Some(EpochReset::SameTimeKinematicsChanged)
        );
        assert_eq!(
            restored.observed_frame_acceleration.status,
            AccelerationStatus::BaselineReset
        );
        assert!(restored.observed_frame_acceleration.vector_mps2.is_none());
        assert!(
            restored
                .observed_frame_acceleration
                .interval_end_s
                .is_none()
        );
        assert_eq!(restored.peaks.speed_mps, restored.metrics.speed_mps);
        assert_eq!(
            restored.peaks.terrain_center_clearance_m,
            restored.metrics.terrain_center_clearance_m
        );
        finite_report(&restored);
    }
}

#[test]
fn seed_changes_and_time_rewinds_start_new_peak_epochs() {
    let mut state = known_state();
    let mut tracker = Tracker::default();
    own_mut(&mut state).velocity = [100.0, 0.0, 0.0];
    let first = advance(&mut tracker, &state);
    state.time_s += 10.0;
    own_mut(&mut state).velocity = [2.0, 0.0, 0.0];
    close(advance(&mut tracker, &state).peaks.speed_mps, 100.0);
    state.time_s = 1.0;
    let rewind = advance(&mut tracker, &state);
    assert_eq!(rewind.epoch.index, first.epoch.index + 1);
    assert_eq!(rewind.epoch.last_reset, Some(EpochReset::TimeRewound));
    close(rewind.peaks.speed_mps, 2.0);
    assert!(rewind.observed_frame_acceleration.vector_mps2.is_none());
    state.seed += 1;
    state.time_s = 20.0;
    own_mut(&mut state).velocity = [1.0, 0.0, 0.0];
    let reseed = advance(&mut tracker, &state);
    assert_eq!(reseed.epoch.last_reset, Some(EpochReset::SeedChanged));
    close(reseed.peaks.speed_mps, 1.0);
    close(reseed.epoch.observed_duration_s, 0.0);
    assert!(reseed.observed_frame_acceleration.vector_mps2.is_none());
}

#[test]
fn all_sampled_peaks_use_signed_maxima_and_share_the_reset_epoch() {
    let mut state = known_state();
    own_mut(&mut state).position = [30_000.0, 0.0, 0.0];
    own_mut(&mut state).velocity = [-10.0, 0.0, 0.0];
    let mut tracker = Tracker::default();
    let first = advance(&mut tracker, &state);
    close(first.peaks.radial_climb_mps, -10.0);
    state.time_s += 1.0;
    own_mut(&mut state).position = [25_000.0, 0.0, 0.0];
    own_mut(&mut state).velocity = [-20.0, 0.0, 0.0];
    let descending = advance(&mut tracker, &state);
    close(descending.peaks.speed_mps, 20.0);
    close(descending.peaks.radial_climb_mps, -10.0);
    assert_eq!(
        descending.peaks.terrain_center_clearance_m,
        first.metrics.terrain_center_clearance_m
    );
    state.time_s += 1.0;
    own_mut(&mut state).position = [20_000.0, 0.0, 0.0];
    own_mut(&mut state).velocity = [5.0, 0.0, 0.0];
    let climbing = advance(&mut tracker, &state);
    close(climbing.peaks.speed_mps, 20.0);
    close(climbing.peaks.radial_climb_mps, 5.0);
    assert_eq!(
        climbing.peaks.terrain_center_clearance_m,
        first.metrics.terrain_center_clearance_m
    );
    state.seed += 1;
    let reset = advance(&mut tracker, &state);
    assert_eq!(reset.peaks.speed_mps, reset.metrics.speed_mps);
    assert_eq!(reset.peaks.radial_climb_mps, reset.metrics.radial_climb_mps);
    assert_eq!(
        reset.peaks.terrain_center_clearance_m,
        reset.metrics.terrain_center_clearance_m
    );
}

#[test]
fn overflowing_velocity_differences_do_not_publish_a_fake_finite_acceleration() {
    let mut state = known_state();
    own_mut(&mut state).velocity = [-f64::MAX, 0.0, 0.0];
    let mut tracker = Tracker::default();
    advance(&mut tracker, &state);
    state.time_s += 1.0;
    own_mut(&mut state).velocity = [f64::MAX, 0.0, 0.0];
    let report = advance(&mut tracker, &state);
    assert_eq!(
        report.observed_frame_acceleration.status,
        AccelerationStatus::Unavailable
    );
    assert!(report.observed_frame_acceleration.vector_mps2.is_none());
    assert!(report.observed_frame_acceleration.magnitude_mps2.is_none());
    assert!(report.observed_frame_acceleration.interval_s.is_none());
    finite_report(&report);
}

#[derive(Clone, Copy)]
enum BadInput {
    Time,
    Position,
    Velocity,
    Rotation,
    Mass,
    Thrust,
    Torque,
    Spin,
    TargetPosition,
    TargetVelocity,
    Fuel,
    Damage,
}

#[test]
fn nonfinite_measurements_are_unavailable_options_and_json_null_not_zero() {
    let template = known_state();
    for invalid in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        for field in [
            BadInput::Time,
            BadInput::Position,
            BadInput::Velocity,
            BadInput::Rotation,
            BadInput::Mass,
            BadInput::Thrust,
            BadInput::Torque,
            BadInput::Spin,
            BadInput::TargetPosition,
            BadInput::TargetVelocity,
            BadInput::Fuel,
            BadInput::Damage,
        ] {
            let mut state = template.clone();
            match field {
                BadInput::Time => state.time_s = invalid,
                BadInput::Position => own_mut(&mut state).position[0] = invalid,
                BadInput::Velocity => own_mut(&mut state).velocity[0] = invalid,
                BadInput::Rotation => own_mut(&mut state).rotation[0] = invalid,
                BadInput::Mass => own_mut(&mut state).mass_kg = invalid,
                BadInput::Thrust => own_mut(&mut state).thrust_n[0] = invalid,
                BadInput::Torque => own_mut(&mut state).torque_nm[0] = invalid,
                BadInput::Spin => own_mut(&mut state).angular_velocity_rad_s[0] = invalid,
                BadInput::TargetPosition => target_mut(&mut state).position[0] = invalid,
                BadInput::TargetVelocity => target_mut(&mut state).velocity[0] = invalid,
                BadInput::Fuel => state.mission.fuel_used_kg = invalid,
                BadInput::Damage => state.mission.damage = invalid,
            }
            let report = advance(&mut Tracker::default(), &state);
            assert_ne!(report.status, ReportStatus::Ready);
            let json = finite_report(&report);
            let field = match field {
                BadInput::Time => {
                    assert!(report.source_time_s.is_none());
                    "speed_mps"
                }
                BadInput::Position => "terrain_center_clearance_m",
                BadInput::Velocity => "speed_mps",
                BadInput::Rotation => {
                    assert!(report.observed_frame_acceleration.vector_mps2.is_none());
                    assert!(report.peaks.speed_mps.is_none());
                    continue;
                }
                BadInput::Mass => "applied_thrust_accel_mps2",
                BadInput::Thrust => "thrust_force_n",
                BadInput::Torque => "torque_nm",
                BadInput::Spin => "angular_speed_rad_s",
                BadInput::TargetPosition => "target_center_range_m",
                BadInput::TargetVelocity => "target_closing_mps",
                BadInput::Fuel => "fuel_used_kg_estimate",
                BadInput::Damage => "damage_j_estimate",
            };
            assert!(json["metrics"][field].is_null());
        }
    }
}

#[test]
fn invalid_baselines_cannot_bridge_missing_velocity_and_recovery() {
    let mut state = known_state();
    let mut tracker = Tracker::default();
    advance(&mut tracker, &state);
    state.time_s += 1.0;
    own_mut(&mut state).velocity = [5.0, 0.0, 0.0];
    let timed = advance(&mut tracker, &state);
    assert_eq!(
        timed.observed_frame_acceleration.status,
        AccelerationStatus::FreshInterval
    );
    own_mut(&mut state).velocity[0] = f64::NAN;
    let invalid = advance(&mut tracker, &state);
    assert_eq!(
        invalid.epoch.last_reset,
        Some(EpochReset::InputDiscontinuity)
    );
    assert!(invalid.observed_frame_acceleration.vector_mps2.is_none());
    assert!(invalid.peaks.speed_mps.is_none());
    own_mut(&mut state).velocity = [1.0, 0.0, 0.0];
    let recovered = advance(&mut tracker, &state);
    assert_eq!(recovered.epoch.last_reset, Some(EpochReset::InputRecovered));
    assert!(recovered.observed_frame_acceleration.vector_mps2.is_none());
    close(recovered.peaks.speed_mps, 1.0);
    state.time_s += 1.0;
    own_mut(&mut state).velocity = [3.0, 0.0, 0.0];
    assert_eq!(
        advance(&mut tracker, &state)
            .observed_frame_acceleration
            .vector_mps2,
        Some([2.0, 0.0, 0.0])
    );
}

#[test]
fn finite_inputs_with_overflowing_derived_values_fail_closed() {
    let mut state = known_state();
    own_mut(&mut state).position = [-f64::MAX, 0.0, 0.0];
    target_mut(&mut state).position = [f64::MAX, 0.0, 0.0];
    let report = measure(&state).report;
    assert!(report.metrics.target_center_range_m.is_none());
    assert!(report.metrics.target_closing_mps.is_none());
    finite_report(&report);
    own_mut(&mut state).position = [20_000.0, 0.0, 0.0];
    own_mut(&mut state).mass_kg = f64::MIN_POSITIVE;
    own_mut(&mut state).thrust_n = [f64::MAX, 0.0, 0.0];
    let report = measure(&state).report;
    assert!(report.metrics.applied_thrust_accel_mps2.is_none());
    finite_report(&report);
    own_mut(&mut state).velocity = [f64::MAX; 3];
    let report = measure(&state).report;
    assert!(report.metrics.speed_mps.is_none());
    finite_report(&report);
}

#[test]
fn missing_actors_bad_frame_and_unknown_terrain_do_not_fabricate_measurements() {
    let template = known_state();
    let mut state = template.clone();
    state
        .assets
        .retain(|actor| actor.id != state.mission.shuttle_id);
    let report = measure(&state).report;
    assert_eq!(report.status, ReportStatus::Unavailable);
    assert!(report.metrics.speed_mps.is_none());
    assert!(report.metrics.thrust_force_n.is_none());
    assert!(report.actuation.practice_boost_active.is_none());
    state = template.clone();
    state
        .assets
        .retain(|actor| actor.id != state.mission.target_id);
    let report = measure(&state).report;
    assert_eq!(report.status, ReportStatus::Partial);
    close(report.metrics.speed_mps, 5.0);
    assert!(report.metrics.target_center_range_m.is_none());
    state = template.clone();
    state.frame = "inertial or unknown".into();
    let report = measure(&state).report;
    assert_eq!(report.status, ReportStatus::Unavailable);
    assert_eq!(
        report.frame.snapshot_description.as_deref(),
        Some(state.frame.as_str())
    );
    assert!(report.metrics.speed_mps.is_none());
    assert!(report.metrics.terrain_center_clearance_m.is_none());
    state = template;
    state.body.radii_m[0] += 1.0;
    let report = measure(&state).report;
    assert_eq!(report.status, ReportStatus::Partial);
    assert!(report.metrics.terrain_center_clearance_m.is_none());
    close(report.metrics.speed_mps, 5.0);
    finite_report(&report);
}

#[test]
fn changing_hidden_debris_observations_contacts_and_rng_does_not_change_flight_inputs() {
    let state = known_state();
    let mut before = finite_report(&measure(&state).report);
    let mut changed = state.clone();
    for actor in &mut changed.assets {
        if actor.kind == EntityKind::Debris {
            actor.position = [f64::NAN; 3];
            actor.velocity = [f64::INFINITY; 3];
            actor.angular_velocity_rad_s = [f64::NAN; 3];
            actor.mass_kg = f64::NAN;
            actor.radius_m = f64::NAN;
        }
    }
    changed.observations.clear();
    changed.active_contacts.clear();
    changed.events.clear();
    changed.collisions = 999;
    changed.rng_state ^= u64::MAX;
    let report = measure(&changed).report;
    assert_eq!(report.mission.collisions, Some(999));
    assert_eq!(report.mission.active_contacts, Some(0));
    let mut after = finite_report(&report);
    // Mission counters are now native instruments too. Changing them must not
    // change any own/target flight input, measurement, provenance or RNG state.
    before.as_object_mut().unwrap().remove("mission");
    after.as_object_mut().unwrap().remove("mission");
    assert_eq!(after, before);
    changed.mission.shuttle_id = EntityId::Debris(1);
    assert_eq!(measure(&changed).report.status, ReportStatus::Unavailable);
    changed.mission.shuttle_id = state.mission.shuttle_id;
    changed.mission.target_id = EntityId::Debris(1);
    changed
        .assets
        .iter_mut()
        .find(|actor| actor.id == EntityId::Debris(1))
        .unwrap()
        .kind = EntityKind::ReturnShip;
    assert!(
        measure(&changed)
            .report
            .metrics
            .target_center_range_m
            .is_none()
    );
}

#[test]
fn calculations_and_publishing_leave_actual_simulation_snapshot_and_rng_unchanged() {
    let simulation = Simulation::new_default();
    let state = simulation.snapshot();
    let before = serde_json::to_string(&state).unwrap();
    let mut tracker = Tracker::default();
    for _ in 0..3 {
        let report = advance(&mut tracker, &state);
        finite_report(&report);
        presentation::publish(report);
        let _ = super::instrument(Instrument::Flight(FlightMetric::Speed));
        let _ = super::details();
        let _: serde_json::Value = serde_json::from_str(&super::status()).unwrap();
    }
    assert_eq!(serde_json::to_string(&state).unwrap(), before);
    assert_eq!(
        serde_json::to_string(&simulation.snapshot()).unwrap(),
        before
    );
    assert_eq!(state.rng_state, simulation.snapshot().rng_state);
}

#[test]
fn installed_readonly_system_preserves_published_snapshot_identity_and_rng() {
    let before = crate::snapshot();
    let serialized = serde_json::to_string(&*before).unwrap();
    let mut app = App::new();
    super::install(&mut app);
    app.update();
    app.update();
    let after = crate::snapshot();
    assert!(std::rc::Rc::ptr_eq(&before, &after));
    assert_eq!(serde_json::to_string(&*after).unwrap(), serialized);
    assert_eq!(before.rng_state, after.rng_state);
    let published: serde_json::Value = serde_json::from_str(&super::status()).unwrap();
    assert_eq!(published["source_time_s"], after.time_s);
    assert_eq!(
        published["ship_inertia_body_kg_m2"],
        serde_json::to_value(
            crate::session::applied_physics()
                .unwrap()
                .inertia_body_kg_m2
        )
        .unwrap()
    );
}

#[test]
fn actual_reference_and_boost_physics_publish_force_speed_climb_fuel_and_frame_acceleration() {
    for boost in [false, true] {
        let mut simulation = Simulation::new_default();
        let before = simulation.snapshot();
        let limit = if boost {
            settings().practice.space_lift_accel_mps2
        } else {
            settings().lander.max_accel_mps2
        };
        if boost {
            simulation.set_space_thrust([0.0, 0.0, limit]).unwrap();
        } else {
            simulation.set_manual_thrust([0.0, 0.0, limit]).unwrap();
        }
        let mut tracker = Tracker::default();
        let commanded = advance(&mut tracker, &simulation.snapshot());
        close(commanded.metrics.applied_thrust_accel_mps2, limit);
        assert_eq!(commanded.actuation.practice_boost_active, Some(boost));
        let elapsed = 4.0 * settings().integration.fixed_step_s;
        simulation.advance(elapsed);
        let state = simulation.snapshot();
        let serialized = serde_json::to_string(&state).unwrap();
        let report = advance(&mut tracker, &state);
        assert!(report.metrics.speed_mps.unwrap() > 0.0);
        assert!(report.metrics.radial_climb_mps.unwrap() > 0.0);
        assert!(
            report.metrics.terrain_center_clearance_m.unwrap()
                > commanded.metrics.terrain_center_clearance_m.unwrap()
        );
        let force = magnitude(own(&state).thrust_n).unwrap();
        close(report.metrics.thrust_force_n, force);
        close(
            report.metrics.applied_thrust_accel_mps2,
            force / own(&state).mass_kg,
        );
        close(
            report.metrics.speed_mps,
            magnitude(own(&state).velocity).unwrap(),
        );
        let radial = DVec3::from_array(own(&state).position).normalize();
        close(
            report.metrics.radial_climb_mps,
            DVec3::from_array(own(&state).velocity).dot(radial),
        );
        close(
            report.metrics.fuel_used_kg_estimate,
            before.mission.fuel_used_kg
                + force * elapsed
                    / (settings().lander.specific_impulse_s
                        * settings().lander.standard_gravity_mps2),
        );
        let expected: [f64; 3] = std::array::from_fn(|axis| {
            (own(&state).velocity[axis] - own(&before).velocity[axis])
                / (state.time_s - before.time_s)
        });
        assert_eq!(
            report.observed_frame_acceleration.vector_mps2,
            Some(expected)
        );
        assert_eq!(
            report.observed_frame_acceleration.status,
            AccelerationStatus::FreshInterval
        );
        finite_report(&report);
        assert_eq!(
            serde_json::to_string(&simulation.snapshot()).unwrap(),
            serialized
        );
    }
}

#[test]
fn waiting_report_has_no_fake_measurements_in_any_instrument_group() {
    let report = waiting_report();
    assert_eq!(report.status, ReportStatus::WaitingForSnapshot);
    assert!(report.source_time_s.is_none());
    for group in InstrumentGroup::ALL {
        for &key in group.instruments() {
            let value = reading(&report, key);
            assert!(
                value.starts_with("--"),
                "{key:?} invented a waiting value: {value}"
            );
        }
    }
    assert_eq!(
        presentation::source(&report),
        "Source -- s · waiting for snapshot"
    );
    finite_report(&report);
}

fn reading(report: &Report, key: Instrument) -> String {
    presentation::format_reading(key.reading(report))
}

#[test]
fn grouped_inventory_covers_every_flight_metric_once_with_its_actual_value() {
    let mut report = waiting_report();
    report.metrics = FlightMetrics {
        speed_mps: Some(1.0),
        radial_climb_mps: Some(2.0),
        terrain_center_clearance_m: Some(3.0),
        target_center_range_m: Some(4.0),
        target_closing_mps: Some(5.0),
        applied_thrust_accel_mps2: Some(6.0),
        thrust_force_n: Some(7.0),
        torque_nm: Some(8.0),
        angular_speed_rad_s: Some(9.0),
        fuel_used_kg_estimate: Some(10.0),
        damage_j_estimate: Some(11.0),
    };
    let inventory: Vec<_> = InstrumentGroup::ALL
        .into_iter()
        .flat_map(|group| group.instruments().iter().copied())
        .collect();
    for (metric, expected) in report.metrics.inventory() {
        let key = Instrument::Flight(metric);
        assert_eq!(
            inventory
                .iter()
                .filter(|&&candidate| candidate == key)
                .count(),
            1,
            "{metric:?}"
        );
        let Reading::Scalar(value, unit, _) = key.reading(&report) else {
            panic!("a flight metric must be a scalar");
        };
        assert_eq!(value, expected, "{metric:?}");
        assert!(!unit.is_empty());
    }
    assert_eq!(FlightMetric::STRIP.len(), 6);
    for metric in FlightMetric::STRIP {
        assert!(
            InstrumentGroup::Flight
                .instruments()
                .contains(&Instrument::Flight(metric))
        );
    }
}

#[test]
fn actor_instruments_read_actual_mass_commands_wrench_and_kinematics() {
    let mut state = known_state();
    let ship = own_mut(&mut state);
    ship.commanded_accel_mps2 = [0.1, -0.2, 0.3];
    ship.commanded_angular_accel_rad_s2 = [0.01, 0.02, -0.03];
    let report = measure(&state).report;
    assert_eq!(report.ship.mass_kg, Some(1_000.0));
    assert_eq!(report.ship.applied_accel_body_mps2, Some([3.0, 4.0, 0.0]));
    assert_eq!(
        report.ship.requested_accel_body_mps2,
        Some([0.1, -0.2, 0.3])
    );
    assert_eq!(
        report.ship.requested_angular_accel_body_rad_s2,
        Some([0.01, 0.02, -0.03])
    );
    assert_eq!(report.ship.applied_torque_body_nm, Some([0.0, 0.0, -12.0]));
    assert_eq!(report.ship.position_m, Some(own(&state).position));
    assert_eq!(report.ship.velocity_mps, Some(own(&state).velocity));
    assert_eq!(report.ship.rotation_xyzw, Some(own(&state).rotation));
    assert_eq!(report.target.position_m, Some([20_010.0, 0.0, 0.0]));
    assert_eq!(
        reading(&report, Instrument::Ship(ActorField::Mass)),
        "1000.00 kg"
    );
    assert_eq!(
        reading(&report, Instrument::Ship(ActorField::AppliedForce)),
        "3000.00, 4000.00, 0.00 N"
    );
    // Snapshot settings are not evidence of a live Rapier tensor.
    assert!(report.ship_inertia_body_kg_m2.is_none());
}

#[test]
fn partial_inputs_show_missing_only_where_unavailable_without_hiding_valid_values() {
    let mut state = known_state();
    own_mut(&mut state).thrust_n[1] = f64::NAN;
    target_mut(&mut state).velocity[2] = f64::INFINITY;
    let report = measure(&state).report;
    assert_eq!(presentation::source(&report), "Source 2.000 s · partial");
    assert_eq!(
        reading(
            &report,
            Instrument::Flight(FlightMetric::ThrustAcceleration)
        ),
        "-- m/s²"
    );
    assert_eq!(
        reading(&report, Instrument::Ship(ActorField::AppliedForce)),
        "-- N"
    );
    assert_eq!(
        reading(&report, Instrument::Target(ActorField::Velocity)),
        "-- m/s"
    );
    assert_eq!(
        reading(&report, Instrument::Flight(FlightMetric::Closing)),
        "-- m/s"
    );
    assert_eq!(
        reading(&report, Instrument::Flight(FlightMetric::Speed)),
        "5.00 m/s"
    );
    state.time_s = f64::NAN;
    let unavailable = measure(&state).report;
    assert_eq!(
        presentation::source(&unavailable),
        "Source -- s · unavailable"
    );
    assert_eq!(
        reading(&unavailable, Instrument::Ship(ActorField::Mass)),
        "-- kg"
    );
    finite_report(&unavailable);
}

#[test]
fn acceleration_inventory_includes_vector_magnitude_and_both_source_times() {
    let mut state = known_state();
    let mut tracker = Tracker::default();
    advance(&mut tracker, &state);
    state.time_s = 4.0;
    own_mut(&mut state).velocity = [7.0, -2.0, 6.0];
    let report = advance(&mut tracker, &state);
    assert_eq!(
        reading(&report, Instrument::ObservedVector),
        "2.0000, -3.0000, 3.0000 m/s²"
    );
    assert_eq!(
        reading(&report, Instrument::ObservedMagnitude),
        "4.6904 m/s²"
    );
    assert_eq!(reading(&report, Instrument::IntervalStart), "2.000 s");
    assert_eq!(reading(&report, Instrument::IntervalEnd), "4.000 s");
    assert_eq!(reading(&report, Instrument::IntervalDuration), "2.000 s");
}

#[test]
fn real_samples_preserve_fixed_metadata_and_leave_the_waiting_report_unchanged() {
    let waiting = finite_report(&waiting_report());
    let mut simulation = Simulation::new_default();
    let mut tracker = Tracker::default();
    for _ in 0..3 {
        let state = simulation.snapshot();
        let report = advance(&mut tracker, &state);
        let json = finite_report(&report);
        assert_eq!(
            report.frame.snapshot_description.as_deref(),
            Some(state.frame.as_str())
        );
        for field in ["schema_version", "units", "caveats"] {
            assert_eq!(json[field], waiting[field]);
        }
        simulation.advance(settings().integration.fixed_step_s);
    }
    assert_eq!(finite_report(&waiting_report()), waiting);
}

#[test]
fn native_debris_diagnostics_sample_real_bodies_without_time_and_never_serialize_hidden_motion() {
    use crate::{
        api::Dispatch,
        model::{ExperimentSettings, FlightPreset, PhysicsCommand, StreamAxis},
    };
    let saved = crate::snapshot();
    let mut settings = ExperimentSettings::default();
    settings.debris.count = 3;
    crate::session::dispatch(Dispatch::Physics(PhysicsCommand::ConfigureFlight {
        settings,
        seed: Some(saved.seed),
        preset: FlightPreset::FarSideL2,
    }))
    .unwrap();
    let mut app = App::new();
    super::install(&mut app);
    assert!(
        !app.world()
            .contains_resource::<bevy::prelude::Time<bevy::prelude::Real>>()
    );
    for _ in 0..4 {
        app.update();
    }
    assert_eq!(
        app.world()
            .resource::<super::debris::DebrisSampleCache>()
            .samples_taken,
        1,
        "a headless app must not invent elapsed wall time or resample per displayed field"
    );
    let actual = crate::session::debris_dynamics().unwrap();
    assert_eq!(
        super::instrument(Instrument::DebrisCount),
        actual.physical_count.to_string()
    );
    assert_eq!(
        super::instrument(Instrument::DebrisMovingCount),
        actual.moving_count.to_string()
    );
    assert_eq!(
        super::instrument(Instrument::DebrisMeanVelocity),
        presentation::format_reading(Reading::Vector(
            Some(actual.mean_velocity_world_mps),
            "m/s",
            3
        ))
    );
    assert_eq!(
        super::instrument(Instrument::DebrisMinSpeed),
        presentation::format_reading(Reading::Scalar(actual.min_speed_mps, "m/s", 3))
    );
    assert_eq!(
        super::instrument(Instrument::DebrisMaxSpeed),
        presentation::format_reading(Reading::Scalar(actual.max_speed_mps, "m/s", 3))
    );
    assert_eq!(
        super::instrument(Instrument::DebrisMeanSpin),
        presentation::format_reading(Reading::Scalar(actual.mean_spin_rad_s, "rad/s", 4))
    );
    assert!(super::group_status(InstrumentGroup::Stream).contains("Paused"));
    assert!(super::group_status(InstrumentGroup::Stream).contains("do not mean"));
    crate::session::dispatch(Dispatch::Physics(PhysicsCommand::TuneDebrisFlow {
        settings: crate::model::DebrisFlowTuning {
            speed_mps: 35.0,
            speed_spread_mps: 4.0,
            stream_axis: StreamAxis::Z,
            stream_sign: -1,
        },
    }))
    .unwrap();
    app.update();
    let changed = crate::session::debris_dynamics().unwrap();
    assert_eq!(changed.source_time_s, actual.source_time_s);
    assert_ne!(
        changed.mean_velocity_world_mps,
        actual.mean_velocity_world_mps
    );
    assert_eq!(
        app.world()
            .resource::<super::debris::DebrisSampleCache>()
            .samples_taken,
        2,
        "same-time flow tuning refreshes the one cached body sample"
    );
    assert_eq!(
        super::instrument(Instrument::DebrisMeanVelocity),
        presentation::format_reading(Reading::Vector(
            Some(changed.mean_velocity_world_mps),
            "m/s",
            3
        ))
    );
    let public: serde_json::Value = serde_json::from_str(&super::status()).unwrap();
    for hidden in [
        "debris_dynamics",
        "debris_dynamics_error",
        "debris_distribution",
        "mean_velocity_world_mps",
        "mean_spin_rad_s",
        "moving_count",
    ] {
        assert!(
            public.get(hidden).is_none(),
            "native debug data must not leak through telemetry_status: {hidden}"
        );
    }
    assert_eq!(public["provenance"]["hidden_debris_truth_used"], false);
    crate::session::dispatch(Dispatch::Physics(PhysicsCommand::Restore {
        state: Box::new(saved.as_ref().clone()),
    }))
    .unwrap();
}

#[test]
fn impact_mode_never_labels_an_inactive_stream_reference_as_a_measured_velocity() {
    let mut settings = crate::model::ExperimentSettings::default();
    settings.debris.count = 3;
    settings.debris.distribution = crate::model::DebrisDistribution::impact_default();
    let simulation = Simulation::with_experiment(100, settings).unwrap();
    let report = measure(&simulation.snapshot()).report;
    assert!(report.debris_flow_reference.is_none());
    for key in [
        Instrument::StreamSpeed,
        Instrument::BirthSpread,
        Instrument::StreamDirection,
    ] {
        assert!(reading(&report, key).starts_with("--"));
    }
    let text = presentation::group_description(InstrumentGroup::Stream, &report);
    assert!(text.contains("Impact ejecta") && text.contains("inactive"));
}
