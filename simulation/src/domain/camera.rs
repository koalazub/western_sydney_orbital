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
    ShipTop,
    ShipBottom,
}

impl CameraMode {
    pub const fn sensor(self) -> Option<ShipCameraId> {
        match self {
            Self::ShipTop => Some(ShipCameraId::Top),
            Self::ShipBottom => Some(ShipCameraId::Bottom),
            Self::Orbit
            | Self::FirstPerson
            | Self::ThirdPerson
            | Self::FreeFlight
            | Self::Overview => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ShipCameraId {
    Top,
    Bottom,
}

impl ShipCameraId {
    /// Pinned STP UserAPI.PanTiltCamera limits, in shuttle-body degrees.
    pub const fn yaw_limits_deg(self) -> [f64; 2] {
        match self {
            Self::Top | Self::Bottom => [-89.0, 89.0],
        }
    }

    pub const fn pitch_limits_deg(self) -> [f64; 2] {
        match self {
            Self::Top => [-50.0, 0.0],
            Self::Bottom => [0.0, 50.0],
        }
    }

    pub const fn index(self) -> usize {
        match self {
            Self::Top => 0,
            Self::Bottom => 1,
        }
    }
    pub const fn label(self) -> &'static str {
        match self {
            Self::Top => "Top",
            Self::Bottom => "Bottom",
        }
    }
}

impl fmt::Display for CameraMode {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Orbit => "Orbit",
            Self::FirstPerson => "First person",
            Self::ThirdPerson => "Third person",
            Self::FreeFlight => "Free flight",
            Self::Overview => "Overview",
            Self::ShipTop => "Ship top",
            Self::ShipBottom => "Ship bottom",
        })
    }
}

#[derive(Clone, Copy, Debug, Default, Deserialize)]
pub struct CameraRequest {
    pub mode: Option<CameraMode>,
    pub focus: Option<EntityId>,
    pub zoom: Option<f32>,
}
