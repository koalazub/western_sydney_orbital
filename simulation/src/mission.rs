use crate::{
    domain::{MissionPhase, MissionStatus, ScoreBreakdown, ScoreFormula, ScoreProgressBasis},
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
    pub score_breakdown: Option<ScoreBreakdown>,
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
    let progress = match status {
        MissionStatus::Running => None,
        MissionStatus::Success => Some((
            if metrics.time_s == 0.0 {
                0.0
            } else {
                config.nominal_time_s / metrics.time_s
            },
            ScoreProgressBasis::TimeRatio,
        )),
        MissionStatus::FailedDamage => Some((
            -metrics.distance_m / mission.initial_distance_m.max(metrics.minimum_step_s),
            ScoreProgressBasis::RemainingDistanceRatio,
        )),
    };
    // Published Scoring.py success/failure algebra, using practice counters.
    // Keep positive-time operation order. At zero elapsed time the published
    // time bonus is zero, not a minimum-step-derived reward.
    let score_breakdown = progress.map(|(progress, basis)| {
        let collision = 2.0 * metrics.damage_j / config.impact_threshold_j;
        let fuel = (mission.fuel_used_kg / config.nominal_fuel_kg - 1.0).max(0.0);
        let total = (progress - collision - fuel + config.score_offset) * 100.0;
        ScoreBreakdown {
            formula: ScoreFormula::PracticeNormalizedTimeDamageFuelV1,
            progress_basis: basis,
            progress_points: progress * 100.0,
            collision_penalty_points: collision * 100.0,
            fuel_penalty_points: fuel * 100.0,
            offset_points: config.score_offset * 100.0,
            total,
            nominal_time_s: config.nominal_time_s,
            nominal_fuel_kg: config.nominal_fuel_kg,
            impact_threshold_j: config.impact_threshold_j,
        }
    });
    Assessment {
        status,
        phase,
        score: score_breakdown.map(|terms| terms.total),
        score_breakdown,
    }
}

fn distance(a: [f64; 3], b: [f64; 3]) -> f64 {
    (a[0] - b[0]).hypot(a[1] - b[1]).hypot(a[2] - b[2])
}

pub fn valid_archived_breakdown(mission: &MissionState) -> bool {
    let Some(terms) = mission.score_breakdown else {
        // Older archives have a score but no component receipt. Leave it missing.
        return true;
    };
    [
        terms.progress_points,
        terms.collision_penalty_points,
        terms.fuel_penalty_points,
        terms.offset_points,
        terms.total,
        terms.nominal_time_s,
        terms.nominal_fuel_kg,
        terms.impact_threshold_j,
    ]
    .iter()
    .all(|value| value.is_finite())
        && terms.collision_penalty_points >= 0.0
        && terms.fuel_penalty_points >= 0.0
        && terms.nominal_time_s > 0.0
        && terms.nominal_fuel_kg > 0.0
        && terms.impact_threshold_j > 0.0
        && mission.score == Some(terms.total)
        && matches!(
            (mission.status, terms.progress_basis),
            (MissionStatus::Success, ScoreProgressBasis::TimeRatio)
                | (
                    MissionStatus::FailedDamage,
                    ScoreProgressBasis::RemainingDistanceRatio
                )
        )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn metrics() -> FlightMetrics {
        FlightMetrics {
            distance_m: 49.0,
            lander_position_m: [-14_000.0, 0.0, 0.0],
            pad_position_m: [-13_000.0, 0.0, 0.0],
            time_s: 120.0,
            damage_j: 0.0,
            minimum_step_s: 0.05,
            transit_x_m: -13_600.0,
            on_pad_radius_m: 5.0,
        }
    }

    #[test]
    fn successful_score_exposes_completion_speed_damage_and_fuel_terms() {
        let cfg = &crate::scenario::settings().mission;
        let mission = MissionState {
            fuel_used_kg: cfg.nominal_fuel_kg,
            initial_distance_m: 10_000.0,
            ..Default::default()
        };
        let result = assess(&mission, metrics(), cfg);
        let terms = result.score_breakdown.unwrap();
        assert_eq!(result.status, MissionStatus::Success);
        assert_eq!(
            terms.formula,
            ScoreFormula::PracticeNormalizedTimeDamageFuelV1
        );
        assert_eq!(terms.progress_basis, ScoreProgressBasis::TimeRatio);
        assert_eq!(terms.progress_points, 100.0);
        assert_eq!(terms.collision_penalty_points, 0.0);
        assert_eq!(terms.fuel_penalty_points, 0.0);
        assert_eq!(terms.offset_points, 50.0);
        assert_eq!(result.score, Some(150.0));
        let zero_time = assess(
            &mission,
            FlightMetrics {
                time_s: 0.0,
                ..metrics()
            },
            cfg,
        );
        assert_eq!(zero_time.score_breakdown.unwrap().progress_points, 0.0);
        assert_eq!(zero_time.score, Some(50.0));

        let slower = assess(
            &mission,
            FlightMetrics {
                time_s: 240.0,
                ..metrics()
            },
            cfg,
        );
        assert_eq!(slower.score, Some(100.0));
        let damaged = assess(
            &mission,
            FlightMetrics {
                damage_j: cfg.impact_threshold_j / 2.0,
                ..metrics()
            },
            cfg,
        );
        assert_eq!(
            damaged.score_breakdown.unwrap().collision_penalty_points,
            100.0
        );
        assert_eq!(damaged.score, Some(50.0));
        let fuel = assess(
            &MissionState {
                fuel_used_kg: 2.0 * cfg.nominal_fuel_kg,
                ..mission.clone()
            },
            metrics(),
            cfg,
        );
        assert_eq!(fuel.score_breakdown.unwrap().fuel_penalty_points, 100.0);
        assert_eq!(fuel.score, Some(50.0));
        // Arrival velocity is reported separately. The existing speed reward is
        // completion time, not an invented penalty on terminal relative speed.
        let fast_entry = assess(
            &MissionState {
                relative_speed_mps: 17.0,
                ..mission
            },
            metrics(),
            cfg,
        );
        assert_eq!(fast_entry.score, result.score);
    }

    #[test]
    fn uncompleted_flights_are_unscored_and_negative_terminal_scores_stay_negative() {
        let cfg = &crate::scenario::settings().mission;
        let mission = MissionState {
            initial_distance_m: 10_000.0,
            ..Default::default()
        };
        let running = assess(
            &mission,
            FlightMetrics {
                distance_m: 5000.0,
                ..metrics()
            },
            cfg,
        );
        assert_eq!(running.status, MissionStatus::Running);
        assert!(running.score.is_none() && running.score_breakdown.is_none());
        let failed = assess(
            &mission,
            FlightMetrics {
                distance_m: 5000.0,
                damage_j: cfg.impact_threshold_j * 1.5,
                ..metrics()
            },
            cfg,
        );
        assert_eq!(failed.status, MissionStatus::FailedDamage);
        let terms = failed.score_breakdown.unwrap();
        assert_eq!(
            terms.progress_basis,
            ScoreProgressBasis::RemainingDistanceRatio
        );
        assert_eq!(terms.progress_points, -50.0);
        assert_eq!(terms.collision_penalty_points, 300.0);
        assert_eq!(failed.score, Some(-300.0));
    }

    #[test]
    fn breakdown_matches_published_algebra_without_postprocessing_or_score_clamping() {
        let cfg = &crate::scenario::settings().mission;
        for time in [0.0, 120.0, 600.0] {
            for damage in [
                0.0,
                cfg.impact_threshold_j * 0.5,
                cfg.impact_threshold_j * 2.0,
            ] {
                for fuel in [0.0, cfg.nominal_fuel_kg, 80.0, 303.0] {
                    let mission = MissionState {
                        fuel_used_kg: fuel,
                        initial_distance_m: 10_000.0,
                        ..Default::default()
                    };
                    let sample = FlightMetrics {
                        time_s: time,
                        damage_j: damage,
                        ..metrics()
                    };
                    let result = assess(&mission, sample, cfg);
                    let progress = if damage > cfg.impact_threshold_j {
                        -sample.distance_m / mission.initial_distance_m.max(sample.minimum_step_s)
                    } else if time == 0.0 {
                        0.0
                    } else {
                        cfg.nominal_time_s / time
                    };
                    let original = (progress
                        - 2.0 * damage / cfg.impact_threshold_j
                        - (fuel / cfg.nominal_fuel_kg - 1.0).max(0.0)
                        + cfg.score_offset)
                        * 100.0;
                    assert_eq!(result.score.unwrap().to_bits(), original.to_bits());
                    assert_eq!(
                        result.score_breakdown.unwrap().total.to_bits(),
                        original.to_bits()
                    );
                }
            }
        }
    }

    #[test]
    fn legacy_scores_have_no_invented_breakdown_and_invalid_new_receipts_are_rejected() {
        let cfg = &crate::scenario::settings().mission;
        let mut mission = MissionState {
            initial_distance_m: 10_000.0,
            ..Default::default()
        };
        let result = assess(&mission, metrics(), cfg);
        mission.status = result.status;
        mission.score = result.score;
        mission.score_breakdown = result.score_breakdown;
        assert!(valid_archived_breakdown(&mission));
        let mut archived = serde_json::to_value(&mission).unwrap();
        archived.as_object_mut().unwrap().remove("score_breakdown");
        let legacy: MissionState = serde_json::from_value(archived).unwrap();
        assert_eq!(legacy.score, mission.score);
        assert!(legacy.score_breakdown.is_none());
        assert!(valid_archived_breakdown(&legacy));
        mission.score_breakdown.as_mut().unwrap().total += 1.0;
        assert!(!valid_archived_breakdown(&mission));
    }
}
