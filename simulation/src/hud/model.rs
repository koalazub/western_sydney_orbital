use bevy::prelude::*;

use crate::domain::{CameraMode, EntityId};

#[derive(Resource, Default)]
pub(super) struct HudState {
    pub help_open: bool,
    pub inspector_open: bool,
    pub reset_confirm_open: bool,
}

#[derive(Default)]
pub(super) struct ModalFocus {
    pub open: bool,
    pub previous: Option<Entity>,
}

#[derive(Component, Clone, Copy)]
pub(super) enum HudAction {
    TogglePlayback,
    Step,
    AskReset,
    ConfirmReset,
    CancelReset,
    Export,
    CycleRate,
    ToggleAutopilot,
    Fullscreen,
    Maximize,
    ToggleCapture,
    ToggleHelp,
    ToggleInspector,
    FocusNext,
    FocusPrevious,
    ChangeCamera(CameraMode),
}

#[derive(Component, Clone, Copy)]
pub(super) enum Readout {
    Static,
    Mission,
    Environment,
    CameraMode,
    Focus,
    Camera,
    Inspector,
    Authority,
    Playback,
    Rate,
    Autopilot,
    Fullscreen,
    Maximize,
    Capture,
}

#[derive(Component, Clone, Copy)]
pub(super) enum Overlay {
    Help,
    Reset,
    Inspector,
}

#[derive(Component, Clone, Copy)]
pub(super) enum LayoutPart {
    TopRow,
    MissionActions,
    BottomRow,
    MetricsPanel,
    CameraPanel,
}

#[derive(Clone, Copy)]
pub(super) struct FocusItem<'a> {
    pub id: EntityId,
    pub label: &'a str,
    pub entity: Option<&'a crate::model::EntityState>,
}

pub(super) const PANEL: Color = Color::srgba(0.025, 0.045, 0.065, 0.92);
pub(super) const BUTTON: Color = Color::srgba(0.12, 0.19, 0.25, 0.98);
pub(super) const SELECTED: Color = Color::srgba(0.12, 0.34, 0.42, 0.98);
pub(super) const FOCUSED: Color = Color::srgba(0.19, 0.46, 0.57, 1.0);
pub(super) const DISABLED: Color = Color::srgba(0.07, 0.09, 0.11, 0.88);
pub(super) const HOVERED: Color = Color::srgba(0.18, 0.31, 0.38, 1.0);
pub(super) const TEXT: Color = Color::srgb(0.91, 0.95, 0.97);
pub(super) const MUTED: Color = Color::srgb(0.68, 0.77, 0.82);
