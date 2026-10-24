use bevy::{
    input_focus::tab_navigation::{TabGroup, TabIndex},
    picking::{Pickable, hover::Hovered},
    prelude::*,
    text::LineHeight,
    ui_widgets::{Button, ScrollArea, observe},
};

use super::{model::*, systems};
use crate::controls::Action;
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
                            (text("", CAPTION_SIZE_PX, MUTED), Readout::ReferenceThrust),
                            (text("Python path preview | none received", CAPTION_SIZE_PX, MUTED), Readout::PathBrief),
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
                            (button("Open notebook · new tab", HudAction::Notebook, Readout::Notebook), PlatformButton::Notebook),
                            (button("Fullscreen [X]", HudAction::Fullscreen, Readout::Fullscreen), PlatformButton::Fullscreen),
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
                            (text("W/S fire front/back | A/D fire left/right | Space lift | Alt/Option debug", CAPTION_SIZE_PX, MUTED), Readout::FlightHint),
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
                    text("Controls and input settings [H]", COPY_SIZE_PX, TEXT),
                    // A stretched action stays one line on short canvases;
                    // sharing a wrapping row with secondary actions can clip it.
                    button("Upright lander · test reset", HudAction::DebugUpright, Readout::Static),
                    (
                        button_row(),
                        children![
                            button("Follow lander [F]", HudAction::FollowLander, Readout::Static),
                            button("Close controls [H]", HudAction::ToggleControls, Readout::ControlsToggle),
                        ],
                    ),
                    text("H or Esc closes. Scroll for all settings; Page Up/Down works from a focused control.", CAPTION_SIZE_PX, MUTED),
                    text("Firing sides are fixed to the lander, not the camera. Body-axis guide: long arrow = +X front, medium = +Y left, short = +Z up. A side jet pushes in the opposite direction.", CAPTION_SIZE_PX, MUTED),
                    (
                        button_row(),
                        children![
                            button("Front: W · change", HudAction::ChangeBinding(Action::Front), Readout::Binding(Action::Front)),
                            button("Back: S · change", HudAction::ChangeBinding(Action::Back), Readout::Binding(Action::Back)),
                            button("Left: A · change", HudAction::ChangeBinding(Action::Left), Readout::Binding(Action::Left)),
                            button("Right: D · change", HudAction::ChangeBinding(Action::Right), Readout::Binding(Action::Right)),
                            button("Up: E · change", HudAction::ChangeBinding(Action::Up), Readout::Binding(Action::Up)),
                            button("Down: Q · change", HudAction::ChangeBinding(Action::Down), Readout::Binding(Action::Down)),
                            button("Lift: Space · change", HudAction::ChangeBinding(Action::Lift), Readout::Binding(Action::Lift)),
                            button("Restore default keys", HudAction::RestoreBindings, Readout::Static),
                        ],
                    ),
                    (text("Choose a binding to change. One physical key per action; no chords.", COPY_SIZE_PX, TEXT), Readout::BindingStatus),
                    text("Front/back fire body +X/-X nozzles for -X/+X force. Left/right fire body +Y/-Y nozzles for -Y/+Y force. Up and Lift fire the bottom jet for +Z force; Down fires the top jet for -Z force.", CAPTION_SIZE_PX, MUTED),
                    text("Upright lander is a local test reset, not a competition maneuver or controller command. It pauses, matches launchpad rotation, stops spin and clears thrust. Position, linear velocity, time, fuel, damage and mission outcome stay unchanged. Each reset is recorded so notebook experiments can stop. Release movement keys before flying again.", CAPTION_SIZE_PX, MUTED),
                    (text("Loading current input state", COPY_SIZE_PX, TEXT), Readout::InputSettings),
                    text("Lift starts paused flight at reference thrust. Up uses the same limit.\nF follows the lander | Shift+F frames the selected object\nAlt/Option + movement keys: debug camera only, with intuitive forward/left movement | Shift boosts debug movement\nP pause/run | R reset | T rate | L capture/release\n1 orbit | 2 debug FP | 3 third-person | 4 free | 5 overview | 6/7 ship sensors\nTab selects controls | Enter activates | H or Esc closes\nChanging a key blocks manual flight until all old and new movement keys are released. It does not clear a notebook-owned force.", CAPTION_SIZE_PX, MUTED),
                    text("Flight algorithms, predictions and controller scheduling belong in the Python notebook. Ship keys replace the current command with manual nozzle forces. Offset side jets can roll or topple the lander. Release keys to stop thrust, not velocity or spin. Opening this panel does not pause flight.", CAPTION_SIZE_PX, MUTED),
                    text("Amber line: Python notebook path preview. Blue-white exhaust: actual-force debug cue. Neither is added to the fixed 512 x 512 RGB8 ship-camera images.", CAPTION_SIZE_PX, MUTED),
                    text("The browser runs local physics, sensors and rendering, not the STP SDK. Notebook pause and step controls affect this local model only.", CAPTION_SIZE_PX, MUTED),
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
        // Rows wrap complete controls. Splitting these short action labels at
        // min-content width can make a button taller than a short scroll panel.
        children![(
            text(label, COPY_SIZE_PX, TEXT),
            TextLayout::no_wrap(),
            readout
        )],
    )
}
