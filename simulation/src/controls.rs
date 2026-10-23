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
