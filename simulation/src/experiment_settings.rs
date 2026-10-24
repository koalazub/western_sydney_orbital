//! Reset-only local experiment parameters. These do not contain flight policies.

use serde::{Deserialize, Serialize};

pub const MAX_DEBRIS_COUNT: usize = 1_000;

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExperimentSettings {
    pub ship: ShipExperimentSettings,
    pub debris: DebrisExperimentSettings,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShipExperimentSettings {
    pub mass_kg: f64,
    /// Additional inertia multiplier after scaling the reference tensor by mass.
    pub inertia_scale: f64,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DebrisExperimentSettings {
    pub count: usize,
    pub radius_min_m: f64,
    pub radius_max_m: f64,
    pub speed_mps: f64,
    /// Maximum Euclidean deviation from the common stream velocity at birth.
    pub speed_spread_mps: f64,
    /// Radial clearance from the terrain, in addition to each body's radius.
    pub shell_inner_m: f64,
    pub shell_outer_m: f64,
    pub stream_axis: StreamAxis,
    pub stream_sign: i8,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum StreamAxis {
    X,
    Y,
    Z,
}

impl StreamAxis {
    pub(crate) fn index(self) -> usize {
        match self {
            Self::X => 0,
            Self::Y => 1,
            Self::Z => 2,
        }
    }
}

impl Default for ExperimentSettings {
    fn default() -> Self {
        crate::scenario::settings().experiment_settings
    }
}

impl ExperimentSettings {
    pub fn validate(&self) -> Result<(), String> {
        let ship = self.ship;
        let debris = self.debris;
        if !finite_between(ship.mass_kg, 100.0, 100_000.0) {
            return Err("ship.mass_kg must be finite and between 100 and 100000 kg".into());
        }
        if !finite_between(ship.inertia_scale, 0.1, 10.0) {
            return Err("ship.inertia_scale must be finite and between 0.1 and 10".into());
        }
        if debris.count > MAX_DEBRIS_COUNT {
            return Err("debris.count must be a whole number from 0 to 1000".into());
        }
        if !finite_between(debris.radius_min_m, 0.05, 100.0)
            || !finite_between(debris.radius_max_m, debris.radius_min_m, 100.0)
        {
            return Err(
                "debris radii must satisfy 0.05 <= radius_min_m <= radius_max_m <= 100".into(),
            );
        }
        if !finite_between(debris.speed_mps, 0.1, 200.0)
            || !finite_between(debris.speed_spread_mps, 0.0, debris.speed_mps * 0.5)
        {
            return Err(
                "debris.speed_mps must be in 0.1..=200 and speed_spread_mps in 0..=speed_mps/2"
                    .into(),
            );
        }
        if !finite_between(debris.shell_inner_m, 0.0, 49_990.0)
            || !finite_between(debris.shell_outer_m, 10.0, 50_000.0)
            || debris.shell_outer_m - debris.shell_inner_m < 10.0
        {
            return Err("debris shell must satisfy 0 <= shell_inner_m < shell_outer_m <= 50000 with at least 10 m thickness".into());
        }
        if !matches!(debris.stream_sign, -1 | 1) {
            return Err("debris.stream_sign must be -1 or 1".into());
        }
        Ok(())
    }
}

impl ShipExperimentSettings {
    /// Body-frame tensor, kg m². Geometry stays fixed when the mass changes.
    pub fn inertia_kg_m2(self) -> [[f64; 3]; 3] {
        let reference = crate::scenario::settings().lander;
        let factor = (self.mass_kg / reference.mass_kg) * self.inertia_scale;
        reference
            .inertia_kg_m2
            .map(|row| row.map(|value| value * factor))
    }
}

fn finite_between(value: f64, minimum: f64, maximum: f64) -> bool {
    value.is_finite() && (minimum..=maximum).contains(&value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn versioned_defaults_and_external_schema_round_trip() {
        let settings = ExperimentSettings::default();
        settings.validate().unwrap();
        let value = serde_json::json!({
            "ship": {"mass_kg": 3388.7, "inertia_scale": 1.0},
            "debris": {
                "count": 500, "radius_min_m": 0.4, "radius_max_m": 40.4,
                "speed_mps": 20.0, "speed_spread_mps": 5.0,
                "shell_inner_m": 200.0, "shell_outer_m": 3000.0,
                "stream_axis": "y", "stream_sign": -1
            }
        });
        assert_eq!(serde_json::to_value(settings).unwrap(), value);
        assert_eq!(
            serde_json::from_value::<ExperimentSettings>(value).unwrap(),
            settings
        );
        assert_eq!(
            settings.ship.inertia_kg_m2(),
            crate::scenario::settings().lander.inertia_kg_m2
        );
    }

    #[test]
    fn malformed_external_types_and_unknown_policy_fields_are_rejected() {
        for (field, value) in [
            ("count", serde_json::json!(1.5)),
            ("count", serde_json::json!(-1)),
            ("stream_axis", serde_json::json!("north")),
            ("stream_sign", serde_json::json!(0.5)),
            ("policy", serde_json::json!("automatic")),
        ] {
            let mut settings = serde_json::to_value(ExperimentSettings::default()).unwrap();
            settings["debris"][field] = value;
            assert!(serde_json::from_value::<ExperimentSettings>(settings).is_err());
        }
    }

    #[test]
    fn inclusive_limits_and_a_zero_count_are_valid() {
        let mut settings = ExperimentSettings::default();
        settings.ship.mass_kg = 100.0;
        settings.ship.inertia_scale = 0.1;
        settings.debris.count = 0;
        settings.debris.radius_min_m = 0.05;
        settings.debris.radius_max_m = 100.0;
        settings.debris.speed_mps = 0.1;
        settings.debris.speed_spread_mps = 0.05;
        settings.debris.shell_inner_m = 0.0;
        settings.debris.shell_outer_m = 10.0;
        settings.validate().unwrap();
        settings.ship.mass_kg = 100_000.0;
        settings.ship.inertia_scale = 10.0;
        settings.debris.count = MAX_DEBRIS_COUNT;
        settings.debris.speed_mps = 200.0;
        settings.debris.speed_spread_mps = 100.0;
        settings.debris.shell_inner_m = 49_990.0;
        settings.debris.shell_outer_m = 50_000.0;
        settings.validate().unwrap();
    }
}
