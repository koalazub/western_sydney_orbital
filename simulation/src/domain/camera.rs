use serde::{Deserialize, Serialize};
use std::fmt;

use super::EntityId;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CameraMode {
    #[default]
    #[serde(alias = "mission")]
    Orbit,
    FirstPerson,
    #[serde(alias = "follow")]
    ThirdPerson,
    #[serde(rename = "free")]
    FreeFlight,
    #[serde(alias = "top")]
    Overview,
}

impl fmt::Display for CameraMode {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Orbit => "Orbit",
            Self::FirstPerson => "First person",
            Self::ThirdPerson => "Third person",
            Self::FreeFlight => "Free flight",
            Self::Overview => "Overview",
        })
    }
}

#[derive(Clone, Copy, Debug, Default, Deserialize)]
pub struct CameraRequest {
    pub mode: Option<CameraMode>,
    pub focus: Option<EntityId>,
    pub zoom: Option<f32>,
}
