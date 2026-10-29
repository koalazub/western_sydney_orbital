pub use crate::domain::FlightPreset;
use crate::domain::{
    EntityId, EntityKind, EntityStatus, FlightAnalytics, LagrangeRegion, MissionPhase,
    MissionStatus, ScoreBreakdown,
};
pub use crate::propulsion::ThrustApplication;
pub use crate::terrain::{
    LandingSite, landing_site, landing_site_at_direction, landing_site_for, phobos_geometry,
};
use serde::{Deserialize, Serialize};
#[path = "avionics.rs"]
mod avionics;
pub use avionics::{
    CommandWatchdog, CommandWatchdogStatus, ControlAuthority, ControlAuthorityStatus, ControlClaim,
    ControlFence, ControlRelease, LeaseError, LeasedCommand, NavigationDeliveryState,
    NavigationSample, RadarDeliveryState, RadarFrame, RadarRow, SensorDelivery, SensorFault,
    SensorFaultMode, SensorFaultTarget, SensorState, SensorUnitId, delivery as sensor_delivery,
    valid_control_id,
};
#[path = "experiment_settings.rs"]
mod experiment_settings;
pub use experiment_settings::{
    DebrisDistribution, DebrisExperimentSettings, DebrisFlowTuning, DebrisTrajectoryAdjustment,
    ExperimentSettings, ImpactEjectaSettings, MAX_DEBRIS_COUNT, ShipExperimentSettings, ShipTuning,
    StreamAxis,
};
pub fn phobos_radii_m() -> [f64; 3] {
    crate::scenario::settings().gravity.phobos_radii_m
}

/// Known world geometry for external planners, not an obstacle estimate.
pub fn phobos_enclosing_radius_m() -> f64 {
    static RADIUS: std::sync::OnceLock<f64> = std::sync::OnceLock::new();
    *RADIUS.get_or_init(|| {
        phobos_geometry()
            .0
            .into_iter()
            .map(|[x, y, z]| x.hypot(y).hypot(z))
            .fold(0.0, f64::max)
    })
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Snapshot {
    pub model: String,
    pub time_s: f64,
    pub seed: u64,
    /// Missing in old archives, which retain the original far-side L2 world.
    #[serde(default, skip_serializing_if = "FlightPreset::is_default")]
    pub flight_preset: FlightPreset,
    /// Explicit custom surface anchor. None keeps the original preset geometry.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub launch_site_direction: Option<[f64; 3]>,
    /// Launch anchor at the last explicit Reset, not the latest local move.
    /// None means the preset seat was the trial's starting point. Debris births
    /// always retain their canonical preset initializer, independent of either anchor.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trial_start_launch_site_direction: Option<[f64; 3]>,
    pub frame: String,
    pub body: PrimaryBody,
    #[serde(default)]
    pub environment: crate::astrophysics::Environment,
    /// None identifies the archived corridor initializer, never a shell profile.
    #[serde(default)]
    pub experiment_settings: Option<ExperimentSettings>,
    /// Additive overrides never relabel an archived corridor as a shell profile.
    #[serde(default)]
    pub ship_tuning_override: Option<ShipTuning>,
    #[serde(default)]
    pub debris_flow_override: Option<DebrisFlowTuning>,
    /// Local tuning does not count as a manual intervention or stop a flight.
    #[serde(default)]
    pub tuning_revision: u32,
    pub assets: Vec<EntityState>,
    pub events: Vec<Event>,
    pub collisions: u32,
    /// Local debug resets, not competition control commands. Preserved on restore.
    #[serde(default)]
    pub manual_interventions: u32,
    #[serde(default)]
    pub mission: MissionState,
    #[serde(default)]
    pub observations: Vec<Observation>,
    #[serde(default, with = "decimal_u64")]
    pub rng_state: u64,
    #[serde(default)]
    pub active_contacts: Vec<[EntityId; 2]>,
    /// Additive sensor extension. Old archives keep their primary samples/RNG.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sensor_state: Option<SensorState>,
    /// Required only for an explicitly enabled adjustment. Never infer past ticks
    /// or impulses from an old archive that has no cursor.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub debris_trajectory_adjustment_state: Option<DebrisTrajectoryAdjustmentState>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DebrisTrajectoryAdjustmentState {
    pub version: u32,
    /// Completed global ticks, including selections skipped by the cutoff.
    /// Validated within JSON's exact integer range.
    pub iteration: u64,
    pub applied_count: u64,
}

#[derive(Clone, Debug, Serialize)]
pub struct DebrisTrajectoryAdjustmentSummary {
    pub mode: DebrisTrajectoryAdjustment,
    pub constants: crate::scenario::DebrisTrajectoryAdjustmentConstants,
    pub applied_count: u64,
}

impl Snapshot {
    pub fn ship_tuning(&self) -> ShipTuning {
        self.ship_tuning_override.unwrap_or_else(|| {
            self.experiment_settings
                .map_or_else(
                    || ShipExperimentSettings {
                        mass_kg: crate::scenario::settings().lander.mass_kg,
                        inertia_scale: 1.0,
                    },
                    |experiment| experiment.ship,
                )
                .into()
        })
    }

    /// Stored stream calibration; inactive when the distribution is impact ejecta.
    pub fn debris_flow(&self) -> DebrisFlowTuning {
        self.debris_flow_override.unwrap_or_else(|| {
            // Archived corridor bodies keep their original isotropic velocities.
            // This is the calibration reference for additive live deltas only;
            // it does not enable shell generation or recycling for that archive.
            self.experiment_settings.map_or(
                crate::scenario::settings().legacy_corridor_flow_reference,
                |experiment| experiment.debris.into(),
            )
        })
    }
}

/// Local debug readback of actual rigid bodies, never planner or sensor input.
#[derive(Clone, Debug, Serialize)]
pub struct DebrisDynamicsReport {
    pub source_time_s: f64,
    pub frame: String,
    pub physical_count: usize,
    pub moving_count: usize,
    pub mean_velocity_world_mps: [f64; 3],
    pub min_speed_mps: Option<f64>,
    pub max_speed_mps: Option<f64>,
    pub mean_spin_rad_s: Option<f64>,
    pub total_mass_kg: f64,
}

/// Local physics readback, not an SDK hardware or sensor contract.
/// Body properties and external accumulators come directly from the same Rapier
/// lander. The actuator record is captured only when forces are actually applied.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AppliedPhysics {
    pub source_time_s: f64,
    pub tuning_revision: u32,
    pub frame: String,
    pub mass_kg: f64,
    pub inertia_body_kg_m2: [[f64; 3]; 3],
    pub position_world_m: [f64; 3],
    pub rotation_body_to_world_xyzw: [f64; 4],
    pub velocity_world_mps: [f64; 3],
    pub angular_velocity_world_rad_s: [f64; 3],
    /// Current Rapier user-force accumulators, not net acceleration measurements.
    pub total_external_force_world_n: [f64; 3],
    pub total_external_torque_world_nm: [f64; 3],
    pub external_force_provenance: ExternalForceProvenance,
    pub requested: ActuationRequest,
    pub actuator_gain: f64,
    /// Playback/paused state belongs to the session, not this force readback.
    pub actuation_state: ActuationState,
    /// None after construction/restore or explicit clearing, until a real application.
    pub last_actuator_application: Option<AppliedActuatorWrench>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExternalForceProvenance {
    /// Includes actuator/gravity user forces, excludes contact and joint impulses.
    RapierUserForcesExcludingSolverImpulses,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ActuationState {
    NotApplied,
    AwaitingApplication,
    Coasting,
    Actuating,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ActuationRequest {
    pub application: ThrustApplication,
    /// Stored bounded command, before the dimensionless local actuator gain.
    /// Archived PracticeBoost uses its original local assist bounds.
    pub accel_body_mps2: [f64; 3],
    pub angular_accel_body_rad_s2: [f64; 3],
}

impl From<&EntityState> for ActuationRequest {
    fn from(lander: &EntityState) -> Self {
        Self {
            application: lander.thrust_application,
            accel_body_mps2: lander.commanded_accel_mps2,
            angular_accel_body_rad_s2: lander.commanded_angular_accel_rad_s2,
        }
    }
}

impl ActuationRequest {
    pub fn is_zero(self) -> bool {
        self.accel_body_mps2
            .into_iter()
            .chain(self.angular_accel_body_rad_s2)
            .all(|component| component == 0.0)
    }
}

/// Immediate accumulator delta inside the real actuator application, before gravity.
/// Body vectors use the recorded application orientation, not a later body pose.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AppliedActuatorWrench {
    pub application_time_s: f64,
    pub tuning_revision: u32,
    pub requested: ActuationRequest,
    pub actuator_gain: f64,
    pub rotation_body_to_world_xyzw: [f64; 4],
    pub force_body_n: [f64; 3],
    pub torque_body_nm: [f64; 3],
    pub force_world_n: [f64; 3],
    pub torque_world_nm: [f64; 3],
    /// Sum used by the existing fuel model, including cancelling nozzle components.
    pub propellant_force_n: f64,
}

/// Typed simulation requests, deserialized only at the external API boundary.
#[derive(Clone, Debug, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
pub enum PhysicsCommand {
    Reset {
        seed: Option<u64>,
    },
    ConfigureExperiment {
        settings: ExperimentSettings,
        seed: Option<u64>,
    },
    ConfigureFlight {
        settings: ExperimentSettings,
        seed: Option<u64>,
        preset: FlightPreset,
    },
    PlaceLander {
        position_m: [f64; 3],
    },
    MoveLaunchpad {
        surface_direction: [f64; 3],
    },
    TuneShip {
        settings: ShipTuning,
    },
    TuneDebrisFlow {
        settings: DebrisFlowTuning,
    },
    #[serde(deserialize_with = "deserialize_empty_command")]
    Observe,
    Restore {
        state: Box<Snapshot>,
    },
    Step {
        seconds: f64,
    },
    SetCommand {
        accel: [f64; 3],
        angular_accel: [f64; 3],
    },
    SetLeasedCommand {
        accel: [f64; 3],
        angular_accel: [f64; 3],
        lease_sim_s: f64,
        source_time_s: f64,
        controller_fence: Option<ControlFence>,
    },
    SetSensorFault {
        target: SensorFaultTarget,
        mode: SensorFaultMode,
        duration_sim_s: Option<f64>,
    },
    #[serde(deserialize_with = "deserialize_empty_command")]
    ClearSensorFaults,
    #[serde(deserialize_with = "deserialize_empty_command")]
    Stop,
    #[serde(deserialize_with = "deserialize_empty_command")]
    DebugUpright,
}

/// Read-only local debug geometry query, not an SDK sensor or a flight policy.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LookaheadQuery {
    pub target_world_m: [f64; 3],
    pub max_distance_m: f64,
}

impl LookaheadQuery {
    pub fn validate(&self) -> Result<(), String> {
        if !self.target_world_m.iter().all(|value| {
            value.is_finite() && value.abs() <= crate::scenario::settings().limits.position_m
        }) {
            return Err(
                "look-ahead target must be finite and within the world position limit".into(),
            );
        }
        if !self.max_distance_m.is_finite() || !(1.0..=100_000.0).contains(&self.max_distance_m) {
            return Err(
                "look-ahead max_distance_m must be finite and between 1 and 100000 m".into(),
            );
        }
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LookaheadReport {
    pub source_seed: u64,
    pub source_time_s: f64,
    pub frame: String,
    pub origin_world_m: [f64; 3],
    pub direction_world: [f64; 3],
    /// Actual query range, limited by both the target and the requested maximum.
    pub max_distance_m: f64,
    pub hit: Option<LookaheadHit>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LookaheadHit {
    pub distance_m: f64,
    pub position_world_m: [f64; 3],
    pub normal_world: [f64; 3],
    pub entity: EntityId,
}

pub(crate) fn deserialize_empty_command<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<(), D::Error> {
    // Serde's internally tagged unit variants otherwise ignore extra fields,
    // even when their enum declares deny_unknown_fields.
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct EmptyCommand {}

    EmptyCommand::deserialize(deserializer).map(|_| ())
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct MissionState {
    pub shuttle_id: EntityId,
    pub target_id: EntityId,
    pub target_distance_m: f64,
    #[serde(default)]
    pub initial_distance_m: f64,
    pub relative_speed_mps: f64,
    pub fuel_used_kg: f64,
    pub damage: f64,
    pub status: MissionStatus,
    #[serde(default)]
    pub score: Option<f64>,
    #[serde(default)]
    pub score_label: String,
    #[serde(default)]
    pub score_breakdown: Option<ScoreBreakdown>,
    #[serde(default)]
    pub phase: MissionPhase,
    pub target_region: LagrangeRegion,
    /// None preserves unavailable history in archives predating native counters.
    #[serde(default)]
    pub flight_analytics: Option<FlightAnalytics>,
}

impl Default for MissionState {
    fn default() -> Self {
        Self {
            shuttle_id: EntityId::Lander,
            target_id: EntityId::ReturnShip,
            target_distance_m: 0.0,
            initial_distance_m: 0.0,
            relative_speed_mps: 0.0,
            fuel_used_kg: 0.0,
            damage: 0.0,
            status: MissionStatus::Running,
            score: None,
            score_label: String::new(),
            score_breakdown: None,
            phase: MissionPhase::OnPad,
            target_region: LagrangeRegion::L2,
            flight_analytics: None,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Observation {
    pub id: EntityId,
    pub kind: EntityKind,
    pub relative_position_m: [f64; 3],
    pub relative_velocity_mps: [f64; 3],
    pub range_m: f64,
    #[serde(default)]
    pub range_rate_mps: f64,
    #[serde(default)]
    pub diameter_m: f64,
    #[serde(default)]
    pub measurement_time_s: f64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PrimaryBody {
    pub name: String,
    pub radii_m: [f64; 3],
    pub mu_m3_s2: f64,
    pub rotation_period_s: f64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct EntityState {
    pub id: EntityId,
    pub name: String,
    pub kind: EntityKind,
    pub position: [f64; 3],
    pub velocity: [f64; 3],
    pub rotation: [f64; 4],
    pub radius_m: f64,
    pub mass_kg: f64,
    pub fuel_kg: f64,
    /// Local hardware maximum, including gain and practice assist. API/manual commands
    /// retain the separate reference acceleration limit in scenario settings.
    pub max_thrust_n: f64,
    pub thrust_n: [f64; 3],
    /// Actual local force/torque gain. Stored command vectors stay unscaled.
    #[serde(default = "unit_actuator_gain")]
    pub actuator_gain: f64,
    #[serde(default)]
    pub thrust_application: ThrustApplication,
    pub status: EntityStatus,
    #[serde(default)]
    pub torque_nm: [f64; 3],
    #[serde(default)]
    pub angular_velocity_rad_s: [f64; 3],
    #[serde(default)]
    pub commanded_accel_mps2: [f64; 3],
    #[serde(default)]
    pub commanded_angular_accel_rad_s2: [f64; 3],
}

fn unit_actuator_gain() -> f64 {
    1.0
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Event {
    pub t_s: f64,
    pub asset: EntityId,
    pub message: String,
}

// JSON numbers cannot preserve an arbitrary u64 through JavaScript. Keeping the
// RNG state as decimal text makes room handoffs preserve future sensor noise.
mod decimal_u64 {
    use serde::{Deserialize, Deserializer, Serializer};

    pub fn serialize<S: Serializer>(value: &u64, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&value.to_string())
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<u64, D::Error> {
        String::deserialize(deserializer)?
            .parse()
            .map_err(serde::de::Error::custom)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn old_snapshot_data_keeps_none_corridors_rng_and_omitted_l2_preset() {
        let scenario = crate::scenario::settings();
        for experiment_settings in [None, Some(ExperimentSettings::default())] {
            let archived = serde_json::json!({
                "model": scenario.description,
                "time_s": 12.5,
                "seed": 100,
                "frame": scenario.frame_description,
                "body": {
                    "name": "Phobos",
                    "radii_m": scenario.gravity.phobos_radii_m,
                    "mu_m3_s2": scenario.gravity.phobos_mu_m3_s2,
                    "rotation_period_s": scenario.gravity.phobos_rotation_period_s
                },
                "experiment_settings": experiment_settings,
                "assets": [],
                "events": [],
                "collisions": 0,
                "rng_state": "18446744073709551615"
            });
            let snapshot: Snapshot = serde_json::from_value(archived).unwrap();
            assert_eq!(snapshot.flight_preset, FlightPreset::FarSideL2);
            assert_eq!(snapshot.launch_site_direction, None);
            assert_eq!(snapshot.trial_start_launch_site_direction, None);
            assert_eq!(snapshot.experiment_settings, experiment_settings);
            assert_eq!(snapshot.rng_state, u64::MAX);
            assert_eq!(snapshot.model, scenario.description);
            let serialized = serde_json::to_value(&snapshot).unwrap();
            assert!(serialized.get("flight_preset").is_none());
            assert!(serialized.get("launch_site_direction").is_none());
            assert!(
                serialized
                    .get("trial_start_launch_site_direction")
                    .is_none()
            );
            assert_eq!(serialized["rng_state"], "18446744073709551615");
            let restored: Snapshot = serde_json::from_value(serialized.clone()).unwrap();
            assert_eq!(serde_json::to_value(restored).unwrap(), serialized);
            let mut explicit_l1 = snapshot;
            explicit_l1.flight_preset = FlightPreset::MarsFacingL1;
            let value = serde_json::to_value(explicit_l1).unwrap();
            assert_eq!(value["flight_preset"], "mars_facing_l1");
            assert_eq!(
                serde_json::from_value::<Snapshot>(value)
                    .unwrap()
                    .flight_preset,
                FlightPreset::MarsFacingL1
            );
        }
    }

    #[test]
    fn explicit_flight_configuration_and_placement_have_strict_typed_envelopes() {
        let settings = ExperimentSettings::default();
        let request = serde_json::json!({
            "op": "configure_flight", "settings": settings,
            "seed": 17, "preset": "mars_facing_l1"
        });
        assert!(matches!(
            serde_json::from_value::<PhysicsCommand>(request.clone()).unwrap(),
            PhysicsCommand::ConfigureFlight {
                seed: Some(17),
                preset: FlightPreset::MarsFacingL1,
                ..
            }
        ));
        let mut missing_preset = request;
        missing_preset.as_object_mut().unwrap().remove("preset");
        assert!(serde_json::from_value::<PhysicsCommand>(missing_preset).is_err());
        assert!(matches!(
            serde_json::from_value::<PhysicsCommand>(serde_json::json!({
                "op": "configure_experiment", "settings": settings, "seed": 17
            }))
            .unwrap(),
            PhysicsCommand::ConfigureExperiment { seed: Some(17), .. }
        ));
        let position_m = [14_000.0, -20.0, 35.0];
        assert!(matches!(
            serde_json::from_value::<PhysicsCommand>(serde_json::json!({
                "op": "place_lander", "position_m": position_m
            }))
            .unwrap(),
            PhysicsCommand::PlaceLander { position_m: actual } if actual == position_m
        ));
        assert!(matches!(
            serde_json::from_value::<PhysicsCommand>(serde_json::json!({
                "op": "move_launchpad", "surface_direction": [1.0, 2.0, 3.0]
            }))
            .unwrap(),
            PhysicsCommand::MoveLaunchpad {
                surface_direction: [1.0, 2.0, 3.0]
            }
        ));
        for request in [
            serde_json::json!({"op": "place_lander", "position_m": [1.0, 2.0]}),
            serde_json::json!({"op": "place_lander", "position_m": [1.0, 2.0, "three"]}),
            serde_json::json!({"op": "place_lander", "position_m": position_m, "velocity": [0.0, 0.0, 0.0]}),
            serde_json::json!({"op": "move_launchpad", "surface_direction": [1.0, 2.0]}),
            serde_json::json!({"op": "move_launchpad", "surface_direction": [1.0, 2.0, "three"]}),
            serde_json::json!({"op": "move_launchpad", "surface_direction": [1.0, 2.0, 3.0], "seed": 17}),
            serde_json::json!({"op": "configure_experiment", "settings": settings, "preset": "far_side_l2"}),
            serde_json::json!({"op": "configure_flight", "settings": settings, "preset": "mars_facing_l2"}),
        ] {
            assert!(serde_json::from_value::<PhysicsCommand>(request).is_err());
        }
    }

    #[test]
    fn empty_debris_report_serializes_unavailable_extrema_and_spin_as_null() {
        let value = serde_json::to_value(DebrisDynamicsReport {
            source_time_s: 0.0,
            frame: crate::scenario::settings().frame_description.clone(),
            physical_count: 0,
            moving_count: 0,
            mean_velocity_world_mps: [0.0; 3],
            min_speed_mps: None,
            max_speed_mps: None,
            mean_spin_rad_s: None,
            total_mass_kg: 0.0,
        })
        .unwrap();
        assert!(value["min_speed_mps"].is_null());
        assert!(value["max_speed_mps"].is_null());
        assert!(value["mean_spin_rad_s"].is_null());
        assert_eq!(value["physical_count"], 0);
        assert_eq!(value["moving_count"], 0);
    }
}
