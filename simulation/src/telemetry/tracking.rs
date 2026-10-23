use super::{
    calculations::{finite, magnitude, measure, nonnegative, vector},
    data::*,
};
use crate::model::Snapshot;

/// One kinematic baseline and three maxima are the entire retained history.
pub(super) fn advance(tracker: &mut Tracker, snapshot: &Snapshot) -> Report {
    let Measurement {
        mut report,
        kinematics,
    } = measure(snapshot);
    let reference = Reference {
        seed: snapshot.seed,
        frame: snapshot.frame.clone(),
        terrain_recognized: report.frame.terrain_recognized,
        own_ship_id: snapshot.mission.shuttle_id,
        target_id: snapshot.mission.target_id,
    };
    let reset = reset_reason(tracker, &reference, report.source_time_s, kinematics);
    if let Some(reason) = reset {
        tracker.epoch = Epoch {
            index: tracker.epoch.index.saturating_add(1),
            seed: Some(snapshot.seed),
            started_at_time_s: kinematics.map(|sample| sample.time_s),
            observed_duration_s: kinematics.map(|_| 0.0),
            last_reset: Some(reason),
        };
        tracker.peaks = Peaks::default();
        tracker.acceleration = ObservedFrameAcceleration {
            status: match (kinematics.is_some(), reason) {
                (false, _) => AccelerationStatus::Unavailable,
                (true, EpochReset::InitialSample) => AccelerationStatus::WaitingForNextSample,
                (
                    true,
                    EpochReset::SeedChanged
                    | EpochReset::TimeRewound
                    | EpochReset::ReferenceChanged
                    | EpochReset::SameTimeKinematicsChanged
                    | EpochReset::InputDiscontinuity
                    | EpochReset::InputRecovered,
                ) => AccelerationStatus::BaselineReset,
            },
            ..ObservedFrameAcceleration::default()
        };
    } else {
        tracker.acceleration = match (tracker.previous, kinematics) {
            (Some(previous), Some(current)) if current.time_s > previous.time_s => {
                let acceleration = observed_acceleration(previous, current);
                if acceleration.status == AccelerationStatus::Unavailable {
                    report.input_issues.push(InputIssue::NonfiniteDerivedValue);
                    if report.status == ReportStatus::Ready {
                        report.status = ReportStatus::Partial;
                    }
                }
                acceleration
            }
            (Some(_), Some(_)) => ObservedFrameAcceleration {
                status: if tracker.acceleration.vector_mps2.is_some() {
                    AccelerationStatus::HeldWithoutTimeAdvance
                } else {
                    AccelerationStatus::WaitingForNextSample
                },
                ..tracker.acceleration
            },
            (None, Some(_)) => ObservedFrameAcceleration::default(),
            (Some(_), None) | (None, None) => ObservedFrameAcceleration {
                status: AccelerationStatus::Unavailable,
                ..ObservedFrameAcceleration::default()
            },
        };
    }
    if kinematics.is_some() {
        tracker.peaks.speed_mps = maximum(tracker.peaks.speed_mps, report.metrics.speed_mps);
        tracker.peaks.radial_climb_mps = maximum(
            tracker.peaks.radial_climb_mps,
            report.metrics.radial_climb_mps,
        );
        tracker.peaks.terrain_center_clearance_m = maximum(
            tracker.peaks.terrain_center_clearance_m,
            report.metrics.terrain_center_clearance_m,
        );
    }
    tracker.epoch.observed_duration_s = report
        .source_time_s
        .zip(tracker.epoch.started_at_time_s)
        .and_then(|(now, start)| nonnegative(now - start));
    tracker.reference = Some(reference);
    tracker.previous_time_s = report.source_time_s;
    tracker.previous = kinematics;
    report.epoch = tracker.epoch.clone();
    report.peaks = tracker.peaks;
    report.observed_frame_acceleration = tracker.acceleration;
    report
}

fn reset_reason(
    tracker: &Tracker,
    current: &Reference,
    time_s: Option<f64>,
    kinematics: Option<Kinematics>,
) -> Option<EpochReset> {
    let Some(previous) = &tracker.reference else {
        return Some(EpochReset::InitialSample);
    };
    if previous.seed != current.seed {
        return Some(EpochReset::SeedChanged);
    }
    if previous != current {
        return Some(EpochReset::ReferenceChanged);
    }
    if time_s
        .zip(tracker.previous_time_s)
        .is_some_and(|(now, before)| now < before)
    {
        return Some(EpochReset::TimeRewound);
    }
    match (tracker.previous, kinematics) {
        (Some(before), Some(now)) if now.time_s == before.time_s && now != before => {
            Some(EpochReset::SameTimeKinematicsChanged)
        }
        (Some(_), None) => Some(EpochReset::InputDiscontinuity),
        (None, Some(_)) => Some(EpochReset::InputRecovered),
        (Some(_), Some(_)) | (None, None) => None,
    }
}

fn maximum(previous: Option<f64>, current: Option<f64>) -> Option<f64> {
    match (previous, current) {
        (Some(previous), Some(current)) => Some(previous.max(current)),
        (Some(previous), None) => Some(previous),
        (None, Some(current)) => Some(current),
        (None, None) => None,
    }
}

fn observed_acceleration(before: Kinematics, now: Kinematics) -> ObservedFrameAcceleration {
    let unavailable = ObservedFrameAcceleration {
        status: AccelerationStatus::Unavailable,
        ..ObservedFrameAcceleration::default()
    };
    let Some(interval_s) = finite(now.time_s - before.time_s).filter(|dt| *dt > 0.0) else {
        return unavailable;
    };
    let Some(value) = vector(std::array::from_fn(|axis| {
        (now.velocity_mps[axis] - before.velocity_mps[axis]) / interval_s
    })) else {
        return unavailable;
    };
    let Some(norm) = magnitude(value) else {
        return unavailable;
    };
    ObservedFrameAcceleration {
        status: AccelerationStatus::FreshInterval,
        vector_mps2: Some(value),
        magnitude_mps2: Some(norm),
        interval_start_s: Some(before.time_s),
        interval_end_s: Some(now.time_s),
        interval_s: Some(interval_s),
    }
}
