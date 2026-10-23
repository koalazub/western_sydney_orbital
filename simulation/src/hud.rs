//! Bevy UI for mission control and the local viewer camera.

mod diagnostics;
mod input;
mod model;
mod systems;
mod view;

#[cfg(test)]
mod layout_tests;

pub(crate) use model::{HudState, LayoutPart, PANEL_GAP_PX, SCREEN_PADDING_PX};

pub fn install(app: &mut bevy::prelude::App) {
    systems::install(app);
}

/// The native button's last laid-out hit target, in canvas-local CSS pixels.
pub(crate) fn fullscreen_button_at(x: f32, y: f32) -> bool {
    input::fullscreen_button_at(x, y)
}

/// Native keyboard focus, independent of pointer position.
pub(crate) fn fullscreen_button_focused() -> bool {
    input::fullscreen_button_focused()
}

pub(crate) fn status() -> String {
    diagnostics::status()
}
