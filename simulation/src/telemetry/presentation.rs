use super::{calculations::waiting_report, data::*};
use crate::model::ThrustApplication;
use std::cell::RefCell;

thread_local! { static REPORT: RefCell<Report> = RefCell::new(waiting_report()); }

pub(super) fn publish(report: Report) {
    REPORT.with(|state| *state.borrow_mut() = report);
}

pub(crate) fn status() -> String {
    REPORT.with(|state| {
        serde_json::to_string(&*state.borrow())
            .expect("telemetry reports contain only finite measurements")
    })
}

pub(crate) fn brief() -> String {
    REPORT.with(|state| compact(&state.borrow()))
}

pub(crate) fn details() -> String {
    REPORT.with(|state| expanded(&state.borrow()))
}

fn number(value: Option<f64>, decimals: usize) -> String {
    match value.filter(|value| value.is_finite()) {
        Some(value) => format!("{value:.decimals$}"),
        None => "n/a".into(),
    }
}

pub(super) fn compact(report: &Report) -> String {
    let m = &report.metrics;
    format!(
        "Speed {} m/s | radial climb {} m/s\nTerrain-center {} m | thrust {} m/s²",
        number(m.speed_mps, 2),
        number(m.radial_climb_mps, 2),
        number(m.terrain_center_clearance_m, 1),
        number(m.applied_thrust_accel_mps2, 2),
    )
}

pub(super) fn expanded(report: &Report) -> String {
    let m = &report.metrics;
    let a = &report.observed_frame_acceleration;
    let mode = match report.actuation.thrust_application {
        Some(ThrustApplication::CenterOfMass) => "center of mass",
        Some(ThrustApplication::ManualNozzles) => "manual nozzles",
        Some(ThrustApplication::PracticeBoost) => "practice boost",
        None => "n/a",
    };
    let acceleration_state = match a.status {
        AccelerationStatus::WaitingForNextSample => "waiting for next timed sample",
        AccelerationStatus::FreshInterval => "new interval",
        AccelerationStatus::HeldWithoutTimeAdvance => "held; no time advance",
        AccelerationStatus::BaselineReset => "baseline reset",
        AccelerationStatus::Unavailable => "unavailable",
    };
    let availability = match report.status {
        ReportStatus::WaitingForSnapshot => "waiting for snapshot",
        ReportStatus::Ready => "ready",
        ReportStatus::Partial => "some inputs unavailable",
        ReportStatus::Unavailable => "input unavailable",
    };
    let frame = if report.frame.recognized {
        "Phobos-centered Mars-rotating; +X to Mars, +Y orbit normal"
    } else {
        "n/a, unrecognized frame"
    };
    let angular_deg_s = m
        .angular_speed_rad_s
        .and_then(|value| super::calculations::finite(value.to_degrees()));
    format!(
        "Local flight telemetry | {availability}\n{}\nTarget center range {} m | closing {} m/s, + toward target\nForce {} N | torque {} N m | mode {mode}\nSpin {} deg/s | fuel used ~{} kg | damage ~{} J\nObserved mean frame accel {} m/s² over {} s\nInterval {} to {} s | {acceleration_state}\nIncludes gravity/contacts/frame terms, not thrust alone\nSampled peaks: speed {} m/s | climb {} m/s | terrain-center {} m\nSimulation {} s | observed epoch {} s\n{frame}\nSpeed is rotating-frame, not inertial. Radial center clearance is not feet/pad clearance.\nOwn ship, mission target and known terrain only; no hidden debris or camera speed.\nLocal practice only, not STP parity. Reference limit {} m/s²; practice boost {} m/s².\nFuel and contact damage are local estimates. Peaks cover received samples only.",
        compact(report),
        number(m.target_center_range_m, 1),
        number(m.target_closing_mps, 2),
        number(m.thrust_force_n, 1),
        number(m.torque_nm, 1),
        number(angular_deg_s, 2),
        number(m.fuel_used_kg_estimate, 3),
        number(m.damage_j_estimate, 1),
        number(a.magnitude_mps2, 3),
        number(a.interval_s, 3),
        number(a.interval_start_s, 3),
        number(a.interval_end_s, 3),
        number(report.peaks.speed_mps, 2),
        number(report.peaks.radial_climb_mps, 2),
        number(report.peaks.terrain_center_clearance_m, 1),
        number(report.source_time_s, 2),
        number(report.epoch.observed_duration_s, 2),
        number(report.caveats.reference_command_limit_mps2, 2),
        number(report.caveats.practice_boost_limit_mps2, 2),
    )
}
