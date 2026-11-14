use bevy::{
    input_focus::tab_navigation::{TabGroup, TabIndex},
    picking::{Pickable, hover::Hovered},
    prelude::*,
    text::{LineBreak, LineHeight},
    ui_widgets::{Button, ScrollArea, observe},
};

use super::{model::*, systems};
use crate::controls::Action;
use crate::domain::CameraMode;
use crate::telemetry::{FlightMetric, Instrument, InstrumentGroup};

pub(super) fn setup(
    mut commands: Commands,
    cameras: Query<Entity, With<crate::camera::SimulationCamera>>,
) {
    let top_row = spawn_top_row(&mut commands);
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
        observe(systems::scroll_details),
        children![
            (
                Node {
                    width: percent(100),
                    padding: UiRect::ZERO,
                    ..panel_style()
                },
                Pickable::IGNORE,
                ScrollArea,
                Hovered::default(),
                LayoutPart::BottomRow,
                children![
                    (
                        text_block("Flight strip scrolls. Page Up/Down.", CAPTION_SIZE_PX, MUTED, Node { width: percent(100), min_width: px(0), flex_shrink: 0.0, display: Display::None, ..default() }, LineBreak::WordOrCharacter),
                        CompactFlightCue,
                    ),
                    (
                        button_row(),
                        children![
                            button("Play sim [P]", HudAction::TogglePlayback, Readout::Playback),
                            button("Follow [F]", HudAction::FollowLander, Readout::Static),
                            (button("Notebook · new tab", HudAction::Notebook, Readout::Notebook), PlatformButton::Notebook),
                        ],
                    ),
                    (
                        button_row(),
                        children![
                            (button("Flight +", HudAction::ToggleInstruments, Readout::InstrumentsToggle), TaskTab(SidePanel::Flight)),
                            (button("Ship +", HudAction::ToggleShip, Readout::ShipToggle), TaskTab(SidePanel::Ship)),
                            (button("Debris +", HudAction::ToggleDebris, Readout::DebrisToggle), TaskTab(SidePanel::Debris)),
                            (button("Launch +", HudAction::ToggleLaunch, Readout::LaunchToggle), TaskTab(SidePanel::Launch)),
                            (button("Views +", HudAction::ToggleCamera, Readout::CameraToggle), TaskTab(SidePanel::Camera)),
                            (button("Help [H]", HudAction::ToggleControls, Readout::ControlsToggle), TaskTab(SidePanel::Controls)),
                        ],
                    ),
                ],
            ),
            (
                side_panel(Overlay::Details),
                children![
                    text("Details and measurement definitions", COPY_SIZE_PX, TEXT),
                    text("Advanced references for this practice model. Scroll for definitions; Esc closes details.", CAPTION_SIZE_PX, MUTED),
                    (
                        button_row(),
                        children![
                            button("Step [.]", HudAction::Step, Readout::Static),
                            button("Reset [R]", HudAction::AskReset, Readout::Static),
                            button("Export", HudAction::Export, Readout::Static),
                            button("Rate [T]", HudAction::CycleRate, Readout::Rate),
                            button("Maximize [M]", HudAction::Maximize, Readout::Maximize),
                            button("Open Flight", HudAction::ToggleInstruments, Readout::Static),
                            button("Close details", HudAction::ToggleDetails, Readout::DetailsToggle),
                        ],
                    ),
                    (
                        Node { width: percent(100), padding: UiRect::ZERO, ..panel_style() },
                        children![
                            (text("", CAPTION_SIZE_PX, MUTED), Readout::Environment),
                            (text("", CAPTION_SIZE_PX, MUTED), Readout::Authority),
                            (text("", CAPTION_SIZE_PX, MUTED), Readout::PathDetails),
                        ],
                    ),
                    text("Measurement definitions", COPY_SIZE_PX, TEXT),
                    (text("", CAPTION_SIZE_PX, MUTED), Readout::TelemetryDetails),
                    (
                        button_row(),
                        children![
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
                side_panel(Overlay::Camera),
                children![
                    text("Views", COPY_SIZE_PX, TEXT),
                    button("Close views", HudAction::ToggleCamera, Readout::Static),
                    text("Viewer controls do not change fixed-exposure RGB feeds. Scroll for sensors and fullscreen; Esc closes views.", CAPTION_SIZE_PX, MUTED),
                    text("Viewer", COPY_SIZE_PX, TEXT),
                    (
                        button_row(),
                        children![
                            button("Viewer light: Context", HudAction::CycleIllumination, Readout::Illumination),
                            button("Stars: on", HudAction::ToggleStars, Readout::Stars),
                            button("Debug guides: on", HudAction::ToggleGuides, Readout::Guides),
                        ],
                    ),
                    text("Overview shows Mars at true scale; Phobos blocks Mars from the close pad view.", CAPTION_SIZE_PX, MUTED),
                    (text("Orbit | Lander", CAPTION_SIZE_PX, MUTED), Readout::CameraMode),
                    (
                        button_row(),
                        LayoutPart::CameraPanel,
                        children![
                            button("Orbit [1]", HudAction::ChangeCamera(CameraMode::Orbit), Readout::Static),
                            button("Debug FP [2]", HudAction::ChangeCamera(CameraMode::FirstPerson), Readout::Static),
                            button("Third [3]", HudAction::ChangeCamera(CameraMode::ThirdPerson), Readout::Static),
                            button("Free [4]", HudAction::ChangeCamera(CameraMode::FreeFlight), Readout::Static),
                            button("Overview [5]", HudAction::ChangeCamera(CameraMode::Overview), Readout::Static),
                        ],
                    ),
                    text("Ship RGB cameras", COPY_SIZE_PX, TEXT),
                    (
                        button_row(),
                        LayoutPart::SensorActions,
                        children![
                            button("Top sensor [6]", HudAction::ChangeCamera(CameraMode::ShipTop), Readout::Static),
                            button("Bottom sensor [7]", HudAction::ChangeCamera(CameraMode::ShipBottom), Readout::Static),
                        ],
                    ),
                    (text("Focus: Lander", COPY_SIZE_PX, TEXT), Readout::Focus),
                    (
                        button_row(),
                        children![
                            button("< Focus [,]", HudAction::FocusPrevious, Readout::Static),
                            button("Focus [/] >", HudAction::FocusNext, Readout::Static),
                            button("Capture [L]", HudAction::ToggleCapture, Readout::Capture),
                            (button("Fullscreen [X]", HudAction::Fullscreen, Readout::Fullscreen), PlatformButton::Fullscreen),
                            button("Details +", HudAction::ToggleDetails, Readout::DetailsToggle),
                        ],
                    ),
                    (text("", CAPTION_SIZE_PX, MUTED), Readout::Camera),
                    text("Scroll for more. Page Up/Down scrolls from a focused control. Esc closes views.", CAPTION_SIZE_PX, MUTED),
                ],
            ),
            (
                side_panel(Overlay::Controls),
                children![
                    text("Help and controls [H]", COPY_SIZE_PX, TEXT),
                    text("Change flight keys here. Scroll for the control guide and advanced Details; H or Esc closes help.", CAPTION_SIZE_PX, MUTED),
                    button("Upright lander · test reset", HudAction::DebugUpright, Readout::Static),
                    (
                        button_row(),
                        children![
                            button("Follow lander [F]", HudAction::FollowLander, Readout::Static),
                            button("Close help [H]", HudAction::ToggleControls, Readout::Static),
                            button("Details and definitions +", HudAction::ToggleDetails, Readout::Static),
                        ],
                    ),
                    (text("", CAPTION_SIZE_PX, MUTED), Readout::FlightControls),
                    text("Flight key bindings", COPY_SIZE_PX, TEXT),
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
                    control_guide(),
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
                        Node { width: px(420), max_width: percent(94), ..panel_style() },
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
                ZIndex(3),
                Overlay::Terminal,
                Visibility::Hidden,
                children![
                    (
                        Node {
                            width: px(560),
                            max_width: percent(94),
                            ..panel_style()
                        },
                        BackgroundColor(PANEL),
                        children![
                            (text("", HERO_SIZE_PX, TEXT), Readout::TerminalTitle),
                            (text("", COPY_SIZE_PX, TEXT), Readout::TerminalScore),
                            (text("", CAPTION_SIZE_PX, MUTED), Readout::TerminalBreakdown),
                            (text("", COPY_SIZE_PX, TEXT), Readout::TerminalResources),
                            text(
                                "Score is the current published formula on local counters, not an STP receipt. Arrival means entry to the 50 m station region, not docking.",
                                CAPTION_SIZE_PX,
                                MUTED,
                            ),
                            (
                                button_row(),
                                children![
                                    button("Reset flight", HudAction::AskReset, Readout::Static),
                                    button("Export results", HudAction::Export, Readout::Static),
                                ],
                            ),
                        ],
                    ),
                ],
            ),
        ],
    )).id();
    commands.entity(root).insert_child(0, top_row);
    commands.entity(root).with_children(|parent| {
        parent.spawn(side_panel(Overlay::Flight)).with_children(|panel| {
            task_intro(panel, "Flight", "Measured state for the current flight. Scroll for kinematics and sample history; Tab reaches each section.", HudAction::ToggleInstruments);
            panel.spawn((text("Source -- s · waiting for snapshot", CAPTION_SIZE_PX, MUTED), Readout::TelemetryStatus));
            panel.spawn((text("", COPY_SIZE_PX, TEXT), Readout::Mission));
            for group in [InstrumentGroup::Flight, InstrumentGroup::Ship, InstrumentGroup::Target, InstrumentGroup::Acceleration, InstrumentGroup::Peaks, InstrumentGroup::Mission] {
                instrument_section(panel, group);
            }
            panel.spawn(button("Open measurement definitions", HudAction::ToggleDetails, Readout::Static));
        });
        parent.spawn(side_panel(Overlay::Ship)).with_children(|panel| {
            task_intro(panel, "Ship", "Scroll for live hardware inputs, then measured body readbacks. Enter, Apply or leaving a field commits it; Esc cancels typing.", HudAction::ToggleShip);
            super::editors::spawn_inputs(panel, InstrumentGroup::Actuation);
            instrument_section(panel, InstrumentGroup::Actuation);
            reference(panel, ReferenceSection::ShipApplication, |contents| {
                contents.spawn((text("", CAPTION_SIZE_PX, MUTED), Readout::InstrumentGroup(InstrumentGroup::Actuation)));
            });
            panel.spawn(button("Open measurement definitions", HudAction::ToggleDetails, Readout::Static));
        });
        parent.spawn(side_panel(Overlay::Debris)).with_children(|panel| {
            task_intro(panel, "Debris", "Tune the live stream, then scroll for actual asteroid motion. Count, size and initial ejection settings belong to the new-world draft in Launch.", HudAction::ToggleDebris);
            panel.spawn((text("", CAPTION_SIZE_PX, MUTED), Readout::DebrisPlayback));
            super::editors::spawn_inputs(panel, InstrumentGroup::Stream);
            instrument_section(panel, InstrumentGroup::Stream);
            reference(panel, ReferenceSection::DebrisSampling, |contents| {
                contents.spawn((text("", CAPTION_SIZE_PX, MUTED), Readout::InstrumentGroup(InstrumentGroup::Stream)));
            });
            panel.spawn(button("Open Launch for a new-world draft", HudAction::ToggleLaunch, Readout::Static));
            panel.spawn(button("Open measurement definitions", HudAction::ToggleDetails, Readout::Static));
        });
        parent.spawn(side_panel(Overlay::Launch)).with_children(|panel| {
            panel.spawn(text("Launchpad on Phobos", COPY_SIZE_PX, TEXT));
            panel.spawn(launchpad_button("Place lander", LaunchpadControl::ChooseSurface));
            panel.spawn((text("", COPY_SIZE_PX, TEXT), Readout::LaunchpadSummary));
            panel.spawn((text("", CAPTION_SIZE_PX, MUTED), Readout::LaunchpadHint));
            panel.spawn((button_row(), children![
                launchpad_button("Frame Phobos", LaunchpadControl::FramePhobos),
                button("Close launch", HudAction::ToggleLaunch, Readout::Static),
            ]));
            panel.spawn((text("", COPY_SIZE_PX, TEXT), Readout::LaunchSite));
            reference(panel, ReferenceSection::LaunchpadEffects, |contents| {
                contents.spawn(text("Applying a site seats the lander at rest on the pad and clears thrust. It records an intervention; time, fuel, damage, mission outcome and the L1/L2 target stay unchanged. Reset retains a custom site. Preview and Cancel do not change physics.", CAPTION_SIZE_PX, MUTED));
            });
            instrument_section(panel, InstrumentGroup::World);
            reference(panel, ReferenceSection::LanderPlacement, super::editors::spawn_placement);
            panel.spawn(button("Open measurement definitions", HudAction::ToggleDetails, Readout::Static));
        });
    });
    if let Ok(camera) = cameras.single() {
        commands.entity(root).insert(UiTargetCamera(camera));
    }
}

fn spawn_top_row(commands: &mut Commands) -> Entity {
    // Keep construction stack use bounded instead of adding these toolbars to
    // the root's already large nested bundle.
    commands
        .spawn((
            Node {
                width: percent(100),
                flex_direction: FlexDirection::Column,
                flex_shrink: 0.0,
                align_items: AlignItems::Stretch,
                column_gap: px(PANEL_GAP_PX),
                row_gap: px(PANEL_GAP_PX),
                ..default()
            },
            ScrollArea,
            Hovered::default(),
            LayoutPart::TopRow,
        ))
        .with_children(|parent| {
            parent.spawn((
                Node { width: percent(100), ..panel_style() },
                BackgroundColor(PANEL),
                Overlay::FlightStrip,
                Visibility::Inherited,
                children![
                    (
                        Node { width: percent(100), padding: UiRect::ZERO, ..panel_style() },
                        Pickable::IGNORE,
                        children![
                            (text("T+-- | paused", CAPTION_SIZE_PX, MUTED), Readout::Summary),
                            (text("Python path preview | Waiting for notebook", CAPTION_SIZE_PX, MUTED), Readout::PathBrief),
                        ],
                    ),
                    (
                        Node { column_gap: px(8), ..button_row() },
                        Pickable::IGNORE,
                        children![
                            flight_tile(FlightMetric::STRIP[0]),
                            flight_tile(FlightMetric::STRIP[1]),
                            flight_tile(FlightMetric::STRIP[2]),
                            flight_tile(FlightMetric::STRIP[3]),
                            flight_tile(FlightMetric::STRIP[4]),
                            flight_tile(FlightMetric::STRIP[5]),
                        ],
                    ),
                ],
            ));
            parent.spawn((
                Node { width: percent(100), display: Display::None, ..panel_style() },
                BackgroundColor(PANEL),
                Overlay::LaunchpadPreview,
                Visibility::Hidden,
                children![
                    (button_row(), children![
                        launchpad_button("Apply", LaunchpadControl::Apply),
                        launchpad_button("Cancel", LaunchpadControl::Cancel),
                    ]),
                    (button_row(), children![
                        launchpad_button("North", LaunchpadControl::North),
                        launchpad_button("South", LaunchpadControl::South),
                        launchpad_button("East", LaunchpadControl::East),
                        launchpad_button("West", LaunchpadControl::West),
                    ]),
                    text("Drag the pad or lift grip. Release moves pad and lander without Apply. Esc cancels. Or click a point or use arrows, then Apply.", CAPTION_SIZE_PX, MUTED),
                    (text("", CAPTION_SIZE_PX, TEXT), Readout::LaunchpadSummary),
                ],
            ));
        })
        .id()
}

fn task_intro(parent: &mut ChildSpawnerCommands, title: &str, description: &str, close: HudAction) {
    parent.spawn(text(title, COPY_SIZE_PX, TEXT));
    parent.spawn(button(
        &format!("Close {}", title.to_lowercase()),
        close,
        Readout::Static,
    ));
    parent.spawn(text(description, CAPTION_SIZE_PX, MUTED));
}

fn instrument_section(parent: &mut ChildSpawnerCommands, group: InstrumentGroup) {
    parent
        .spawn((
            Node {
                width: percent(100),
                padding: UiRect::ZERO,
                margin: UiRect::top(px(8)),
                ..panel_style()
            },
            InstrumentSection(group),
        ))
        .with_children(|section| {
            section.spawn(button(
                group.title(),
                HudAction::ToggleInstrumentSection(group),
                Readout::InstrumentSection(group),
            ));
            section
                .spawn((
                    Node {
                        width: percent(100),
                        padding: UiRect::ZERO,
                        ..panel_style()
                    },
                    InstrumentContents(group),
                    Visibility::Inherited,
                ))
                .with_children(|contents| {
                    match group {
                        InstrumentGroup::World => super::editors::spawn_inputs(contents, group),
                        InstrumentGroup::Acceleration
                        | InstrumentGroup::Peaks
                        | InstrumentGroup::Ship
                        | InstrumentGroup::Target => {
                            contents.spawn((
                                text("", CAPTION_SIZE_PX, MUTED),
                                Readout::InstrumentGroup(group),
                            ));
                        }
                        InstrumentGroup::Flight
                        | InstrumentGroup::Mission
                        | InstrumentGroup::Actuation
                        | InstrumentGroup::Stream => {}
                    }
                    for &key in group.instruments() {
                        contents.spawn((
                            Node {
                                width: percent(100),
                                padding: UiRect::ZERO,
                                row_gap: px(0),
                                ..panel_style()
                            },
                            children![
                                text(key.label(), CAPTION_SIZE_PX, MUTED),
                                (text("--", COPY_SIZE_PX, TEXT), Readout::Instrument(key)),
                            ],
                        ));
                    }
                });
        });
}

fn reference(
    parent: &mut ChildSpawnerCommands,
    section: ReferenceSection,
    contents: impl FnOnce(&mut ChildSpawnerCommands),
) {
    parent.spawn(button(
        section.title(),
        HudAction::ToggleReference(section),
        Readout::ReferenceToggle(section),
    ));
    parent
        .spawn((
            Node {
                width: percent(100),
                padding: UiRect::ZERO,
                display: Display::None,
                ..panel_style()
            },
            ReferenceContents(section),
            Visibility::Hidden,
        ))
        .with_children(contents);
}

fn control_guide() -> impl Bundle {
    (
        Node {
            width: percent(100),
            padding: UiRect::ZERO,
            ..panel_style()
        },
        children![
            button(
                "Control guide and input diagnostics +",
                HudAction::ToggleReference(ReferenceSection::ControlGuide),
                Readout::ReferenceToggle(ReferenceSection::ControlGuide)
            ),
            (
                Node {
                    width: percent(100),
                    padding: UiRect::ZERO,
                    display: Display::None,
                    ..panel_style()
                },
                ReferenceContents(ReferenceSection::ControlGuide),
                Visibility::Hidden,
                children![
                    text(
                        "Firing sides are fixed to the lander, not the camera. Body-axis guide: long arrow = +X front, medium = +Y left, short = +Z up. A side jet pushes in the opposite direction.",
                        CAPTION_SIZE_PX,
                        MUTED
                    ),
                    text(
                        "Front/back fire body +X/-X nozzles for -X/+X force. Left/right fire body +Y/-Y nozzles for -Y/+Y force. Up and Lift fire the bottom jet for +Z force; Down fires the top jet for -Z force.",
                        CAPTION_SIZE_PX,
                        MUTED
                    ),
                    text(
                        "Upright lander is a local test reset, not a competition maneuver or controller command. It pauses, matches launchpad rotation, stops spin and clears thrust. Position, linear velocity, time, fuel, damage and mission outcome stay unchanged. Each reset is recorded so notebook experiments can stop. Release movement keys before flying again.",
                        CAPTION_SIZE_PX,
                        MUTED
                    ),
                    (
                        text("Loading current input state", COPY_SIZE_PX, TEXT),
                        Readout::InputSettings
                    ),
                    text(
                        "Lift starts paused flight at reference thrust. Up uses the same limit.\nF follows the lander | Shift+F frames the selected object\nAlt/Option + movement keys: debug camera only, with intuitive forward/left movement | Shift boosts debug movement\nP pause/run | R reset | T rate | L capture/release\n1 orbit | 2 debug FP | 3 third-person | 4 free | 5 overview | 6/7 ship sensors\nTab selects controls | Enter activates | H or Esc closes\nChanging a key blocks manual flight until all old and new movement keys are released. It does not clear a notebook-owned force.",
                        CAPTION_SIZE_PX,
                        MUTED
                    ),
                    text(
                        "Flight algorithms, predictions and controller scheduling belong in the Python notebook. Ship keys replace the current command with manual nozzle forces. Offset side jets can roll or topple the lander. Release keys to stop thrust, not velocity or spin. Opening this panel does not pause flight.",
                        CAPTION_SIZE_PX,
                        MUTED
                    ),
                ],
            ),
        ],
    )
}

fn flight_tile(metric: FlightMetric) -> impl Bundle {
    (
        Node {
            min_width: px(120),
            flex_basis: px(120),
            flex_grow: 1.0,
            flex_direction: FlexDirection::Column,
            flex_shrink: 0.0,
            ..default()
        },
        children![
            atomic_text(metric.strip_label(), CAPTION_SIZE_PX, MUTED),
            (
                text("--", COPY_SIZE_PX, TEXT),
                Readout::Instrument(Instrument::Flight(metric))
            ),
        ],
    )
}

pub(super) fn panel_style() -> Node {
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

pub(super) fn button_row() -> Node {
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

pub(super) fn text(value: &str, size: u32, color: Color) -> impl Bundle {
    text_block(
        value,
        size,
        color,
        Node {
            width: percent(100),
            min_width: px(0),
            flex_shrink: 0.0,
            ..default()
        },
        LineBreak::WordOrCharacter,
    )
}

fn atomic_text(value: &str, size: u32, color: Color) -> impl Bundle {
    text_block(
        value,
        size,
        color,
        Node {
            width: percent(100),
            min_width: px(0),
            flex_shrink: 0.0,
            ..default()
        },
        LineBreak::NoWrap,
    )
}

/// Intrinsic-width labels stay compact in a row, but shrink within the button's
/// content box. The available width excludes the parent's horizontal padding.
pub(super) fn button_text(value: &str, size: u32, color: Color) -> impl Bundle {
    text_block(
        value,
        size,
        color,
        Node {
            flex_shrink: 1.0,
            ..default()
        },
        LineBreak::WordOrCharacter,
    )
}

fn text_block(
    value: &str,
    size: u32,
    color: Color,
    node: Node,
    linebreak: LineBreak,
) -> impl Bundle {
    (
        Text::new(super::readable_text(value)),
        TextFont {
            font_size: FontSize::Px(size as f32),
            ..default()
        },
        node,
        super::text_layout::HudTextLayout::default(),
        TextLayout::linebreak(linebreak),
        LineHeight::RelativeToFont(1.4),
        TextColor(color),
        Pickable::IGNORE,
    )
}

fn launchpad_button(label: &str, control: LaunchpadControl) -> impl Bundle {
    (
        button(label, HudAction::Launchpad(control), Readout::Static),
        crate::launchpad::SurfaceControl,
    )
}

fn button(label: &str, action: HudAction, readout: Readout) -> impl Bundle {
    (
        Button,
        action,
        AccessibleLabel::new(super::readable_text(label)),
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
        children![(button_text(label, COPY_SIZE_PX, TEXT), readout)],
    )
}
