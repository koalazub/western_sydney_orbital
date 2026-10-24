use bevy::{
    a11y::AccessibilityNode,
    input::{ButtonState, InputSystems, keyboard::KeyboardInput},
    input_focus::{
        FocusCause, FocusedInput, InputFocus, InputFocusSystems,
        tab_navigation::{TabGroup, TabNavigationPlugin},
    },
    picking::hover::Hovered,
    prelude::*,
    ui::{InteractionDisabled, Pressed, UiSystems},
    ui_widgets::{Activate, Button, ScrollIntoView},
};

use super::{input, model::*, policy::action_enabled, view};
use crate::{
    EngineSet, PlaybackState, camera, controls,
    domain::{CameraRequest, EngineAction, EntityId, EntityKind, LagrangeRegion, ViewportAction},
    model::Snapshot,
};
use bevy::window::PrimaryWindow;

pub(super) fn install(app: &mut App) {
    if !app.is_plugin_added::<TabNavigationPlugin>() {
        app.add_plugins(TabNavigationPlugin);
    }
    app.init_resource::<HudState>()
        .add_systems(Startup, view::setup.after(crate::scene::setup))
        .add_systems(
            PreUpdate,
            capture_binding_input
                .after(InputSystems)
                .before(InputFocusSystems::Dispatch),
        )
        .add_systems(Update, interactions.in_set(EngineSet::Input))
        .add_systems(
            Update,
            (
                update_layout,
                update_overlays,
                update_buttons,
                update_readouts,
            )
                .chain()
                .in_set(EngineSet::Hud),
        )
        .add_systems(
            PostUpdate,
            scroll_focused_control_into_view.after(UiSystems::PostLayout),
        );
    input::install(app);
    super::diagnostics::install(app);
}

pub(super) fn activate_button(
    activate: On<Activate>,
    actions: Query<&HudAction>,
    mut hud: ResMut<HudState>,
    playback: Res<PlaybackState>,
) {
    if !camera::input_active() {
        return;
    }
    let Ok(action) = actions.get(activate.entity) else {
        return;
    };
    let mode = interaction_mode(&hud);
    activate_action(*action, mode, &mut hud, &playback);
}

/// Read the physical event before native widgets see Enter or Space. Capture
/// keeps an action guard through the successful key and the final key-up frame.
pub(super) fn capture_binding_input(
    mut events: MessageReader<KeyboardInput>,
    keys: Res<ButtonInput<KeyCode>>,
    hud: Res<HudState>,
) {
    controls::begin_input_frame();
    if controls::binding_capture_active()
        && (!camera::input_active() || hud.reset_confirm_open || hud.panel != SidePanel::Controls)
    {
        controls::cancel_capture();
    }
    for input in events.read() {
        if controls::binding_capture_active()
            && input.state == ButtonState::Pressed
            && !input.repeat
        {
            let modified = [
                KeyCode::AltLeft,
                KeyCode::AltRight,
                KeyCode::ShiftLeft,
                KeyCode::ShiftRight,
                KeyCode::ControlLeft,
                KeyCode::ControlRight,
                KeyCode::SuperLeft,
                KeyCode::SuperRight,
            ]
            .into_iter()
            .any(|key| keys.pressed(key));
            controls::capture_key(input.key_code, modified);
        }
    }
    controls::observe_input_release(&keys);
}

pub(super) fn interactions(
    keys: Res<ButtonInput<KeyCode>>,
    input_focus: Res<InputFocus>,
    buttons: Query<(), With<Button>>,
    mut hud: ResMut<HudState>,
    playback: Res<PlaybackState>,
) {
    let mode = interaction_mode(&hud);
    if mode == InteractionMode::Rebinding {
        return;
    }
    let button_focused = input_focus
        .get()
        .is_some_and(|entity| buttons.contains(entity));
    if keys.just_pressed(KeyCode::Escape) {
        if mode == InteractionMode::ResetConfirmation {
            hud.reset_confirm_open = false;
        } else {
            hud.panel = hud.panel.close_top();
        }
        return;
    }
    if !camera::input_active() {
        return;
    }
    if mode == InteractionMode::ResetConfirmation {
        if !button_focused && plain_key_pressed(&keys, KeyCode::Enter) {
            activate_action(HudAction::ConfirmReset, mode, &mut hud, &playback);
        }
        return;
    }
    // P and H remain application shortcuts when a native button retains focus.
    // Space belongs to lift and is deliberately absent from this table.
    for (key, action) in [
        (KeyCode::KeyP, HudAction::TogglePlayback),
        (KeyCode::KeyH, HudAction::ToggleControls),
        (KeyCode::Period, HudAction::Step),
        (KeyCode::KeyT, HudAction::CycleRate),
        (KeyCode::KeyR, HudAction::AskReset),
        (KeyCode::Comma, HudAction::FocusPrevious),
        (KeyCode::Slash, HudAction::FocusNext),
        (KeyCode::KeyM, HudAction::Maximize),
        (KeyCode::KeyX, HudAction::Fullscreen),
        (KeyCode::KeyL, HudAction::ToggleCapture),
        (KeyCode::KeyI, HudAction::ToggleInspector),
    ] {
        if (!button_focused || matches!(key, KeyCode::KeyP | KeyCode::KeyH))
            && plain_key_pressed(&keys, key)
        {
            activate_action(action, mode, &mut hud, &playback);
            // Opening the modal invalidates the Ready snapshot. No remaining
            // shortcut in this frame may act through the newly opened modal.
            if hud.reset_confirm_open {
                return;
            }
        }
    }
}

fn plain_key_pressed(keys: &ButtonInput<KeyCode>, key: KeyCode) -> bool {
    keys.just_pressed(key)
        && ![
            KeyCode::ControlLeft,
            KeyCode::ControlRight,
            KeyCode::SuperLeft,
            KeyCode::SuperRight,
            KeyCode::AltLeft,
            KeyCode::AltRight,
        ]
        .into_iter()
        .any(|key| keys.pressed(key))
}

fn interaction_mode(hud: &HudState) -> InteractionMode {
    if controls::binding_actions_blocked() {
        InteractionMode::Rebinding
    } else if hud.reset_confirm_open {
        InteractionMode::ResetConfirmation
    } else {
        InteractionMode::Ready
    }
}

fn activate_action(
    action: HudAction,
    mode: InteractionMode,
    hud: &mut HudState,
    playback: &PlaybackState,
) {
    if !action_enabled(
        action,
        mode,
        playback,
        crate::snapshot().mission.status.is_terminal(),
    ) {
        return;
    }
    match action {
        HudAction::TogglePlayback => crate::request_action(EngineAction::TogglePlayback),
        HudAction::Step => crate::request_action(EngineAction::Step),
        HudAction::AskReset => hud.reset_confirm_open = true,
        HudAction::ConfirmReset => {
            crate::request_action(EngineAction::Reset);
            hud.reset_confirm_open = false;
        }
        HudAction::CancelReset => hud.reset_confirm_open = false,
        HudAction::DebugUpright => crate::request_action(EngineAction::DebugUpright),
        HudAction::Export => crate::request_action(EngineAction::Viewport(ViewportAction::Export)),
        HudAction::CycleRate => crate::request_action(EngineAction::CycleRate),
        HudAction::Fullscreen => {
            // Web fullscreen must be requested by the trusted DOM event adapter,
            // using the actual native hit/focus target before Bevy's next frame.
            #[cfg(not(target_arch = "wasm32"))]
            crate::request_action(EngineAction::Viewport(ViewportAction::Fullscreen));
        }
        HudAction::Notebook => {
            // Like fullscreen, browser window.open belongs to trusted DOM input.
            #[cfg(not(target_arch = "wasm32"))]
            crate::request_action(EngineAction::Viewport(ViewportAction::Notebook));
        }
        HudAction::ChangeBinding(action) => controls::begin_capture(action),
        HudAction::RestoreBindings => controls::restore_defaults(),
        HudAction::Maximize => {
            crate::request_action(EngineAction::Viewport(ViewportAction::Maximize))
        }
        HudAction::ToggleCapture => {
            let action = if camera::status().locked {
                ViewportAction::Release
            } else {
                ViewportAction::Capture
            };
            crate::request_action(EngineAction::Viewport(action));
        }
        HudAction::ToggleDetails => {
            hud.panel = hud.panel.toggle(SidePanel::Details);
        }
        HudAction::ToggleControls => {
            hud.panel = hud.panel.toggle(SidePanel::Controls);
        }
        HudAction::FollowLander => {
            let _ = camera::request_view(CameraRequest {
                mode: Some(crate::domain::CameraMode::Orbit),
                focus: Some(EntityId::Lander),
                zoom: Some(1.0),
            });
        }
        HudAction::ToggleInspector => {
            hud.panel = hud.panel.toggle(SidePanel::Inspector);
        }
        HudAction::FocusNext => {
            cycle_focus(&crate::snapshot(), true);
        }
        HudAction::FocusPrevious => {
            cycle_focus(&crate::snapshot(), false);
        }
        HudAction::ChangeCamera(mode) => {
            let _ = camera::request_view(CameraRequest {
                mode: Some(mode),
                focus: None,
                zoom: None,
            });
        }
    }
}

pub(super) fn update_layout(
    windows: Query<&Window, With<PrimaryWindow>>,
    ui_scale: Res<UiScale>,
    mut input_focus: ResMut<InputFocus>,
    mut previous_sensor: Local<bool>,
    mut layout: Query<(&mut Node, &mut Visibility, &ComputedNode, &LayoutPart), Without<Overlay>>,
    mut details: Query<&mut Node, (With<DetailsPanel>, Without<LayoutPart>)>,
) {
    let sensor = camera::status().mode.sensor().is_some();
    if *previous_sensor != sensor {
        // The clicked view button may now be hidden with its toolbar. Keep
        // keyboard navigation on the canvas rather than on an invisible button.
        input_focus.clear();
        *previous_sensor = sensor;
    }
    let scale = if ui_scale.0.is_finite() && ui_scale.0 > 0.0 {
        ui_scale.0
    } else {
        1.0
    };
    let (width, height) = windows
        .single()
        .map(|window| (window.width() / scale, window.height() / scale))
        .unwrap_or((800.0, 800.0));
    let mut top = SCREEN_PADDING_PX + PANEL_GAP_PX;
    let mut bottom = SCREEN_PADDING_PX + PANEL_GAP_PX;
    for (mut node, mut visibility, computed, part) in &mut layout {
        let height = computed.size().y * computed.inverse_scale_factor();
        match part {
            LayoutPart::TopRow => top += height,
            LayoutPart::BottomRow => bottom += height,
            LayoutPart::CameraPanel | LayoutPart::SensorActions => {}
        }
        let shown = match part {
            LayoutPart::CameraPanel => !sensor,
            LayoutPart::SensorActions => sensor,
            LayoutPart::TopRow | LayoutPart::BottomRow => true,
        };
        let display = if shown { Display::Flex } else { Display::None };
        let next_visibility = if shown {
            Visibility::Inherited
        } else {
            Visibility::Hidden
        };
        if node.display != display {
            node.display = display;
        }
        if *visibility != next_visibility {
            *visibility = next_visibility;
        }
    }
    for mut node in &mut details {
        // Fit disclosed content between the real top and bottom strips. Keep
        // overflow scrollable instead of pushing controls off a narrow canvas.
        let panel_width = px((width - 2.0 * SCREEN_PADDING_PX).clamp(0.0, 440.0));
        let panel_height = px((height - top - bottom).max(0.0));
        if node.width != panel_width {
            node.width = panel_width;
        }
        if node.top != px(top) {
            node.top = px(top);
        }
        if node.max_height != panel_height {
            node.max_height = panel_height;
        }
    }
}

type OverlayVisual = (
    Entity,
    &'static mut Node,
    &'static mut Visibility,
    &'static Overlay,
    Has<TabGroup>,
    &'static mut ScrollPosition,
);

pub(super) fn update_overlays(
    mut commands: Commands,
    hud: Res<HudState>,
    mut input_focus: ResMut<InputFocus>,
    buttons: Query<(Entity, &HudAction), With<Button>>,
    parents: Query<&ChildOf>,
    mut modal: Local<ModalFocus>,
    mut overlays: Query<OverlayVisual, Without<LayoutPart>>,
) {
    if hud.reset_confirm_open != modal.open {
        if hud.reset_confirm_open {
            modal.previous = input_focus.get();
            if let Some((entity, _)) = buttons
                .iter()
                .find(|(_, action)| matches!(action, HudAction::ConfirmReset))
            {
                input_focus.set(entity, FocusCause::Navigated);
            }
        } else if let Some(previous) = modal.previous.take() {
            input_focus.set(previous, FocusCause::Navigated);
        } else {
            input_focus.clear();
        }
        modal.open = hud.reset_confirm_open;
    }
    let mut hidden_focus = false;
    for (entity, mut node, mut visibility, overlay, modal_group, mut position) in &mut overlays {
        let was_shown = node.display != Display::None;
        let shown = match overlay {
            Overlay::Details => hud.panel.details_open(),
            Overlay::Controls => hud.panel == SidePanel::Controls,
            Overlay::Reset => hud.reset_confirm_open,
            Overlay::Inspector => hud.panel == SidePanel::Inspector,
        };
        node.display = if shown { Display::Flex } else { Display::None };
        if matches!(overlay, Overlay::Details | Overlay::Controls)
            && !was_shown
            && node.display != Display::None
        {
            position.y = 0.0;
        }
        *visibility = if node.display == Display::None {
            Visibility::Hidden
        } else {
            Visibility::Inherited
        };
        if node.display == Display::None
            && input_focus.get().is_some_and(|focused| {
                focused == entity
                    || parents
                        .iter_ancestors(focused)
                        .any(|parent| parent == entity)
            })
        {
            hidden_focus = true;
        }
        let needs_modal_group = matches!(overlay, Overlay::Reset) && hud.reset_confirm_open;
        if needs_modal_group != modal_group {
            if needs_modal_group {
                commands.entity(entity).insert(TabGroup::modal());
            } else {
                commands.entity(entity).remove::<TabGroup>();
            }
        }
    }
    if hidden_focus && !hud.reset_confirm_open {
        if let Some((entity, _)) = buttons
            .iter()
            .find(|(_, action)| matches!(action, HudAction::ToggleDetails))
        {
            input_focus.set(entity, FocusCause::Navigated);
        } else {
            input_focus.clear();
        }
    }
}

type ButtonVisual = (
    Entity,
    &'static HudAction,
    &'static Hovered,
    Has<Pressed>,
    Has<InteractionDisabled>,
    &'static mut BackgroundColor,
    &'static mut AccessibilityNode,
    &'static mut Outline,
);

pub(super) fn update_buttons(
    mut commands: Commands,
    input_focus: Res<InputFocus>,
    playback: Res<PlaybackState>,
    hud: Res<HudState>,
    mut button_views: Query<ButtonVisual, With<Button>>,
) {
    let snapshot = crate::snapshot();
    let camera_status = camera::status();
    let mode = interaction_mode(&hud);
    for (entity, action, hovered, pressed, disabled, mut background, mut accessible, mut outline) in
        &mut button_views
    {
        let should_disable = !action_enabled(
            *action,
            mode,
            &playback,
            snapshot.mission.status.is_terminal(),
        );
        if should_disable != disabled {
            if should_disable {
                commands.entity(entity).insert(InteractionDisabled);
            } else {
                commands.entity(entity).remove::<InteractionDisabled>();
            }
        }
        let (selected, expanded) = match *action {
            HudAction::ChangeCamera(mode) => (Some(mode == camera_status.mode), None),
            HudAction::ToggleDetails => (None, Some(hud.panel.details_open())),
            HudAction::ToggleControls => (None, Some(hud.panel == SidePanel::Controls)),
            HudAction::ToggleInspector => (None, Some(hud.panel == SidePanel::Inspector)),
            HudAction::TogglePlayback
            | HudAction::Step
            | HudAction::AskReset
            | HudAction::ConfirmReset
            | HudAction::CancelReset
            | HudAction::DebugUpright
            | HudAction::Export
            | HudAction::CycleRate
            | HudAction::Fullscreen
            | HudAction::Notebook
            | HudAction::ChangeBinding(_)
            | HudAction::RestoreBindings
            | HudAction::Maximize
            | HudAction::ToggleCapture
            | HudAction::FollowLander
            | HudAction::FocusNext
            | HudAction::FocusPrevious => (None, None),
        };
        if let Some(expanded) = expanded
            && accessible.is_expanded() != Some(expanded)
        {
            accessible.set_expanded(expanded);
        }
        if let Some(selected) = selected
            && accessible.is_selected() != Some(selected)
        {
            accessible.set_selected(selected);
        }
        let keyboard_focused = input_focus.get() == Some(entity);
        let color = if should_disable {
            DISABLED
        } else if keyboard_focused {
            FOCUSED
        } else if pressed || selected == Some(true) {
            SELECTED
        } else if hovered.get() {
            HOVERED
        } else {
            BUTTON
        };
        if background.0 != color {
            background.0 = color;
        }
        let focus_color = if keyboard_focused { TEXT } else { Color::NONE };
        if outline.color != focus_color {
            outline.color = focus_color;
        }
    }
}

pub(super) fn update_readouts(
    playback: Res<PlaybackState>,
    keys: Res<ButtonInput<KeyCode>>,
    hud: Res<HudState>,
    mut reads: Query<(&mut Text, &Readout, &ChildOf)>,
    mut buttons: Query<&mut AccessibilityNode, With<Button>>,
) {
    let snapshot = crate::snapshot();
    let camera_status = camera::status();
    let focus = focused_item(&snapshot, camera_status.focus);
    for (mut text, readout, parent) in &mut reads {
        let value = match readout {
            Readout::Summary => summary_readout(&snapshot, &playback),
            Readout::PathBrief => crate::navigation::brief(),
            Readout::PathDetails => crate::navigation::details(),
            Readout::FlightControls => flight_controls_readout(&playback),
            Readout::TelemetryBrief => crate::telemetry::brief(),
            Readout::TelemetryDetails => crate::telemetry::details(),
            Readout::ReferenceThrust => reference_thrust_brief(),
            Readout::InputSettings => input_settings_readout(&playback, &keys),
            Readout::BindingStatus => controls::capture_message(),
            Readout::Binding(action) => format!(
                "{}: {} · change",
                action.label(),
                controls::key_label(*action)
            ),
            Readout::FlightHint => format!(
                "{}/{} fire front/back | {}/{} fire left/right | {} lift | Alt/Option debug",
                controls::key_label(controls::Action::Front),
                controls::key_label(controls::Action::Back),
                controls::key_label(controls::Action::Left),
                controls::key_label(controls::Action::Right),
                controls::key_label(controls::Action::Lift),
            ),
            Readout::Notebook => "Open notebook · new tab".into(),
            Readout::Mission => mission_readout(&snapshot),
            Readout::Environment => environment_readout(&snapshot),
            Readout::CameraMode => format!("{} | {}", camera_status.mode, focus_label(focus)),
            Readout::Focus => format!("Focus: {}", focus_label(focus)),
            Readout::Camera => camera_readout(camera_status),
            Readout::Inspector => inspector_readout(&snapshot, focus),
            Readout::Authority => {
                let restriction = authority_readout(
                    playback.can_control,
                    playback.running,
                    snapshot.mission.status.is_terminal(),
                );
                if restriction.is_empty() {
                    playback.message.clone()
                } else {
                    restriction
                }
            }
            Readout::Playback => {
                if snapshot.mission.status.is_terminal() {
                    snapshot.mission.status.to_string()
                } else if playback.running {
                    "Pause [P]".into()
                } else {
                    "Run [P]".into()
                }
            }
            Readout::Rate => format!("{}x [T]", playback.rate.factor() as u32),
            Readout::Fullscreen => {
                if playback.fullscreen {
                    "Exit fullscreen [X]".into()
                } else {
                    "Fullscreen [X]".into()
                }
            }
            Readout::Maximize => {
                if playback.maximized {
                    "Windowed [M]".into()
                } else {
                    "Maximize [M]".into()
                }
            }
            Readout::Capture => {
                if camera_status.locked {
                    "Release [L]".into()
                } else {
                    "Capture [L]".into()
                }
            }
            Readout::DetailsToggle => if hud.panel.details_open() {
                "Details -"
            } else {
                "Details +"
            }
            .into(),
            Readout::ControlsToggle => if hud.panel == SidePanel::Controls {
                "Close controls [H]"
            } else {
                "Controls [H]"
            }
            .into(),
            Readout::InspectorToggle => if hud.panel == SidePanel::Inspector {
                "Close inspector [I]"
            } else {
                "Inspect [I]"
            }
            .into(),
            Readout::Static => continue,
        };
        if text.0 != value {
            if let Ok(mut accessible) = buttons.get_mut(parent.parent()) {
                accessible.set_label(value.clone());
            }
            text.0 = value;
        }
    }
}

pub(super) fn scroll_details(
    mut event: On<FocusedInput<KeyboardInput>>,
    mut panels: Query<(&Node, &ComputedNode, &mut ScrollPosition), With<DetailsPanel>>,
) {
    if controls::binding_actions_blocked() {
        event.propagate(false);
        return;
    }
    if event.input.state != ButtonState::Pressed {
        return;
    }
    let Some((_, computed, mut position)) = panels
        .iter_mut()
        .find(|(node, computed, _)| node.display != Display::None && !computed.is_empty())
    else {
        return;
    };
    let visible = computed.size().y * computed.inverse_scale_factor();
    let content = computed.content_size().y * computed.inverse_scale_factor();
    let max_scroll = (content - visible).max(0.0);
    let next = match event.input.key_code {
        KeyCode::ArrowUp => position.y - BUTTON_HEIGHT_PX,
        KeyCode::ArrowDown => position.y + BUTTON_HEIGHT_PX,
        KeyCode::PageUp => position.y - visible * 0.8,
        KeyCode::PageDown => position.y + visible * 0.8,
        KeyCode::Home => 0.0,
        KeyCode::End => max_scroll,
        _ => return,
    };
    event.propagate(false);
    position.y = next.clamp(0.0, max_scroll);
}

pub(super) fn scroll_focused_control_into_view(
    mut commands: Commands,
    input_focus: Res<InputFocus>,
    buttons: Query<(), With<Button>>,
    mut previous: Local<Option<Entity>>,
) {
    let focused = input_focus.get();
    if *previous != focused
        && let Some(entity) = focused
        && buttons.contains(entity)
    {
        commands.trigger(ScrollIntoView { entity });
    }
    *previous = focused;
}

fn mission_readout(snapshot: &Snapshot) -> String {
    let fuel = format_number(snapshot.mission.fuel_used_kg);
    let terminal_score = if snapshot.mission.status.is_terminal() {
        snapshot
            .mission
            .score
            .map(|score| format!(" | est. score {:.0}", score))
            .unwrap_or_default()
    } else {
        String::new()
    };
    format!(
        "T+{} | {} ({})\nReturn ship {} | range {} | speed {}\nFuel used ~{} kg | damage estimate ~{} J\n{} debris | {} contacts | {} scene entities{}\nLocal test resets: {}",
        duration(snapshot.time_s),
        snapshot.mission.phase,
        snapshot.mission.status,
        region_label(snapshot.mission.target_region),
        meters(snapshot.mission.target_distance_m),
        speed(snapshot.mission.relative_speed_mps),
        fuel,
        format_number(snapshot.mission.damage),
        snapshot
            .assets
            .iter()
            .filter(|entity| entity.kind == EntityKind::Debris)
            .count(),
        snapshot.active_contacts.len(),
        snapshot.assets.len(),
        terminal_score,
        snapshot.manual_interventions,
    )
}

fn summary_readout(snapshot: &Snapshot, playback: &PlaybackState) -> String {
    let state = if snapshot.mission.status.is_terminal() {
        snapshot.mission.status.to_string()
    } else if !playback.can_control {
        "view only".into()
    } else if playback.running {
        "running".into()
    } else {
        "paused".into()
    };
    format!(
        "Phobos practice | T+{} | {state}",
        duration(snapshot.time_s)
    )
}

fn environment_readout(snapshot: &Snapshot) -> String {
    format!(
        "L1 {:.1} km | L2 {:.1} km",
        snapshot.environment.l1_position_m[0] / 1000.0,
        snapshot.environment.l2_position_m[0] / 1000.0,
    )
}

fn flight_controls_readout(playback: &PlaybackState) -> String {
    if controls::binding_capture_active() {
        return "Changing a key. Manual flight is blocked; press one key or Esc to cancel.".into();
    }
    let state = if !playback.can_control {
        "Ship controls unavailable in view-only mode."
    } else if !playback.running {
        return format!(
            "Lander paused. {} launches; P or Run resumes playback.",
            controls::key_label(controls::Action::Lift)
        );
    } else if !camera::input_active() {
        "Click the scene or focus the canvas to enable movement keys."
    } else if crate::session::keyboard_requires_release() {
        "Release the movement keys, then press them again to rearm ship thrust."
    } else {
        "Ship keys apply manual nozzle forces. Release thrust keys to coast."
    };
    state.into()
}

fn reference_thrust_readout() -> String {
    format!(
        "{} and {}: reference limit {:.2} m/s²\nLocal nozzle physics, not an STP-equivalent allocator.",
        controls::key_label(controls::Action::Lift),
        controls::key_label(controls::Action::Up),
        crate::scenario::settings().lander.max_accel_mps2
    )
}

pub(super) fn reference_thrust_brief() -> String {
    format!(
        "{} reference {:.2} m/s² | local model",
        controls::key_label(controls::Action::Lift),
        crate::scenario::settings().lander.max_accel_mps2
    )
}

fn input_settings_readout(playback: &PlaybackState, keys: &ButtonInput<KeyCode>) -> String {
    let config = crate::scenario::settings();
    let input = if camera::input_active() {
        "active"
    } else {
        "inactive; click/focus the scene"
    };
    format!(
        "Input {input}\nHeld keys: {}\nController owner: external Python notebook or manual keys\nLocal simulation {}\nReference command caps: {:.2} m/s² and {:.2} rad/s²\n{}",
        crate::controls::held_key_labels(keys),
        if playback.running {
            "running"
        } else {
            "paused"
        },
        config.lander.max_accel_mps2,
        config.lander.max_angular_accel_rad_s2,
        reference_thrust_readout()
    )
}

fn camera_readout(camera: crate::camera::CameraStatus) -> String {
    format!(
        "{} | range {:.1} km | camera {:.1}, {:.1}, {:.1} km | controls {}",
        camera.mode,
        camera.distance_m / 1000.0,
        camera.position_m[0] / 1000.0,
        camera.position_m[1] / 1000.0,
        camera.position_m[2] / 1000.0,
        if camera.active {
            "active"
        } else {
            "click scene"
        },
    )
}

fn authority_readout(can_control: bool, running: bool, terminal: bool) -> String {
    if !can_control {
        "View-only access | mission controls disabled".into()
    } else if running {
        "Pause the mission before stepping".into()
    } else if terminal {
        "Mission ended | reset to start another run".into()
    } else {
        String::new()
    }
}

fn cycle_focus(snapshot: &Snapshot, forward: bool) {
    let choices = focus_choices(snapshot);
    let current = choices
        .iter()
        .position(|id| *id == camera::status().focus)
        .unwrap_or(0);
    let next = if forward {
        (current + 1) % choices.len()
    } else {
        (current + choices.len() - 1) % choices.len()
    };
    let _ = camera::request_view(CameraRequest {
        mode: camera::status()
            .mode
            .sensor()
            .is_some()
            .then_some(crate::domain::CameraMode::Orbit),
        focus: Some(choices[next]),
        ..default()
    });
}

fn focused_item(snapshot: &Snapshot, id: EntityId) -> Option<FocusItem<'_>> {
    let label = match id {
        EntityId::Phobos => "Phobos",
        EntityId::Mars => "Mars",
        EntityId::L1 => "L1",
        EntityId::L2 => "L2",
        EntityId::Lander | EntityId::ReturnShip | EntityId::LandingPad | EntityId::Debris(_) => {
            let entity = snapshot.assets.iter().find(|entity| entity.id == id)?;
            return Some(FocusItem {
                id,
                label: &entity.name,
                entity: Some(entity),
            });
        }
    };
    Some(FocusItem {
        id,
        label,
        entity: None,
    })
}

fn focus_choices(snapshot: &Snapshot) -> Vec<EntityId> {
    let lander = snapshot
        .assets
        .iter()
        .find(|entity| entity.id == EntityId::Lander);
    let nearest_debris = lander.and_then(|lander| {
        snapshot
            .assets
            .iter()
            .filter(|entity| entity.kind == EntityKind::Debris)
            .min_by(|a, b| {
                squared_distance(a.position, lander.position)
                    .total_cmp(&squared_distance(b.position, lander.position))
            })
    });
    let nearest = nearest_debris.map(|entity| entity.id);
    [
        Some(EntityId::Lander),
        Some(EntityId::ReturnShip),
        Some(EntityId::Phobos),
        nearest,
        Some(EntityId::LandingPad),
        Some(EntityId::Mars),
        Some(EntityId::L1),
        Some(EntityId::L2),
    ]
    .into_iter()
    .flatten()
    .chain(
        snapshot
            .assets
            .iter()
            .filter(|entity| entity.kind == EntityKind::Debris && Some(entity.id) != nearest)
            .map(|entity| entity.id),
    )
    .collect()
}

fn focus_label(focus: Option<FocusItem<'_>>) -> &str {
    focus.map(|item| item.label).unwrap_or("none")
}

fn inspector_readout(snapshot: &Snapshot, focus: Option<FocusItem<'_>>) -> String {
    let Some(focus) = focus else {
        return "No focused object".into();
    };
    if let Some(entity) = focus.entity {
        return format!(
            "{} | {} | {}\nPosition {:.1}, {:.1}, {:.1} m\nVelocity {:.3}, {:.3}, {:.3} m/s\nQuaternion {:.4}, {:.4}, {:.4}, {:.4}\nAngular velocity {:.4}, {:.4}, {:.4} rad/s\nMass {:.1} kg | radius {:.2} m | fuel {:.1} kg\nApplied command {:.4}, {:.4}, {:.4} m/s^2",
            entity.name,
            entity.kind,
            entity.status,
            entity.position[0],
            entity.position[1],
            entity.position[2],
            entity.velocity[0],
            entity.velocity[1],
            entity.velocity[2],
            entity.rotation[0],
            entity.rotation[1],
            entity.rotation[2],
            entity.rotation[3],
            entity.angular_velocity_rad_s[0],
            entity.angular_velocity_rad_s[1],
            entity.angular_velocity_rad_s[2],
            entity.mass_kg,
            entity.radius_m,
            entity.fuel_kg,
            entity.commanded_accel_mps2[0],
            entity.commanded_accel_mps2[1],
            entity.commanded_accel_mps2[2],
        );
    }
    let environment = &snapshot.environment;
    match focus.id {
        EntityId::Phobos => format!(
            "Phobos | approximate radii {:.1}, {:.1}, {:.1} km\nGM {:.0} m^3/s^2 | Mars separation {:.1} km\nAngular rate {:.6} rad/s | frame origin",
            snapshot.body.radii_m[0] / 1000.0,
            snapshot.body.radii_m[1] / 1000.0,
            snapshot.body.radii_m[2] / 1000.0,
            environment.phobos_mu_m3_s2,
            environment.separation_m / 1000.0,
            environment.angular_velocity_rad_s[1],
        ),
        EntityId::Mars => format!(
            "Mars | position {:.1}, {:.1}, {:.1} km\nRadius {:.0} km | GM {:.3e} m^3/s^2",
            environment.mars_position_m[0] / 1000.0,
            environment.mars_position_m[1] / 1000.0,
            environment.mars_position_m[2] / 1000.0,
            environment.mars_radius_m / 1000.0,
            environment.mars_mu_m3_s2,
        ),
        EntityId::L1 => reference_readout("L1", environment.l1_position_m),
        EntityId::L2 => reference_readout("L2", environment.l2_position_m),
        _ => focus.label.into(),
    }
}

fn reference_readout(label: &str, position: [f64; 3]) -> String {
    format!(
        "{label} reference | {:.1}, {:.1}, {:.1} km from Phobos center",
        position[0] / 1000.0,
        position[1] / 1000.0,
        position[2] / 1000.0
    )
}

fn squared_distance(a: [f64; 3], b: [f64; 3]) -> f64 {
    a.iter()
        .zip(b)
        .map(|(left, right)| (left - right).powi(2))
        .sum()
}

fn format_number(value: f64) -> String {
    if value.is_finite() {
        format!("{value:.1}")
    } else {
        "-".into()
    }
}

fn meters(value: f64) -> String {
    if value.is_finite() {
        format!("{value:.1} m")
    } else {
        "-".into()
    }
}

fn speed(value: f64) -> String {
    if value.is_finite() {
        format!("{value:.3} m/s")
    } else {
        "-".into()
    }
}

fn duration(time: f64) -> String {
    if time.is_finite() {
        let seconds = time.max(0.0) as u64;
        format!(
            "{:02}:{:02}:{:02}",
            seconds / 3600,
            (seconds / 60) % 60,
            seconds % 60
        )
    } else {
        "-".into()
    }
}

fn region_label(region: LagrangeRegion) -> &'static str {
    match region {
        LagrangeRegion::L1 => "L1",
        LagrangeRegion::L2 => "L2",
    }
}

#[cfg(test)]
mod interaction_tests {
    use super::*;

    #[test]
    fn p_controls_playback_but_space_and_browser_modifier_chords_do_not() {
        let mut keys = ButtonInput::default();
        keys.press(KeyCode::Space);
        assert!(!plain_key_pressed(&keys, KeyCode::KeyP));
        for modifier in [
            None,
            Some(KeyCode::ControlLeft),
            Some(KeyCode::SuperLeft),
            Some(KeyCode::AltLeft),
        ] {
            let mut keys = ButtonInput::default();
            keys.press(KeyCode::KeyP);
            if let Some(modifier) = modifier {
                keys.press(modifier);
            }
            assert_eq!(plain_key_pressed(&keys, KeyCode::KeyP), modifier.is_none());
        }
    }

    #[test]
    fn shortcuts_leave_browser_modifier_chords_alone() {
        for key in [KeyCode::KeyH, KeyCode::KeyR, KeyCode::KeyI, KeyCode::KeyL] {
            for modifier in [KeyCode::ControlLeft, KeyCode::SuperRight, KeyCode::AltRight] {
                let mut keys = ButtonInput::default();
                keys.press(key);
                keys.press(modifier);
                assert!(!plain_key_pressed(&keys, key));
            }
        }
    }
}
