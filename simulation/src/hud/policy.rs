use super::model::{HudAction, InteractionMode, LaunchpadControl};
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
        InteractionMode::Ready | InteractionMode::LaunchpadPlacement => true,
        InteractionMode::ResetConfirmation => false,
        InteractionMode::Rebinding => return false,
    };
    match action {
        HudAction::TogglePlayback => {
            mode == InteractionMode::Ready && playback.can_control && !terminal
        }
        HudAction::Step => {
            mode == InteractionMode::Ready && playback.can_control && !playback.running && !terminal
        }
        HudAction::ConfirmReset => {
            mode == InteractionMode::ResetConfirmation && playback.can_control
        }
        HudAction::AskReset
        | HudAction::DebugUpright
        | HudAction::Export
        | HudAction::CycleRate => mode == InteractionMode::Ready && playback.can_control,
        HudAction::Launchpad(control) => match control {
            LaunchpadControl::FramePhobos => ready,
            LaunchpadControl::ChooseSurface => ready && playback.can_control && !playback.running,
            LaunchpadControl::Apply
            | LaunchpadControl::North
            | LaunchpadControl::South
            | LaunchpadControl::East
            | LaunchpadControl::West => {
                mode == InteractionMode::LaunchpadPlacement
                    && playback.can_control
                    && !playback.running
            }
            LaunchpadControl::Cancel => mode == InteractionMode::LaunchpadPlacement,
        },
        HudAction::CancelReset => true,
        HudAction::Fullscreen
        | HudAction::Notebook
        | HudAction::ChangeBinding(_)
        | HudAction::RestoreBindings
        | HudAction::Maximize
        | HudAction::ToggleCapture
        | HudAction::ToggleDetails
        | HudAction::ToggleInstruments
        | HudAction::ToggleShip
        | HudAction::ToggleDebris
        | HudAction::ToggleLaunch
        | HudAction::ToggleCamera
        | HudAction::CycleIllumination
        | HudAction::ToggleStars
        | HudAction::ToggleGuides
        | HudAction::ToggleInstrumentSection(_)
        | HudAction::ToggleReference(_)
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

    const ACTIONS: &[HudAction] = &[
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
        HudAction::ToggleInstruments,
        HudAction::ToggleShip,
        HudAction::ToggleDebris,
        HudAction::ToggleLaunch,
        HudAction::ToggleCamera,
        HudAction::CycleIllumination,
        HudAction::ToggleStars,
        HudAction::ToggleGuides,
        HudAction::ToggleInstrumentSection(crate::telemetry::InstrumentGroup::Actuation),
        HudAction::ToggleReference(super::super::model::ReferenceSection::ControlGuide),
        HudAction::Launchpad(LaunchpadControl::FramePhobos),
        HudAction::Launchpad(LaunchpadControl::ChooseSurface),
        HudAction::Launchpad(LaunchpadControl::Apply),
        HudAction::Launchpad(LaunchpadControl::Cancel),
        HudAction::Launchpad(LaunchpadControl::North),
        HudAction::Launchpad(LaunchpadControl::South),
        HudAction::Launchpad(LaunchpadControl::East),
        HudAction::Launchpad(LaunchpadControl::West),
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
        for &action in ACTIONS {
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

    #[test]
    fn surface_nudges_are_paused_preview_actions_not_flight_commands() {
        let mut playback = PlaybackState::default();
        let ready = InteractionMode::Ready;
        let placement = InteractionMode::LaunchpadPlacement;
        assert!(action_enabled(
            HudAction::Launchpad(LaunchpadControl::ChooseSurface),
            ready,
            &playback,
            true
        ));
        for control in [
            LaunchpadControl::Apply,
            LaunchpadControl::North,
            LaunchpadControl::South,
            LaunchpadControl::East,
            LaunchpadControl::West,
        ] {
            assert!(!action_enabled(
                HudAction::Launchpad(control),
                ready,
                &playback,
                false
            ));
            assert!(action_enabled(
                HudAction::Launchpad(control),
                placement,
                &playback,
                true
            ));
            playback.running = true;
            assert!(!action_enabled(
                HudAction::Launchpad(control),
                placement,
                &playback,
                false
            ));
            playback.running = false;
        }
        for action in [
            HudAction::TogglePlayback,
            HudAction::Step,
            HudAction::AskReset,
            HudAction::DebugUpright,
            HudAction::CycleRate,
        ] {
            assert!(!action_enabled(action, placement, &playback, false));
        }
        playback.can_control = false;
        assert!(!action_enabled(
            HudAction::Launchpad(LaunchpadControl::ChooseSurface),
            ready,
            &playback,
            false
        ));
        assert!(action_enabled(
            HudAction::Launchpad(LaunchpadControl::FramePhobos),
            ready,
            &playback,
            false
        ));
        assert!(action_enabled(
            HudAction::Launchpad(LaunchpadControl::Cancel),
            placement,
            &playback,
            false
        ));
    }
}
