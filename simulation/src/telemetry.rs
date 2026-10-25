//! Read-only local flight measurements. Native all-body debug samples are kept
//! out of serialized telemetry and never become planner or sensor observations.
mod calculations;
mod data;
mod debris;
mod instruments;
mod presentation;
mod systems;
mod tracking;

pub(crate) use instruments::{FlightMetric, Instrument, InstrumentGroup};
pub(crate) use presentation::{details, group_status, instrument, sample_status, status};
pub(crate) use systems::install;

#[cfg(test)]
mod tests;
