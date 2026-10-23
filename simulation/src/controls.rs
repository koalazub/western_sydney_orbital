//! Pure keyboard routing and ship-command ownership, with input-only adapters.

mod model;
mod pilot;
mod routing;

pub(crate) use model::{Movement, MovementTarget, PilotContext, PilotEffect};
pub(crate) use pilot::KeyboardPilot;

/// Decode the browser's physical-key sample without reading platform state.
pub(crate) fn decode(mask: u16) -> Movement {
    routing::decode(mask)
}

/// Store the latest physical-key sample. This does not call browser APIs.
pub(crate) fn set_movement_keys(mask: u16) {
    routing::set_movement_keys(mask);
}

/// Use the authoritative browser sample on WASM and held Bevy keys on native.
pub(crate) fn movement(keys: &bevy::prelude::ButtonInput<bevy::prelude::KeyCode>) -> Movement {
    decode(routing::key_mask(keys))
}

/// Space is physical bit 9 in the shared browser/native movement protocol.
pub(crate) fn lift_key_down(keys: &bevy::prelude::ButtonInput<bevy::prelude::KeyCode>) -> bool {
    routing::key_mask(keys) & (1 << 9) != 0
}

pub(crate) fn held_key_labels(keys: &bevy::prelude::ButtonInput<bevy::prelude::KeyCode>) -> String {
    let mask = routing::key_mask(keys);
    let held: Vec<_> = [
        (0, "W"),
        (1, "A"),
        (2, "S"),
        (3, "D"),
        (4, "Q"),
        (5, "E"),
        (9, "Space"),
        (6, "Alt/Option"),
        (7, "Shift"),
        (8, "Ctrl/Meta"),
    ]
    .into_iter()
    .filter_map(|(bit, name)| (mask & (1 << bit) != 0).then_some(name))
    .collect();
    if held.is_empty() {
        "none".into()
    } else {
        held.join(" + ")
    }
}
