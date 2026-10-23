//! Pathway-engineer preview only. No controller, vehicle mutation or hidden
//! debris truth is available to the search. The public radar adapter is a
//! deliberately incomplete input, not a substitute for the team's fusion layer.
mod geometry;
mod input;
mod model;
mod search;
mod systems;

pub(crate) use model::{brief, details, status};
pub(crate) use systems::install;
