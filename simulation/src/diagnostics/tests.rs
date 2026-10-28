use super::{data::Diagnostics, install};
use crate::{
    domain::{EntityId, EntityKind},
    scene::AssetVisual,
};
use bevy::{
    asset::{AssetApp, AssetMetaCheck, AssetPlugin},
    diagnostic::FrameCount,
    gizmos::GizmoPlugin,
    input_focus::InputFocus,
    prelude::*,
    render::RenderApp,
};
use serde_json::{Value, json};
use std::rc::Rc;

fn report() -> Value {
    serde_json::from_str(&crate::presentation_diagnostics()).unwrap()
}

fn vector(value: &Value) -> [f64; 3] {
    std::array::from_fn(|axis| value[axis].as_f64().unwrap())
}

fn near(actual: [f64; 3], expected: [f64; 3], tolerance: f64) {
    for axis in 0..3 {
        assert!(
            (actual[axis] - expected[axis]).abs() < tolerance,
            "actual {actual:?}, expected {expected:?}"
        );
    }
}

fn metres(position: Vec3) -> [f64; 3] {
    position
        .to_array()
        .map(|component| f64::from(component) * 1_000.0)
}

fn own_entity(app: &mut App, id: EntityId) -> Entity {
    let world = app.world_mut();
    world
        .query::<(Entity, &AssetVisual)>()
        .iter(world)
        .find_map(|(entity, visual)| (visual.id == id).then_some(entity))
        .unwrap()
}

/// Real scene setup/sync and paused session runtime. There is deliberately no
/// RenderPlugin, device, presentation or mounted-camera capture in this fixture.
fn scene_app() -> App {
    let mut app = App::new();
    app.add_plugins((
        MinimalPlugins,
        AssetPlugin {
            file_path: format!("{}/../worker/web/assets", env!("CARGO_MANIFEST_DIR")),
            meta_check: AssetMetaCheck::Never,
            watch_for_changes_override: Some(false),
            ..default()
        },
        TransformPlugin,
        bevy::image::ImagePlugin::default(),
        bevy::mesh::MeshPlugin,
        GizmoPlugin,
    ))
    .init_asset::<StandardMaterial>()
    .init_resource::<crate::camera::CameraRig>()
    .init_resource::<crate::scene::VisualSettings>()
    .init_resource::<crate::PlaybackState>()
    .init_resource::<crate::hud::HudState>()
    .init_resource::<ButtonInput<KeyCode>>()
    .init_resource::<InputFocus>()
    .configure_sets(
        Update,
        (
            crate::EngineSet::Input,
            crate::EngineSet::Runtime,
            crate::EngineSet::Reports,
            crate::EngineSet::Camera,
            crate::EngineSet::Scene,
            crate::EngineSet::Hud,
        )
            .chain(),
    )
    .add_systems(Startup, crate::scene::setup)
    .add_systems(
        Update,
        crate::session::advance_world.in_set(crate::EngineSet::Runtime),
    )
    .add_systems(
        Update,
        crate::scene::sync_scene.in_set(crate::EngineSet::Scene),
    );
    install(&mut app);
    app.finish();
    app.cleanup();
    assert!(app.get_sub_app(RenderApp).is_none());
    app
}

#[test]
fn default_off_enable_waits_for_collection_and_disable_drops_all_cached_data() {
    assert!(matches!(Diagnostics::default(), Diagnostics::Disabled));
    let initial = report();
    assert_eq!(initial["schema"], "wsorb.presentation_diagnostics.v1");
    assert_eq!(initial["enabled"], false);
    assert_eq!(initial["available"], false);
    assert_eq!(initial["state"], "disabled");
    assert!(initial["sample"].is_null());

    let mut app = scene_app();
    app.update();
    assert_eq!(report(), initial, "a real scene does not opt itself in");
    crate::set_presentation_diagnostics(true);
    assert_eq!(report()["state"], "waiting_for_scene");
    assert_eq!(report()["available"], false);
    assert!(report()["sample"].is_null());

    app.update();
    let observed = crate::presentation_diagnostics();
    assert_eq!(report()["state"], "observed");
    assert_eq!(report()["available"], true);
    crate::set_presentation_diagnostics(true);
    assert_eq!(crate::presentation_diagnostics(), observed);

    crate::set_presentation_diagnostics(false);
    assert_eq!(report(), initial);
    app.world_mut().run_schedule(PostUpdate);
    assert_eq!(
        report(),
        initial,
        "disabled collection cannot restore stale data"
    );
    crate::set_presentation_diagnostics(true);
    assert!(report()["sample"].is_null());
    assert_eq!(report()["state"], "waiting_for_scene");
    app.world_mut().run_schedule(PostUpdate);
    assert_eq!(report()["available"], true);
}

#[test]
fn actual_scene_local_and_propagated_world_transforms_are_paired_with_native_body_pose() {
    let mut app = scene_app();
    crate::set_presentation_diagnostics(true);
    app.update();
    let snapshot = crate::snapshot();
    let actual = crate::session::applied_physics().unwrap();
    let observed = report();
    let sample = &observed["sample"];
    assert_eq!(
        observed["measurement"],
        "bevy_main_world_transforms_not_gpu_pixels"
    );
    assert_eq!(
        sample["simulation_time_basis"],
        "simulation_elapsed_seconds"
    );
    assert_eq!(
        sample["collection"]["stage"],
        "post_update_after_transform_propagation"
    );
    assert_eq!(
        sample["collection"]["bevy_frame"],
        app.world().resource::<FrameCount>().0.wrapping_sub(1)
    );
    assert_eq!(
        sample["collection"]["real_elapsed_s"],
        app.world().resource::<Time<Real>>().elapsed_secs_f64()
    );
    assert_eq!(sample["scene_snapshot_clock"], "not_exposed_by_scene_api");
    assert!(sample["scene_snapshot_source_time_s"].is_null());
    assert_eq!(
        sample["published_snapshot"]["source_time_s"],
        snapshot.time_s
    );
    assert_eq!(sample["published_snapshot"]["source_seed"], snapshot.seed);
    assert_eq!(
        sample["published_snapshot"]["trial_revision"],
        crate::session::trial_revision()
    );
    assert_eq!(sample["published_snapshot"]["frame"], snapshot.frame);
    assert_eq!(
        sample["native_lander"]["source_time_s"],
        actual.source_time_s
    );
    assert_eq!(sample["native_lander"]["frame"], actual.frame);
    assert_eq!(
        sample["native_lander"]["pose"]["position_world_m"],
        json!(actual.position_world_m)
    );
    for (id, key, published_key) in [
        (EntityId::Lander, "lander_visual", "lander"),
        (EntityId::LandingPad, "landing_pad_visual", "landing_pad"),
    ] {
        let entity = own_entity(&mut app, id);
        let local = app.world().get::<Transform>(entity).unwrap();
        let global = app.world().get::<GlobalTransform>(entity).unwrap();
        let visual = &sample[key]["transforms"];
        assert_eq!(sample[key]["state"], "observed");
        assert_eq!(visual["has_parent"], false);
        assert_eq!(
            visual["local_transform"]["translation_parent_m"],
            json!(metres(local.translation))
        );
        assert_eq!(
            serde_json::from_value::<[f32; 4]>(
                visual["local_transform"]["rotation_parent_from_local_xyzw"].clone()
            )
            .unwrap(),
            local.rotation.to_array()
        );
        assert_eq!(
            serde_json::from_value::<[f32; 3]>(visual["local_transform"]["scale"].clone()).unwrap(),
            local.scale.to_array()
        );
        assert_eq!(
            visual["global_transform"]["translation_world_m"],
            json!(metres(global.translation()))
        );
        near(
            vector(&visual["global_transform"]["translation_world_m"]),
            vector(&sample["published_snapshot"][published_key]["position_world_m"]),
            0.002,
        );
    }
    near(
        vector(&sample["position_differences"]["lander_global_minus_native_m"]),
        [0.0; 3],
        0.002,
    );
    assert_eq!(
        sample["position_differences"]["lander_native_minus_published_m"],
        json!([0.0, 0.0, 0.0])
    );
    assert!(!crate::session::is_running());
    assert!(app.get_sub_app(RenderApp).is_none());
    let renderer: Value = serde_json::from_str(&crate::renderer_status()).unwrap();
    assert_eq!(renderer["started"], false);
    assert_eq!(renderer["ready"], false);
}

#[test]
fn real_ecs_transform_mismatch_is_observed_after_propagation_without_touching_native_state() {
    let mut app = scene_app();
    crate::set_presentation_diagnostics(true);
    app.update();
    let lander = own_entity(&mut app, EntityId::Lander);
    let original_global = *app.world().get::<GlobalTransform>(lander).unwrap();
    let local_before = *app.world().get::<Transform>(lander).unwrap();
    let snapshot = crate::snapshot();
    let actual = crate::session::applied_physics().unwrap();
    let observation = crate::session::observation();
    let runtime = crate::session::runtime_report();
    let renderer = crate::renderer_status();
    let sensors = crate::ship_camera_status();
    let queued = crate::session::queued_actions_for_tests();

    // Change only the real scene ECS. The native lander and publication stay put.
    app.world_mut()
        .get_mut::<Transform>(lander)
        .unwrap()
        .translation += Vec3::new(0.125, -0.25, 0.5);
    let parent_transform = Transform::from_xyz(0.75, -0.5, 0.25)
        .with_rotation(Quat::from_rotation_z(0.3))
        .with_scale(Vec3::new(1.1, 0.9, 1.2));
    let parent = app.world_mut().spawn(parent_transform).id();
    app.world_mut().entity_mut(lander).insert(ChildOf(parent));
    app.world_mut().run_schedule(PostUpdate);

    let local = app.world().get::<Transform>(lander).unwrap();
    let global = app.world().get::<GlobalTransform>(lander).unwrap();
    let expected_global = GlobalTransform::from(parent_transform).mul_transform(*local);
    assert_eq!(
        *global, expected_global,
        "Bevy performed real parent propagation"
    );
    assert_ne!(*global, original_global);
    let observed = report();
    let sample = &observed["sample"];
    let transforms = &sample["lander_visual"]["transforms"];
    assert_eq!(transforms["has_parent"], true);
    assert_eq!(
        transforms["local_transform"]["translation_parent_m"],
        json!(metres(local.translation))
    );
    near(
        vector(&transforms["local_transform"]["translation_parent_m"]),
        metres(local_before.translation + Vec3::new(0.125, -0.25, 0.5)),
        1.0e-9,
    );
    assert_eq!(
        transforms["global_transform"]["translation_world_m"],
        json!(metres(expected_global.translation()))
    );
    assert_eq!(
        serde_json::from_value::<[[f32; 3]; 3]>(
            transforms["global_transform"]["linear_world_from_local_columns"].clone()
        )
        .unwrap(),
        expected_global.affine().matrix3.to_cols_array_2d()
    );
    let expected_delta = std::array::from_fn(|axis| {
        metres(expected_global.translation())[axis] - actual.position_world_m[axis]
    });
    near(
        vector(&sample["position_differences"]["lander_global_minus_native_m"]),
        expected_delta,
        1.0e-9,
    );
    assert!(
        expected_delta
            .into_iter()
            .any(|component| component.abs() > 1.0)
    );

    // Repeated collection/getters cannot advance clocks, replace control cohorts,
    // change forces, sample sensors, enqueue commands or mark a renderer ready.
    for _ in 0..4 {
        let cached = crate::presentation_diagnostics();
        assert_eq!(crate::presentation_diagnostics(), cached);
        app.world_mut().run_schedule(PostUpdate);
    }
    assert!(Rc::ptr_eq(&snapshot, &crate::snapshot()));
    assert_eq!(crate::session::applied_physics().unwrap(), actual);
    assert_eq!(crate::session::observation(), observation);
    assert_eq!(crate::session::runtime_report(), runtime);
    assert_eq!(crate::renderer_status(), renderer);
    assert_eq!(crate::ship_camera_status(), sensors);
    assert_eq!(crate::session::queued_actions_for_tests(), queued);
    assert!(app.get_sub_app(RenderApp).is_none());
}

#[test]
fn unstarted_and_actual_headless_runtime_never_fabricate_scene_availability() {
    crate::set_presentation_diagnostics(true);
    assert_eq!(report()["state"], "waiting_for_scene");
    assert_eq!(report()["available"], false);
    assert!(report()["sample"].is_null());
    let renderer = crate::renderer_status();
    let sensors = crate::ship_camera_status();
    crate::headless::start().unwrap();
    crate::headless::tick(0.05).unwrap();
    assert!(!crate::session::is_running());
    assert_eq!(report()["state"], "waiting_for_scene");
    assert_eq!(report()["available"], false);
    assert!(report()["sample"].is_null());
    assert_eq!(crate::renderer_status(), renderer);
    assert_eq!(crate::ship_camera_status(), sensors);
}

#[test]
fn unrelated_visuals_and_missing_scene_remain_unavailable() {
    let mut app = App::new();
    app.add_plugins(TransformPlugin);
    install(&mut app);
    app.world_mut().spawn((
        AssetVisual {
            id: EntityId::ReturnShip,
            kind: EntityKind::ReturnShip,
        },
        Transform::IDENTITY,
    ));
    app.world_mut().spawn((
        AssetVisual {
            id: EntityId::Lander,
            kind: EntityKind::Debris,
        },
        Transform::IDENTITY,
    ));
    crate::set_presentation_diagnostics(true);
    app.update();
    assert_eq!(report()["state"], "waiting_for_scene");
    assert_eq!(report()["available"], false);
    assert!(report()["sample"].is_null());
}

#[test]
fn absent_global_transform_and_clock_resources_are_null_not_invented() {
    let mut app = scene_app();
    crate::set_presentation_diagnostics(true);
    app.update();
    let lander = own_entity(&mut app, EntityId::Lander);
    app.world_mut()
        .entity_mut(lander)
        .remove::<GlobalTransform>();
    app.world_mut().remove_resource::<FrameCount>();
    app.world_mut().remove_resource::<Time<Real>>();
    app.world_mut().run_schedule(PostUpdate);
    assert!(app.world().get::<GlobalTransform>(lander).is_none());
    let observed = report();
    let sample = &observed["sample"];
    assert_eq!(observed["available"], true);
    assert!(sample["collection"]["bevy_frame"].is_null());
    assert!(sample["collection"]["real_elapsed_s"].is_null());
    assert!(sample["lander_visual"]["transforms"]["local_transform"].is_object());
    assert!(sample["lander_visual"]["transforms"]["global_transform"].is_null());
    assert!(sample["position_differences"]["lander_global_minus_native_m"].is_null());
    assert!(sample["position_differences"]["lander_global_minus_published_m"].is_null());
}

#[test]
fn duplicates_are_bounded_and_scene_loss_or_app_drop_clears_old_samples() {
    let mut app = scene_app();
    crate::set_presentation_diagnostics(true);
    app.update();
    let lander = own_entity(&mut app, EntityId::Lander);
    let pad = own_entity(&mut app, EntityId::LandingPad);
    let duplicates: Vec<_> = (0..64)
        .map(|_| {
            app.world_mut()
                .spawn((
                    AssetVisual {
                        id: EntityId::Lander,
                        kind: EntityKind::Lander,
                    },
                    Transform::IDENTITY,
                ))
                .id()
        })
        .collect();
    app.world_mut().run_schedule(PostUpdate);
    let observed = report();
    assert_eq!(
        observed["sample"]["lander_visual"],
        json!({
            "state": "ambiguous", "matching_entities": 65
        })
    );
    assert!(observed["sample"]["position_differences"]["lander_global_minus_native_m"].is_null());
    assert!(crate::presentation_diagnostics().len() < 16_384);
    let text = crate::presentation_diagnostics();
    for forbidden in [
        "rng_state",
        "assets",
        "observations",
        "events",
        "Debris",
        "submitted_primary_frames",
        "completed_primary_frame",
        "pending_pipelines",
    ] {
        assert!(
            !text.contains(forbidden),
            "unexpected payload field {forbidden}"
        );
    }
    assert!(observed.get("pixels").is_none());
    assert!(observed["sample"].get("pixels").is_none());
    for entity in duplicates.into_iter().chain([lander, pad]) {
        app.world_mut().entity_mut(entity).despawn();
    }
    app.world_mut().run_schedule(PostUpdate);
    assert_eq!(report()["available"], false);
    assert!(report()["sample"].is_null());

    // The production sync repopulates the actual scene, not the diagnostics.
    app.update();
    assert_eq!(report()["available"], true);
    drop(app);
    assert_eq!(report()["state"], "waiting_for_scene");
    assert_eq!(report()["available"], false);
    assert!(report()["sample"].is_null());
}
