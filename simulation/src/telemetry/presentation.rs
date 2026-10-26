use super::{calculations::waiting_report, data::*, instruments::*};
use crate::model::ThrustApplication;
use std::{cell::RefCell, fmt};

thread_local! {
    static REPORT: RefCell<Report> = RefCell::new(waiting_report());
    static EXTERNAL_SAMPLE: RefCell<Option<(u32, serde_json::Value)>> = const { RefCell::new(None) };
}

pub(super) fn publish(report: Report) {
    let snapshot = crate::snapshot();
    if snapshot
        .sensor_state
        .as_ref()
        .is_none_or(|state| state.navigation_delivery() == crate::model::SensorDelivery::Ready)
    {
        let sample = serde_json::to_value(&report).expect("finite telemetry report");
        EXTERNAL_SAMPLE
            .with(|cached| *cached.borrow_mut() = Some((crate::session::trial_revision(), sample)));
    }
    REPORT.with(|state| *state.borrow_mut() = report);
}

pub(crate) fn status() -> String {
    let snapshot = crate::snapshot();
    if let Some(state) = &snapshot.sensor_state {
        let delivery = state.navigation_delivery();
        if delivery == crate::model::SensorDelivery::Frozen {
            let frozen = EXTERNAL_SAMPLE.with(|cached| {
                let cached = cached.borrow();
                let (trial, value) = cached.as_ref()?;
                let sample_time = state.navigation.sample.as_ref()?.sample_time_s;
                (*trial == crate::session::trial_revision()
                    && value["source_time_s"]
                        .as_f64()
                        .is_some_and(|time| time <= sample_time))
                .then(|| value.clone())
            });
            if let Some(mut frozen) = frozen {
                frozen["sensor_delivery"] = serde_json::json!(delivery);
                return frozen.to_string();
            }
        }
        if delivery != crate::model::SensorDelivery::Ready {
            // Native instruments may keep their debug readback for the owner.
            // The serialized external report cannot bypass navigation delivery.
            let mut unavailable = waiting_report();
            unavailable.status = ReportStatus::Unavailable;
            unavailable
                .input_issues
                .push(InputIssue::SensorDeliveryUnavailable);
            let mut value =
                serde_json::to_value(unavailable).expect("finite unavailable telemetry");
            value["sensor_delivery"] = serde_json::json!(delivery);
            return value.to_string();
        }
    }
    REPORT.with(|state| {
        serde_json::to_string(&*state.borrow())
            .expect("telemetry reports contain only finite measurements")
    })
}

pub(crate) fn instrument(key: Instrument) -> String {
    REPORT.with(|state| format_reading(key.reading(&state.borrow())))
}

pub(crate) fn sample_status() -> String {
    REPORT.with(|state| source(&state.borrow()))
}

pub(crate) fn group_status(group: InstrumentGroup) -> String {
    REPORT.with(|state| group_description(group, &state.borrow()))
}

/// Definitions and model caveats are disclosed once, in Details. They do not
/// compete with the measured values in either the flight strip or Instruments.
pub(crate) fn details() -> String {
    REPORT.with(|state| measurement_notes(&state.borrow()))
}

struct Number(Option<f64>, usize);

impl fmt::Display for Number {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let Self(value, decimals) = *self;
        match value.filter(|value| value.is_finite()) {
            Some(value) => write!(formatter, "{value:.decimals$}"),
            None => formatter.write_str("--"),
        }
    }
}

pub(super) fn format_reading(reading: Reading) -> String {
    match reading {
        Reading::Text(value) => value.unwrap_or("--").into(),
        Reading::Scalar(value, unit, decimals) => format!("{} {unit}", Number(value, decimals)),
        Reading::Vector(vector, unit, decimals) => match vector {
            Some(vector) if vector.iter().all(|value| value.is_finite()) => format!(
                "{}, {}, {} {unit}",
                Number(Some(vector[0]), decimals),
                Number(Some(vector[1]), decimals),
                Number(Some(vector[2]), decimals),
            ),
            Some(_) | None => format!("-- {unit}"),
        },
        Reading::Quaternion(rotation) => match rotation {
            Some(rotation) if rotation.iter().all(|value| value.is_finite()) => format!(
                "{}, {}, {}, {}",
                Number(Some(rotation[0]), 4),
                Number(Some(rotation[1]), 4),
                Number(Some(rotation[2]), 4),
                Number(Some(rotation[3]), 4),
            ),
            Some(_) | None => "--".into(),
        },
        Reading::Count(value) => value
            .map(|value| value.to_string())
            .unwrap_or_else(|| "--".into()),
        Reading::Direction(value) => value
            .map(|(axis, sign)| {
                format!(
                    "{}{}",
                    if sign < 0 { "−" } else { "+" },
                    match axis {
                        crate::model::StreamAxis::X => "X",
                        crate::model::StreamAxis::Y => "Y",
                        crate::model::StreamAxis::Z => "Z",
                    }
                )
            })
            .unwrap_or_else(|| "--".into()),
    }
}

pub(super) fn source(report: &Report) -> String {
    let availability = match report.status {
        ReportStatus::WaitingForSnapshot => "waiting for snapshot",
        ReportStatus::Ready => "ready",
        ReportStatus::Partial => "partial",
        ReportStatus::Unavailable => "unavailable",
    };
    format!(
        "Source {} s · {availability}",
        Number(report.source_time_s, 3)
    )
}

pub(super) fn group_description(group: InstrumentGroup, report: &Report) -> String {
    match group {
        InstrumentGroup::Flight | InstrumentGroup::Mission | InstrumentGroup::World => String::new(),
        InstrumentGroup::Stream => {
            format!(
                "{}\n{}\n{}\nShape model: spherical Rapier colliders. Birth density: {:.0} kg/m^3. Individual inertia tensors are not sampled here.",
                match report.playback_context {
                    Some(PlaybackContext::Running) => "Running. Body motion is sampled about twice per wall-clock second.",
                    Some(PlaybackContext::Paused) => "Paused. Nonzero stored velocities do not mean the asteroids are animating.",
                    Some(PlaybackContext::Ended) => "Mission ended. Body motion is not advancing.",
                    None => "Playback unavailable.",
                },
                report.debris_dynamics_error.as_deref().unwrap_or(if report.debris_dynamics.is_some() {
                    "Measured motion comes from the live Rapier bodies, not the stream reference. Native debug only, not an SDK sensor."
                } else { "Waiting for live asteroid-body measurements." }),
                match report.debris_distribution {
                    Some(crate::model::DebrisDistribution::ImpactEjecta { .. }) => "Impact ejecta. Stream reference is inactive. Change ejection parameters in World setup, then create a new world.",
                    Some(crate::model::DebrisDistribution::DirectedStream) | None => "Stream reference is an input, not measured orbital speed. Spread affects new births.",
                },
                crate::scenario::settings().debris.density_kg_m3,
            )
        }
        InstrumentGroup::Acceleration => match report.observed_frame_acceleration.status {
            AccelerationStatus::WaitingForNextSample => "Waiting for next timed sample",
            AccelerationStatus::FreshInterval => "New interval · delta frame velocity / delta time",
            AccelerationStatus::HeldWithoutTimeAdvance => "Held; no time advance",
            AccelerationStatus::BaselineReset => "Baseline reset; waiting for next timed sample",
            AccelerationStatus::Unavailable => "Unavailable",
        }
        .into(),
        InstrumentGroup::Peaks => format!(
            "Epoch {} · {}",
            report
                .epoch
                .started_at_time_s
                .map(|_| report.epoch.index.to_string())
                .unwrap_or_else(|| "--".into()),
            match report.epoch.last_reset {
                None => "waiting for snapshot",
                Some(EpochReset::InitialSample) => "initial sample",
                Some(EpochReset::SeedChanged) => "seed changed",
                Some(EpochReset::TimeRewound) => "time rewound",
                Some(EpochReset::ReferenceChanged) => "reference changed",
                Some(EpochReset::SameTimeKinematicsChanged) => "same-time kinematics changed",
                Some(EpochReset::InputDiscontinuity) => "input discontinuity",
                Some(EpochReset::InputRecovered) => "input recovered",
            },
        ),
        InstrumentGroup::Actuation => format!(
            "{} · {}\n{}",
            match report.playback_context {
                Some(PlaybackContext::Running) => "Running",
                Some(PlaybackContext::Paused) => "Paused · forces do not advance motion",
                Some(PlaybackContext::Ended) => "Mission ended · Reset starts another flight",
                None => "Playback unavailable",
            },
            match report.actuation.thrust_application {
                Some(ThrustApplication::CenterOfMass) => "center of mass",
                Some(ThrustApplication::ManualNozzles) => "manual nozzles",
                Some(ThrustApplication::PracticeBoost) => "practice boost",
                None => "--",
            },
            report.applied_physics_error.as_deref().unwrap_or(if report.applied_physics.is_some() {
                "Live Rapier body and last actuator application. Acceleration requests scale force with mass. User-force totals exclude contact impulses."
            } else { "Live Rapier body unavailable; snapshot values alone do not confirm application." }),
        ),
        InstrumentGroup::Ship => actor_identity(report.provenance.own_ship_id),
        InstrumentGroup::Target => actor_identity(report.provenance.target_id),
    }
}

fn actor_identity(id: Option<crate::domain::EntityId>) -> String {
    id.map(|id| format!("Actor {id}"))
        .unwrap_or_else(|| "Actor --".into())
}

pub(super) fn measurement_notes(report: &Report) -> String {
    let frame = if report.frame.recognized {
        "Phobos-centered Mars-rotating. +X toward Mars, +Y orbit normal, +Z right-handed."
    } else {
        "Frame unavailable or unrecognized."
    };
    format!(
        "Local practice model, not the STP SDK. Measurements are read-only.\n{frame}\nSpeed and actor velocity use the rotating frame, not inertial or camera speed.\nTerrain clearance uses the lander center along radial up. It is not feet clearance, pad altitude or a collision margin. Climb is radial velocity, not terrain-normal velocity.\nTarget range is center-to-center; positive closing means toward the target.\nObserved acceleration is mean delta frame velocity / delta sample time. It includes gravity, contacts and rotating-frame terms, not thrust alone.\nRequested acceleration is the stored bounded body-frame command. Last-applied force and torque are recorded Rapier accumulator deltas before gravity, at the displayed application time and tuning revision. A new request can await application. Thrust on the flight strip uses that record divided by actual body mass. Paused forces do not advance motion. Rapier user-force totals include gravity and rotating-frame terms but exclude solver contact impulses. Inertia is the live body-frame tensor. Snapshot wrench values in Lander kinematics are separate cached values, not proof of application.\nFuel used and damage are local estimates. Stored fuel may be unlimited; fixed mass can apply. Peaks cover received samples in the current epoch, not every physics substep.\nReference command limit {} m/s². Flight algorithms and scheduling remain in Python.\nSeed/time/reference changes and same-time kinematic changes reset the epoch. Forward-time restores indistinguishable from flight cannot be detected.\nMissing values are --, never assumed zero.",
        Number(report.caveats.reference_command_limit_mps2, 2),
    )
}
