use bevy::{
    input_focus::tab_navigation::{TabGroup, TabIndex},
    picking::{Pickable, hover::Hovered},
    prelude::*,
    text::LineHeight,
    ui_widgets::{Button, ScrollArea, observe},
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
                        panel_style(),
                        BackgroundColor(PANEL),
                        children![(text("Phobos practice | T+00:00:00 | paused", CAPTION_SIZE_PX, TEXT), Readout::Summary)],
                    ),
                    (
                        Node {
                            max_width: percent(100),
                            flex_shrink: 0.0,
                            justify_content: JustifyContent::FlexEnd,
                            ..button_row()
                        },
                        Pickable::IGNORE,
                        LayoutPart::MissionActions,
                        children![
                            button("Run [Space]", HudAction::TogglePlayback, Readout::Playback),
                            (button("Fullscreen [X]", HudAction::Fullscreen, Readout::Fullscreen), FullscreenButton),
                            button("Details +", HudAction::ToggleControls, Readout::Controls),
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
                Node {
                    position_type: PositionType::Absolute,
                    right: px(SCREEN_PADDING_PX),
                    width: px(300),
                    overflow: Overflow::scroll_y(),
                    display: Display::None,
                    ..panel_style()
                },
                BackgroundColor(PANEL),
                ZIndex(1),
                Overlay::Controls,
                DetailsPanel,
                ScrollArea,
                Hovered::default(),
                observe(systems::scroll_details),
                Visibility::Hidden,
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
                            button("Maximize [M]", HudAction::Maximize, Readout::Maximize),
                        ],
                    ),
                    (
                        Node { width: percent(100), padding: UiRect::ZERO, ..panel_style() },
                        LayoutPart::MetricsPanel,
                        children![
                            (text("", COPY_SIZE_PX, TEXT), Readout::Mission),
                            (text("", CAPTION_SIZE_PX, MUTED), Readout::Environment),
                            (text("", CAPTION_SIZE_PX, MUTED), Readout::Authority),
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
                            button("Help [H]", HudAction::ToggleHelp, Readout::HelpToggle),
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
                    (
                        Node { display: Display::None, width: percent(100), ..panel_style() },
                        Overlay::Help,
                        Visibility::Hidden,
                        children![
                            text("Keyboard and camera help", COPY_SIZE_PX, TEXT),
                            text("Space run/pause | . step | R reset | T rate", COPY_SIZE_PX, MUTED),
                            text("1-5 viewer cameras | 6 top sensor | 7 bottom sensor", COPY_SIZE_PX, MUTED),
                            text("Ship sensors: 512 x 512, RGB8 per channel, fixed exposure and configured field of view. No debug markers or contrast boost.", COPY_SIZE_PX, MUTED),
                            text(", / change focus | L capture or release", COPY_SIZE_PX, MUTED),
                            text("Drag to orbit; wheel or trackpad pinch zooms viewer cameras. Ship sensor optics stay fixed.", COPY_SIZE_PX, MUTED),
                            text("Arrows orbit | WASD/QE fly | Tab moves focus | Enter or Space activates a button", COPY_SIZE_PX, MUTED),
                            text("M maximize | X fullscreen | I inspect | H help", COPY_SIZE_PX, MUTED),
                            text("Esc closes help, inspector, details or the reset prompt.", COPY_SIZE_PX, MUTED),
                            text("Frame axes: +X points to Mars; +Y is orbit normal. L1/L2 use a circular point-mass model. Terrain, contacts and damage are practice approximations. Viewer cameras are not STP sensor feeds.", COPY_SIZE_PX, MUTED),
                        ],
                    ),
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
    ));
}

fn panel_style() -> Node {
    Node {
        flex_direction: FlexDirection::Column,
        flex_shrink: 0.0,
        min_width: px(0),
        align_items: AlignItems::FlexStart,
        row_gap: px(CONTROL_GAP_PX),
        padding: UiRect::axes(px(PANEL_PADDING_X_PX), px(PANEL_PADDING_Y_PX)),
        max_width: percent(100),
        ..default()
    }
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
            max_width: percent(100),
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
