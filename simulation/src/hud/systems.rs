use bevy::{
    a11y::AccessibilityNode,
    input::{ButtonState, keyboard::KeyboardInput},
    input_focus::{
        FocusCause, FocusedInput, InputFocus,
        tab_navigation::{TabGroup, TabNavigationPlugin},
    },
    picking::hover::Hovered,
    prelude::*,
    ui::{InteractionDisabled, Pressed, UiSystems},
    ui_widgets::{Activate, Button, ScrollIntoView},
};

use super::{input, model::*, view};
use crate::{
    EngineSet, PlaybackState, camera,
    domain::{CameraRequest, EngineAction, EntityId, EntityKind, LagrangeRegion, ViewportAction},
    model::Snapshot,
};
use bevy::window::PrimaryWindow;

pub(super) fn install(app: &mut App) {
    if !app.is_plugin_added::<TabNavigationPlugin>() {
        app.add_plugins(TabNavigationPlugin);
    }
    app.init_resource::<HudState>()
        .add_systems(Startup, view::setup)
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
    if hud.reset_confirm_open && !matches!(action, HudAction::ConfirmReset | HudAction::CancelReset)
    {
        return;
    }
    activate_action(*action, &mut hud, &playback);
}

fn interactions(
    keys: Res<ButtonInput<KeyCode>>,
    input_focus: Res<InputFocus>,
    buttons: Query<(), With<Button>>,
    mut hud: ResMut<HudState>,
    playback: Res<PlaybackState>,
) {
    let button_focused = input_focus
        .get()
        .is_some_and(|entity| buttons.contains(entity));
    let shortcuts_active = (camera::input_active() && !button_focused)
        || (keys.just_pressed(KeyCode::Escape)
            && (hud.controls.expanded()
                || hud.help_open
                || hud.inspector_open
                || hud.reset_confirm_open));
    if !shortcuts_active {
        return;
    }
    if hud.reset_confirm_open {
        if keys.just_pressed(KeyCode::Escape) {
            hud.reset_confirm_open = false;
        } else if !button_focused && keys.just_pressed(KeyCode::Enter) && playback.can_control {
            crate::request_action(EngineAction::Reset);
            hud.reset_confirm_open = false;
        }
        return;
    }
    if keys.just_pressed(KeyCode::Space)
        && playback.can_control
        && !crate::snapshot().mission.status.is_terminal()
    {
        crate::request_action(EngineAction::TogglePlayback);
    }
    if keys.just_pressed(KeyCode::Period)
        && playback.can_control
        && !playback.running
        && !crate::snapshot().mission.status.is_terminal()
    {
        crate::request_action(EngineAction::Step);
    }
    if keys.just_pressed(KeyCode::KeyT) && playback.can_control {
        crate::request_action(EngineAction::CycleRate);
    }
    if keys.just_pressed(KeyCode::KeyR) && playback.can_control {
        hud.reset_confirm_open = true;
    }
    if keys.just_pressed(KeyCode::Comma) {
        cycle_focus(&crate::snapshot(), false);
    }
    if keys.just_pressed(KeyCode::Slash) {
        cycle_focus(&crate::snapshot(), true);
    }
    if keys.just_pressed(KeyCode::KeyM) {
        crate::request_action(EngineAction::Viewport(ViewportAction::Maximize));
    }
    #[cfg(not(target_arch = "wasm32"))]
    if keys.just_pressed(KeyCode::KeyX) {
        crate::request_action(EngineAction::Viewport(ViewportAction::Fullscreen));
    }
    if keys.just_pressed(KeyCode::KeyL) {
        let action = if camera::status().locked {
            ViewportAction::Release
        } else {
            ViewportAction::Capture
        };
        crate::request_action(EngineAction::Viewport(action));
    }
    if keys.just_pressed(KeyCode::KeyH) {
        hud.help_open = !hud.help_open;
        if hud.help_open {
            hud.controls = ControlsState::Expanded;
        }
    }
    if keys.just_pressed(KeyCode::KeyI) {
        hud.inspector_open = !hud.inspector_open;
        if hud.inspector_open {
            hud.controls = ControlsState::Expanded;
        }
    }
    if keys.just_pressed(KeyCode::Escape) {
        if hud.reset_confirm_open {
            hud.reset_confirm_open = false;
        } else if hud.help_open {
            hud.help_open = false;
        } else if hud.inspector_open {
            hud.inspector_open = false;
        } else if hud.controls.expanded() {
            hud.controls = ControlsState::Compact;
        }
    }
    if hud.reset_confirm_open && keys.just_pressed(KeyCode::Enter) && playback.can_control {
        crate::request_action(EngineAction::Reset);
        hud.reset_confirm_open = false;
    }
}

fn activate_action(action: HudAction, hud: &mut HudState, playback: &PlaybackState) {
    match action {
        HudAction::TogglePlayback
            if playback.can_control && !crate::snapshot().mission.status.is_terminal() =>
        {
            crate::request_action(EngineAction::TogglePlayback)
        }
        HudAction::Step
            if playback.can_control
                && !playback.running
                && !crate::snapshot().mission.status.is_terminal() =>
        {
            crate::request_action(EngineAction::Step)
        }
        HudAction::AskReset if playback.can_control => hud.reset_confirm_open = true,
        HudAction::ConfirmReset if playback.can_control && hud.reset_confirm_open => {
            crate::request_action(EngineAction::Reset);
            hud.reset_confirm_open = false;
        }
        HudAction::CancelReset => hud.reset_confirm_open = false,
        HudAction::Export if playback.can_control => {
            crate::request_action(EngineAction::Viewport(ViewportAction::Export))
        }
        HudAction::CycleRate if playback.can_control => {
            crate::request_action(EngineAction::CycleRate)
        }
        HudAction::ToggleAutopilot if playback.can_control => {
            crate::request_action(EngineAction::ToggleAutopilot)
        }
        HudAction::Fullscreen => {
            // Web fullscreen must be requested by the trusted DOM event adapter,
            // using the actual native hit/focus target before Bevy's next frame.
            #[cfg(not(target_arch = "wasm32"))]
            crate::request_action(EngineAction::Viewport(ViewportAction::Fullscreen));
        }
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
        HudAction::ToggleControls => {
            hud.controls = match hud.controls {
                ControlsState::Compact => ControlsState::Expanded,
                ControlsState::Expanded => ControlsState::Compact,
            };
            if !hud.controls.expanded() {
                hud.help_open = false;
                hud.inspector_open = false;
            }
        }
        HudAction::ToggleHelp => {
            hud.help_open = !hud.help_open;
            if hud.help_open {
                hud.controls = ControlsState::Expanded;
            }
        }
        HudAction::ToggleInspector => {
            hud.inspector_open = !hud.inspector_open;
            if hud.inspector_open {
                hud.controls = ControlsState::Expanded;
            }
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
        _ => {}
    }
}

fn update_layout(
    windows: Query<&Window, With<PrimaryWindow>>,
    hud: Res<HudState>,
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
    let (width, height) = windows
        .single()
        .map(|window| (window.width(), window.height()))
        .unwrap_or((800.0, 800.0));
    let mut top = SCREEN_PADDING_PX + PANEL_GAP_PX;
    let mut bottom = SCREEN_PADDING_PX + PANEL_GAP_PX;
    for (mut node, mut visibility, computed, part) in &mut layout {
        let height = computed.size().y * computed.inverse_scale_factor();
        match part {
            LayoutPart::TopRow => top += height,
            LayoutPart::BottomRow => bottom += height,
            LayoutPart::MissionActions
            | LayoutPart::MetricsPanel
            | LayoutPart::CameraPanel
            | LayoutPart::SensorActions => {}
        }
        let shown = match part {
            LayoutPart::MetricsPanel => hud.controls.expanded(),
            LayoutPart::CameraPanel => !sensor,
            LayoutPart::SensorActions => sensor,
            LayoutPart::TopRow | LayoutPart::MissionActions | LayoutPart::BottomRow => true,
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

fn update_overlays(
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
        node.display = match overlay {
            Overlay::Controls if hud.controls.expanded() => Display::Flex,
            Overlay::Help if hud.help_open => Display::Flex,
            Overlay::Reset if hud.reset_confirm_open => Display::Flex,
            Overlay::Inspector if hud.inspector_open => Display::Flex,
            _ => Display::None,
        };
        if matches!(overlay, Overlay::Controls) && !was_shown && node.display != Display::None {
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
            .find(|(_, action)| matches!(action, HudAction::ToggleControls))
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

fn update_buttons(
    mut commands: Commands,
    input_focus: Res<InputFocus>,
    playback: Res<PlaybackState>,
    hud: Res<HudState>,
    mut button_views: Query<ButtonVisual, With<Button>>,
) {
    let snapshot = crate::snapshot();
    let camera_status = camera::status();
    for (entity, action, hovered, pressed, disabled, mut background, mut accessible, mut outline) in
        &mut button_views
    {
        let should_disable = hud.reset_confirm_open
            && !matches!(action, HudAction::ConfirmReset | HudAction::CancelReset)
            || is_control(*action)
                && (!playback.can_control
                    || matches!(action, HudAction::Step)
                        && (playback.running || snapshot.mission.status.is_terminal())
                    || matches!(action, HudAction::TogglePlayback)
                        && snapshot.mission.status.is_terminal());
        if should_disable != disabled {
            if should_disable {
                commands.entity(entity).insert(InteractionDisabled);
            } else {
                commands.entity(entity).remove::<InteractionDisabled>();
            }
        }
        let selected = match action {
            HudAction::ChangeCamera(mode) => *mode == camera_status.mode,
            _ => false,
        };
        let expanded = match action {
            HudAction::ToggleControls => Some(hud.controls.expanded()),
            HudAction::ToggleHelp => Some(hud.help_open),
            HudAction::ToggleInspector => Some(hud.inspector_open),
            _ => None,
        };
        if let Some(expanded) = expanded
            && accessible.is_expanded() != Some(expanded)
        {
            accessible.set_expanded(expanded);
        }
        if matches!(action, HudAction::ChangeCamera(_))
            && accessible.is_selected() != Some(selected)
        {
            accessible.set_selected(selected);
        }
        let keyboard_focused = input_focus.get() == Some(entity);
        let color = if should_disable {
            DISABLED
        } else if keyboard_focused {
            FOCUSED
        } else if pressed || selected {
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

fn update_readouts(
    playback: Res<PlaybackState>,
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
                    "Pause [Space]".into()
                } else {
                    "Run [Space]".into()
                }
            }
            Readout::Rate => format!("{}x [T]", playback.rate.factor() as u32),
            Readout::Autopilot => {
                if playback.autopilot {
                    "Example controller: on".into()
                } else {
                    "Example controller: off".into()
                }
            }
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
            Readout::Controls => if hud.controls.expanded() {
                "Details -"
            } else {
                "Details +"
            }
            .into(),
            Readout::HelpToggle => if hud.help_open {
                "Close help [H]"
            } else {
                "Help [H]"
            }
            .into(),
            Readout::InspectorToggle => if hud.inspector_open {
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
    mut panels: Query<(&ComputedNode, &mut ScrollPosition), With<DetailsPanel>>,
) {
    if event.input.state != ButtonState::Pressed {
        return;
    }
    let Ok((computed, mut position)) = panels.single_mut() else {
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

fn scroll_focused_control_into_view(
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

fn is_control(action: HudAction) -> bool {
    matches!(
        action,
        HudAction::TogglePlayback
            | HudAction::Step
            | HudAction::AskReset
            | HudAction::ConfirmReset
            | HudAction::Export
            | HudAction::CycleRate
            | HudAction::ToggleAutopilot
    )
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
        "T+{} | {} ({})\nReturn ship {} | range {} | speed {}\nFuel used ~{} kg | damage estimate ~{} J\n{} debris | {} contacts | {} scene entities{}",
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
    let state = if !playback.can_control {
        "Ship controls unavailable in view-only mode."
    } else if !playback.running {
        "Ship paused. Press Space or Run before flying."
    } else if !camera::input_active() {
        "Click the scene or focus the canvas to enable movement keys."
    } else if crate::session::keyboard_requires_release() {
        "Release the movement keys, then press them again to rearm ship thrust."
    } else if playback.autopilot {
        "Example controller active. A ship thrust key switches to manual control."
    } else {
        "Manual ship control. Release thrust keys to coast."
    };
    format!(
        "{state}\nShip: W/S forward/back, A/D left/right, E/Q up/down. E lifts off from the pad.\nAlt/Option + WASD/QE: debug camera only. Shift boosts the debug camera, not the ship.\nRelease keys to stop thrust. The ship keeps coasting; opposite thrust slows it."
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
