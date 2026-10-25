use crate::domain::{CameraMode, EntityId};
use bevy::prelude::*;
use serde::Serialize;

#[derive(Resource)]
pub(crate) struct CameraRig {
    pub mode: CameraMode,
    pub focus: EntityId,
    pub(crate) yaw: f32,
    pub(crate) pitch: f32,
    pub(crate) distance_km: f32,
    pub(crate) zoom: f32,
    pub(crate) speed_mps: f32,
    pub(crate) pan_offset: Vec3,
    pub(crate) initialized: bool,
}

impl Default for CameraRig {
    fn default() -> Self {
        Self {
            mode: CameraMode::Overview,
            focus: EntityId::Phobos,
            yaw: -0.72,
            pitch: 0.28,
            distance_km: crate::model::phobos_enclosing_radius_m().max(
                crate::model::phobos_radii_m()
                    .into_iter()
                    .fold(0.0_f64, f64::max),
            ) as f32
                * 0.005,
            zoom: 1.0,
            speed_mps: 100.0,
            pan_offset: Vec3::ZERO,
            initialized: false,
        }
    }
}

#[derive(Clone, Copy, Debug, Serialize)]
pub struct CameraStatus {
    pub mode: CameraMode,
    pub focus: EntityId,
    pub position_m: [f64; 3],
    pub rotation: [f32; 4],
    pub distance_m: f64,
    pub active: bool,
    pub locked: bool,
    pub speed_mps: f32,
}

impl Default for CameraStatus {
    fn default() -> Self {
        let rig = CameraRig::default();
        Self {
            mode: rig.mode,
            focus: rig.focus,
            position_m: [0.0; 3],
            rotation: [0.0, 0.0, 0.0, 1.0],
            distance_m: f64::from(rig.distance_km) * 1_000.0,
            active: false,
            locked: false,
            speed_mps: 100.0,
        }
    }
}

impl CameraStatus {
    pub(crate) fn from_view(
        rig: &CameraRig,
        transform: &Transform,
        target: Vec3,
        active: bool,
        locked: bool,
    ) -> Self {
        let position = transform.translation.as_dvec3() / 0.001;
        let rotation = transform.rotation;
        Self {
            mode: rig.mode,
            focus: if rig.mode.sensor().is_some() {
                EntityId::Lander
            } else {
                rig.focus
            },
            position_m: [position.x, position.y, position.z],
            rotation: [rotation.x, rotation.y, rotation.z, rotation.w],
            distance_m: transform.translation.distance(target) as f64 / 0.001,
            active,
            locked,
            speed_mps: rig.speed_mps,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CameraError {
    InvalidZoom,
    UnknownFocus(EntityId),
}

impl std::fmt::Display for CameraError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidZoom => formatter.write_str("zoom must be finite and between 0.05 and 20"),
            Self::UnknownFocus(id) => write!(formatter, "unknown camera focus: {id}"),
        }
    }
}

impl std::error::Error for CameraError {}
