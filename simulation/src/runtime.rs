//! Pure playback-state transitions. The Bevy adapter performs the returned effects.
use crate::domain::{EngineAction, MissionStatus, PlaybackRate, ViewportAction};

#[derive(bevy::prelude::Resource, Clone, Debug, serde::Serialize)]
pub(crate) struct PlaybackState {
    pub running: bool,
    pub rate: PlaybackRate,
    pub can_control: bool,
    pub fullscreen: bool,
    pub maximized: bool,
    pub message: String,
    #[serde(skip)]
    pub(crate) accumulator: f64,
}

impl Default for PlaybackState {
    fn default() -> Self {
        Self {
            running: false,
            rate: PlaybackRate::RealTime,
            can_control: true,
            fullscreen: false,
            maximized: true,
            message: "WebGPU · Click the scene to activate ship and debug controls".into(),
            accumulator: 0.0,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Effect {
    None,
    Advance,
    Reset,
    DebugUpright,
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
            Effect::Reset
        }
        EngineAction::DebugUpright if state.can_control => {
            state.running = false;
            state.accumulator = 0.0;
            Effect::DebugUpright
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
        | EngineAction::DebugUpright
        | EngineAction::CycleRate => Effect::None,
    };
    Transition { state, effect }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_upright_reset_pauses_and_clears_time_without_reviving_terminal_missions() {
        for mission in [
            MissionStatus::Running,
            MissionStatus::FailedDamage,
            MissionStatus::Success,
        ] {
            let reset = transition(
                PlaybackState {
                    running: true,
                    rate: PlaybackRate::Tenfold,
                    accumulator: 0.04,
                    ..Default::default()
                },
                EngineAction::DebugUpright,
                mission,
            );
            assert!(!reset.state.running);
            assert_eq!(reset.state.accumulator, 0.0);
            assert_eq!(reset.state.rate, PlaybackRate::Tenfold);
            assert_eq!(reset.effect, Effect::DebugUpright);
            let denied = transition(
                PlaybackState {
                    running: true,
                    can_control: false,
                    accumulator: 0.04,
                    ..Default::default()
                },
                EngineAction::DebugUpright,
                mission,
            );
            assert!(denied.state.running);
            assert_eq!(denied.state.accumulator, 0.04);
            assert_eq!(denied.effect, Effect::None);
        }
    }

    #[test]
    fn rate_changes_do_not_start_playback_and_require_authority() {
        let state = PlaybackState {
            running: false,
            ..Default::default()
        };
        let changed = transition(
            state.clone(),
            EngineAction::CycleRate,
            MissionStatus::Running,
        );
        assert_eq!(changed.state.rate, PlaybackRate::Fivefold);
        assert!(!changed.state.running);
        assert_eq!(changed.effect, Effect::None);
        let locked = PlaybackState {
            can_control: false,
            ..state
        };
        let denied = transition(locked, EngineAction::CycleRate, MissionStatus::Running);
        assert_eq!(denied.state.rate, PlaybackRate::RealTime);
        assert!(!denied.state.running);
        assert_eq!(denied.effect, Effect::None);
    }

    #[test]
    fn pause_clears_pending_physics_time_even_without_control_authority() {
        let paused = transition(
            PlaybackState {
                running: true,
                can_control: false,
                accumulator: 0.04,
                ..Default::default()
            },
            EngineAction::Pause,
            MissionStatus::Running,
        );
        assert!(!paused.state.running);
        assert_eq!(paused.state.accumulator, 0.0);
        assert_eq!(paused.effect, Effect::None);
    }

    #[test]
    fn stepping_requires_paused_authorized_playback() {
        for (running, can_control) in [(false, true), (true, true), (false, false)] {
            let stepped = transition(
                PlaybackState {
                    running,
                    can_control,
                    ..Default::default()
                },
                EngineAction::Step,
                MissionStatus::Running,
            );
            assert_eq!(
                stepped.effect,
                if !running && can_control {
                    Effect::Advance
                } else {
                    Effect::None
                }
            );
            assert_eq!(stepped.state.running, running);
        }
    }
}
