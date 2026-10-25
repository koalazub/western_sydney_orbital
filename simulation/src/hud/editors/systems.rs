use super::model::*;
use crate::hud::model::{BUTTON, DISABLED, FOCUSED, HOVERED, HudState, SELECTED, SidePanel, TEXT};
use crate::{
    EngineSet, PlaybackState,
    model::{DebrisDistribution, PhysicsCommand, Snapshot},
};
use bevy::{
    a11y::AccessibilityNode,
    ecs::system::SystemParam,
    input::{ButtonState, keyboard::KeyboardInput},
    input_focus::{
        FocusedInput, InputFocus, InputFocusSystems, pointer_focus::PointerFocusPlugin,
        tab_navigation::TabNavigationPlugin,
    },
    picking::hover::Hovered,
    prelude::*,
    text::{EditableText, EditableTextSystems, TextEdit, TextReadWriteMode},
    ui::{InteractionDisabled, Pressed},
    ui_widgets::{Activate, TextInput, TextInputPlugin},
};
use std::cell::Cell;

#[derive(Clone, Copy, Default)]
enum InputGuard {
    #[default]
    Idle,
    Focused,
    Releasing,
}

thread_local! { static INPUT_GUARD: Cell<InputGuard> = const { Cell::new(InputGuard::Idle) }; }

/// Includes the final blur/commit frame and held keys after leaving a field.
/// This guard does not send Stop or change the API-owned command.
pub(crate) fn instrument_editing() -> bool {
    INPUT_GUARD.with(|guard| !matches!(guard.get(), InputGuard::Idle))
}

pub(in crate::hud) fn begin_input_frame(focused: bool, keys: &ButtonInput<KeyCode>) {
    INPUT_GUARD.with(|guard| {
        guard.set(if focused {
            InputGuard::Focused
        } else {
            match guard.get() {
                InputGuard::Idle => InputGuard::Idle,
                InputGuard::Focused => InputGuard::Releasing,
                InputGuard::Releasing if keys.get_pressed().next().is_none() => InputGuard::Idle,
                InputGuard::Releasing => InputGuard::Releasing,
            }
        });
    });
}

pub(in crate::hud) fn install(app: &mut App) {
    INPUT_GUARD.with(|guard| guard.set(InputGuard::Idle));
    if !app.is_plugin_added::<TextInputPlugin>() {
        app.add_plugins(TextInputPlugin);
    }
    if !app.is_plugin_added::<PointerFocusPlugin>() {
        app.add_plugins(PointerFocusPlugin);
    }
    if !app.is_plugin_added::<TabNavigationPlugin>() {
        app.add_plugins(TabNavigationPlugin);
    }
    app.init_resource::<InstrumentEditors>()
        .add_systems(
            Update,
            prepare
                .in_set(EngineSet::Input)
                .before(crate::hud::systems::interactions),
        )
        .add_systems(
            Update,
            update_views
                .in_set(EngineSet::Hud)
                .after(crate::hud::systems::update_instrument_sections),
        )
        // Enter can arrive in the same frame as text/IME/paste. Read the real
        // EditableText only after Bevy has applied those edits.
        .add_systems(
            PostUpdate,
            (apply_intents, sync_values)
                .chain()
                .after(EditableTextSystems)
                .after(InputFocusSystems::FocusChangeEvents),
        );
}

fn position(snapshot: &Snapshot) -> Option<[f64; 3]> {
    snapshot
        .assets
        .iter()
        .find(|actor| actor.id == crate::domain::EntityId::Lander)
        .map(|actor| actor.position)
        .filter(|value| value.iter().all(|value| value.is_finite()))
}

fn reload_all(state: &mut InstrumentEditors, snapshot: &Snapshot) {
    let confirmed = ConfirmedInputs::from(snapshot);
    state.world = Some(confirmed.world_draft());
    state.placement = position(snapshot);
    state.confirmed = Some(confirmed);
    state.world_revision = Some(crate::session::trial_revision());
    state.reload = Reload::All;
    state.session = EditSession::Idle;
    state.field_notices.clear();
    state.scope_notices.clear();
    state.create_confirmation = Disclosure::Collapsed;
}

fn prepare(
    input_focus: Res<InputFocus>,
    inputs: Query<&EditorField, With<TextInput>>,
    editors: Query<(), With<InstrumentEditor>>,
    mut state: ResMut<InstrumentEditors>,
) {
    let snapshot = crate::snapshot();
    let current = ConfirmedInputs::from(snapshot.as_ref());
    if input_focus
        .get()
        .is_some_and(|entity| editors.contains(entity))
    {
        INPUT_GUARD.with(|guard| guard.set(InputGuard::Focused));
    }
    match state.confirmed.as_ref() {
        None => reload_all(&mut state, &snapshot),
        Some(before) => {
            let world_changed = current.seed != before.seed
                || current.preset != before.preset
                || current.experiment != before.experiment
                || current.time_s < before.time_s
                || state.world_revision != Some(crate::session::trial_revision());
            let live_changed = current.ship != before.ship
                || current.flow != before.flow
                || current.revision != before.revision;
            let placed = current.interventions != before.interventions;
            if world_changed {
                reload_all(&mut state, &snapshot);
                state.intents.clear();
                state.notice_scope(
                    EditorScope::World,
                    InputNotice::Diverged("World setup changed outside this editor.".into()),
                );
            } else {
                if live_changed {
                    state.reload = Reload::Live;
                    if let EditSession::Editing { field, .. } = state.session
                        && matches!(field.scope(), EditorScope::Ship | EditorScope::Flow)
                    {
                        state.session = EditSession::Idle;
                    }
                    state.intents.retain(|intent| match intent {
                        EditorIntent::Commit(_, field) | EditorIntent::Cancel(_, field) => {
                            !matches!(field.scope(), EditorScope::Ship | EditorScope::Flow)
                        }
                        EditorIntent::Action(
                            EditorAction::Commit(field) | EditorAction::Nudge(field, _),
                        ) => !matches!(field.scope(), EditorScope::Ship | EditorScope::Flow),
                        EditorIntent::Action(
                            EditorAction::FlowAxis(_) | EditorAction::ReverseFlow,
                        ) => false,
                        EditorIntent::Action(
                            EditorAction::Preset(_)
                            | EditorAction::Distribution(_)
                            | EditorAction::ToggleAdvanced
                            | EditorAction::ResetImpact
                            | EditorAction::AskCreateWorld
                            | EditorAction::CancelCreateWorld
                            | EditorAction::CreateWorld
                            | EditorAction::PlaceLander
                            | EditorAction::ReloadPlacement,
                        ) => true,
                    });
                    state.notice_scope(
                        EditorScope::Ship,
                        InputNotice::Diverged(
                            "Live calibration changed outside this editor.".into(),
                        ),
                    );
                    state.notice_scope(
                        EditorScope::Flow,
                        InputNotice::Diverged(
                            "Live calibration changed outside this editor.".into(),
                        ),
                    );
                }
                if placed {
                    state.placement = position(&snapshot);
                    state.reload = Reload::All;
                    state.notice_scope(
                        EditorScope::Placement,
                        InputNotice::Diverged(
                            "A recorded lander intervention changed its state.".into(),
                        ),
                    );
                }
                state.confirmed = Some(current);
            }
        }
    }
    let focused = input_focus
        .get()
        .and_then(|entity| inputs.get(entity).ok().map(|field| (entity, *field)));
    if let EditSession::Editing { entity, field } = state.session
        && focused.is_none_or(|(next, _)| next != entity)
    {
        // Blur commits numeric calibration or stages setup values. The explicit
        // world/placement action is still the only operation that moves objects.
        state
            .intents
            .push_front(EditorIntent::Commit(entity, field));
    }
    state.session = match focused {
        Some((entity, field)) => EditSession::Editing { entity, field },
        None => EditSession::Idle,
    };
}

pub(super) fn on_key(
    mut event: On<FocusedInput<KeyboardInput>>,
    fields: Query<(&EditorField, &EditableText), With<TextInput>>,
    mut state: ResMut<InstrumentEditors>,
) {
    let Ok((field, text)) = fields.get(event.focused_entity) else {
        return;
    };
    if text.is_composing() {
        event.propagate(false);
        return;
    }
    if event.input.state != ButtonState::Pressed || event.input.repeat {
        return;
    }
    let intent = match event.input.key_code {
        KeyCode::Enter => EditorIntent::Commit(event.focused_entity, *field),
        KeyCode::Escape => EditorIntent::Cancel(event.focused_entity, *field),
        _ => return,
    };
    INPUT_GUARD.with(|guard| guard.set(InputGuard::Focused));
    state.intents.push_back(intent);
    event.propagate(false);
}

pub(super) fn on_action(
    event: On<Activate>,
    actions: Query<&EditorAction>,
    hud: Res<HudState>,
    playback: Res<PlaybackState>,
    mut state: ResMut<InstrumentEditors>,
) {
    let Ok(action) = actions.get(event.entity) else {
        return;
    };
    if !action_available(&hud, &playback, *action) {
        return;
    }
    state.intents.push_back(EditorIntent::Action(*action));
}

pub(super) fn on_button_key(
    mut event: On<FocusedInput<KeyboardInput>>,
    buttons: Query<(), With<EditorAction>>,
    mut hud: ResMut<HudState>,
    mut state: ResMut<InstrumentEditors>,
) {
    if buttons.contains(event.focused_entity)
        && event.input.state == ButtonState::Pressed
        && event.input.key_code == KeyCode::Escape
        && !event.input.repeat
    {
        event.propagate(false);
        if state.create_confirmation == Disclosure::Expanded {
            state.create_confirmation = Disclosure::Collapsed;
        } else {
            hud.panel = hud.panel.close_top();
        }
    }
}

fn available(hud: &HudState, playback: &PlaybackState) -> bool {
    interface_available(hud) && playback.can_control
}

fn interface_available(hud: &HudState) -> bool {
    crate::camera::input_active()
        && !hud.reset_confirm_open
        && !crate::controls::binding_actions_blocked()
        && !crate::launchpad::input_blocked()
}

fn action_available(hud: &HudState, playback: &PlaybackState, action: EditorAction) -> bool {
    action_in_panel(hud, action)
        && match action {
            EditorAction::ToggleAdvanced | EditorAction::CancelCreateWorld => {
                interface_available(hud)
            }
            EditorAction::Commit(_)
            | EditorAction::Nudge(_, _)
            | EditorAction::FlowAxis(_)
            | EditorAction::ReverseFlow
            | EditorAction::Preset(_)
            | EditorAction::Distribution(_)
            | EditorAction::ResetImpact
            | EditorAction::AskCreateWorld
            | EditorAction::CreateWorld
            | EditorAction::PlaceLander
            | EditorAction::ReloadPlacement => available(hud, playback),
        }
}

fn scope_in_panel(hud: &HudState, scope: EditorScope) -> bool {
    match scope {
        EditorScope::Ship => hud.panel == SidePanel::Ship,
        EditorScope::Flow => hud.panel == SidePanel::Debris,
        EditorScope::World | EditorScope::Placement => hud.panel == SidePanel::Launch,
    }
}

fn action_in_panel(hud: &HudState, action: EditorAction) -> bool {
    match action {
        EditorAction::Commit(field) | EditorAction::Nudge(field, _) => {
            scope_in_panel(hud, field.scope())
        }
        EditorAction::FlowAxis(_) | EditorAction::ReverseFlow => {
            scope_in_panel(hud, EditorScope::Flow)
        }
        EditorAction::Preset(_)
        | EditorAction::Distribution(_)
        | EditorAction::ToggleAdvanced
        | EditorAction::ResetImpact
        | EditorAction::AskCreateWorld
        | EditorAction::CancelCreateWorld
        | EditorAction::CreateWorld => scope_in_panel(hud, EditorScope::World),
        EditorAction::PlaceLander | EditorAction::ReloadPlacement => {
            scope_in_panel(hud, EditorScope::Placement)
        }
    }
}

fn field_available(state: &InstrumentEditors, field: EditorField) -> bool {
    match field {
        EditorField::StreamSpeed | EditorField::StreamSpread => state
            .confirmed
            .as_ref()
            .is_some_and(ConfirmedInputs::stream_active),
        EditorField::ImpactSource(_)
        | EditorField::ImpactKick(_)
        | EditorField::ImpactTangentialSpeed
        | EditorField::ImpactPatchAngle => state.impact().is_some(),
        EditorField::WorldCount
        | EditorField::RadiusMin
        | EditorField::RadiusMax
        | EditorField::ShellInner
        | EditorField::ShellOuter => state.world.is_some(),
        EditorField::Mass | EditorField::InertiaFactor | EditorField::ThrustGain => {
            state.confirmed.is_some()
        }
        EditorField::Placement(_) => state.placement.is_some(),
    }
}

fn apply_intents(
    mut state: ResMut<InstrumentEditors>,
    mut fields: FieldQuery,
    hud: Res<HudState>,
    playback: Res<PlaybackState>,
) {
    if state.intents.is_empty() {
        return;
    }
    let mut intents = std::mem::take(&mut state.intents);
    let cancelled: Vec<_> = intents
        .iter()
        .filter_map(|intent| match intent {
            EditorIntent::Cancel(entity, _) => Some(*entity),
            _ => None,
        })
        .collect();
    // A real asynchronous clipboard read must finish before a queued commit or
    // an explicit world action can use the text. Do not submit the previous value.
    if intents.iter().any(|intent| match intent {
        EditorIntent::Commit(entity, _) if !cancelled.contains(entity) => fields
            .get(*entity)
            .is_ok_and(|(_, text, _)| text.pending_paste.is_some()),
        _ => false,
    }) {
        state.intents = intents;
        return;
    }
    let mut rejected_scope = None;
    while let Some(intent) = intents.pop_front() {
        match intent {
            EditorIntent::Cancel(entity, field) => {
                if let Ok((_, mut text, mut buffer)) = fields.get_mut(entity) {
                    reset_text(&mut text, &mut buffer, state.value(field), false);
                }
            }
            EditorIntent::Commit(entity, field) => {
                if cancelled.contains(&entity) {
                    continue;
                }
                let Ok((_, mut text, mut buffer)) = fields.get_mut(entity) else {
                    continue;
                };
                let value = text.value().to_string();
                if value == buffer.0 {
                    continue;
                }
                let result = if available(&hud, &playback) && field_available(&state, field) {
                    field
                        .parse(&value)
                        .and_then(|value| apply_value(&mut state, field, value))
                } else {
                    Err("This input is unavailable. Request control, finish launchpad placement if active, then retry in Ship, Debris or Launch.".into())
                };
                if let Err(error) = result {
                    reject(&mut state, field.scope(), error.clone());
                    state.notice_field(field, InputNotice::Rejected(error));
                    rejected_scope = Some(field.scope());
                }
                reset_text(&mut text, &mut buffer, state.value(field), true);
            }
            EditorIntent::Action(action) => {
                if !action_available(&hud, &playback, action) {
                    continue;
                }
                if matches!(action, EditorAction::CreateWorld)
                    && rejected_scope == Some(EditorScope::World)
                    || matches!(action, EditorAction::PlaceLander)
                        && rejected_scope == Some(EditorScope::Placement)
                {
                    continue;
                }
                let result = apply_action(&mut state, action, &mut fields);
                if let Err((scope, error)) = result {
                    reject(&mut state, scope, error.clone());
                    if let EditorAction::Commit(field) | EditorAction::Nudge(field, _) = action {
                        state.notice_field(field, InputNotice::Rejected(error));
                    }
                }
            }
        }
    }
}

fn reject(state: &mut InstrumentEditors, scope: EditorScope, error: String) {
    let snapshot = crate::snapshot();
    let confirmed = ConfirmedInputs::from(snapshot.as_ref());
    match scope {
        EditorScope::Ship | EditorScope::Flow => state.reload = Reload::Live,
        EditorScope::World => {
            state.world = Some(confirmed.world_draft());
            state.reload = Reload::All;
        }
        EditorScope::Placement => {
            state.placement = position(&snapshot);
            state.reload = Reload::All;
        }
    }
    if matches!(scope, EditorScope::World | EditorScope::Placement) {
        state
            .field_notices
            .retain(|(field, _)| field.scope() != scope);
    }
    state.confirmed = Some(confirmed);
    state.world_revision = Some(crate::session::trial_revision());
    state.notice_scope(scope, InputNotice::Rejected(error));
}

fn apply_value(
    state: &mut InstrumentEditors,
    field: EditorField,
    value: f64,
) -> Result<(), String> {
    let confirmed = state
        .confirmed
        .as_ref()
        .ok_or("Wait for a current-world snapshot, then retry.")?;
    let value = field.parse(&value.to_string())?;
    let mut ship = confirmed.ship;
    let mut flow = confirmed.flow;
    let mut draft = state.world.unwrap_or_else(|| confirmed.world_draft());
    let mut placement = state.placement;
    match field {
        EditorField::Mass => ship.mass_kg = value,
        EditorField::InertiaFactor => ship.inertia_scale = value,
        EditorField::ThrustGain => ship.thrust_scale = value,
        EditorField::StreamSpeed => flow.speed_mps = value,
        EditorField::StreamSpread => flow.speed_spread_mps = value,
        EditorField::WorldCount => draft.settings.debris.count = value as usize,
        EditorField::RadiusMin => draft.settings.debris.radius_min_m = value,
        EditorField::RadiusMax => draft.settings.debris.radius_max_m = value,
        EditorField::ShellInner => draft.settings.debris.shell_inner_m = value,
        EditorField::ShellOuter => draft.settings.debris.shell_outer_m = value,
        EditorField::Placement(axis) => {
            if value.abs() > crate::scenario::settings().limits.position_m {
                return Err(
                    "Enter a position within the world position limit, then retry Place lander."
                        .into(),
                );
            }
            placement.as_mut().ok_or(
                "Current lander position is unavailable. Reload its position, then retry.",
            )?[axis.index()] = value;
        }
        EditorField::ImpactSource(axis) => {
            impact_settings(&mut draft)?.source_direction[axis.index()] = value
        }
        EditorField::ImpactKick(axis) => {
            impact_settings(&mut draft)?.kick_velocity_mps[axis.index()] = value
        }
        EditorField::ImpactTangentialSpeed => {
            impact_settings(&mut draft)?.tangential_speed_mps = value
        }
        EditorField::ImpactPatchAngle => impact_settings(&mut draft)?.patch_half_angle_deg = value,
    }
    match field.scope() {
        EditorScope::Ship => {
            ship.validate()?;
            if ship != confirmed.ship {
                dispatch(
                    state,
                    EditorScope::Ship,
                    PhysicsCommand::TuneShip { settings: ship },
                )?;
                if let Err(error) = verify_ship(ship) {
                    state.notice_field(field, InputNotice::Diverged(error.clone()));
                    state.notice_scope(EditorScope::Ship, InputNotice::Diverged(error));
                    return Ok(());
                }
            }
            state.notice_field(field, InputNotice::Applied);
        }
        EditorScope::Flow => {
            if !confirmed.stream_active() {
                return Err("Impact ejecta has no live stream tuning. Edit the new-world ejection draft in Launch, then create a new world.".into());
            }
            flow.validate()?;
            if flow != confirmed.flow {
                dispatch(
                    state,
                    EditorScope::Flow,
                    PhysicsCommand::TuneDebrisFlow { settings: flow },
                )?;
            }
            state.notice_field(field, InputNotice::Applied);
        }
        EditorScope::World => {
            draft.settings.validate()?;
            state.world = Some(draft);
            state.notice_field(field, InputNotice::Staged);
            state.notice_scope(EditorScope::World, InputNotice::Staged);
        }
        EditorScope::Placement => {
            state.placement = placement;
            state.notice_field(field, InputNotice::Staged);
            state.notice_scope(EditorScope::Placement, InputNotice::Staged);
        }
    }
    Ok(())
}

fn impact_settings(
    draft: &mut WorldDraft,
) -> Result<&mut crate::model::ImpactEjectaSettings, String> {
    match &mut draft.settings.debris.distribution {
        DebrisDistribution::DirectedStream => Err(
            "Select Impact ejecta in the world draft before editing ejection parameters.".into(),
        ),
        DebrisDistribution::ImpactEjecta { settings } => Ok(settings),
    }
}

type FieldQuery<'w, 's> = Query<
    'w,
    's,
    (
        &'static EditorField,
        &'static mut EditableText,
        &'static mut EditorBuffer,
    ),
    With<TextInput>,
>;

fn apply_action(
    state: &mut InstrumentEditors,
    action: EditorAction,
    fields: &mut FieldQuery,
) -> Result<(), (EditorScope, String)> {
    match action {
        EditorAction::Commit(field) | EditorAction::Nudge(field, _) => {
            let mut value = state.value(field).ok_or_else(|| {
                (
                    field.scope(),
                    "Current input value is unavailable. Reload the current world first.".into(),
                )
            })?;
            if let Some((_, text, _)) = fields.iter().find(|(key, _, _)| **key == field) {
                value = field
                    .parse(&text.value().to_string())
                    .map_err(|error| (field.scope(), error))?;
            }
            if let EditorAction::Nudge(_, direction) = action {
                value += match direction {
                    StepDirection::Decrease => -field.step(),
                    StepDirection::Increase => field.step(),
                };
                if (value * 1e9).is_finite() {
                    value = (value * 1e9).round() / 1e9;
                }
                value = field
                    .parse(&format_number(value))
                    .map_err(|error| (field.scope(), error))?;
            }
            if !field_available(state, field) {
                return Err((field.scope(), "Stream controls are inactive for impact ejecta. Edit the new-world ejection draft in Launch, then create a new world.".into()));
            }
            apply_value(state, field, value).map_err(|error| (field.scope(), error))?;
            for (key, mut text, mut buffer) in fields.iter_mut() {
                if *key == field {
                    reset_text(&mut text, &mut buffer, state.value(field), false);
                }
            }
            Ok(())
        }
        EditorAction::FlowAxis(axis) => {
            let mut settings = active_flow(state)?;
            settings.stream_axis = axis;
            dispatch(
                state,
                EditorScope::Flow,
                PhysicsCommand::TuneDebrisFlow { settings },
            )
            .map_err(|error| (EditorScope::Flow, error))
        }
        EditorAction::ReverseFlow => {
            let mut settings = active_flow(state)?;
            settings.stream_sign = -settings.stream_sign;
            dispatch(
                state,
                EditorScope::Flow,
                PhysicsCommand::TuneDebrisFlow { settings },
            )
            .map_err(|error| (EditorScope::Flow, error))
        }
        EditorAction::Preset(preset) => {
            if let Some(world) = state.world.as_mut() {
                world.preset = preset;
            }
            state.notice_scope(EditorScope::World, InputNotice::Staged);
            Ok(())
        }
        EditorAction::Distribution(choice) => {
            if let Some(world) = state.world.as_mut() {
                world.settings.debris.distribution = match choice {
                    DistributionChoice::Stream => DebrisDistribution::DirectedStream,
                    DistributionChoice::Impact => match world.settings.debris.distribution {
                        DebrisDistribution::DirectedStream => DebrisDistribution::impact_default(),
                        distribution @ DebrisDistribution::ImpactEjecta { .. } => distribution,
                    },
                };
            }
            state.notice_scope(EditorScope::World, InputNotice::Staged);
            state.reload = Reload::All;
            Ok(())
        }
        EditorAction::ToggleAdvanced => {
            state.advanced = match state.advanced {
                Disclosure::Collapsed => Disclosure::Expanded,
                Disclosure::Expanded => Disclosure::Collapsed,
            };
            Ok(())
        }
        EditorAction::ResetImpact => {
            if let Some(world) = state.world.as_mut() {
                world.settings.debris.distribution = DebrisDistribution::impact_default();
            }
            state.notice_scope(EditorScope::World, InputNotice::Staged);
            state.reload = Reload::All;
            Ok(())
        }
        EditorAction::AskCreateWorld => {
            state.create_confirmation = Disclosure::Expanded;
            Ok(())
        }
        EditorAction::CancelCreateWorld => {
            state.create_confirmation = Disclosure::Collapsed;
            Ok(())
        }
        EditorAction::CreateWorld => {
            if state.create_confirmation != Disclosure::Expanded {
                return Ok(());
            }
            let mut draft = state.world.ok_or_else(|| {
                (
                    EditorScope::World,
                    "Wait for current-world settings.".into(),
                )
            })?;
            let current = crate::snapshot();
            let ship = current.ship_tuning();
            draft.settings.ship.mass_kg = ship.mass_kg;
            draft.settings.ship.inertia_scale = ship.inertia_scale;
            draft.settings.debris = current.debris_flow().apply_to(draft.settings.debris);
            draft
                .settings
                .validate()
                .map_err(|error| (EditorScope::World, error))?;
            dispatch(
                state,
                EditorScope::World,
                PhysicsCommand::ConfigureFlight {
                    settings: draft.settings,
                    seed: Some(current.seed),
                    preset: draft.preset,
                },
            )
            .map_err(|error| (EditorScope::World, error))
        }
        EditorAction::PlaceLander => {
            let position_m = state.placement.ok_or_else(|| {
                (
                    EditorScope::Placement,
                    "Reload the current lander position, then enter XYZ.".into(),
                )
            })?;
            dispatch(
                state,
                EditorScope::Placement,
                PhysicsCommand::PlaceLander { position_m },
            )
            .map_err(|error| (EditorScope::Placement, error))
        }
        EditorAction::ReloadPlacement => {
            state.placement = position(&crate::snapshot());
            state.reload = Reload::All;
            Ok(())
        }
    }
}

fn active_flow(
    state: &InstrumentEditors,
) -> Result<crate::model::DebrisFlowTuning, (EditorScope, String)> {
    let confirmed = state
        .confirmed
        .as_ref()
        .ok_or_else(|| (EditorScope::Flow, "Wait for current flow settings.".into()))?;
    if !confirmed.stream_active() {
        return Err((EditorScope::Flow, "Impact ejecta has no live stream tuning. Edit the new-world ejection draft in Launch, then create a new world.".into()));
    }
    Ok(confirmed.flow)
}

fn dispatch(
    state: &mut InstrumentEditors,
    scope: EditorScope,
    command: PhysicsCommand,
) -> Result<(), String> {
    // Do not read a request echo or route native commands through JSON parsing.
    crate::session::dispatch(crate::api::Dispatch::Physics(command))?;
    let snapshot = crate::snapshot();
    match scope {
        EditorScope::Ship | EditorScope::Flow => {
            state.confirmed = Some(ConfirmedInputs::from(snapshot.as_ref()));
            state.reload = Reload::Live;
        }
        EditorScope::World => reload_all(state, &snapshot),
        EditorScope::Placement => {
            state.placement = position(&snapshot);
            state.confirmed = Some(ConfirmedInputs::from(snapshot.as_ref()));
            state.reload = Reload::All;
        }
    }
    if scope == EditorScope::Placement {
        state
            .field_notices
            .retain(|(field, _)| field.scope() != EditorScope::Placement);
    }
    state.world_revision = Some(crate::session::trial_revision());
    state.notice_scope(scope, InputNotice::Applied);
    Ok(())
}

fn verify_ship(settings: crate::model::ShipTuning) -> Result<(), String> {
    let snapshot = crate::snapshot();
    let body = crate::session::applied_physics()?;
    let near =
        |actual: f64, expected: f64| (actual - expected).abs() <= expected.abs().max(1.0) * 1e-9;
    if snapshot.ship_tuning() != settings
        || body.tuning_revision != snapshot.tuning_revision
        || body.source_time_s != snapshot.time_s
        || body.frame != snapshot.frame
        || !near(body.mass_kg, settings.mass_kg)
        || !near(body.actuator_gain, settings.thrust_scale)
        || body
            .inertia_body_kg_m2
            .iter()
            .flatten()
            .zip(settings.inertia_kg_m2().iter().flatten())
            .any(|(actual, expected)| !near(*actual, *expected))
    {
        return Err("Live body readback differs from the calibration. Current-world values have been reloaded; check the measured body mass and inertia before retrying.".into());
    }
    Ok(())
}

fn reset_text(
    text: &mut EditableText,
    buffer: &mut EditorBuffer,
    value: Option<f64>,
    select: bool,
) {
    let desired = value.map(format_number).unwrap_or_else(|| "--".into());
    text.editor_mut().set_text(&desired);
    text.pending_edits.clear();
    text.pending_paste = None;
    text.queue_edit(if select {
        TextEdit::SelectAll
    } else {
        TextEdit::TextEnd(false)
    });
    buffer.0 = desired;
}

fn format_number(value: f64) -> String {
    if !value.is_finite() {
        return "--".into();
    }
    if value.abs() >= 1e9 || value != 0.0 && value.abs() < 1e-6 {
        format!("{value:e}")
    } else {
        value.to_string()
    }
}

fn sync_values(
    input_focus: Res<InputFocus>,
    mut state: ResMut<InstrumentEditors>,
    mut fields: FieldQuery,
) {
    let reload = state.reload;
    for (field, mut text, mut buffer) in &mut fields {
        let desired = state
            .value(*field)
            .map(format_number)
            .unwrap_or_else(|| "--".into());
        let focused = match state.session {
            EditSession::Editing {
                entity,
                field: active,
            } => active == *field && input_focus.get() == Some(entity),
            EditSession::Idle => false,
        };
        let forced = match reload {
            Reload::None => false,
            Reload::Live => matches!(field.scope(), EditorScope::Ship | EditorScope::Flow),
            Reload::All => true,
        };
        let pending = state
            .intents
            .iter()
            .any(|intent| matches!(intent, EditorIntent::Commit(_, key) if *key == *field));
        if forced || (!focused && !pending && buffer.0 != desired) {
            reset_text(&mut text, &mut buffer, state.value(*field), focused);
        }
    }
    state.reload = Reload::None;
}

type InputVisual = (
    Entity,
    &'static EditorField,
    &'static EditableText,
    &'static mut TextReadWriteMode,
    Has<InteractionDisabled>,
    &'static mut BackgroundColor,
    &'static mut Outline,
    &'static mut AccessibilityNode,
);
type ActionVisual = (
    Entity,
    &'static EditorAction,
    &'static Hovered,
    Has<Pressed>,
    Has<InteractionDisabled>,
    &'static mut BackgroundColor,
    &'static mut Outline,
    &'static mut AccessibilityNode,
);

#[derive(SystemParam)]
struct EditorViewQueries<'w, 's> {
    inputs: Query<'w, 's, InputVisual, (With<TextInput>, Without<EditorAction>)>,
    actions: Query<'w, 's, ActionVisual, (With<EditorAction>, Without<TextInput>)>,
    readouts: Query<'w, 's, (&'static EditorReadout, &'static mut Text)>,
    disclosures: Query<
        'w,
        's,
        (
            Entity,
            &'static EditorDisclosure,
            &'static mut Node,
            &'static mut Visibility,
        ),
    >,
    parents: Query<'w, 's, &'static ChildOf>,
}

fn update_views(
    mut commands: Commands,
    mut input_focus: ResMut<InputFocus>,
    hud: Res<HudState>,
    playback: Res<PlaybackState>,
    state: Res<InstrumentEditors>,
    queries: EditorViewQueries,
) {
    let EditorViewQueries {
        mut inputs,
        mut actions,
        mut readouts,
        mut disclosures,
        parents,
    } = queries;
    let enabled = available(&hud, &playback);
    for (entity, field, text, mut mode, disabled, mut background, mut outline, mut accessible) in
        &mut inputs
    {
        let enabled =
            enabled && scope_in_panel(&hud, field.scope()) && field_available(&state, *field);
        update_disabled(&mut commands, entity, !enabled, disabled);
        *mode = if enabled {
            TextReadWriteMode::Editable
        } else {
            TextReadWriteMode::ReadOnly
        };
        let focused = input_focus.get() == Some(entity);
        background.0 = if !enabled {
            DISABLED
        } else if focused {
            FOCUSED
        } else {
            BUTTON
        };
        outline.color = if focused { TEXT } else { Color::NONE };
        accessible.set_value(text.value().to_string());
        let notice = state
            .field_notices
            .iter()
            .find(|(key, _)| key == field)
            .map(|(_, notice)| notice.text());
        accessible.set_description(notice.unwrap_or_else(|| field.hint().into()));
    }
    for (entity, action, hovered, pressed, disabled, mut background, mut outline, mut accessible) in
        &mut actions
    {
        let active = action_available(&hud, &playback, *action)
            && match action {
                EditorAction::Commit(field) | EditorAction::Nudge(field, _) => {
                    field_available(&state, *field)
                }
                EditorAction::FlowAxis(_) | EditorAction::ReverseFlow => state
                    .confirmed
                    .as_ref()
                    .is_some_and(ConfirmedInputs::stream_active),
                EditorAction::ResetImpact => state.impact().is_some(),
                EditorAction::Preset(_)
                | EditorAction::Distribution(_)
                | EditorAction::ToggleAdvanced
                | EditorAction::AskCreateWorld
                | EditorAction::CancelCreateWorld
                | EditorAction::PlaceLander
                | EditorAction::ReloadPlacement => true,
                EditorAction::CreateWorld => state.create_confirmation == Disclosure::Expanded,
            };
        let selected = match action {
            EditorAction::FlowAxis(axis) => state
                .confirmed
                .as_ref()
                .is_some_and(|current| current.flow.stream_axis == *axis),
            EditorAction::Preset(preset) => {
                state.world.is_some_and(|world| world.preset == *preset)
            }
            EditorAction::Distribution(choice) => state.world.is_some_and(|world| match choice {
                DistributionChoice::Stream => {
                    world.settings.debris.distribution.is_directed_stream()
                }
                DistributionChoice::Impact => {
                    !world.settings.debris.distribution.is_directed_stream()
                }
            }),
            EditorAction::Commit(_)
            | EditorAction::Nudge(_, _)
            | EditorAction::ReverseFlow
            | EditorAction::ToggleAdvanced
            | EditorAction::ResetImpact
            | EditorAction::AskCreateWorld
            | EditorAction::CancelCreateWorld
            | EditorAction::CreateWorld
            | EditorAction::PlaceLander
            | EditorAction::ReloadPlacement => false,
        };
        update_disabled(&mut commands, entity, !active, disabled);
        let focused = input_focus.get() == Some(entity);
        background.0 = if !active {
            DISABLED
        } else if focused {
            FOCUSED
        } else if selected || pressed {
            SELECTED
        } else if hovered.get() {
            HOVERED
        } else {
            BUTTON
        };
        outline.color = if focused { TEXT } else { Color::NONE };
        accessible.set_selected(selected);
        if matches!(action, EditorAction::ToggleAdvanced) {
            accessible.set_expanded(state.advanced == Disclosure::Expanded);
        }
        if matches!(action, EditorAction::AskCreateWorld) {
            accessible.set_expanded(state.create_confirmation == Disclosure::Expanded);
        }
    }
    for (readout, mut text) in &mut readouts {
        let value = match readout {
            EditorReadout::Field(field) => state.field_notices.iter().find(|(key, _)| key == field).map(|(_, notice)| notice.text()).unwrap_or_default(),
            EditorReadout::Scope(scope) => state.scope_notices.iter().find(|(key, _)| key == scope).map(|(_, notice)| notice.text()).unwrap_or_default(),
            EditorReadout::FlowSummary => state.confirmed.as_ref().map(|current| {
                if !current.stream_active() {
                    "Inactive for impact ejecta. Edit the new-world ejection draft in Launch; the stream reference is not measured asteroid velocity.".into()
                } else {
                    format!("Current stream reference: {}{}. Speed changes live body velocities; spread affects new births only.",
                        if current.flow.stream_sign < 0 { "-" } else { "+" },
                        match current.flow.stream_axis { crate::model::StreamAxis::X => "X", crate::model::StreamAxis::Y => "Y", crate::model::StreamAxis::Z => "Z" })
                }
            }).unwrap_or_else(|| "Waiting for current stream inputs.".into()),
            EditorReadout::WorldSummary => world_summary(&state),
            EditorReadout::ImpactSummary => state.impact().map(|settings| format!(
                "Draft ejection source XYZ: {:.3}, {:.3}, {:.3}.\nInitial kick XYZ: {:.3}, {:.3}, {:.3} m/s.\nTangential speed: {:.3} m/s; patch half-angle: {:.2} deg. Expand Size, shell and ejection inputs to edit these defaults.",
                settings.source_direction[0], settings.source_direction[1], settings.source_direction[2],
                settings.kick_velocity_mps[0], settings.kick_velocity_mps[1], settings.kick_velocity_mps[2],
                settings.tangential_speed_mps, settings.patch_half_angle_deg,
            )).unwrap_or_default(),
            EditorReadout::PlacementSummary => position(&crate::snapshot()).map(|[x, y, z]| format!("Current measured position: {x:.2}, {y:.2}, {z:.2} m. +X points toward Mars.")).unwrap_or_else(|| "Current lander position unavailable. Reload before placing.".into()),
        };
        let value = crate::hud::readable_text(&value);
        if text.0 != value {
            text.0 = value;
        }
    }
    let mut restore_creation_focus = false;
    for (entity, disclosure, mut node, mut visibility) in &mut disclosures {
        let shown = match disclosure {
            EditorDisclosure::Advanced => state.advanced == Disclosure::Expanded,
            EditorDisclosure::Impact => state.impact().is_some(),
            EditorDisclosure::CreateConfirmation => {
                state.create_confirmation == Disclosure::Expanded
            }
        };
        if matches!(disclosure, EditorDisclosure::CreateConfirmation)
            && !shown
            && node.display != Display::None
        {
            restore_creation_focus = input_focus.get().is_some_and(|focused| {
                focused == entity
                    || parents
                        .iter_ancestors(focused)
                        .any(|parent| parent == entity)
            });
        }
        node.display = if shown { Display::Flex } else { Display::None };
        *visibility = if shown {
            Visibility::Inherited
        } else {
            Visibility::Hidden
        };
    }
    if restore_creation_focus
        && let Some((entity, ..)) = actions
            .iter()
            .find(|(_, action, ..)| matches!(action, EditorAction::AskCreateWorld))
    {
        input_focus.set(entity, bevy::input_focus::FocusCause::Navigated);
    }
}

fn update_disabled(commands: &mut Commands, entity: Entity, disabled: bool, was_disabled: bool) {
    if disabled != was_disabled {
        if disabled {
            commands.entity(entity).insert(InteractionDisabled);
        } else {
            commands.entity(entity).remove::<InteractionDisabled>();
        }
    }
}

fn world_summary(state: &InstrumentEditors) -> String {
    if state.confirmed.is_none() {
        return "Waiting for current-world inputs.".into();
    }
    let Some(draft) = state.world else {
        return "World draft unavailable.".into();
    };
    format!(
        "{}\nNew-world draft template: {}; {}; {} asteroids.\nCreate paused world replaces this run at time zero and clears thrust. {}",
        crate::hud::systems::launch_site_readout(&crate::snapshot()),
        preset_label(draft.preset),
        match draft.settings.debris.distribution {
            DebrisDistribution::DirectedStream => "directed stream",
            DebrisDistribution::ImpactEjecta { .. } => "impact ejecta",
        },
        draft.settings.debris.count,
        if draft.settings.debris.count >= 10_000 {
            "Heavy world; generation and playback may be slow."
        } else {
            "Opening this draft changes nothing."
        },
    )
}

fn preset_label(preset: crate::model::FlightPreset) -> &'static str {
    match preset {
        crate::model::FlightPreset::MarsFacingL1 => "Mars-facing L1",
        crate::model::FlightPreset::FarSideL2 => "far-side L2",
    }
}
