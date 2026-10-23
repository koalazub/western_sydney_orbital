use bevy::prelude::*;

use crate::domain::{CameraMode, EntityId};

#[derive(Resource, Default)]
pub(super) struct HudState {
    pub controls: ControlsState,
    pub help_open: bool,
    pub inspector_open: bool,
    pub reset_confirm_open: bool,
}

#[derive(Clone, Copy, Default, PartialEq, Eq)]
pub(super) enum ControlsState {
    #[default]
    Compact,
    Expanded,
}

impl ControlsState {
    pub fn expanded(self) -> bool {
        matches!(self, Self::Expanded)
    }
}

#[derive(Component)]
pub(super) struct DetailsPanel;

#[derive(Component)]
pub(super) struct FullscreenButton;

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
    ToggleControls,
    ToggleHelp,
    ToggleInspector,
    FocusNext,
    FocusPrevious,
    ChangeCamera(CameraMode),
}

#[derive(Component, Clone, Copy)]
pub(super) enum Readout {
    Static,
    Summary,
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
    Controls,
    HelpToggle,
    InspectorToggle,
}

#[derive(Component, Clone, Copy)]
pub(super) enum Overlay {
    Controls,
    Help,
    Reset,
    Inspector,
}

#[derive(Component, Clone, Copy)]
pub(crate) enum LayoutPart {
    TopRow,
    MissionActions,
    BottomRow,
    MetricsPanel,
    CameraPanel,
    SensorActions,
}

pub(crate) const SCREEN_PADDING_PX: f32 = 6.0;
pub(crate) const PANEL_GAP_PX: f32 = 4.0;
pub(super) const CONTROL_GAP_PX: f32 = 4.0;
pub(super) const PANEL_PADDING_X_PX: f32 = 6.0;
pub(super) const PANEL_PADDING_Y_PX: f32 = 4.0;
pub(super) const BUTTON_HEIGHT_PX: f32 = 28.0;
pub(super) const BUTTON_PADDING_X_PX: f32 = 7.0;
pub(super) const BUTTON_PADDING_Y_PX: f32 = 4.0;
pub(super) const COPY_SIZE_PX: u32 = 12;
pub(super) const CAPTION_SIZE_PX: u32 = 11;

#[derive(Clone, Copy)]
pub(super) struct FocusItem<'a> {
    pub id: EntityId,
    pub label: &'a str,
    pub entity: Option<&'a crate::model::EntityState>,
}

pub(super) const PANEL: Color = Color::srgba(0.025, 0.045, 0.065, 0.92);
pub(super) const MODAL_SCRIM: Color = Color::srgba(0.0, 0.0, 0.0, 0.65);
pub(super) const BUTTON: Color = Color::srgba(0.12, 0.19, 0.25, 0.98);
pub(super) const SELECTED: Color = Color::srgba(0.12, 0.34, 0.42, 0.98);
pub(super) const FOCUSED: Color = Color::srgba(0.19, 0.46, 0.57, 1.0);
pub(super) const DISABLED: Color = Color::srgba(0.07, 0.09, 0.11, 0.88);
pub(super) const HOVERED: Color = Color::srgba(0.18, 0.31, 0.38, 1.0);
pub(super) const TEXT: Color = Color::srgb(0.91, 0.95, 0.97);
pub(super) const MUTED: Color = Color::srgb(0.68, 0.77, 0.82);
