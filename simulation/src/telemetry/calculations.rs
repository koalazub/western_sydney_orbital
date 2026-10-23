use super::data::*;
use crate::{
    domain::{EntityId, EntityKind},
    model::{EntityState, Snapshot, ThrustApplication},
    scenario::settings,
};
use bevy::math::DVec3;

pub(super) fn finite(value: f64) -> Option<f64> {
    value.is_finite().then_some(value)
}

pub(super) fn nonnegative(value: f64) -> Option<f64> {
    finite(value).filter(|value| *value >= 0.0)
}

fn positive(value: f64) -> Option<f64> {
    finite(value).filter(|value| *value > 0.0)
}

pub(super) fn vector(value: [f64; 3]) -> Option<[f64; 3]> {
    value.iter().all(|value| value.is_finite()).then_some(value)
}

pub(super) fn magnitude(value: [f64; 3]) -> Option<f64> {
    let [x, y, z] = vector(value)?;
    finite(x.hypot(y).hypot(z))
}

fn difference(a: [f64; 3], b: [f64; 3]) -> Option<[f64; 3]> {
    vector(std::array::from_fn(|axis| a[axis] - b[axis]))
}

fn projection(a: [f64; 3], direction: [f64; 3]) -> Option<f64> {
    finite(a[0] * direction[0] + a[1] * direction[1] + a[2] * direction[2])
}

fn rotation(value: [f64; 4]) -> Option<[f64; 4]> {
    let norm = value[0].hypot(value[1]).hypot(value[2]).hypot(value[3]);
    (value.iter().all(|value| value.is_finite()) && (0.99..=1.01).contains(&norm)).then_some(value)
}

/// Inspect only identity/kind while selecting. Never select a debris actor as
/// own state or target, even if a malformed mission points at one.
fn selected(snapshot: &Snapshot, id: EntityId, kind: EntityKind) -> Option<&EntityState> {
    if !matches!(
        (id, kind),
        (EntityId::Lander, EntityKind::Lander) | (EntityId::ReturnShip, EntityKind::ReturnShip)
    ) {
        return None;
    }
    let mut matching = snapshot.assets.iter().filter(|actor| actor.id == id);
    let actor = matching.next()?;
    (matching.next().is_none() && actor.kind == kind).then_some(actor)
}

pub(super) fn waiting_report() -> Report {
    let config = settings();
    Report {
        schema_version: 1,
        status: ReportStatus::WaitingForSnapshot,
        input_issues: Vec::new(),
        source_time_s: None,
        epoch: Epoch::default(),
        frame: FrameMetadata {
            snapshot_description: None,
            recognized: false,
            terrain_recognized: false,
            origin: "Phobos center",
            axes: "+X toward Mars; +Y orbit normal; +Z right-handed",
            speed_basis: "Phobos-centered Mars-Phobos circular rotating frame, not inertial",
        },
        metrics: FlightMetrics::default(),
        observed_frame_acceleration: ObservedFrameAcceleration::default(),
        peaks: Peaks::default(),
        actuation: Actuation::default(),
        units: Units {
            distance: "m",
            speed: "m/s",
            acceleration: "m/s^2",
            force: "N",
            torque: "N m",
            angular_speed: "rad/s",
            mass: "kg",
            energy: "J",
            simulation_time: "s",
        },
        provenance: Provenance {
            own_ship_id: None,
            target_id: None,
            own_state: "immutable snapshot actor selected by mission.shuttle_id and lander kind",
            target_state: "immutable snapshot actor selected by mission.target_id and return-ship kind",
            terrain: "versioned local terrain::surface_radius_m mesh intersection",
            fuel_and_damage: "snapshot mission accumulators, local model estimates",
            hidden_debris_truth_used: false,
            camera_state_used: false,
        },
        caveats: Caveats {
            local_practice_only: true,
            stp_parity: false,
            read_only: true,
            reference_command_limit_mps2: positive(config.lander.max_accel_mps2),
            practice_boost_limit_mps2: positive(config.practice.space_lift_accel_mps2),
            practice_boost_enabled_in_settings: config.practice.space_boost_enabled,
            fuel_unlimited: config.lander.fuel_unlimited,
            speed: "Magnitude of own velocity in the rotating frame, not inertial speed or camera speed.",
            clearance: "Radial center clearance |position| minus known terrain radius. Not feet clearance, pad altitude or a collision margin.",
            climb: "Own velocity dotted with radial up. Not terrain-normal velocity or the derivative of terrain clearance.",
            target_range: "Center-to-center distance, with neither actor radius subtracted.",
            target_closing: "Positive toward target: minus relative velocity dotted with normalized target-minus-own position. Undefined at coincident centers.",
            observed_acceleration: "Average delta rotating-frame velocity / delta simulation time between telemetry samples. Includes gravity, contacts and rotating-coordinate terms, not thrust alone. May span several physics substeps.",
            actuation: "Force and torque are magnitudes of the own actor's stored applied wrench. Thrust acceleration is force / actor mass, not configured maximum, keyboard request or inertial acceleration. Boost active describes the actual stored mode, not the selected UI profile.",
            fuel: "Existing estimated impulse / Isp / standard-gravity accumulator. Unlimited fuel and fixed vehicle mass may apply; this is not remaining propellant.",
            damage: "Existing estimated accumulated contact closing energy, not an STP damage measurement.",
            peaks: "Maxima of telemetry samples within this epoch, not every internal physics substep. Climb is a signed maximum, not maximum descent magnitude.",
            restore_detection: "Seed changes, time rewinds, frame/identity changes and same-time own-kinematic changes reset history. A forward-time restore indistinguishable from flight cannot be identified from snapshots alone.",
        },
    }
}

pub(super) fn measure(snapshot: &Snapshot) -> Measurement {
    let mut report = waiting_report();
    report.source_time_s = nonnegative(snapshot.time_s);
    report.frame.snapshot_description = Some(snapshot.frame.clone());
    report.frame.recognized = snapshot.frame == settings().frame_description;
    report.frame.terrain_recognized =
        snapshot.body.name == "Phobos" && snapshot.body.radii_m == settings().terrain.radii_m;
    let own = selected(snapshot, snapshot.mission.shuttle_id, EntityKind::Lander);
    let target = selected(snapshot, snapshot.mission.target_id, EntityKind::ReturnShip);
    report.provenance.own_ship_id = own.map(|actor| actor.id);
    report.provenance.target_id = target.map(|actor| actor.id);
    report.actuation = Actuation {
        thrust_application: own.map(|actor| actor.thrust_application),
        practice_boost_active: own
            .map(|actor| actor.thrust_application == ThrustApplication::PracticeBoost),
    };
    if report.source_time_s.is_none() {
        report.input_issues.push(InputIssue::InvalidSimulationTime);
    }
    if !report.frame.recognized {
        report.input_issues.push(InputIssue::UnknownFrame);
    }
    if !report.frame.terrain_recognized {
        report.input_issues.push(InputIssue::UnknownTerrain);
    }
    if own.is_none() {
        report
            .input_issues
            .push(InputIssue::MissingOrAmbiguousOwnShip);
    }
    if target.is_none() {
        report
            .input_issues
            .push(InputIssue::MissingOrAmbiguousMissionTarget);
    }
    let Some((own, time_s)) = own
        .zip(report.source_time_s)
        .filter(|_| report.frame.recognized)
    else {
        report.status = ReportStatus::Unavailable;
        return Measurement {
            report,
            kinematics: None,
        };
    };

    let metrics = &mut report.metrics;
    let issues = &mut report.input_issues;
    let position = vector(own.position).filter(|value| magnitude(*value).is_some());
    let velocity = vector(own.velocity).filter(|value| magnitude(*value).is_some());
    let orientation = rotation(own.rotation);
    let angular_velocity =
        vector(own.angular_velocity_rad_s).filter(|value| magnitude(*value).is_some());
    if position.is_none() {
        issues.push(InputIssue::InvalidOwnPosition);
    }
    if velocity.is_none() {
        issues.push(InputIssue::InvalidOwnVelocity);
    }
    if orientation.is_none() {
        issues.push(InputIssue::InvalidOwnRotation);
    }
    if angular_velocity.is_none() {
        issues.push(InputIssue::InvalidAngularVelocity);
    }
    metrics.speed_mps = velocity.and_then(magnitude);
    metrics.angular_speed_rad_s = angular_velocity.and_then(magnitude);
    if let Some((position, radius)) = position.zip(position.and_then(magnitude)) {
        if radius > 0.0 {
            let up = position.map(|coordinate| coordinate / radius);
            metrics.radial_climb_mps = velocity.and_then(|velocity| projection(velocity, up));
            if velocity.is_some() && metrics.radial_climb_mps.is_none() {
                issues.push(InputIssue::NonfiniteDerivedValue);
            }
            if report.frame.terrain_recognized {
                metrics.terrain_center_clearance_m =
                    positive(crate::terrain::surface_radius_m(DVec3::from_array(up)))
                        .and_then(|surface| finite(radius - surface));
                if metrics.terrain_center_clearance_m.is_none() {
                    issues.push(InputIssue::NonfiniteDerivedValue);
                }
            }
        } else {
            issues.push(InputIssue::UndefinedRadialDirection);
        }
    }

    metrics.thrust_force_n = magnitude(own.thrust_n);
    if metrics.thrust_force_n.is_none() {
        issues.push(InputIssue::InvalidThrust);
    }
    let mass = positive(own.mass_kg);
    if mass.is_none() {
        issues.push(InputIssue::InvalidMass);
    }
    metrics.applied_thrust_accel_mps2 = metrics
        .thrust_force_n
        .zip(mass)
        .and_then(|(force, mass)| finite(force / mass));
    if metrics.thrust_force_n.is_some()
        && mass.is_some()
        && metrics.applied_thrust_accel_mps2.is_none()
    {
        issues.push(InputIssue::NonfiniteDerivedValue);
    }
    metrics.torque_nm = magnitude(own.torque_nm);
    if metrics.torque_nm.is_none() {
        issues.push(InputIssue::InvalidTorque);
    }
    metrics.fuel_used_kg_estimate = nonnegative(snapshot.mission.fuel_used_kg);
    if metrics.fuel_used_kg_estimate.is_none() {
        issues.push(InputIssue::InvalidFuelEstimate);
    }
    metrics.damage_j_estimate = nonnegative(snapshot.mission.damage);
    if metrics.damage_j_estimate.is_none() {
        issues.push(InputIssue::InvalidDamageEstimate);
    }

    if let Some(target) = target {
        let target_position = vector(target.position);
        let target_velocity = vector(target.velocity);
        if target_position.is_none() {
            issues.push(InputIssue::InvalidTargetPosition);
        }
        if target_velocity.is_none() {
            issues.push(InputIssue::InvalidTargetVelocity);
        }
        if let Some((position, target_position)) = position.zip(target_position) {
            let relative_position = difference(target_position, position);
            metrics.target_center_range_m = relative_position.and_then(magnitude);
            if let Some((relative_position, range)) =
                relative_position.zip(metrics.target_center_range_m)
            {
                if range > 0.0 {
                    metrics.target_closing_mps = velocity
                        .zip(target_velocity)
                        .and_then(|(own, target)| difference(target, own))
                        .and_then(|velocity| {
                            projection(velocity, relative_position.map(|x| x / range))
                        })
                        .and_then(|range_rate| finite(-range_rate));
                    if velocity.is_some()
                        && target_velocity.is_some()
                        && metrics.target_closing_mps.is_none()
                    {
                        issues.push(InputIssue::NonfiniteDerivedValue);
                    }
                } else {
                    issues.push(InputIssue::UndefinedClosingDirection);
                }
            } else {
                issues.push(InputIssue::NonfiniteDerivedValue);
            }
        }
    }
    report.status = if issues.is_empty() {
        ReportStatus::Ready
    } else {
        ReportStatus::Partial
    };
    let kinematics = position
        .zip(velocity)
        .zip(orientation)
        .zip(angular_velocity)
        .map(
            |(((position_m, velocity_mps), rotation_xyzw), angular_velocity_rad_s)| Kinematics {
                time_s,
                position_m,
                velocity_mps,
                rotation_xyzw,
                angular_velocity_rad_s,
            },
        );
    Measurement { report, kinematics }
}
