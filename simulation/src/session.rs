//! Imperative adapter for the pure playback transition and physical world.
use crate::{
    api::Dispatch,
    domain::{EngineAction, MissionStatus, ViewportAction},
    model::Snapshot,
    physics::{PhysicsCommand, Simulation},
    runtime::{self, Effect, PlaybackState},
    scenario,
};
use bevy::prelude::*;
use std::{cell::RefCell, collections::VecDeque, rc::Rc};
use wasm_bindgen::prelude::*;

#[derive(Clone)]
struct ViewportState {
    fullscreen: bool,
    maximized: bool,
    message: String,
}

impl Default for ViewportState {
    fn default() -> Self {
        Self {
            fullscreen: false,
            maximized: true,
            message: "WebGPU · Click the scene to activate camera controls".into(),
        }
    }
}

thread_local! {
    static SIMULATION: RefCell<Simulation> = RefCell::new(Simulation::new_default());
    static ACTIONS: RefCell<VecDeque<EngineAction>> = const { RefCell::new(VecDeque::new()) };
    static RUNTIME: RefCell<PlaybackState> = RefCell::new(PlaybackState::default());
    static VIEWPORT: RefCell<ViewportState> = RefCell::new(ViewportState::default());
    static PUBLISHED: RefCell<Option<Rc<Snapshot>>> = const { RefCell::new(None) };
}

pub(crate) fn request_action(action: EngineAction) {
    ACTIONS.with(|queue| {
        let mut queue = queue.borrow_mut();
        if queue.len() < 32 {
            queue.push_back(action);
        }
    });
}

pub(crate) fn snapshot() -> Rc<Snapshot> {
    PUBLISHED.with(|cell| {
        if let Some(state) = cell.borrow().as_ref() {
            return Rc::clone(state);
        }
        let state = SIMULATION.with(|sim| sim.borrow().snapshot());
        publish(state)
    })
}

fn publish(state: Snapshot) -> Rc<Snapshot> {
    let state = Rc::new(state);
    PUBLISHED.with(|cell| *cell.borrow_mut() = Some(Rc::clone(&state)));
    state
}

fn refresh() {
    publish(SIMULATION.with(|sim| sim.borrow().snapshot()));
}

pub(crate) fn observation() -> serde_json::Value {
    SIMULATION.with(|sim| sim.borrow().observation())
}

pub(crate) fn set_viewport(fullscreen: bool, maximized: bool, message: &str) {
    VIEWPORT.with(|cell| {
        *cell.borrow_mut() = ViewportState {
            fullscreen,
            maximized,
            message: message.chars().take(180).collect(),
        }
    });
}

pub(crate) fn runtime_report() -> String {
    RUNTIME.with(|state| {
        let state = state.borrow();
        serde_json::json!({"running":state.running,"autopilot":state.autopilot,"rate":state.rate.factor(),"can_control":state.can_control,"fullscreen":state.fullscreen,"maximized":state.maximized,"message":state.message}).to_string()
    })
}

fn platform_effect(action: ViewportAction) {
    #[cfg(target_arch = "wasm32")]
    if let Ok(callback) = js_sys::Reflect::get(&js_sys::global(), &"__wsorbPlatformAction".into())
        && let Some(callback) = callback.dyn_ref::<js_sys::Function>()
    {
        let name = serde_json::to_value(action).expect("viewport actions serialize to a name");
        let _ = callback.call1(
            &JsValue::NULL,
            &JsValue::from_str(name.as_str().expect("viewport action name")),
        );
    }
    #[cfg(not(target_arch = "wasm32"))]
    let _ = action;
}

pub(crate) fn dispatch(request: Dispatch) -> Result<Rc<Snapshot>, String> {
    match request {
        Dispatch::Action(action) => {
            request_action(action);
            Ok(snapshot())
        }
        Dispatch::View(request) => {
            crate::camera::request_view(request).map_err(|error| error.to_string())?;
            Ok(snapshot())
        }
        Dispatch::Physics(command) => {
            if matches!(command, PhysicsCommand::Step { .. })
                && RUNTIME.with(|state| state.borrow().running)
            {
                return Err("Pause the mission before stepping manually".into());
            }
            match command {
                PhysicsCommand::Reset { .. } | PhysicsCommand::Restore { .. } => {
                    request_action(EngineAction::Pause)
                }
                PhysicsCommand::SetCommand { .. } | PhysicsCommand::Stop => {
                    request_action(EngineAction::ManualControl)
                }
                PhysicsCommand::Observe | PhysicsCommand::Step { .. } => {}
            }
            SIMULATION
                .with(|sim| sim.borrow_mut().execute(command))
                .map(publish)
        }
    }
}

pub(crate) fn advance_world(time: Res<Time>, mut state: ResMut<PlaybackState>) {
    let context = VIEWPORT.with(|context| context.borrow().clone());
    state.fullscreen = context.fullscreen;
    state.maximized = context.maximized;
    state.message = context.message;
    let actions: Vec<_> = ACTIONS.with(|queue| queue.borrow_mut().drain(..).collect());
    for action in actions {
        let status = SIMULATION.with(|sim| sim.borrow().mission_status());
        let transition = runtime::transition(state.clone(), action, status);
        *state = transition.state;
        match transition.effect {
            Effect::None => {}
            Effect::Advance => {
                SIMULATION.with(|sim| {
                    sim.borrow_mut()
                        .advance(scenario::settings().integration.fixed_step_s)
                });
                refresh();
            }
            Effect::Reset => {
                let seed = snapshot().seed;
                SIMULATION.with(|sim| *sim.borrow_mut() = Simulation::new(seed));
                refresh();
            }
            Effect::ZeroCommand => {
                SIMULATION.with(|sim| {
                    let _ = sim.borrow_mut().set_command([0.0; 3], [0.0; 3]);
                });
                refresh();
            }
            Effect::Viewport(action) => platform_effect(action),
        }
    }
    if state.running && state.can_control {
        let step = scenario::settings().integration.fixed_step_s;
        state.accumulator += time.delta_secs_f64().min(0.1) * state.rate.factor();
        let steps = (state.accumulator / step).floor().min(20.0) as u32;
        if steps > 0 {
            SIMULATION.with(|sim| {
                let mut sim = sim.borrow_mut();
                for _ in 0..steps {
                    if state.autopilot
                        && state.guidance_elapsed >= 1.0 / scenario::settings().guidance.cadence_hz
                    {
                        if let Some(accel) = sim.guidance_command() {
                            let _ = sim.set_command(accel, [0.0; 3]);
                        }
                        state.guidance_elapsed = 0.0;
                    }
                    sim.advance(step);
                    state.guidance_elapsed += step;
                    if sim.mission_status() != MissionStatus::Running {
                        state.running = false;
                        let _ = sim.set_command([0.0; 3], [0.0; 3]);
                        break;
                    }
                }
            });
            refresh();
            state.accumulator = (state.accumulator - f64::from(steps) * step).clamp(0.0, 1.0);
        }
    } else {
        state.accumulator = 0.0;
    }
    RUNTIME.with(|current| *current.borrow_mut() = state.clone());
}
