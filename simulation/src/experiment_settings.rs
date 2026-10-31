//! Baseline experiment parameters and local live calibration, without flight policies.

use serde::{Deserialize, Serialize};

pub const MAX_DEBRIS_COUNT: usize = 100_000;
/// The official competition cloud is a fixed 500-actor field.
pub const OFFICIAL_CLOUD_COUNT: usize = 500;

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

/// Local actuator calibration, not an SDK setter or a reference-parity claim.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShipTuning {
    pub mass_kg: f64,
    pub inertia_scale: f64,
    /// Dimensionless gain on applied force and torque, never on stored requests.
    pub thrust_scale: f64,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DebrisFlowTuning {
    pub speed_mps: f64,
    /// Birth dispersion only. Existing bodies keep their individual perturbations.
    pub speed_spread_mps: f64,
    pub stream_axis: StreamAxis,
    pub stream_sign: i8,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DebrisExperimentSettings {
    pub count: usize,
    #[serde(
        default,
        skip_serializing_if = "DebrisDistribution::is_directed_stream"
    )]
    pub distribution: DebrisDistribution,
    #[serde(default, skip_serializing_if = "DebrisTrajectoryAdjustment::is_off")]
    pub trajectory_adjustment: DebrisTrajectoryAdjustment,
    pub radius_min_m: f64,
    pub radius_max_m: f64,
    /// Common stream speed; inactive for impact ejecta.
    pub speed_mps: f64,
    /// Maximum stream deviation at birth; inactive for impact ejecta.
    pub speed_spread_mps: f64,
    /// Radial terrain clearance for shell distributions; inactive for transit.
    pub shell_inner_m: f64,
    pub shell_outer_m: f64,
    pub stream_axis: StreamAxis,
    pub stream_sign: i8,
}

/// Initial post-impact cloud velocities, not a fracture or impact-energy solver.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ImpactEjectaSettings {
    /// Nonzero direction from Phobos toward the center of the source patch.
    pub source_direction: [f64; 3],
    /// Common initial kick in the rotating world frame, before gravity acts.
    pub kick_velocity_mps: [f64; 3],
    pub tangential_speed_mps: f64,
    pub patch_half_angle_deg: f64,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum DebrisDistribution {
    #[default]
    #[serde(deserialize_with = "deserialize_empty_distribution")]
    DirectedStream,
    #[serde(deserialize_with = "deserialize_empty_distribution")]
    TransitStream,
    #[serde(deserialize_with = "deserialize_empty_distribution")]
    OfficialCloud,
    ImpactEjecta {
        settings: ImpactEjectaSettings,
    },
}

impl DebrisDistribution {
    pub const fn is_directed_stream(&self) -> bool {
        matches!(self, Self::DirectedStream)
    }

    pub const fn is_stream(&self) -> bool {
        matches!(self, Self::DirectedStream | Self::TransitStream)
    }

    pub const fn is_official_cloud(&self) -> bool {
        matches!(self, Self::OfficialCloud)
    }

    pub fn impact_default() -> Self {
        Self::ImpactEjecta {
            settings: crate::scenario::settings().impact_ejecta_defaults,
        }
    }
}

/// Opt-in local adaptation of the pinned source's disabled trajectory adjustment.
/// This is a new-flight setting, not a live flow calibration.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", try_from = "String")]
pub enum DebrisTrajectoryAdjustment {
    #[default]
    Off,
    TowardLander,
}

impl TryFrom<String> for DebrisTrajectoryAdjustment {
    type Error = String;

    fn try_from(value: String) -> Result<Self, Self::Error> {
        match value.as_str() {
            "off" => Ok(Self::Off),
            "toward_lander" => Ok(Self::TowardLander),
            _ => Err("trajectory_adjustment must be off or toward_lander".into()),
        }
    }
}

impl DebrisTrajectoryAdjustment {
    pub const fn is_off(&self) -> bool {
        matches!(self, Self::Off)
    }
}

impl ImpactEjectaSettings {
    pub fn validate(&self) -> Result<(), String> {
        if !self.source_direction.iter().all(|value| value.is_finite())
            || !self.source_direction.iter().any(|value| *value != 0.0)
        {
            return Err("debris impact source_direction must be finite and nonzero".into());
        }
        let [x, y, z] = self.kick_velocity_mps;
        if !self.kick_velocity_mps.iter().all(|value| value.is_finite())
            || x.hypot(y).hypot(z) > 200.0
        {
            return Err(
                "debris impact kick_velocity_mps must be finite with speed <= 200 m/s".into(),
            );
        }
        if !finite_between(self.tangential_speed_mps, 0.0, 200.0) {
            return Err("debris impact tangential_speed_mps must be finite and in 0..=200".into());
        }
        if !finite_between(self.patch_half_angle_deg, 0.0, 180.0)
            || self.patch_half_angle_deg == 0.0
        {
            return Err(
                "debris impact patch_half_angle_deg must be finite, greater than 0, and at most 180 degrees"
                    .into(),
            );
        }
        Ok(())
    }
}

fn deserialize_empty_distribution<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<(), D::Error> {
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct EmptyPayload {}

    EmptyPayload::deserialize(deserializer).map(|_| ())
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
            return Err("debris.count must be a whole number from 0 to 100000".into());
        }
        match debris.distribution {
            DebrisDistribution::DirectedStream | DebrisDistribution::TransitStream => {}
            DebrisDistribution::OfficialCloud => {
                if debris.count != OFFICIAL_CLOUD_COUNT {
                    return Err(format!(
                        "debris.count must be {OFFICIAL_CLOUD_COUNT} for official_cloud"
                    ));
                }
                if debris.trajectory_adjustment != DebrisTrajectoryAdjustment::Off {
                    return Err(
                        "official_cloud does not support a debris trajectory adjustment".into(),
                    );
                }
            }
            DebrisDistribution::ImpactEjecta { settings } => settings.validate()?,
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

impl From<ShipExperimentSettings> for ShipTuning {
    fn from(ship: ShipExperimentSettings) -> Self {
        Self {
            mass_kg: ship.mass_kg,
            inertia_scale: ship.inertia_scale,
            thrust_scale: 1.0,
        }
    }
}

impl ShipTuning {
    pub fn validate(&self) -> Result<(), String> {
        if !finite_between(self.mass_kg, 100.0, 100_000.0) {
            return Err("ship.mass_kg must be finite and between 100 and 100000 kg".into());
        }
        if !finite_between(self.inertia_scale, 0.1, 10.0) {
            return Err("ship.inertia_scale must be finite and between 0.1 and 10".into());
        }
        if !finite_between(self.thrust_scale, 0.1, 10.0) {
            return Err("ship.thrust_scale must be finite and between 0.1 and 10".into());
        }
        Ok(())
    }

    pub fn inertia_kg_m2(self) -> [[f64; 3]; 3] {
        ShipExperimentSettings {
            mass_kg: self.mass_kg,
            inertia_scale: self.inertia_scale,
        }
        .inertia_kg_m2()
    }
}

impl From<DebrisExperimentSettings> for DebrisFlowTuning {
    fn from(debris: DebrisExperimentSettings) -> Self {
        Self {
            speed_mps: debris.speed_mps,
            speed_spread_mps: debris.speed_spread_mps,
            stream_axis: debris.stream_axis,
            stream_sign: debris.stream_sign,
        }
    }
}

impl DebrisFlowTuning {
    pub fn validate(&self) -> Result<(), String> {
        if !finite_between(self.speed_mps, 0.1, 200.0)
            || !finite_between(self.speed_spread_mps, 0.0, self.speed_mps * 0.5)
        {
            return Err(
                "debris.speed_mps must be in 0.1..=200 and speed_spread_mps in 0..=speed_mps/2"
                    .into(),
            );
        }
        if !matches!(self.stream_sign, -1 | 1) {
            return Err("debris.stream_sign must be -1 or 1".into());
        }
        Ok(())
    }

    pub(crate) fn common_velocity_mps(self) -> [f64; 3] {
        let mut velocity = [0.0; 3];
        velocity[self.stream_axis.index()] = f64::from(self.stream_sign) * self.speed_mps;
        velocity
    }

    pub(crate) fn apply_to(self, mut debris: DebrisExperimentSettings) -> DebrisExperimentSettings {
        debris.speed_mps = self.speed_mps;
        debris.speed_spread_mps = self.speed_spread_mps;
        debris.stream_axis = self.stream_axis;
        debris.stream_sign = self.stream_sign;
        debris
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
                "count": 500, "distribution": {"kind": "transit_stream"},
                "radius_min_m": 0.4, "radius_max_m": 40.4,
                "speed_mps": 20.0, "speed_spread_mps": 4.0,
                "shell_inner_m": 60.0, "shell_outer_m": 700.0,
                "stream_axis": "x", "stream_sign": 1
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
    fn older_explicit_shell_archives_keep_their_original_parameters() {
        let mut archived = serde_json::to_value(ExperimentSettings::default()).unwrap();
        archived["debris"]
            .as_object_mut()
            .unwrap()
            .remove("distribution");
        archived["debris"]["speed_spread_mps"] = serde_json::json!(5.0);
        archived["debris"]["shell_inner_m"] = serde_json::json!(200.0);
        archived["debris"]["shell_outer_m"] = serde_json::json!(3000.0);
        archived["debris"]["stream_axis"] = serde_json::json!("y");
        archived["debris"]["stream_sign"] = serde_json::json!(-1);
        let settings: ExperimentSettings = serde_json::from_value(archived.clone()).unwrap();
        settings.validate().unwrap();
        assert_eq!(
            settings.debris.distribution,
            DebrisDistribution::DirectedStream
        );
        assert_eq!(serde_json::to_value(settings).unwrap(), archived);
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

    #[test]
    fn debris_counts_above_the_old_limit_are_valid_but_the_new_limit_is_checked() {
        let mut settings = ExperimentSettings::default();
        for count in [1_001, 10_000, 65_536, 100_000] {
            settings.debris.count = count;
            settings.validate().unwrap();
        }
        settings.debris.count = MAX_DEBRIS_COUNT + 1;
        assert_eq!(
            settings.validate().unwrap_err(),
            "debris.count must be a whole number from 0 to 100000"
        );
    }

    #[test]
    fn impact_distribution_round_trips_and_keeps_stream_reference_fields() {
        let mut settings = ExperimentSettings::default();
        settings.debris.distribution = DebrisDistribution::impact_default();
        settings.validate().unwrap();
        let value = serde_json::to_value(settings).unwrap();
        assert_eq!(
            value["debris"]["distribution"],
            serde_json::json!({
                "kind": "impact_ejecta",
                "settings": {
                    "source_direction": [-1.0, 0.0, 0.0],
                    "kick_velocity_mps": [-1.0, 0.0, 0.0],
                    "tangential_speed_mps": 7.0,
                    "patch_half_angle_deg": 15.0
                }
            })
        );
        assert_eq!(value["debris"]["speed_mps"], 20.0);
        assert_eq!(value["debris"]["speed_spread_mps"], 4.0);
        assert_eq!(
            serde_json::from_value::<ExperimentSettings>(value).unwrap(),
            settings
        );
    }

    #[test]
    fn explicit_stream_distribution_is_accepted_but_omitted_on_write() {
        let mut settings = ExperimentSettings::default();
        settings.debris.distribution = DebrisDistribution::DirectedStream;
        let mut value = serde_json::to_value(settings).unwrap();
        value["debris"]["distribution"] = serde_json::json!({"kind": "directed_stream"});
        let explicit = serde_json::from_value::<ExperimentSettings>(value).unwrap();
        assert_eq!(explicit, settings);
        assert!(
            serde_json::to_value(explicit).unwrap()["debris"]
                .get("distribution")
                .is_none()
        );
    }

    #[test]
    fn unknown_distribution_fields_and_malformed_impact_settings_are_rejected() {
        for distribution in [
            serde_json::json!({"kind": "directed_stream", "orbit_speed_mps": 7.0}),
            serde_json::json!({"kind": "transit_stream", "settings": {}}),
            serde_json::json!({"kind": "transit_stream", "target_lander": true}),
            serde_json::json!({"kind": "official_cloud", "settings": {}}),
            serde_json::json!({"kind": "official_cloud", "num_debris": 500}),
            serde_json::json!({"kind": "fracture_solver"}),
            serde_json::json!({"kind": "impact_ejecta"}),
            serde_json::json!({"kind": "impact_ejecta", "settings": {}, "policy": "orbit"}),
        ] {
            let mut value = serde_json::to_value(ExperimentSettings::default()).unwrap();
            value["debris"]["distribution"] = distribution;
            assert!(serde_json::from_value::<ExperimentSettings>(value).is_err());
        }
        let mut value = serde_json::to_value(ExperimentSettings::default()).unwrap();
        value["debris"]["distribution"] =
            serde_json::to_value(DebrisDistribution::impact_default()).unwrap();
        value["debris"]["distribution"]["settings"]["orbit_speed_mps"] = serde_json::json!(7.0);
        assert!(serde_json::from_value::<ExperimentSettings>(value).is_err());
    }

    #[test]
    fn transit_and_adjustment_are_strict_explicit_new_flight_choices() {
        assert_eq!(
            DebrisDistribution::default(),
            DebrisDistribution::DirectedStream
        );
        assert!(DebrisDistribution::DirectedStream.is_stream());
        assert!(DebrisDistribution::TransitStream.is_stream());
        assert!(!DebrisDistribution::impact_default().is_stream());
        assert!(!DebrisDistribution::OfficialCloud.is_stream());
        assert!(DebrisDistribution::OfficialCloud.is_official_cloud());
        let defaults = ExperimentSettings::default();
        assert_eq!(
            defaults.debris.distribution,
            DebrisDistribution::TransitStream
        );
        assert_eq!(
            defaults.debris.trajectory_adjustment,
            DebrisTrajectoryAdjustment::Off
        );
        assert!(
            serde_json::to_value(defaults).unwrap()["debris"]
                .get("trajectory_adjustment")
                .is_none()
        );
        for mode in [
            DebrisTrajectoryAdjustment::Off,
            DebrisTrajectoryAdjustment::TowardLander,
        ] {
            let mut config = defaults;
            config.debris.trajectory_adjustment = mode;
            let value = serde_json::to_value(config).unwrap();
            assert_eq!(
                serde_json::from_value::<ExperimentSettings>(value.clone()).unwrap(),
                config
            );
            if !mode.is_off() {
                assert_eq!(
                    value["debris"]["trajectory_adjustment"],
                    serde_json::json!("toward_lander")
                );
            }
        }
        let mut explicit_off = serde_json::to_value(defaults).unwrap();
        explicit_off["debris"]["trajectory_adjustment"] = serde_json::json!("off");
        assert_eq!(
            serde_json::from_value::<ExperimentSettings>(explicit_off).unwrap(),
            defaults
        );
        for adjustment in [
            serde_json::json!({"off": null}),
            serde_json::json!({"toward_lander": null}),
            serde_json::json!({"mode": "off", "gain": 1.0}),
            serde_json::json!({"mode": "toward_lander", "rate_hz": 60}),
            serde_json::json!({"mode": "orbit"}),
            serde_json::json!("orbit"),
            serde_json::Value::Null,
        ] {
            let mut value = serde_json::to_value(defaults).unwrap();
            value["debris"]["trajectory_adjustment"] = adjustment;
            assert!(serde_json::from_value::<ExperimentSettings>(value).is_err());
        }
        let mut old = serde_json::to_value(defaults).unwrap();
        old["debris"]
            .as_object_mut()
            .unwrap()
            .remove("distribution");
        let old: ExperimentSettings = serde_json::from_value(old).unwrap();
        assert_eq!(old.debris.distribution, DebrisDistribution::DirectedStream);
        assert!(old.debris.trajectory_adjustment.is_off());
        let mut live = serde_json::to_value(DebrisFlowTuning::from(defaults.debris)).unwrap();
        live["trajectory_adjustment"] = serde_json::json!("toward_lander");
        assert!(serde_json::from_value::<DebrisFlowTuning>(live).is_err());
    }

    #[test]
    fn impact_settings_validate_vectors_and_inclusive_scalar_limits() {
        let DebrisDistribution::ImpactEjecta {
            settings: reference,
        } = DebrisDistribution::impact_default()
        else {
            panic!("versioned impact defaults must select impact ejecta");
        };
        for source_direction in [[0.0; 3], [f64::NAN, 0.0, 0.0], [0.0, f64::INFINITY, 1.0]] {
            assert!(
                ImpactEjectaSettings {
                    source_direction,
                    ..reference
                }
                .validate()
                .is_err()
            );
        }
        for kick_velocity_mps in [
            [200.1, 0.0, 0.0],
            [150.0, 150.0, 0.0],
            [f64::NAN, 0.0, 0.0],
            [0.0, 0.0, f64::INFINITY],
        ] {
            assert!(
                ImpactEjectaSettings {
                    kick_velocity_mps,
                    ..reference
                }
                .validate()
                .is_err()
            );
        }
        for tangential_speed_mps in [-0.1, 200.1, f64::NAN, f64::INFINITY] {
            assert!(
                ImpactEjectaSettings {
                    tangential_speed_mps,
                    ..reference
                }
                .validate()
                .is_err()
            );
        }
        for patch_half_angle_deg in [-1.0, 0.0, 180.1, f64::NAN, f64::INFINITY] {
            assert!(
                ImpactEjectaSettings {
                    patch_half_angle_deg,
                    ..reference
                }
                .validate()
                .is_err()
            );
        }
        for (kick_velocity_mps, tangential_speed_mps, patch_half_angle_deg) in
            [([0.0; 3], 0.0, 0.01), ([0.0, 0.0, -200.0], 200.0, 180.0)]
        {
            ImpactEjectaSettings {
                kick_velocity_mps,
                tangential_speed_mps,
                patch_half_angle_deg,
                ..reference
            }
            .validate()
            .unwrap();
        }
        let mut experiment = ExperimentSettings::default();
        experiment.debris.distribution = DebrisDistribution::ImpactEjecta {
            settings: ImpactEjectaSettings {
                source_direction: [0.0; 3],
                ..reference
            },
        };
        assert!(experiment.validate().is_err());
    }

    #[test]
    fn official_cloud_is_selectable_and_keeps_the_fixed_official_population() {
        let mut settings = ExperimentSettings::default();
        settings.debris.distribution = DebrisDistribution::OfficialCloud;
        settings.validate().unwrap();
        assert_eq!(settings.debris.count, OFFICIAL_CLOUD_COUNT);
        let value = serde_json::to_value(settings).unwrap();
        assert_eq!(
            value["debris"]["distribution"],
            serde_json::json!({"kind": "official_cloud"})
        );
        assert_eq!(
            serde_json::from_value::<ExperimentSettings>(value).unwrap(),
            settings
        );
        settings.debris.count = OFFICIAL_CLOUD_COUNT - 1;
        assert!(settings.validate().is_err());
        settings.debris.count = OFFICIAL_CLOUD_COUNT + 1;
        assert!(settings.validate().is_err());
        settings.debris.count = OFFICIAL_CLOUD_COUNT;
        settings.debris.trajectory_adjustment = DebrisTrajectoryAdjustment::TowardLander;
        assert!(settings.validate().is_err());
        settings.debris.trajectory_adjustment = DebrisTrajectoryAdjustment::Off;
        settings.validate().unwrap();
        let mut explicit = serde_json::to_value(settings).unwrap();
        explicit["debris"]["distribution"] = serde_json::json!({"kind": "official_cloud"});
        assert_eq!(
            serde_json::from_value::<ExperimentSettings>(explicit).unwrap(),
            settings
        );
    }

    #[test]
    fn live_tuning_inclusive_bounds_keep_reference_requests_separate_from_local_gain() {
        for (mass_kg, inertia_scale, thrust_scale) in [(100.0, 0.1, 0.1), (100_000.0, 10.0, 10.0)] {
            ShipTuning {
                mass_kg,
                inertia_scale,
                thrust_scale,
            }
            .validate()
            .unwrap();
        }
        for (speed_mps, speed_spread_mps) in [(0.1, 0.0), (0.1, 0.05), (200.0, 100.0)] {
            for stream_axis in [StreamAxis::X, StreamAxis::Y, StreamAxis::Z] {
                for stream_sign in [-1, 1] {
                    let flow = DebrisFlowTuning {
                        speed_mps,
                        speed_spread_mps,
                        stream_axis,
                        stream_sign,
                    };
                    flow.validate().unwrap();
                    assert_eq!(
                        flow.common_velocity_mps()[stream_axis.index()],
                        f64::from(stream_sign) * speed_mps
                    );
                }
            }
        }
        let reference = ExperimentSettings::default();
        let ship = ShipTuning::from(reference.ship);
        assert_eq!(ship.thrust_scale, 1.0);
        assert_eq!(ship.inertia_kg_m2(), reference.ship.inertia_kg_m2());
        assert_eq!(
            DebrisFlowTuning::from(reference.debris).apply_to(reference.debris),
            reference.debris
        );
    }
}
