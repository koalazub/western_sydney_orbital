//! Local launch setup. Previews never write to the physical world.
//!
//! The only write is the session's checked MoveLaunchpad adapter, after a valid
//! drag release or an explicit confirmation. This is a recorded intervention,
//! not a new world, flight reset or automatic flight controller.
mod geometry;
mod model;
mod systems;

#[cfg(all(test, not(target_arch = "wasm32")))]
mod tests;

use bevy::prelude::*;
use std::{cell::RefCell, collections::VecDeque};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum LaunchpadAction {
    PlaceOnSurface,
    Confirm,
    Cancel,
    FramePhobos,
    Nudge(SurfaceStep),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum SurfaceStep {
    North,
    South,
    East,
    West,
}

/// Setup buttons consume flight keys while keyboard-focused, without pretending
/// to be text editors or blocking their own placement actions.
#[derive(Component)]
pub(crate) struct SurfaceControl;

const ACTION_LIMIT: usize = 32;

#[derive(SystemSet, Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub(crate) enum LaunchpadSystems {
    Input,
}

#[derive(Default)]
struct Bridge {
    actions: VecDeque<LaunchpadAction>,
    blocked: bool,
    status: String,
    preview_direction: Option<[f64; 3]>,
}

thread_local! {
    static BRIDGE: RefCell<Bridge> = RefCell::new(Bridge::default());
}

/// Queue an intent only. Validation and any checked commit run in EngineSet::Input.
pub(crate) fn request(action: LaunchpadAction) {
    BRIDGE.with(|bridge| {
        let mut bridge = bridge.borrow_mut();
        // A cancellation queued before the next input frame must also cancel a
        // pending Confirm, rather than run after that Confirm has already moved.
        if action == LaunchpadAction::Cancel {
            bridge.actions.clear();
        }
        if bridge.actions.len() < ACTION_LIMIT {
            bridge.actions.push_back(action);
        }
    });
}

pub(crate) fn summary() -> String {
    BRIDGE.with(|bridge| {
        let bridge = bridge.borrow();
        if bridge.status.is_empty() {
            "Pause, then drag the pad or lift grip. Or use Choose surface point.".into()
        } else {
            bridge.status.clone()
        }
    })
}

/// Includes queued edit intents, armed placement and the final key/pointer-up frame.
pub(crate) fn input_blocked() -> bool {
    BRIDGE.with(|bridge| {
        let bridge = bridge.borrow();
        bridge.blocked
            || bridge
                .actions
                .iter()
                .any(|action| *action != LaunchpadAction::FramePhobos)
    })
}

/// Camera input uses the same release guard as ship input. Explicit view requests
/// still reach the camera controller while a placement is active.
pub(crate) fn gesture_active() -> bool {
    input_blocked()
}

/// A deliberate Frame Phobos request can also reveal the pending preview.
pub(crate) fn frame_direction() -> Option<[f64; 3]> {
    BRIDGE.with(|bridge| bridge.borrow().preview_direction)
}

pub(crate) fn install(app: &mut App) {
    BRIDGE.with(|bridge| *bridge.borrow_mut() = Bridge::default());
    systems::install(app);
}

#[cfg(test)]
pub(crate) fn reset_for_tests() {
    BRIDGE.with(|bridge| *bridge.borrow_mut() = Bridge::default());
}
