//! Snapshot-only Bevy rendering; simulation state remains owned by the model.
mod meshes;
mod model;
mod systems;

pub use systems::{setup, sync_scene};
