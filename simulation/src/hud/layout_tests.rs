//! CPU-only checks of the production HUD, including FiraMono shaping and Taffy layout.
//! These do not exercise render extraction or prove that a GPU draws the HUD.

#![cfg(not(target_arch = "wasm32"))]

use bevy::{
    asset::{AssetApp, AssetEventSystems, AssetId, AssetPlugin},
    camera::{CameraPlugin, CameraUpdateSystems, RenderTarget, visibility::update_frusta},
    image::ImagePlugin,
    input::InputPlugin,
    input_focus::InputFocus,
    mesh::MeshPlugin,
    picking::{InteractionPlugin, PickingPlugin},
    prelude::*,
    render::{RenderApp, camera::camera_system, texture::ManualTextureViews},
    text::{TextLayoutInfo, TextPlugin},
    ui::UiPlugin,
    window::{ExitCondition, WindowRef, WindowResolution},
};

use super::model::{HudAction, HudState, Readout};
use bevy::input::{
    ButtonState,
    keyboard::{Key, KeyboardInput},
};

#[test]
fn desktop_hud_survives_telemetry_and_profile_text() {
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

    let settings = crate::scenario::settings();
    let profile = format!(
        "Space: PRACTICE BOOST {:.1} m/s² | {:.0}x reference\nNot competition thrust. Reset before comparing profiles.",
        settings.practice.space_lift_accel_mps2,
        settings.practice.space_lift_accel_mps2 / settings.lander.max_accel_mps2,
    );
    let path = crate::navigation::brief();
    for (stage, brief) in [
        ("waiting telemetry", crate::telemetry::brief()),
        (
            "numeric flight text",
            "Speed 123.45 m/s | radial climb -12.34 m/s\nTerrain-center 98765.4 m | thrust 2.00 m/s²".into(),
        ),
    ] {
        // Change only production readout strings. Measurement and geometry stay engine-owned.
        let world = app.world_mut();
        for (readout, mut text) in world.query::<(&Readout, &mut Text)>().iter_mut(world) {
            match readout {
                Readout::TelemetryBrief => text.0.clone_from(&brief),
                Readout::ThrustProfile => text.0.clone_from(&profile),
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
    check_controls_shortcut(&mut app, camera);
}

fn check_controls_shortcut(app: &mut App, camera: Entity) {
    use bevy::input_focus::FocusCause;
    app.add_systems(
        Update,
        (
            super::systems::interactions,
            super::systems::update_layout,
            super::systems::update_overlays,
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
    for axis in 0..2 {
        let size = status["viewport_css"][axis].as_f64().unwrap();
        assert!((root["rect_css"][axis + 2].as_f64().unwrap() - size).abs() <= 1.0);
    }
    let settings = app
        .world_mut()
        .query::<(Entity, &Readout)>()
        .iter(app.world())
        .find_map(|(entity, readout)| {
            matches!(readout, Readout::ControllerSettings).then_some(entity)
        })
        .unwrap();
    let text = app.world().get::<Text>(settings).unwrap();
    assert!(text.0.contains("Held keys:"));
    assert!(text.0.contains("Controller:"));
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
}

fn tap_key(app: &mut App, window: Entity, key_code: KeyCode, logical_key: Key) {
    for state in [ButtonState::Pressed, ButtonState::Released] {
        app.world_mut().write_message(KeyboardInput {
            key_code,
            logical_key: logical_key.clone(),
            state,
            text: None,
            repeat: false,
            window,
        });
        app.update();
    }
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
                _ => return None,
            };
            Some((entity, label))
        })
        .collect();
    assert_eq!(readouts.len(), 4, "critical readouts must exist");
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
            _ => None,
        })
        .collect();
    assert_eq!(buttons.len(), 2, "critical buttons must exist");
    let button_rects: Vec<_> = buttons
        .into_iter()
        .map(|(entity, label)| assert_in_viewport(world, entity, viewport, stage, label))
        .collect();
    let overlap =
        button_rects[0].max.min(button_rects[1].max) - button_rects[0].min.max(button_rects[1].min);
    assert!(
        overlap.x <= 0.0 || overlap.y <= 0.0,
        "{stage}: Run and Details must not overlap: {button_rects:?}"
    );

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
