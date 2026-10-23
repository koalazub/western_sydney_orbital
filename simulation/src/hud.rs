//! Bevy UI for mission control and the local viewer camera.

mod model;
mod systems;
mod view;

pub(crate) use model::{LayoutPart, PANEL_GAP_PX, SCREEN_PADDING_PX};

pub fn install(app: &mut bevy::prelude::App) {
    systems::install(app);
}
