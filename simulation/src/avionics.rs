//! Persisted delivery state. These faults suppress real samples, not physics.
use super::{AppliedPhysics, DebrisDistribution, EntityState, MissionState};
use serde::{Deserialize, Serialize};

pub type RadarRow = [[f64; 3]; 3];

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SensorUnitId {
    RadarPrimary,
    RadarBackup,
    CameraTop,
    CameraBottom,
    Navigation,
}

impl SensorUnitId {
    pub const ALL: [Self; 5] = [
        Self::RadarPrimary,
        Self::RadarBackup,
        Self::CameraTop,
        Self::CameraBottom,
        Self::Navigation,
    ];
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SensorFaultTarget {
    RadarPrimary,
    RadarBackup,
    CameraTop,
    CameraBottom,
    RadarBus,
    AllAvionics,
    Navigation,
}

impl SensorFaultTarget {
    pub fn affects(self, unit: SensorUnitId) -> bool {
        match self {
            Self::RadarPrimary => unit == SensorUnitId::RadarPrimary,
            Self::RadarBackup => unit == SensorUnitId::RadarBackup,
            Self::CameraTop => unit == SensorUnitId::CameraTop,
            Self::CameraBottom => unit == SensorUnitId::CameraBottom,
            Self::Navigation => unit == SensorUnitId::Navigation,
            Self::RadarBus => {
                matches!(unit, SensorUnitId::RadarPrimary | SensorUnitId::RadarBackup)
            }
            Self::AllAvionics => true,
        }
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SensorFaultMode {
    #[default]
    Nominal,
    Frozen,
    Unavailable,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SensorDelivery {
    Ready,
    Unavailable,
    Frozen,
    WaitingForSample,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SensorFault {
    pub target: SensorFaultTarget,
    pub mode: SensorFaultMode,
    pub expires_at_sim_time_s: f64,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum RadarSelection {
    #[default]
    Unknown,
    LegacyFixedSubset,
    NearestNoisyCentersV1,
    #[serde(
        rename = "official_rolling",
        alias = "official_first_200_rolling",
        alias = "official_first200_rolling_v1"
    )]
    OfficialRollingV1,
}

impl RadarSelection {
    fn for_acquisition(distribution: Option<DebrisDistribution>) -> Self {
        match distribution {
            Some(DebrisDistribution::TransitStream) => Self::NearestNoisyCentersV1,
            Some(DebrisDistribution::OfficialCloud) => Self::OfficialRollingV1,
            Some(DebrisDistribution::DirectedStream | DebrisDistribution::ImpactEjecta { .. })
            | None => Self::LegacyFixedSubset,
        }
    }

    fn candidate_limit(self, sample_limit: usize) -> usize {
        match self {
            Self::Unknown | Self::LegacyFixedSubset | Self::OfficialRollingV1 => sample_limit,
            Self::NearestNoisyCentersV1 => usize::MAX,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RadarFrame {
    pub sample_time_s: f64,
    pub sequence: u64,
    pub rows: Vec<RadarRow>,
    /// An absent version cannot establish how an archived sample was selected.
    #[serde(default)]
    selection: RadarSelection,
}

impl RadarFrame {
    pub(crate) fn unversioned(sample_time_s: f64, sequence: u64, rows: Vec<RadarRow>) -> Self {
        Self {
            sample_time_s,
            sequence,
            rows,
            selection: RadarSelection::Unknown,
        }
    }

    pub(crate) fn acquired(
        sample_time_s: f64,
        sequence: u64,
        rows: Vec<RadarRow>,
        distribution: Option<DebrisDistribution>,
    ) -> Self {
        Self {
            sample_time_s,
            sequence,
            rows,
            selection: RadarSelection::for_acquisition(distribution),
        }
    }

    pub(crate) fn acquisition_candidate_limit(
        distribution: Option<DebrisDistribution>,
        sample_limit: usize,
    ) -> usize {
        RadarSelection::for_acquisition(distribution).candidate_limit(sample_limit)
    }

    fn observed_selection(&self) -> RadarSelection {
        if self.rows.is_empty() {
            RadarSelection::Unknown
        } else {
            self.selection
        }
    }

    pub(crate) fn selection_label(&self) -> &'static str {
        match self.observed_selection() {
            RadarSelection::Unknown => "unknown",
            RadarSelection::LegacyFixedSubset => "legacy_fixed_subset",
            RadarSelection::NearestNoisyCentersV1 => "nearest_noisy_centers",
            RadarSelection::OfficialRollingV1 => "official_rolling",
        }
    }

    pub(crate) fn primary_provenance(&self) -> &'static str {
        match self.observed_selection() {
            RadarSelection::Unknown => "unknown",
            RadarSelection::LegacyFixedSubset => "native_world_radar_primary_legacy_rng",
            RadarSelection::NearestNoisyCentersV1 => {
                "native_world_radar_primary_nearest_noisy_centers_v1"
            }
            RadarSelection::OfficialRollingV1 => "native_world_radar_primary_official_rolling_v1",
        }
    }

    pub(crate) fn backup_provenance(&self) -> &'static str {
        match self.observed_selection() {
            RadarSelection::Unknown => "unknown",
            RadarSelection::LegacyFixedSubset => "native_world_radar_backup_independent_rng_v1",
            RadarSelection::NearestNoisyCentersV1 => {
                "native_world_radar_backup_independent_rng_nearest_noisy_centers_v1"
            }
            RadarSelection::OfficialRollingV1 => {
                "native_world_radar_backup_independent_rng_official_rolling_v1"
            }
        }
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RadarDeliveryState {
    pub acquisition_sequence: u64,
    pub sample: Option<RadarFrame>,
    pub waiting_for_sample: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NavigationSample {
    pub sample_time_s: f64,
    pub sequence: u64,
    pub shuttle: EntityState,
    pub target: EntityState,
    pub mission: MissionState,
    pub applied_physics: Option<AppliedPhysics>,
    pub applied_physics_error: Option<String>,
    pub terrain_radius_at_shuttle_m: Option<f64>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NavigationDeliveryState {
    pub acquisition_sequence: u64,
    pub sample: Option<NavigationSample>,
    pub waiting_for_sample: bool,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CommandWatchdogStatus {
    #[default]
    Idle,
    Active,
    Expired,
    SensorChanged,
    Restored,
    Revoked,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CommandWatchdog {
    pub status: CommandWatchdogStatus,
    pub source_time_s: Option<f64>,
    pub expires_at_sim_time_s: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub controller_fence: Option<ControlFence>,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ControlAuthorityStatus {
    #[default]
    Idle,
    Active,
    Expired,
    Revoked,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlAuthority {
    pub epoch: u32,
    pub term: u32,
    pub status: ControlAuthorityStatus,
    pub run_id: Option<String>,
    pub controller_id: Option<String>,
    pub expires_at_sim_time_s: Option<f64>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlFence {
    pub epoch: u32,
    pub term: u32,
    pub run_id: String,
    pub controller_id: String,
}

impl ControlAuthority {
    pub fn fence(&self) -> Option<ControlFence> {
        Some(ControlFence {
            epoch: self.epoch,
            term: self.term,
            run_id: self.run_id.clone()?,
            controller_id: self.controller_id.clone()?,
        })
    }
}

/// IDs identify a consent cohort and a process, not a secret or a credential.
pub fn valid_control_id(id: &str) -> bool {
    (1..=64).contains(&id.len())
        && id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlClaim {
    pub run_id: String,
    pub controller_id: String,
    pub expected_epoch: u32,
    pub expected_term: u32,
    pub source_time_s: f64,
    pub lease_sim_s: f64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlRenewal {
    pub controller_fence: ControlFence,
    pub source_time_s: f64,
    pub lease_sim_s: f64,
}

/// An ownership receipt, not a sensor sample or an actuator application.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlRenewalReceipt {
    pub source_time_s: f64,
    pub lease_sim_s: f64,
    pub sensor_revision: u32,
    pub control_authority: ControlAuthority,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlRelease {
    pub run_id: String,
    pub expected_epoch: u32,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SensorState {
    pub schema_version: u32,
    pub revision: u32,
    pub faults: Vec<SensorFault>,
    #[serde(with = "super::decimal_u64")]
    pub backup_rng_state: u64,
    pub primary: RadarDeliveryState,
    pub backup: RadarDeliveryState,
    pub navigation: NavigationDeliveryState,
    pub command_watchdog: CommandWatchdog,
    /// Absent in legacy archives and before the first fenced consent cohort.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub control_authority: Option<ControlAuthority>,
}

impl SensorState {
    pub fn new(seed: u64) -> Self {
        // A separate, versioned stream. Never draw a seed from the physics RNG.
        let backup_rng_state = (seed ^ 0x7261_6461_725f_6231).max(1);
        Self {
            schema_version: 1,
            revision: 0,
            faults: Vec::new(),
            backup_rng_state,
            primary: RadarDeliveryState::default(),
            backup: RadarDeliveryState::default(),
            navigation: NavigationDeliveryState::default(),
            command_watchdog: CommandWatchdog::default(),
            control_authority: None,
        }
    }

    pub fn mode(&self, unit: SensorUnitId) -> SensorFaultMode {
        let mut result = SensorFaultMode::Nominal;
        for fault in self
            .faults
            .iter()
            .filter(|fault| fault.target.affects(unit))
        {
            match fault.mode {
                SensorFaultMode::Unavailable => return SensorFaultMode::Unavailable,
                SensorFaultMode::Frozen => result = SensorFaultMode::Frozen,
                SensorFaultMode::Nominal => {}
            }
        }
        result
    }

    pub fn active_faults(&self, unit: SensorUnitId) -> Vec<SensorFaultTarget> {
        self.faults
            .iter()
            .filter(|fault| fault.target.affects(unit))
            .map(|fault| fault.target)
            .collect()
    }

    pub fn navigation_delivery(&self) -> SensorDelivery {
        if self.mode(SensorUnitId::Navigation) == SensorFaultMode::Nominal
            && !self.navigation.waiting_for_sample
            && self.navigation.sample.is_some()
        {
            // Nominal navigation retains the original live local readback.
            return SensorDelivery::Ready;
        }
        delivery(
            self.mode(SensorUnitId::Navigation),
            self.navigation.sample.is_some(),
            self.navigation.waiting_for_sample,
        )
    }
}

pub fn delivery(mode: SensorFaultMode, has_sample: bool, waiting: bool) -> SensorDelivery {
    match mode {
        SensorFaultMode::Unavailable => SensorDelivery::Unavailable,
        SensorFaultMode::Frozen if has_sample => SensorDelivery::Frozen,
        SensorFaultMode::Frozen => SensorDelivery::WaitingForSample,
        SensorFaultMode::Nominal if waiting || !has_sample => SensorDelivery::WaitingForSample,
        SensorFaultMode::Nominal => SensorDelivery::Ready,
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LeasedCommand {
    pub accel: [f64; 3],
    pub angular_accel: [f64; 3],
    pub lease_sim_s: f64,
    pub source_time_s: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub controller_fence: Option<ControlFence>,
}

#[derive(Debug)]
pub enum LeaseError {
    Expired,
    Unavailable,
    StaleContext,
    Rejected(String),
}

impl std::fmt::Display for LeaseError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Expired => formatter.write_str("The autonomous decision expired before invocation; no command was applied."),
            Self::Unavailable => formatter.write_str("Autonomous actuation requires ready navigation delivery and at least one ready radar; no command was applied."),
            Self::StaleContext => formatter.write_str("Controller authority changed or is no longer active; no command was applied."),
            Self::Rejected(message) => formatter.write_str(message),
        }
    }
}
