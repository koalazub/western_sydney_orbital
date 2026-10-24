//! CPU-only checks of the production HUD, including FiraMono shaping and Taffy layout.
//! These do not exercise render extraction or prove that a GPU draws the HUD.

#![cfg(not(target_arch = "wasm32"))]

use bevy::{
    asset::{AssetApp, AssetEventSystems, AssetId, AssetPlugin},
    camera::{CameraPlugin, CameraUpdateSystems, RenderTarget, visibility::update_frusta},
    image::ImagePlugin,
    input::InputPlugin,
    input_focus::{InputDispatchPlugin, InputFocus, InputFocusPlugin, InputFocusSystems},
    mesh::MeshPlugin,
    picking::{InteractionPlugin, PickingPlugin},
    prelude::*,
    render::{RenderApp, camera::camera_system, texture::ManualTextureViews},
    text::{TextLayoutInfo, TextPlugin},
    ui::UiPlugin,
    window::{ExitCondition, WindowRef, WindowResolution},
};

use super::model::{HudAction, HudState, Readout};
use crate::PlaybackState;
use bevy::input::{
    ButtonState,
    keyboard::{Key, KeyboardInput},
};

#[test]
fn desktop_hud_survives_telemetry_and_reference_thrust_text() {
    check_hud(1280, 800, 1.0, 1.0);
}

#[test]
fn narrow_hud_keeps_run_details_and_telemetry_in_view() {
    check_hud(320, 640, 1.0, 1.0);
}

#[test]
fn short_hud_keeps_run_details_and_telemetry_in_view() {
    check_hud(800, 360, 1.0, 1.0);
}

#[test]
fn hidpi_hud_uses_the_real_window_scale() {
    check_hud(1280, 800, 2.0, 1.0);
}

#[test]
fn enlarged_ui_keeps_critical_chrome_in_view() {
    check_hud(1280, 800, 1.0, 2.0);
}

fn check_hud(width: u32, height: u32, window_scale: f32, ui_scale: f32) {
    crate::controls::reset_for_tests();
    let mut app = App::new();
    app.add_plugins(MinimalPlugins)
        .add_plugins((
            AssetPlugin {
                watch_for_changes_override: Some(false),
                ..default()
            },
            TransformPlugin,
            InputPlugin,
            WindowPlugin {
                primary_window: Some(Window {
                    resolution: WindowResolution::new(
                        (width as f32 * window_scale) as u32,
                        (height as f32 * window_scale) as u32,
                    )
                    .with_scale_factor_override(window_scale),
                    ..default()
                }),
                exit_condition: ExitCondition::DontExit,
                close_when_requested: false,
                ..default()
            },
            ImagePlugin::default(),
            MeshPlugin,
            CameraPlugin,
            TextPlugin,
            PickingPlugin,
            InteractionPlugin,
            UiPlugin,
            InputFocusPlugin,
            InputDispatchPlugin,
            bevy::ui_widgets::ButtonPlugin,
            bevy::ui_widgets::ScrollAreaPlugin,
        ))
        .init_asset::<TextureAtlasLayout>()
        .init_resource::<ManualTextureViews>()
        .init_resource::<InputFocus>()
        .init_resource::<HudState>()
        .init_resource::<crate::PlaybackState>()
        .insert_resource(UiScale(ui_scale))
        .add_systems(Startup, super::view::setup)
        // This is Bevy's real window-to-camera calculation, without RenderPlugin.
        .add_systems(
            PostUpdate,
            camera_system
                .in_set(CameraUpdateSystems)
                .before(AssetEventSystems)
                .before(update_frusta),
        );
    super::input::install(&mut app);
    let camera = app
        .world_mut()
        .spawn((
            Camera2d,
            crate::camera::SimulationCamera,
            RenderTarget::Window(WindowRef::Primary),
        ))
        .id();
    app.finish();
    app.cleanup();
    app.update();

    assert!(app.get_sub_app(RenderApp).is_none());
    assert!(
        app.world()
            .resource::<Assets<Font>>()
            .get(AssetId::default())
            .is_some(),
        "TextPlugin must load Bevy's built-in FiraMono font"
    );

    let reference_thrust = super::systems::reference_thrust_brief();
    let path = crate::navigation::brief();
    for (stage, brief) in [
        ("waiting telemetry", crate::telemetry::brief()),
        (
            "numeric flight text",
            "Speed 123.45 m/s | radial climb -12.34 m/s\nTerrain-center 98765.4 m | thrust 0.50 m/s²".into(),
        ),
    ] {
        // Change only production readout strings. Measurement and geometry stay engine-owned.
        let world = app.world_mut();
        for (readout, mut text) in world.query::<(&Readout, &mut Text)>().iter_mut(world) {
            match readout {
                Readout::TelemetryBrief => text.0.clone_from(&brief),
                Readout::ReferenceThrust => text.0.clone_from(&reference_thrust),
                Readout::PathBrief => text.0.clone_from(&path),
                _ => {}
            }
        }
        // Allow font registration, camera propagation and changed-text layout to settle.
        for _ in 0..3 {
            app.update();
        }
        assert_chrome(&mut app, camera, stage, window_scale * ui_scale);
    }
    check_platform_geometry(&mut app, window_scale);
    check_controls_shortcut(&mut app, camera);
    crate::controls::reset_for_tests();
}

fn check_controls_shortcut(app: &mut App, camera: Entity) {
    use bevy::input_focus::FocusCause;
    app.add_systems(
        PreUpdate,
        super::systems::capture_binding_input
            .after(bevy::input::InputSystems)
            .before(InputFocusSystems::Dispatch),
    );
    app.add_systems(
        PostUpdate,
        super::systems::scroll_focused_control_into_view.after(bevy::ui::UiSystems::PostLayout),
    );
    app.add_systems(
        Update,
        (
            super::systems::interactions,
            super::systems::update_layout,
            super::systems::update_overlays,
            super::systems::update_buttons,
            super::systems::update_readouts,
        )
            .chain(),
    );
    super::diagnostics::install(app);
    crate::camera::set_input_active(true, false);
    let window = app
        .world_mut()
        .query_filtered::<Entity, With<bevy::window::PrimaryWindow>>()
        .single(app.world())
        .unwrap();
    let run = app
        .world_mut()
        .query::<(Entity, &HudAction)>()
        .iter(app.world())
        .find_map(|(entity, action)| matches!(action, HudAction::TogglePlayback).then_some(entity))
        .unwrap();
    app.world_mut()
        .resource_mut::<InputFocus>()
        .set(run, FocusCause::Navigated);
    tap_key(app, window, KeyCode::KeyH, Key::Character("h".into()));
    for _ in 0..3 {
        app.update();
    }
    assert!(
        app.world().resource::<HudState>().panel == super::model::SidePanel::Controls,
        "H must open controls even with a HUD button focused"
    );
    let viewport = Rect::from_corners(
        Vec2::ZERO,
        app.world()
            .get::<Camera>(camera)
            .unwrap()
            .physical_viewport_size()
            .unwrap()
            .as_vec2(),
    );
    let panel = app
        .world_mut()
        .query::<(Entity, &super::model::Overlay)>()
        .iter(app.world())
        .find_map(|(entity, overlay)| {
            matches!(overlay, super::model::Overlay::Controls).then_some(entity)
        })
        .unwrap();
    assert_in_viewport(app.world(), panel, viewport, "controls H", "controls panel");
    let upright = app
        .world_mut()
        .query::<(Entity, &HudAction)>()
        .iter(app.world())
        .find_map(|(entity, action)| matches!(action, HudAction::DebugUpright).then_some(entity))
        .expect("H must expose the native upright test-reset button");
    assert!(
        app.world()
            .get::<bevy::ui_widgets::Button>(upright)
            .is_some()
    );
    assert_eq!(
        app.world()
            .get::<bevy::input_focus::tab_navigation::TabIndex>(upright)
            .unwrap()
            .0,
        0
    );
    assert_eq!(
        app.world().get::<AccessibleLabel>(upright).unwrap().0,
        "Upright lander · test reset"
    );
    let mut ancestor = upright;
    while ancestor != panel {
        ancestor = app
            .world()
            .get::<ChildOf>(ancestor)
            .expect("upright belongs to the H controls panel")
            .parent();
    }
    let upright_rect = assert_in_viewport(
        app.world(),
        upright,
        viewport,
        "controls H",
        "upright test reset",
    );
    let upright_label = app
        .world()
        .get::<Children>(upright)
        .unwrap()
        .iter()
        .find_map(|child| app.world().get::<TextLayoutInfo>(child))
        .expect("the native Upright action has a shaped text label");
    let upright_content = app
        .world()
        .get::<ComputedNode>(upright)
        .unwrap()
        .content_box()
        .size();
    assert!(
        upright_label.size.cmple(upright_content + Vec2::ONE).all(),
        "the complete Upright label must fit its button: {:?} in {upright_content:?}",
        upright_label.size
    );
    if let Some(clip) = app.world().get::<CalculatedClip>(upright) {
        for point in [
            upright_rect.min,
            Vec2::new(upright_rect.max.x, upright_rect.min.y),
            upright_rect.max,
            Vec2::new(upright_rect.min.x, upright_rect.max.y),
        ] {
            assert!(
                clip.contains_point(point),
                "the complete Upright action must fit before scrolling: rect {upright_rect:?}, corner {point:?}, clip {clip:?}"
            );
        }
    }
    let strips: Vec<_> = app
        .world_mut()
        .query::<(Entity, &super::model::LayoutPart)>()
        .iter(app.world())
        .filter_map(|(entity, part)| {
            matches!(
                part,
                super::model::LayoutPart::TopRow | super::model::LayoutPart::BottomRow
            )
            .then_some(entity)
        })
        .collect();
    for strip in strips {
        let rect = assert_in_viewport(app.world(), strip, viewport, "controls H", "HUD strip");
        let overlap = upright_rect.max.min(rect.max) - upright_rect.min.max(rect.min);
        assert!(
            overlap.x <= 0.0 || overlap.y <= 0.0,
            "Upright must not overlap persistent HUD controls"
        );
    }
    let notebook = app
        .world_mut()
        .query::<(Entity, &HudAction)>()
        .iter(app.world())
        .find_map(|(entity, action)| matches!(action, HudAction::Notebook).then_some(entity))
        .unwrap();
    assert_in_viewport(
        app.world(),
        notebook,
        viewport,
        "controls H",
        "Open notebook button",
    );
    let notebook_point = app
        .world()
        .get::<UiGlobalTransform>(notebook)
        .unwrap()
        .transform_point2(Vec2::ZERO)
        * app
            .world()
            .get::<ComputedNode>(notebook)
            .unwrap()
            .inverse_scale_factor()
        * app.world().resource::<UiScale>().0;
    assert!(
        super::notebook_button_at(notebook_point.x, notebook_point.y),
        "Open notebook remains an active native target while controls are open"
    );

    // Lower settings use the production focus-to-ScrollIntoView observer, not
    // test-owned node offsets or scroll positions. Returning to Upright must
    // also recover a visible action after scrolling through the bindings.
    let restore = app
        .world_mut()
        .query::<(Entity, &HudAction)>()
        .iter(app.world())
        .find_map(|(entity, action)| matches!(action, HudAction::RestoreBindings).then_some(entity))
        .unwrap();
    assert!(
        app.world()
            .get::<bevy::ui_widgets::ScrollArea>(panel)
            .is_some()
    );
    for (entity, label) in [
        (restore, "Restore default keys"),
        (upright, "Upright after scrolling"),
    ] {
        app.world_mut()
            .resource_mut::<InputFocus>()
            .set(entity, FocusCause::Navigated);
        for _ in 0..3 {
            app.update();
        }
        assert_in_viewport(
            app.world(),
            entity,
            viewport,
            "controls focus scrolling",
            label,
        );
    }
    let status: serde_json::Value = serde_json::from_str(&super::status()).unwrap();
    assert!(status["samples"].as_u64().unwrap() > 0);
    assert!(status["native_nodes"].as_u64().unwrap() > 0);
    let root = status["elements"]
        .as_array()
        .unwrap()
        .iter()
        .find(|element| element["role"] == "root")
        .unwrap();
    assert_eq!(root["displayed"], true);
    assert_eq!(root["inherited_visible"], true);
    assert_eq!(root["target_camera_active"], true);
    assert_eq!(root["targets_window"], true);
    let upright_report = status["elements"]
        .as_array()
        .unwrap()
        .iter()
        .find(|element| element["role"] == "debug_upright")
        .unwrap();
    assert_eq!(upright_report["displayed"], true);
    assert_eq!(upright_report["inherited_visible"], true);
    for axis in 0..2 {
        let size = status["viewport_css"][axis].as_f64().unwrap();
        assert!((root["rect_css"][axis + 2].as_f64().unwrap() - size).abs() <= 1.0);
    }
    let settings = app
        .world_mut()
        .query::<(Entity, &Readout)>()
        .iter(app.world())
        .find_map(|(entity, readout)| matches!(readout, Readout::InputSettings).then_some(entity))
        .unwrap();
    let text = app.world().get::<Text>(settings).unwrap();
    assert!(text.0.contains("Held keys:"));
    assert!(
        text.0
            .contains("Controller owner: external Python notebook or manual keys")
    );
    assert!(text.0.contains("Space and E: reference limit"));
    assert!(
        !app.world()
            .get::<TextLayoutInfo>(settings)
            .unwrap()
            .glyphs
            .is_empty()
    );
    tap_key(app, window, KeyCode::KeyH, Key::Character("h".into()));
    assert_eq!(
        app.world().resource::<HudState>().panel,
        super::model::SidePanel::Closed
    );
    assert_eq!(
        app.world().get::<Node>(panel).unwrap().display,
        Display::None
    );

    app.world_mut().resource_mut::<InputFocus>().clear();
    tap_key(app, window, KeyCode::KeyI, Key::Character("i".into()));
    assert_eq!(
        app.world().resource::<HudState>().panel,
        super::model::SidePanel::Inspector
    );
    tap_key(app, window, KeyCode::KeyH, Key::Character("h".into()));
    assert_eq!(
        app.world().resource::<HudState>().panel,
        super::model::SidePanel::Controls
    );
    for (overlay, node) in app
        .world_mut()
        .query::<(&super::model::Overlay, &Node)>()
        .iter(app.world())
    {
        if matches!(
            overlay,
            super::model::Overlay::Details | super::model::Overlay::Inspector
        ) {
            assert_eq!(
                node.display,
                Display::None,
                "Controls must replace Details and Inspector"
            );
        }
    }
    tap_key(app, window, KeyCode::Escape, Key::Escape);
    assert_eq!(
        app.world().resource::<HudState>().panel,
        super::model::SidePanel::Closed
    );
    check_binding_capture(app, window, run);
}

fn check_binding_capture(app: &mut App, window: Entity, run: Entity) {
    use crate::controls::{self, Action};
    use bevy::input_focus::FocusCause;

    tap_key(app, window, KeyCode::KeyH, Key::Character("h".into()));
    let front = app
        .world_mut()
        .query::<(Entity, &HudAction)>()
        .iter(app.world())
        .find_map(|(entity, action)| {
            matches!(action, HudAction::ChangeBinding(Action::Front)).then_some(entity)
        })
        .expect("the production H panel has a native Front binding button");
    app.world_mut()
        .resource_mut::<InputFocus>()
        .set(front, FocusCause::Navigated);
    tap_key(app, window, KeyCode::Enter, Key::Enter);
    assert!(
        controls::binding_capture_active(),
        "Enter on the native binding button begins capture"
    );
    let camera = app
        .world_mut()
        .query_filtered::<&Camera, With<crate::camera::SimulationCamera>>()
        .single(app.world())
        .unwrap();
    let viewport = Rect::from_corners(
        Vec2::ZERO,
        camera.physical_viewport_size().unwrap().as_vec2(),
    );
    assert_in_viewport(
        app.world(),
        front,
        viewport,
        "binding focus",
        "Front binding button",
    );
    let queued_before_capture = crate::session::queued_actions_for_tests();

    // Native Activate must not toggle playback, close H, reset or open a platform action.
    app.world_mut()
        .resource_mut::<InputFocus>()
        .set(run, FocusCause::Navigated);
    tap_key(app, window, KeyCode::Space, Key::Space);
    assert!(controls::binding_capture_active());
    assert!(controls::capture_message().contains("already controls Lift"));
    let notebook = app
        .world_mut()
        .query::<(Entity, &HudAction)>()
        .iter(app.world())
        .find_map(|(entity, action)| matches!(action, HudAction::Notebook).then_some(entity))
        .unwrap();
    app.world_mut()
        .resource_mut::<InputFocus>()
        .set(notebook, FocusCause::Navigated);
    tap_key(app, window, KeyCode::Enter, Key::Enter);
    assert!(controls::binding_capture_active());
    assert!(controls::capture_message().contains("reserved"));
    tap_key(app, window, KeyCode::KeyH, Key::Character("h".into()));
    assert_eq!(
        app.world().resource::<HudState>().panel,
        super::model::SidePanel::Controls
    );
    assert!(controls::capture_message().contains("reserved"));
    tap_key(app, window, KeyCode::KeyA, Key::Character("a".into()));
    assert!(controls::capture_message().contains("already controls Left"));
    tap_key(app, window, KeyCode::KeyB, Key::Character("b".into()));
    assert!(!controls::binding_capture_active());
    assert_eq!(controls::key_label(Action::Front), "B");
    for _ in 0..3 {
        app.update();
    }
    let label = app
        .world()
        .get::<Children>(front)
        .unwrap()
        .iter()
        .find_map(|child| app.world().get::<Text>(child))
        .unwrap();
    assert_eq!(label.0, "Front: B · change");
    assert_eq!(
        app.world()
            .get::<bevy::a11y::AccessibilityNode>(front)
            .unwrap()
            .label(),
        Some("Front: B · change")
    );
    assert!(!app.world().resource::<PlaybackState>().running);
    assert_eq!(
        crate::session::queued_actions_for_tests(),
        queued_before_capture,
        "capture must not queue native Run or notebook activation"
    );

    app.world_mut()
        .resource_mut::<InputFocus>()
        .set(front, FocusCause::Navigated);
    tap_key(app, window, KeyCode::Enter, Key::Enter);
    assert!(controls::binding_capture_active());
    tap_key(app, window, KeyCode::Escape, Key::Escape);
    assert!(!controls::binding_capture_active());
    assert_eq!(controls::key_label(Action::Front), "B");
    assert_eq!(
        app.world().resource::<HudState>().panel,
        super::model::SidePanel::Controls
    );
    assert!(
        !controls::manual_input_blocked(),
        "the UI observes release without a physics runtime"
    );
    app.update();
    assert!(
        app.world()
            .get::<bevy::ui::InteractionDisabled>(front)
            .is_none()
    );

    let lift = app
        .world_mut()
        .query::<(Entity, &HudAction)>()
        .iter(app.world())
        .find_map(|(entity, action)| {
            matches!(action, HudAction::ChangeBinding(Action::Lift)).then_some(entity)
        })
        .expect("the production H panel has a native Lift binding button");
    app.world_mut()
        .resource_mut::<InputFocus>()
        .set(lift, FocusCause::Navigated);
    tap_key(app, window, KeyCode::Enter, Key::Enter);
    assert!(controls::binding_capture_active());
    key_frame(
        app,
        window,
        KeyCode::Space,
        Key::Space,
        ButtonState::Pressed,
    );
    assert!(!controls::binding_capture_active());
    assert!(controls::manual_input_blocked());
    app.update();
    assert!(
        controls::binding_actions_blocked(),
        "held captured Space must not rearm"
    );
    key_frame(
        app,
        window,
        KeyCode::Space,
        Key::Space,
        ButtonState::Released,
    );
    assert!(
        !controls::binding_capture_active(),
        "captured Space key-up cannot recapture on its focused binding button"
    );
    assert!(!controls::manual_input_blocked());
    assert!(
        controls::binding_actions_blocked(),
        "release consumes the rest of its frame"
    );
    assert!(
        app.world()
            .get::<bevy::ui::InteractionDisabled>(lift)
            .is_some()
    );
    assert_eq!(
        crate::session::queued_actions_for_tests(),
        queued_before_capture
    );
    app.update();
    assert!(!controls::binding_actions_blocked());
    assert!(
        app.world()
            .get::<bevy::ui::InteractionDisabled>(lift)
            .is_none()
    );
    tap_key(app, window, KeyCode::Enter, Key::Enter);
    assert!(
        controls::binding_capture_active(),
        "a fresh Enter still activates the native control after release"
    );
    tap_key(app, window, KeyCode::Escape, Key::Escape);
    assert!(!controls::binding_capture_active());

    app.update();
    let restore = app
        .world_mut()
        .query::<(Entity, &HudAction)>()
        .iter(app.world())
        .find_map(|(entity, action)| matches!(action, HudAction::RestoreBindings).then_some(entity))
        .expect("the production H panel has a native Restore default keys button");
    app.world_mut()
        .resource_mut::<InputFocus>()
        .set(restore, FocusCause::Navigated);
    tap_key(app, window, KeyCode::Enter, Key::Enter);
    assert_eq!(controls::key_label(Action::Front), "W");
    assert!(!controls::manual_input_blocked());
    assert_eq!(
        crate::session::queued_actions_for_tests(),
        queued_before_capture
    );
    app.update();

    // Revalidate authority before acting, even when the previous frame still
    // showed an enabled native button.
    let upright = app
        .world_mut()
        .query::<(Entity, &HudAction)>()
        .iter(app.world())
        .find_map(|(entity, action)| matches!(action, HudAction::DebugUpright).then_some(entity))
        .unwrap();
    assert!(
        app.world()
            .get::<bevy::ui::InteractionDisabled>(upright)
            .is_none()
    );
    app.world_mut()
        .resource_mut::<InputFocus>()
        .set(upright, FocusCause::Navigated);
    app.world_mut().resource_mut::<PlaybackState>().can_control = false;
    tap_key(app, window, KeyCode::Enter, Key::Enter);
    assert_eq!(
        crate::session::queued_actions_for_tests(),
        queued_before_capture,
        "stale enabled visuals cannot authorize an upright reset"
    );
    app.world_mut().resource_mut::<PlaybackState>().can_control = true;
    app.update();
    tap_key(app, window, KeyCode::Escape, Key::Escape);

    app.world_mut().resource_mut::<InputFocus>().clear();
    for (key_code, logical_key) in [
        (KeyCode::KeyR, Key::Character("r".into())),
        (KeyCode::KeyI, Key::Character("i".into())),
    ] {
        queue_key(app, window, key_code, logical_key, ButtonState::Pressed);
    }
    app.update();
    assert!(app.world().resource::<HudState>().reset_confirm_open);
    assert_eq!(
        app.world().resource::<HudState>().panel,
        super::model::SidePanel::Closed,
        "a later shortcut cannot act through a modal opened in the same frame"
    );
    key_frame(
        app,
        window,
        KeyCode::KeyR,
        Key::Character("r".into()),
        ButtonState::Released,
    );
    key_frame(
        app,
        window,
        KeyCode::KeyI,
        Key::Character("i".into()),
        ButtonState::Released,
    );
    tap_key(app, window, KeyCode::Escape, Key::Escape);
    assert!(!app.world().resource::<HudState>().reset_confirm_open);
}

fn check_platform_geometry(app: &mut App, window_scale: f32) {
    use super::model::PlatformButton;
    use bevy::{input_focus::FocusCause, ui::InteractionDisabled};

    fn target_at(button: PlatformButton, point: Vec2) -> bool {
        match button {
            PlatformButton::Fullscreen => super::fullscreen_button_at(point.x, point.y),
            PlatformButton::Notebook => super::notebook_button_at(point.x, point.y),
        }
    }
    fn focused(button: PlatformButton) -> bool {
        match button {
            PlatformButton::Fullscreen => super::fullscreen_button_focused(),
            PlatformButton::Notebook => super::notebook_button_focused(),
        }
    }
    fn center(app: &App, entity: Entity, window_scale: f32) -> Vec2 {
        app.world()
            .get::<UiGlobalTransform>(entity)
            .unwrap()
            .transform_point2(Vec2::ZERO)
            / window_scale
    }
    let buttons: Vec<_> = app
        .world_mut()
        .query::<(Entity, &PlatformButton)>()
        .iter(app.world())
        .map(|(entity, button)| (entity, *button))
        .collect();
    assert_eq!(
        buttons.len(),
        2,
        "one visible native notebook and fullscreen button"
    );
    for (entity, button) in buttons {
        if button == PlatformButton::Notebook {
            assert_eq!(
                app.world().get::<AccessibleLabel>(entity).unwrap().0,
                "Open notebook · new tab"
            );
        }
        app.world_mut()
            .resource_mut::<InputFocus>()
            .set(entity, FocusCause::Navigated);
        app.update();
        let point = center(app, entity, window_scale);
        assert!(target_at(button, point));
        assert!(focused(button));
        assert!(!target_at(button, Vec2::splat(f32::NAN)));
        assert!(!target_at(button, Vec2::splat(-100.0)));

        app.world_mut()
            .entity_mut(entity)
            .insert(InteractionDisabled);
        app.update();
        assert!(
            !target_at(button, point),
            "disabled native geometry cannot activate"
        );
        assert!(!focused(button));
        app.world_mut()
            .entity_mut(entity)
            .remove::<InteractionDisabled>();
        app.update();
        assert!(target_at(button, center(app, entity, window_scale)));

        app.world_mut()
            .entity_mut(entity)
            .insert(Visibility::Hidden);
        app.update();
        assert!(!target_at(button, point));
        assert!(!focused(button));
        app.world_mut()
            .entity_mut(entity)
            .insert(Visibility::Inherited);
        app.update();

        app.world_mut().get_mut::<Node>(entity).unwrap().display = Display::None;
        app.update();
        assert!(!target_at(button, point));
        assert!(!focused(button));
        app.world_mut().get_mut::<Node>(entity).unwrap().display = Display::Flex;
        for _ in 0..3 {
            app.update();
        }
        assert!(target_at(button, center(app, entity, window_scale)));

        let parent = app.world().get::<ChildOf>(entity).unwrap().parent();
        let original = app.world().get::<Node>(parent).unwrap().clone();
        {
            let mut node = app.world_mut().get_mut::<Node>(parent).unwrap();
            node.width = px(1);
            node.height = px(1);
            node.overflow = Overflow::clip();
        }
        for _ in 0..3 {
            app.update();
        }
        assert!(
            !target_at(button, center(app, entity, window_scale)),
            "inherited clipping cannot leave an active target"
        );
        assert!(!focused(button));
        app.world_mut().entity_mut(parent).insert(original);
        for _ in 0..3 {
            app.update();
        }
        assert!(target_at(button, center(app, entity, window_scale)));

        app.world_mut()
            .resource_mut::<HudState>()
            .reset_confirm_open = true;
        app.update();
        assert!(!target_at(button, point));
        assert!(!focused(button));
        app.world_mut()
            .resource_mut::<HudState>()
            .reset_confirm_open = false;
        app.update();

        crate::controls::begin_capture(crate::controls::Action::Front);
        assert!(
            !target_at(button, point),
            "capture invalidates cached targets immediately"
        );
        assert!(!focused(button));
        app.update();
        crate::controls::reset_for_tests();
        app.update();
        assert!(target_at(button, center(app, entity, window_scale)));

        app.world_mut()
            .entity_mut(entity)
            .remove::<PlatformButton>();
        app.update();
        assert!(
            !target_at(button, point),
            "removed markers cannot leave stale targets"
        );
        assert!(!focused(button));
        app.world_mut().entity_mut(entity).insert(button);
        app.update();
    }
    app.world_mut().resource_mut::<InputFocus>().clear();
}

fn tap_key(app: &mut App, window: Entity, key_code: KeyCode, logical_key: Key) {
    for state in [ButtonState::Pressed, ButtonState::Released] {
        key_frame(app, window, key_code, logical_key.clone(), state);
    }
}

fn key_frame(
    app: &mut App,
    window: Entity,
    key_code: KeyCode,
    logical_key: Key,
    state: ButtonState,
) {
    queue_key(app, window, key_code, logical_key, state);
    app.update();
}

fn queue_key(
    app: &mut App,
    window: Entity,
    key_code: KeyCode,
    logical_key: Key,
    state: ButtonState,
) {
    app.world_mut().write_message(KeyboardInput {
        key_code,
        logical_key,
        state,
        text: None,
        repeat: false,
        window,
    });
}

fn assert_chrome(app: &mut App, camera: Entity, stage: &str, scale: f32) {
    let world = app.world_mut();
    let viewport_size = world
        .get::<Camera>(camera)
        .unwrap()
        .physical_viewport_size()
        .expect("camera_system must resolve the Window render target");
    let viewport = Rect::from_corners(Vec2::ZERO, viewport_size.as_vec2());
    let root = world
        .query_filtered::<Entity, (With<Node>, Without<ChildOf>)>()
        .single(world)
        .expect("one production HUD root");
    assert_in_viewport(world, root, viewport, stage, "HUD root");
    assert_eq!(
        world
            .get::<UiTargetCamera>(root)
            .map(UiTargetCamera::entity),
        Some(camera),
        "view::setup must bind the HUD to the simulation camera"
    );
    let target = world.get::<ComputedUiRenderTargetInfo>(root).unwrap();
    assert_eq!(target.physical_size(), viewport_size);
    assert_eq!(target.scale_factor(), scale);

    let readouts: Vec<_> = world
        .query::<(Entity, &Readout)>()
        .iter(world)
        .filter_map(|(entity, readout)| {
            let label = match readout {
                Readout::Summary => "summary",
                Readout::TelemetryBrief => "telemetry",
                Readout::Playback => "Run label",
                Readout::DetailsToggle => "Details label",
                Readout::Notebook => "Open notebook label",
                _ => return None,
            };
            Some((entity, label))
        })
        .collect();
    assert_eq!(readouts.len(), 5, "critical readouts must exist");
    for (entity, label) in readouts {
        let rect = assert_in_viewport(world, entity, viewport, stage, label);
        assert_eq!(
            world.get::<ComputedUiTargetCamera>(entity).unwrap().get(),
            Some(camera),
            "{stage}: {label} must inherit the HUD camera"
        );
        let text_layout = world.get::<TextLayoutInfo>(entity).unwrap();
        assert!(
            !text_layout.glyphs.is_empty()
                && text_layout.size.is_finite()
                && text_layout.size.cmpgt(Vec2::ZERO).all(),
            "{stage}: {label} must have real shaped glyphs: {text_layout:?}"
        );
        let text_box = world
            .get::<ComputedNode>(entity)
            .unwrap()
            .content_box()
            .size();
        assert!(
            text_layout.size.cmple(text_box + Vec2::ONE).all(),
            "{stage}: {label} shaped text {:?} overflows its computed box {text_box:?}",
            text_layout.size,
        );
        eprintln!(
            "{stage}: viewport={viewport_size:?} scale={scale} {label} rect={rect:?} glyphs={} text_size={:?}",
            text_layout.glyphs.len(),
            text_layout.size,
        );
    }

    let buttons: Vec<_> = world
        .query::<(Entity, &HudAction)>()
        .iter(world)
        .filter_map(|(entity, action)| match action {
            HudAction::TogglePlayback => Some((entity, "Run button")),
            HudAction::ToggleDetails => Some((entity, "Details button")),
            HudAction::Notebook => Some((entity, "Open notebook button")),
            _ => None,
        })
        .collect();
    assert_eq!(buttons.len(), 3, "critical buttons must exist");
    let button_rects: Vec<_> = buttons
        .into_iter()
        .map(|(entity, label)| assert_in_viewport(world, entity, viewport, stage, label))
        .collect();
    for (index, rect) in button_rects.iter().enumerate() {
        for other in &button_rects[..index] {
            let overlap = rect.max.min(other.max) - rect.min.max(other.min);
            assert!(
                overlap.x <= 0.0 || overlap.y <= 0.0,
                "{stage}: critical buttons must not overlap: {button_rects:?}"
            );
        }
    }

    let backgrounds: Vec<_> = world
        .query_filtered::<(Entity, &ComputedNode, &InheritedVisibility), With<BackgroundColor>>()
        .iter(world)
        .filter(|(_, node, visibility)| visibility.get() && !node.is_empty())
        .map(|(entity, _, _)| entity)
        .collect();
    assert!(!backgrounds.is_empty(), "visible HUD chrome must exist");
    for entity in backgrounds {
        assert_in_viewport(world, entity, viewport, stage, "visible HUD background");
    }
}

fn assert_in_viewport(
    world: &World,
    entity: Entity,
    viewport: Rect,
    stage: &str,
    label: &str,
) -> Rect {
    let node = world.get::<ComputedNode>(entity).unwrap();
    let transform = world.get::<UiGlobalTransform>(entity).unwrap();
    let bounds = node.border_box();
    let corners = [
        bounds.min,
        Vec2::new(bounds.max.x, bounds.min.y),
        bounds.max,
        Vec2::new(bounds.min.x, bounds.max.y),
    ]
    .map(|point| transform.transform_point2(point));
    let rect = Rect {
        min: corners.into_iter().fold(Vec2::INFINITY, Vec2::min),
        max: corners.into_iter().fold(Vec2::NEG_INFINITY, Vec2::max),
    };
    assert!(
        node.size().is_finite()
            && node.size().cmpgt(Vec2::ZERO).all()
            && rect.min.is_finite()
            && rect.max.is_finite(),
        "{stage}: {label} {entity:?} has no finite layout: {rect:?}, {node:?}"
    );
    assert!(
        viewport.inflate(1.0).contains(rect.min) && viewport.inflate(1.0).contains(rect.max),
        "{stage}: {label} {entity:?} is outside {viewport:?}: {rect:?}"
    );
    assert!(
        world.get::<InheritedVisibility>(entity).unwrap().get(),
        "{stage}: {label} {entity:?} is hidden"
    );
    assert!(
        world
            .get::<CalculatedClip>(entity)
            .is_none_or(|clip| clip.contains_point(rect.center())),
        "{stage}: {label} {entity:?} is clipped at {rect:?}"
    );
    rect
}
