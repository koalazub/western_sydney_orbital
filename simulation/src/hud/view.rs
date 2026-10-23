use bevy::{
    input_focus::tab_navigation::{TabGroup, TabIndex},
    picking::hover::Hovered,
    prelude::*,
    ui_widgets::{Button, observe},
};

use super::{model::*, systems};
use crate::domain::CameraMode;

pub(super) fn setup(mut commands: Commands) {
    commands.spawn((
        Node {
            width: percent(100),
            height: percent(100),
            padding: UiRect::all(px(SCREEN_PADDING_PX)),
            flex_direction: FlexDirection::Column,
            justify_content: JustifyContent::SpaceBetween,
            align_items: AlignItems::Stretch,
            row_gap: px(PANEL_GAP_PX),
            ..default()
        },
        TabGroup::new(0),
        children![
            (
                Node {
                    width: percent(100),
                    flex_direction: FlexDirection::Row,
                    justify_content: JustifyContent::SpaceBetween,
                    align_items: AlignItems::FlexStart,
                    column_gap: px(8),
                    ..default()
                },
                LayoutPart::TopRow,
                children![
                    (
                        panel_style(),
                        BackgroundColor(PANEL),
                        children![
                            text("ESCAPE FROM PHOBOS", 14, TEXT),
                            text("PRACTICE | synthetic mission", 12, MUTED),
                            (text("Orbit | Lander", 12, TEXT), Readout::CameraMode),
                        ],
                    ),
                    (
                        Node {
                            max_width: percent(72),
                            flex_direction: FlexDirection::Row,
                            flex_wrap: FlexWrap::Wrap,
                            justify_content: JustifyContent::FlexEnd,
                            align_items: AlignItems::FlexStart,
                            column_gap: px(5),
                            row_gap: px(5),
                            ..default()
                        },
                        LayoutPart::MissionActions,
                        children![
                            button("Run [Space]", HudAction::TogglePlayback, Readout::Playback, 0),
                            button("Step [.]", HudAction::Step, Readout::Static, 1),
                            button("Reset [R]", HudAction::AskReset, Readout::Static, 2),
                            button("Export", HudAction::Export, Readout::Static, 3),
                            button("Rate [T]", HudAction::CycleRate, Readout::Rate, 4),
                            button("Example controller", HudAction::ToggleAutopilot, Readout::Autopilot, 5),
                            button("Window [M]", HudAction::Maximize, Readout::Maximize, 6),
                            button("Full [X]", HudAction::Fullscreen, Readout::Fullscreen, 7),
                        ],
                    ),
                ],
            ),
            (
                Node {
                    width: percent(100),
                    flex_direction: FlexDirection::Row,
                    align_items: AlignItems::FlexEnd,
                    justify_content: JustifyContent::SpaceBetween,
                    column_gap: px(8),
                    ..default()
                },
                LayoutPart::BottomRow,
                children![
                    (
                        panel_style(),
                        BackgroundColor(PANEL),
                        LayoutPart::MetricsPanel,
                        children![
                            (
                                text("Time - | phase -\nTarget - | range - | speed -\nFuel used ~- kg | damage estimate ~- J\nDebris - | contacts - | scene entities -", 12, TEXT),
                                Readout::Mission,
                            ),
                            (text("L1 - km | L2 - km", 12, TEXT), Readout::Environment),
                            (text("", 12, MUTED), Readout::Authority),
                            (
                                Node {
                                    width: percent(100),
                                    max_width: percent(100),
                                    padding: UiRect::axes(px(8), px(6)),
                                    display: Display::None,
                                    ..default()
                                },
                                BackgroundColor(BUTTON),
                                Overlay::Inspector,
                                Visibility::Hidden,
                                children![(text("", 12, TEXT), Readout::Inspector)],
                            ),
                        ],
                    ),
                    (
                        Node {
                            max_width: px(330),
                            flex_direction: FlexDirection::Column,
                            align_items: AlignItems::FlexEnd,
                            row_gap: px(5),
                            ..default()
                        },
                        LayoutPart::CameraPanel,
                        children![
                            (
                                panel_style(),
                                BackgroundColor(PANEL),
                                children![
                                    text("DEBUG / SHIP CAMERAS", 12, MUTED),
                                    (text("Focus: Lander", 12, TEXT), Readout::Focus),
                                    (text("Orbit | range -", 12, TEXT), Readout::Camera),
                                    (
                                        Node {
                                            flex_direction: FlexDirection::Row,
                                            flex_wrap: FlexWrap::Wrap,
                                            column_gap: px(4),
                                            row_gap: px(4),
                                            ..default()
                                        },
                                        children![
                                            button("Orbit [1]", HudAction::ChangeCamera(CameraMode::Orbit), Readout::Static, 8),
                                            button("Debug FP [2]", HudAction::ChangeCamera(CameraMode::FirstPerson), Readout::Static, 9),
                                            button("Third [3]", HudAction::ChangeCamera(CameraMode::ThirdPerson), Readout::Static, 10),
                                            button("Free [4]", HudAction::ChangeCamera(CameraMode::FreeFlight), Readout::Static, 11),
                                            button("Overview [5]", HudAction::ChangeCamera(CameraMode::Overview), Readout::Static, 12),
                                            button("Ship top [6]", HudAction::ChangeCamera(CameraMode::ShipTop), Readout::Static, 13),
                                            button("Ship bottom [7]", HudAction::ChangeCamera(CameraMode::ShipBottom), Readout::Static, 14),
                                            button("< Focus [,]", HudAction::FocusPrevious, Readout::Static, 15),
                                            button("Focus [/] >", HudAction::FocusNext, Readout::Static, 16),
                                            button("Capture [L]", HudAction::ToggleCapture, Readout::Capture, 17),
                                            button("Help [H]", HudAction::ToggleHelp, Readout::Static, 18),
                                            button("Inspect [I]", HudAction::ToggleInspector, Readout::Static, 19),
                                        ],
                                    ),
                                ],
                            ),
                        ],
                    ),
                    (
                        Node { flex_direction: FlexDirection::Row, flex_wrap: FlexWrap::Wrap,
                            justify_content: JustifyContent::Center, column_gap: px(5), row_gap: px(5),
                            display: Display::None, ..default() },
                        Visibility::Hidden,
                        LayoutPart::SensorActions,
                        children![
                            button("Top [6]", HudAction::ChangeCamera(CameraMode::ShipTop), Readout::Static, 20),
                            button("Bottom [7]", HudAction::ChangeCamera(CameraMode::ShipBottom), Readout::Static, 21),
                            button("Debug [4]", HudAction::ChangeCamera(CameraMode::FreeFlight), Readout::Static, 22),
                        ],
                    ),
                ],
            ),
            (
                Node {
                    position_type: PositionType::Absolute,
                    left: percent(5),
                    right: percent(5),
                    top: percent(35),
                    max_width: percent(90),
                    display: Display::None,
                    ..panel_style()
                },
                BackgroundColor(PANEL),
                Overlay::Reset,
                Visibility::Hidden,
                children![
                    text("Reset this practice run?", 15, TEXT),
                    text("The current run will be replaced.", 12, MUTED),
                    button("Reset run", HudAction::ConfirmReset, Readout::Static, 0),
                    button("Cancel", HudAction::CancelReset, Readout::Static, 1),
                ],
            ),
            (
                Node {
                    position_type: PositionType::Absolute,
                    right: px(10),
                    top: px(86),
                    max_width: percent(92),
                    width: px(320),
                    display: Display::None,
                    ..panel_style()
                },
                BackgroundColor(PANEL),
                Overlay::Help,
                Visibility::Hidden,
                children![
                    text("KEYBOARD", 12, TEXT),
                    text("Space run/pause | . step | R reset | T rate", 12, MUTED),
                    text("1-5 debug views | 6 top sensor | 7 bottom sensor", 12, MUTED),
                    text("Ship sensors: 512 x 512, RGB8 per channel, fixed exposure. No debug markers or contrast boost.", 12, MUTED),
                    text(", / focus | L capture | drag/wheel debug camera", 12, MUTED),
                    text("Arrows orbit | WASD/QE fly | Tab then Enter activates buttons", 12, MUTED),
                    text("M window | X fullscreen | I inspect | H help", 12, MUTED),
                    text("Esc closes help, inspector or reset prompt", 12, MUTED),
                    text("Frame axes: +X points to Mars; +Y is orbit normal. L1/L2 use a circular point-mass model. Terrain, contacts and damage are practice approximations. This is a viewer camera, not an STP feed.", 12, MUTED),
                ],
            ),
        ],
    ));
}

fn panel_style() -> Node {
    Node {
        flex_direction: FlexDirection::Column,
        align_items: AlignItems::FlexStart,
        row_gap: px(5),
        padding: UiRect::axes(px(10), px(8)),
        max_width: percent(100),
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
        TextColor(color),
    )
}

fn button(label: &str, action: HudAction, readout: Readout, tab_index: i32) -> impl Bundle {
    (
        Button,
        action,
        Hovered::default(),
        Node {
            min_height: px(36),
            padding: UiRect::axes(px(9), px(6)),
            justify_content: JustifyContent::Center,
            align_items: AlignItems::Center,
            border_radius: BorderRadius::all(px(5)),
            ..default()
        },
        BackgroundColor(BUTTON),
        TabIndex(tab_index),
        observe(systems::activate_button),
        children![(text(label, 12, TEXT), readout)],
    )
}
