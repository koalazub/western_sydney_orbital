//! Fixed-optics, steerable ship sensors, separate from the debug viewer.
mod capture;
mod model;
mod optics;
mod systems;
mod view;

pub(crate) use capture::{invalidate_pending, request_capture};
pub(crate) use systems::{attach_health, pan_tilt, pose, status};

pub(crate) fn invalidate_trial() {
    capture::invalidate_trial();
    systems::invalidate_trial();
}

pub(crate) fn install(app: &mut bevy::prelude::App) {
    use bevy::prelude::*;
    app.add_systems(
        Startup,
        (systems::setup, view::setup)
            .chain()
            .after(crate::scene::setup),
    )
    .add_systems(Update, systems::update.in_set(crate::EngineSet::Scene))
    .add_systems(Update, view::update.in_set(crate::EngineSet::Hud));
}
