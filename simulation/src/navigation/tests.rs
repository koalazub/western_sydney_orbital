use super::{model, submit};
use crate::{api::Dispatch, model::PhysicsCommand, scenario::settings};
use serde_json::{Value, json};
use std::rc::Rc;

fn execute(command: PhysicsCommand) -> Rc<crate::model::Snapshot> {
    crate::session::dispatch(Dispatch::Physics(command)).unwrap()
}

fn reset(seed: u64) -> Rc<crate::model::Snapshot> {
    execute(PhysicsCommand::Reset { seed: Some(seed) })
}

/// These points are transport input, deliberately not a claim about a valid
/// flight route. A line through the body must not trigger a Rust planning check.
fn transport_input(snapshot: &crate::model::Snapshot) -> Value {
    json!({
        "source_seed": snapshot.seed,
        "source_time_s": snapshot.time_s,
        "status": "candidate",
        "detail": "Transport test input only, not a flight-valid route",
        "points_world_m": [[0.0, 0.0, 0.0], [1.0, 2.0, 3.0]],
        "goal_center_m": [4.0, 5.0, 6.0],
        "goal_radius_m": 1.0,
        "measured_obstacles": 1,
        "unmodelled_obstacles_lower_bound": 1,
        "forecast_horizon_s": 0.0,
        "max_measurement_age_s": 0.0,
        "oldest_measurement_time_s": null,
        "newest_measurement_time_s": null,
        "assumed_tangential_speed_allowance_mps": 0.0,
        "expanded_nodes": 0,
    })
}

fn report() -> Value {
    serde_json::from_str(&model::status()).unwrap()
}

fn assert_waiting() {
    let report = report();
    assert_eq!(report["algorithm_owner"], "python_notebook");
    assert_eq!(report["advisory_only"], true);
    assert_eq!(report["plan"]["status"], "waiting_for_notebook");
    assert_eq!(report["plan"]["points_world_m"], json!([]));
    assert!(report["plan"]["measured_obstacles"].is_null());
    assert!(report["plan"]["goal_center_m"].is_null());
    assert!(model::brief().contains("connect notebook"));
    assert!(model::with_preview(|preview| preview.is_none()));
}

#[test]
fn valid_transport_submission_preserves_actual_physics_commands_and_rng() {
    let before = reset(settings().default_seed);
    let before_json = serde_json::to_string(before.as_ref()).unwrap();
    let observation_before = crate::session::observation();
    let payload = transport_input(&before);

    submit(&payload.to_string()).unwrap();

    // Observe reads the actual Simulation again, not just the published Rc cache.
    let after = execute(PhysicsCommand::Observe);
    assert_eq!(before_json, serde_json::to_string(after.as_ref()).unwrap());
    assert_eq!(before.rng_state, after.rng_state);
    assert_eq!(observation_before, crate::session::observation());
    let published = report();
    assert_eq!(published["plan"], payload);
    assert_eq!(published["algorithm_owner"], "python_notebook");
    assert_eq!(published["advisory_only"], true);
    assert!(published["plan"].get("search_failure").is_none());
    assert!(published["plan"].get("replanning").is_none());
    assert_eq!(model::with_preview(|p| p.unwrap().render_points().len()), 2);
}

#[test]
fn python_execution_and_real_lookahead_remain_readonly_and_are_reported_together() {
    let before = reset(settings().default_seed);
    let mut payload = transport_input(&before);
    payload["execution"] = json!({"state":"watching","controller":"planned_waypoint_pd","source_time_s":before.time_s,
        "source_seed":before.seed,"next_waypoint_world_m":[1.0,2.0,3.0],"tuning_revision":0});
    submit(&payload.to_string()).unwrap();
    let ray: Value = serde_json::from_str(&crate::lookahead_raycast(
        r#"{"target_world_m":[0,0,0],"max_distance_m":250}"#,
    ))
    .unwrap();
    assert_eq!(ray["ok"], true);
    assert_eq!(report()["raycast"], ray["report"]);
    assert_eq!(report()["plan"], payload);
    assert!(model::brief().contains("A* + waypoint PD preview"));
    assert!(model::details().contains("Look-ahead ray:"));
    submit(&payload.to_string()).unwrap();
    assert_eq!(
        report()["raycast"],
        ray["report"],
        "a new Python plan must not erase the real physical trace"
    );
    assert_eq!(
        serde_json::to_string(execute(PhysicsCommand::Observe).as_ref()).unwrap(),
        serde_json::to_string(before.as_ref()).unwrap()
    );
    let published = model::status();
    for execution in [
        json!({"state":"watching","controller":"native_autopilot","source_time_s":0,"source_seed":before.seed,"next_waypoint_world_m":null,"tuning_revision":0}),
        json!({"state":"running","controller":"planned_waypoint_pd","source_time_s":-1,"source_seed":before.seed,"next_waypoint_world_m":null,"tuning_revision":0}),
    ] {
        let mut invalid = payload.clone();
        invalid["execution"] = execution;
        assert!(submit(&invalid.to_string()).is_err());
        assert_eq!(model::status(), published);
    }
    reset((before.seed + 1) % (u64::from(u32::MAX) + 1));
    assert_waiting();
    assert!(report()["raycast"].is_null());
}

#[test]
fn wrong_trial_preview_has_a_structured_stale_error_without_erasing_current_guidance() {
    let initial = reset(settings().default_seed);
    let mut old = transport_input(&initial);
    old["execution"] = json!({
        "state": "watching", "controller": "planned_waypoint_pd",
        "source_time_s": initial.time_s, "source_seed": initial.seed,
        "next_waypoint_world_m": null, "tuning_revision": initial.tuning_revision,
        "trial_revision": crate::session::trial_revision(),
    });
    submit(&old.to_string()).unwrap();
    reset(initial.seed);
    let mut current = old.clone();
    current["execution"]["trial_revision"] = json!(crate::session::trial_revision());
    submit(&current.to_string()).unwrap();
    let physics = crate::session::observation();
    let navigation = model::status();
    let rejection: Value =
        serde_json::from_str(&crate::set_navigation_preview(&old.to_string())).unwrap();
    assert_eq!(rejection["ok"], false);
    assert_eq!(rejection["code"], "stale_context");
    assert_eq!(crate::session::observation(), physics);
    assert_eq!(model::status(), navigation);
}

#[test]
fn an_explicit_null_clears_only_the_rendered_preview() {
    let snapshot = reset(settings().default_seed);
    submit(&transport_input(&snapshot).to_string()).unwrap();
    submit("null").unwrap();
    assert_waiting();
    assert_eq!(
        serde_json::to_string(snapshot.as_ref()).unwrap(),
        serde_json::to_string(execute(PhysicsCommand::Observe).as_ref()).unwrap()
    );
}

#[test]
fn invalid_payloads_leave_the_published_report_and_actual_simulation_unchanged() {
    let snapshot = reset(settings().default_seed);
    let payload = transport_input(&snapshot);
    submit(&payload.to_string()).unwrap();
    let published = model::status();
    let physics = serde_json::to_string(snapshot.as_ref()).unwrap();
    let position_limit = settings().limits.position_m;
    let mut invalid_payloads = Vec::new();
    for (field, value) in [
        (
            "source_seed",
            json!((snapshot.seed + 1) % (u64::from(u32::MAX) + 1)),
        ),
        ("source_seed", json!(u64::from(u32::MAX) + 1)),
        ("source_seed", json!(-1)),
        ("source_time_s", json!(-1.0)),
        ("source_time_s", json!(snapshot.time_s + 0.01)),
        ("status", json!("waiting_for_notebook")),
        ("detail", json!("x".repeat(513))),
        ("points_world_m", json!([[0.0, 0.0, 0.0]])),
        ("points_world_m", json!([[0.0, 0.0], [1.0, 2.0, 3.0]])),
        (
            "points_world_m",
            json!([[0.0, 0.0, 0.0, 0.0], [1.0, 2.0, 3.0]]),
        ),
        ("points_world_m", json!([])),
        (
            "points_world_m",
            json!(vec![[0.0; 3]; model::MAX_RENDER_POINTS + 1]),
        ),
        (
            "points_world_m",
            json!([[0.0, 0.0, 0.0], [position_limit + 1.0, 0.0, 0.0]]),
        ),
        ("goal_center_m", json!([-position_limit - 1.0, 0.0, 0.0])),
        ("goal_center_m", json!([0.0, 0.0])),
        ("goal_radius_m", json!(0.0)),
        ("goal_radius_m", json!(-1.0)),
        ("goal_radius_m", json!(settings().limits.radius_m + 1.0)),
        ("measured_obstacles", json!(-1)),
        ("unmodelled_obstacles_lower_bound", json!(0.5)),
        ("expanded_nodes", json!(-1)),
        ("forecast_horizon_s", json!(-1.0)),
        ("max_measurement_age_s", json!(-1.0)),
        ("assumed_tangential_speed_allowance_mps", json!(-1.0)),
        ("oldest_measurement_time_s", json!(-1.0)),
        ("newest_measurement_time_s", json!(snapshot.time_s + 0.01)),
    ] {
        let mut invalid = payload.clone();
        invalid[field] = value;
        invalid_payloads.push(invalid.to_string());
    }
    for missing in [
        "source_seed",
        "oldest_measurement_time_s",
        "newest_measurement_time_s",
    ] {
        let mut invalid = payload.clone();
        invalid.as_object_mut().unwrap().remove(missing);
        invalid_payloads.push(invalid.to_string());
    }
    let mut unknown = payload.clone();
    unknown["thrust"] = json!([1.0, 0.0, 0.0]);
    invalid_payloads.push(unknown.to_string());
    for nonfinite in ["NaN", "Infinity", "-Infinity", "1e999"] {
        invalid_payloads.push(payload.to_string().replace(
            "\"forecast_horizon_s\":0.0",
            &format!("\"forecast_horizon_s\":{nonfinite}"),
        ));
    }
    invalid_payloads.push(format!("{}{}", payload, " ".repeat(model::MAX_JSON_BYTES)));

    for invalid in invalid_payloads {
        assert!(
            submit(&invalid).is_err(),
            "accepted invalid transport input"
        );
        assert_eq!(published, model::status());
        let actual = execute(PhysicsCommand::Observe);
        assert_eq!(physics, serde_json::to_string(actual.as_ref()).unwrap());
        assert_eq!(snapshot.rng_state, actual.rng_state);
    }
}

#[test]
fn render_limits_and_nullable_measurement_metadata_are_transport_bounds() {
    let snapshot = reset(settings().default_seed);
    let mut payload = transport_input(&snapshot);
    payload["detail"] = json!("é".repeat(512));
    payload["points_world_m"] = json!(vec![
        [
            settings().limits.position_m,
            -settings().limits.position_m,
            0.0
        ];
        model::MAX_RENDER_POINTS
    ]);
    payload["goal_radius_m"] = json!(settings().limits.radius_m);
    payload["oldest_measurement_time_s"] = json!(0.0);
    payload["newest_measurement_time_s"] = json!(0.0);
    // Counters are notebook metadata, not checks against private obstacle truth.
    payload["measured_obstacles"] = json!(u32::MAX);
    payload["expanded_nodes"] = json!(u32::MAX);
    submit(&payload.to_string()).unwrap();
    assert_eq!(report()["plan"], payload);
    assert_eq!(
        model::with_preview(|p| p.unwrap().render_points().len()),
        4096
    );

    let published = model::status();
    payload["detail"] = json!("é".repeat(513));
    assert!(submit(&payload.to_string()).is_err());
    assert_eq!(published, model::status());
}

#[test]
fn source_time_tolerance_and_measurement_order_are_validated_atomically() {
    reset(settings().default_seed);
    let snapshot = execute(PhysicsCommand::Step { seconds: 0.5 });
    let mut payload = transport_input(&snapshot);
    payload["source_time_s"] = json!(snapshot.time_s + 0.5e-6);
    payload["oldest_measurement_time_s"] = json!(0.1);
    payload["newest_measurement_time_s"] = json!(0.2);
    submit(&payload.to_string()).unwrap();
    let published = model::status();

    for (field, value) in [
        ("source_time_s", json!(snapshot.time_s + 2e-6)),
        ("oldest_measurement_time_s", json!(0.3)),
        ("newest_measurement_time_s", json!(snapshot.time_s + 2e-6)),
    ] {
        let mut invalid = payload.clone();
        invalid[field] = value;
        assert!(submit(&invalid.to_string()).is_err());
        assert_eq!(published, model::status());
    }
}

#[test]
fn the_json_byte_limit_includes_trailing_whitespace() {
    let snapshot = reset(settings().default_seed);
    let json = transport_input(&snapshot).to_string();
    let at_limit = format!("{}{}", json, " ".repeat(model::MAX_JSON_BYTES - json.len()));
    submit(&at_limit).unwrap();
    let published = model::status();
    assert!(submit(&format!("{at_limit} ")).is_err());
    assert_eq!(published, model::status());
}

#[test]
fn every_noncandidate_status_replaces_the_old_line_without_native_fallback() {
    let snapshot = reset(settings().default_seed);
    let payload = transport_input(&snapshot);
    for status in [
        "no_candidate",
        "planning",
        "waiting_for_radar",
        "invalid_input",
        "at_waypoint",
    ] {
        submit(&payload.to_string()).unwrap();
        let mut replacement = payload.clone();
        replacement["status"] = json!(status);
        replacement["points_world_m"] = json!([]);
        submit(&replacement.to_string()).unwrap();
        for _ in 0..3 {
            model::clear_on_reset();
        }
        assert_eq!(report()["plan"], replacement);
        assert!(model::with_preview(|p| p
            .unwrap()
            .render_points()
            .is_empty()));

        // Even noncandidate input containing points cannot draw a candidate line.
        replacement["points_world_m"] = payload["points_world_m"].clone();
        submit(&replacement.to_string()).unwrap();
        assert!(model::with_preview(|p| p
            .unwrap()
            .render_points()
            .is_empty()));
    }
}

#[test]
fn actual_seed_change_clears_before_status_read_without_touching_physics_or_rng() {
    let snapshot = reset(settings().default_seed);
    submit(&transport_input(&snapshot).to_string()).unwrap();
    let changed = reset((snapshot.seed + 1) % (u64::from(u32::MAX) + 1));
    let physics = serde_json::to_string(changed.as_ref()).unwrap();
    assert_waiting();
    let actual = execute(PhysicsCommand::Observe);
    assert_eq!(physics, serde_json::to_string(actual.as_ref()).unwrap());
    assert_eq!(changed.rng_state, actual.rng_state);
    // A rejected old-seed report cannot revive the line.
    assert!(submit(&transport_input(&snapshot).to_string()).is_err());
    assert_waiting();
}

#[test]
fn actual_same_seed_reset_and_restore_rewind_clear_even_a_backdated_preview() {
    let initial = reset(settings().default_seed);
    for restore in [false, true] {
        execute(PhysicsCommand::Step { seconds: 0.5 });
        assert!(crate::snapshot().time_s > initial.time_s);
        // The publication epoch must use actual time, not this older source time.
        submit(&transport_input(&initial).to_string()).unwrap();
        if restore {
            execute(PhysicsCommand::Restore {
                state: Box::new(initial.as_ref().clone()),
            });
        } else {
            reset(initial.seed);
        }
        let rewound = crate::snapshot();
        let physics = serde_json::to_string(rewound.as_ref()).unwrap();
        model::clear_on_reset();
        assert_waiting();
        let actual = execute(PhysicsCommand::Observe);
        assert_eq!(physics, serde_json::to_string(actual.as_ref()).unwrap());
        assert_eq!(rewound.rng_state, actual.rng_state);
    }
}

#[test]
fn notebook_absence_stays_waiting_as_the_actual_simulation_advances() {
    // Use an actual seed change to discard any prior publication on this thread.
    let initial = reset(settings().default_seed);
    submit(&transport_input(&initial).to_string()).unwrap();
    reset((initial.seed + 1) % (u64::from(u32::MAX) + 1));
    for _ in 0..3 {
        execute(PhysicsCommand::Step { seconds: 0.1 });
        let before = serde_json::to_string(crate::snapshot().as_ref()).unwrap();
        model::clear_on_reset();
        assert_waiting();
        let actual = execute(PhysicsCommand::Observe);
        assert_eq!(before, serde_json::to_string(actual.as_ref()).unwrap());
    }
}
