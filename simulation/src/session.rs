//! Imperative adapter for the pure playback transition and physical world.
use crate::{
    api::Dispatch,
    controls::{KeyboardPilot, Movement, MovementTarget, PilotContext, PilotEffect},
    domain::{EngineAction, MissionStatus, ViewportAction},
    model::Snapshot,
    physics::{PhysicsCommand, Simulation},
    runtime::{self, Effect, PlaybackState},
    scenario,
};
use bevy::prelude::*;
use std::{
    cell::{Cell, RefCell},
    collections::VecDeque,
    rc::Rc,
};
#[cfg(target_arch = "wasm32")]
use wasm_bindgen::prelude::*;

struct ViewportState {
    fullscreen: bool,
    maximized: bool,
    message: String,
}

const MAX_ACTIONS: usize = 32;

impl Default for ViewportState {
    fn default() -> Self {
        Self {
            fullscreen: false,
            maximized: true,
            message: "WebGPU · Click the scene to activate ship and debug controls".into(),
        }
    }
}

thread_local! {
    static SIMULATION: RefCell<Simulation> = RefCell::new(Simulation::new_default());
    static ACTIONS: RefCell<VecDeque<EngineAction>> = const { RefCell::new(VecDeque::new()) };
    static RUNTIME: RefCell<PlaybackState> = RefCell::new(PlaybackState::default());
    static VIEWPORT: RefCell<ViewportState> = RefCell::new(ViewportState::default());
    static PUBLISHED: RefCell<Option<Rc<Snapshot>>> = const { RefCell::new(None) };
    static KEYBOARD_PILOT: RefCell<KeyboardPilot> = RefCell::new(KeyboardPilot::default());
    static SPACE_HELD: Cell<bool> = const { Cell::new(false) };
}

pub(crate) fn request_action(action: EngineAction) {
    ACTIONS.with(|queue| {
        let mut queue = queue.borrow_mut();
        if queue.len() < MAX_ACTIONS {
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
        let config = scenario::settings();
        let keyboard_thrust =
            KEYBOARD_PILOT.with(|pilot| pilot.borrow().owns_command()) && state.running;
        serde_json::json!({
            "running": state.running,
            "controller": "external_python_or_manual",
            "reference_accel_limit_mps2": config.lander.max_accel_mps2,
            "keyboard_thrust": keyboard_thrust,
            "keyboard_requires_release": keyboard_requires_release(),
            "manual_interventions": SIMULATION.with(|sim| sim.borrow().manual_interventions()),
            "rate": state.rate.factor(),
            "can_control": state.can_control,
            "fullscreen": state.fullscreen,
            "maximized": state.maximized,
            "message": state.message,
        })
        .to_string()
    })
}

pub(crate) fn keyboard_requires_release() -> bool {
    KEYBOARD_PILOT.with(|pilot| pilot.borrow().requires_release())
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
            let debug_upright = matches!(command, PhysicsCommand::DebugUpright);
            if debug_upright && !RUNTIME.with(|state| state.borrow().can_control) {
                return Err(
                    "Upright lander is unavailable in view-only mode. Request control first."
                        .into(),
                );
            }
            if debug_upright && ACTIONS.with(|actions| actions.borrow().len() >= MAX_ACTIONS) {
                return Err("Upright lander is waiting for queued actions. Let the next frame finish, then retry.".into());
            }
            if matches!(command, PhysicsCommand::Step { .. })
                && RUNTIME.with(|state| state.borrow().running)
            {
                return Err("Pause the mission before stepping manually".into());
            }
            let replaces_keyboard_command = matches!(
                command,
                PhysicsCommand::Reset { .. }
                    | PhysicsCommand::Restore { .. }
                    | PhysicsCommand::DebugUpright
                    | PhysicsCommand::SetCommand { .. }
                    | PhysicsCommand::Stop
            );
            match command {
                PhysicsCommand::Reset { .. }
                | PhysicsCommand::Restore { .. }
                | PhysicsCommand::DebugUpright => request_action(EngineAction::Pause),
                PhysicsCommand::Observe
                | PhysicsCommand::Step { .. }
                | PhysicsCommand::SetCommand { .. }
                | PhysicsCommand::Stop => {}
            }
            let state = SIMULATION.with(|sim| sim.borrow_mut().execute(command))?;
            if replaces_keyboard_command {
                // An external command already replaced our force. A later
                // key release must not zero it or revive a previously held key.
                KEYBOARD_PILOT.with(|pilot| pilot.borrow_mut().cancel_until_release());
            }
            if debug_upright {
                // The queued Pause reaches Bevy before its next physics advance.
                // Report the pause immediately to external experiment runners.
                RUNTIME.with(|runtime| {
                    let mut reported = runtime.borrow_mut();
                    *reported = runtime::transition(
                        reported.clone(),
                        EngineAction::Pause,
                        state.mission.status,
                    )
                    .state;
                });
            }
            Ok(publish(state))
        }
    }
}

pub(crate) fn advance_world(
    time: Res<Time>,
    keys: Res<ButtonInput<KeyCode>>,
    hud: Res<crate::hud::HudState>,
    mut state: ResMut<PlaybackState>,
) {
    let movement = crate::controls::movement(&keys);
    let space_down = crate::controls::lift_key_down(&keys);
    let fresh_space = SPACE_HELD.with(|held| !held.replace(space_down) && space_down);
    VIEWPORT.with(|context| {
        let context = context.borrow();
        state.fullscreen = context.fullscreen;
        state.maximized = context.maximized;
        state.message.clone_from(&context.message);
    });
    let actions: Vec<_> = ACTIONS.with(|queue| queue.borrow_mut().drain(..).collect());
    let explicit_playback_action = actions.iter().any(|action| {
        matches!(
            action,
            EngineAction::Pause
                | EngineAction::TogglePlayback
                | EngineAction::Reset
                | EngineAction::DebugUpright
                | EngineAction::Step
        )
    });
    for action in actions {
        let status = SIMULATION.with(|sim| sim.borrow().mission_status());
        let transition = runtime::transition(state.clone(), action, status);
        *state = transition.state;
        match transition.effect {
            Effect::None => {}
            Effect::Advance => {
                // A queued pause/step must not advance with stale keyboard thrust.
                update_keyboard_pilot(&state, movement, hud.movement_blocked());
                SIMULATION.with(|sim| {
                    sim.borrow_mut()
                        .advance(scenario::settings().integration.fixed_step_s)
                });
                refresh();
            }
            Effect::Reset => {
                let seed = snapshot().seed;
                SIMULATION.with(|sim| *sim.borrow_mut() = Simulation::new(seed));
                KEYBOARD_PILOT.with(|pilot| pilot.borrow_mut().cancel_until_release());
                refresh();
            }
            Effect::DebugUpright => {
                match SIMULATION.with(|sim| sim.borrow_mut().execute(PhysicsCommand::DebugUpright))
                {
                    Ok(snapshot) => {
                        KEYBOARD_PILOT.with(|pilot| pilot.borrow_mut().cancel_until_release());
                        publish(snapshot);
                    }
                    Err(error) => state.message = error,
                }
            }
            Effect::Viewport(action) => platform_effect(action),
        }
    }
    if fresh_space
        && !explicit_playback_action
        && !state.running
        && state.can_control
        // A fresh lift key must not revive another movement key held across a reset.
        && !keyboard_requires_release()
        && crate::camera::input_active()
        && !hud.movement_blocked()
        && movement.target == MovementTarget::Ship
        && movement.axes[2] > 0.0
        && SIMULATION.with(|sim| sim.borrow().mission_status() == MissionStatus::Running)
    {
        // A fresh lift key is a launch action, not a pause toggle or teleport.
        let rate = state.rate;
        *state = runtime::transition(
            state.clone(),
            EngineAction::Play(rate),
            MissionStatus::Running,
        )
        .state;
        KEYBOARD_PILOT.with(|pilot| pilot.borrow_mut().rearm_for_launch());
    }
    update_keyboard_pilot(&state, movement, hud.movement_blocked());
    if state.running && state.can_control {
        let step = scenario::settings().integration.fixed_step_s;
        state.accumulator += time.delta_secs_f64().min(0.1) * state.rate.factor();
        let steps = (state.accumulator / step).floor().min(20.0) as u32;
        if steps > 0 {
            SIMULATION.with(|sim| {
                let mut sim = sim.borrow_mut();
                for _ in 0..steps {
                    sim.advance(step);
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

fn update_keyboard_pilot(state: &PlaybackState, movement: Movement, modal: bool) {
    let mission_running =
        SIMULATION.with(|sim| sim.borrow().mission_status() == MissionStatus::Running);
    let effect = KEYBOARD_PILOT.with(|pilot| {
        pilot.borrow_mut().update(
            movement,
            PilotContext {
                active: crate::camera::input_active(),
                running: state.running,
                can_control: state.can_control,
                mission_running,
                modal,
            },
            scenario::settings().lander.max_accel_mps2,
        )
    });
    let changed =
        SIMULATION.with(|sim| apply_keyboard_effect(&mut sim.borrow_mut(), state, effect));
    if changed {
        refresh();
    }
}

/// The only keyboard-to-physics adapter. This sets bounded body-frame thrust,
/// never position or velocity. Space and E use the same reference acceleration cap.
fn apply_keyboard_effect(sim: &mut Simulation, state: &PlaybackState, effect: PilotEffect) -> bool {
    match effect {
        PilotEffect::None => false,
        PilotEffect::SetCommand(accel)
            if state.can_control
                && state.running
                && sim.mission_status() == MissionStatus::Running =>
        {
            sim.set_manual_thrust(accel).is_ok()
        }
        PilotEffect::ReleaseCommand if state.can_control => {
            sim.set_command([0.0; 3], [0.0; 3]).is_ok()
        }
        PilotEffect::SetCommand(_) | PilotEffect::ReleaseCommand => false,
    }
}

#[cfg(test)]
mod keyboard_tests;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ship_keys_use_real_thrust_without_teleporting_or_changing_vehicle_parameters() {
        let mut simulation = Simulation::new_default();
        let before = simulation.snapshot();
        let playback = PlaybackState {
            running: true,
            ..default()
        };
        let mut pilot = KeyboardPilot::default();
        let context = PilotContext {
            active: true,
            running: true,
            can_control: true,
            mission_running: true,
            modal: false,
        };
        let effect = pilot.update(
            crate::controls::decode(1),
            context,
            scenario::settings().lander.max_accel_mps2,
        );
        assert!(apply_keyboard_effect(&mut simulation, &playback, effect));
        assert!(playback.running);
        let after = simulation.snapshot();
        let old_ship = before
            .assets
            .iter()
            .find(|actor| actor.id == before.mission.shuttle_id)
            .unwrap();
        let ship = after
            .assets
            .iter()
            .find(|actor| actor.id == after.mission.shuttle_id)
            .unwrap();
        assert_eq!(ship.position, old_ship.position);
        assert_eq!(ship.velocity, old_ship.velocity);
        assert_eq!(ship.mass_kg, old_ship.mass_kg);
        assert_eq!(ship.radius_m, old_ship.radius_m);
        assert_eq!(ship.max_thrust_n, old_ship.max_thrust_n);
        assert_eq!(ship.rotation, old_ship.rotation);
        assert_eq!(
            ship.commanded_accel_mps2,
            [scenario::settings().lander.max_accel_mps2, 0.0, 0.0]
        );
        simulation.advance(0.5);
        let moving = simulation.snapshot();
        assert!(moving.mission.fuel_used_kg > before.mission.fuel_used_kg);
        let velocity = moving
            .assets
            .iter()
            .find(|actor| actor.id == moving.mission.shuttle_id)
            .unwrap()
            .velocity;
        let release = pilot.update(
            crate::controls::decode(0),
            context,
            scenario::settings().lander.max_accel_mps2,
        );
        assert!(apply_keyboard_effect(&mut simulation, &playback, release));
        let stopped_thrust = simulation.snapshot();
        let ship = stopped_thrust
            .assets
            .iter()
            .find(|actor| actor.id == stopped_thrust.mission.shuttle_id)
            .unwrap();
        assert_eq!(
            ship.velocity, velocity,
            "key release stops thrust, not inertia"
        );
        assert_eq!(ship.commanded_accel_mps2, [0.0; 3]);
    }

    #[test]
    fn alt_only_camera_input_cannot_replace_an_external_ship_command() {
        let mut simulation = Simulation::new_default();
        simulation.set_command([0.0, 0.2, 0.0], [0.0; 3]).unwrap();
        let before = serde_json::to_string(&simulation.snapshot()).unwrap();
        let playback = PlaybackState {
            running: true,
            ..default()
        };
        let mut pilot = KeyboardPilot::default();
        let effect = pilot.update(
            crate::controls::decode(1 | (1 << 6)),
            PilotContext {
                active: true,
                running: true,
                can_control: true,
                mission_running: true,
                modal: false,
            },
            scenario::settings().lander.max_accel_mps2,
        );
        assert!(!apply_keyboard_effect(&mut simulation, &playback, effect));
        assert!(!pilot.owns_command());
        assert_eq!(
            before,
            serde_json::to_string(&simulation.snapshot()).unwrap()
        );
    }

    #[test]
    fn paused_or_view_only_keyboard_adapter_cannot_apply_ship_thrust() {
        for (running, can_control) in [(false, true), (true, false)] {
            let mut simulation = Simulation::new_default();
            simulation.set_command([0.0, 0.2, 0.0], [0.0; 3]).unwrap();
            let before = serde_json::to_string(&simulation.snapshot()).unwrap();
            let playback = PlaybackState {
                running,
                can_control,
                ..default()
            };
            assert!(!apply_keyboard_effect(
                &mut simulation,
                &playback,
                PilotEffect::SetCommand([0.5, 0.0, 0.0]),
            ));
            assert_eq!(
                before,
                serde_json::to_string(&simulation.snapshot()).unwrap()
            );
        }
    }
}
