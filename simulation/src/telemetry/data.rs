use crate::{domain::EntityId, model::ThrustApplication};
use serde::Serialize;
use std::borrow::Cow;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum ReportStatus {
    #[default]
    WaitingForSnapshot,
    Ready,
    Partial,
    Unavailable,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum InputIssue {
    InvalidSimulationTime,
    UnknownFrame,
    UnknownTerrain,
    MissingOrAmbiguousOwnShip,
    MissingOrAmbiguousMissionTarget,
    InvalidOwnPosition,
    InvalidOwnVelocity,
    InvalidOwnRotation,
    UndefinedRadialDirection,
    InvalidMass,
    InvalidThrust,
    InvalidTorque,
    InvalidAngularVelocity,
    InvalidTargetPosition,
    InvalidTargetVelocity,
    UndefinedClosingDirection,
    InvalidFuelEstimate,
    InvalidDamageEstimate,
    NonfiniteDerivedValue,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum EpochReset {
    InitialSample,
    SeedChanged,
    TimeRewound,
    ReferenceChanged,
    SameTimeKinematicsChanged,
    InputDiscontinuity,
    InputRecovered,
}

#[derive(Clone, Debug, Default, Serialize)]
pub(super) struct Epoch {
    pub index: u64,
    pub seed: Option<u64>,
    pub started_at_time_s: Option<f64>,
    pub observed_duration_s: Option<f64>,
    pub last_reset: Option<EpochReset>,
}

#[derive(Clone, Copy, Debug, Default, Serialize)]
pub(super) struct FlightMetrics {
    pub speed_mps: Option<f64>,
    pub radial_climb_mps: Option<f64>,
    pub terrain_center_clearance_m: Option<f64>,
    pub target_center_range_m: Option<f64>,
    pub target_closing_mps: Option<f64>,
    pub applied_thrust_accel_mps2: Option<f64>,
    pub thrust_force_n: Option<f64>,
    pub torque_nm: Option<f64>,
    pub angular_speed_rad_s: Option<f64>,
    pub fuel_used_kg_estimate: Option<f64>,
    pub damage_j_estimate: Option<f64>,
}

#[derive(Clone, Copy, Debug, Default, Serialize)]
pub(super) struct Peaks {
    pub speed_mps: Option<f64>,
    pub radial_climb_mps: Option<f64>,
    pub terrain_center_clearance_m: Option<f64>,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum AccelerationStatus {
    #[default]
    WaitingForNextSample,
    FreshInterval,
    HeldWithoutTimeAdvance,
    BaselineReset,
    Unavailable,
}

#[derive(Clone, Copy, Debug, Default, Serialize)]
pub(super) struct ObservedFrameAcceleration {
    pub status: AccelerationStatus,
    pub vector_mps2: Option<[f64; 3]>,
    pub magnitude_mps2: Option<f64>,
    pub interval_start_s: Option<f64>,
    pub interval_end_s: Option<f64>,
    pub interval_s: Option<f64>,
}

#[derive(Clone, Copy, Debug, Default, Serialize)]
pub(super) struct Actuation {
    pub thrust_application: Option<ThrustApplication>,
    /// This describes the stored actuator mode, not a keyboard or UI toggle.
    pub practice_boost_active: Option<bool>,
}

#[derive(Debug, Serialize)]
pub(super) struct FrameMetadata {
    pub snapshot_description: Option<Cow<'static, str>>,
    pub recognized: bool,
    pub terrain_recognized: bool,
    pub origin: &'static str,
    pub axes: &'static str,
    pub speed_basis: &'static str,
}

#[derive(Debug, Serialize)]
pub(super) struct Provenance {
    pub own_ship_id: Option<EntityId>,
    pub target_id: Option<EntityId>,
    pub own_state: &'static str,
    pub target_state: &'static str,
    pub terrain: &'static str,
    pub fuel_and_damage: &'static str,
    pub hidden_debris_truth_used: bool,
    pub camera_state_used: bool,
}

#[derive(Clone, Copy, Debug, Serialize)]
pub(super) struct Units {
    pub distance: &'static str,
    pub speed: &'static str,
    pub acceleration: &'static str,
    pub force: &'static str,
    pub torque: &'static str,
    pub angular_speed: &'static str,
    pub mass: &'static str,
    pub energy: &'static str,
    pub simulation_time: &'static str,
}

#[derive(Debug, Serialize)]
pub(super) struct Caveats {
    pub local_practice_only: bool,
    pub stp_parity: bool,
    pub read_only: bool,
    pub reference_command_limit_mps2: Option<f64>,
    pub practice_boost_limit_mps2: Option<f64>,
    pub practice_boost_enabled_in_settings: bool,
    pub fuel_unlimited: bool,
    pub speed: &'static str,
    pub clearance: &'static str,
    pub climb: &'static str,
    pub target_range: &'static str,
    pub target_closing: &'static str,
    pub observed_acceleration: &'static str,
    pub actuation: &'static str,
    pub fuel: &'static str,
    pub damage: &'static str,
    pub peaks: &'static str,
    pub restore_detection: &'static str,
}

#[derive(Debug, Serialize)]
pub(super) struct Report {
    pub schema_version: u32,
    pub status: ReportStatus,
    pub input_issues: Vec<InputIssue>,
    pub source_time_s: Option<f64>,
    pub epoch: Epoch,
    pub frame: FrameMetadata,
    pub metrics: FlightMetrics,
    pub observed_frame_acceleration: ObservedFrameAcceleration,
    pub peaks: Peaks,
    pub actuation: Actuation,
    pub units: &'static Units,
    pub provenance: Provenance,
    pub caveats: &'static Caveats,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub(super) struct Kinematics {
    pub time_s: f64,
    pub position_m: [f64; 3],
    pub velocity_mps: [f64; 3],
    pub rotation_xyzw: [f64; 4],
    pub angular_velocity_rad_s: [f64; 3],
}

pub(super) struct Measurement {
    pub report: Report,
    pub kinematics: Option<Kinematics>,
}

pub(super) struct Reference {
    pub seed: u64,
    pub frame: String,
    pub terrain_recognized: bool,
    pub own_ship_id: EntityId,
    pub target_id: EntityId,
}

#[derive(Default, bevy::prelude::Resource)]
pub(super) struct Tracker {
    pub reference: Option<Reference>,
    pub previous_time_s: Option<f64>,
    pub previous: Option<Kinematics>,
    pub epoch: Epoch,
    pub peaks: Peaks,
    pub acceleration: ObservedFrameAcceleration,
}
