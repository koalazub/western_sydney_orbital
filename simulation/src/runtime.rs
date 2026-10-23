//! Pure playback-state transitions. The Bevy adapter performs the returned effects.
use crate::domain::{EngineAction, MissionStatus, PlaybackRate, ViewportAction};

#[derive(bevy::prelude::Resource, Clone, Debug, serde::Serialize)]
pub(crate) struct PlaybackState {
    pub running: bool,
    pub autopilot: bool,
    pub rate: PlaybackRate,
    pub can_control: bool,
    pub fullscreen: bool,
    pub maximized: bool,
    pub message: String,
    #[serde(skip)]
    pub(crate) accumulator: f64,
    #[serde(skip)]
    pub(crate) guidance_elapsed: f64,
}

impl Default for PlaybackState {
    fn default() -> Self {
        Self {
            running: false,
            autopilot: true,
            rate: PlaybackRate::RealTime,
            can_control: true,
            fullscreen: false,
            maximized: true,
            message: "WebGPU · Click the scene to activate ship and debug controls".into(),
            accumulator: 0.0,
            guidance_elapsed: f64::MAX,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Effect {
    None,
    Advance,
    Reset,
    ZeroCommand,
    Viewport(ViewportAction),
}

pub(crate) struct Transition {
    pub state: PlaybackState,
    pub effect: Effect,
}

pub(crate) fn transition(
    mut state: PlaybackState,
    action: EngineAction,
    mission: MissionStatus,
) -> Transition {
    let effect = match action {
        EngineAction::TogglePlayback if state.can_control => {
            state.running = !state.running && mission == MissionStatus::Running;
            Effect::None
        }
        EngineAction::Play(rate) if state.can_control => {
            state.rate = rate;
            state.running = mission == MissionStatus::Running;
            Effect::None
        }
        EngineAction::Pause => {
            state.running = false;
            state.accumulator = 0.0;
            state.guidance_elapsed = f64::MAX;
            Effect::None
        }
        EngineAction::Step
            if state.can_control && !state.running && mission == MissionStatus::Running =>
        {
            Effect::Advance
        }
        EngineAction::Reset if state.can_control => {
            state.running = false;
            state.accumulator = 0.0;
            state.guidance_elapsed = f64::MAX;
            Effect::Reset
        }
        EngineAction::ToggleAutopilot if state.can_control => {
            state.autopilot = !state.autopilot;
            state.guidance_elapsed = f64::MAX;
            if state.autopilot {
                Effect::None
            } else {
                Effect::ZeroCommand
            }
        }
        EngineAction::ManualControl => {
            state.autopilot = false;
            Effect::None
        }
        EngineAction::CycleRate if state.can_control => {
            state.rate = state.rate.next();
            Effect::None
        }
        EngineAction::Viewport(action) => Effect::Viewport(action),
        EngineAction::TogglePlayback
        | EngineAction::Play(_)
        | EngineAction::Step
        | EngineAction::Reset
        | EngineAction::ToggleAutopilot
        | EngineAction::CycleRate => Effect::None,
    };
    Transition { state, effect }
}
