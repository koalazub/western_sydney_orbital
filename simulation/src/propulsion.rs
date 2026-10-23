//! Body-frame forces at synthetic practice mounts, not an authoritative STP nozzle map.

use crate::{
    domain::{EntityId, EntityKind},
    model::EntityState,
    scenario::settings,
};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ThrustApplication {
    #[default]
    CenterOfMass,
    ManualNozzles,
    /// Local-only positive body-Z assist, not an STP actuation mode.
    PracticeBoost,
}

pub(crate) fn accel_limit(application: ThrustApplication) -> f64 {
    match application {
        ThrustApplication::CenterOfMass | ThrustApplication::ManualNozzles => {
            settings().lander.max_accel_mps2
        }
        ThrustApplication::PracticeBoost => settings().practice.space_lift_accel_mps2,
    }
}

/// Available local hardware, independent of the selected input/profile limit.
pub(crate) fn local_max_thrust_n() -> f64 {
    settings().lander.mass_kg
        * accel_limit(ThrustApplication::CenterOfMass)
            .max(accel_limit(ThrustApplication::PracticeBoost))
}

pub(crate) fn space_shape_is_valid(accel: [f64; 3]) -> bool {
    accel.iter().all(|v| v.is_finite())
        && accel[2] >= 0.0
        && accel[0].hypot(accel[1]) <= settings().lander.max_accel_mps2 + 1e-9
}

pub(crate) fn accel_within_limits(application: ThrustApplication, accel: [f64; 3]) -> bool {
    accel.iter().all(|v| v.is_finite())
        && accel[0].hypot(accel[1]).hypot(accel[2]) <= accel_limit(application) + 1e-9
        && match application {
            ThrustApplication::CenterOfMass | ThrustApplication::ManualNozzles => true,
            ThrustApplication::PracticeBoost => space_shape_is_valid(accel),
        }
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct AppliedJet {
    pub point_body_m: [f64; 3],
    pub force_body_n: [f64; 3],
}

#[derive(Clone, Copy, Debug, Default)]
pub(crate) struct AppliedWrench {
    pub force_body_n: [f64; 3],
    pub torque_body_nm: [f64; 3],
    /// Sum of individual force magnitudes, including any cancelling components.
    pub propellant_force_n: f64,
}

/// Actual body forces, independent of input ownership, cameras, or rendering.
/// Manual and practice-boost mounts are ordered by force +X, -X, +Y, -Y, +Z, -Z.
/// Each active axis uses one mount, so a net command emits at most three jets.
/// Exhaust travels opposite each `force_body_n`; the center mode has no jet moment.
pub(crate) fn applied_jets(lander: &EntityState) -> Vec<AppliedJet> {
    let cfg = settings().lander;
    let force = lander.thrust_n;
    let magnitude = force[0].hypot(force[1]).hypot(force[2]);
    if lander.id != EntityId::Lander
        || lander.kind != EntityKind::Lander
        || !force.iter().all(|v| v.is_finite())
        || !magnitude.is_finite()
        || magnitude == 0.0
        || !accel_within_limits(
            lander.thrust_application,
            force.map(|component| component / cfg.mass_kg),
        )
    {
        return Vec::new();
    }
    match lander.thrust_application {
        ThrustApplication::CenterOfMass => vec![AppliedJet {
            point_body_m: [0.0; 3],
            force_body_n: force,
        }],
        ThrustApplication::ManualNozzles | ThrustApplication::PracticeBoost => {
            let mounts = cfg.manual_nozzles_body_m;
            if !mounts.iter().flatten().all(|v| v.is_finite()) {
                return Vec::new();
            }
            let mut jets = Vec::with_capacity(3);
            for (axis, component) in force.into_iter().enumerate() {
                if component == 0.0 {
                    continue;
                }
                let nozzle = axis * 2 + usize::from(component < 0.0);
                let mut force_body_n = [0.0; 3];
                force_body_n[axis] = component;
                jets.push(AppliedJet {
                    point_body_m: mounts[nozzle],
                    force_body_n,
                });
            }
            jets
        }
    }
}

/// Applied moment about the explicitly configured body origin/center of mass.
/// Direct angular-control torque and contact impulses are not nozzle forces.
pub(crate) fn applied_wrench(jets: &[AppliedJet]) -> AppliedWrench {
    let mut wrench = AppliedWrench::default();
    for jet in jets {
        let [x, y, z] = jet.point_body_m;
        let [fx, fy, fz] = jet.force_body_n;
        let moment = [y * fz - z * fy, z * fx - x * fz, x * fy - y * fx];
        for (axis, moment_nm) in moment.into_iter().enumerate() {
            wrench.force_body_n[axis] += jet.force_body_n[axis];
            wrench.torque_body_nm[axis] += moment_nm;
        }
        wrench.propellant_force_n += fx.hypot(fy).hypot(fz);
    }
    wrench
}

#[cfg(test)]
mod tests;
