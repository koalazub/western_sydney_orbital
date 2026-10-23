use super::{
    input::{self, InputFailure, PlanningFrame},
    model::{self, PlanStatus, Report},
    search::{Search, SearchFailure, SearchProgress, SearchSettings},
};
use crate::{EngineSet, camera::CameraRig, model::Snapshot, scenario::settings};
use bevy::{math::DVec3, prelude::*};
use std::sync::Arc;

#[derive(Resource)]
struct NavigationState {
    body_radius_m: f64,
    epoch: Option<(u64, f64)>,
    signature: Option<u64>,
    frame: Option<PlanningFrame>,
    search: Option<Search>,
    route: Option<Arc<Vec<DVec3>>>,
    displayed: Vec<DVec3>,
    last_search_s: Option<f64>,
    status: PlanStatus,
    failure: Option<SearchFailure>,
    detail: String,
    expanded: usize,
}

impl Default for NavigationState {
    fn default() -> Self {
        // Enclose the same versioned terrain mesh used by rendering/contacts.
        // This conservative collision bound is not a new gravity model.
        let body_radius_m = crate::model::phobos_geometry()
            .0
            .into_iter()
            .map(|point| DVec3::from_array(point).length())
            .fold(0.0, f64::max);
        Self {
            body_radius_m,
            epoch: None,
            signature: None,
            frame: None,
            search: None,
            route: None,
            displayed: Vec::new(),
            last_search_s: None,
            status: PlanStatus::WaitingForRadar,
            failure: None,
            detail: "Waiting for measurements".into(),
            expanded: 0,
        }
    }
}

pub(crate) fn install(app: &mut App) {
    app.init_resource::<NavigationState>()
        .add_systems(Update, update.in_set(EngineSet::Planning))
        .add_systems(Update, draw.in_set(EngineSet::Scene));
}

fn update(mut navigation: ResMut<NavigationState>) {
    let snapshot = crate::snapshot();
    advance(&mut navigation, &snapshot);
}

/// Planning changes only its own cache and report. The engine adapter above
/// supplies an immutable snapshot and has no vehicle-command output.
fn advance(state: &mut NavigationState, snapshot: &Snapshot) {
    let config = settings().path_planning;
    let previous_time = state.epoch.map(|(_, time)| time);
    let reset = state
        .epoch
        .is_none_or(|(seed, time)| seed != snapshot.seed || snapshot.time_s < time);
    if reset {
        state.signature = None;
        state.frame = None;
        state.search = None;
        state.route = None;
        state.displayed.clear();
        state.last_search_s = None;
        state.failure = None;
        state.expanded = 0;
    }
    state.epoch = Some((snapshot.seed, snapshot.time_s));
    let signature = input::signature(snapshot);
    let changed = state.signature != Some(signature);
    let mut dirty = changed || reset || state.search.is_some();
    if changed {
        // A paused restore can change permitted inputs without advancing time.
        // Do not wait for a simulation-time replan timer or finish the old job.
        if !reset && previous_time == Some(snapshot.time_s) {
            state.search = None;
            state.last_search_s = None;
        }
        state.signature = Some(signature);
        match input::prepare(snapshot, config, state.body_radius_m) {
            Ok(frame) => {
                if frame.at_waypoint {
                    state.search = None;
                    state.route = None;
                    state.displayed.clear();
                    state.failure = None;
                    state.status = PlanStatus::AtWaypoint;
                    state.detail =
                        "Inside the waypoint arrival region; no flight command is issued".into();
                } else if let Some(route) = &state.route {
                    if let Some(points) = input::reconnect(&frame, route) {
                        state.displayed = points;
                    } else {
                        state.route = None;
                        state.displayed.clear();
                        state.last_search_s = None;
                        state.status = PlanStatus::Planning;
                        state.detail =
                            "Previous candidate invalidated by newer measurements".into();
                    }
                }
                state.frame = Some(frame);
            }
            Err(error) => {
                state.frame = None;
                state.search = None;
                state.route = None;
                state.displayed.clear();
                state.last_search_s = None;
                state.failure = None;
                state.status = match error {
                    InputFailure::NoRadar | InputFailure::StaleRadar => PlanStatus::WaitingForRadar,
                    InputFailure::MissingVehicleOrWaypoint
                    | InputFailure::InvalidState
                    | InputFailure::InvalidRadar
                    | InputFailure::InsideTerrain => PlanStatus::InvalidInput,
                    InputFailure::DepartureBlocked
                    | InputFailure::GoalRegionTooTight
                    | InputFailure::NoClearGoalApproach => PlanStatus::NoCandidate,
                };
                state.detail = error.to_string();
            }
        }
    }
    if let Some(frame) = &state.frame
        && !frame.at_waypoint
        && state.search.is_none()
        && state
            .last_search_s
            .is_none_or(|last| frame.time_s - last >= config.replan_interval_s)
    {
        dirty = true;
        state.last_search_s = Some(frame.time_s);
        state.failure = None;
        state.expanded = 0;
        match Search::new(
            frame.start,
            frame.goal,
            frame.obstacles.clone(),
            SearchSettings {
                cell_size_m: config.cell_size_m,
                corridor_half_width_m: config.corridor_half_width_m,
                max_nodes: config.max_nodes,
                max_expansions: config.max_expansions,
            },
        ) {
            Ok(search) => {
                state.search = Some(search);
                if state.route.is_none() {
                    state.status = PlanStatus::Planning;
                }
                state.detail = "Searching an inflated 3D corridor; radar-only candidate, not a control command".into();
            }
            Err(error) => record_search_failure(state, error),
        }
    }
    if let Some(search) = &mut state.search {
        dirty = true;
        let progress = search.advance(config.expansions_per_frame);
        state.expanded = search.expanded();
        match progress {
            SearchProgress::Searching => {}
            SearchProgress::Found(route) => {
                state.search = None;
                if let Some(frame) = &state.frame {
                    if let Some(points) = input::reconnect(frame, &route) {
                        state.route = Some(route);
                        state.displayed = points;
                        state.status = PlanStatus::Candidate;
                        state.detail = "Candidate checked against current radar envelopes; unobserved rocks remain unknown".into();
                    } else {
                        state.route = None;
                        state.displayed.clear();
                        state.last_search_s = None;
                        state.status = PlanStatus::Planning;
                        state.detail =
                            "Completed search was stale; a new search is required".into();
                    }
                }
            }
            SearchProgress::Failed(error) => {
                state.search = None;
                record_search_failure(state, error);
            }
        }
    }
    if dirty {
        let frame = state.frame.as_ref();
        let times: Vec<_> = snapshot
            .observations
            .iter()
            .map(|m| m.measurement_time_s)
            .filter(|time| time.is_finite() && *time >= 0.0)
            .collect();
        let oldest = times.iter().copied().reduce(f64::min);
        let newest = times.iter().copied().reduce(f64::max);
        model::publish(Report {
            status: state.status,
            search_failure: state.failure,
            detail: state.detail.clone(),
            source_time_s: snapshot.time_s,
            oldest_measurement_time_s: frame.and_then(|f| f.oldest_measurement_s).or(oldest),
            newest_measurement_time_s: frame.and_then(|f| f.newest_measurement_s).or(newest),
            max_measurement_age_s: frame.map_or_else(
                || oldest.map_or(0.0, |t| (snapshot.time_s - t).max(0.0)),
                |f| f.max_age_s,
            ),
            measured_obstacles: frame.map_or(0, |f| f.measured),
            unmodelled_obstacles: frame.map_or(settings().debris.count, |f| f.unmodelled),
            forecast_horizon_s: config.forecast_horizon_s,
            assumed_tangential_speed_allowance_mps: config.unknown_tangential_speed_mps,
            expanded_nodes: state.expanded,
            replanning: state.search.is_some(),
            path_length_m: state
                .displayed
                .windows(2)
                .map(|p| p[0].distance(p[1]))
                .sum(),
            points_world_m: state
                .displayed
                .iter()
                .map(|point| point.to_array())
                .collect(),
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn actual_default_world_produces_a_candidate_without_changing_simulation_state() {
        let simulation = crate::physics::Simulation::new_default();
        let snapshot = simulation.snapshot();
        let before = serde_json::to_string(&snapshot).unwrap();
        let mut preview = NavigationState::default();
        for _ in 0..1024 {
            advance(&mut preview, &snapshot);
            if matches!(
                preview.status,
                PlanStatus::Candidate | PlanStatus::NoCandidate | PlanStatus::InvalidInput
            ) {
                break;
            }
        }
        assert!(
            matches!(preview.status, PlanStatus::Candidate),
            "{}",
            preview.detail
        );
        assert!(preview.displayed.len() >= 2);
        let ship = snapshot
            .assets
            .iter()
            .find(|actor| actor.id == snapshot.mission.shuttle_id)
            .unwrap();
        assert_eq!(preview.displayed[0], DVec3::from_array(ship.position));
        assert!(
            input::reconnect(
                preview.frame.as_ref().unwrap(),
                preview.route.as_ref().unwrap()
            )
            .is_some()
        );
        let report: serde_json::Value = serde_json::from_str(&model::status()).unwrap();
        assert_eq!(report["advisory_only"], true);
        assert_eq!(report["plan"]["status"], "candidate");
        assert_eq!(
            report["plan"]["measured_obstacles"],
            settings().debris.radar_count
        );
        assert_eq!(
            report["plan"]["points_world_m"].as_array().unwrap().len(),
            preview.displayed.len()
        );
        assert_eq!(
            before,
            serde_json::to_string(&simulation.snapshot()).unwrap()
        );
    }

    #[test]
    fn repeated_old_readings_remove_the_candidate_instead_of_becoming_new_measurements() {
        let simulation = crate::physics::Simulation::new_default();
        let mut snapshot = simulation.snapshot();
        let mut preview = NavigationState::default();
        advance(&mut preview, &snapshot);
        snapshot.time_s = settings().path_planning.max_measurement_age_s + 0.01;
        advance(&mut preview, &snapshot);
        assert!(matches!(preview.status, PlanStatus::WaitingForRadar));
        assert!(preview.displayed.is_empty());
        assert!(preview.search.is_none());
        assert!(preview.frame.is_none());
        assert!(preview.detail.contains("freshness"));
    }

    #[test]
    fn real_physics_refreshes_replan_without_planner_side_effects() {
        let mut simulation = crate::physics::Simulation::new_default();
        let mut preview = NavigationState::default();
        let mut candidate_frames = 0;
        for _ in 0..40 {
            simulation.advance(settings().integration.fixed_step_s);
            let snapshot = simulation.snapshot();
            let before = serde_json::to_string(&snapshot).unwrap();
            advance(&mut preview, &snapshot);
            assert_eq!(
                before,
                serde_json::to_string(&simulation.snapshot()).unwrap()
            );
            if matches!(preview.status, PlanStatus::Candidate) {
                candidate_frames += 1;
                let frame = preview.frame.as_ref().unwrap();
                assert_eq!(frame.time_s, snapshot.time_s);
                assert!(frame.max_age_s < settings().path_planning.max_measurement_age_s);
                assert!(input::reconnect(frame, preview.route.as_ref().unwrap()).is_some());
            }
        }
        assert!(candidate_frames > 0, "{}", preview.detail);
        assert!(preview.last_search_s.unwrap() >= settings().path_planning.replan_interval_s);
    }
}

fn record_search_failure(state: &mut NavigationState, error: SearchFailure) {
    state.failure = Some(error);
    state.status = if state.route.is_some() {
        PlanStatus::Candidate
    } else {
        PlanStatus::NoCandidate
    };
    state.detail = if state.route.is_some() {
        format!(
            "New search stopped: {error}. Showing the previously validated candidate; rechecks as the simulation advances."
        )
    } else {
        format!(
            "{error}. Rechecks as the simulation advances. A route may exist outside these search limits."
        )
    };
}

fn draw(state: Res<NavigationState>, rig: Res<CameraRig>, mut gizmos: Gizmos) {
    // Sensor setup assigns default gizmos to debug layer 1. Mounted cameras
    // see layers 0/2 only, even while this overlay is drawn in a debug view.
    if rig.mode.sensor().is_some() {
        return;
    }
    if state.displayed.len() >= 2 {
        let points = state
            .displayed
            .iter()
            .map(|point| (*point * 0.001).as_vec3());
        gizmos.linestrip(points, Color::srgb(1.0, 0.72, 0.18));
    }
    if let Some(frame) = &state.frame {
        gizmos.sphere(
            Isometry3d::from_translation((frame.goal_center * 0.001).as_vec3()),
            (frame.goal_radius_m * 0.001) as f32,
            Color::srgb(0.12, 0.75, 0.95),
        );
    }
}
