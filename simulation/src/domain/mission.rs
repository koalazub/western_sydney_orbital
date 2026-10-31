use serde::{Deserialize, Serialize};
use std::fmt;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MissionStatus {
    #[default]
    Running,
    Success,
    FailedDamage,
}

impl MissionStatus {
    pub const fn is_terminal(self) -> bool {
        !matches!(self, Self::Running)
    }
}

impl fmt::Display for MissionStatus {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Running => "Running",
            Self::Success => "Arrived",
            Self::FailedDamage => "Failed: impact damage",
        })
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MissionPhase {
    #[default]
    OnPad,
    Ascending,
    Transit,
    Arrived,
    Failed,
}

impl fmt::Display for MissionPhase {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::OnPad => "On landing pad",
            Self::Ascending => "Ascending",
            Self::Transit => "Crossing debris belt",
            Self::Arrived => "Arrived",
            Self::Failed => "Failed",
        })
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub enum LagrangeRegion {
    L1,
    #[default]
    L2,
    /// The competition target is a local-up point, not a collinear Lagrange point.
    #[serde(rename = "local_up")]
    LocalUp,
}
