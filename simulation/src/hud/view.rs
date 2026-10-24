use bevy::{
    input_focus::tab_navigation::{TabGroup, TabIndex},
    picking::{Pickable, hover::Hovered},
    prelude::*,
    text::LineHeight,
    ui_widgets::{Button, ScrollArea, observe},
};

use super::{model::*, systems};
use crate::domain::CameraMode;

pub(super) fn setup(
    mut commands: Commands,
    cameras: Query<Entity, With<crate::camera::SimulationCamera>>,
) {
    let root = commands.spawn((
        Node {
            position_type: PositionType::Absolute,
            left: px(0),
            top: px(0),
            width: percent(100),
            height: percent(100),
            padding: UiRect::all(px(SCREEN_PADDING_PX)),
            flex_direction: FlexDirection::Column,
            justify_content: JustifyContent::SpaceBetween,
            align_items: AlignItems::Stretch,
            row_gap: px(PANEL_GAP_PX),
            ..default()
        },
        HudRoot,
        Name::new("Flight HUD"),
        GlobalZIndex(10),
        Visibility::Visible,
        TabGroup::new(0),
        children![
            (
                Node {
                    width: percent(100),
                    flex_direction: FlexDirection::Row,
                    flex_wrap: FlexWrap::Wrap,
                    flex_shrink: 0.0,
                    justify_content: JustifyContent::SpaceBetween,
                    align_items: AlignItems::Center,
                    column_gap: px(PANEL_GAP_PX),
                    row_gap: px(PANEL_GAP_PX),
                    ..default()
                },
                Pickable::IGNORE,
                LayoutPart::TopRow,
                children![
                    (
                        Node { width: px(360), ..panel_style() },
                        BackgroundColor(PANEL),
                        children![
                            (text("Phobos practice | T+00:00:00 | paused", CAPTION_SIZE_PX, TEXT), Readout::Summary),
                            (text("Waiting for flight telemetry", COPY_SIZE_PX, TEXT), Readout::TelemetryBrief),
                            (text("", CAPTION_SIZE_PX, MUTED), Readout::ThrustProfile),
                            (text("Path preview | waiting for radar", CAPTION_SIZE_PX, MUTED), Readout::PathBrief),
                        ],
                    ),
                    (
                        Node {
                            max_width: percent(100),
                            flex_shrink: 0.0,
                            justify_content: JustifyContent::FlexEnd,
                            ..button_row()
                        },
                        Pickable::IGNORE,
                        children![
                            button("Run [P]", HudAction::TogglePlayback, Readout::Playback),
                            button("Follow lander [F]", HudAction::FollowLander, Readout::Static),
                            button("Controls [H]", HudAction::ToggleControls, Readout::ControlsToggle),
                            (button("Fullscreen [X]", HudAction::Fullscreen, Readout::Fullscreen), FullscreenButton),
                            button("Details +", HudAction::ToggleDetails, Readout::DetailsToggle),
                        ],
                    ),
                ],
            ),
            (
                Node {
                    width: percent(100),
                    flex_direction: FlexDirection::Row,
                    flex_shrink: 0.0,
                    align_items: AlignItems::FlexEnd,
                    justify_content: JustifyContent::SpaceBetween,
                    column_gap: px(PANEL_GAP_PX),
                    ..default()
                },
                Pickable::IGNORE,
                LayoutPart::BottomRow,
                children![
                    (
                        panel_style(),
                        BackgroundColor(PANEL),
                        LayoutPart::CameraPanel,
                        children![
                            (text("Orbit | Lander", CAPTION_SIZE_PX, MUTED), Readout::CameraMode),
                            text("WASD lander | Space lift | Alt/Option debug | P pause", CAPTION_SIZE_PX, MUTED),
                            (
                                button_row(),
                                Pickable::IGNORE,
                                children![
                                    button("Orbit [1]", HudAction::ChangeCamera(CameraMode::Orbit), Readout::Static),
                                    button("Debug FP [2]", HudAction::ChangeCamera(CameraMode::FirstPerson), Readout::Static),
                                    button("Third [3]", HudAction::ChangeCamera(CameraMode::ThirdPerson), Readout::Static),
                                    button("Free [4]", HudAction::ChangeCamera(CameraMode::FreeFlight), Readout::Static),
                                    button("Overview [5]", HudAction::ChangeCamera(CameraMode::Overview), Readout::Static),
                                    button("Top [6]", HudAction::ChangeCamera(CameraMode::ShipTop), Readout::Static),
                                    button("Bottom [7]", HudAction::ChangeCamera(CameraMode::ShipBottom), Readout::Static),
                                ],
                            ),
                        ],
                    ),
                    (
                        Node { display: Display::None, ..button_row() },
                        Visibility::Hidden,
                        LayoutPart::SensorActions,
                        children![
                            button("Top [6]", HudAction::ChangeCamera(CameraMode::ShipTop), Readout::Static),
                            button("Bottom [7]", HudAction::ChangeCamera(CameraMode::ShipBottom), Readout::Static),
                            button("Debug [4]", HudAction::ChangeCamera(CameraMode::FreeFlight), Readout::Static),
                        ],
                    ),
                ],
            ),
            (
                side_panel(Overlay::Details),
                children![
                    text("Mission details and controls", COPY_SIZE_PX, TEXT),
                    text("Scroll for more. Focus a control to use Page Up/Down. Esc closes details.", CAPTION_SIZE_PX, MUTED),
                    (
                        button_row(),
                        children![
                            button("Step [.]", HudAction::Step, Readout::Static),
                            button("Reset [R]", HudAction::AskReset, Readout::Static),
                            button("Export", HudAction::Export, Readout::Static),
                            button("Rate [T]", HudAction::CycleRate, Readout::Rate),
                            button("Example controller", HudAction::ToggleAutopilot, Readout::Autopilot),
                            button("Use reference Space thrust", HudAction::TogglePracticeBoost, Readout::BoostToggle),
                            button("Maximize [M]", HudAction::Maximize, Readout::Maximize),
                        ],
                    ),
                    (
                        Node { width: percent(100), padding: UiRect::ZERO, ..panel_style() },
                        children![
                            (text("", COPY_SIZE_PX, TEXT), Readout::Mission),
                            (text("", CAPTION_SIZE_PX, MUTED), Readout::Environment),
                            (text("", CAPTION_SIZE_PX, MUTED), Readout::Authority),
                            (text("", CAPTION_SIZE_PX, MUTED), Readout::FlightControls),
                            (text("", COPY_SIZE_PX, TEXT), Readout::TelemetryDetails),
                            (text("", CAPTION_SIZE_PX, MUTED), Readout::PathDetails),
                        ],
                    ),
                    (text("Focus: Lander", COPY_SIZE_PX, TEXT), Readout::Focus),
                    (text("", CAPTION_SIZE_PX, MUTED), Readout::Camera),
                    (
                        button_row(),
                        children![
                            button("< Focus [,]", HudAction::FocusPrevious, Readout::Static),
                            button("Focus [/] >", HudAction::FocusNext, Readout::Static),
                            button("Capture [L]", HudAction::ToggleCapture, Readout::Capture),
                            button("Controls [H]", HudAction::ToggleControls, Readout::ControlsToggle),
                            button("Inspect [I]", HudAction::ToggleInspector, Readout::InspectorToggle),
                        ],
                    ),
                    (
                        Node { display: Display::None, width: percent(100), ..panel_style() },
                        BackgroundColor(BUTTON),
                        Overlay::Inspector,
                        Visibility::Hidden,
                        children![(text("", COPY_SIZE_PX, TEXT), Readout::Inspector)],
                    ),
                ],
            ),
            (
                side_panel(Overlay::Controls),
                children![
                    text("Controls and controller settings [H]", COPY_SIZE_PX, TEXT),
                    text("H or Esc closes. Scroll for all settings; Page Up/Down works from a focused control.", CAPTION_SIZE_PX, MUTED),
                    (
                        button_row(),
                        children![
                            button("Follow lander [F]", HudAction::FollowLander, Readout::Static),
                            button("Example controller", HudAction::ToggleAutopilot, Readout::Autopilot),
                            button("Use reference Space thrust", HudAction::TogglePracticeBoost, Readout::BoostToggle),
                            button("Close controls [H]", HudAction::ToggleControls, Readout::ControlsToggle),
                        ],
                    ),
                    (text("Loading current input state", COPY_SIZE_PX, TEXT), Readout::ControllerSettings),
                    text("Space launches/lifts | W/S forward/back | A/D left/right | E/Q up/down\nF follows the lander | Shift+F frames the selected object\nAlt/Option + movement keys: debug camera only | Shift boosts debug movement\nP pause/run | R reset | T rate | L capture/release\n1 orbit | 2 debug FP | 3 third-person | 4 free | 5 overview | 6/7 ship sensors\nTab selects controls | Enter activates | H or Esc closes", CAPTION_SIZE_PX, MUTED),
                    text("Manual thrust takes over from the example controller. Offset side jets can roll or topple the lander. Release keys to stop thrust, not velocity or spin. This panel does not pause flight.", CAPTION_SIZE_PX, MUTED),
                    text("Amber line: advisory path. Blue-white exhaust: actual-force debug cue. Neither is added to the fixed 512 x 512 RGB8 ship-camera images.", CAPTION_SIZE_PX, MUTED),
                    text("The planner does not control the lander. The browser uses local practice physics, not the STP SDK.", CAPTION_SIZE_PX, MUTED),
                ],
            ),
            (
                Node {
                    position_type: PositionType::Absolute,
                    left: px(0),
                    right: px(0),
                    top: px(0),
                    bottom: px(0),
                    justify_content: JustifyContent::Center,
                    align_items: AlignItems::Center,
                    display: Display::None,
                    ..default()
                },
                BackgroundColor(MODAL_SCRIM),
                ZIndex(2),
                Overlay::Reset,
                Visibility::Hidden,
                children![
                    (
                        Node { max_width: percent(94), ..panel_style() },
                        BackgroundColor(PANEL),
                        children![
                            text("Reset this practice run?", COPY_SIZE_PX, TEXT),
                            text("The current run will be replaced.", COPY_SIZE_PX, MUTED),
                            (
                                button_row(),
                                children![
                                    button("Reset run", HudAction::ConfirmReset, Readout::Static),
                                    button("Cancel", HudAction::CancelReset, Readout::Static),
                                ],
                            ),
                        ],
                    ),
                ],
            ),
        ],
    )).id();
    if let Ok(camera) = cameras.single() {
        commands.entity(root).insert(UiTargetCamera(camera));
    }
}

fn panel_style() -> Node {
    Node {
        flex_direction: FlexDirection::Column,
        flex_shrink: 0.0,
        min_width: px(0),
        align_items: AlignItems::Stretch,
        row_gap: px(CONTROL_GAP_PX),
        padding: UiRect::axes(px(PANEL_PADDING_X_PX), px(PANEL_PADDING_Y_PX)),
        max_width: percent(100),
        ..default()
    }
}

fn side_panel(overlay: Overlay) -> impl Bundle {
    (
        Node {
            position_type: PositionType::Absolute,
            right: px(SCREEN_PADDING_PX),
            width: px(420),
            overflow: Overflow::scroll_y(),
            display: Display::None,
            ..panel_style()
        },
        BackgroundColor(PANEL),
        ZIndex(1),
        overlay,
        DetailsPanel,
        ScrollArea,
        Hovered::default(),
        observe(systems::scroll_details),
        Visibility::Hidden,
    )
}

fn button_row() -> Node {
    Node {
        max_width: percent(100),
        flex_direction: FlexDirection::Row,
        flex_wrap: FlexWrap::Wrap,
        flex_shrink: 0.0,
        min_width: px(0),
        align_items: AlignItems::Center,
        column_gap: px(CONTROL_GAP_PX),
        row_gap: px(CONTROL_GAP_PX),
        ..default()
    }
}

fn text(value: &str, size: u32, color: Color) -> impl Bundle {
    (
        Text::new(value),
        TextFont {
            font_size: FontSize::Px(size as f32),
            ..default()
        },
        Node {
            min_width: px(0),
            flex_shrink: 0.0,
            ..default()
        },
        LineHeight::RelativeToFont(1.4),
        TextColor(color),
        Pickable::IGNORE,
    )
}

fn button(label: &str, action: HudAction, readout: Readout) -> impl Bundle {
    (
        Button,
        action,
        AccessibleLabel::new(label),
        Hovered::default(),
        Node {
            min_width: px(24),
            max_width: percent(100),
            min_height: px(BUTTON_HEIGHT_PX),
            flex_shrink: 0.0,
            padding: UiRect::axes(px(BUTTON_PADDING_X_PX), px(BUTTON_PADDING_Y_PX)),
            justify_content: JustifyContent::Center,
            align_items: AlignItems::Center,
            border_radius: BorderRadius::all(px(4)),
            ..default()
        },
        BackgroundColor(BUTTON),
        Outline::new(px(1), px(1), Color::NONE),
        TabIndex(0),
        observe(systems::activate_button),
        children![(text(label, COPY_SIZE_PX, TEXT), readout)],
    )
}
