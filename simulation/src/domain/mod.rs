//! Typed domain vocabulary shared by the renderer, simulation and API adapters.
mod camera;
mod command;
mod entity;
mod flight;
mod mission;
mod scoring;

pub use camera::{CameraMode, CameraRequest, ShipCameraId};
pub use command::{EngineAction, PlaybackRate, ViewportAction};
pub use entity::{EntityId, EntityKind, EntityStatus};
pub use flight::{FlightPreset, LaunchSide};
pub use mission::{LagrangeRegion, MissionPhase, MissionStatus};
pub use scoring::{ScoreBreakdown, ScoreFormula, ScoreProgressBasis};
