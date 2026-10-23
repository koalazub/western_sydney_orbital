use super::search::SearchFailure;
use serde::Serialize;
use std::cell::RefCell;

#[derive(Clone, Copy, Debug, Default, Serialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum PlanStatus {
    #[default]
    WaitingForRadar,
    Planning,
    Candidate,
    AtWaypoint,
    NoCandidate,
    InvalidInput,
}

#[derive(Clone, Debug, Default, Serialize)]
pub(super) struct Report {
    pub status: PlanStatus,
    pub search_failure: Option<SearchFailure>,
    pub detail: String,
    pub source_time_s: f64,
    pub oldest_measurement_time_s: Option<f64>,
    pub newest_measurement_time_s: Option<f64>,
    pub max_measurement_age_s: f64,
    pub measured_obstacles: usize,
    #[serde(rename = "unmodelled_obstacles_lower_bound")]
    pub unmodelled_obstacles: usize,
    pub forecast_horizon_s: f64,
    pub assumed_tangential_speed_allowance_mps: f64,
    pub expanded_nodes: usize,
    pub replanning: bool,
    pub path_length_m: f64,
    pub points_world_m: Vec<[f64; 3]>,
}

thread_local! { static REPORT: RefCell<Report> = RefCell::new(Report::default()); }

pub(super) fn publish(report: Report) {
    REPORT.with(|state| *state.borrow_mut() = report);
}

pub(crate) fn status() -> String {
    REPORT.with(|state| serde_json::json!({
        "plan": &*state.borrow(),
        "advisory_only": true,
        "input": "public_radar_fields_plus_own_state_waypoint_and_known_bodies",
        "motion_estimate": "range_rate_projection_not_full_velocity",
        "observer_backdating": "Constant current velocity with a thrust-sized uncertainty allowance, not a bound on gravity or contacts.",
        "uncertainty_limits": "The sideways speed allowance and sigma inflation are planning assumptions. Asteroid acceleration and Gaussian tails are not guaranteed bounded.",
        "obstacle_model": "uncertainty_inflated_swept_capsules",
        "frame": crate::scenario::settings().frame_description,
        "coverage": "radar-limited; camera-only obstacles are not yet modelled",
        "forecast_scope": "Obstacle envelopes predict over the reported horizon, not the route travel time.",
        "surface_departure": "A landed start uses a checked outward radial prefix to leave the enclosing Phobos sphere; surface contact clearance is not a flight-feasibility proof.",
        "limitations": "Geometric candidate, not a flight-feasible or globally safe trajectory. No thrust, speed or ship attributes are set."
    }).to_string())
}

pub(crate) fn brief() -> String {
    REPORT.with(|state| match state.borrow().status {
        PlanStatus::WaitingForRadar => "Path preview | waiting for fresh radar".into(),
        PlanStatus::Planning => "Path preview | searching | radar only".into(),
        PlanStatus::Candidate => "Path preview | candidate | radar only".into(),
        PlanStatus::AtWaypoint => "Path preview | inside waypoint region".into(),
        PlanStatus::NoCandidate => "Path preview | no candidate found".into(),
        PlanStatus::InvalidInput => "Path preview | input unavailable".into(),
    })
}

pub(crate) fn details() -> String {
    REPORT.with(|state| {
        let r = state.borrow();
        format!(
            "Path preview | no flight control\n{}\n{} radar rows modelled; at least {} rocks unmodelled\nRadar age up to {:.2} s | forecast {:.1} s, not travel time\nRadial motion only; sideways allowance {:.1} m/s\n{} search cells | {} points | {:.0} m\nAmber line: candidate. Cyan sphere: waypoint region.\nUnobserved rocks and flight dynamics prevent a safety guarantee. The example controller does not follow this path.",
            r.detail, r.measured_obstacles, r.unmodelled_obstacles,
            r.max_measurement_age_s, r.forecast_horizon_s,
            r.assumed_tangential_speed_allowance_mps, r.expanded_nodes,
            r.points_world_m.len(), r.path_length_m,
        )
    })
}
