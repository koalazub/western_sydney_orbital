//! Imperative adapter for the pure playback transition and physical world.
use crate::{
    api::{CommandContext, CommandError, Dispatch},
    controls::{KeyboardPilot, Movement, MovementTarget, PilotContext, PilotEffect},
    domain::{EngineAction, MissionStatus, PlaybackRate, ViewportAction},
    model::Snapshot,
    physics::{PhysicsCommand, Simulation},
    runtime::{self, Effect, PlaybackState},
    scenario,
};
use bevy::input_focus::InputFocus;
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

#[derive(Clone, Copy, Debug)]
enum QueuedAction {
    Native(EngineAction),
    CheckedPlay {
        rate: PlaybackRate,
        expected: CommandContext,
    },
}

impl QueuedAction {
    fn engine_action(self) -> EngineAction {
        match self {
            Self::Native(action) => action,
            Self::CheckedPlay { rate, .. } => EngineAction::Play(rate),
        }
    }
}

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
    static ACTIONS: RefCell<VecDeque<QueuedAction>> = const { RefCell::new(VecDeque::new()) };
    static RUNTIME: RefCell<PlaybackState> = RefCell::new(PlaybackState::default());
    static VIEWPORT: RefCell<ViewportState> = RefCell::new(ViewportState::default());
    static PUBLISHED: RefCell<Option<Rc<Snapshot>>> = const { RefCell::new(None) };
    static KEYBOARD_PILOT: RefCell<KeyboardPilot> = RefCell::new(KeyboardPilot::default());
    static SPACE_HELD: Cell<bool> = const { Cell::new(false) };
    static TRIAL_REVISION: Cell<u32> = const { Cell::new(0) };
}

pub(crate) fn request_action(action: EngineAction) {
    ACTIONS.with(|queue| {
        let mut queue = queue.borrow_mut();
        if queue.len() < MAX_ACTIONS {
            queue.push_back(QueuedAction::Native(action));
        }
    });
}

/// The explicit placement button pauses playback without replacing the world.
/// Return the same paused state for Bevy's resource before its next runtime pass.
pub(crate) fn pause_for_launchpad_placement() -> Result<PlaybackState, String> {
    let playback = RUNTIME.with(|runtime| runtime.borrow().clone());
    require_control(
        &playback,
        "Lander placement is unavailable in view-only mode. Request control first.",
    )?;
    ACTIONS.with(|queue| {
        let mut actions = queue.borrow_mut();
        if actions.iter().any(|action| {
            matches!(
                action.engine_action(),
                EngineAction::TogglePlayback
                    | EngineAction::Play(_)
                    | EngineAction::Step
                    | EngineAction::Reset
                    | EngineAction::DebugUpright
            )
        }) {
            return Err("Let queued flight actions finish, then choose Place lander again.".into());
        }
        require_pause_slot(
            &actions,
            "Let queued actions finish, then choose Place lander again.",
        )?;
        let mission = SIMULATION.with(|sim| sim.borrow().mission_status());
        let paused = runtime::transition(playback, EngineAction::Pause, mission).state;
        actions.push_back(QueuedAction::Native(EngineAction::Pause));
        RUNTIME.with(|runtime| *runtime.borrow_mut() = paused.clone());
        Ok(paused)
    })
}

#[cfg(test)]
pub(crate) fn queued_actions_for_tests() -> usize {
    ACTIONS.with(|queue| queue.borrow().len())
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
    let mut report = SIMULATION.with(|sim| sim.borrow().observation());
    report["trial_revision"] = serde_json::json!(trial_revision());
    report
}

pub(crate) fn trial_revision() -> u32 {
    TRIAL_REVISION.with(Cell::get)
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
            "trial_revision": trial_revision(),
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

pub(crate) fn is_running() -> bool {
    RUNTIME.with(|state| state.borrow().running)
}

pub(crate) fn block_keyboard_until_release() {
    KEYBOARD_PILOT.with(|pilot| pilot.borrow_mut().block_until_release());
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
        Dispatch::Physics(command) => dispatch_physics(command),
        Dispatch::CheckedCommand {
            accel,
            angular_accel,
            expected_context,
        } => dispatch_checked(accel, angular_accel, expected_context)
            .map_err(|error| error.to_string()),
        Dispatch::CheckedPlay {
            rate,
            expected_context,
        } => dispatch_play_checked(rate, expected_context).map_err(|error| error.to_string()),
        Dispatch::CheckedLaunchpad {
            surface_direction,
            expected_context,
        } => dispatch_launchpad_checked(surface_direction, expected_context)
            .map_err(|error| error.to_string()),
    }
}

/// The check and real command execute synchronously in the same engine call.
/// Native edits cannot slip between a Python read and an unchecked write.
pub(crate) fn dispatch_checked(
    accel: [f64; 3],
    angular_accel: [f64; 3],
    expected: CommandContext,
) -> Result<Rc<Snapshot>, CommandError> {
    let playback = RUNTIME.with(|runtime| runtime.borrow().clone());
    validate_context(expected, &playback)?;
    if ACTIONS.with(|actions| pending_flight_action(&actions.borrow(), playback.running)) {
        return Err(CommandError::StaleContext);
    }
    dispatch_physics(PhysicsCommand::SetCommand {
        accel,
        angular_accel,
    })
    .map_err(CommandError::Rejected)
}

fn validate_context(
    expected: CommandContext,
    playback: &PlaybackState,
) -> Result<(), CommandError> {
    validate_setup_context(expected, playback)?;
    if SIMULATION.with(|simulation| simulation.borrow().mission_status().is_terminal())
        || keyboard_owns_command()
    {
        return Err(CommandError::StaleContext);
    }
    Ok(())
}

fn validate_setup_context(
    expected: CommandContext,
    playback: &PlaybackState,
) -> Result<(), CommandError> {
    let state = PUBLISHED
        .with(|published| published.borrow().clone())
        .unwrap_or_else(|| Rc::new(SIMULATION.with(|simulation| simulation.borrow().snapshot())));
    if expected.trial_revision != trial_revision()
        || expected.tuning_revision != state.tuning_revision
        || expected.manual_interventions != state.manual_interventions
        || expected.seed != state.seed
        || expected.running != playback.running
        || !playback.can_control
    {
        return Err(CommandError::StaleContext);
    }
    Ok(())
}

fn pending_flight_action(actions: &VecDeque<QueuedAction>, running: bool) -> bool {
    actions.iter().any(|action| match action.engine_action() {
        EngineAction::Viewport(_) | EngineAction::CycleRate => false,
        // A successful explicit setup commits the paused mirror before
        // its queued Pause reaches Bevy. That no-op pause is already
        // represented by the observed context; a pending pause of a
        // running flight is not.
        EngineAction::Pause => running,
        EngineAction::TogglePlayback
        | EngineAction::Play(_)
        | EngineAction::Step
        | EngineAction::Reset
        | EngineAction::DebugUpright => true,
    })
}

/// Check before queueing and again at the runtime frame, when Play takes effect.
/// A tuning/setup change while waiting must not revive an old controller flight.
pub(crate) fn dispatch_play_checked(
    rate: PlaybackRate,
    expected: CommandContext,
) -> Result<Rc<Snapshot>, CommandError> {
    let playback = RUNTIME.with(|runtime| runtime.borrow().clone());
    validate_context(expected, &playback)?;
    ACTIONS.with(|actions| {
        let mut actions = actions.borrow_mut();
        if pending_flight_action(&actions, playback.running) {
            return Err(CommandError::StaleContext);
        }
        require_pause_slot(
            &actions,
            "Let queued actions finish before starting playback",
        )
        .map_err(CommandError::Rejected)?;
        actions.push_back(QueuedAction::CheckedPlay { rate, expected });
        Ok(snapshot())
    })
}

/// A surface gesture is a paused local intervention, never an implicit reset.
/// Reject a changed trial or pending flight action before touching either body.
pub(crate) fn dispatch_launchpad_checked(
    surface_direction: [f64; 3],
    expected: CommandContext,
) -> Result<Rc<Snapshot>, CommandError> {
    let playback = RUNTIME.with(|runtime| runtime.borrow().clone());
    validate_setup_context(expected, &playback)?;
    if ACTIONS.with(|actions| pending_flight_action(&actions.borrow(), playback.running)) {
        return Err(CommandError::StaleContext);
    }
    dispatch_physics(PhysicsCommand::MoveLaunchpad { surface_direction })
        .map_err(CommandError::Rejected)
}

/// Successful execution determines which input and playback state can be committed.
enum PhysicsExecution {
    KeyboardUnchanged(Snapshot),
    ExternalCommand(Snapshot),
    PausedTrial(Snapshot),
}

/// Typed native readback from the same Rapier world used by the public API.
pub(crate) fn applied_physics() -> Result<crate::model::AppliedPhysics, String> {
    SIMULATION.with(|simulation| simulation.borrow().applied_physics())
}

/// Native debug field measurements, never an SDK sensor or planner input.
pub(crate) fn debris_dynamics() -> Result<crate::model::DebrisDynamicsReport, String> {
    SIMULATION.with(|simulation| simulation.borrow().debris_dynamics())
}

pub(crate) fn keyboard_owns_command() -> bool {
    KEYBOARD_PILOT.with(|pilot| pilot.borrow().owns_command())
}

pub(crate) fn raycast(
    query: &crate::model::LookaheadQuery,
) -> Result<crate::model::LookaheadReport, String> {
    SIMULATION.with(|simulation| simulation.borrow().raycast(query))
}

fn dispatch_physics(command: PhysicsCommand) -> Result<Rc<Snapshot>, String> {
    let playback = RUNTIME.with(|state| state.borrow().clone());
    ACTIONS.with(|queue| {
        // Keep the checked pause slot available through execution and commit.
        let mut actions = queue.borrow_mut();
        let execution = execute_physics(command, &playback, &actions)?;
        // No session side effects occur until physics has succeeded. Commit cannot fail.
        Ok(commit_physics(execution, playback, &mut actions))
    })
}

fn execute_physics(
    command: PhysicsCommand,
    playback: &PlaybackState,
    actions: &VecDeque<QueuedAction>,
) -> Result<PhysicsExecution, String> {
    let execute = |command| SIMULATION.with(|sim| sim.borrow_mut().execute(command));
    match command {
        command @ PhysicsCommand::Observe => {
            Ok(PhysicsExecution::KeyboardUnchanged(execute(command)?))
        }
        command @ (PhysicsCommand::TuneShip { .. } | PhysicsCommand::TuneDebrisFlow { .. }) => {
            require_control(playback, "Live tuning is unavailable in view-only mode")?;
            // Tuning is neither a flight-command handoff nor a new trial. Keep
            // playback, pending actions and the existing command writer intact.
            Ok(PhysicsExecution::KeyboardUnchanged(execute(command)?))
        }
        command @ PhysicsCommand::Step { .. } => {
            if playback.running {
                return Err("Pause the mission before stepping manually".into());
            }
            Ok(PhysicsExecution::KeyboardUnchanged(execute(command)?))
        }
        command @ (PhysicsCommand::SetCommand { .. } | PhysicsCommand::Stop) => {
            Ok(PhysicsExecution::ExternalCommand(execute(command)?))
        }
        command @ (PhysicsCommand::Reset { .. } | PhysicsCommand::Restore { .. }) => {
            require_trial_revision_capacity()?;
            require_pause_slot(
                actions,
                "Let queued actions finish before resetting or restoring the mission",
            )?;
            Ok(PhysicsExecution::PausedTrial(execute(command)?))
        }
        command @ PhysicsCommand::DebugUpright => {
            require_trial_revision_capacity()?;
            require_control(
                playback,
                "Upright lander is unavailable in view-only mode. Request control first.",
            )?;
            require_pause_slot(
                actions,
                "Upright lander is waiting for queued actions. Let the next frame finish, then retry.",
            )?;
            Ok(PhysicsExecution::PausedTrial(execute(command)?))
        }
        command @ (PhysicsCommand::ConfigureExperiment { .. }
        | PhysicsCommand::ConfigureFlight { .. }) => {
            require_trial_revision_capacity()?;
            require_control(
                playback,
                "World and ship settings are unavailable in view-only mode",
            )?;
            require_pause_slot(
                actions,
                "Let queued actions finish before applying experiment settings",
            )?;
            Ok(PhysicsExecution::PausedTrial(execute(command)?))
        }
        command @ PhysicsCommand::PlaceLander { .. } => {
            require_control(
                playback,
                "Lander placement is unavailable in view-only mode",
            )?;
            require_trial_revision_capacity()?;
            require_pause_slot(
                actions,
                "Let queued actions finish before placing the lander",
            )?;
            Ok(PhysicsExecution::PausedTrial(execute(command)?))
        }
        command @ PhysicsCommand::MoveLaunchpad { .. } => {
            require_control(
                playback,
                "Launchpad movement is unavailable in view-only mode. Request control first.",
            )?;
            if playback.running {
                return Err("Pause the simulation before moving the launchpad.".into());
            }
            if pending_flight_action(actions, playback.running) {
                return Err(
                    "The flight has queued actions. Let them finish before moving the launchpad."
                        .into(),
                );
            }
            require_trial_revision_capacity()?;
            require_pause_slot(
                actions,
                "Let queued actions finish before moving the launchpad",
            )?;
            Ok(PhysicsExecution::PausedTrial(execute(command)?))
        }
    }
}

fn require_control(playback: &PlaybackState, message: &str) -> Result<(), String> {
    if playback.can_control {
        Ok(())
    } else {
        Err(message.into())
    }
}

fn require_trial_revision_capacity() -> Result<(), String> {
    if trial_revision() == u32::MAX {
        Err("Local trial revision exhausted. Save the flight and reload the game before another test setup.".into())
    } else {
        Ok(())
    }
}

fn note_trial_change() {
    TRIAL_REVISION.with(|revision| revision.set(revision.get() + 1));
    crate::navigation::clear_for_trial();
}

fn require_pause_slot(actions: &VecDeque<QueuedAction>, message: &str) -> Result<(), String> {
    if actions.len() < MAX_ACTIONS {
        Ok(())
    } else {
        Err(message.into())
    }
}

fn commit_physics(
    execution: PhysicsExecution,
    playback: PlaybackState,
    actions: &mut VecDeque<QueuedAction>,
) -> Rc<Snapshot> {
    let state = match execution {
        PhysicsExecution::KeyboardUnchanged(state) => state,
        PhysicsExecution::ExternalCommand(state) => {
            supersede_keyboard_command();
            state
        }
        PhysicsExecution::PausedTrial(state) => {
            commit_paused_trial(&state, playback, actions);
            state
        }
    };
    publish(state)
}

fn supersede_keyboard_command() {
    // The external operation already replaced our force. A later key release
    // must not zero it or revive a key held across the handoff.
    KEYBOARD_PILOT.with(|pilot| pilot.borrow_mut().cancel_until_release());
}

fn commit_paused_trial(
    state: &Snapshot,
    playback: PlaybackState,
    actions: &mut VecDeque<QueuedAction>,
) {
    note_trial_change();
    // This successful API commit supersedes all previously queued flight
    // actions, including old Pause. Window actions and rate changes keep their
    // order. A new Play requested after this commit can resume the paused trial.
    actions.retain(|action| match action.engine_action() {
        EngineAction::Viewport(_) | EngineAction::CycleRate => true,
        EngineAction::TogglePlayback
        | EngineAction::Play(_)
        | EngineAction::Pause
        | EngineAction::Step
        | EngineAction::Reset
        | EngineAction::DebugUpright => false,
    });
    // The slot was checked before execution. Replace old pauses with exactly
    // one Pause that reaches the playback runtime before any later flight action.
    actions.push_front(QueuedAction::Native(EngineAction::Pause));
    supersede_keyboard_command();
    let paused = runtime::transition(playback, EngineAction::Pause, state.mission.status).state;
    RUNTIME.with(|runtime| *runtime.borrow_mut() = paused);
}

pub(crate) fn advance_world(
    time: Res<Time>,
    keys: Res<ButtonInput<KeyCode>>,
    hud: Res<crate::hud::HudState>,
    mut state: ResMut<PlaybackState>,
    input_focus: Option<Res<InputFocus>>,
    editors: Query<(), With<crate::hud::InstrumentEditor>>,
    surface_controls: Query<(), With<crate::launchpad::SurfaceControl>>,
) {
    let editor_focused = input_focus
        .as_ref()
        .and_then(|focus| focus.get())
        .is_some_and(|entity| editors.contains(entity));
    let surface_control_focused = input_focus
        .as_ref()
        .and_then(|focus| focus.get())
        .is_some_and(|entity| surface_controls.contains(entity));
    let movement = crate::controls::movement(&keys);
    // Movement observes physical key-up and may finish rebinding's release
    // guard. Read the resulting HUD gate before updating the real pilot.
    let movement_blocked = hud.movement_blocked()
        || editor_focused
        || surface_control_focused
        || crate::launchpad::input_blocked();
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
            action.engine_action(),
            EngineAction::Pause
                | EngineAction::TogglePlayback
                | EngineAction::Reset
                | EngineAction::DebugUpright
                | EngineAction::Step
        )
    });
    for queued in actions {
        let action = match queued {
            QueuedAction::Native(action) => action,
            QueuedAction::CheckedPlay { rate, expected } => {
                if let Err(error) = validate_context(expected, &state) {
                    state.message = error.to_string();
                    continue;
                }
                EngineAction::Play(rate)
            }
        };
        let status = SIMULATION.with(|sim| sim.borrow().mission_status());
        let transition = runtime::transition(state.clone(), action, status);
        *state = transition.state;
        match transition.effect {
            Effect::None => {}
            Effect::Advance => {
                // A queued pause/step must not advance with stale keyboard thrust.
                update_keyboard_pilot(&state, movement, movement_blocked);
                SIMULATION.with(|sim| {
                    sim.borrow_mut()
                        .advance(scenario::settings().integration.fixed_step_s)
                });
                refresh();
            }
            Effect::Reset => {
                let seed = snapshot().seed;
                let result = require_trial_revision_capacity()
                    .and_then(|()| SIMULATION.with(|sim| sim.borrow_mut().reset(seed)));
                match result {
                    Ok(()) => {
                        note_trial_change();
                        KEYBOARD_PILOT.with(|pilot| pilot.borrow_mut().cancel_until_release());
                        refresh();
                    }
                    Err(error) => state.message = error,
                }
            }
            Effect::DebugUpright => {
                match require_trial_revision_capacity().and_then(|()| {
                    SIMULATION.with(|sim| sim.borrow_mut().execute(PhysicsCommand::DebugUpright))
                }) {
                    Ok(snapshot) => {
                        note_trial_change();
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
        && !movement_blocked
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
    update_keyboard_pilot(&state, movement, movement_blocked);
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
                modal: modal || crate::controls::manual_input_blocked(),
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
mod dispatch_tests;

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
            [-scenario::settings().lander.max_accel_mps2, 0.0, 0.0]
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
