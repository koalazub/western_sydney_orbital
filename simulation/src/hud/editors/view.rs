use super::{model::*, systems};
use crate::hud::{
    model::*,
    view::{button_row, button_text, panel_style, text},
};
use crate::{
    model::{FlightPreset, StreamAxis},
    telemetry::InstrumentGroup,
};
use bevy::{
    input_focus::tab_navigation::TabIndex,
    picking::hover::Hovered,
    prelude::*,
    text::{EditableText, EditableTextFilter, TextCursorStyle},
    ui_widgets::{Button, SelectAllOnFocus, TextInput, observe},
};

pub(in crate::hud) fn spawn_inputs(parent: &mut ChildSpawnerCommands, group: InstrumentGroup) {
    match group {
        InstrumentGroup::Actuation => {
            parent.spawn(text("Live hardware inputs", COPY_SIZE_PX, TEXT));
            parent.spawn(text(
                "Edits keep the current world, time, command owner and playback state.",
                CAPTION_SIZE_PX,
                MUTED,
            ));
            for field in EditorField::SHIP {
                numeric(parent, field);
            }
            status(parent, EditorScope::Ship);
            parent.spawn(text("Observed outputs, read-only", COPY_SIZE_PX, TEXT));
        }
        InstrumentGroup::Stream => {
            parent.spawn(text("Live stream inputs", COPY_SIZE_PX, TEXT));
            parent.spawn((text("", CAPTION_SIZE_PX, MUTED), EditorReadout::FlowSummary));
            for field in EditorField::FLOW {
                numeric(parent, field);
            }
            parent.spawn(text("Stream axis and sign", CAPTION_SIZE_PX, MUTED));
            parent.spawn((
                button_row(),
                children![
                    action(
                        "X",
                        "Use stream axis X",
                        EditorAction::FlowAxis(StreamAxis::X)
                    ),
                    action(
                        "Y",
                        "Use stream axis Y",
                        EditorAction::FlowAxis(StreamAxis::Y)
                    ),
                    action(
                        "Z",
                        "Use stream axis Z",
                        EditorAction::FlowAxis(StreamAxis::Z)
                    ),
                    action(
                        "Reverse sign",
                        "Reverse sign of stream",
                        EditorAction::ReverseFlow
                    ),
                ],
            ));
            status(parent, EditorScope::Flow);
            parent.spawn(text(
                "Observed outputs and reference readback, read-only",
                COPY_SIZE_PX,
                TEXT,
            ));
        }
        InstrumentGroup::World => world(parent),
        InstrumentGroup::Flight
        | InstrumentGroup::Acceleration
        | InstrumentGroup::Peaks
        | InstrumentGroup::Ship
        | InstrumentGroup::Target
        | InstrumentGroup::Mission => {}
    }
}

fn world(parent: &mut ChildSpawnerCommands) {
    parent.spawn(text("New world draft", COPY_SIZE_PX, TEXT));
    parent.spawn((
        text("", CAPTION_SIZE_PX, MUTED),
        EditorReadout::WorldSummary,
    ));
    parent.spawn(text(
        "New-world launch side and target",
        CAPTION_SIZE_PX,
        MUTED,
    ));
    parent.spawn((
        button_row(),
        children![
            action(
                "Mars-facing L1",
                "Select Mars-facing L1 for a new world",
                EditorAction::Preset(FlightPreset::MarsFacingL1)
            ),
            action(
                "Far-side L2",
                "Select far-side L2 for a new world",
                EditorAction::Preset(FlightPreset::FarSideL2)
            ),
        ],
    ));
    parent.spawn(text(
        "New-world asteroid distribution",
        CAPTION_SIZE_PX,
        MUTED,
    ));
    parent.spawn((
        button_row(),
        children![
            action(
                "Directed stream",
                "Select directed stream for a new world",
                EditorAction::Distribution(DistributionChoice::Stream)
            ),
            action(
                "Impact ejecta",
                "Select impact ejecta for a new world",
                EditorAction::Distribution(DistributionChoice::Impact)
            ),
        ],
    ));
    numeric(parent, EditorField::WorldCount);
    parent.spawn((
        text("", CAPTION_SIZE_PX, MUTED),
        EditorReadout::ImpactSummary,
    ));
    parent.spawn(action(
        "Size, shell and ejection inputs +",
        "Size, shell and ejection inputs",
        EditorAction::ToggleAdvanced,
    ));
    parent.spawn((
        Node { width: percent(100), padding: UiRect::ZERO, display: Display::None, ..panel_style() },
        EditorDisclosure::Advanced,
        Visibility::Hidden,
    )).with_children(|advanced| {
        advanced.spawn(text("New-world draft only. Ship mass/inertia and stream reference use the confirmed live inputs; local thrust gain starts at 1x in the new world.", CAPTION_SIZE_PX, MUTED));
        for field in EditorField::WORLD.into_iter().skip(1) { numeric(advanced, field); }
        advanced.spawn((
            Node { width: percent(100), padding: UiRect::ZERO, display: Display::None, ..panel_style() },
            EditorDisclosure::Impact,
            Visibility::Hidden,
        )).with_children(|impact| {
            impact.spawn(text("Initial ejection parameters", COPY_SIZE_PX, TEXT));
            impact.spawn(text("This is an initial post-impact cloud, not a fracture solver or an orbit controller. Changes replace the world only with Create paused world.", CAPTION_SIZE_PX, MUTED));
            impact.spawn(action("Reset ejection defaults", "Reset draft ejection parameters to versioned defaults", EditorAction::ResetImpact));
            for field in EditorField::IMPACT { numeric(impact, field); }
        });
    });
    parent.spawn(action(
        "Create paused world",
        "Create paused world, review replacement",
        EditorAction::AskCreateWorld,
    ));
    parent.spawn((
        Node { width: percent(100), padding: UiRect::ZERO, display: Display::None, ..panel_style() },
        EditorDisclosure::CreateConfirmation,
        Visibility::Hidden,
    )).with_children(|confirmation| {
        confirmation.spawn(text("Replace this run with the drafted world? Time returns to zero and thrust clears; the current flight cannot be recovered here.", COPY_SIZE_PX, TEXT));
        confirmation.spawn((button_row(), children![
            action("Replace run at time zero", "Replace run at time zero with the drafted world", EditorAction::CreateWorld),
            action("Keep current run", "Keep current run and cancel world creation", EditorAction::CancelCreateWorld),
        ]));
    });
    status(parent, EditorScope::World);
}

pub(in crate::hud) fn spawn_placement(parent: &mut ChildSpawnerCommands) {
    parent.spawn((
        Node {
            margin: UiRect::top(px(12)),
            width: percent(100),
            padding: UiRect::ZERO,
            ..panel_style()
        },
        children![text(
            "Debug-only XYZ, moves lander only",
            COPY_SIZE_PX,
            TEXT
        )],
    ));
    parent.spawn((
        text("", CAPTION_SIZE_PX, MUTED),
        EditorReadout::PlacementSummary,
    ));
    parent.spawn(text("Debug-only. This moves only the lander, not the launchpad. Stage XYZ, then Apply XYZ position. It pauses flight, clears motion/thrust and records an intervention without changing time, fuel, damage or mission outcome. To move both together, use Place lander above.", CAPTION_SIZE_PX, MUTED));
    for field in EditorField::PLACEMENT {
        numeric(parent, field);
    }
    parent.spawn((
        button_row(),
        children![
            action(
                "Apply XYZ position",
                "Apply XYZ position to the lander only and pause, debug-only",
                EditorAction::PlaceLander
            ),
            action(
                "Reload current XYZ",
                "Reload the current measured lander XYZ position",
                EditorAction::ReloadPlacement
            ),
        ],
    ));
    status(parent, EditorScope::Placement);
}

fn numeric(parent: &mut ChildSpawnerCommands, field: EditorField) {
    parent
        .spawn(Node {
            width: percent(100),
            padding: UiRect::ZERO,
            margin: UiRect::top(px(4)),
            ..panel_style()
        })
        .with_children(|row| {
            row.spawn(text(field.label(), CAPTION_SIZE_PX, TEXT));
            row.spawn(Node {
                width: percent(100),
                ..button_row()
            })
            .with_children(|controls| {
                controls.spawn((
                    (
                        TextInput,
                        InstrumentEditor,
                        field,
                        EditorBuffer::default(),
                        EditableText {
                            max_characters: Some(40),
                            visible_width: Some(12.0),
                            ..EditableText::new("--")
                        },
                        EditableTextFilter::new(|character| {
                            character.is_ascii_digit()
                                || matches!(character, '.' | '-' | '+' | 'e' | 'E')
                                || character.is_ascii_whitespace()
                        }),
                        SelectAllOnFocus,
                        TextCursorStyle {
                            color: TEXT,
                            selection_color: SELECTED,
                            unfocused_selection_color: Color::NONE,
                            selected_text_color: Some(TEXT),
                            ..default()
                        },
                        TextFont {
                            font_size: FontSize::Px(COPY_SIZE_PX as f32),
                            ..default()
                        },
                        TextColor(TEXT),
                        TextLayout::no_wrap(),
                    ),
                    (
                        AccessibleLabel::new(field.label()),
                        TabIndex(0),
                        Node {
                            min_width: px(48),
                            max_width: percent(100),
                            min_height: px(BUTTON_HEIGHT_PX),
                            flex_basis: px(140),
                            flex_grow: 1.0,
                            flex_shrink: 1.0,
                            padding: UiRect::axes(px(BUTTON_PADDING_X_PX), px(BUTTON_PADDING_Y_PX)),
                            border_radius: BorderRadius::all(px(4)),
                            overflow: Overflow::clip(),
                            ..default()
                        },
                        BackgroundColor(BUTTON),
                        Outline::new(px(1), px(1), Color::NONE),
                        observe(systems::on_key),
                    ),
                ));
                controls.spawn(action(
                    "-",
                    &format!("Decrease {} (-)", field.label()),
                    EditorAction::Nudge(field, StepDirection::Decrease),
                ));
                controls.spawn(action(
                    "+",
                    &format!("Increase {} (+)", field.label()),
                    EditorAction::Nudge(field, StepDirection::Increase),
                ));
                controls.spawn(action(
                    field.commit_label(),
                    &format!("{} {}", field.commit_label(), field.label()),
                    EditorAction::Commit(field),
                ));
            });
            row.spawn(text(field.hint(), CAPTION_SIZE_PX, MUTED));
            row.spawn((text("", CAPTION_SIZE_PX, TEXT), EditorReadout::Field(field)));
        });
}

fn status(parent: &mut ChildSpawnerCommands, scope: EditorScope) {
    parent.spawn((text("", CAPTION_SIZE_PX, TEXT), EditorReadout::Scope(scope)));
}

fn action(label: &str, accessible_label: &str, action: EditorAction) -> impl Bundle {
    let label = crate::hud::readable_text(label);
    (
        Button,
        InstrumentEditor,
        action,
        AccessibleLabel::new(crate::hud::readable_text(accessible_label)),
        Hovered::default(),
        Node {
            min_width: px(28),
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
        observe(systems::on_action),
        observe(systems::on_button_key),
        children![button_text(&label, COPY_SIZE_PX, TEXT)],
    )
}
