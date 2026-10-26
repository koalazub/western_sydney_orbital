//! Bevy UI for mission control and the local viewer camera.

mod diagnostics;
mod editors;
mod input;
mod model;
mod policy;
mod render_text;
mod systems;
mod text_layout;
mod view;

#[cfg(test)]
mod layout_tests;

pub(crate) use editors::{EditorField, InstrumentEditor, instrument_editing};
pub(crate) use model::{HudState, LayoutPart, PANEL_GAP_PX, SCREEN_PADDING_PX};
pub(crate) use render_text::readable as readable_text;

#[cfg(test)]
pub(crate) fn reset_instrument_editing_for_tests() {
    let keys = bevy::prelude::ButtonInput::<bevy::prelude::KeyCode>::default();
    editors::begin_input_frame(false, &keys);
    editors::begin_input_frame(false, &keys);
}

pub fn install(app: &mut bevy::prelude::App) {
    systems::install(app);
}

/// Settled native permission for the global X shortcut, with live input guards.
/// False until a HUD input frame has published its state. No browser calls.
pub(crate) fn fullscreen_shortcut_allowed() -> bool {
    input::fullscreen_shortcut_allowed()
}

/// The native button's last laid-out hit target, in canvas-local CSS pixels.
pub(crate) fn fullscreen_button_at(x: f32, y: f32) -> bool {
    input::fullscreen_button_at(x, y)
}

/// Native keyboard focus, independent of pointer position.
pub(crate) fn fullscreen_button_focused() -> bool {
    input::fullscreen_button_focused()
}

pub(crate) fn notebook_button_at(x: f32, y: f32) -> bool {
    input::notebook_button_at(x, y)
}

pub(crate) fn notebook_button_focused() -> bool {
    input::notebook_button_focused()
}

pub(crate) fn status() -> String {
    diagnostics::status()
}
