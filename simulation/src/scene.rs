//! Snapshot-only Bevy rendering; simulation state remains owned by the model.
mod body_axes;
mod meshes;
mod model;
mod systems;

pub(crate) use body_axes::install as install_body_axes;
pub use systems::{setup, sync_scene};
