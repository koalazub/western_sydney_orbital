//! Native Bevy text inputs for physical calibration and explicit world changes.

mod model;
mod systems;
mod view;

#[cfg(test)]
mod tests;

pub(crate) use model::{EditorField, InstrumentEditor};
pub(crate) use systems::instrument_editing;
pub(super) use systems::{begin_input_frame, install};
pub(super) use view::{spawn_inputs, spawn_placement};
