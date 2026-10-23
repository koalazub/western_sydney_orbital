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
/// Manual mounts are ordered by force +X, -X, +Y, -Y, +Z, -Z.
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
        || magnitude > cfg.mass_kg * cfg.max_accel_mps2 + 1e-6
    {
        return Vec::new();
    }
    match lander.thrust_application {
        ThrustApplication::CenterOfMass => vec![AppliedJet {
            point_body_m: [0.0; 3],
            force_body_n: force,
        }],
        ThrustApplication::ManualNozzles => {
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
