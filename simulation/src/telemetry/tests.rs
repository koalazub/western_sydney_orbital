use super::{
    calculations::{magnitude, measure, waiting_report},
    data::*,
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
    let json = serde_json::to_string(report).unwrap();
    assert!(!json.contains("NaN"));
    assert!(!json.contains("Infinity"));
    serde_json::from_str(&json).unwrap()
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
    assert_eq!(presentation::compact(&report).lines().count(), 2);
    assert!(presentation::expanded(&report).contains("28.65 deg/s"));
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
    assert!(presentation::compact(&report).contains("Terrain-center n/a m"));
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
    assert!(presentation::expanded(&paused).contains("held; no time advance"));
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
    let before = finite_report(&measure(&state).report);
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
    assert_eq!(finite_report(&measure(&changed).report), before);
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
        let _ = super::brief();
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
fn waiting_report_has_no_fake_measurements_and_compact_si_labels() {
    let report = waiting_report();
    assert_eq!(report.status, ReportStatus::WaitingForSnapshot);
    assert!(report.source_time_s.is_none());
    let text = presentation::compact(&report);
    assert_eq!(text.lines().count(), 2);
    assert!(text.contains("Speed n/a m/s"));
    assert!(text.contains("radial climb n/a m/s"));
    assert!(text.contains("Terrain-center n/a m"));
    assert!(text.contains("thrust n/a m/s²"));
    finite_report(&report);
}
