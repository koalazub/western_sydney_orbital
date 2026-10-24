use crate::domain::{
    EntityId, EntityKind, EntityStatus, LagrangeRegion, MissionPhase, MissionStatus,
};
pub use crate::propulsion::ThrustApplication;
pub use crate::terrain::{LandingSite, landing_site, phobos_geometry};
use serde::{Deserialize, Serialize};
#[path = "experiment_settings.rs"]
mod experiment_settings;
pub use experiment_settings::{
    DebrisExperimentSettings, ExperimentSettings, MAX_DEBRIS_COUNT, ShipExperimentSettings,
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
    pub frame: String,
    pub body: PrimaryBody,
    #[serde(default)]
    pub environment: crate::astrophysics::Environment,
    /// None identifies archived corridor runs with their original fixed physics.
    #[serde(default)]
    pub experiment_settings: Option<ExperimentSettings>,
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
    #[serde(deserialize_with = "deserialize_empty_command")]
    Stop,
    #[serde(deserialize_with = "deserialize_empty_command")]
    DebugUpright,
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
    pub phase: MissionPhase,
    pub target_region: LagrangeRegion,
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
            phase: MissionPhase::OnPad,
            target_region: LagrangeRegion::L2,
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
    /// Local hardware maximum, including practice assist. API/manual commands
    /// retain the separate reference acceleration limit in scenario settings.
    pub max_thrust_n: f64,
    pub thrust_n: [f64; 3],
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
