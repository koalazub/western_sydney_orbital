//! Bevy UI for mission control and the local viewer camera.

mod model;
mod systems;
mod view;

pub fn install(app: &mut bevy::prelude::App) {
    systems::install(app);
}
