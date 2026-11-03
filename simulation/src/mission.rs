use crate::{
    domain::{
        MissionPhase, MissionScoring, MissionStatus, ScoreBreakdown, ScoreFormula,
        ScoreProgressBasis,
    },
    model::MissionState,
    scenario::Mission,
};

#[derive(Clone, Copy, Debug)]
pub struct FlightMetrics {
    pub distance_m: f64,
    pub lander_position_m: [f64; 3],
    pub pad_position_m: [f64; 3],
    #[allow(
        dead_code,
        reason = "Retained for the existing flight-metrics adapter."
    )]
    pub time_s: f64,
    pub damage_j: f64,
    #[allow(
        dead_code,
        reason = "Retained for the existing flight-metrics adapter."
    )]
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
    let (score, score_breakdown) = match (mission.scoring, status) {
        (MissionScoring::OfficialTimeDamageFuelV1, MissionStatus::Success) => {
            let breakdown = official_breakdown(
                metrics.time_s,
                metrics.damage_j,
                mission.fuel_used_kg,
                ScoreProgressBasis::TimeRatio,
                config,
            );
            (Some(breakdown.total), Some(breakdown))
        }
        (MissionScoring::OfficialTimeDamageFuelV1, MissionStatus::FailedDamage) => {
            let breakdown = official_loss_breakdown(
                metrics.distance_m,
                mission.initial_distance_m,
                metrics.damage_j,
                mission.fuel_used_kg,
                config,
            );
            (Some(breakdown.total), Some(breakdown))
        }
        // The local actuator and contact models differ from STP's jet allocator
        // and hit counters. Only the competition preset emits a native estimate.
        _ => (None, None),
    };
    Assessment {
        status,
        phase,
        score,
        score_breakdown,
    }
}

/// Scoring.py `TimeScore`: nominal / elapsed, or zero at the initial instant.
pub fn time_score(time_s: f64, nominal_time_s: f64) -> f64 {
    if time_s != 0.0 {
        nominal_time_s / time_s
    } else {
        0.0
    }
}

/// Scoring.py `DamagePenalty`: two times the accumulated damage ratio.
pub fn damage_penalty(damage_j: f64, maximum_damage_j: f64) -> f64 {
    2.0 * damage_j / maximum_damage_j
}

/// Current Scoring.py `FuelPenalty`: nonnegative fuel ratio. Both current models
/// estimate propellant mass, but STP sums individual jet impulses while this
/// engine integrates its local actuator model.
pub fn fuel_penalty(fuel_used_kg: f64, nominal_fuel_kg: f64) -> f64 {
    (fuel_used_kg / nominal_fuel_kg).max(0.0)
}

/// Scoring.py `FullScoreWin`, using the scenario's remap offset (0.5 -> 50 points).
#[cfg(test)]
pub fn official_win_score(time_s: f64, damage_j: f64, fuel_used_kg: f64, config: &Mission) -> f64 {
    let raw = time_score(time_s, config.nominal_time_s)
        - damage_penalty(damage_j, config.impact_threshold_j)
        - fuel_penalty(fuel_used_kg, config.nominal_fuel_kg);
    remap_unclamped(raw, -config.score_offset, config.score_offset, 0.0, 100.0)
}

/// Scoring.py `FullScoreLoss`, using the local current/initial range ratio.
#[cfg(test)]
pub fn official_loss_score(
    distance_m: f64,
    initial_distance_m: f64,
    damage_j: f64,
    fuel_used_kg: f64,
    config: &Mission,
) -> f64 {
    let progress = if initial_distance_m > 0.0 {
        distance_m / initial_distance_m
    } else {
        0.0
    };
    let raw = -progress
        - damage_penalty(damage_j, config.impact_threshold_j)
        - fuel_penalty(fuel_used_kg, config.nominal_fuel_kg);
    remap_unclamped(raw, -config.score_offset, config.score_offset, 0.0, 100.0)
}

fn official_breakdown(
    time_s: f64,
    damage_j: f64,
    fuel_used_kg: f64,
    progress_basis: ScoreProgressBasis,
    config: &Mission,
) -> ScoreBreakdown {
    let progress = time_score(time_s, config.nominal_time_s);
    let damage = damage_penalty(damage_j, config.impact_threshold_j);
    let fuel = fuel_penalty(fuel_used_kg, config.nominal_fuel_kg);
    let progress_points = 100.0 * progress;
    let collision_penalty_points = 100.0 * damage;
    let fuel_penalty_points = 100.0 * fuel;
    let offset_points = 100.0 * config.score_offset;
    ScoreBreakdown {
        formula: ScoreFormula::OfficialTimeDamageFuelV1,
        progress_basis,
        progress_points,
        collision_penalty_points,
        fuel_penalty_points,
        offset_points,
        total: progress_points - collision_penalty_points - fuel_penalty_points + offset_points,
        nominal_time_s: config.nominal_time_s,
        nominal_fuel_kg: config.nominal_fuel_kg,
        impact_threshold_j: config.impact_threshold_j,
    }
}

fn official_loss_breakdown(
    distance_m: f64,
    initial_distance_m: f64,
    damage_j: f64,
    fuel_used_kg: f64,
    config: &Mission,
) -> ScoreBreakdown {
    let progress = if initial_distance_m > 0.0 {
        -distance_m / initial_distance_m
    } else {
        0.0
    };
    let damage = damage_penalty(damage_j, config.impact_threshold_j);
    let fuel = fuel_penalty(fuel_used_kg, config.nominal_fuel_kg);
    let progress_points = 100.0 * progress;
    let collision_penalty_points = 100.0 * damage;
    let fuel_penalty_points = 100.0 * fuel;
    let offset_points = 100.0 * config.score_offset;
    ScoreBreakdown {
        formula: ScoreFormula::OfficialTimeDamageFuelV1,
        progress_basis: ScoreProgressBasis::RemainingDistanceRatio,
        progress_points,
        collision_penalty_points,
        fuel_penalty_points,
        offset_points,
        total: progress_points - collision_penalty_points - fuel_penalty_points + offset_points,
        nominal_time_s: config.nominal_time_s,
        nominal_fuel_kg: config.nominal_fuel_kg,
        impact_threshold_j: config.impact_threshold_j,
    }
}

#[cfg(test)]
fn remap_unclamped(value: f64, in_min: f64, in_max: f64, out_min: f64, out_max: f64) -> f64 {
    out_min + (out_max - out_min) * (value - in_min) / (in_max - in_min)
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
    use crate::domain::ScoreFormula;

    // Fixed legacy local SI diagnostics, independent of the current assessor.
    const LEGACY_SUCCESS: &str = r#"{
        "shuttle_id": "Shuttle",
        "target_id": "Target",
        "target_distance_m": 49.0,
        "initial_distance_m": 10000.0,
        "relative_speed_mps": 17.0,
        "fuel_used_kg": 2.3,
        "damage": 104600.0,
        "status": "success",
        "score": -50.0,
        "score_label": "Estimated practice score",
        "score_breakdown": {
            "formula": "practice_normalized_time_damage_fuel_v1",
            "progress_basis": "time_ratio",
            "progress_points": 100.0,
            "collision_penalty_points": 100.0,
            "fuel_penalty_points": 100.0,
            "offset_points": 50.0,
            "total": -50.0,
            "nominal_time_s": 120.0,
            "nominal_fuel_kg": 1.15,
            "impact_threshold_j": 209200.0
        },
        "phase": "arrived",
        "target_region": "L2",
        "flight_analytics": null
    }"#;

    const LEGACY_FAILURE: &str = r#"{
        "shuttle_id": "Shuttle",
        "target_id": "Target",
        "target_distance_m": 5000.0,
        "initial_distance_m": 10000.0,
        "relative_speed_mps": 0.0,
        "fuel_used_kg": 0.0,
        "damage": 313800.0,
        "status": "failed_damage",
        "score": -300.0,
        "score_label": "Estimated practice score",
        "score_breakdown": {
            "formula": "practice_normalized_time_damage_fuel_v1",
            "progress_basis": "remaining_distance_ratio",
            "progress_points": -50.0,
            "collision_penalty_points": 300.0,
            "fuel_penalty_points": 0.0,
            "offset_points": 50.0,
            "total": -300.0,
            "nominal_time_s": 120.0,
            "nominal_fuel_kg": 1.15,
            "impact_threshold_j": 209200.0
        },
        "phase": "failed",
        "target_region": "L2",
        "flight_analytics": null
    }"#;

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
    fn arrival_and_damage_thresholds_preserve_outcomes_and_failure_precedence() {
        let cfg = &crate::scenario::settings().mission;
        for (damage_j, inside_status, outside_status) in [
            (
                cfg.impact_threshold_j.next_down(),
                MissionStatus::Success,
                MissionStatus::Running,
            ),
            (
                cfg.impact_threshold_j,
                MissionStatus::Success,
                MissionStatus::Running,
            ),
            (
                cfg.impact_threshold_j.next_up(),
                MissionStatus::FailedDamage,
                MissionStatus::FailedDamage,
            ),
        ] {
            for (distance_m, expected_status) in [
                (0.0, inside_status),
                (cfg.arrival_radius_m.next_down(), inside_status),
                (cfg.arrival_radius_m, inside_status),
                (cfg.arrival_radius_m.next_up(), outside_status),
            ] {
                let result = assess(
                    &MissionState::default(),
                    FlightMetrics {
                        distance_m,
                        damage_j,
                        ..metrics()
                    },
                    cfg,
                );
                assert_eq!(result.status, expected_status);
                assert_eq!(
                    result.phase,
                    match expected_status {
                        MissionStatus::Success => MissionPhase::Arrived,
                        MissionStatus::FailedDamage => MissionPhase::Failed,
                        MissionStatus::Running => MissionPhase::Transit,
                    }
                );
                assert!(result.score.is_none() && result.score_breakdown.is_none());
            }
        }
    }

    #[test]
    fn phases_keep_transit_precedence_and_three_dimensional_pad_boundaries() {
        let cfg = &crate::scenario::settings().mission;
        let sample = metrics();
        let transit = sample.transit_x_m;
        let pad_x = sample.pad_position_m[0];
        let radius = sample.on_pad_radius_m;
        for (lander_position_m, running_phase) in [
            ([transit.next_down(), 0.0, 0.0], MissionPhase::Transit),
            ([transit, 0.0, 0.0], MissionPhase::Transit),
            ([transit.next_up(), 0.0, 0.0], MissionPhase::Ascending),
            (sample.pad_position_m, MissionPhase::OnPad),
            ([pad_x, radius.next_down(), 0.0], MissionPhase::OnPad),
            ([pad_x, radius, 0.0], MissionPhase::OnPad),
            ([pad_x, radius.next_up(), 0.0], MissionPhase::Ascending),
            ([pad_x, 0.0, radius], MissionPhase::OnPad),
            ([pad_x, 0.0, radius.next_up()], MissionPhase::Ascending),
            ([pad_x, 3.0, 4.0], MissionPhase::OnPad),
        ] {
            for (distance_m, damage_j, status, phase) in [
                (
                    cfg.arrival_radius_m.next_up(),
                    0.0,
                    MissionStatus::Running,
                    running_phase,
                ),
                (
                    cfg.arrival_radius_m,
                    cfg.impact_threshold_j,
                    MissionStatus::Success,
                    MissionPhase::Arrived,
                ),
                (
                    cfg.arrival_radius_m,
                    cfg.impact_threshold_j.next_up(),
                    MissionStatus::FailedDamage,
                    MissionPhase::Failed,
                ),
            ] {
                let result = assess(
                    &MissionState::default(),
                    FlightMetrics {
                        distance_m,
                        damage_j,
                        lander_position_m,
                        ..sample
                    },
                    cfg,
                );
                assert_eq!(result.status, status);
                assert_eq!(result.phase, phase);
                assert!(result.score.is_none() && result.score_breakdown.is_none());
            }
        }
        let overlapping_pad = assess(
            &MissionState::default(),
            FlightMetrics {
                distance_m: 5000.0,
                lander_position_m: [transit, 0.0, 0.0],
                pad_position_m: [transit, 0.0, 0.0],
                ..sample
            },
            cfg,
        );
        assert_eq!(overlapping_pad.status, MissionStatus::Running);
        assert_eq!(overlapping_pad.phase, MissionPhase::Transit);
        assert!(overlapping_pad.score.is_none() && overlapping_pad.score_breakdown.is_none());
    }

    #[test]
    fn local_outcomes_never_invent_scores_from_si_fuel_counters() {
        let cfg = &crate::scenario::settings().mission;
        for time_s in [0.0, metrics().minimum_step_s, 120.0, 240.0, 600.0] {
            for fuel_used_kg in [
                0.0,
                cfg.nominal_fuel_kg,
                2.0 * cfg.nominal_fuel_kg,
                80.0,
                83.2,
                303.0,
                305.0,
            ] {
                for initial_distance_m in [0.0, 10_000.0] {
                    for distance_m in [
                        0.0,
                        cfg.arrival_radius_m.next_down(),
                        cfg.arrival_radius_m,
                        cfg.arrival_radius_m.next_up(),
                        5000.0,
                    ] {
                        for damage_j in [
                            0.0,
                            cfg.impact_threshold_j * 0.5,
                            cfg.impact_threshold_j,
                            cfg.impact_threshold_j.next_up(),
                            cfg.impact_threshold_j * 1.5,
                        ] {
                            let mission = MissionState {
                                fuel_used_kg,
                                initial_distance_m,
                                relative_speed_mps: 17.0,
                                ..Default::default()
                            };
                            let sample = FlightMetrics {
                                time_s,
                                distance_m,
                                damage_j,
                                ..metrics()
                            };
                            let result = assess(&mission, sample, cfg);
                            assert!(
                                result.score.is_none() && result.score_breakdown.is_none(),
                                "invented score for {mission:?}, {sample:?}: {result:?}"
                            );
                        }
                    }
                }
            }
        }
    }

    #[test]
    fn official_time_damage_fuel_formula_matches_the_source_arithmetic() {
        let cfg = &crate::scenario::settings().mission;
        assert_eq!(cfg.nominal_time_s, 120.0);
        assert_eq!(cfg.nominal_fuel_kg, 75.0);
        assert_eq!(cfg.impact_threshold_j, 209_200.0);
        assert_eq!(cfg.arrival_radius_m, 50.0);
        assert_eq!(time_score(0.0, 120.0), 0.0);
        assert_eq!(time_score(120.0, 120.0), 1.0);
        assert_eq!(damage_penalty(209_200.0, 209_200.0), 2.0);
        assert_eq!(fuel_penalty(37.5, 75.0), 0.5);
        assert!((official_win_score(120.0, 0.0, 75.0, cfg) - 50.0).abs() < 1.0e-9);
        assert!((official_win_score(60.0, 104_600.0, 0.0, cfg) - 150.0).abs() < 1.0e-9);
        assert!((official_win_score(240.0, 0.0, 37.5, cfg) - 50.0).abs() < 1.0e-9);
        assert!((official_loss_score(652.9, 1305.8, 313_800.0, 37.5, cfg) + 350.0).abs() < 1.0e-9);

        let mission = MissionState {
            scoring: MissionScoring::OfficialTimeDamageFuelV1,
            initial_distance_m: 1305.8,
            // The official nominal run spends the nominal 75 kg, so its
            // time/damage/fuel raw score is zero and the mapped score is 50.
            fuel_used_kg: 75.0,
            ..Default::default()
        };
        let metrics = FlightMetrics {
            distance_m: 50.0,
            lander_position_m: [-13_000.0, 0.0, 0.0],
            pad_position_m: [-12_990.0, 0.0, 0.0],
            time_s: 120.0,
            damage_j: 0.0,
            minimum_step_s: 0.05,
            transit_x_m: -13_600.0,
            on_pad_radius_m: 5.0,
        };
        let result = assess(&mission, metrics, cfg);
        assert_eq!(result.status, MissionStatus::Success);
        let breakdown = result.score_breakdown.unwrap();
        assert!((result.score.unwrap() - 50.0).abs() < 1.0e-9);
        assert_eq!(breakdown.formula, ScoreFormula::OfficialTimeDamageFuelV1);
        assert_eq!(breakdown.progress_basis, ScoreProgressBasis::TimeRatio);
        assert!((breakdown.progress_points - 100.0).abs() < 1.0e-9);
        assert!((breakdown.collision_penalty_points).abs() < 1.0e-9);
        assert!((breakdown.fuel_penalty_points - 100.0).abs() < 1.0e-9);
        assert!((breakdown.offset_points - 50.0).abs() < 1.0e-9);
        assert!((breakdown.total - 50.0).abs() < 1.0e-9);
        assert_eq!(
            serde_json::to_value(breakdown.formula).unwrap(),
            serde_json::json!("official_time_damage_fuel_v1")
        );
        assert_eq!(
            serde_json::to_value(MissionScoring::OfficialTimeDamageFuelV1).unwrap(),
            serde_json::json!("official_time_damage_fuel_v1")
        );
    }

    #[test]
    fn official_loss_receipt_preserves_negative_totals_and_archived_modes_stay_unscored() {
        let cfg = &crate::scenario::settings().mission;
        let mission = MissionState {
            scoring: MissionScoring::OfficialTimeDamageFuelV1,
            initial_distance_m: 1305.8,
            fuel_used_kg: 37.5,
            ..Default::default()
        };
        let result = assess(
            &mission,
            FlightMetrics {
                distance_m: 652.9,
                damage_j: 313_800.0,
                ..metrics()
            },
            cfg,
        );
        assert_eq!(result.status, MissionStatus::FailedDamage);
        let breakdown = result.score_breakdown.unwrap();
        assert_eq!(
            breakdown.progress_basis,
            ScoreProgressBasis::RemainingDistanceRatio
        );
        assert!((breakdown.progress_points + 50.0).abs() < 1.0e-9);
        assert!((breakdown.collision_penalty_points - 300.0).abs() < 1.0e-9);
        assert!((breakdown.fuel_penalty_points - 50.0).abs() < 1.0e-9);
        assert!((breakdown.offset_points - 50.0).abs() < 1.0e-9);
        assert!((breakdown.total + 350.0).abs() < 1.0e-9);
        assert!((result.score.unwrap() + 350.0).abs() < 1.0e-9);
        let archived = MissionState {
            initial_distance_m: 1305.8,
            ..Default::default()
        };
        let unscored = assess(&archived, metrics(), cfg);
        assert!(unscored.score.is_none() && unscored.score_breakdown.is_none());
        assert!(MissionScoring::default().is_archived());
    }

    #[test]
    fn legacy_receipts_keep_recorded_numbers_and_are_not_reused_by_assessment() {
        let cfg = &crate::scenario::settings().mission;
        for fixture in [LEGACY_SUCCESS, LEGACY_FAILURE] {
            let archived: serde_json::Value = serde_json::from_str(fixture).unwrap();
            let mission: MissionState = serde_json::from_str(fixture).unwrap();
            let terms = mission.score_breakdown.unwrap();
            assert_eq!(
                terms.formula,
                ScoreFormula::PracticeNormalizedTimeDamageFuelV1
            );
            assert_eq!(mission.score, Some(terms.total));
            assert!(valid_archived_breakdown(&mission));
            assert_eq!(serde_json::to_value(&mission).unwrap(), archived);

            let result = assess(&mission, metrics(), cfg);
            assert!(result.score.is_none() && result.score_breakdown.is_none());
            assert_eq!(serde_json::to_value(&mission).unwrap(), archived);
            let restored: MissionState =
                serde_json::from_str(&serde_json::to_string(&mission).unwrap()).unwrap();
            assert!(valid_archived_breakdown(&restored));
            assert_eq!(serde_json::to_value(restored).unwrap(), archived);
        }
    }

    #[test]
    fn older_scalar_scores_remain_without_an_invented_receipt() {
        for fixture in [LEGACY_SUCCESS, LEGACY_FAILURE] {
            let mut archived: serde_json::Value = serde_json::from_str(fixture).unwrap();
            let score = archived["score"].clone();
            archived.as_object_mut().unwrap().remove("score_breakdown");
            let mission: MissionState = serde_json::from_value(archived.clone()).unwrap();
            assert_eq!(serde_json::to_value(mission.score).unwrap(), score);
            assert!(mission.score_breakdown.is_none());
            assert!(valid_archived_breakdown(&mission));

            archived.as_object_mut().unwrap().remove("score");
            let unscored: MissionState = serde_json::from_value(archived).unwrap();
            assert!(unscored.score.is_none() && unscored.score_breakdown.is_none());
            assert!(valid_archived_breakdown(&unscored));
        }
    }

    #[test]
    fn invalid_legacy_receipts_are_still_rejected() {
        for fixture in [LEGACY_SUCCESS, LEGACY_FAILURE] {
            let original: MissionState = serde_json::from_str(fixture).unwrap();
            for (field, value) in [
                ("collision_penalty_points", -1.0),
                ("fuel_penalty_points", -1.0),
                ("nominal_time_s", 0.0),
                ("nominal_time_s", -1.0),
                ("nominal_fuel_kg", 0.0),
                ("nominal_fuel_kg", -1.0),
                ("impact_threshold_j", 0.0),
                ("impact_threshold_j", -1.0),
                ("total", 1.0),
            ] {
                let mut archived: serde_json::Value = serde_json::from_str(fixture).unwrap();
                archived["score_breakdown"][field] = serde_json::json!(value);
                let invalid: MissionState = serde_json::from_value(archived).unwrap();
                assert!(!valid_archived_breakdown(&invalid), "{field}: {value}");
            }

            type ReceiptField = fn(&mut ScoreBreakdown) -> &mut f64;
            let fields: [ReceiptField; 8] = [
                |terms| &mut terms.progress_points,
                |terms| &mut terms.collision_penalty_points,
                |terms| &mut terms.fuel_penalty_points,
                |terms| &mut terms.offset_points,
                |terms| &mut terms.total,
                |terms| &mut terms.nominal_time_s,
                |terms| &mut terms.nominal_fuel_kg,
                |terms| &mut terms.impact_threshold_j,
            ];
            for field in fields {
                for value in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
                    let mut invalid = original.clone();
                    *field(invalid.score_breakdown.as_mut().unwrap()) = value;
                    assert!(!valid_archived_breakdown(&invalid));
                }
            }
            let mut missing_score = original.clone();
            missing_score.score = None;
            assert!(!valid_archived_breakdown(&missing_score));

            let mut wrong_basis = original.clone();
            let terms = wrong_basis.score_breakdown.as_mut().unwrap();
            terms.progress_basis = match terms.progress_basis {
                ScoreProgressBasis::TimeRatio => ScoreProgressBasis::RemainingDistanceRatio,
                ScoreProgressBasis::RemainingDistanceRatio => ScoreProgressBasis::TimeRatio,
            };
            assert!(!valid_archived_breakdown(&wrong_basis));

            let mut running = original;
            running.status = MissionStatus::Running;
            assert!(!valid_archived_breakdown(&running));
        }
    }

    #[test]
    fn legacy_receipt_deserialization_still_rejects_unknown_fields_and_ids() {
        for (field, value) in [
            ("formula", serde_json::json!("invented_stp_formula")),
            ("progress_basis", serde_json::json!("invented_progress")),
            ("unknown_term", serde_json::json!(1.0)),
        ] {
            let mut archived: serde_json::Value = serde_json::from_str(LEGACY_SUCCESS).unwrap();
            archived["score_breakdown"][field] = value;
            assert!(serde_json::from_value::<MissionState>(archived).is_err());
        }
    }
}
