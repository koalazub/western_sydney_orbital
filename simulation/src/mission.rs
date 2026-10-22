use crate::{
    domain::{MissionPhase, MissionStatus},
    model::MissionState,
    scenario::Mission,
};

#[derive(Clone, Copy, Debug)]
pub struct FlightMetrics {
    pub distance_m: f64,
    pub lander_position_m: [f64; 3],
    pub pad_position_m: [f64; 3],
    pub time_s: f64,
    pub damage_j: f64,
    pub minimum_step_s: f64,
    pub transit_x_m: f64,
    pub on_pad_radius_m: f64,
}

#[derive(Clone, Copy, Debug)]
pub struct Assessment {
    pub status: MissionStatus,
    pub phase: MissionPhase,
    pub score: Option<f64>,
}

/// Pure mission-state assessment: no physics, clocks, events, or renderer state.
pub fn assess(mission: &MissionState, metrics: FlightMetrics, config: &Mission) -> Assessment {
    let status = if metrics.damage_j > config.impact_threshold_j {
        MissionStatus::FailedDamage
    } else if metrics.distance_m <= config.arrival_radius_m {
        MissionStatus::Success
    } else {
        MissionStatus::Running
    };
    let phase = match status {
        MissionStatus::Success => MissionPhase::Arrived,
        MissionStatus::FailedDamage => MissionPhase::Failed,
        MissionStatus::Running if metrics.lander_position_m[0] <= metrics.transit_x_m => {
            MissionPhase::Transit
        }
        MissionStatus::Running
            if distance(metrics.lander_position_m, metrics.pad_position_m)
                > metrics.on_pad_radius_m =>
        {
            MissionPhase::Ascending
        }
        MissionStatus::Running => MissionPhase::OnPad,
    };
    let score = match status {
        MissionStatus::Running => None,
        MissionStatus::Success => Some(
            (config.nominal_time_s / metrics.time_s.max(metrics.minimum_step_s)
                - 2.0 * metrics.damage_j / config.impact_threshold_j
                - (mission.fuel_used_kg / config.nominal_fuel_kg - 1.0).max(0.0)
                + config.score_offset)
                * 100.0,
        ),
        MissionStatus::FailedDamage => Some(
            (-metrics.distance_m / mission.initial_distance_m.max(metrics.minimum_step_s)
                - 2.0 * metrics.damage_j / config.impact_threshold_j
                - (mission.fuel_used_kg / config.nominal_fuel_kg - 1.0).max(0.0)
                + config.score_offset)
                * 100.0,
        ),
    };
    Assessment {
        status,
        phase,
        score,
    }
}

fn distance(a: [f64; 3], b: [f64; 3]) -> f64 {
    (a[0] - b[0]).hypot(a[1] - b[1]).hypot(a[2] - b[2])
}
