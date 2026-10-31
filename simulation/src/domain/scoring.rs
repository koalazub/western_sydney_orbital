use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ScoreFormula {
    /// Legacy local SI diagnostic identifier, retained for archive compatibility.
    PracticeNormalizedTimeDamageFuelV1,
    /// Source-equivalent arithmetic over local elapsed time, damage and integrated fuel.
    /// The counters are local, so this is not a claim of STP runtime parity.
    OfficialTimeDamageFuelV1,
}

/// Selects whether a live flight publishes the archived local diagnostic or the
/// source-equivalent formula. Old archives omit the field and remain unscored.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MissionScoring {
    #[default]
    ArchivedUnscored,
    OfficialTimeDamageFuelV1,
}

impl MissionScoring {
    pub const fn is_archived(&self) -> bool {
        matches!(self, Self::ArchivedUnscored)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ScoreProgressBasis {
    TimeRatio,
    RemainingDistanceRatio,
}

/// Local diagnostic receipts, not STP scores or compatible source counters.
/// Retain recorded values when reading historical receipts. The archived practice
/// formula and the source-arithmetic estimate remain distinguishable by `formula`.
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
