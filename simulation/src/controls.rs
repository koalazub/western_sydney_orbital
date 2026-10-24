//! Pure keyboard routing and ship-command ownership, with input-only adapters.

mod bindings;
mod model;
mod pilot;
mod routing;

pub(crate) use bindings::{
    Action, actions_blocked as binding_actions_blocked, begin_capture, begin_input_frame,
    cancel_capture, capture_key, capture_message, key_label, manual_input_blocked,
    observe_input_release, restore_defaults,
};
pub(crate) use model::{Movement, MovementTarget, PilotContext, PilotEffect};
pub(crate) use pilot::KeyboardPilot;

/// Versioned binding map and capture state for the thin browser input adapter.
pub fn bindings_status() -> String {
    bindings::status()
}

/// Replace all seven bindings atomically. Physical DOM codes are parsed here.
pub fn set_bindings(json: &str) -> Result<(), String> {
    bindings::set(json)
}

pub fn binding_capture_active() -> bool {
    bindings::capture_active()
}

#[cfg(test)]
pub(crate) use bindings::reset_for_tests;

/// Decode abstract action bits without reading platform state.
pub(crate) fn decode(mask: u16) -> Movement {
    routing::decode(mask)
}

/// Store the latest browser action-bit sample. This does not call browser APIs.
pub(crate) fn set_movement_keys(mask: u16) {
    routing::set_movement_keys(mask);
}

/// Use the authoritative browser sample on WASM and held Bevy keys on native.
pub(crate) fn movement(keys: &bevy::prelude::ButtonInput<bevy::prelude::KeyCode>) -> Movement {
    let mask = routing::key_mask(keys);
    let guarded_held = bindings::observe_release(keys, mask);
    let mut movement = decode(mask);
    if manual_input_blocked() {
        movement.target = MovementTarget::Suppressed;
        movement.axes = [0.0; 3];
        movement.boost = false;
        movement.held |= guarded_held;
    }
    movement
}

/// The configured fresh-launch/lift action uses bit 9 in the shared protocol.
pub(crate) fn lift_key_down(keys: &bevy::prelude::ButtonInput<bevy::prelude::KeyCode>) -> bool {
    routing::key_mask(keys) & Action::Lift.bit() != 0
}

pub(crate) fn held_key_labels(keys: &bevy::prelude::ButtonInput<bevy::prelude::KeyCode>) -> String {
    let mask = routing::key_mask(keys);
    let held: Vec<_> = Action::ALL
        .into_iter()
        .filter(|action| mask & action.bit() != 0)
        .map(key_label)
        .chain(
            [
                (routing::ALT, "Alt/Option"),
                (routing::SHIFT, "Shift"),
                (routing::BLOCKED_CTRL_META, "Ctrl/Meta"),
            ]
            .into_iter()
            .filter_map(|(bit, name)| (mask & bit != 0).then_some(name)),
        )
        .collect();
    if held.is_empty() {
        "none".into()
    } else {
        held.join(" + ")
    }
}
