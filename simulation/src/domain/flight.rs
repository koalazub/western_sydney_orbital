use serde::{Deserialize, Serialize};

use super::LagrangeRegion;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FlightPreset {
    MarsFacingL1,
    #[default]
    FarSideL2,
}

impl FlightPreset {
    pub const fn is_default(&self) -> bool {
        matches!(self, Self::FarSideL2)
    }

    pub const fn target_region(self) -> LagrangeRegion {
        match self {
            Self::MarsFacingL1 => LagrangeRegion::L1,
            Self::FarSideL2 => LagrangeRegion::L2,
        }
    }

    pub const fn launch_side(self) -> LaunchSide {
        match self {
            Self::MarsFacingL1 => LaunchSide::MarsFacing,
            Self::FarSideL2 => LaunchSide::FarSide,
        }
    }
}

/// Physical hemisphere in the rotating frame, whose +X axis points toward Mars.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LaunchSide {
    MarsFacing,
    FarSide,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preset_names_keep_l2_on_the_far_side() {
        for (preset, name, side, region) in [
            (
                FlightPreset::MarsFacingL1,
                "mars_facing_l1",
                LaunchSide::MarsFacing,
                LagrangeRegion::L1,
            ),
            (
                FlightPreset::FarSideL2,
                "far_side_l2",
                LaunchSide::FarSide,
                LagrangeRegion::L2,
            ),
        ] {
            assert_eq!(serde_json::to_value(preset).unwrap(), name);
            assert_eq!(
                serde_json::from_value::<FlightPreset>(serde_json::json!(name)).unwrap(),
                preset
            );
            assert_eq!(preset.launch_side(), side);
            assert_eq!(preset.target_region(), region);
        }
        assert!(FlightPreset::default().is_default());
        assert!(!FlightPreset::MarsFacingL1.is_default());
        assert!(serde_json::from_str::<FlightPreset>("\"mars_facing_l2\"").is_err());
    }
}
