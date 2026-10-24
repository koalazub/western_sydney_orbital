use super::model::{HudAction, InteractionMode};
use crate::PlaybackState;

/// Availability shared by native button visuals and activation-time checks.
/// The caller snapshots interaction state; this policy reads no global state.
pub(super) fn action_enabled(
    action: HudAction,
    mode: InteractionMode,
    playback: &PlaybackState,
    terminal: bool,
) -> bool {
    let ready = match mode {
        InteractionMode::Ready => true,
        InteractionMode::ResetConfirmation => false,
        InteractionMode::Rebinding => return false,
    };
    match action {
        HudAction::TogglePlayback => ready && playback.can_control && !terminal,
        HudAction::Step => ready && playback.can_control && !playback.running && !terminal,
        HudAction::ConfirmReset => {
            mode == InteractionMode::ResetConfirmation && playback.can_control
        }
        HudAction::AskReset
        | HudAction::DebugUpright
        | HudAction::Export
        | HudAction::CycleRate => ready && playback.can_control,
        HudAction::CancelReset => true,
        HudAction::Fullscreen
        | HudAction::Notebook
        | HudAction::ChangeBinding(_)
        | HudAction::RestoreBindings
        | HudAction::Maximize
        | HudAction::ToggleCapture
        | HudAction::ToggleDetails
        | HudAction::ToggleControls
        | HudAction::FollowLander
        | HudAction::ToggleInspector
        | HudAction::FocusNext
        | HudAction::FocusPrevious
        | HudAction::ChangeCamera(_) => ready,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{controls::Action, domain::CameraMode};

    const ACTIONS: [HudAction; 22] = [
        HudAction::TogglePlayback,
        HudAction::Step,
        HudAction::AskReset,
        HudAction::ConfirmReset,
        HudAction::CancelReset,
        HudAction::DebugUpright,
        HudAction::Export,
        HudAction::CycleRate,
        HudAction::Fullscreen,
        HudAction::Notebook,
        HudAction::ChangeBinding(Action::Front),
        HudAction::RestoreBindings,
        HudAction::Maximize,
        HudAction::ToggleCapture,
        HudAction::ToggleDetails,
        HudAction::ToggleControls,
        HudAction::FollowLander,
        HudAction::ToggleInspector,
        HudAction::FocusNext,
        HudAction::FocusPrevious,
        HudAction::ChangeCamera(CameraMode::Orbit),
        HudAction::ChangeCamera(CameraMode::ShipTop),
    ];

    #[test]
    fn buttons_and_shortcuts_share_authority_and_modal_rules() {
        let mut playback = PlaybackState::default();
        let ready = InteractionMode::Ready;
        let reset = InteractionMode::ResetConfirmation;
        assert!(action_enabled(HudAction::Step, ready, &playback, false));
        assert!(!action_enabled(
            HudAction::ConfirmReset,
            ready,
            &playback,
            false
        ));
        playback.running = true;
        assert!(!action_enabled(HudAction::Step, ready, &playback, false));
        assert!(!action_enabled(
            HudAction::TogglePlayback,
            ready,
            &playback,
            true
        ));
        assert!(action_enabled(
            HudAction::DebugUpright,
            ready,
            &playback,
            true
        ));
        playback.running = false;
        assert!(!action_enabled(HudAction::Step, ready, &playback, true));
        playback.can_control = false;
        for action in [
            HudAction::TogglePlayback,
            HudAction::Step,
            HudAction::AskReset,
            HudAction::DebugUpright,
            HudAction::Export,
            HudAction::CycleRate,
        ] {
            assert!(
                !action_enabled(action, ready, &playback, false),
                "{action:?}"
            );
        }
        for action in [
            HudAction::ToggleControls,
            HudAction::FollowLander,
            HudAction::Notebook,
            HudAction::Fullscreen,
            HudAction::ChangeBinding(Action::Lift),
            HudAction::RestoreBindings,
        ] {
            assert!(action_enabled(action, ready, &playback, true), "{action:?}");
        }
        assert!(!action_enabled(
            HudAction::ConfirmReset,
            reset,
            &playback,
            false
        ));
        assert!(action_enabled(
            HudAction::CancelReset,
            reset,
            &playback,
            true
        ));
        playback.can_control = true;
        assert!(action_enabled(
            HudAction::ConfirmReset,
            reset,
            &playback,
            true
        ));
    }

    #[test]
    fn reset_and_rebinding_close_every_unrelated_action_path() {
        let playback = PlaybackState::default();
        for action in ACTIONS {
            assert!(
                !action_enabled(action, InteractionMode::Rebinding, &playback, false),
                "{action:?}"
            );
            assert_eq!(
                action_enabled(action, InteractionMode::ResetConfirmation, &playback, false),
                matches!(action, HudAction::ConfirmReset | HudAction::CancelReset),
                "{action:?}",
            );
        }
    }
}
