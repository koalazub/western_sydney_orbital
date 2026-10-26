//! Caller-driven scheduling of the real session, without graphics or sensor setup.
use bevy::{ecs::schedule::SingleThreadedExecutor, input_focus::InputFocus, prelude::*};
use serde_json::{Value, json};
use std::{
    any::Any,
    cell::{Cell, RefCell},
    panic::{AssertUnwindSafe, catch_unwind},
    time::Duration,
};

#[derive(Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
enum Phase {
    Unstarted,
    Starting,
    Idle,
    Updating,
    Failed,
}

struct Owner {
    phase: Cell<Phase>,
    app: RefCell<Option<App>>,
    error: RefCell<Option<String>>,
}

thread_local! {
    static OWNER: Owner = const { Owner {
        phase: Cell::new(Phase::Unstarted),
        app: RefCell::new(None),
        error: RefCell::new(None),
    } };
}

/// Ownership remains held during updates and after a failed, possibly partial frame.
pub(crate) fn started() -> bool {
    OWNER.with(|owner| owner.phase.get() != Phase::Unstarted)
}

pub(crate) fn start() -> Result<(), String> {
    if started() {
        return Err("Headless runtime already started".into());
    }
    // Read the renderer's actual ownership flag. Never change its state or hook.
    let renderer: Value = serde_json::from_str(&crate::app::status())
        .map_err(|error| format!("Cannot read renderer ownership: {error}"))?;
    match renderer["started"].as_bool() {
        Some(false) => {}
        Some(true) => return Err("Renderer already owns the runtime".into()),
        None => return Err("Renderer status has no ownership flag".into()),
    }
    OWNER.with(|owner| {
        if owner.phase.get() != Phase::Unstarted {
            return Err("Headless runtime already started".into());
        }
        owner.phase.set(Phase::Starting);
        // No RefCell borrow is held through construction or plugin finalization.
        let result = catch_unwind(AssertUnwindSafe(build_app)).map_err(panic_error);
        match result {
            Ok(app) => {
                owner.app.replace(Some(app));
                owner.error.replace(None);
                owner.phase.set(Phase::Idle);
                Ok(())
            }
            Err(error) => {
                owner.error.replace(Some(error.clone()));
                owner.phase.set(Phase::Unstarted);
                Err(error)
            }
        }
    })
}

fn build_app() -> App {
    let mut app = App::new();
    app.insert_resource(crate::session::playback_state())
        .init_resource::<Time<()>>()
        .init_resource::<Time<Real>>()
        .init_resource::<ButtonInput<KeyCode>>()
        .init_resource::<crate::hud::HudState>()
        .init_resource::<InputFocus>()
        .configure_sets(
            Update,
            (crate::EngineSet::Runtime, crate::EngineSet::Reports).chain(),
        )
        .add_systems(
            Update,
            crate::session::advance_world.in_set(crate::EngineSet::Runtime),
        )
        // Session and report publication both use the calling thread's world.
        .edit_schedule(Update, |schedule| {
            schedule.set_executor(SingleThreadedExecutor::new());
        });
    crate::telemetry::install(&mut app);
    app.finish();
    app.cleanup();
    // In particular, do not update here: queued actions belong to the next tick.
    app
}

pub(crate) fn tick(delta_wall_s: f64) -> Result<(), String> {
    if !delta_wall_s.is_finite() || !(0.0..=1.0).contains(&delta_wall_s) {
        return Err("Headless tick requires finite elapsed seconds between 0 and 1".into());
    }
    let delta = Duration::from_secs_f64(delta_wall_s);
    let mut app =
        OWNER.with(|owner| {
            match owner.phase.get() {
                Phase::Unstarted => return Err("Headless runtime has not started".to_owned()),
                Phase::Starting | Phase::Updating => {
                    return Err("Headless runtime is already busy".to_owned());
                }
                Phase::Failed => {
                    return Err(owner.error.borrow().clone().unwrap_or_else(|| {
                        "Headless runtime stopped after a failed frame".into()
                    }));
                }
                Phase::Idle => {}
            }
            let app = owner
                .app
                .try_borrow_mut()
                .map_err(|_| "Headless runtime is already borrowed".to_owned())?
                .take()
                .ok_or_else(|| "Headless runtime has no scheduler".to_owned())?;
            owner.phase.set(Phase::Updating);
            Ok(app)
        })?;
    // The owner holds no app borrow while real systems run. There is deliberately
    // no TimePlugin to overwrite the caller's elapsed duration or drop tick one.
    let result = catch_unwind(AssertUnwindSafe(|| {
        app.world_mut().resource_mut::<Time<()>>().advance_by(delta);
        app.world_mut()
            .resource_mut::<Time<Real>>()
            .advance_by(delta);
        app.update();
    }))
    .map_err(panic_error);
    OWNER.with(|owner| {
        owner.app.replace(Some(app));
        match &result {
            Ok(()) => owner.phase.set(Phase::Idle),
            Err(error) => {
                // Do not rerun a partially executed schedule or release its world
                // to a renderer. No global renderer failure bridge is installed.
                owner.error.replace(Some(error.clone()));
                owner.phase.set(Phase::Failed);
            }
        }
    });
    result
}

pub(crate) fn status() -> Value {
    let (phase, wall_elapsed_s, error) = OWNER.with(|owner| {
        let elapsed = owner.app.borrow().as_ref().and_then(|app| {
            app.world()
                .get_resource::<Time<Real>>()
                .map(Time::elapsed_secs_f64)
        });
        (owner.phase.get(), elapsed, owner.error.borrow().clone())
    });
    // Release owner borrows before reading the real session or published world.
    json!({
        "backend": "native_headless",
        "owned": phase != Phase::Unstarted,
        "started": matches!(phase, Phase::Idle | Phase::Updating | Phase::Failed),
        "phase": phase,
        "wall_elapsed_s": wall_elapsed_s,
        "time_s": crate::snapshot().time_s,
        "running": crate::session::is_running(),
        "error": error,
    })
}

fn panic_error(payload: Box<dyn Any + Send>) -> String {
    let message = payload
        .downcast_ref::<String>()
        .map(String::as_str)
        .or_else(|| payload.downcast_ref::<&str>().copied())
        .unwrap_or("scheduler panic");
    format!("Headless runtime stopped: {message}")
}

#[cfg(all(test, not(target_arch = "wasm32")))]
mod tests {
    use super::*;
    use crate::{
        api::Dispatch,
        domain::{EngineAction, PlaybackRate},
        physics::PhysicsCommand,
    };

    #[test]
    fn lifecycle_preserves_the_session_and_runs_real_runtime_then_reports() {
        assert!(!started());
        assert!(tick(0.0).is_err());
        let step = crate::scenario::settings().integration.fixed_step_s;
        crate::session::dispatch(Dispatch::Physics(PhysicsCommand::Step { seconds: step }))
            .unwrap();
        let before = crate::snapshot();
        let serialized = serde_json::to_value(before.as_ref()).unwrap();
        let runtime = crate::session::runtime_report();
        let renderer = crate::app::status();
        let cameras = crate::sensors::status();
        crate::request_action(EngineAction::Play(PlaybackRate::RealTime));
        let queued = crate::session::queued_actions_for_tests();

        start().unwrap();
        assert!(started());
        assert_eq!(crate::session::runtime_report(), runtime);
        assert_eq!(crate::session::queued_actions_for_tests(), queued);
        assert_eq!(
            serde_json::to_value(crate::snapshot().as_ref()).unwrap(),
            serialized
        );
        assert_eq!(crate::app::status(), renderer);
        assert_eq!(crate::sensors::status(), cameras);
        assert!(start().is_err());

        OWNER.with(|owner| {
            let app = owner.app.borrow();
            let app = app.as_ref().unwrap();
            let world = app.world();
            assert!(world.contains_resource::<Time<()>>());
            assert!(world.contains_resource::<Time<Real>>());
            assert!(world.contains_resource::<ButtonInput<KeyCode>>());
            assert!(world.contains_resource::<crate::PlaybackState>());
            assert!(world.contains_resource::<crate::hud::HudState>());
            assert!(world.contains_resource::<InputFocus>());
            assert!(app.get_sub_app(bevy::render::RenderApp).is_none());
            assert_eq!(world.resource::<Time<Real>>().elapsed(), Duration::ZERO);
        });
        for delta in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY, -0.01, 1.01] {
            assert!(tick(delta).is_err());
        }
        assert_eq!(crate::session::queued_actions_for_tests(), queued);
        assert_eq!(
            serde_json::to_value(crate::snapshot().as_ref()).unwrap(),
            serialized
        );
        assert_eq!(status()["wall_elapsed_s"], 0.0);

        tick(0.0).unwrap();
        assert!(crate::session::is_running());
        assert_eq!(crate::snapshot().time_s, before.time_s);
        tick(step).unwrap();
        assert!(crate::session::is_running());
        assert!(crate::snapshot().time_s > before.time_s);
        let report: Value = serde_json::from_str(&crate::telemetry::status()).unwrap();
        assert_eq!(report["source_time_s"], crate::snapshot().time_s);
        assert_eq!(status()["backend"], "native_headless");
        assert_eq!(status()["started"], true);
        assert_eq!(status()["owned"], true);

        let before_long_tick = crate::snapshot().time_s;
        tick(1.0).unwrap();
        assert!(crate::snapshot().time_s > before_long_tick);
        assert!(crate::snapshot().time_s - before_long_tick <= 0.1 + 1e-12);
        let elapsed = Duration::from_secs_f64(step) + Duration::from_secs(1);
        OWNER.with(|owner| {
            let app = owner.app.borrow();
            let world = app.as_ref().unwrap().world();
            assert_eq!(world.resource::<Time<()>>().elapsed(), elapsed);
            assert_eq!(world.resource::<Time<Real>>().elapsed(), elapsed);
            assert_eq!(world.resource::<Time<()>>().delta(), Duration::from_secs(1));
            assert_eq!(
                world.resource::<Time<Real>>().delta(),
                Duration::from_secs(1)
            );
        });

        crate::request_action(EngineAction::Pause);
        let paused_time = crate::snapshot().time_s;
        tick(step).unwrap();
        assert!(!crate::session::is_running());
        assert_eq!(crate::snapshot().time_s, paused_time);
        let report: Value = serde_json::from_str(&crate::telemetry::status()).unwrap();
        assert_eq!(report["source_time_s"], paused_time);
        assert_eq!(crate::app::status(), renderer);
        let cameras: Value = serde_json::from_str(&crate::sensors::status()).unwrap();
        assert_eq!(cameras["configured"], false);
        assert_eq!(cameras["views"], json!([]));
    }

    #[test]
    fn missing_required_clock_fails_closed_without_changing_renderer_state() {
        let renderer = crate::app::status();
        start().unwrap();
        OWNER.with(|owner| {
            owner
                .app
                .borrow_mut()
                .as_mut()
                .unwrap()
                .world_mut()
                .remove_resource::<Time<()>>();
        });
        let error = tick(0.05).unwrap_err();
        assert!(started());
        assert!(start().is_err());
        assert_eq!(status()["phase"], "failed");
        assert_eq!(status()["owned"], true);
        assert_eq!(status()["error"], error);
        assert_eq!(tick(0.05).unwrap_err(), error);
        assert_eq!(crate::app::status(), renderer);
    }
}
