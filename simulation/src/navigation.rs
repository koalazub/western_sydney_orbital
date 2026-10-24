//! Validated transport and debug rendering for Python notebook path previews.
//! Python owns all planning and scheduling. This module issues no flight commands
//! and does not assess routes against obstacle truth or vehicle dynamics.
mod model;
mod systems;

#[cfg(test)]
mod tests;

pub(crate) use model::{brief, details, status, submit};
pub(crate) use systems::install;
