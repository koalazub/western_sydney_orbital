//! CPU geometry, state transitions and actual Bevy input systems. No GPU claims.
use super::{
    BRIDGE, Bridge, LaunchpadAction, SurfaceStep, frame_direction,
    geometry::{self, Ray, Terrain},
    gesture_active, input_blocked, install,
    model::{ActorIndices, Phase, Placement, Pointer, Preview, Source},
    request, reset_for_tests,
};
use crate::{
    domain::{EngineAction, EntityId, ShipCameraId},
    physics::Simulation,
};
use bevy::{
    camera::{
        CameraProjection, ComputedCameraValues, RenderTarget, RenderTargetInfo, Viewport,
        visibility::RenderLayers,
    },
    gizmos::{
        config::{DefaultGizmoConfigGroup, GizmoConfig, GizmoConfigStore},
        gizmos::GizmoStorage,
    },
    input_focus::{FocusCause, InputFocus},
    math::DVec3,
    picking::pointer::{
        Location, PointerAction, PointerButton, PointerId, PointerInput, PointerLocation,
        PointerMap,
    },
    prelude::*,
    window::{PrimaryWindow, WindowRef},
};

fn real_simulation() -> Simulation {
    let mut settings = crate::model::ExperimentSettings::default();
    settings.debris.count = 0;
    Simulation::with_experiment(19, settings).unwrap()
}

fn anchors(snapshot: &crate::model::Snapshot) -> ActorIndices {
    let mut indices = ActorIndices::default();
    indices.refresh(snapshot, 7);
    indices
}

fn initial(snapshot: &crate::model::Snapshot) -> (Source, Preview) {
    let indices = anchors(snapshot);
    (
        Source::capture(snapshot, indices, 7, false).unwrap(),
        geometry::actual_preview(snapshot, indices).unwrap(),
    )
}

/// Construct real Bevy projection matrices for a known logical viewport. This
/// exercises Camera conversion on the CPU; it does not fabricate GPU readiness.
fn camera(
    site: crate::terrain::LandingSite,
    scale: f32,
    orthographic: bool,
    hidden: bool,
) -> (Camera, GlobalTransform, Projection) {
    let point = DVec3::from_array(site.pad_position_m);
    let outward = point.normalize();
    let position = if hidden {
        -outward * 50_000.0
    } else {
        point + outward * 30_000.0
    };
    let up = if outward.y.abs() > 0.95 {
        Vec3::Z
    } else {
        Vec3::Y
    };
    let transform = Transform::from_translation(geometry::render_position(position))
        .looking_at(geometry::render_position(point), up);
    let projection = if orthographic {
        Projection::Orthographic(OrthographicProjection {
            near: 0.0005,
            far: 1000.0,
            area: Rect::new(-20.0, -15.0, 20.0, 15.0),
            ..OrthographicProjection::default_3d()
        })
    } else {
        Projection::Perspective(PerspectiveProjection {
            near: 0.0005,
            far: 1000.0,
            aspect_ratio: 800.0 / 600.0,
            ..default()
        })
    };
    let camera = Camera {
        viewport: Some(Viewport {
            physical_position: UVec2::new((40.0 * scale) as u32, (20.0 * scale) as u32),
            physical_size: UVec2::new((800.0 * scale) as u32, (600.0 * scale) as u32),
            ..default()
        }),
        computed: ComputedCameraValues {
            clip_from_view: CameraProjection::get_clip_from_view(&*projection),
            target_info: Some(RenderTargetInfo {
                physical_size: UVec2::new((840.0 * scale) as u32, (620.0 * scale) as u32),
                scale_factor: scale,
            }),
            ..default()
        },
        ..default()
    };
    (camera, GlobalTransform::from(transform), projection)
}

#[test]
fn terrain_rays_hit_real_mesh_and_reject_sky_and_far_side_pad() {
    let direction = DVec3::new(-1.0, 0.2, 0.3).normalize();
    let site = crate::terrain::landing_site_at_direction(direction.to_array()).unwrap();
    let hit = Terrain::intersect(Ray {
        origin: direction * 60_000.0,
        direction: -direction,
    })
    .unwrap();
    assert!(
        hit.position_m
            .distance(DVec3::from_array(site.surface_position_m))
            < 1.0e-7
    );
    assert!((hit.distance_m - (60_000.0 - hit.position_m.length())).abs() < 1.0e-7);
    assert!(hit.normal.dot(hit.position_m) > 0.0);
    assert!(
        Terrain::intersect(Ray {
            origin: DVec3::new(60_000.0, 0.0, 0.0),
            direction: DVec3::Y
        })
        .is_none()
    );
    let (camera, global, _) = camera(site, 2.0, false, true);
    let at = camera
        .world_to_viewport(
            &global,
            geometry::render_position(DVec3::from_array(site.pad_position_m)),
        )
        .unwrap();
    let ray = geometry::viewport_ray(&camera, &global, at).unwrap();
    assert!(!geometry::pad_hit(ray, site));
    assert!(geometry::grip(&camera, &global, site).is_none());
}

#[test]
fn actual_bevy_projection_and_grip_keep_css_targets_at_both_dpis_and_projection_types() {
    let snapshot = real_simulation().snapshot();
    let (_, initial) = initial(&snapshot);
    for orthographic in [false, true] {
        let mut logical_grip = None;
        for scale in [1.0, 2.0] {
            let (camera, global, _) = camera(initial.site, scale, orthographic, false);
            let grip = geometry::grip(&camera, &global, initial.site).unwrap();
            if let Some(previous) = logical_grip {
                assert!(grip.handle_css.distance(previous) < 1.0e-3);
            }
            logical_grip = Some(grip.handle_css);
            assert!(grip.hit(grip.handle_css + Vec2::splat(12.0)));
            assert!(!grip.hit(grip.handle_css + Vec2::X * 30.0));
            assert!(2.0 * geometry::TARGET_HALF_PX >= 24.0);
            let projected = camera
                .world_to_viewport(&global, geometry::render_position(grip.handle_m))
                .unwrap();
            assert!(projected.distance(grip.handle_css) < 1.0e-3);
            let projected_pad = camera
                .world_to_viewport(&global, geometry::render_position(grip.pad_m))
                .unwrap();
            let ray = geometry::viewport_ray(&camera, &global, projected_pad).unwrap();
            assert!(geometry::pad_hit(ray, initial.site));
            let preview =
                geometry::surface_preview(&camera, &global, projected_pad + Vec2::X * 25.0)
                    .unwrap();
            let mesh_site = crate::terrain::landing_site_at_direction(preview.direction).unwrap();
            assert_eq!(
                preview.site.surface_position_m,
                mesh_site.surface_position_m
            );
            assert!(DVec3::from_array(preview.site.surface_position_m).length() > 5000.0);
            assert!(geometry::viewport_ray(&camera, &global, Vec2::new(0.0, 0.0)).is_none());
        }
    }
}

#[test]
fn dragging_a_lift_grip_or_corner_preserves_its_pickup_offset_in_real_projection() {
    let snapshot = real_simulation().snapshot();
    let (source, initial) = initial(&snapshot);
    for orthographic in [false, true] {
        for scale in [1.0, 2.0] {
            let (camera, global, _) = camera(initial.site, scale, orthographic, false);
            let grip = geometry::grip(&camera, &global, initial.site).unwrap();
            let anchor = camera
                .world_to_viewport(
                    &global,
                    geometry::render_position(DVec3::from_array(initial.site.surface_position_m)),
                )
                .unwrap();
            for press in [grip.handle_css, grip.pad_css + Vec2::new(5.0, 3.0)] {
                let mut state = Placement::default();
                state.start_drag(
                    source,
                    initial,
                    Pointer::Mouse,
                    press.to_array(),
                    anchor.to_array(),
                );
                let delta = Vec2::new(18.0, 7.0);
                let surface_cursor =
                    Vec2::from_array(state.surface_cursor((press + delta).to_array()));
                assert!(surface_cursor.distance(anchor + delta) < 1.0e-4);
                let preview = geometry::surface_preview(&camera, &global, surface_cursor).unwrap();
                let projected = camera
                    .world_to_viewport(
                        &global,
                        geometry::render_position(DVec3::from_array(
                            preview.site.surface_position_m,
                        )),
                    )
                    .unwrap();
                assert!(projected.distance(anchor + delta) < 0.01);
            }
        }
    }
}

#[test]
fn nudge_steps_follow_contact_mesh_including_both_poles_without_writing_the_world() {
    let simulation = real_simulation();
    let before = serde_json::to_value(simulation.snapshot()).unwrap();
    for direction in [DVec3::Y, DVec3::NEG_Y, DVec3::X, DVec3::NEG_X, DVec3::Z] {
        let initial = Preview {
            direction: direction.to_array(),
            site: crate::terrain::landing_site_at_direction(direction.to_array()).unwrap(),
        };
        for step in [
            SurfaceStep::North,
            SurfaceStep::South,
            SurfaceStep::East,
            SurfaceStep::West,
        ] {
            let next = geometry::nudge(initial, step).unwrap();
            let radial = DVec3::from_array(next.direction);
            assert!(radial.is_finite());
            assert!((radial.length() - 1.0).abs() < 1.0e-12);
            let distance = DVec3::from_array(next.site.surface_position_m)
                .distance(DVec3::from_array(initial.site.surface_position_m));
            assert!(
                (80.0..130.0).contains(&distance),
                "{direction:?} {step:?}: {distance}"
            );
            let projected = crate::terrain::landing_site_at_direction(next.direction).unwrap();
            assert_eq!(next.site.surface_position_m, projected.surface_position_m);
            assert_eq!(next.site.normal, projected.normal);
        }
    }
    assert_eq!(serde_json::to_value(simulation.snapshot()).unwrap(), before);
}

#[test]
fn drag_owns_only_its_pointer_and_consumes_one_commit_intent_before_dispatch() {
    let snapshot = real_simulation().snapshot();
    let (source, initial) = initial(&snapshot);
    let next = geometry::nudge(initial, SurfaceStep::East).unwrap();
    let mut state = Placement::default();
    state.start_drag(
        source,
        initial,
        Pointer::Mouse,
        [400.0, 300.0],
        [400.0, 300.0],
    );
    state.move_drag(Pointer::Touch(4), [450.0, 300.0], Some(next));
    assert!(state.finish_drag(Pointer::Touch(4), true).is_none());
    state.move_drag(Pointer::Mouse, [450.0, 300.0], Some(next));
    let operation = state.finish_drag(Pointer::Mouse, true).unwrap();
    assert_eq!(operation.direction, next.direction);
    assert_eq!(operation.expected.trial_revision, 7);
    assert!(state.finish_drag(Pointer::Mouse, true).is_none());
    assert!(state.take_commit().is_none());
    assert!(!state.active());
    assert!(state.blocked());
    state.begin_frame(true, false);
    assert!(state.blocked());
    state.begin_frame(false, true);
    assert!(
        state.blocked(),
        "the final key/pointer-up frame must stay consumed"
    );
    state.begin_frame(true, false);
    assert!(!state.blocked());

    state.start_drag(
        source,
        initial,
        Pointer::Mouse,
        [400.0, 300.0],
        [400.0, 300.0],
    );
    state.move_drag(Pointer::Mouse, [450.0, 300.0], Some(next));
    assert!(state.finish_drag(Pointer::Mouse, true).is_some());
    state.end_frame(false);
    assert!(state.blocked());
    state.begin_frame(true, false);
    assert!(
        !state.blocked(),
        "a fresh press after an observed terminal release can work"
    );
}

#[test]
fn click_invalid_release_and_interruption_clear_preview_without_a_commit_intent() {
    let snapshot = real_simulation().snapshot();
    let (source, initial) = initial(&snapshot);
    for moved in [false, true] {
        let mut state = Placement::default();
        state.start_drag(
            source,
            initial,
            Pointer::Mouse,
            [400.0, 300.0],
            [400.0, 300.0],
        );
        if moved {
            state.move_drag(Pointer::Mouse, [450.0, 300.0], None);
        }
        assert!(state.finish_drag(Pointer::Mouse, true).is_none());
        assert!(state.edit().is_none());
        assert!(state.blocked());
    }
    for reason in [
        "Escape",
        "Focus lost",
        "Pointer cancelled",
        "Modal opened",
        "Flight changed",
    ] {
        let mut state = Placement::default();
        state.arm(source);
        state.nudge(SurfaceStep::North, initial).unwrap();
        state.cancel(reason);
        assert!(state.edit().is_none());
        assert!(state.take_commit().is_none());
        assert!(state.blocked());
    }
    let mut blocked_ui_release = Placement::default();
    blocked_ui_release.start_drag(
        source,
        initial,
        Pointer::Mouse,
        [400.0, 300.0],
        [400.0, 300.0],
    );
    blocked_ui_release.move_drag(Pointer::Mouse, [450.0, 300.0], Some(initial));
    assert!(
        blocked_ui_release
            .finish_drag(Pointer::Mouse, false)
            .is_none()
    );
    assert!(blocked_ui_release.edit().is_none());
}

#[test]
fn source_guard_detects_real_time_pose_and_tuning_changes_as_well_as_revision_and_playback() {
    let mut simulation = real_simulation();
    let snapshot = simulation.snapshot();
    let (source, _) = initial(&snapshot);
    assert!(source.matches(&snapshot, 7, false));
    assert!(!source.matches(&snapshot, 8, false));
    assert!(!source.matches(&snapshot, 7, true));
    let next = simulation
        .execute(crate::model::PhysicsCommand::Step { seconds: 0.05 })
        .unwrap();
    assert!(!source.matches(&next, 7, false));
    let updated = Source::capture(&next, anchors(&next), 7, false).unwrap();
    let mut tuning = next.ship_tuning();
    tuning.mass_kg += 1.0;
    let tuned = simulation
        .execute(crate::model::PhysicsCommand::TuneShip { settings: tuning })
        .unwrap();
    assert!(!updated.matches(&tuned, 7, false));
}

#[test]
fn actual_custom_anchor_comes_from_the_real_moved_pad_and_stored_direction() {
    let mut simulation = real_simulation();
    let direction = DVec3::new(-1.0, 0.15, 0.2).normalize().to_array();
    let moved = simulation
        .execute(crate::model::PhysicsCommand::MoveLaunchpad {
            surface_direction: direction,
        })
        .unwrap();
    let indices = anchors(&moved);
    let preview = geometry::actual_preview(&moved, indices).unwrap();
    let pad = &moved.assets[indices.pad.unwrap()];
    assert_eq!(pad.id, EntityId::LandingPad);
    assert_eq!(preview.site.pad_position_m, pad.position);
    assert!(
        DVec3::from_array(preview.direction)
            .distance(DVec3::from_array(moved.launch_site_direction.unwrap()))
            < 1.0e-14
    );
    let nudge = geometry::nudge(preview, SurfaceStep::North).unwrap();
    assert!(
        DVec3::from_array(nudge.site.pad_position_m).distance(DVec3::from_array(pad.position))
            > 80.0
    );
}

struct RestoreBridge(Bridge);

impl RestoreBridge {
    fn fresh() -> Self {
        Self(BRIDGE.with(|bridge| bridge.replace(Bridge::default())))
    }
}

impl Drop for RestoreBridge {
    fn drop(&mut self) {
        BRIDGE.with(|bridge| *bridge.borrow_mut() = std::mem::take(&mut self.0));
        crate::controls::reset_for_tests();
        crate::controls::set_movement_keys(0);
        crate::hud::reset_instrument_editing_for_tests();
    }
}

#[test]
fn request_is_intent_only_and_reset_clears_every_thread_local_launchpad_gate() {
    let _restore = RestoreBridge::fresh();
    let before = serde_json::to_value(crate::snapshot().as_ref()).unwrap();
    let actions = crate::session::queued_actions_for_tests();
    let revision = crate::session::trial_revision();
    request(LaunchpadAction::Nudge(SurfaceStep::East));
    assert!(input_blocked() && gesture_active());
    assert!(frame_direction().is_none());
    assert_eq!(crate::session::queued_actions_for_tests(), actions);
    assert_eq!(crate::session::trial_revision(), revision);
    assert_eq!(
        serde_json::to_value(crate::snapshot().as_ref()).unwrap(),
        before
    );
    request(LaunchpadAction::Confirm);
    request(LaunchpadAction::Cancel);
    BRIDGE.with(|bridge| {
        assert_eq!(
            bridge.borrow().actions.iter().copied().collect::<Vec<_>>(),
            vec![LaunchpadAction::Cancel]
        );
    });
    reset_for_tests();
    assert!(!input_blocked() && !gesture_active());
    BRIDGE.with(|bridge| {
        assert!(bridge.borrow().actions.is_empty() && bridge.borrow().status.is_empty())
    });
}

fn input_app(sensors: bool) -> (App, Entity, Entity) {
    crate::controls::reset_for_tests();
    crate::controls::set_movement_keys(0);
    crate::hud::reset_instrument_editing_for_tests();
    crate::camera::set_input_active(true, false);
    let mut config = GizmoConfigStore::default();
    config.insert(
        GizmoConfig {
            render_layers: RenderLayers::layer(1),
            ..default()
        },
        DefaultGizmoConfigGroup,
    );
    let mut app = App::new();
    app.init_resource::<Time>()
        .init_resource::<Time<Real>>()
        .init_resource::<UiScale>()
        .init_resource::<Assets<Image>>()
        .init_resource::<InputFocus>()
        .init_resource::<ButtonInput<KeyCode>>()
        .init_resource::<ButtonInput<MouseButton>>()
        .init_resource::<crate::hud::HudState>()
        .init_resource::<crate::PlaybackState>()
        .init_resource::<crate::camera::CameraRig>()
        .init_resource::<PointerMap>()
        .init_resource::<GizmoStorage<DefaultGizmoConfigGroup, ()>>()
        .insert_resource(config)
        .configure_sets(
            Update,
            (
                crate::EngineSet::Input,
                crate::EngineSet::Runtime,
                crate::EngineSet::Scene,
                crate::EngineSet::Hud,
            )
                .chain(),
        )
        .add_systems(
            Update,
            crate::session::advance_world.in_set(crate::EngineSet::Runtime),
        );
    if sensors {
        crate::sensors::install(&mut app);
    }
    // Real runtime setup, before the measured input flow. Drain inherited native
    // actions first, then pause on an available queue. No mocked session backend.
    app.update();
    crate::request_action(EngineAction::Pause);
    app.update();
    assert!(!crate::session::is_running());
    let mut settings = crate::model::ExperimentSettings::default();
    settings.debris.count = 0;
    crate::session::dispatch(crate::api::Dispatch::Physics(
        crate::model::PhysicsCommand::ConfigureFlight {
            settings,
            seed: Some(19),
            preset: crate::model::FlightPreset::FarSideL2,
        },
    ))
    .unwrap();
    app.update();
    let snapshot = crate::snapshot();
    let (_, initial) = initial(&snapshot);
    let window = app
        .world_mut()
        .spawn((
            Window {
                focused: true,
                resolution: bevy::window::WindowResolution::new(1680, 1240)
                    .with_scale_factor_override(2.0),
                ..default()
            },
            PrimaryWindow,
        ))
        .id();
    let (camera, global, projection) = camera(initial.site, 2.0, false, false);
    let camera = app
        .world_mut()
        .spawn((
            crate::camera::SimulationCamera,
            Camera3d::default(),
            camera,
            global,
            Transform::from_matrix(global.to_matrix()),
            projection,
            RenderTarget::Window(WindowRef::Entity(window)),
            RenderLayers::layer(0).with(1),
        ))
        .id();
    let target = RenderTarget::Window(WindowRef::Entity(window))
        .normalize(Some(window))
        .unwrap();
    app.world_mut().spawn((
        PointerId::Mouse,
        PointerLocation::new(Location {
            target,
            position: Vec2::new(440.0, 320.0),
        }),
    ));
    install(&mut app);
    app.add_systems(PreUpdate, PointerInput::receive);
    (app, window, camera)
}

fn pointer_frame(app: &mut App, window: Entity, position: Vec2, action: PointerAction) {
    app.world_mut()
        .resource_mut::<ButtonInput<KeyCode>>()
        .clear();
    app.world_mut()
        .resource_mut::<ButtonInput<MouseButton>>()
        .clear();
    app.world_mut()
        .get_mut::<Window>(window)
        .unwrap()
        .set_cursor_position(Some(position));
    match action {
        PointerAction::Press(PointerButton::Primary) => {
            app.world_mut()
                .resource_mut::<ButtonInput<MouseButton>>()
                .press(MouseButton::Left);
        }
        PointerAction::Release(PointerButton::Primary) => {
            app.world_mut()
                .resource_mut::<ButtonInput<MouseButton>>()
                .release(MouseButton::Left);
        }
        _ => {}
    }
    let target = RenderTarget::Window(WindowRef::Entity(window))
        .normalize(Some(window))
        .unwrap();
    app.world_mut().write_message(PointerInput::new(
        PointerId::Mouse,
        Location { target, position },
        action,
    ));
    app.update();
}

#[test]
fn scheduled_native_drag_previews_then_escape_cancels_without_touching_the_session() {
    let _restore = RestoreBridge::fresh();
    let (mut app, window, camera) = input_app(false);
    let before = serde_json::to_value(crate::snapshot().as_ref()).unwrap();
    let revision = crate::session::trial_revision();
    let current = crate::snapshot();
    let (_, initial) = initial(&current);
    let grip = geometry::grip(
        app.world().get::<Camera>(camera).unwrap(),
        app.world().get::<GlobalTransform>(camera).unwrap(),
        initial.site,
    )
    .unwrap();
    pointer_frame(
        &mut app,
        window,
        grip.pad_css,
        PointerAction::Press(PointerButton::Primary),
    );
    assert!(matches!(
        app.world().resource::<Placement>().phase,
        Phase::Dragging { .. }
    ));
    pointer_frame(
        &mut app,
        window,
        grip.pad_css + Vec2::X * 25.0,
        PointerAction::Move {
            delta: Vec2::X * 25.0,
        },
    );
    assert!(
        app.world()
            .resource::<Placement>()
            .edit()
            .unwrap()
            .preview
            .is_some()
    );
    assert!(gesture_active() && input_blocked());
    app.world_mut()
        .resource_mut::<ButtonInput<KeyCode>>()
        .press(KeyCode::Escape);
    app.update();
    assert!(app.world().resource::<Placement>().edit().is_none());
    assert!(
        input_blocked(),
        "Escape must consume its held/release frames"
    );
    assert_eq!(crate::session::trial_revision(), revision);
    assert_eq!(
        serde_json::to_value(crate::snapshot().as_ref()).unwrap(),
        before
    );
}

#[test]
fn scheduled_lift_grip_drag_commits_both_real_bodies_once_on_release() {
    let _restore = RestoreBridge::fresh();
    let (mut app, window, camera) = input_app(false);
    let before = crate::snapshot();
    let revision = crate::session::trial_revision();
    let (_, initial) = initial(&before);
    let grip = geometry::grip(
        app.world().get::<Camera>(camera).unwrap(),
        app.world().get::<GlobalTransform>(camera).unwrap(),
        initial.site,
    )
    .unwrap();
    pointer_frame(
        &mut app,
        window,
        grip.handle_css,
        PointerAction::Press(PointerButton::Primary),
    );
    assert!(matches!(
        app.world().resource::<Placement>().phase,
        Phase::Dragging { .. }
    ));
    let destination = grip.handle_css + Vec2::X * 25.0;
    pointer_frame(
        &mut app,
        window,
        destination,
        PointerAction::Move {
            delta: Vec2::X * 25.0,
        },
    );
    let preview = app
        .world()
        .resource::<Placement>()
        .edit()
        .unwrap()
        .preview
        .unwrap();
    assert_eq!(crate::session::trial_revision(), revision);
    assert_eq!(
        serde_json::to_value(crate::snapshot().as_ref()).unwrap(),
        serde_json::to_value(before.as_ref()).unwrap()
    );
    pointer_frame(
        &mut app,
        window,
        destination,
        PointerAction::Release(PointerButton::Primary),
    );
    let after = crate::snapshot();
    assert_eq!(after.manual_interventions, before.manual_interventions + 1);
    assert_eq!(crate::session::trial_revision(), revision + 1);
    assert_eq!(after.time_s, before.time_s);
    assert_eq!(after.rng_state, before.rng_state);
    assert!(!crate::session::is_running());
    let physics = crate::session::applied_physics().unwrap();
    assert_eq!(physics.position_world_m, preview.site.lander_position_m);
    assert_eq!(physics.velocity_world_mps, [0.0; 3]);
    assert_eq!(physics.angular_velocity_world_rad_s, [0.0; 3]);
    let pad = after
        .assets
        .iter()
        .find(|asset| asset.id == EntityId::LandingPad)
        .unwrap();
    assert_eq!(pad.position, preview.site.pad_position_m);
    pointer_frame(
        &mut app,
        window,
        destination,
        PointerAction::Release(PointerButton::Primary),
    );
    assert_eq!(crate::session::trial_revision(), revision + 1);
    assert_eq!(
        crate::snapshot().manual_interventions,
        before.manual_interventions + 1
    );
}

#[test]
fn scheduled_text_focus_cancels_placement_without_hijacking_enter_or_arrows() {
    let _restore = RestoreBridge::fresh();
    let (mut app, _, _) = input_app(false);
    let before = serde_json::to_value(crate::snapshot().as_ref()).unwrap();
    request(LaunchpadAction::Nudge(SurfaceStep::North));
    app.update();
    assert!(matches!(
        app.world().resource::<Placement>().phase,
        Phase::Armed(_)
    ));
    let editor = app.world_mut().spawn(crate::hud::InstrumentEditor).id();
    app.world_mut()
        .resource_mut::<InputFocus>()
        .set(editor, FocusCause::Navigated);
    for key in [KeyCode::ArrowUp, KeyCode::Enter, KeyCode::Escape] {
        app.world_mut()
            .resource_mut::<ButtonInput<KeyCode>>()
            .press(key);
    }
    app.update();
    assert!(app.world().resource::<Placement>().edit().is_none());
    assert_eq!(app.world().resource::<InputFocus>().get(), Some(editor));
    for key in [KeyCode::ArrowUp, KeyCode::Enter, KeyCode::Escape] {
        assert!(app.world().resource::<ButtonInput<KeyCode>>().pressed(key));
    }
    assert_eq!(
        serde_json::to_value(crate::snapshot().as_ref()).unwrap(),
        before
    );
}

#[test]
fn full_module_keeps_debug_gizmos_and_existing_mounted_camera_layers_separate() {
    let _restore = RestoreBridge::fresh();
    let (mut app, _, _) = input_app(true);
    let snapshot = crate::snapshot();
    let before = serde_json::to_value(snapshot.as_ref()).unwrap();
    let poses =
        [ShipCameraId::Top, ShipCameraId::Bottom].map(|id| crate::sensors::pose(id, &snapshot));
    let ui_count = {
        let world = app.world_mut();
        world.query::<&Node>().iter(world).count()
    };
    request(LaunchpadAction::Nudge(SurfaceStep::East));
    app.update();
    let config = app
        .world()
        .resource::<GizmoConfigStore>()
        .config::<DefaultGizmoConfigGroup>()
        .0;
    assert_eq!(config.render_layers, RenderLayers::layer(1));
    let world = app.world_mut();
    let sensor_layers: Vec<_> = world.query_filtered::<&RenderLayers, (With<Camera3d>, Without<crate::camera::SimulationCamera>)>()
        .iter(world).cloned().collect();
    assert_eq!(sensor_layers.len(), 2);
    assert!(
        sensor_layers
            .iter()
            .all(|layers| *layers == RenderLayers::layer(0).with(2))
    );
    assert_eq!(
        poses,
        [ShipCameraId::Top, ShipCameraId::Bottom]
            .map(|id| crate::sensors::pose(id, &crate::snapshot()))
    );
    assert_eq!(
        serde_json::to_value(crate::snapshot().as_ref()).unwrap(),
        before
    );
    let after_ui_count = {
        let world = app.world_mut();
        world.query::<&Node>().iter(world).count()
    };
    assert_eq!(after_ui_count, ui_count);
    assert!(app.get_sub_app(bevy::render::RenderApp).is_none());
}
