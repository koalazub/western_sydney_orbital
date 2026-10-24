use bevy::prelude::*;

use crate::domain::{CameraMode, EntityId};

#[derive(Resource, Default)]
pub(crate) struct HudState {
    pub(super) panel: SidePanel,
    pub(super) reset_confirm_open: bool,
}

impl HudState {
    pub(crate) fn movement_blocked(&self) -> bool {
        self.reset_confirm_open
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(super) enum SidePanel {
    #[default]
    Closed,
    Details,
    Inspector,
    Controls,
}

impl SidePanel {
    pub fn details_open(self) -> bool {
        matches!(self, Self::Details | Self::Inspector)
    }

    pub fn toggle(self, requested: Self) -> Self {
        match (self, requested) {
            (Self::Details | Self::Inspector, Self::Details) | (Self::Controls, Self::Controls) => {
                Self::Closed
            }
            (Self::Inspector, Self::Inspector) => Self::Details,
            (_, requested) => requested,
        }
    }

    pub fn close_top(self) -> Self {
        match self {
            Self::Inspector => Self::Details,
            Self::Closed | Self::Details | Self::Controls => Self::Closed,
        }
    }
}

#[derive(Component)]
pub(super) struct HudRoot;

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
    DebugUpright,
    Export,
    CycleRate,
    Fullscreen,
    Maximize,
    ToggleCapture,
    ToggleDetails,
    ToggleControls,
    FollowLander,
    ToggleInspector,
    FocusNext,
    FocusPrevious,
    ChangeCamera(CameraMode),
}

#[derive(Component, Clone, Copy)]
pub(super) enum Readout {
    Static,
    Summary,
    PathBrief,
    PathDetails,
    FlightControls,
    TelemetryBrief,
    TelemetryDetails,
    ReferenceThrust,
    InputSettings,
    Mission,
    Environment,
    CameraMode,
    Focus,
    Camera,
    Inspector,
    Authority,
    Playback,
    Rate,
    Fullscreen,
    Maximize,
    Capture,
    DetailsToggle,
    ControlsToggle,
    InspectorToggle,
}

#[derive(Component, Clone, Copy)]
pub(super) enum Overlay {
    Details,
    Controls,
    Reset,
    Inspector,
}

#[derive(Component, Clone, Copy)]
pub(crate) enum LayoutPart {
    TopRow,
    BottomRow,
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

#[cfg(test)]
mod tests {
    use super::SidePanel::*;

    #[test]
    fn opening_a_panel_replaces_the_previous_panel() {
        for current in [Closed, Details, Inspector, Controls] {
            assert_eq!(
                current.toggle(Controls),
                if current == Controls {
                    Closed
                } else {
                    Controls
                }
            );
            assert_eq!(
                current.toggle(Details),
                if current.details_open() {
                    Closed
                } else {
                    Details
                }
            );
            assert_eq!(
                current.toggle(Inspector),
                if current == Inspector {
                    Details
                } else {
                    Inspector
                }
            );
        }
    }

    #[test]
    fn escape_closes_the_inspector_before_its_details_panel() {
        assert_eq!(Inspector.close_top(), Details);
        assert_eq!(Inspector.close_top().close_top(), Closed);
        assert_eq!(Controls.close_top(), Closed);
        assert_eq!(Closed.close_top(), Closed);
    }
}
