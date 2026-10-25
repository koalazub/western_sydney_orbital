use bevy::prelude::*;

use crate::domain::{CameraMode, EntityId};
use crate::telemetry::{Instrument, InstrumentGroup};
use std::collections::HashSet;

#[derive(Resource)]
pub(crate) struct HudState {
    pub(super) panel: SidePanel,
    pub(super) reset_confirm_open: bool,
    pub(super) expanded_instruments: HashSet<InstrumentGroup>,
    pub(super) expanded_references: HashSet<ReferenceSection>,
}

impl Default for HudState {
    fn default() -> Self {
        Self {
            panel: SidePanel::Closed,
            reset_confirm_open: false,
            expanded_instruments: HashSet::from([
                InstrumentGroup::Flight,
                InstrumentGroup::Mission,
                InstrumentGroup::Actuation,
                InstrumentGroup::Stream,
            ]),
            expanded_references: HashSet::new(),
        }
    }
}

impl HudState {
    /// Other HUD work blocks placement, but an active placement must not block
    /// itself through the launchpad adapter's own input guard.
    pub(crate) fn launchpad_input_blocked(&self) -> bool {
        self.reset_confirm_open
            || crate::controls::binding_actions_blocked()
            || super::instrument_editing()
    }

    pub(crate) fn movement_blocked(&self) -> bool {
        self.reset_confirm_open
            || crate::controls::manual_input_blocked()
            // Bevy can blur a text field on Escape before Runtime reads focus.
            // Keep that frame, and any held editing keys, behind the same gate.
            || super::instrument_editing()
            || crate::launchpad::input_blocked()
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum InteractionMode {
    Ready,
    ResetConfirmation,
    Rebinding,
    LaunchpadPlacement,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(super) enum SidePanel {
    #[default]
    Closed,
    Flight,
    Ship,
    Debris,
    Launch,
    Camera,
    Details,
    Inspector,
    Controls,
}

impl SidePanel {
    #[cfg(test)]
    pub(super) const TASKS: [Self; 6] = [
        Self::Flight,
        Self::Ship,
        Self::Debris,
        Self::Launch,
        Self::Camera,
        Self::Controls,
    ];

    pub fn details_open(self) -> bool {
        matches!(self, Self::Details | Self::Inspector)
    }

    pub fn toggle(self, requested: Self) -> Self {
        match requested {
            Self::Closed => Self::Closed,
            Self::Flight
            | Self::Ship
            | Self::Debris
            | Self::Launch
            | Self::Camera
            | Self::Controls => {
                if self == requested {
                    Self::Closed
                } else {
                    requested
                }
            }
            Self::Details => match self {
                Self::Details | Self::Inspector => Self::Closed,
                Self::Closed
                | Self::Flight
                | Self::Ship
                | Self::Debris
                | Self::Launch
                | Self::Camera
                | Self::Controls => Self::Details,
            },
            Self::Inspector => match self {
                Self::Inspector => Self::Details,
                Self::Closed
                | Self::Flight
                | Self::Ship
                | Self::Debris
                | Self::Launch
                | Self::Camera
                | Self::Details
                | Self::Controls => Self::Inspector,
            },
        }
    }

    pub fn close_top(self) -> Self {
        match self {
            Self::Inspector => Self::Details,
            Self::Closed
            | Self::Flight
            | Self::Ship
            | Self::Debris
            | Self::Launch
            | Self::Camera
            | Self::Details
            | Self::Controls => Self::Closed,
        }
    }
}

#[derive(Component)]
pub(super) struct HudRoot;

#[derive(Component)]
pub(super) struct CompactFlightCue;

#[derive(Component)]
pub(super) struct TaskTab(pub SidePanel);

#[derive(Component)]
pub(super) struct DetailsPanel;

#[derive(Component)]
pub(super) struct InstrumentSection(pub InstrumentGroup);

#[derive(Component)]
pub(super) struct InstrumentContents(pub InstrumentGroup);

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub(super) enum ReferenceSection {
    ShipApplication,
    DebrisSampling,
    ControlGuide,
    LaunchpadEffects,
    LanderPlacement,
}

impl ReferenceSection {
    pub(super) fn title(self) -> &'static str {
        match self {
            Self::ShipApplication => "Body application details",
            Self::DebrisSampling => "Sampling and collider details",
            Self::ControlGuide => "Control guide and input diagnostics",
            Self::LaunchpadEffects => "What applying a launch site changes",
            Self::LanderPlacement => "Advanced: place lander at XYZ",
        }
    }
}

#[derive(Component)]
pub(super) struct ReferenceContents(pub ReferenceSection);

/// HUD intent stays typed without requiring traits on the launchpad adapter.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum LaunchpadControl {
    FramePhobos,
    ChooseSurface,
    Apply,
    Cancel,
    North,
    South,
    East,
    West,
}

impl LaunchpadControl {
    pub(super) fn request(self) -> crate::launchpad::LaunchpadAction {
        use crate::launchpad::{LaunchpadAction, SurfaceStep};
        match self {
            Self::FramePhobos => LaunchpadAction::FramePhobos,
            Self::ChooseSurface => LaunchpadAction::PlaceOnSurface,
            Self::Apply => LaunchpadAction::Confirm,
            Self::Cancel => LaunchpadAction::Cancel,
            Self::North => LaunchpadAction::Nudge(SurfaceStep::North),
            Self::South => LaunchpadAction::Nudge(SurfaceStep::South),
            Self::East => LaunchpadAction::Nudge(SurfaceStep::East),
            Self::West => LaunchpadAction::Nudge(SurfaceStep::West),
        }
    }
}

#[derive(Component, Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum PlatformButton {
    Fullscreen,
    Notebook,
}

impl PlatformButton {
    pub(super) const fn index(self) -> usize {
        match self {
            Self::Fullscreen => 0,
            Self::Notebook => 1,
        }
    }
}

#[derive(Default)]
pub(super) struct ModalFocus {
    pub open: bool,
    pub previous: Option<Entity>,
}

#[derive(Component, Clone, Copy, Debug, PartialEq, Eq)]
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
    Notebook,
    ChangeBinding(crate::controls::Action),
    RestoreBindings,
    Maximize,
    ToggleCapture,
    ToggleDetails,
    ToggleInstruments,
    ToggleShip,
    ToggleDebris,
    ToggleLaunch,
    ToggleCamera,
    CycleIllumination,
    ToggleStars,
    ToggleGuides,
    ToggleInstrumentSection(InstrumentGroup),
    ToggleReference(ReferenceSection),
    Launchpad(LaunchpadControl),
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
    TelemetryDetails,
    Instrument(Instrument),
    InstrumentGroup(InstrumentGroup),
    InstrumentSection(InstrumentGroup),
    ReferenceToggle(ReferenceSection),
    LaunchpadSummary,
    LaunchSite,
    LaunchpadHint,
    DebrisPlayback,
    TelemetryStatus,
    InputSettings,
    BindingStatus,
    Binding(crate::controls::Action),
    Notebook,
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
    InstrumentsToggle,
    ShipToggle,
    DebrisToggle,
    LaunchToggle,
    CameraToggle,
    ControlsToggle,
    InspectorToggle,
    Illumination,
    Stars,
    Guides,
}

#[derive(Component, Clone, Copy)]
pub(super) enum Overlay {
    Flight,
    Ship,
    Debris,
    Launch,
    LaunchpadPreview,
    Camera,
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

pub(super) const PANEL: Color = Color::srgba(0.025, 0.045, 0.065, 0.96);
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
    fn captions_keep_aa_contrast_over_the_brightest_sdr_scene() {
        let panel = super::PANEL.to_linear();
        let caption = super::MUTED.to_linear();
        let luminance = |rgb: [f32; 3]| rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722;
        let background = [panel.red, panel.green, panel.blue]
            .map(|value| value * panel.alpha + 1.0 - panel.alpha);
        let contrast = (luminance([caption.red, caption.green, caption.blue]) + 0.05)
            / (luminance(background) + 0.05);
        assert!(
            contrast >= 4.5,
            "caption contrast over white SDR background: {contrast}"
        );
    }

    #[test]
    fn opening_a_panel_replaces_the_previous_panel() {
        for current in [
            Closed, Flight, Ship, Debris, Launch, Camera, Details, Inspector, Controls,
        ] {
            for requested in super::SidePanel::TASKS {
                assert_eq!(
                    current.toggle(requested),
                    if current == requested {
                        Closed
                    } else {
                        requested
                    }
                );
            }
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
        assert_eq!(Flight.close_top(), Closed);
        assert_eq!(Ship.close_top(), Closed);
        assert_eq!(Debris.close_top(), Closed);
        assert_eq!(Launch.close_top(), Closed);
        assert_eq!(Camera.close_top(), Closed);
        assert_eq!(Closed.close_top(), Closed);
    }

    #[test]
    fn placement_blocks_flight_without_circularly_blocking_its_own_hud_gate() {
        crate::controls::reset_for_tests();
        crate::launchpad::reset_for_tests();
        crate::hud::reset_instrument_editing_for_tests();
        let mut hud = super::HudState::default();
        assert!(!hud.launchpad_input_blocked());
        crate::launchpad::request(crate::launchpad::LaunchpadAction::PlaceOnSurface);
        assert!(hud.movement_blocked());
        assert!(!hud.launchpad_input_blocked());
        hud.reset_confirm_open = true;
        assert!(hud.launchpad_input_blocked());
        crate::launchpad::reset_for_tests();
    }
}
