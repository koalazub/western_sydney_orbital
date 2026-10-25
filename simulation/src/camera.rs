//! Typed camera API and Bevy integration.

mod controller;
mod model;
mod movement;
mod overview;
mod zoom;

pub(crate) use controller::SimulationCamera;
pub(crate) use model::CameraRig;
pub use model::{CameraError, CameraStatus};

use crate::domain::CameraRequest;
use bevy::prelude::*;

pub(crate) fn initial_overview(
    rig: &mut CameraRig,
    snapshot: &crate::model::Snapshot,
    window: Option<&Window>,
) -> (Transform, Projection) {
    let mut projection = Projection::Perspective(PerspectiveProjection {
        near: 0.0005,
        far: 20_000.0,
        fov: 60.0_f32.to_radians(),
        ..default()
    });
    overview::update_viewport_aspect(&Camera::default(), window, &mut projection);
    overview::frame(rig, snapshot);
    let view = overview::view(rig, snapshot, &mut projection);
    (view, projection)
}

pub fn install(app: &mut App) {
    controller::install(app);
}

pub fn set_input_active(active: bool, locked: bool) {
    controller::set_input_active(active, locked);
}

pub fn input_active() -> bool {
    controller::input_active()
}

pub(crate) fn pinch_zoom(log_distance_delta: f32) {
    controller::pinch_zoom(log_distance_delta);
}

/// Validate a typed request and enqueue it for the camera system.
pub fn request_view(request: CameraRequest) -> Result<(), CameraError> {
    controller::request_view(request)
}

pub fn status() -> CameraStatus {
    controller::status()
}
