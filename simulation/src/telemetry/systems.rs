use super::{
    calculations::magnitude,
    data::{InputIssue, PlaybackContext, Report, ReportStatus, Tracker},
    debris::DebrisSampleCache,
    presentation::publish,
    tracking,
};
use crate::EngineSet;
use bevy::prelude::*;

pub(crate) fn install(app: &mut App) {
    app.init_resource::<Tracker>()
        .init_resource::<DebrisSampleCache>()
        .add_systems(
            Update,
            update.in_set(EngineSet::Reports).after(EngineSet::Runtime),
        );
}

fn update(
    mut tracker: ResMut<Tracker>,
    mut debris: ResMut<DebrisSampleCache>,
    real_time: Option<Res<Time<Real>>>,
) {
    let snapshot = crate::snapshot();
    debris.refresh(
        &snapshot,
        crate::session::trial_revision(),
        real_time.as_ref().map(|time| time.elapsed_secs_f64()),
    );
    let mut report = tracking::advance(&mut tracker, &snapshot);
    let playback = if snapshot.mission.status.is_terminal() {
        PlaybackContext::Ended
    } else if crate::session::is_running() {
        PlaybackContext::Running
    } else {
        PlaybackContext::Paused
    };
    attach_physics(&mut report, crate::session::applied_physics(), playback);
    report.debris_dynamics = debris.sample.clone();
    report.debris_dynamics_error = debris.error.clone();
    publish(report);
}

pub(super) fn attach_physics(
    report: &mut Report,
    physics: Result<crate::model::AppliedPhysics, String>,
    playback: PlaybackContext,
) {
    report.playback_context = Some(playback);
    report.applied_physics = None;
    report.applied_physics_error = None;
    report.ship_inertia_body_kg_m2 = None;
    report.metrics.thrust_force_n = None;
    report.metrics.torque_nm = None;
    report.metrics.applied_thrust_accel_mps2 = None;
    let physics = match physics {
        Ok(physics)
            if report.source_time_s == Some(physics.source_time_s)
                && report.mission.tuning_revision == Some(physics.tuning_revision)
                && report.provenance.own_ship_id == Some(crate::domain::EntityId::Lander)
                && report.frame.snapshot_description.as_deref() == Some(physics.frame.as_str())
                && physics.mass_kg.is_finite()
                && physics.mass_kg > 0.0
                && physics
                    .inertia_body_kg_m2
                    .iter()
                    .flatten()
                    .all(|value| value.is_finite()) =>
        {
            physics
        }
        Ok(_) => {
            unavailable_physics(
                report,
                "Live body sample does not match this snapshot. Reconnect or reset the flight."
                    .into(),
            );
            return;
        }
        Err(error) => {
            unavailable_physics(report, error);
            return;
        }
    };
    report.ship_inertia_body_kg_m2 = Some(physics.inertia_body_kg_m2);
    // The flight strip shows the last real actuator application, never a newly
    // edited request pretending that a physics step has already occurred.
    report.metrics.thrust_force_n = physics
        .last_actuator_application
        .and_then(|last| magnitude(last.force_body_n));
    report.metrics.torque_nm = physics
        .last_actuator_application
        .and_then(|last| magnitude(last.torque_body_nm));
    report.metrics.applied_thrust_accel_mps2 = report
        .metrics
        .thrust_force_n
        .map(|force| force / physics.mass_kg);
    report.applied_physics = Some(physics);
}

fn unavailable_physics(report: &mut Report, error: String) {
    report.applied_physics_error = Some(error);
    report.input_issues.push(InputIssue::LivePhysicsUnavailable);
    if report.status == ReportStatus::Ready {
        report.status = ReportStatus::Partial;
    }
}
