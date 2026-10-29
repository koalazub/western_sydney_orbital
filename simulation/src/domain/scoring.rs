use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ScoreFormula {
    /// Legacy local SI diagnostic identifier, retained for archive compatibility.
    PracticeNormalizedTimeDamageFuelV1,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ScoreProgressBasis {
    TimeRatio,
    RemainingDistanceRatio,
}

/// Archived local SI diagnostics, not STP scores or compatible source counters.
/// Retain recorded values when reading historical receipts.
/// Penalties are positive values to subtract; the total can be negative.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScoreBreakdown {
    pub formula: ScoreFormula,
    pub progress_basis: ScoreProgressBasis,
    pub progress_points: f64,
    pub collision_penalty_points: f64,
    pub fuel_penalty_points: f64,
    pub offset_points: f64,
    pub total: f64,
    pub nominal_time_s: f64,
    pub nominal_fuel_kg: f64,
    pub impact_threshold_j: f64,
}
