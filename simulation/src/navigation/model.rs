use crate::{api::CommandError, scenario::settings};
use serde::{Deserialize, Deserializer, Serialize};
use std::cell::RefCell;

pub(super) const MAX_JSON_BYTES: usize = 1024 * 1024;
pub(super) const MAX_RENDER_POINTS: usize = 4096;
const MAX_DETAIL_CHARS: usize = 512;
const TIME_EPSILON_S: f64 = 1e-6;
const HEARTBEAT_TIMEOUT_S: f64 = 6.0;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum GuidanceConnection {
    Waiting,
    PreviewOnly,
    Connected,
    Disconnected,
}

fn connection_at(has_preview: bool, received_at_s: Option<f64>, now_s: f64) -> GuidanceConnection {
    match received_at_s {
        Some(received)
            if received.is_finite()
                && now_s.is_finite()
                && now_s >= received
                && now_s - received <= HEARTBEAT_TIMEOUT_S =>
        {
            GuidanceConnection::Connected
        }
        Some(_) => GuidanceConnection::Disconnected,
        None if has_preview => GuidanceConnection::PreviewOnly,
        None => GuidanceConnection::Waiting,
    }
}

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

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
enum GuidanceState {
    Starting,
    Watching,
    Running,
    Stopping,
    Stopped,
    Error,
}

impl GuidanceState {
    fn label(self) -> &'static str {
        match self {
            Self::Starting => "starting",
            Self::Watching => "preview",
            Self::Running => "running",
            Self::Stopping => "stopping",
            Self::Stopped => "stopped",
            Self::Error => "error",
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
enum PythonController {
    Coast,
    BasicPdRadar,
    PlannedWaypointPd,
}

impl PythonController {
    fn label(self) -> &'static str {
        match self {
            Self::Coast => "coast",
            Self::BasicPdRadar => "PD + radar",
            Self::PlannedWaypointPd => "A* + waypoint PD",
        }
    }
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct PythonExecution {
    state: GuidanceState,
    controller: PythonController,
    source_time_s: f64,
    source_seed: u32,
    next_waypoint_world_m: Option<[f64; 3]>,
    tuning_revision: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    trial_revision: Option<u32>,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    execution: Option<PythonExecution>,
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
        if let Some(execution) = &self.execution
            && (execution.source_seed != self.source_seed
                || !execution.source_time_s.is_finite()
                || execution.source_time_s < 0.0
                || execution.source_time_s > time_s + TIME_EPSILON_S
                || execution.next_waypoint_world_m.is_some_and(|point| {
                    point
                        .iter()
                        .any(|value| !value.is_finite() || value.abs() > limits.position_m)
                }))
        {
            return Err("Python execution metadata needs the current seed, a valid source time and finite world waypoint".into());
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
    raycast: Option<crate::model::LookaheadReport>,
    received_at_s: Option<f64>,
    trial_revision: Option<u32>,
}

thread_local! { static REPORT: RefCell<Report> = RefCell::new(Report::default()); }

pub(crate) fn submit(json: &str) -> Result<(), CommandError> {
    if json.len() > MAX_JSON_BYTES {
        return Err(CommandError::Rejected(
            "Navigation preview exceeds the 1 MiB limit".into(),
        ));
    }
    let preview: Option<Preview> =
        serde_json::from_str(json).map_err(|error| CommandError::Rejected(error.to_string()))?;
    let snapshot = crate::snapshot();
    if let Some(preview) = &preview {
        if u64::from(preview.source_seed) != snapshot.seed
            || preview
                .execution
                .as_ref()
                .and_then(|execution| execution.trial_revision)
                .is_some_and(|revision| revision != crate::session::trial_revision())
        {
            return Err(CommandError::StaleContext);
        }
        preview
            .validate(snapshot.seed, snapshot.time_s)
            .map_err(CommandError::Rejected)?;
    }
    // Do not change even the publication epoch until every field has passed.
    clear_on_reset();
    REPORT.with(|state| {
        let mut state = state.borrow_mut();
        state.epoch = Some((snapshot.seed, snapshot.time_s));
        state.trial_revision = Some(crate::session::trial_revision());
        state.received_at_s = preview
            .as_ref()
            .and_then(|preview| preview.execution.as_ref())
            .map(|_| super::clock::now_s());
        state.preview = preview;
    });
    Ok(())
}

pub(crate) fn clear_for_trial() {
    REPORT.with(|state| *state.borrow_mut() = Report::default());
}

pub(super) fn clear_on_reset() {
    let snapshot = crate::snapshot();
    REPORT.with(|state| {
        let mut state = state.borrow_mut();
        if state.epoch.is_some_and(|(seed, time)| {
            seed != snapshot.seed
                || snapshot.time_s < time
                || state.trial_revision != Some(crate::session::trial_revision())
        }) {
            state.preview = None;
            state.raycast = None;
            state.received_at_s = None;
        }
        state.epoch = Some((snapshot.seed, snapshot.time_s));
        state.trial_revision = Some(crate::session::trial_revision());
    });
}

pub(super) fn connection() -> GuidanceConnection {
    clear_on_reset();
    REPORT.with(|state| {
        let state = state.borrow();
        connection_at(
            state.preview.is_some(),
            state.received_at_s,
            super::clock::now_s(),
        )
    })
}

pub(crate) fn publish_raycast(report: crate::model::LookaheadReport) {
    clear_on_reset();
    REPORT.with(|state| state.borrow_mut().raycast = Some(report));
}

pub(super) fn with_raycast<T>(read: impl FnOnce(Option<&crate::model::LookaheadReport>) -> T) -> T {
    clear_on_reset();
    REPORT.with(|state| read(state.borrow().raycast.as_ref()))
}

pub(super) fn with_preview<T>(read: impl FnOnce(Option<&Preview>) -> T) -> T {
    // Exports and HUD reads can happen after a reset but before the next Update.
    clear_on_reset();
    REPORT.with(|state| read(state.borrow().preview.as_ref()))
}

fn waiting_plan() -> serde_json::Value {
    serde_json::json!({
        "status": "waiting_for_notebook",
        "detail": "Open Notebook and select Connect to show Python A* guidance and the look-ahead ray.",
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
    let connection = connection();
    let raycast = with_raycast(|raycast| {
        raycast.map(|report| {
            serde_json::to_value(report).expect("physical ray reports contain finite values")
        })
    });
    with_preview(|preview| {
        let plan = preview.map_or_else(waiting_plan, |preview| {
            serde_json::to_value(preview).expect("validated preview fields are finite")
        });
        serde_json::json!({
            "plan": plan,
            "raycast": raycast,
            "connection": connection,
            "algorithm_owner": "python_notebook",
            "advisory_only": true,
            "input": "python_notebook_preview",
            "frame": settings().frame_description,
            "validation": "transport_and_rendering_only_not_route_or_collision_validation",
            "publication_policy": "Python schedules replanning. Source timestamps identify the last received plan and physical look-ahead query; resets clear both.",
            "coverage": "Notebook-reported metadata only. Missing radar is not assumed clear space.",
            "limitations": "No route safety or flight-feasibility guarantee. Rust does not predict obstacles, select waypoints or issue flight commands.",
        })
        .to_string()
    })
}

pub(crate) fn brief() -> String {
    let connection = connection();
    let ray: String = with_raycast(|report| match report {
        None => "ray waiting".into(),
        Some(report) => match &report.hit {
            Some(hit) => format!("last ray hit {:.0} m", hit.distance_m),
            None => format!("last ray clear {:.0} m", report.max_distance_m),
        },
    });
    with_preview(|preview| {
        let label = match preview.map(|preview| preview.status) {
            None => "connect notebook",
            Some(PlanStatus::Candidate) => "candidate",
            Some(PlanStatus::NoCandidate) => "no candidate",
            Some(PlanStatus::Planning) => "notebook planning",
            Some(PlanStatus::WaitingForRadar) => "waiting for radar",
            Some(PlanStatus::InvalidInput) => "invalid input",
            Some(PlanStatus::AtWaypoint) => "at waypoint",
        };
        let execution = preview
            .and_then(|preview| preview.execution.as_ref())
            .map(|execution| {
                format!(
                    "{} {}",
                    execution.controller.label(),
                    if connection == GuidanceConnection::Disconnected {
                        "notebook disconnected"
                    } else {
                        execution.state.label()
                    }
                )
            })
            .unwrap_or_else(|| "Python A*".into());
        format!("{execution} | {label} | {ray}")
    })
}

pub(crate) fn details() -> String {
    let connection = connection();
    let ray: String = with_raycast(|report| match report {
        None => "Look-ahead ray: waiting for Python guidance".into(),
        Some(report) => match &report.hit {
            Some(hit) => format!(
                "Look-ahead ray: hit {} at {:.2} m | source {:.2} s",
                hit.entity, hit.distance_m, report.source_time_s
            ),
            None => format!(
                "Look-ahead ray: no hit within {:.0} m | source {:.2} s",
                report.max_distance_m, report.source_time_s
            ),
        },
    });
    with_preview(|preview| {
        let Some(preview) = preview else {
            return format!(
                "Python guidance\nOpen Notebook, then Connect for the A* route and look-ahead ray. Run controller starts flight; connecting only shows guidance.\n{ray}"
            );
        };
        let execution = preview
            .execution
            .as_ref()
            .map(|execution| {
                format!(
                    "{} | {} | tuning revision {}",
                    execution.controller.label(),
                    if connection == GuidanceConnection::Disconnected {
                        "notebook disconnected; held controller state is stale"
                    } else {
                        execution.state.label()
                    },
                    execution.tuning_revision
                )
            })
            .unwrap_or_else(|| "Python preview received; controller state not reported".into());
        format!(
            "{}\n{}\nNotebook source seed {} at {:.2} s\n{} measured obstacles; at least {} unmodelled\nMeasurement age up to {:.2} s | forecast {:.1} s\nSideways allowance {:.1} m/s\n{} expanded nodes | {} submitted points\n{}\nAmber: Python route. Cyan: goal. White: physical look-ahead ray.\nRaycast is a local debug instrument, not SDK radar or a route-safety guarantee.",
            execution,
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
            ray,
        )
    })
}

#[cfg(test)]
mod presence_tests {
    use super::*;

    #[test]
    fn heartbeat_expiry_uses_receipt_time_without_retiming_the_python_plan() {
        assert_eq!(
            connection_at(false, None, 10.0),
            GuidanceConnection::Waiting
        );
        assert_eq!(
            connection_at(true, None, 10.0),
            GuidanceConnection::PreviewOnly
        );
        assert_eq!(
            connection_at(true, Some(10.0), 16.0),
            GuidanceConnection::Connected
        );
        assert_eq!(
            connection_at(true, Some(10.0), 16.001),
            GuidanceConnection::Disconnected
        );
        assert_eq!(
            connection_at(true, Some(10.0), 9.0),
            GuidanceConnection::Disconnected
        );
        assert_eq!(
            connection_at(true, Some(17.0), 17.0),
            GuidanceConnection::Connected
        );
    }
}
