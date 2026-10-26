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
use crate::telemetry::{FlightMetric, Instrument, InstrumentGroup};
use bevy::input::{
    ButtonState,
    keyboard::{Key, KeyboardInput},
};

#[test]
fn desktop_hud_keeps_the_six_flight_instruments_glanceable() {
    check_hud(1280, 800, 1.0, 1.0);
}

#[test]
fn narrow_hud_keeps_actions_and_the_flight_strip_in_view() {
    check_hud(320, 640, 1.0, 1.0);
}

#[test]
fn short_hud_keeps_actions_and_the_flight_strip_in_view() {
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

#[test]
fn hidpi_and_enlarged_ui_keep_real_native_targets_reachable() {
    check_hud(1280, 800, 2.0, 2.0);
}

#[test]
fn fullscreen_shortcut_tracks_native_reset_focus_and_editor_release() {
    use bevy::{input_focus::FocusCause, text::EditableText, ui_widgets::TextInput};

    let (mut app, window) = fullscreen_shortcut_app();
    let unchanged = crate::snapshot();
    let queued = crate::session::queued_actions_for_tests();
    assert!(super::fullscreen_shortcut_allowed());
    crate::camera::set_input_active(false, false);
    assert!(!super::fullscreen_shortcut_allowed());
    crate::camera::set_input_active(true, false);
    assert!(super::fullscreen_shortcut_allowed());

    let before: serde_json::Value = serde_json::from_str(&super::status()).unwrap();
    for (code, text) in [(KeyCode::KeyR, "r"), (KeyCode::KeyX, "x")] {
        queue_key(
            &mut app,
            window,
            code,
            Key::Character(text.into()),
            ButtonState::Pressed,
        );
    }
    app.update();
    assert!(app.world().resource::<HudState>().reset_confirm_open);
    assert!(!super::fullscreen_shortcut_allowed());
    let after: serde_json::Value = serde_json::from_str(&super::status()).unwrap();
    assert!(after["samples"].as_u64().unwrap() > before["samples"].as_u64().unwrap());
    assert_eq!(crate::session::queued_actions_for_tests(), queued);
    for (code, text) in [(KeyCode::KeyR, "r"), (KeyCode::KeyX, "x")] {
        key_frame(
            &mut app,
            window,
            code,
            Key::Character(text.into()),
            ButtonState::Released,
        );
    }
    app.world_mut().resource_mut::<InputFocus>().clear();
    app.update();
    assert!(
        !super::fullscreen_shortcut_allowed(),
        "the reset modal blocks X even without a focused modal button"
    );
    tap_key(&mut app, window, KeyCode::Escape, Key::Escape);
    app.update();
    assert!(!app.world().resource::<HudState>().reset_confirm_open);
    assert!(super::fullscreen_shortcut_allowed());

    let run = app
        .world_mut()
        .query::<(Entity, &HudAction)>()
        .iter(app.world())
        .find_map(|(entity, action)| (*action == HudAction::TogglePlayback).then_some(entity))
        .unwrap();
    app.world_mut()
        .resource_mut::<InputFocus>()
        .set(run, FocusCause::Navigated);
    app.update();
    assert!(
        !super::fullscreen_shortcut_allowed(),
        "native buttons own X just as they do in HUD keyboard routing"
    );
    app.world_mut().resource_mut::<InputFocus>().clear();
    app.world_mut().resource_mut::<HudState>().panel = super::model::SidePanel::Ship;
    for _ in 0..3 {
        app.update();
    }
    assert!(super::fullscreen_shortcut_allowed());
    let mass = app
        .world_mut()
        .query::<(Entity, &super::EditorField)>()
        .iter(app.world())
        .find_map(|(entity, field)| (*field == super::EditorField::Mass).then_some(entity))
        .unwrap();
    assert!(app.world().get::<TextInput>(mass).is_some());
    assert!(app.world().get::<EditableText>(mass).is_some());
    app.world_mut()
        .resource_mut::<InputFocus>()
        .set(mass, FocusCause::Navigated);
    app.update();
    assert!(!super::fullscreen_shortcut_allowed());
    key_frame(
        &mut app,
        window,
        KeyCode::KeyX,
        Key::Character("x".into()),
        ButtonState::Pressed,
    );
    app.world_mut().resource_mut::<InputFocus>().clear();
    for _ in 0..2 {
        app.update();
        assert!(super::instrument_editing());
        assert!(
            !super::fullscreen_shortcut_allowed(),
            "blurring an editor cannot rearm its still-held key"
        );
    }
    key_frame(
        &mut app,
        window,
        KeyCode::KeyX,
        Key::Character("x".into()),
        ButtonState::Released,
    );
    app.update();
    assert!(!super::instrument_editing());
    assert!(super::fullscreen_shortcut_allowed());
    assert_eq!(crate::session::queued_actions_for_tests(), queued);
    assert!(std::rc::Rc::ptr_eq(&unchanged, &crate::snapshot()));
}

#[test]
fn fullscreen_shortcut_keeps_binding_and_placement_guards_separate_from_buttons() {
    use super::model::{PlatformButton, SidePanel};
    use crate::controls::{self, Action};
    use bevy::input_focus::FocusCause;

    let (mut app, window) = fullscreen_shortcut_app();
    let unchanged = crate::snapshot();
    app.world_mut().resource_mut::<HudState>().panel = SidePanel::Controls;
    app.update();
    assert!(super::fullscreen_shortcut_allowed());
    controls::begin_capture(Action::Front);
    assert!(
        !super::fullscreen_shortcut_allowed(),
        "live capture invalidates an allowed cache before the next frame"
    );
    app.update();
    assert!(!super::fullscreen_shortcut_allowed());
    key_frame(
        &mut app,
        window,
        KeyCode::KeyB,
        Key::Character("b".into()),
        ButtonState::Pressed,
    );
    assert!(!controls::binding_capture_active());
    assert!(controls::binding_actions_blocked());
    app.update();
    assert!(!super::fullscreen_shortcut_allowed());
    key_frame(
        &mut app,
        window,
        KeyCode::KeyB,
        Key::Character("b".into()),
        ButtonState::Released,
    );
    assert!(!controls::manual_input_blocked());
    assert!(controls::binding_actions_blocked());
    assert!(
        !super::fullscreen_shortcut_allowed(),
        "capture's final release frame still owns X without native button focus"
    );
    app.update();
    assert!(super::fullscreen_shortcut_allowed());

    // This HUD fixture processes the real queued-intent gate, not the launchpad
    // adapter's physical placement. No placement state or geometry is invented.
    crate::launchpad::request(crate::launchpad::LaunchpadAction::PlaceOnSurface);
    assert!(!super::fullscreen_shortcut_allowed());
    app.update();
    assert!(!super::fullscreen_shortcut_allowed());
    crate::launchpad::request(crate::launchpad::LaunchpadAction::Cancel);
    app.update();
    assert!(!super::fullscreen_shortcut_allowed());
    crate::launchpad::reset_for_tests();
    app.update();
    assert!(super::fullscreen_shortcut_allowed());

    app.world_mut().resource_mut::<HudState>().panel = SidePanel::Camera;
    let fullscreen = app
        .world_mut()
        .query::<(Entity, &PlatformButton)>()
        .iter(app.world())
        .find_map(|(entity, button)| (*button == PlatformButton::Fullscreen).then_some(entity))
        .unwrap();
    app.world_mut()
        .resource_mut::<InputFocus>()
        .set(fullscreen, FocusCause::Navigated);
    crate::launchpad::request(crate::launchpad::LaunchpadAction::PlaceOnSurface);
    for _ in 0..3 {
        app.update();
    }
    let point = app
        .world()
        .get::<UiGlobalTransform>(fullscreen)
        .unwrap()
        .transform_point2(Vec2::ZERO)
        * app
            .world()
            .get::<ComputedNode>(fullscreen)
            .unwrap()
            .inverse_scale_factor()
        * app.world().resource::<UiScale>().0;
    assert!(!super::fullscreen_shortcut_allowed());
    assert!(
        super::fullscreen_button_focused(),
        "explicit enabled Fullscreen Enter keeps the native action policy"
    );
    assert!(
        super::fullscreen_button_at(point.x, point.y),
        "explicit Fullscreen pointer input keeps its real laid-out target"
    );
    crate::launchpad::reset_for_tests();
    controls::reset_for_tests();
    app.world_mut().resource_mut::<InputFocus>().clear();
    app.update();
    assert!(super::fullscreen_shortcut_allowed());
    assert!(std::rc::Rc::ptr_eq(&unchanged, &crate::snapshot()));
}

#[test]
fn fullscreen_dom_source_keeps_native_permission_and_trusted_event_wiring() {
    // Source-contract checks only. No DOM, browser, fullscreen result or user
    // activation is fabricated by this native test.
    let source = include_str!("../../../worker/web/app-runtime.ts");
    fn section<'a>(source: &'a str, start: &str, end: &str) -> &'a str {
        source
            .split_once(start)
            .unwrap()
            .1
            .split_once(end)
            .unwrap()
            .0
    }
    let permission = section(
        source,
        "function fullscreenShortcutAllowed()",
        "function platformKey(",
    );
    assert!(permission.contains("engine?.fullscreen_shortcut_allowed?.() !== true"));
    assert!(permission.contains("sample <= fullscreenShortcutInputSample"));
    let handler = section(
        source,
        "function platformKey(",
        "function preventViewportScroll(",
    );
    assert!(handler.contains("event.code === \"KeyX\" && fullscreenShortcutAllowed()"));
    assert!(handler.contains("engine.fullscreen_button_focused()"));
    let enter = section(handler, "if (event.code === \"Enter\")", "if (!action)");
    assert!(!enter.contains("fullscreenShortcutAllowed"));
    assert!(handler.contains("if (!event.repeat) trustedAction(action)"));
    assert!(!handler.contains("await"));
    assert!(!handler.contains("requestAnimationFrame"));
    assert!(
        source.contains("window.addEventListener(\"keydown\", platformKey, { capture: true })")
    );
    let pending_input = section(
        source,
        "function rememberNativeShortcutInput(",
        "function fullscreenShortcutAllowed()",
    );
    assert!(pending_input.contains("event.target !== canvas"));
    assert!(pending_input.contains("event.code === \"KeyX\" || event.repeat"));
    assert!(pending_input.contains("fullscreenShortcutInputSample = null"));
    assert!(pending_input.contains("queueMicrotask(() =>"));
    assert!(pending_input.contains("sequence === fullscreenShortcutInputSequence"));
    assert!(pending_input.contains("fullscreenShortcutInputSample = nativeHudInputSample()"));
    assert!(
        source
            .find("window.addEventListener(\"keydown\", rememberNativeShortcutInput")
            .unwrap()
            < source
                .find("window.addEventListener(\"keydown\", event => movementKey")
                .unwrap()
    );
    assert!(source.contains(
        "window.addEventListener(\"keyup\", rememberNativeShortcutInput, { capture: true })"
    ));
    assert!(source.contains("[\"pointerdown\", \"pointerup\", \"pointercancel\"] as const"));
    let trusted_action = section(source, "function trustedAction(", "function pinch(");
    assert!(trusted_action.contains("if (action === \"fullscreen\") void fullscreen()"));
    let fullscreen = section(
        source,
        "async function fullscreen()",
        "function platformButtonAt(",
    );
    assert!(fullscreen.contains("await viewport.requestFullscreen()"));
    assert!(fullscreen.contains("await viewport.webkitRequestFullscreen()"));
    for deferred in [
        "requestAnimationFrame",
        "setTimeout",
        "queueMicrotask",
        "maximize(",
    ] {
        assert!(
            !fullscreen.contains(deferred),
            "fullscreen must not use {deferred}"
        );
    }
}

fn fullscreen_shortcut_app() -> (App, Entity) {
    let (mut app, _) = hud_app(1280, 800, 1.0, 1.0);
    app.add_systems(
        PreUpdate,
        super::systems::capture_binding_input
            .after(bevy::input::InputSystems)
            .before(InputFocusSystems::Dispatch),
    )
    .add_systems(
        Update,
        (super::systems::interactions, super::systems::update_buttons).chain(),
    );
    super::diagnostics::install(&mut app);
    crate::camera::set_input_active(true, false);
    app.update();
    let window = app
        .world_mut()
        .query_filtered::<Entity, With<bevy::window::PrimaryWindow>>()
        .single(app.world())
        .unwrap();
    (app, window)
}

#[test]
fn placement_toolbar_keeps_native_controls_reachable_and_the_pad_center_clear() {
    use super::model::{BUTTON_HEIGHT_PX, LaunchpadControl, LayoutPart, Overlay, SidePanel};
    use bevy::input_focus::FocusCause;

    for (width, height, ui_scale) in [
        (1280, 800, 1.0),
        (880, 640, 1.0),
        (320, 640, 1.0),
        (320, 640, 2.0),
        (800, 360, 2.0),
    ] {
        let (mut app, camera) = hud_app(width, height, 1.0, ui_scale);
        app.add_systems(
            Update,
            (
                super::systems::update_buttons,
                super::systems::update_readouts,
            )
                .chain(),
        );
        crate::camera::set_input_active(true, false);
        let unchanged = crate::snapshot();
        app.world_mut().resource_mut::<HudState>().panel = SidePanel::Launch;
        for _ in 0..4 {
            app.update();
        }
        let actions: Vec<_> = app
            .world_mut()
            .query::<(Entity, &HudAction)>()
            .iter(app.world())
            .map(|(entity, action)| (entity, *action))
            .collect();
        let place = actions
            .iter()
            .find(|(_, action)| *action == HudAction::Launchpad(LaunchpadControl::ChooseSurface))
            .unwrap()
            .0;
        let cancel = actions
            .iter()
            .find(|(_, action)| *action == HudAction::Launchpad(LaunchpadControl::Cancel))
            .unwrap()
            .0;
        let window = app
            .world_mut()
            .query_filtered::<Entity, With<bevy::window::PrimaryWindow>>()
            .single(app.world())
            .unwrap();
        app.world_mut()
            .resource_mut::<InputFocus>()
            .set(place, FocusCause::Navigated);
        tap_key(&mut app, window, KeyCode::Enter, Key::Enter);
        for _ in 0..4 {
            app.update();
        }
        assert!(crate::launchpad::gesture_active());
        assert_eq!(app.world().resource::<HudState>().panel, SidePanel::Launch);
        assert_eq!(app.world().resource::<InputFocus>().get(), Some(cancel));
        let overlays: Vec<_> = app
            .world_mut()
            .query::<(Entity, &Overlay)>()
            .iter(app.world())
            .map(|(entity, overlay)| (entity, *overlay))
            .collect();
        for (entity, overlay) in &overlays {
            if matches!(overlay, Overlay::FlightStrip | Overlay::Launch) {
                assert_eq!(
                    app.world().get::<Node>(*entity).unwrap().display,
                    Display::None
                );
            }
        }
        let preview = overlays
            .iter()
            .find(|(_, overlay)| matches!(overlay, Overlay::LaunchpadPreview))
            .unwrap()
            .0;
        assert_eq!(
            app.world().get::<Node>(preview).unwrap().display,
            Display::Flex
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
        let strips: Vec<_> = app
            .world_mut()
            .query::<(Entity, &LayoutPart)>()
            .iter(app.world())
            .filter_map(|(entity, part)| {
                matches!(part, LayoutPart::TopRow | LayoutPart::BottomRow).then_some(entity)
            })
            .collect();
        for strip in strips {
            let rect = assert_in_viewport(app.world(), strip, viewport, "placement", "HUD strip");
            assert!(
                rect.max.y <= viewport.center().y - BUTTON_HEIGHT_PX * ui_scale + 1.0
                    || rect.min.y >= viewport.center().y + BUTTON_HEIGHT_PX * ui_scale - 1.0,
                "placement chrome must leave the framed pad center uncovered: {rect:?}"
            );
        }
        let controls: Vec<_> = actions
            .iter()
            .filter_map(|(entity, _)| {
                descendant_of(app.world(), *entity, preview).then_some(*entity)
            })
            .collect();
        assert_eq!(
            controls.len(),
            6,
            "one Apply, Cancel and four native nudges"
        );
        for control in controls {
            app.world_mut()
                .resource_mut::<InputFocus>()
                .set(control, FocusCause::Navigated);
            for _ in 0..3 {
                app.update();
            }
            assert_in_viewport(
                app.world(),
                control,
                viewport,
                "placement focus",
                "native action",
            );
            assert_button_glyphs_fit(app.world(), control, "placement action");
            let node = app.world().get::<ComputedNode>(control).unwrap();
            assert!(node.size().min_element() * node.inverse_scale_factor() >= 24.0);
            assert_ne!(
                app.world().get::<Outline>(control).unwrap().color,
                Color::NONE
            );
        }
        // This fixture checks queued HUD intents and native layout, not the
        // launchpad adapter's commit. Clear its request bridge for cleanup.
        crate::launchpad::reset_for_tests();
        for _ in 0..4 {
            app.update();
        }
        assert_eq!(
            app.world().get::<Node>(preview).unwrap().display,
            Display::None
        );
        for (entity, overlay) in &overlays {
            if matches!(overlay, Overlay::FlightStrip | Overlay::Launch) {
                assert_eq!(
                    app.world().get::<Node>(*entity).unwrap().display,
                    Display::Flex
                );
            }
        }
        let focused = app.world().resource::<InputFocus>().get().unwrap();
        assert_eq!(
            focused, place,
            "return to the logical Launch task after placement"
        );
        assert!(
            app.world()
                .get::<bevy::ui_widgets::Button>(focused)
                .is_some()
        );
        assert_in_viewport(
            app.world(),
            focused,
            viewport,
            "placement ended",
            "restored focus",
        );
        let launch = overlays
            .iter()
            .find(|(_, overlay)| matches!(overlay, Overlay::Launch))
            .unwrap()
            .0;
        let status = app
            .world_mut()
            .query::<(Entity, &Readout)>()
            .iter(app.world())
            .find_map(|(entity, readout)| {
                (matches!(readout, Readout::LaunchpadSummary)
                    && descendant_of(app.world(), entity, launch))
                .then_some(entity)
            })
            .unwrap();
        assert!(
            app.world()
                .get::<InheritedVisibility>(status)
                .unwrap()
                .get()
        );
        assert_eq!(
            app.world().get::<Text>(status).unwrap().0,
            super::readable_text(&crate::launchpad::summary()),
            "Launch retains the adapter's result, cancellation or rejection status"
        );
        assert!(std::rc::Rc::ptr_eq(&unchanged, &crate::snapshot()));
    }
}

#[test]
fn captions_errors_vectors_and_long_labels_shape_inside_real_content_widths() {
    use super::model::{Overlay, SidePanel, TaskTab};
    use bevy::input_focus::FocusCause;

    // These are text stress cases in the actual production widgets. The font,
    // glyphs, padding, camera scale, clipping and Taffy geometry are all Bevy's.
    // No ComputedNode, glyph position or render target is fabricated here.
    for (width, height, ui_scale) in [
        (320, 640, 1.0),
        (321, 640, 1.0),
        (320, 640, 2.0),
        (320, 960, 2.0),
        (321, 960, 2.0),
        (800, 360, 2.0),
    ] {
        let (mut app, camera) = hud_app(width, height, 1.0, ui_scale);
        app.add_systems(Update, super::systems::update_buttons);
        let unchanged = crate::snapshot();
        {
            let mut hud = app.world_mut().resource_mut::<HudState>();
            hud.panel = SidePanel::Launch;
            hud.expanded_instruments.insert(InstrumentGroup::World);
        }
        for _ in 0..5 {
            app.update();
        }
        let panel = app
            .world_mut()
            .query::<(Entity, &Overlay)>()
            .iter(app.world())
            .find_map(|(entity, overlay)| matches!(overlay, Overlay::Launch).then_some(entity))
            .unwrap();
        let viewport = Rect::from_corners(
            Vec2::ZERO,
            app.world()
                .get::<Camera>(camera)
                .unwrap()
                .physical_viewport_size()
                .unwrap()
                .as_vec2(),
        );
        assert_in_viewport(app.world(), panel, viewport, "wrapped task", "Launch panel");
        let place = app
            .world_mut()
            .query::<(Entity, &HudAction)>()
            .iter(app.world())
            .find_map(|(entity, action)| {
                matches!(
                    action,
                    HudAction::Launchpad(super::model::LaunchpadControl::ChooseSurface)
                )
                .then_some(entity)
            })
            .unwrap();
        assert_eq!(
            app.world().get::<AccessibleLabel>(place).unwrap().0,
            "Place lander"
        );
        assert_eq!(app.world().get::<ChildOf>(place).unwrap().parent(), panel);
        assert_in_viewport(
            app.world(),
            place,
            viewport,
            "Launch primary action",
            "Place lander",
        );
        let target = |app: &mut App, caption: bool| {
            app.world_mut()
                .query::<(Entity, &Readout)>()
                .iter(app.world())
                .find_map(|(entity, readout)| {
                    (if caption {
                        matches!(readout, Readout::LaunchpadHint)
                    } else {
                        matches!(readout, Readout::LaunchSite)
                    })
                    .then_some(entity)
                })
                .unwrap()
        };
        let error = format!(
            "Not applied. {} Confirmed values reloaded.",
            super::EditorField::WorldCount.parse("1e309").unwrap_err()
        );
        let vector = format!(
            "XYZ: {:.16e}, {:.16e}, {:.16e} m/s^2",
            f64::MAX,
            -f64::MAX,
            f64::MIN_POSITIVE
        );
        for (label, caption, value) in [
            ("long caption", true, "Stage a new-world asteroid distribution without replacing the current flight. Count, shell clearance and ejection fields remain a draft until you confirm creation.".to_owned()),
            ("validation error", false, error),
            ("scientific vector", false, vector),
            ("long identifier", false, "flight-export-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef".to_owned()),
            ("hanging whitespace", true, "Measured velocity   and acceleration   stay read-only. This caption has repeated spaces   at potential word boundaries and retains the complete source text.".to_owned()),
        ] {
            let entity = target(&mut app, caption);
            app.world_mut().get_mut::<Text>(entity).unwrap().0 = value.clone();
            for _ in 0..3 { app.update(); }
            assert_eq!(app.world().get::<Text>(entity).unwrap().0, value, "full text remains available, never an ellipsis");
            let lines = assert_text_glyphs_fit(app.world(), entity, label);
            assert!(lines > 1, "{width}x{height} at {ui_scale}: {label} must really wrap, not just report a narrow Node");
            let text_width = app.world().get::<ComputedNode>(entity).unwrap().content_box().size().x;
            let content_width = app.world().get::<ComputedNode>(panel).unwrap().content_box().size().x;
            assert!(text_width <= content_width + 1.0, "{label}: text width {text_width} exceeds real panel content width {content_width}");
            let layout = app.world().get::<TextLayoutInfo>(entity).unwrap();
            let transform = app.world().get::<UiGlobalTransform>(entity).unwrap();
            let origin = app.world().get::<ComputedNode>(entity).unwrap().content_box().min;
            if let Some(clip) = app.world().get::<CalculatedClip>(entity).and_then(CalculatedClip::rects) {
                for glyph in &layout.glyphs {
                    for point in [glyph.position - glyph.atlas_info.rect.size() * 0.5, glyph.position + glyph.atlas_info.rect.size() * 0.5] {
                        let rendered = transform.transform_point2(origin + point);
                        for inherited in clip {
                            let clipped = inherited.world_to_clip_local.transform_point2(rendered);
                            assert!(clipped.x >= inherited.rect.min.x - 1.0 && clipped.x <= inherited.rect.max.x + 1.0, "{label}: a shaped glyph crosses the horizontal clip: {clipped:?}, {inherited:?}");
                        }
                    }
                }
            }
        }
        let controls: Vec<_> = app
            .world_mut()
            .query::<(Entity, &bevy::ui_widgets::Button, &InheritedVisibility)>()
            .iter(app.world())
            .filter_map(|(entity, _, visible)| {
                (visible.get() && descendant_of(app.world(), entity, panel)).then_some(entity)
            })
            .collect();
        for entity in controls {
            assert_button_glyphs_fit(app.world(), entity, "wrapped Launch action");
            let node = app.world().get::<ComputedNode>(entity).unwrap();
            assert!(node.size().min_element() * node.inverse_scale_factor() >= 24.0);
            assert!(
                !app.world()
                    .get::<AccessibleLabel>(entity)
                    .unwrap()
                    .0
                    .is_empty()
            );
        }
        let main_toolbar = app
            .world_mut()
            .query::<(Entity, &super::model::LayoutPart)>()
            .iter(app.world())
            .find_map(|(entity, part)| {
                matches!(part, super::model::LayoutPart::BottomRow).then_some(entity)
            })
            .unwrap();
        let critical: Vec<_> = app
            .world_mut()
            .query::<(Entity, &HudAction, Option<&TaskTab>)>()
            .iter(app.world())
            .filter_map(|(entity, action, tab)| {
                (descendant_of(app.world(), entity, main_toolbar)
                    && (tab.is_some()
                        || matches!(
                            action,
                            HudAction::TogglePlayback
                                | HudAction::FollowLander
                                | HudAction::Notebook
                        )))
                .then_some(entity)
            })
            .collect();
        assert_eq!(critical.len(), 9);
        for entity in critical {
            assert_in_viewport(
                app.world(),
                entity,
                viewport,
                "200 percent task layout",
                "main action or task tab",
            );
            assert_button_glyphs_fit(app.world(), entity, "main action or task tab");
        }
        let footer = app
            .world()
            .get::<Children>(panel)
            .unwrap()
            .iter()
            .last()
            .unwrap();
        app.world_mut()
            .resource_mut::<InputFocus>()
            .set(footer, FocusCause::Navigated);
        for _ in 0..3 {
            app.update();
        }
        assert_in_viewport(
            app.world(),
            footer,
            viewport,
            "wrapped task focus",
            "definitions footer",
        );
        assert_ne!(
            app.world().get::<Outline>(footer).unwrap().color,
            Color::NONE
        );
        assert!(app.world().get::<ScrollPosition>(panel).unwrap().y > 0.0);
        assert_eq!(app.world().get::<ScrollPosition>(panel).unwrap().x, 0.0);
        let window = app
            .world_mut()
            .query_filtered::<Entity, With<bevy::window::PrimaryWindow>>()
            .single(app.world())
            .unwrap();
        tap_key(&mut app, window, KeyCode::Home, Key::Home);
        for _ in 0..3 {
            app.update();
        }
        assert_eq!(app.world().get::<ScrollPosition>(panel).unwrap().y, 0.0);
        tap_key(&mut app, window, KeyCode::PageDown, Key::PageDown);
        assert!(
            app.world().get::<ScrollPosition>(panel).unwrap().y > 0.0,
            "long prose remains reachable by keyboard scrolling"
        );
        app.world_mut().resource_mut::<HudState>().panel = SidePanel::Closed;
        for _ in 0..5 {
            app.update();
        }
        if ui_scale == 2.0 {
            let cue = app
                .world_mut()
                .query_filtered::<&Node, With<super::model::CompactFlightCue>>()
                .single(app.world())
                .unwrap();
            assert_eq!(
                cue.display,
                Display::Flex,
                "extreme zoom advertises the scrollable flight strip"
            );
            let strip = app
                .world_mut()
                .query::<(Entity, &super::model::LayoutPart)>()
                .iter(app.world())
                .find_map(|(entity, part)| {
                    matches!(part, super::model::LayoutPart::TopRow).then_some(entity)
                })
                .unwrap();
            let tab = app
                .world_mut()
                .query::<(Entity, &TaskTab)>()
                .iter(app.world())
                .find_map(|(entity, tab)| (tab.0 == SidePanel::Flight).then_some(entity))
                .unwrap();
            app.world_mut()
                .resource_mut::<InputFocus>()
                .set(tab, FocusCause::Navigated);
            for _ in 0..3 {
                app.update();
            }
            tap_key(&mut app, window, KeyCode::End, Key::End);
            assert!(
                app.world().get::<ScrollPosition>(strip).unwrap().y > 0.0,
                "the disclosed flight strip can be scrolled without a pointer"
            );
            tap_key(&mut app, window, KeyCode::Home, Key::Home);
            assert_eq!(app.world().get::<ScrollPosition>(strip).unwrap().y, 0.0);
        }
        assert!(std::rc::Rc::ptr_eq(&unchanged, &crate::snapshot()));
        assert!(app.get_sub_app(RenderApp).is_none());
    }
}

fn hud_app(width: u32, height: u32, window_scale: f32, ui_scale: f32) -> (App, Entity) {
    crate::controls::reset_for_tests();
    crate::launchpad::reset_for_tests();
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
        .add_systems(
            Update,
            (
                super::systems::update_layout,
                super::systems::update_overlays,
                super::systems::update_instrument_sections,
            )
                .chain(),
        )
        .add_systems(
            PostUpdate,
            super::systems::scroll_focused_control_into_view.after(bevy::ui::UiSystems::PostLayout),
        )
        // This is Bevy's real window-to-camera calculation, without RenderPlugin.
        .add_systems(
            PostUpdate,
            camera_system
                .in_set(CameraUpdateSystems)
                .before(AssetEventSystems)
                .before(update_frusta),
        );
    super::input::install(&mut app);
    assert!(
        !super::fullscreen_shortcut_allowed(),
        "a HUD must publish real input state before authorizing a shortcut"
    );
    super::text_layout::install(&mut app);
    super::editors::install(&mut app);
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
    (app, camera)
}

fn check_hud(width: u32, height: u32, window_scale: f32, ui_scale: f32) {
    let (mut app, camera) = hud_app(width, height, window_scale, ui_scale);
    let path = crate::navigation::brief();
    for (stage, numeric) in [
        ("missing telemetry", false),
        ("numeric flight instruments", true),
    ] {
        // Change only production readout strings. Measurement and geometry stay engine-owned.
        let world = app.world_mut();
        for (readout, mut text) in world.query::<(&Readout, &mut Text)>().iter_mut(world) {
            match readout {
                Readout::Instrument(Instrument::Flight(metric)) if numeric => {
                    let value = match metric {
                        FlightMetric::Speed => "123.45 m/s",
                        FlightMetric::TerrainClearance => "98765.4 m",
                        FlightMetric::Climb => "-12.34 m/s",
                        FlightMetric::TargetRange => "98765.4 m",
                        FlightMetric::Closing => "-12.34 m/s",
                        FlightMetric::ThrustAcceleration => "0.500 m/s²",
                        FlightMetric::Force
                        | FlightMetric::Torque
                        | FlightMetric::Spin
                        | FlightMetric::FuelUsed
                        | FlightMetric::Damage => continue,
                    };
                    text.0 = super::readable_text(value);
                }
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

fn descendant_of(world: &World, mut entity: Entity, ancestor: Entity) -> bool {
    while let Some(parent) = world.get::<ChildOf>(entity) {
        entity = parent.parent();
        if entity == ancestor {
            return true;
        }
    }
    false
}

fn check_instruments_disclosure(app: &mut App, camera: Entity, window: Entity) {
    use super::model::{InstrumentContents, InstrumentSection, Overlay, SidePanel, TaskTab};
    use bevy::input_focus::FocusCause;

    for _ in 0..3 {
        app.update();
    }
    let trigger = app
        .world_mut()
        .query::<(Entity, &HudAction, &InheritedVisibility)>()
        .iter(app.world())
        .find_map(|(entity, action, visible)| {
            (matches!(action, HudAction::ToggleInstruments) && visible.get()).then_some(entity)
        })
        .expect("Flight is a visible native task tab, not a keyboard-only secret");
    let scale = app
        .world()
        .get::<ComputedNode>(trigger)
        .unwrap()
        .inverse_scale_factor()
        .recip();
    assert_chrome(app, camera, "live ownership and plan status", scale);
    let panel = app
        .world_mut()
        .query::<(Entity, &Overlay)>()
        .iter(app.world())
        .find_map(|(entity, overlay)| matches!(overlay, Overlay::Flight).then_some(entity))
        .unwrap();
    assert_eq!(
        app.world().get::<Node>(panel).unwrap().display,
        Display::None
    );
    app.world_mut()
        .resource_mut::<InputFocus>()
        .set(trigger, FocusCause::Navigated);
    tap_key(app, window, KeyCode::Enter, Key::Enter);
    for _ in 0..3 {
        app.update();
    }
    assert_eq!(app.world().resource::<HudState>().panel, SidePanel::Flight);
    assert!(
        !app.world().resource::<HudState>().reset_confirm_open,
        "a read-only disclosure needs no confirmation"
    );
    assert_eq!(
        app.world()
            .get::<bevy::a11y::AccessibilityNode>(trigger)
            .unwrap()
            .is_expanded(),
        Some(true)
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
    assert_in_viewport(
        app.world(),
        panel,
        viewport,
        "Flight disclosure",
        "Flight panel",
    );

    let sections: Vec<_> = app
        .world_mut()
        .query::<(Entity, &InstrumentSection)>()
        .iter(app.world())
        .map(|(entity, section)| (entity, section.0))
        .collect();
    assert_eq!(sections.len(), InstrumentGroup::ALL.len());
    let surface_controls: Vec<_> = app
        .world_mut()
        .query::<(Entity, &HudAction)>()
        .iter(app.world())
        .filter_map(|(entity, action)| matches!(action, HudAction::Launchpad(_)).then_some(entity))
        .collect();
    assert_eq!(surface_controls.len(), 8);
    for entity in surface_controls {
        assert!(
            app.world()
                .get::<crate::launchpad::SurfaceControl>(entity)
                .is_some(),
            "every launchpad action blocks manual flight while focused"
        );
        assert!(
            app.world().get::<super::InstrumentEditor>(entity).is_none(),
            "surface controls must not cancel their own placement as text editors"
        );
    }
    let task_tabs: Vec<_> = app
        .world_mut()
        .query::<(Entity, &TaskTab)>()
        .iter(app.world())
        .map(|(entity, tab)| (entity, tab.0))
        .collect();
    assert_eq!(
        task_tabs.len(),
        6,
        "six concise task tabs, separate from Play, Follow and Notebook"
    );
    for task in SidePanel::TASKS {
        assert_eq!(
            task_tabs.iter().filter(|(_, panel)| *panel == task).count(),
            1
        );
    }
    for group in InstrumentGroup::ALL {
        let owner = match group {
            InstrumentGroup::Flight
            | InstrumentGroup::Acceleration
            | InstrumentGroup::Peaks
            | InstrumentGroup::Ship
            | InstrumentGroup::Target
            | InstrumentGroup::Mission => SidePanel::Flight,
            InstrumentGroup::Actuation => SidePanel::Ship,
            InstrumentGroup::Stream => SidePanel::Debris,
            InstrumentGroup::World => SidePanel::Launch,
        };
        let owner_tab = task_tabs
            .iter()
            .find(|(_, panel)| *panel == owner)
            .unwrap()
            .0;
        let unchanged = crate::snapshot();
        let queued = crate::session::queued_actions_for_tests();
        if app.world().resource::<HudState>().panel != owner {
            app.world_mut()
                .resource_mut::<InputFocus>()
                .set(owner_tab, FocusCause::Navigated);
            tap_key(app, window, KeyCode::Enter, Key::Enter);
            for _ in 0..3 {
                app.update();
            }
        }
        assert_eq!(app.world().resource::<HudState>().panel, owner);
        assert!(
            std::rc::Rc::ptr_eq(&unchanged, &crate::snapshot()),
            "opening {owner:?} never changes a flight"
        );
        assert_eq!(crate::session::queued_actions_for_tests(), queued);
        let owner_panel = app
            .world_mut()
            .query::<(Entity, &Overlay)>()
            .iter(app.world())
            .find_map(|(entity, overlay)| {
                let matches = match overlay {
                    Overlay::Flight => owner == SidePanel::Flight,
                    Overlay::Ship => owner == SidePanel::Ship,
                    Overlay::Debris => owner == SidePanel::Debris,
                    Overlay::Launch => owner == SidePanel::Launch,
                    Overlay::FlightStrip
                    | Overlay::LaunchpadPreview
                    | Overlay::Camera
                    | Overlay::Details
                    | Overlay::Controls
                    | Overlay::Reset
                    | Overlay::Inspector => false,
                };
                matches.then_some(entity)
            })
            .unwrap();
        assert_in_viewport(
            app.world(),
            owner_panel,
            viewport,
            "Task inventory",
            group.title(),
        );
        let matching: Vec<_> = sections
            .iter()
            .filter(|(_, value)| *value == group)
            .collect();
        assert_eq!(matching.len(), 1, "one native section for {group:?}");
        let section = matching[0].0;
        assert!(descendant_of(app.world(), section, owner_panel));
        let actual: Vec<_> = app
            .world_mut()
            .query::<(Entity, &Readout)>()
            .iter(app.world())
            .filter_map(|(entity, readout)| match readout {
                Readout::Instrument(key) if descendant_of(app.world(), entity, section) => {
                    Some(*key)
                }
                _ => None,
            })
            .collect();
        assert_eq!(actual.len(), group.instruments().len(), "{group:?}");
        for key in group.instruments() {
            assert_eq!(
                actual.iter().filter(|value| *value == key).count(),
                1,
                "{group:?}: {key:?}"
            );
        }
        let contents = app
            .world_mut()
            .query::<(Entity, &InstrumentContents)>()
            .iter(app.world())
            .find_map(|(entity, contents)| (contents.0 == group).then_some(entity))
            .unwrap();
        let toggle = app
            .world_mut()
            .query::<(Entity, &HudAction)>()
            .iter(app.world())
            .find_map(|(entity, action)| {
                matches!(action, HudAction::ToggleInstrumentSection(value) if *value == group)
                    .then_some(entity)
            })
            .unwrap();
        assert!(descendant_of(app.world(), toggle, section));
        let initially_expanded = matches!(
            group,
            InstrumentGroup::Flight
                | InstrumentGroup::Mission
                | InstrumentGroup::Actuation
                | InstrumentGroup::Stream
        );
        assert_eq!(
            app.world()
                .get::<bevy::a11y::AccessibilityNode>(toggle)
                .unwrap()
                .is_expanded(),
            Some(initially_expanded),
        );
        assert_eq!(
            app.world().get::<Node>(contents).unwrap().display,
            if initially_expanded {
                Display::Flex
            } else {
                Display::None
            },
        );
        app.world_mut()
            .resource_mut::<InputFocus>()
            .set(toggle, FocusCause::Navigated);
        for _ in 0..3 {
            app.update();
        }
        assert_in_viewport(
            app.world(),
            toggle,
            viewport,
            "Instrument sections",
            group.title(),
        );
        let node = app.world().get::<ComputedNode>(toggle).unwrap();
        assert!(node.size().min_element() * node.inverse_scale_factor() >= 24.0);
        assert_ne!(
            app.world().get::<Outline>(toggle).unwrap().color,
            Color::NONE
        );
        assert_button_glyphs_fit(app.world(), toggle, "task section label");
        // Close and reopen the initial section too. Every disclosure takes the
        // same keyboard path, without altering the underlying measurements.
        if initially_expanded {
            tap_key(app, window, KeyCode::Enter, Key::Enter);
            for _ in 0..3 {
                app.update();
            }
            assert_eq!(
                app.world().get::<Node>(contents).unwrap().display,
                Display::None
            );
        }
        tap_key(app, window, KeyCode::Enter, Key::Enter);
        for _ in 0..3 {
            app.update();
        }
        assert_eq!(
            app.world().get::<Node>(contents).unwrap().display,
            Display::Flex
        );
        assert_eq!(
            app.world()
                .get::<bevy::a11y::AccessibilityNode>(toggle)
                .unwrap()
                .is_expanded(),
            Some(true),
        );
        assert_eq!(app.world().resource::<InputFocus>().get(), Some(toggle));
    }
    // Reach the bottom through the production focus-to-ScrollIntoView system.
    // No test-owned transforms, ComputedNodes or fabricated captures are used.
    let footer = app
        .world()
        .get::<Children>(panel)
        .unwrap()
        .iter()
        .last()
        .unwrap();
    app.world_mut()
        .resource_mut::<InputFocus>()
        .set(footer, FocusCause::Navigated);
    for _ in 0..3 {
        app.update();
    }
    assert_in_viewport(
        app.world(),
        footer,
        viewport,
        "Flight focus scrolling",
        "measurement definitions action",
    );
    assert!(
        app.world().get::<ScrollPosition>(panel).unwrap().y > 0.0,
        "the comprehensive instrument inventory must scroll"
    );
    let epoch = app
        .world_mut()
        .query::<(Entity, &Readout)>()
        .iter(app.world())
        .find_map(|(entity, readout)| {
            matches!(readout, Readout::Instrument(Instrument::EpochIndex)).then_some(entity)
        })
        .unwrap();
    assert_in_viewport(
        app.world(),
        epoch,
        viewport,
        "Flight focus scrolling",
        "last sampled measurement",
    );
    tap_key(app, window, KeyCode::Home, Key::Home);
    for _ in 0..3 {
        app.update();
    }
    let source = app
        .world_mut()
        .query::<(Entity, &Readout)>()
        .iter(app.world())
        .find_map(|(entity, readout)| matches!(readout, Readout::TelemetryStatus).then_some(entity))
        .unwrap();
    assert_in_viewport(
        app.world(),
        source,
        viewport,
        "Flight keyboard scrolling",
        "source time and availability",
    );
    assert!(
        app.world()
            .get::<Text>(source)
            .unwrap()
            .0
            .starts_with("Source ")
    );
    tap_key(app, window, KeyCode::Escape, Key::Escape);
    for _ in 0..3 {
        app.update();
    }
    assert_eq!(app.world().resource::<HudState>().panel, SidePanel::Closed);
    assert_eq!(
        app.world().resource::<InputFocus>().get(),
        Some(trigger),
        "closing a scrolled panel returns focus to its visible disclosure"
    );
    assert_eq!(
        app.world()
            .get::<bevy::a11y::AccessibilityNode>(trigger)
            .unwrap()
            .is_expanded(),
        Some(false)
    );

    let views = app
        .world_mut()
        .query::<(Entity, &HudAction, &InheritedVisibility)>()
        .iter(app.world())
        .find_map(|(entity, action, visible)| {
            (matches!(action, HudAction::ToggleCamera) && visible.get()).then_some(entity)
        })
        .unwrap();
    app.world_mut()
        .resource_mut::<InputFocus>()
        .set(views, FocusCause::Navigated);
    tap_key(app, window, KeyCode::Enter, Key::Enter);
    for _ in 0..3 {
        app.update();
    }
    assert_eq!(app.world().resource::<HudState>().panel, SidePanel::Camera);
    for action in [
        HudAction::CycleIllumination,
        HudAction::ToggleStars,
        HudAction::ToggleGuides,
    ] {
        let entity = app
            .world_mut()
            .query::<(Entity, &HudAction)>()
            .iter(app.world())
            .find_map(|(entity, candidate)| (*candidate == action).then_some(entity))
            .unwrap();
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
            "Viewer aids",
            "lighting or visibility control",
        );
        let label = &app.world().get::<AccessibleLabel>(entity).unwrap().0;
        assert!(!label.is_empty() && label.is_ascii());
        assert_eq!(
            app.world()
                .get::<bevy::input_focus::tab_navigation::TabIndex>(entity)
                .unwrap()
                .0,
            0
        );
        let node = app.world().get::<ComputedNode>(entity).unwrap();
        assert!(node.size().min_element() * node.inverse_scale_factor() >= 24.0);
        assert_ne!(
            app.world().get::<Outline>(entity).unwrap().color,
            Color::NONE
        );
    }
    let choices: Vec<_> = app
        .world_mut()
        .query::<(Entity, &HudAction)>()
        .iter(app.world())
        .filter_map(|(entity, action)| match action {
            HudAction::ChangeCamera(mode) => Some((entity, *mode)),
            _ => None,
        })
        .collect();
    assert_eq!(
        choices.len(),
        7,
        "all viewer and sensor modes remain explicit native choices"
    );
    for (entity, mode) in choices {
        if mode.sensor().is_some() {
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
                "Views focus scrolling",
                "explicit RGB sensor choice",
            );
        }
    }
    tap_key(app, window, KeyCode::Enter, Key::Enter);
    for _ in 0..3 {
        app.update();
    }
    assert_eq!(
        app.world().resource::<HudState>().panel,
        SidePanel::Closed,
        "selecting a sensor dismisses its menu instead of covering the feed"
    );
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
        Update,
        (
            super::systems::interactions,
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
    check_instruments_disclosure(app, camera, window);
    check_native_instrument_editors(app, camera, window);
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
        "Upright lander | test reset"
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
        "Notebook button",
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
        "Notebook remains an active native target while controls are open"
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
    let guide = app
        .world_mut()
        .query::<(Entity, &HudAction)>()
        .iter(app.world())
        .find_map(|(entity, action)| {
            matches!(
                action,
                HudAction::ToggleReference(super::model::ReferenceSection::ControlGuide)
            )
            .then_some(entity)
        })
        .unwrap();
    assert!(
        !app.world()
            .resource::<HudState>()
            .expanded_references
            .contains(&super::model::ReferenceSection::ControlGuide),
        "dense input references are not the default help view"
    );
    app.world_mut()
        .resource_mut::<InputFocus>()
        .set(guide, FocusCause::Navigated);
    tap_key(app, window, KeyCode::Enter, Key::Enter);
    for _ in 0..3 {
        app.update();
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
    assert!(text.0.contains("Command owner: "));
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

fn check_native_instrument_editors(app: &mut App, camera: Entity, window: Entity) {
    use super::{EditorField, InstrumentEditor, model::SidePanel};
    use crate::model::{
        DebrisDistribution, ExperimentSettings, FlightPreset, PhysicsCommand, StreamAxis,
    };
    use bevy::{
        input_focus::FocusCause,
        text::EditableText,
        ui_widgets::{Button, TextInput},
    };

    fn field(app: &mut App, requested: EditorField) -> Entity {
        app.world_mut()
            .query::<(Entity, &EditorField)>()
            .iter(app.world())
            .find_map(|(entity, field)| (*field == requested).then_some(entity))
            .unwrap()
    }
    fn action(app: &mut App, label: &str) -> Entity {
        app.world_mut()
            .query_filtered::<(Entity, &AccessibleLabel), (With<Button>, With<InstrumentEditor>)>()
            .iter(app.world())
            .find_map(|(entity, name)| (name.0 == label).then_some(entity))
            .unwrap()
    }
    fn activate(app: &mut App, window: Entity, entity: Entity) {
        app.world_mut()
            .resource_mut::<InputFocus>()
            .set(entity, FocusCause::Navigated);
        for _ in 0..3 {
            app.update();
        }
        tap_key(app, window, KeyCode::Enter, Key::Enter);
        for _ in 0..3 {
            app.update();
        }
    }
    fn enter_number(app: &mut App, window: Entity, entity: Entity, value: &str) {
        app.world_mut()
            .resource_mut::<InputFocus>()
            .set(entity, FocusCause::Navigated);
        for _ in 0..3 {
            app.update();
        }
        // Use real keyboard text messages. SelectAllOnFocus, TextInputPlugin and
        // EditableTextSystems own the editor buffer and shaping, not the test.
        for character in value.chars() {
            let code = match character {
                '0' => KeyCode::Digit0,
                '1' => KeyCode::Digit1,
                '2' => KeyCode::Digit2,
                '3' => KeyCode::Digit3,
                '4' => KeyCode::Digit4,
                '5' => KeyCode::Digit5,
                '9' => KeyCode::Digit9,
                'e' => KeyCode::KeyE,
                '.' => KeyCode::Period,
                '-' => KeyCode::Minus,
                _ => panic!("test character {character}"),
            };
            let text = character.to_string();
            app.world_mut().write_message(KeyboardInput {
                key_code: code,
                logical_key: Key::Character(text.clone().into()),
                state: ButtonState::Pressed,
                text: Some(text.into()),
                repeat: false,
                window,
            });
            app.update();
            key_frame(
                app,
                window,
                code,
                Key::Character(character.to_string().into()),
                ButtonState::Released,
            );
        }
        tap_key(app, window, KeyCode::Enter, Key::Enter);
        for _ in 0..3 {
            app.update();
        }
    }

    let saved = crate::snapshot();
    let mut settings = ExperimentSettings::default();
    settings.debris.count = 0;
    crate::session::dispatch(crate::api::Dispatch::Physics(
        PhysicsCommand::ConfigureFlight {
            settings,
            seed: Some(saved.seed),
            preset: FlightPreset::FarSideL2,
        },
    ))
    .unwrap();
    crate::session::dispatch(crate::api::Dispatch::Physics(PhysicsCommand::SetCommand {
        accel: [0.0, 0.1, 0.0],
        angular_accel: [0.0; 3],
    }))
    .unwrap();
    app.world_mut().resource_mut::<HudState>().panel = SidePanel::Ship;
    for _ in 0..4 {
        app.update();
    }
    let mass = field(app, EditorField::Mass);
    assert!(app.world().get::<TextInput>(mass).is_some());
    assert!(app.world().get::<InstrumentEditor>(mass).is_some());
    assert_eq!(
        app.world().get::<AccessibleLabel>(mass).unwrap().0,
        EditorField::Mass.label()
    );
    assert_eq!(
        app.world()
            .get::<bevy::input_focus::tab_navigation::TabIndex>(mass)
            .unwrap()
            .0,
        0
    );
    let before = crate::session::applied_physics().unwrap();
    let queued = crate::session::queued_actions_for_tests();
    enter_number(app, window, mass, "4500");
    let body = crate::session::applied_physics().unwrap();
    assert_eq!(body.mass_kg, 4500.0);
    assert_eq!(body.source_time_s, before.source_time_s);
    assert_eq!(body.position_world_m, before.position_world_m);
    assert_eq!(body.velocity_world_mps, before.velocity_world_mps);
    assert_eq!(
        body.requested, before.requested,
        "calibration preserves the API-owned command"
    );
    assert!(!crate::session::is_running());
    assert_eq!(crate::session::queued_actions_for_tests(), queued);
    let viewport = Rect::from_corners(
        Vec2::ZERO,
        app.world()
            .get::<Camera>(camera)
            .unwrap()
            .physical_viewport_size()
            .unwrap()
            .as_vec2(),
    );
    assert_in_viewport(
        app.world(),
        mass,
        viewport,
        "Native numeric focus",
        "Mass input",
    );
    let node = app.world().get::<ComputedNode>(mass).unwrap();
    assert!(node.size().min_element() * node.inverse_scale_factor() >= 24.0);
    assert_ne!(app.world().get::<Outline>(mass).unwrap().color, Color::NONE);
    let shaped = app.world().get::<TextLayoutInfo>(mass).unwrap();
    assert!(
        !shaped.glyphs.is_empty(),
        "the real editable widget has shaped numeric glyphs"
    );
    tap_key(app, window, KeyCode::Tab, Key::Tab);
    let decrease = app.world().resource::<InputFocus>().get().unwrap();
    assert_eq!(
        app.world().get::<AccessibleLabel>(decrease).unwrap().0,
        "Decrease Mass input [kg] (-)"
    );
    assert!(app.world().get::<InstrumentEditor>(decrease).is_some());
    tap_key(app, window, KeyCode::Enter, Key::Enter);
    assert_eq!(crate::session::applied_physics().unwrap().mass_kg, 4400.0);
    tap_key(app, window, KeyCode::Tab, Key::Tab);
    let increase = app.world().resource::<InputFocus>().get().unwrap();
    assert_eq!(
        app.world().get::<AccessibleLabel>(increase).unwrap().0,
        "Increase Mass input [kg] (+)"
    );
    tap_key(app, window, KeyCode::Enter, Key::Enter);
    assert_eq!(crate::session::applied_physics().unwrap().mass_kg, 4500.0);
    enter_number(app, window, mass, "99");
    assert_eq!(crate::session::applied_physics().unwrap().mass_kg, 4500.0);
    assert_eq!(
        app.world()
            .get::<EditableText>(mass)
            .unwrap()
            .value()
            .to_string(),
        "4500"
    );
    for _ in 0..8 {
        app.update();
    }
    assert!(
        app.world()
            .get::<bevy::a11y::AccessibilityNode>(mass)
            .unwrap()
            .description()
            .unwrap()
            .contains("100000")
    );

    let inertia = field(app, EditorField::InertiaFactor);
    enter_number(app, window, inertia, "1.5");
    let gain = field(app, EditorField::ThrustGain);
    enter_number(app, window, gain, "2");
    let tuning = crate::snapshot().ship_tuning();
    assert_eq!(tuning.inertia_scale, 1.5);
    assert_eq!(
        crate::session::applied_physics().unwrap().actuator_gain,
        2.0
    );
    for (actual, expected) in crate::session::applied_physics()
        .unwrap()
        .inertia_body_kg_m2
        .iter()
        .flatten()
        .zip(tuning.inertia_kg_m2().iter().flatten())
    {
        assert!((actual - expected).abs() <= expected.abs().max(1.0) * 1e-9);
    }
    // Native text focus owns game keys even when their numeric filter rejects
    // the characters. These CPU checks cover the HUD queue, not GPU appearance.
    for (code, key) in [
        (KeyCode::KeyP, Key::Character("p".into())),
        (KeyCode::KeyR, Key::Character("r".into())),
        (KeyCode::Space, Key::Space),
    ] {
        tap_key(app, window, code, key);
    }
    assert!(!app.world().resource::<HudState>().reset_confirm_open);
    assert_eq!(crate::session::queued_actions_for_tests(), queued);
    assert_eq!(
        crate::session::applied_physics().unwrap().requested,
        before.requested
    );
    tap_key(app, window, KeyCode::Escape, Key::Escape);
    assert_eq!(
        app.world().resource::<HudState>().panel,
        SidePanel::Ship,
        "Esc cancels a text edit before closing Ship"
    );

    let launch_tab = app
        .world_mut()
        .query::<(Entity, &super::model::TaskTab)>()
        .iter(app.world())
        .find_map(|(entity, tab)| (tab.0 == SidePanel::Launch).then_some(entity))
        .unwrap();
    activate(app, window, launch_tab);
    let world_toggle = app
        .world_mut()
        .query::<(Entity, &HudAction)>()
        .iter(app.world())
        .find_map(|(entity, action)| {
            (*action == HudAction::ToggleInstrumentSection(InstrumentGroup::World))
                .then_some(entity)
        })
        .unwrap();
    if !app
        .world()
        .resource::<HudState>()
        .expanded_instruments
        .contains(&InstrumentGroup::World)
    {
        activate(app, window, world_toggle);
    }
    let count = field(app, EditorField::WorldCount);
    for value in ["10000", "1e5"] {
        // Refocus through another native control to get a fresh select-all.
        let reload = action(app, "Create paused world, review replacement");
        app.world_mut()
            .resource_mut::<InputFocus>()
            .set(reload, FocusCause::Navigated);
        for _ in 0..3 {
            app.update();
        }
        enter_number(app, window, count, value);
        assert_eq!(
            crate::session::debris_dynamics().unwrap().physical_count,
            0,
            "typing a large count never replaces the world"
        );
    }
    assert_eq!(
        app.world()
            .get::<EditableText>(count)
            .unwrap()
            .value()
            .to_string(),
        "100000"
    );
    assert_eq!(crate::session::queued_actions_for_tests(), queued);
    assert_eq!(
        crate::session::applied_physics().unwrap().requested,
        before.requested,
        "typing e in 1e5 is not thrust"
    );
    assert_in_viewport(
        app.world(),
        count,
        viewport,
        "Native focus scrolling",
        "New world count",
    );
    assert!(!app.world().resource::<HudState>().reset_confirm_open);
    app.world_mut().write_message(bevy::window::Ime::Preedit {
        window,
        value: "500".into(),
        cursor: Some((3, 3)),
    });
    app.update();
    assert!(
        app.world()
            .get::<EditableText>(count)
            .unwrap()
            .is_composing()
    );
    tap_key(app, window, KeyCode::Enter, Key::Enter);
    assert_eq!(
        crate::session::debris_dynamics().unwrap().physical_count,
        0,
        "IME confirmation is not a world action"
    );
    app.world_mut().write_message(bevy::window::Ime::Commit {
        window,
        value: "500".into(),
    });
    app.update();
    tap_key(app, window, KeyCode::Enter, Key::Enter);
    assert_eq!(
        app.world()
            .get::<EditableText>(count)
            .unwrap()
            .value()
            .to_string(),
        "500"
    );
    assert_eq!(crate::session::debris_dynamics().unwrap().physical_count, 0);
    let preset = action(app, "Select Mars-facing L1 for a new world");
    activate(app, window, preset);
    assert_eq!(
        crate::snapshot().flight_preset,
        FlightPreset::FarSideL2,
        "selecting a preset only stages it"
    );
    enter_number(app, window, count, "3");
    let impact = action(app, "Select impact ejecta for a new world");
    activate(app, window, impact);
    let before_review = crate::snapshot();
    let review = action(app, "Create paused world, review replacement");
    activate(app, window, review);
    assert!(
        std::rc::Rc::ptr_eq(&before_review, &crate::snapshot()),
        "reviewing replacement does not reset the world"
    );
    let keep = action(app, "Keep current run and cancel world creation");
    activate(app, window, keep);
    assert!(std::rc::Rc::ptr_eq(&before_review, &crate::snapshot()));
    activate(app, window, review);
    let create = action(app, "Replace run at time zero with the drafted world");
    activate(app, window, create);
    let created = crate::snapshot();
    assert_eq!(created.flight_preset, FlightPreset::MarsFacingL1);
    assert!(matches!(
        created.experiment_settings.unwrap().debris.distribution,
        DebrisDistribution::ImpactEjecta { .. }
    ));
    assert_eq!(crate::session::debris_dynamics().unwrap().physical_count, 3);
    assert!(!crate::session::is_running());
    assert_eq!(created.ship_tuning().thrust_scale, 1.0);

    let placement = app
        .world_mut()
        .query::<(Entity, &HudAction)>()
        .iter(app.world())
        .find_map(|(entity, action)| {
            matches!(
                action,
                HudAction::ToggleReference(super::model::ReferenceSection::LanderPlacement)
            )
            .then_some(entity)
        })
        .unwrap();
    activate(app, window, placement);
    let x = field(app, EditorField::Placement(StreamAxis::X));
    let y = field(app, EditorField::Placement(StreamAxis::Y));
    let z = field(app, EditorField::Placement(StreamAxis::Z));
    let old_position = crate::session::applied_physics().unwrap().position_world_m;
    enter_number(app, window, x, "20000");
    enter_number(app, window, y, "100");
    enter_number(app, window, z, "0");
    assert_eq!(
        crate::session::applied_physics().unwrap().position_world_m,
        old_position
    );
    let place = action(
        app,
        "Apply XYZ position to the lander only and pause, debug-only",
    );
    activate(app, window, place);
    let placed = crate::session::applied_physics().unwrap();
    assert_eq!(placed.position_world_m, [20000.0, 100.0, 0.0]);
    assert_eq!(placed.velocity_world_mps, [0.0; 3]);
    assert_eq!(placed.angular_velocity_world_rad_s, [0.0; 3]);
    assert!(placed.requested.is_zero());
    assert_eq!(
        crate::snapshot().manual_interventions,
        created.manual_interventions + 1
    );
    assert!(!crate::session::is_running());

    crate::session::dispatch(crate::api::Dispatch::Physics(PhysicsCommand::Restore {
        state: Box::new(saved.as_ref().clone()),
    }))
    .unwrap();
    app.world_mut().resource_mut::<HudState>().panel = SidePanel::Closed;
    app.world_mut().resource_mut::<InputFocus>().clear();
    for _ in 0..4 {
        app.update();
    }
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

    for instruments in [true, false] {
        let disclosure = app
            .world_mut()
            .query::<(Entity, &HudAction, &InheritedVisibility)>()
            .iter(app.world())
            .find_map(|(entity, action, visible)| {
                let requested = if instruments {
                    matches!(action, HudAction::ToggleInstruments)
                } else {
                    matches!(action, HudAction::ToggleCamera)
                };
                (requested && visible.get()).then_some(entity)
            })
            .unwrap();
        app.world_mut()
            .resource_mut::<InputFocus>()
            .set(disclosure, FocusCause::Navigated);
        tap_key(app, window, KeyCode::Enter, Key::Enter);
        assert!(controls::binding_capture_active());
        assert_eq!(
            app.world().resource::<HudState>().panel,
            super::model::SidePanel::Controls,
            "disclosures cannot act through a binding capture"
        );
    }

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
    assert_eq!(label.0, "Front: B | change");
    assert_eq!(
        app.world()
            .get::<bevy::a11y::AccessibilityNode>(front)
            .unwrap()
            .label(),
        Some("Front: B | change")
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
    fn refocus(app: &mut App, entity: Entity) {
        app.world_mut().resource_mut::<InputFocus>().clear();
        app.update();
        app.world_mut()
            .resource_mut::<InputFocus>()
            .set(entity, FocusCause::Navigated);
        for _ in 0..3 {
            app.update();
        }
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
        "one native notebook and one disclosed fullscreen button"
    );
    for (entity, button) in buttons {
        if button == PlatformButton::Notebook {
            assert_eq!(
                app.world().get::<AccessibleLabel>(entity).unwrap().0,
                "Notebook | new tab"
            );
        }
        app.world_mut().resource_mut::<HudState>().panel = super::model::SidePanel::Camera;
        for _ in 0..3 {
            app.update();
        }
        app.world_mut()
            .resource_mut::<InputFocus>()
            .set(entity, FocusCause::Navigated);
        for _ in 0..3 {
            app.update();
        }
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
        refocus(app, entity);
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
        refocus(app, entity);
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
    let fullscreen = app
        .world_mut()
        .query::<(Entity, &PlatformButton)>()
        .iter(app.world())
        .find_map(|(entity, button)| (*button == PlatformButton::Fullscreen).then_some(entity))
        .unwrap();
    let old_fullscreen_point = center(app, fullscreen, window_scale);
    app.world_mut().resource_mut::<HudState>().panel = super::model::SidePanel::Closed;
    for _ in 0..3 {
        app.update();
    }
    assert!(!super::fullscreen_button_focused());
    assert!(
        !super::fullscreen_button_at(old_fullscreen_point.x, old_fullscreen_point.y),
        "closing Views must remove its trusted hit target, not just its pixels"
    );
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
                Readout::Instrument(Instrument::Flight(_)) => "flight instrument",
                Readout::Playback => "Run label",
                Readout::InstrumentsToggle => "Flight label",
                Readout::ShipToggle => "Ship label",
                Readout::DebrisToggle => "Debris label",
                Readout::LaunchToggle => "Launch label",
                Readout::CameraToggle => "Views label",
                Readout::ControlsToggle => "Controls label",
                Readout::Notebook => "Notebook label",
                _ => return None,
            };
            world
                .get::<InheritedVisibility>(entity)
                .unwrap()
                .get()
                .then_some((entity, label))
        })
        .collect();
    assert_eq!(
        readouts.len(),
        15,
        "six measurements, status, Play, Notebook and six task labels must exist"
    );
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
        if matches!(
            world.get::<Readout>(entity),
            Some(Readout::Playback | Readout::Notebook)
        ) {
            assert!(
                text_layout.glyphs.iter().all(|glyph| glyph.line_index == 0),
                "{stage}: {label} must retain its intact intrinsic label"
            );
        }
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
            HudAction::ToggleInstruments => Some((entity, "Flight button")),
            HudAction::ToggleShip => Some((entity, "Ship button")),
            HudAction::ToggleDebris => Some((entity, "Debris button")),
            HudAction::ToggleLaunch => Some((entity, "Launch button")),
            HudAction::ToggleCamera => Some((entity, "Views button")),
            HudAction::ToggleControls => Some((entity, "Controls button")),
            HudAction::FollowLander => Some((entity, "Follow button")),
            HudAction::Notebook => Some((entity, "Notebook button")),
            _ => None,
        })
        .filter(|(entity, _)| world.get::<InheritedVisibility>(*entity).unwrap().get())
        .collect();
    assert_eq!(
        buttons.len(),
        9,
        "the main controls and all six task tabs must stay reachable"
    );
    let button_rects: Vec<_> = buttons
        .into_iter()
        .map(|(entity, label)| {
            let rect = assert_in_viewport(world, entity, viewport, stage, label);
            assert!(
                rect.size().cmpge(Vec2::splat(24.0 * scale)).all(),
                "{label} must retain its minimum hit area"
            );
            assert_button_glyphs_fit(world, entity, label);
            rect
        })
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
    let visible_metrics: Vec<_> = world
        .query::<(&Readout, &InheritedVisibility)>()
        .iter(world)
        .filter_map(|(readout, visible)| match readout {
            Readout::Instrument(Instrument::Flight(metric)) if visible.get() => Some(*metric),
            _ => None,
        })
        .collect();
    assert_eq!(visible_metrics.len(), FlightMetric::STRIP.len());
    for metric in FlightMetric::STRIP {
        assert_eq!(
            visible_metrics
                .iter()
                .filter(|&&value| value == metric)
                .count(),
            1,
            "{metric:?} must appear once in the default flight strip"
        );
    }
    for (action, visibility) in world
        .query::<(&HudAction, &InheritedVisibility)>()
        .iter(world)
    {
        if matches!(
            action,
            HudAction::ChangeCamera(_) | HudAction::DebugUpright | HudAction::AskReset
        ) {
            assert!(
                !visibility.get(),
                "camera selection and debug actions stay disclosed"
            );
        }
    }
    for (readout, visibility) in world
        .query::<(&Readout, &InheritedVisibility)>()
        .iter(world)
    {
        if matches!(
            readout,
            Readout::Mission
                | Readout::Environment
                | Readout::TelemetryDetails
                | Readout::InputSettings
                | Readout::FlightControls
                | Readout::PathDetails
                | Readout::Inspector
        ) {
            assert!(
                !visibility.get(),
                "metadata, help and caveats stay out of the default HUD"
            );
        }
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
            .is_none_or(|clip| corners.into_iter().all(|point| clip.contains_point(point))),
        "{stage}: {label} {entity:?} is clipped at {rect:?}"
    );
    rect
}

fn assert_text_glyphs_fit(world: &World, entity: Entity, label: &str) -> u32 {
    let layout = world
        .get::<TextLayoutInfo>(entity)
        .expect("production text is shaped by TextPlugin");
    let content = world
        .get::<ComputedNode>(entity)
        .unwrap()
        .content_box()
        .size();
    assert!(
        !layout.glyphs.is_empty(),
        "{label}: real glyphs are required"
    );
    assert!(
        layout.size.is_finite() && layout.size.cmple(content + Vec2::ONE).all(),
        "{label}: shaped text {:?} escapes its computed content box {content:?}",
        layout.size
    );
    for glyph in &layout.glyphs {
        let half = glyph.atlas_info.rect.size() * 0.5;
        let min = glyph.position - half;
        let max = glyph.position + half;
        assert!(
            min.is_finite()
                && max.is_finite()
                && min.cmpge(-Vec2::ONE).all()
                && max.cmple(content + Vec2::ONE).all(),
            "{label}: rendered glyph bounds {min:?}..{max:?} escape content {content:?}"
        );
    }
    layout
        .glyphs
        .iter()
        .map(|glyph| glyph.line_index)
        .max()
        .unwrap()
        + 1
}

fn assert_button_glyphs_fit(world: &World, button: Entity, label: &str) {
    let child = world
        .get::<Children>(button)
        .unwrap()
        .iter()
        .find(|child| world.get::<Text>(*child).is_some())
        .expect("a native action has a text label");
    assert_text_glyphs_fit(world, child, label);
    let text_node = world.get::<ComputedNode>(child).unwrap();
    let text_transform = world.get::<UiGlobalTransform>(child).unwrap();
    let button_node = world.get::<ComputedNode>(button).unwrap();
    let inverse = world
        .get::<UiGlobalTransform>(button)
        .unwrap()
        .try_inverse()
        .unwrap();
    let content = button_node.content_box().inflate(1.0);
    for glyph in &world.get::<TextLayoutInfo>(child).unwrap().glyphs {
        let half = glyph.atlas_info.rect.size() * 0.5;
        for point in [glyph.position - half, glyph.position + half] {
            let in_button = inverse.transform_point2(
                text_transform.transform_point2(text_node.content_box().min + point),
            );
            assert!(
                content.contains(in_button),
                "{label}: actual glyph {in_button:?} escapes the padded button content box {content:?}"
            );
        }
    }
}
