use crate::scenario::settings;
use serde::{Deserialize, Deserializer, Serialize};
use std::cell::RefCell;

pub(super) const MAX_JSON_BYTES: usize = 1024 * 1024;
pub(super) const MAX_RENDER_POINTS: usize = 4096;
const MAX_DETAIL_CHARS: usize = 512;
const TIME_EPSILON_S: f64 = 1e-6;

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum PlanStatus {
    Candidate,
    NoCandidate,
    Planning,
    WaitingForRadar,
    InvalidInput,
    AtWaypoint,
}

/// Notebook data only. Validation below concerns the transport and renderer,
/// not obstacle coverage, collision clearance, or flight feasibility.
#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Preview {
    pub source_seed: u32,
    pub source_time_s: f64,
    pub status: PlanStatus,
    pub detail: String,
    pub points_world_m: Vec<[f64; 3]>,
    pub goal_center_m: [f64; 3],
    pub goal_radius_m: f64,
    pub measured_obstacles: u32,
    pub unmodelled_obstacles_lower_bound: u32,
    pub forecast_horizon_s: f64,
    pub max_measurement_age_s: f64,
    #[serde(deserialize_with = "measurement_time")]
    pub oldest_measurement_time_s: Option<f64>,
    #[serde(deserialize_with = "measurement_time")]
    pub newest_measurement_time_s: Option<f64>,
    pub assumed_tangential_speed_allowance_mps: f64,
    pub expanded_nodes: u32,
}

// Require both fields in JSON, while allowing an explicit null for unknown times.
fn measurement_time<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Option<f64>, D::Error> {
    Option::deserialize(deserializer)
}

impl Preview {
    fn validate(&self, seed: u64, time_s: f64) -> Result<(), String> {
        if u64::from(self.source_seed) != seed {
            return Err("source_seed must match the actual simulation seed".into());
        }
        for (name, value) in [
            ("source_time_s", self.source_time_s),
            ("forecast_horizon_s", self.forecast_horizon_s),
            ("max_measurement_age_s", self.max_measurement_age_s),
            (
                "assumed_tangential_speed_allowance_mps",
                self.assumed_tangential_speed_allowance_mps,
            ),
        ] {
            if !value.is_finite() || value < 0.0 {
                return Err(format!("{name} must be finite and nonnegative"));
            }
        }
        if !time_s.is_finite() || time_s < 0.0 || self.source_time_s > time_s + TIME_EPSILON_S {
            return Err("source_time_s cannot be later than the actual simulation time".into());
        }
        if self.detail.chars().count() > MAX_DETAIL_CHARS {
            return Err("detail exceeds the 512-character limit".into());
        }
        if self.points_world_m.len() > MAX_RENDER_POINTS {
            return Err("points_world_m exceeds the 4096-point limit".into());
        }
        if self.status == PlanStatus::Candidate && self.points_world_m.len() < 2 {
            return Err("a candidate preview requires at least two points".into());
        }
        let limits = settings().limits;
        if !self
            .points_world_m
            .iter()
            .chain(std::iter::once(&self.goal_center_m))
            .flatten()
            .all(|value| value.is_finite() && value.abs() <= limits.position_m)
        {
            return Err(
                "preview positions must be finite and within scenario position limits".into(),
            );
        }
        if !self.goal_radius_m.is_finite()
            || self.goal_radius_m <= 0.0
            || self.goal_radius_m > limits.radius_m
        {
            return Err(
                "goal_radius_m must be positive and within the scenario radius limit".into(),
            );
        }
        for time in [
            self.oldest_measurement_time_s,
            self.newest_measurement_time_s,
        ]
        .into_iter()
        .flatten()
        {
            if !time.is_finite() || time < 0.0 || time > self.source_time_s + TIME_EPSILON_S {
                return Err(
                    "measurement times must be finite, nonnegative and no later than source_time_s"
                        .into(),
                );
            }
        }
        if let (Some(oldest), Some(newest)) = (
            self.oldest_measurement_time_s,
            self.newest_measurement_time_s,
        ) && oldest > newest
        {
            return Err("oldest_measurement_time_s cannot exceed newest_measurement_time_s".into());
        }
        Ok(())
    }

    pub(super) fn render_points(&self) -> &[[f64; 3]] {
        if self.status == PlanStatus::Candidate {
            &self.points_world_m
        } else {
            &[]
        }
    }
}

#[derive(Default)]
struct Report {
    epoch: Option<(u64, f64)>,
    preview: Option<Preview>,
}

thread_local! { static REPORT: RefCell<Report> = RefCell::new(Report::default()); }

pub(crate) fn submit(json: &str) -> Result<(), String> {
    if json.len() > MAX_JSON_BYTES {
        return Err("Navigation preview exceeds the 1 MiB limit".into());
    }
    let preview: Option<Preview> = serde_json::from_str(json).map_err(|error| error.to_string())?;
    let snapshot = crate::snapshot();
    if let Some(preview) = &preview {
        preview.validate(snapshot.seed, snapshot.time_s)?;
    }
    // Do not change even the publication epoch until every field has passed.
    REPORT.with(|state| {
        *state.borrow_mut() = Report {
            epoch: Some((snapshot.seed, snapshot.time_s)),
            preview,
        };
    });
    Ok(())
}

pub(super) fn clear_on_reset() {
    let snapshot = crate::snapshot();
    REPORT.with(|state| {
        let mut state = state.borrow_mut();
        if state
            .epoch
            .is_some_and(|(seed, time)| seed != snapshot.seed || snapshot.time_s < time)
        {
            state.preview = None;
        }
        state.epoch = Some((snapshot.seed, snapshot.time_s));
    });
}

pub(super) fn with_preview<T>(read: impl FnOnce(Option<&Preview>) -> T) -> T {
    // Exports and HUD reads can happen after a reset but before the next Update.
    clear_on_reset();
    REPORT.with(|state| read(state.borrow().preview.as_ref()))
}

fn waiting_plan() -> serde_json::Value {
    serde_json::json!({
        "status": "waiting_for_notebook",
        "detail": "Waiting for notebook",
        "source_seed": null,
        "source_time_s": null,
        "points_world_m": [],
        "goal_center_m": null,
        "goal_radius_m": null,
        "measured_obstacles": null,
        "unmodelled_obstacles_lower_bound": null,
        "forecast_horizon_s": null,
        "max_measurement_age_s": null,
        "oldest_measurement_time_s": null,
        "newest_measurement_time_s": null,
        "assumed_tangential_speed_allowance_mps": null,
        "expanded_nodes": null,
    })
}

pub(crate) fn status() -> String {
    with_preview(|preview| {
        let plan = preview.map_or_else(waiting_plan, |preview| {
            serde_json::to_value(preview).expect("validated preview fields are finite")
        });
        serde_json::json!({
            "plan": plan,
            "algorithm_owner": "python_notebook",
            "advisory_only": true,
            "input": "python_notebook_preview",
            "frame": settings().frame_description,
            "validation": "transport_and_rendering_only_not_route_or_collision_validation",
            "publication_policy": "Held until the notebook replaces it or the actual simulation seed changes or time rewinds. No autonomous replanning.",
            "coverage": "Notebook-reported metadata only. Missing radar is not assumed clear space.",
            "limitations": "No route safety or flight-feasibility guarantee. Rust does not predict obstacles, select waypoints or issue flight commands.",
        })
        .to_string()
    })
}

pub(crate) fn brief() -> String {
    with_preview(|preview| {
        let label = match preview.map(|preview| preview.status) {
            None => "Waiting for notebook",
            Some(PlanStatus::Candidate) => "candidate",
            Some(PlanStatus::NoCandidate) => "no candidate",
            Some(PlanStatus::Planning) => "notebook planning",
            Some(PlanStatus::WaitingForRadar) => "waiting for radar",
            Some(PlanStatus::InvalidInput) => "invalid input",
            Some(PlanStatus::AtWaypoint) => "at waypoint",
        };
        format!("Python path preview | {label}")
    })
}

pub(crate) fn details() -> String {
    with_preview(|preview| {
        let Some(preview) = preview else {
            return "Python path preview\nWaiting for notebook\nPython owns planning and scheduling. Rust only validates and draws submitted previews.\nNo route or obstacle coverage has been supplied. No flight commands are issued.".into();
        };
        format!(
            "Python path preview | advisory only\n{}\nNotebook source seed {} at {:.2} s\nNotebook reports {} measured obstacles; at least {} unmodelled\nReported measurement age up to {:.2} s | forecast {:.1} s\nAssumed sideways allowance {:.1} m/s\n{} expanded nodes | {} submitted points\nAmber line: notebook candidate. Cyan sphere: notebook goal.\nTransport/render validation only, not collision or flight-safety proof. No flight commands are issued.",
            preview.detail,
            preview.source_seed,
            preview.source_time_s,
            preview.measured_obstacles,
            preview.unmodelled_obstacles_lower_bound,
            preview.max_measurement_age_s,
            preview.forecast_horizon_s,
            preview.assumed_tangential_speed_allowance_mps,
            preview.expanded_nodes,
            preview.points_world_m.len(),
        )
    })
}
