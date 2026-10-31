use crate::domain::{FlightPreset, LagrangeRegion, LaunchSide, ShipCameraId};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize)]
pub struct Scenario {
    pub default_seed: u64,
    pub description: String,
    pub frame_description: String,
    pub experiment_description: String,
    pub directed_stream_description: String,
    pub experiment_settings: crate::model::ExperimentSettings,
    pub flight_presets: FlightPresets,
    pub impact_ejecta_defaults: crate::model::ImpactEjectaSettings,
    pub official_cloud: OfficialCloudSettings,
    pub transit_stream: TransitStreamSettings,
    pub debris_trajectory_adjustment: DebrisTrajectoryAdjustmentConstants,
    /// Frozen calibration reference for archived None corridor live overrides.
    pub legacy_corridor_flow_reference: crate::model::DebrisFlowTuning,
    pub gravity: Gravity,
    pub integration: Integration,
    pub lander: Lander,
    pub debug_thrust: DebugThrust,
    pub practice: Practice,
    pub pad: Pad,
    pub mission: Mission,
    pub debris: Debris,
    pub sensors: Sensors,
    pub ship_cameras: ShipCameras,
    pub lighting: Lighting,
    pub terrain: Terrain,
    pub limits: Limits,
    pub contacts: Contacts,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TransitStreamSettings {
    pub version: u32,
    pub transverse_half_width_m: f64,
    pub launch_trail_m: f64,
    pub target_lead_m: f64,
}

/// Official SpawnDebris.py relative cloud, entity weights and radar schedule.
/// Values are versioned data; the terrain site supplies the local-up direction.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OfficialCloudSettings {
    pub version: u32,
    pub count: usize,
    /// Body-frame half extents around the offset centre, from Spread_m.
    pub half_extents_m: [f64; 3],
    /// Offset along the lander site's local up, from VerticalOffset_m.
    pub vertical_offset_m: f64,
    /// sqrt(mu / radius) uses the official Phobos value, not the local mesh.
    pub drift_reference_mu_m3_s2: f64,
    pub drift_reference_radius_m: f64,
    pub drift_speed_fraction_min: f64,
    pub drift_speed_fraction_max: f64,
    pub mass_scale_exponent: f64,
    pub collision_radius_max_m: f64,
    pub classes: Vec<OfficialDebrisClass>,
    pub radar: OfficialRadarSettings,
}

impl OfficialCloudSettings {
    pub fn drift_speed_range_mps(&self) -> (f64, f64) {
        let orbital = (self.drift_reference_mu_m3_s2 / self.drift_reference_radius_m).sqrt();
        (
            self.drift_speed_fraction_min * orbital,
            self.drift_speed_fraction_max * orbital,
        )
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OfficialDebrisClass {
    pub id: OfficialDebrisClassId,
    pub weight: f64,
    pub radar_diameter_m: f64,
    pub scale_min: f64,
    pub scale_max: f64,
    pub base_mass_kg: f64,
}

impl OfficialDebrisClass {
    pub fn radius_m(&self, scale: f64) -> f64 {
        0.5 * self.radar_diameter_m * scale
    }

    pub fn mass_kg(&self, scale: f64, exponent: f64) -> f64 {
        self.base_mass_kg * scale.powf(exponent)
    }
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum OfficialDebrisClassId {
    Rock1,
    Rock2,
    Rock3,
    ExplodedTank,
}

#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OfficialRadarSettings {
    pub version: u32,
    pub observed_count: usize,
    pub iteration_hz: f64,
    pub publish_every_rows: usize,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DebrisTrajectoryAdjustmentConstants {
    pub version: u32,
    pub source_url: String,
    pub source_enabled_by_default: bool,
    pub global_rate_hz: f64,
    pub cutoff_m: f64,
    pub typical_speed_mps: f64,
    pub delta_v_factor: f64,
    pub scheduling_note: String,
}

impl Scenario {
    pub fn flight_preset(&self, preset: FlightPreset) -> &FlightPresetSettings {
        match preset {
            FlightPreset::MarsFacingL1 => &self.flight_presets.mars_facing_l1,
            FlightPreset::FarSideL2 => &self.flight_presets.far_side_l2,
            FlightPreset::CompetitionReference => &self.flight_presets.competition_reference,
        }
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FlightPresets {
    pub mars_facing_l1: FlightPresetSettings,
    pub far_side_l2: FlightPresetSettings,
    pub competition_reference: FlightPresetSettings,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FlightPresetSettings {
    pub label: String,
    pub launch_side: LaunchSide,
    pub target_region: LagrangeRegion,
    pub target_name: String,
    /// Required only for the local-up competition target; lagrange presets use
    /// their computed collinear environment positions.
    #[serde(default)]
    pub target_distance_m: Option<f64>,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct Gravity {
    pub mars_mu_m3_s2: f64,
    pub phobos_mu_m3_s2: f64,
    pub separation_m: f64,
    pub mars_radius_m: f64,
    pub phobos_radii_m: [f64; 3],
    pub phobos_rotation_period_s: f64,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct Integration {
    pub fixed_step_s: f64,
    pub maximum_command_step_s: f64,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct Lander {
    pub mass_kg: f64,
    pub fuel_unlimited: bool,
    pub inertia_kg_m2: [[f64; 3]; 3],
    pub max_accel_mps2: f64,
    pub max_angular_accel_rad_s2: f64,
    pub specific_impulse_s: f64,
    pub standard_gravity_mps2: f64,
    pub proxy_radius_m: f64,
    pub hull_half_extents_m: [f64; 3],
    pub leg_positions_m: [[f64; 3]; 4],
    pub leg_half_extents_m: [f64; 3],
    pub foot_radius_m: f64,
    /// Synthetic practice nozzles for forces +X, -X, +Y, -Y, +Z, -Z.
    /// These are not a claimed STP actuator layout.
    pub manual_nozzles_body_m: [[f64; 3]; 6],
}

/// Debug-only exhaust animation, not a photometric or propellant-flow model.
#[derive(Clone, Copy, Debug, Deserialize)]
pub struct DebugThrust {
    pub max_particles: usize,
    pub particles_per_second_per_jet: f64,
    pub particle_lifetime_s: f64,
    pub exhaust_speed_mps: f64,
    pub particle_radius_m: f64,
    pub spread_rad: f64,
}

/// Local keyboard assistance, separate from the reference controller API limits.
#[derive(Clone, Copy, Debug, Deserialize)]
pub struct Practice {
    pub space_lift_accel_mps2: f64,
    pub space_boost_enabled: bool,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct Pad {
    pub half_extents_m: [f64; 3],
    pub lander_clearance_m: f64,
    pub fixed_mass_kg: f64,
}

#[derive(Clone, Debug, Deserialize)]
pub struct Mission {
    pub target_name: String,
    pub target_region: LagrangeRegion,
    pub target_mass_kg: f64,
    pub target_radius_m: f64,
    pub arrival_radius_m: f64,
    pub impact_threshold_j: f64,
    pub nominal_fuel_kg: f64,
    pub nominal_time_s: f64,
    pub score_offset: f64,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct Debris {
    pub count: usize,
    pub x_min_m: f64,
    pub x_span_m: f64,
    pub y_half_width_m: f64,
    pub z_half_width_m: f64,
    pub speed_min_mps: f64,
    pub speed_span_mps: f64,
    pub radius_min_m: f64,
    pub radius_span_m: f64,
    pub density_kg_m3: f64,
    pub spin_max_rad_s: f64,
    pub radar_count: usize,
    pub diameter_noise_fraction: f64,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct Sensors {
    pub range_noise_m: f64,
    pub range_rate_noise_mps: f64,
    pub cadence_hz: f64,
}

/// Fixed optics for two RGB8 sensors. Each color channel has eight bits.
#[derive(Clone, Debug, Deserialize)]
pub struct ShipCameras {
    pub resolution_px: [u32; 2],
    /// Full angle, not the half-angle from the optical axis to an image edge.
    pub field_of_view_deg: f32,
    pub field_of_view_convention: FieldOfViewConvention,
    pub field_of_view_source: CameraSource,
    pub mount_source: CameraSource,
    pub near_clip_m: f64,
    pub far_clip_m: f64,
    pub cadence_hz: f64,
    /// Fixed exposure. The initial 15.0 is Bevy's Exposure::SUNLIGHT EV100.
    pub exposure_ev100: f32,
    pub ambient_brightness: f32,
    pub mounts: [CameraMount; 2],
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum FieldOfViewConvention {
    VerticalFullAngle,
    HorizontalFullAngle,
    DiagonalFullAngle,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CameraSourceStatus {
    PracticeUnverified,
    DocumentedExample,
    DocumentedStp,
}

#[derive(Clone, Debug, Deserialize)]
pub struct CameraSource {
    pub status: CameraSourceStatus,
    pub url: String,
    pub note: String,
}

/// STP body frame: +X forward, +Y left, +Z up. The lander's +Z launch axis is unchanged.
/// Aim follows the documented passive body-to-camera Euler 3-2-1 transform, with zero roll.
#[derive(Clone, Copy, Debug, Deserialize)]
pub struct CameraMount {
    pub id: ShipCameraId,
    pub position_m: [f64; 3],
    pub yaw_limits_deg: [f32; 2],
    pub pitch_limits_deg: [f32; 2],
    pub aim_yaw_deg: f32,
    pub aim_pitch_deg: f32,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct Lighting {
    /// Fixed unit direction toward the Sun in the rotating world frame, not an ephemeris.
    pub direction_to_sun: [f32; 3],
    pub illuminance_lux: f32,
    /// At mean Mars distance, asin(695_700 km / 227_940_000 km) is about 0.175 degrees.
    pub sun_angular_radius_rad: f32,
    /// Background rendering distance, not an astrophysical Sun position.
    pub sun_proxy_distance_m: f64,
    pub debug_ambient_brightness: f32,
    pub mars_surface: RockSurface,
    pub phobos_surface: RockSurface,
    pub debris_surface: RockSurface,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct RockSurface {
    pub base_color_srgb: [f32; 3],
    pub perceptual_roughness: f32,
}

#[derive(Clone, Debug, Deserialize)]
pub struct Terrain {
    pub radii_m: [f64; 3],
    pub latitude_segments: usize,
    pub longitude_segments: usize,
    pub relief_amplitude: f64,
    pub relief_longitude_frequency: f64,
    pub relief_latitude_frequency: f64,
    pub relief_longitude_phase: f64,
    pub crater_rim_fraction: f64,
    pub crater_rim_width: f64,
    pub crater_rim_depth: f64,
    pub craters: Vec<Crater>,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct Crater {
    pub center: [f64; 3],
    pub width_rad: f64,
    pub depth: f64,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct Limits {
    pub events: usize,
    pub contacts: usize,
    pub observations: usize,
    pub position_m: f64,
    pub velocity_mps: f64,
    pub mass_kg: f64,
    pub radius_m: f64,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct Contacts {
    pub friction: f64,
    pub restitution: f64,
}
