use bevy::{
    input_focus::{
        FocusCause, InputFocus,
        tab_navigation::{TabGroup, TabNavigationPlugin},
    },
    picking::hover::Hovered,
    prelude::*,
    ui::{InteractionDisabled, Pressed},
    ui_widgets::{Activate, Button},
};

use super::{model::*, view};
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
        );
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
            && (hud.help_open || hud.inspector_open || hud.reset_confirm_open));
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
    }
    if keys.just_pressed(KeyCode::KeyI) {
        hud.inspector_open = !hud.inspector_open;
    }
    if keys.just_pressed(KeyCode::Escape) {
        if hud.reset_confirm_open {
            hud.reset_confirm_open = false;
        } else if hud.help_open {
            hud.help_open = false;
        } else if hud.inspector_open {
            hud.inspector_open = false;
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
            crate::request_action(EngineAction::Viewport(ViewportAction::Fullscreen))
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
        HudAction::ToggleHelp => hud.help_open = !hud.help_open,
        HudAction::ToggleInspector => hud.inspector_open = !hud.inspector_open,
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
    mut previous_compact: Local<Option<bool>>,
    mut layout: Query<(&mut Node, &mut Visibility, &LayoutPart), Without<Overlay>>,
) {
    let sensor = camera::status().mode.sensor().is_some();
    if *previous_sensor != sensor {
        // The clicked view button may now be hidden with its toolbar. Keep
        // keyboard navigation on the canvas rather than on an invisible button.
        input_focus.clear();
        *previous_sensor = sensor;
    }
    for (mut node, mut visibility, part) in &mut layout {
        let shown = match part {
            LayoutPart::MetricsPanel => !sensor || hud.inspector_open,
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
    let compact = windows.single().map(Window::width).unwrap_or(800.0) < 640.0;

    if previous_compact.is_none_or(|previous| previous != compact) {
        for (mut node, _, part) in &mut layout {
            match part {
                LayoutPart::TopRow => {
                    node.width = percent(100.0);
                    node.flex_direction = if compact {
                        FlexDirection::Column
                    } else {
                        FlexDirection::Row
                    };
                }
                LayoutPart::MissionActions => {
                    node.max_width = percent(if compact { 100.0 } else { 72.0 });
                    node.width = if compact { percent(100.0) } else { auto() };
                }
                LayoutPart::BottomRow => {
                    node.width = percent(100.0);
                    node.flex_direction = if compact {
                        FlexDirection::Column
                    } else {
                        FlexDirection::Row
                    };
                }
                LayoutPart::MetricsPanel => {
                    node.width = if compact { percent(100.0) } else { auto() };
                    node.max_width = percent(100.0);
                }
                LayoutPart::CameraPanel => {
                    node.margin.left = if compact { px(0.0) } else { auto() };
                    node.max_width = if compact { percent(100.0) } else { px(330.0) };
                    node.width = if compact { percent(100.0) } else { auto() };
                    node.align_items = if compact {
                        AlignItems::FlexStart
                    } else {
                        AlignItems::FlexEnd
                    };
                }
                LayoutPart::SensorActions => {
                    node.width = percent(100.0);
                }
            }
        }
        *previous_compact = Some(compact);
    }
}

type OverlayVisual = (
    Entity,
    &'static mut Node,
    &'static mut Visibility,
    &'static Overlay,
    Has<TabGroup>,
);

fn update_overlays(
    mut commands: Commands,
    hud: Res<HudState>,
    mut input_focus: ResMut<InputFocus>,
    buttons: Query<(Entity, &HudAction), With<Button>>,
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
    for (entity, mut node, mut visibility, overlay, modal_group) in &mut overlays {
        node.display = match overlay {
            Overlay::Help if hud.help_open => Display::Flex,
            Overlay::Reset if hud.reset_confirm_open => Display::Flex,
            Overlay::Inspector if hud.inspector_open => Display::Flex,
            _ => Display::None,
        };
        *visibility = if node.display == Display::None {
            Visibility::Hidden
        } else {
            Visibility::Visible
        };
        let needs_modal_group = matches!(overlay, Overlay::Reset) && hud.reset_confirm_open;
        if needs_modal_group != modal_group {
            if needs_modal_group {
                commands.entity(entity).insert(TabGroup::modal());
            } else {
                commands.entity(entity).remove::<TabGroup>();
            }
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
    for (entity, action, hovered, pressed, disabled, mut background) in &mut button_views {
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
    }
}

fn update_readouts(playback: Res<PlaybackState>, mut reads: Query<(&mut Text, &Readout)>) {
    let snapshot = crate::snapshot();
    let camera_status = camera::status();
    let focus = focused_item(&snapshot, camera_status.focus);
    for (mut text, readout) in &mut reads {
        let value = match readout {
            Readout::Mission => mission_readout(&snapshot),
            Readout::Environment => environment_readout(&snapshot),
            Readout::CameraMode => format!("{} | {}", camera_status.mode, focus_label(focus)),
            Readout::Focus => format!("Focus: {}", focus_label(focus)),
            Readout::Camera => camera_readout(camera_status),
            Readout::Inspector => inspector_readout(&snapshot, focus),
            Readout::Authority => authority_readout(
                playback.can_control,
                playback.running,
                snapshot.mission.status.is_terminal(),
            ),
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
                    "Exit full [X]".into()
                } else {
                    "Full [X]".into()
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
            Readout::Static => continue,
        };
        if text.0 != value {
            text.0 = value;
        }
    }
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

fn environment_readout(snapshot: &Snapshot) -> String {
    format!(
        "L1 {:.1} km | L2 {:.1} km",
        snapshot.environment.l1_position_m[0] / 1000.0,
        snapshot.environment.l2_position_m[0] / 1000.0,
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
