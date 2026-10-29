use serde::{Deserialize, Serialize};

/// Own-ship telemetry integrated by native physics, not official score inputs.
/// Active flags and current durations describe the last integrated step.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FlightAnalytics {
    pub schema_version: u32,
    pub definition_id: String,
    pub observed_sim_s: f64,
    /// Contiguous steps above the configured actual propellant-force threshold.
    pub burn_count: u64,
    pub burn_duration_s: f64,
    pub longest_burn_s: f64,
    /// Applied actuator torque only, excluding gravity and contact impulses.
    pub angular_burn_count: u64,
    pub angular_burn_duration_s: f64,
    pub longest_angular_burn_s: f64,
    /// Actual post-step spin, including coasting and contact-induced rotation.
    pub turn_count: u64,
    pub turn_duration_s: f64,
    pub longest_turn_s: f64,
    /// Sum of shortest pre/post native-step orientation arcs, in radians.
    pub total_turn_angle_rad: f64,
    pub peak_angular_speed_rad_s: f64,
    /// Integral of the force charged by the existing local propellant model.
    pub propellant_impulse_ns: f64,
    /// Integral of actual actuator torque magnitude, in newton-metre seconds.
    pub angular_impulse_nms: f64,
    pub burn_active: bool,
    pub angular_burn_active: bool,
    pub turn_active: bool,
    pub current_burn_s: f64,
    pub current_angular_burn_s: f64,
    pub current_turn_s: f64,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct FlightAnalyticsDefinition {
    pub schema_version: u32,
    pub definition_id: String,
    pub burn_force_threshold_n: f64,
    pub angular_torque_threshold_nm: f64,
    pub turn_speed_threshold_rad_s: f64,
}

/// Measurements from one real actuator application and the ensuing Rapier step.
#[derive(Clone, Copy, Debug)]
pub(crate) struct FlightAnalyticsStep {
    pub start_sim_s: f64,
    pub duration_s: f64,
    pub propellant_force_n: f64,
    pub actuator_torque_world_nm: [f64; 3],
    pub rotation_before_xyzw: [f64; 4],
    pub rotation_after_xyzw: [f64; 4],
    pub angular_velocity_after_rad_s: [f64; 3],
}
