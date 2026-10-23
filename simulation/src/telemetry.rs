//! Read-only local flight measurements. No controls, camera state or debris truth.
mod calculations;
mod data;
mod presentation;
mod systems;
mod tracking;

pub(crate) use presentation::{brief, details, status};
pub(crate) use systems::install;

#[cfg(test)]
mod tests;
