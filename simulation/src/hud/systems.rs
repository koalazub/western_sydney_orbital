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
        .add_systems(
            Update,
            interactions
                .in_set(EngineSet::Input)
                .after(crate::launchpad::LaunchpadSystems::Input),
        )
        .add_systems(
            Update,
            (
                update_layout,
                update_overlays,
                update_instrument_sections,
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
    super::text_layout::install(app);
    super::editors::install(app);
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
    input_focus: Res<InputFocus>,
    editors: Query<(), With<super::InstrumentEditor>>,
) {
    controls::begin_input_frame();
    super::editors::begin_input_frame(
        input_focus
            .get()
            .is_some_and(|entity| editors.contains(entity)),
        &keys,
    );
    if controls::binding_capture_active()
        && (!camera::input_active()
            || hud.reset_confirm_open
            || hud.panel != SidePanel::Controls
            || super::instrument_editing())
    {
        controls::cancel_capture();
    }
    for input in events.read() {
        if !super::instrument_editing()
            && controls::binding_capture_active()
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
    editors: Query<(), With<super::InstrumentEditor>>,
    mut hud: ResMut<HudState>,
    playback: Res<PlaybackState>,
) {
    // EditableText handles its own keyboard events, but ButtonInput is global.
    // Check actual focus before Escape, P/R, capture or any other HUD shortcut.
    if super::instrument_editing()
        || input_focus
            .get()
            .is_some_and(|entity| editors.contains(entity))
    {
        return;
    }
    let mode = interaction_mode(&hud);
    if mode == InteractionMode::Rebinding {
        return;
    }
    // Placement owns its complete gesture and key-release interval. Escape
    // cancels the preview before it can close a panel or reach a flight key.
    if crate::launchpad::input_blocked() {
        if crate::launchpad::gesture_active() && keys.just_pressed(KeyCode::Escape) {
            crate::launchpad::request(crate::launchpad::LaunchpadAction::Cancel);
        }
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
    } else if crate::launchpad::gesture_active() {
        InteractionMode::LaunchpadPlacement
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
    if crate::launchpad::gesture_active()
        && matches!(
            action,
            HudAction::ToggleDetails
                | HudAction::ToggleInstruments
                | HudAction::ToggleShip
                | HudAction::ToggleDebris
                | HudAction::ToggleLaunch
                | HudAction::ToggleCamera
                | HudAction::ToggleControls
                | HudAction::ToggleInspector
                | HudAction::ChangeCamera(_)
        )
    {
        crate::launchpad::request(crate::launchpad::LaunchpadAction::Cancel);
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
        HudAction::ToggleInstruments => {
            hud.panel = hud.panel.toggle(SidePanel::Flight);
        }
        HudAction::ToggleShip => {
            hud.panel = hud.panel.toggle(SidePanel::Ship);
        }
        HudAction::ToggleDebris => {
            hud.panel = hud.panel.toggle(SidePanel::Debris);
        }
        HudAction::ToggleLaunch => {
            hud.panel = hud.panel.toggle(SidePanel::Launch);
        }
        HudAction::ToggleCamera => {
            hud.panel = hud.panel.toggle(SidePanel::Camera);
        }
        HudAction::CycleIllumination => {
            crate::scene::request_visual_action(crate::scene::VisualAction::CycleIllumination)
        }
        HudAction::ToggleStars => {
            crate::scene::request_visual_action(crate::scene::VisualAction::ToggleStars)
        }
        HudAction::ToggleGuides => {
            crate::scene::request_visual_action(crate::scene::VisualAction::ToggleGuides)
        }
        HudAction::ToggleInstrumentSection(group) => {
            if !hud.expanded_instruments.remove(&group) {
                hud.expanded_instruments.insert(group);
            }
        }
        HudAction::ToggleReference(section) => {
            if !hud.expanded_references.remove(&section) {
                hud.expanded_references.insert(section);
            }
        }
        HudAction::Launchpad(control) => {
            if control == LaunchpadControl::ChooseSurface {
                // Keep the task open logically. Its full overlay hides only
                // during placement, then returns with the result or rejection.
                hud.panel = SidePanel::Launch;
            }
            crate::launchpad::request(control.request());
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
            if hud.panel == SidePanel::Camera {
                hud.panel = SidePanel::Closed;
            }
        }
    }
}

type ChromeLayout = (
    &'static ComputedNode,
    &'static LayoutPart,
    &'static mut Node,
    &'static mut ScrollPosition,
);
type ChromeLayoutFilter = (
    Without<Overlay>,
    Without<DetailsPanel>,
    Without<CompactFlightCue>,
);
type PanelLayoutFilter = (
    With<DetailsPanel>,
    Without<LayoutPart>,
    Without<CompactFlightCue>,
);
type CueLayoutFilter = (
    With<CompactFlightCue>,
    Without<LayoutPart>,
    Without<DetailsPanel>,
);

#[derive(Default)]
pub(super) struct ChromeMeasurements {
    viewport: Vec2,
    top_height: f32,
    placement: bool,
}

pub(super) fn update_layout(
    windows: Query<&Window, With<PrimaryWindow>>,
    ui_scale: Res<UiScale>,
    hud: Res<HudState>,
    mut layout: Query<ChromeLayout, ChromeLayoutFilter>,
    mut details: Query<&mut Node, PanelLayoutFilter>,
    mut cues: Query<&mut Node, CueLayoutFilter>,
    mut natural: Local<ChromeMeasurements>,
) {
    let scale = if ui_scale.0.is_finite() && ui_scale.0 > 0.0 {
        ui_scale.0
    } else {
        1.0
    };
    let (width, height) = windows
        .single()
        .map(|window| (window.width() / scale, window.height() / scale))
        .unwrap_or((800.0, 800.0));
    let viewport = Vec2::new(width, height);
    let placement = crate::launchpad::gesture_active();
    let placement_changed = natural.placement != placement;
    natural.placement = placement;
    let resized = natural.viewport != viewport;
    natural.viewport = viewport;
    let mut top_height = 0.0;
    let mut bottom_height = 0.0;
    for (computed, part, node, _) in &layout {
        let measured = computed.size().y * computed.inverse_scale_factor();
        match part {
            LayoutPart::TopRow => {
                top_height = measured;
                if !placement
                    && node.height == Val::Auto
                    && node.max_height == Val::Auto
                    && measured > 0.0
                {
                    natural.top_height =
                        (computed.content_size().y * computed.inverse_scale_factor()).max(measured);
                }
            }
            LayoutPart::BottomRow => bottom_height = measured,
            LayoutPart::CameraPanel | LayoutPart::SensorActions => {}
        }
    }
    // At extreme zoom or short heights, persistent chrome cannot all fit at
    // once. Keep every main action visible, make the flight strip scrollable,
    // and let a task panel use that space while open. Measurements remain in
    // Flight; no fonts, hit targets or measured inventory are reduced.
    let panel_floor = 2.0 * BUTTON_HEIGHT_PX + 2.0 * PANEL_PADDING_Y_PX;
    let compact = !resized
        && height - natural.top_height - bottom_height - 2.0 * (SCREEN_PADDING_PX + PANEL_GAP_PX)
            < panel_floor;
    let task_open = hud.panel != SidePanel::Closed;
    let top_budget = (height - bottom_height - 2.0 * SCREEN_PADDING_PX - PANEL_GAP_PX).max(0.0);
    // Keep a drag-sized gap around the framed pad, even at 320px and 200%.
    // Both toolbars remain scrollable rather than shrinking native controls.
    let placement_budget = (height * 0.5 - SCREEN_PADDING_PX - BUTTON_HEIGHT_PX).max(0.0);
    for (_, part, mut node, mut position) in &mut layout {
        if placement_changed && matches!(part, LayoutPart::TopRow | LayoutPart::BottomRow) {
            *position = ScrollPosition::default();
        }
        if matches!(part, LayoutPart::TopRow) {
            node.height = if !placement && compact && task_open {
                px(0)
            } else {
                Val::Auto
            };
            node.max_height = if placement {
                px(placement_budget)
            } else if compact {
                px(top_budget)
            } else {
                Val::Auto
            };
            node.overflow = if placement || compact {
                Overflow::scroll_y()
            } else {
                Overflow::visible()
            };
            if !placement && compact && task_open {
                top_height = 0.0;
            }
        } else if matches!(part, LayoutPart::BottomRow) {
            node.max_height = if placement {
                px(placement_budget)
            } else {
                Val::Auto
            };
            node.overflow = if placement {
                Overflow::scroll_y()
            } else {
                Overflow::visible()
            };
        }
    }
    for mut cue in &mut cues {
        cue.display = if !placement && compact && !task_open {
            Display::Flex
        } else {
            Display::None
        };
    }
    let top = SCREEN_PADDING_PX + PANEL_GAP_PX + top_height;
    let bottom = SCREEN_PADDING_PX + PANEL_GAP_PX + bottom_height;
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
    let mut hidden_focus = None;
    for (entity, mut node, mut visibility, overlay, modal_group, mut position) in &mut overlays {
        let was_shown = node.display != Display::None;
        let shown = match overlay {
            Overlay::FlightStrip => !crate::launchpad::gesture_active(),
            Overlay::Flight => hud.panel == SidePanel::Flight,
            Overlay::Ship => hud.panel == SidePanel::Ship,
            Overlay::Debris => hud.panel == SidePanel::Debris,
            Overlay::Launch => {
                hud.panel == SidePanel::Launch && !crate::launchpad::gesture_active()
            }
            Overlay::LaunchpadPreview => crate::launchpad::gesture_active(),
            Overlay::Camera => hud.panel == SidePanel::Camera,
            Overlay::Details => hud.panel.details_open(),
            Overlay::Controls => hud.panel == SidePanel::Controls,
            Overlay::Reset => hud.reset_confirm_open,
            Overlay::Inspector => hud.panel == SidePanel::Inspector,
        };
        node.display = if shown { Display::Flex } else { Display::None };
        if matches!(
            overlay,
            Overlay::Flight
                | Overlay::Ship
                | Overlay::Debris
                | Overlay::Launch
                | Overlay::Camera
                | Overlay::Details
                | Overlay::Controls
        ) && !was_shown
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
            hidden_focus = Some(match overlay {
                Overlay::Camera => SidePanel::Camera,
                Overlay::Controls => SidePanel::Controls,
                Overlay::FlightStrip | Overlay::Flight => SidePanel::Flight,
                Overlay::Ship => SidePanel::Ship,
                Overlay::Debris => SidePanel::Debris,
                Overlay::Launch | Overlay::LaunchpadPreview => SidePanel::Launch,
                Overlay::Details | Overlay::Inspector | Overlay::Reset => SidePanel::Controls,
            });
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
    if let Some(panel) = hidden_focus
        && !hud.reset_confirm_open
    {
        if crate::launchpad::gesture_active()
            && let Some((entity, _)) = buttons.iter().find(|(_, action)| {
                matches!(action, HudAction::Launchpad(LaunchpadControl::Cancel))
            })
        {
            input_focus.set(entity, FocusCause::Navigated);
        } else if panel == SidePanel::Launch
            && hud.panel == SidePanel::Launch
            && let Some((entity, _)) = buttons.iter().find(|(_, action)| {
                matches!(
                    action,
                    HudAction::Launchpad(LaunchpadControl::ChooseSurface)
                )
            })
        {
            input_focus.set(entity, FocusCause::Navigated);
        } else if let Some((entity, _)) = buttons.iter().find(|(entity, action)| {
            let matches_panel = match panel {
                SidePanel::Camera => matches!(action, HudAction::ToggleCamera),
                SidePanel::Controls => matches!(action, HudAction::ToggleControls),
                SidePanel::Flight => matches!(action, HudAction::ToggleInstruments),
                SidePanel::Ship => matches!(action, HudAction::ToggleShip),
                SidePanel::Debris => matches!(action, HudAction::ToggleDebris),
                SidePanel::Launch => matches!(action, HudAction::ToggleLaunch),
                SidePanel::Details | SidePanel::Inspector => {
                    matches!(action, HudAction::ToggleControls)
                }
                SidePanel::Closed => false,
            };
            matches_panel
                && !parents
                    .iter_ancestors(*entity)
                    .any(|parent| overlays.contains(parent))
        }) {
            input_focus.set(entity, FocusCause::Navigated);
        } else {
            input_focus.clear();
        }
    }
}

pub(super) fn update_instrument_sections(
    hud: Res<HudState>,
    mut sections: Query<
        (&InstrumentContents, &mut Node, &mut Visibility),
        Without<ReferenceContents>,
    >,
    mut references: Query<
        (&ReferenceContents, &mut Node, &mut Visibility),
        Without<InstrumentContents>,
    >,
) {
    for (section, mut node, mut visibility) in &mut sections {
        let expanded = hud.expanded_instruments.contains(&section.0);
        let display = if expanded {
            Display::Flex
        } else {
            Display::None
        };
        let shown = if expanded {
            Visibility::Inherited
        } else {
            Visibility::Hidden
        };
        if node.display != display {
            node.display = display;
        }
        if *visibility != shown {
            *visibility = shown;
        }
    }
    for (section, mut node, mut visibility) in &mut references {
        let expanded = hud.expanded_references.contains(&section.0);
        node.display = if expanded {
            Display::Flex
        } else {
            Display::None
        };
        *visibility = if expanded {
            Visibility::Inherited
        } else {
            Visibility::Hidden
        };
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
    Option<&'static TaskTab>,
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
    let visual_settings: crate::scene::VisualSettings = crate::scene::visual_settings();
    let mode = interaction_mode(&hud);
    for (
        entity,
        action,
        hovered,
        pressed,
        disabled,
        mut background,
        mut accessible,
        mut outline,
        task_tab,
    ) in &mut button_views
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
        let (mut selected, expanded) = match *action {
            HudAction::ChangeCamera(mode) => (Some(mode == camera_status.mode), None),
            HudAction::ToggleDetails => (None, Some(hud.panel.details_open())),
            HudAction::ToggleInstruments => (None, Some(hud.panel == SidePanel::Flight)),
            HudAction::ToggleShip => (None, Some(hud.panel == SidePanel::Ship)),
            HudAction::ToggleDebris => (None, Some(hud.panel == SidePanel::Debris)),
            HudAction::ToggleLaunch => (None, Some(hud.panel == SidePanel::Launch)),
            HudAction::ToggleCamera => (None, Some(hud.panel == SidePanel::Camera)),
            HudAction::ToggleControls => (None, Some(hud.panel == SidePanel::Controls)),
            HudAction::ToggleInspector => (None, Some(hud.panel == SidePanel::Inspector)),
            HudAction::ToggleStars => (Some(visual_settings.stars), None),
            HudAction::ToggleGuides => (Some(visual_settings.guides), None),
            HudAction::ToggleInstrumentSection(group) => {
                (None, Some(hud.expanded_instruments.contains(&group)))
            }
            HudAction::ToggleReference(section) => {
                (None, Some(hud.expanded_references.contains(&section)))
            }
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
            | HudAction::FocusPrevious
            | HudAction::Launchpad(_) => (None, None),
            HudAction::CycleIllumination => (None, None),
        };
        if let Some(tab) = task_tab {
            selected = Some(
                hud.panel == tab.0 || (tab.0 == SidePanel::Controls && hud.panel.details_open()),
            );
        }
        if let HudAction::Launchpad(control) = action {
            let description = match control {
                LaunchpadControl::FramePhobos => {
                    "Frame Phobos in the debug viewer without moving the launchpad."
                }
                LaunchpadControl::ChooseSurface => {
                    "Pause flight and frame the pad. Drag the pad or lift grip; release moves both pad and lander without Apply. Or click a surface point or use arrow keys, then Apply. Esc cancels."
                }
                LaunchpadControl::Apply => {
                    "Commit the previewed launch site and seat the lander at rest on the pad."
                }
                LaunchpadControl::Cancel => {
                    "Discard the launch site preview without changing physics."
                }
                LaunchpadControl::North
                | LaunchpadControl::South
                | LaunchpadControl::East
                | LaunchpadControl::West => {
                    "Nudge only the launch site preview. Apply commits; Cancel leaves physics unchanged."
                }
            };
            if accessible.description() != Some(description) {
                accessible.set_description(description);
            }
        }
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
    mut reads: Query<(Entity, &mut Text, &Readout, &ChildOf)>,
    mut buttons: Query<&mut AccessibilityNode, With<Button>>,
    parents: Query<&ChildOf>,
    nodes: Query<&Node>,
) {
    let snapshot = crate::snapshot();
    let camera_status = camera::status();
    let visual_settings = crate::scene::visual_settings();
    let focus = focused_item(&snapshot, camera_status.focus);
    for (entity, mut text, readout, parent) in &mut reads {
        // Use this frame's disclosure state, not last frame's inherited
        // visibility. Newly opened panels refresh before their first layout.
        if parents.iter_ancestors(entity).any(|ancestor| {
            nodes
                .get(ancestor)
                .is_ok_and(|node| node.display == Display::None)
        }) {
            continue;
        }
        let value = match readout {
            Readout::Summary => summary_readout(
                &snapshot,
                &playback,
                crate::session::keyboard_owns_command(),
            ),
            Readout::PathBrief => crate::navigation::brief(),
            Readout::PathDetails => crate::navigation::details(),
            Readout::FlightControls => flight_controls_readout(&snapshot, &playback),
            Readout::TelemetryDetails => crate::telemetry::details(),
            Readout::Instrument(key) => crate::telemetry::instrument(*key),
            Readout::InstrumentGroup(group) => crate::telemetry::group_status(*group),
            Readout::InstrumentSection(group) => format!(
                "{} {}",
                if hud.expanded_instruments.contains(group) {
                    "-"
                } else {
                    "+"
                },
                group.title()
            ),
            Readout::ReferenceToggle(section) => format!(
                "{} {}",
                if hud.expanded_references.contains(section) {
                    "-"
                } else {
                    "+"
                },
                section.title()
            ),
            Readout::LaunchpadSummary => crate::launchpad::summary(),
            Readout::LaunchSite => launch_site_readout(&snapshot),
            Readout::LaunchpadHint => {
                "Place lander pauses flight and frames the pad. Drag the pad or lift grip; release moves both pad and lander without Apply. Esc cancels.".into()
            }
            Readout::DebrisPlayback => {
                if snapshot.mission.status.is_terminal() {
                    "Mission ended. Stored asteroid velocities are not advancing motion.".into()
                } else if playback.running {
                    "Running. Actual body dynamics are sampled about twice per second.".into()
                } else {
                    "Paused. Nonzero stored velocities do not mean asteroids are moving.".into()
                }
            }
            Readout::TelemetryStatus => crate::telemetry::sample_status(),
            Readout::InputSettings => input_settings_readout(&playback, &keys),
            Readout::BindingStatus => controls::capture_message(),
            Readout::Binding(action) => format!(
                "{}: {} · change",
                action.label(),
                controls::key_label(*action)
            ),
            Readout::Notebook => "Notebook · new tab".into(),
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
                    "Pause sim [P]".into()
                } else {
                    "Play sim [P]".into()
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
                "Close details"
            } else {
                "Details +"
            }
            .into(),
            Readout::InstrumentsToggle => if hud.panel == SidePanel::Flight {
                "Flight -"
            } else {
                "Flight +"
            }
            .into(),
            Readout::ShipToggle => if hud.panel == SidePanel::Ship {
                "Ship -"
            } else {
                "Ship +"
            }
            .into(),
            Readout::DebrisToggle => if hud.panel == SidePanel::Debris {
                "Debris -"
            } else {
                "Debris +"
            }
            .into(),
            Readout::LaunchToggle => if hud.panel == SidePanel::Launch {
                "Launch -"
            } else {
                "Launch +"
            }
            .into(),
            Readout::CameraToggle => if hud.panel == SidePanel::Camera {
                "Views -"
            } else {
                "Views +"
            }
            .into(),
            Readout::ControlsToggle => if hud.panel == SidePanel::Controls {
                "Help - [H]"
            } else {
                "Help [H]"
            }
            .into(),
            Readout::InspectorToggle => if hud.panel == SidePanel::Inspector {
                "Close inspector [I]"
            } else {
                "Inspect [I]"
            }
            .into(),
            Readout::Illumination => format!(
                "Viewer light: {}",
                match visual_settings.illumination {
                    crate::scene::IlluminationMode::Natural => "Natural",
                    crate::scene::IlluminationMode::Context => "Context",
                    crate::scene::IlluminationMode::Inspection => "Inspection",
                }
            ),
            Readout::Stars => format!(
                "Stars: {}",
                if visual_settings.stars { "on" } else { "off" }
            ),
            Readout::Guides => format!(
                "Debug guides: {}",
                if visual_settings.guides { "on" } else { "off" }
            ),
            Readout::Static => continue,
        };
        let value = if value.is_ascii() {
            value
        } else {
            super::readable_text(&value)
        };
        if text.0 != value {
            if let Ok(mut accessible) = buttons.get_mut(parent.parent()) {
                accessible.set_label(value.clone());
            }
            text.0 = value;
        }
    }
}

type ScrollPanel = (
    Entity,
    &'static Node,
    &'static ComputedNode,
    &'static mut ScrollPosition,
    Option<&'static LayoutPart>,
);
type ScrollPanelFilter = Or<(With<DetailsPanel>, With<LayoutPart>)>;

pub(super) fn scroll_details(
    mut event: On<FocusedInput<KeyboardInput>>,
    mut panels: Query<ScrollPanel, ScrollPanelFilter>,
    parents: Query<&ChildOf>,
    inputs: Query<(), With<bevy::ui_widgets::TextInput>>,
    hud: Res<HudState>,
) {
    if inputs.contains(event.original_event_target()) {
        return;
    }
    if controls::binding_actions_blocked() {
        event.propagate(false);
        return;
    }
    if event.input.state != ButtonState::Pressed {
        return;
    }
    if crate::launchpad::input_blocked()
        && matches!(
            event.input.key_code,
            KeyCode::ArrowUp | KeyCode::ArrowDown | KeyCode::ArrowLeft | KeyCode::ArrowRight
        )
    {
        // Armed placement owns its direction keys. Page Up/Down still scrolls
        // the panel, and Tab/Enter still reaches its native nudge buttons.
        event.propagate(false);
        return;
    }
    let focused_panel = parents
        .iter_ancestors(event.original_event_target())
        .find(|ancestor| {
            panels
                .get(*ancestor)
                .is_ok_and(|(_, node, computed, _, _)| {
                    node.display != Display::None
                        && !computed.is_empty()
                        && node.overflow.y == OverflowAxis::Scroll
                })
        });
    let Some((_, _, computed, mut position, _)) =
        panels.iter_mut().find(|(entity, node, computed, _, part)| {
            node.display != Display::None
                && !computed.is_empty()
                && focused_panel.is_none_or(|focused| focused == *entity)
                && match part {
                    Some(LayoutPart::TopRow) => {
                        (hud.panel == SidePanel::Closed || crate::launchpad::gesture_active())
                            && node.overflow.y == OverflowAxis::Scroll
                    }
                    Some(LayoutPart::BottomRow) => focused_panel == Some(*entity),
                    Some(LayoutPart::CameraPanel | LayoutPart::SensorActions) => false,
                    None => hud.panel != SidePanel::Closed,
                }
        })
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

type FocusableControls = Or<(With<Button>, With<super::InstrumentEditor>)>;

pub(super) fn scroll_focused_control_into_view(
    mut commands: Commands,
    input_focus: Res<InputFocus>,
    buttons: Query<(), FocusableControls>,
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
            .map(|score| format!(" | legacy SI total {:.0}, not STP", score))
            .unwrap_or_default()
    } else {
        String::new()
    };
    format!(
        "T+{} | {} ({})\nReturn ship {} | range {} | speed {}\nFuel used ~{} kg | damage estimate ~{} J\n{} debris | {} contacts | {} scene entities{}\nRecorded local interventions: {}",
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

fn summary_readout(snapshot: &Snapshot, playback: &PlaybackState, manual_command: bool) -> String {
    let state = if snapshot.mission.status.is_terminal() {
        snapshot.mission.status.to_string()
    } else if playback.running {
        "running".into()
    } else {
        "paused".into()
    };
    format!(
        "T+{} | {state} · {}x · {}{}",
        duration(snapshot.time_s),
        playback.rate.factor() as u32,
        command_owner_label(manual_command),
        if playback.can_control {
            ""
        } else {
            " · view only"
        },
    )
}

fn environment_readout(snapshot: &Snapshot) -> String {
    format!(
        "L1 {:.1} km | L2 {:.1} km",
        snapshot.environment.l1_position_m[0] / 1000.0,
        snapshot.environment.l2_position_m[0] / 1000.0,
    )
}

fn flight_controls_readout(snapshot: &Snapshot, playback: &PlaybackState) -> String {
    if snapshot.mission.status.is_terminal() {
        return "Mission ended. Reset to start another flight; tuning keeps this result intact."
            .into();
    }
    if controls::binding_capture_active() {
        return "Changing a key. Manual flight is blocked; press one key or Esc to cancel.".into();
    }
    let state = if !playback.can_control {
        "Ship controls unavailable in view-only mode."
    } else if !playback.running {
        return format!(
            "Lander paused. {} launches; P or Play sim resumes playback.",
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
        "{} and {}: reference limit {:.2} m/s²",
        controls::key_label(controls::Action::Lift),
        controls::key_label(controls::Action::Up),
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
        "Input {input}\nHeld keys: {}\nCommand owner: {}\nSimulation {}\nReference command caps: {:.2} m/s² and {:.2} rad/s²\n{}",
        crate::controls::held_key_labels(keys),
        command_owner_label(crate::session::keyboard_owns_command()),
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

fn command_owner_label(manual: bool) -> &'static str {
    if manual { "manual keys" } else { "API / idle" }
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
            "{} | {} | {}\nPosition {:.1}, {:.1}, {:.1} m\nVelocity {:.3}, {:.3}, {:.3} m/s\nQuaternion {:.4}, {:.4}, {:.4}, {:.4}\nAngular velocity {:.4}, {:.4}, {:.4} rad/s\nMass {:.1} kg | radius {:.2} m | fuel {:.1} kg\nRequested acceleration {:.4}, {:.4}, {:.4} m/s^2",
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
        "--".into()
    }
}

fn meters(value: f64) -> String {
    if value.is_finite() {
        format!("{value:.1} m")
    } else {
        "--".into()
    }
}

fn speed(value: f64) -> String {
    if value.is_finite() {
        format!("{value:.3} m/s")
    } else {
        "--".into()
    }
}

fn duration(time: f64) -> String {
    if time.is_finite() && time >= 0.0 {
        let seconds = time as u64;
        format!(
            "{:02}:{:02}:{:02}",
            seconds / 3600,
            (seconds / 60) % 60,
            seconds % 60
        )
    } else {
        "--".into()
    }
}

fn region_label(region: LagrangeRegion) -> &'static str {
    match region {
        LagrangeRegion::L1 => "L1",
        LagrangeRegion::L2 => "L2",
        LagrangeRegion::LocalUp => "local up",
    }
}

pub(in crate::hud) fn launch_site_readout(snapshot: &Snapshot) -> String {
    format!(
        "{}\nTarget {}",
        if snapshot.launch_site_direction.is_some() {
            "Custom launch site"
        } else {
            "Template launch site"
        },
        region_label(snapshot.mission.target_region)
    )
}

#[cfg(test)]
mod interaction_tests {
    use super::*;

    #[test]
    fn a_custom_launch_site_does_not_claim_the_template_hemisphere() {
        let mut snapshot = crate::physics::Simulation::new_default().snapshot();
        snapshot.launch_site_direction = Some([-1.0, 0.0, 0.0]);
        snapshot.mission.target_region = LagrangeRegion::L1;
        assert_eq!(
            launch_site_readout(&snapshot),
            "Custom launch site\nTarget L1"
        );
        snapshot.mission.target_region = LagrangeRegion::L2;
        assert_eq!(
            launch_site_readout(&snapshot),
            "Custom launch site\nTarget L2"
        );
    }

    #[test]
    fn summary_reports_playback_and_command_ownership_without_metadata_or_fake_time() {
        let mut snapshot = crate::physics::Simulation::new_default().snapshot();
        let mut playback = PlaybackState::default();
        assert!(summary_readout(&snapshot, &playback, true).contains("paused · 1x · manual keys"));
        playback.running = true;
        assert!(summary_readout(&snapshot, &playback, false).contains("running · 1x · API / idle"));
        playback.can_control = false;
        assert!(
            summary_readout(&snapshot, &playback, false)
                .contains("running · 1x · API / idle · view only")
        );
        for invalid in [f64::NAN, f64::INFINITY, -1.0] {
            snapshot.time_s = invalid;
            assert!(summary_readout(&snapshot, &playback, false).starts_with("T+-- |"));
        }
        let text = summary_readout(&snapshot, &playback, false);
        for metadata in ["local model", "STP", "limit", "Click", "controls", "m/s"] {
            assert!(!text.contains(metadata));
        }
    }

    #[test]
    fn terminal_flight_help_never_promises_that_lift_or_tuning_will_restart_it() {
        let mut snapshot = crate::physics::Simulation::new_default().snapshot();
        let playback = PlaybackState::default();
        assert!(flight_controls_readout(&snapshot, &playback).contains("launches"));
        for status in [
            crate::domain::MissionStatus::Success,
            crate::domain::MissionStatus::FailedDamage,
        ] {
            snapshot.mission.status = status;
            let help = flight_controls_readout(&snapshot, &playback);
            assert!(help.contains("Mission ended") && help.contains("Reset"));
            assert!(!help.contains("launches"));
        }
    }

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
