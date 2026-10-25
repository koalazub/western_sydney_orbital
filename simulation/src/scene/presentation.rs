//! Main-viewer aids, never physical illumination or mounted sensor settings.
use crate::camera::SimulationCamera;
use bevy::{camera::Exposure, core_pipeline::tonemapping::Tonemapping, light::Skybox, prelude::*};
use serde::Serialize;
use std::{cell::RefCell, collections::VecDeque};

const ACTION_LIMIT: usize = 32;
// Display context, not calibrated stellar radiance or an epoch-aligned sky.
const STARFIELD_EXPOSED_PEAK: f32 = 0.04;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum IlluminationMode {
    Natural,
    #[default]
    Context,
    Inspection,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum VisualAction {
    CycleIllumination,
    ToggleStars,
    ToggleGuides,
}

#[derive(Resource, Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub(crate) struct VisualSettings {
    pub(crate) illumination: IlluminationMode,
    pub(crate) stars: bool,
    pub(crate) guides: bool,
}

impl Default for VisualSettings {
    fn default() -> Self {
        Self {
            illumination: IlluminationMode::Context,
            stars: true,
            guides: true,
        }
    }
}

impl VisualSettings {
    pub(crate) fn transition(self, action: VisualAction) -> Self {
        match action {
            VisualAction::CycleIllumination => Self {
                illumination: match self.illumination {
                    IlluminationMode::Natural => IlluminationMode::Context,
                    IlluminationMode::Context => IlluminationMode::Inspection,
                    IlluminationMode::Inspection => IlluminationMode::Natural,
                },
                ..self
            },
            VisualAction::ToggleStars => Self {
                stars: !self.stars,
                ..self
            },
            VisualAction::ToggleGuides => Self {
                guides: !self.guides,
                ..self
            },
        }
    }
}

struct ViewerLighting {
    ambient_cd_m2: f32,
    exposure_ev100: f32,
    tonemapping: Tonemapping,
}

impl IlluminationMode {
    fn lighting(self) -> ViewerLighting {
        match self {
            Self::Natural => ViewerLighting {
                ambient_cd_m2: 0.0,
                exposure_ev100: Exposure::EV100_SUNLIGHT,
                tonemapping: Tonemapping::None,
            },
            Self::Context => ViewerLighting {
                ambient_cd_m2: 2_500.0,
                exposure_ev100: 14.0,
                tonemapping: Tonemapping::Reinhard,
            },
            Self::Inspection => ViewerLighting {
                ambient_cd_m2: 8_000.0,
                exposure_ev100: 13.0,
                tonemapping: Tonemapping::Reinhard,
            },
        }
    }
}

impl ViewerLighting {
    fn ambient(&self) -> AmbientLight {
        AmbientLight {
            color: Color::WHITE,
            brightness: self.ambient_cd_m2,
            ..default()
        }
    }

    fn exposure(&self) -> Exposure {
        Exposure {
            ev100: self.exposure_ev100,
        }
    }

    fn sky_brightness(&self) -> f32 {
        // Bevy multiplies Skybox brightness by this camera's exposure. Keep the
        // context map subdued as the viewer changes between exposure presets.
        STARFIELD_EXPOSED_PEAK / self.exposure().exposure()
    }
}

#[derive(Component)]
struct ViewerStarfield(Handle<Image>);

#[derive(Bundle)]
pub(super) struct MainCameraPresentation {
    ambient: AmbientLight,
    exposure: Exposure,
    tonemapping: Tonemapping,
    skybox: Skybox,
    starfield: ViewerStarfield,
}

impl MainCameraPresentation {
    pub(super) fn new(settings: VisualSettings, starfield: Handle<Image>) -> Self {
        let lighting = settings.illumination.lighting();
        Self {
            ambient: lighting.ambient(),
            exposure: lighting.exposure(),
            tonemapping: lighting.tonemapping,
            skybox: Skybox {
                image: settings.stars.then(|| starfield.clone()),
                brightness: lighting.sky_brightness(),
                rotation: Quat::IDENTITY,
            },
            starfield: ViewerStarfield(starfield),
        }
    }
}

thread_local! {
    static ACTIONS: RefCell<VecDeque<VisualAction>> = const { RefCell::new(VecDeque::new()) };
    static SETTINGS: RefCell<VisualSettings> = RefCell::new(VisualSettings::default());
}

/// The ECS resource owns settings; this mirror is the native HUD/export adapter.
pub(crate) fn visual_settings() -> VisualSettings {
    SETTINGS.with(|settings| *settings.borrow())
}

/// Keep recent viewer input bounded. Overflow discards the oldest pending action.
pub(crate) fn request_visual_action(action: VisualAction) {
    ACTIONS.with(|actions| {
        let mut actions = actions.borrow_mut();
        if actions.len() == ACTION_LIMIT {
            actions.pop_front();
        }
        actions.push_back(action);
    });
}

pub(super) fn install(app: &mut App) {
    app.init_resource::<VisualSettings>().add_systems(
        Update,
        update
            .after(crate::EngineSet::Camera)
            .before(crate::EngineSet::Scene),
    );
}

type ViewerComponents = (
    &'static mut AmbientLight,
    &'static mut Exposure,
    &'static mut Tonemapping,
    &'static mut Skybox,
    &'static ViewerStarfield,
);

fn update(
    mut settings: ResMut<VisualSettings>,
    mut viewers: Query<ViewerComponents, With<SimulationCamera>>,
) {
    ACTIONS.with(|actions| {
        for action in actions.borrow_mut().drain(..) {
            *settings = settings.transition(action);
        }
    });
    if !settings.is_changed() {
        return;
    }
    SETTINGS.with(|mirror| *mirror.borrow_mut() = *settings);
    let lighting = settings.illumination.lighting();
    for (mut ambient, mut exposure, mut tonemapping, mut skybox, starfield) in &mut viewers {
        *ambient = lighting.ambient();
        *exposure = lighting.exposure();
        *tonemapping = lighting.tonemapping;
        skybox.image = settings.stars.then(|| starfield.0.clone());
        skybox.brightness = lighting.sky_brightness();
    }
}

#[cfg(all(test, not(target_arch = "wasm32")))]
mod tests {
    use super::*;
    use crate::domain::ShipCameraId;
    use bevy::{
        camera::visibility::RenderLayers,
        gizmos::config::{DefaultGizmoConfigGroup, GizmoConfig, GizmoConfigStore},
        light::GlobalAmbientLight,
    };

    struct RestoreBridge {
        actions: VecDeque<VisualAction>,
        settings: VisualSettings,
    }

    impl RestoreBridge {
        fn fresh() -> Self {
            Self {
                actions: ACTIONS.with(|actions| actions.take()),
                settings: SETTINGS.with(|settings| settings.replace(VisualSettings::default())),
            }
        }
    }

    impl Drop for RestoreBridge {
        fn drop(&mut self) {
            ACTIONS.with(|actions| *actions.borrow_mut() = std::mem::take(&mut self.actions));
            SETTINGS.with(|settings| *settings.borrow_mut() = self.settings);
        }
    }

    #[test]
    fn typed_actions_change_only_the_requested_viewer_preference() {
        assert_eq!(
            VisualSettings::default().illumination,
            IlluminationMode::Context
        );
        assert!(VisualSettings::default().stars && VisualSettings::default().guides);
        for mode in [
            IlluminationMode::Natural,
            IlluminationMode::Context,
            IlluminationMode::Inspection,
        ] {
            for stars in [false, true] {
                for guides in [false, true] {
                    let settings = VisualSettings {
                        illumination: mode,
                        stars,
                        guides,
                    };
                    let stars_toggled = settings.transition(VisualAction::ToggleStars);
                    assert_eq!(stars_toggled.illumination, mode);
                    assert_eq!(stars_toggled.stars, !stars);
                    assert_eq!(stars_toggled.guides, guides);
                    assert_eq!(
                        stars_toggled.transition(VisualAction::ToggleStars),
                        settings
                    );
                    let guides_toggled = settings.transition(VisualAction::ToggleGuides);
                    assert_eq!(guides_toggled.illumination, mode);
                    assert_eq!(guides_toggled.stars, stars);
                    assert_eq!(guides_toggled.guides, !guides);
                    assert_eq!(
                        guides_toggled.transition(VisualAction::ToggleGuides),
                        settings
                    );
                    let mut cycled = settings;
                    for _ in 0..3 {
                        cycled = cycled.transition(VisualAction::CycleIllumination);
                        assert_eq!(cycled.stars, stars);
                        assert_eq!(cycled.guides, guides);
                    }
                    assert_eq!(cycled, settings);
                }
            }
        }
    }

    #[test]
    fn viewer_action_adapter_is_bounded_and_publishes_only_applied_settings() {
        let _restore = RestoreBridge::fresh();
        let mut app = App::new();
        install(&mut app);
        for _ in 0..ACTION_LIMIT {
            request_visual_action(VisualAction::ToggleStars);
        }
        request_visual_action(VisualAction::ToggleGuides);
        ACTIONS.with(|actions| {
            assert_eq!(actions.borrow().len(), ACTION_LIMIT);
            assert_eq!(actions.borrow().back(), Some(&VisualAction::ToggleGuides));
        });
        assert_eq!(visual_settings(), VisualSettings::default());
        app.update();
        assert!(!visual_settings().stars);
        assert!(!visual_settings().guides);
        assert_eq!(visual_settings(), *app.world().resource::<VisualSettings>());
        ACTIONS.with(|actions| assert!(actions.borrow().is_empty()));
        assert_eq!(
            serde_json::to_value(visual_settings()).unwrap(),
            serde_json::json!({ "illumination": "context", "stars": false, "guides": false })
        );
    }

    #[test]
    fn actual_bevy_viewer_presets_leave_mounted_cameras_sun_and_snapshot_unchanged() {
        let _restore = RestoreBridge::fresh();
        let snapshot = crate::snapshot();
        let before = serde_json::to_value(snapshot.as_ref()).unwrap();
        let rng = snapshot.rng_state;
        let aim = serde_json::to_value(crate::camera::status()).unwrap();
        let queued = crate::session::queued_actions_for_tests();
        let mounted =
            [ShipCameraId::Top, ShipCameraId::Bottom].map(|id| crate::sensors::pose(id, &snapshot));
        let lighting = &crate::scenario::settings().lighting;
        let sun_transform = Transform::default().looking_to(
            -Vec3::from_array(lighting.direction_to_sun).normalize(),
            Vec3::Y,
        );
        let mut gizmos = GizmoConfigStore::default();
        gizmos.insert(GizmoConfig::default(), DefaultGizmoConfigGroup);
        let mut app = App::new();
        app.init_resource::<Assets<Image>>()
            .init_resource::<Time<Real>>()
            .init_resource::<UiScale>()
            .init_resource::<crate::camera::CameraRig>()
            .insert_resource(gizmos)
            .insert_resource(GlobalAmbientLight::NONE);
        install(&mut app);
        crate::sensors::install(&mut app);
        let (view, projection) = crate::camera::initial_overview(
            &mut crate::camera::CameraRig::default(),
            &snapshot,
            None,
        );
        let Projection::Perspective(lens) = &projection else {
            panic!("viewer perspective")
        };
        let viewer_lens = (lens.fov, lens.aspect_ratio, lens.near, lens.far);
        let viewer = app
            .world_mut()
            .spawn((
                SimulationCamera,
                Camera3d::default(),
                projection,
                view,
                MainCameraPresentation::new(VisualSettings::default(), Handle::default()),
            ))
            .id();
        let sun = app
            .world_mut()
            .spawn((
                DirectionalLight {
                    illuminance: lighting.illuminance_lux,
                    ..default()
                },
                sun_transform,
                RenderLayers::from_layers(&[0, 2]),
            ))
            .id();
        app.update();
        let raw_metadata = crate::sensors::status();
        let world = app.world_mut();
        let sensors: Vec<_> = world
            .query_filtered::<Entity, (With<Camera3d>, Without<SimulationCamera>)>()
            .iter(world)
            .collect();
        assert_eq!(sensors.len(), 2, "production mounted-camera setup must run");
        let metadata: serde_json::Value = serde_json::from_str(&raw_metadata).unwrap();
        for camera in metadata["cameras"].as_array().unwrap() {
            assert_eq!(camera["exposure_ev100"], 15.0);
            assert_eq!(camera["ambient_brightness"], 0.0);
            assert_eq!(camera["field_of_view_deg"], 60.0);
            assert_eq!(camera["debug_overlays"], false);
        }
        for (mode, ambient_cd_m2, ev100, tonemapping) in [
            (
                IlluminationMode::Context,
                2_500.0,
                14.0,
                Tonemapping::Reinhard,
            ),
            (
                IlluminationMode::Inspection,
                8_000.0,
                13.0,
                Tonemapping::Reinhard,
            ),
            (IlluminationMode::Natural, 0.0, 15.0, Tonemapping::None),
            (
                IlluminationMode::Context,
                2_500.0,
                14.0,
                Tonemapping::Reinhard,
            ),
        ] {
            assert_eq!(visual_settings().illumination, mode);
            let ambient = app.world().get::<AmbientLight>(viewer).unwrap();
            let exposure = app.world().get::<Exposure>(viewer).unwrap();
            let sky = app.world().get::<Skybox>(viewer).unwrap();
            assert_eq!(ambient.color, Color::WHITE);
            assert_eq!(ambient.brightness, ambient_cd_m2);
            assert_eq!(exposure.ev100, ev100);
            assert_eq!(
                *app.world().get::<Tonemapping>(viewer).unwrap(),
                tonemapping
            );
            assert!(sky.image.is_some());
            assert!((sky.brightness * exposure.exposure() - STARFIELD_EXPOSED_PEAK).abs() < 1e-7);
            assert_eq!(sky.rotation, Quat::IDENTITY);
            assert_eq!(*app.world().get::<Transform>(viewer).unwrap(), view);
            let Projection::Perspective(lens) = app.world().get::<Projection>(viewer).unwrap()
            else {
                panic!("viewer perspective")
            };
            assert_eq!(
                (lens.fov, lens.aspect_ratio, lens.near, lens.far),
                viewer_lens
            );
            for entity in &sensors {
                let world = app.world();
                assert_eq!(world.get::<AmbientLight>(*entity).unwrap().brightness, 0.0);
                assert_eq!(world.get::<Exposure>(*entity).unwrap().ev100, 15.0);
                assert_eq!(
                    *world.get::<Tonemapping>(*entity).unwrap(),
                    Tonemapping::None
                );
                assert!(world.get::<Skybox>(*entity).is_none());
                let Projection::Perspective(projection) = world.get::<Projection>(*entity).unwrap()
                else {
                    panic!("mounted pinhole camera")
                };
                assert!((projection.fov.to_degrees() - 60.0).abs() < 1e-4);
                assert_eq!(projection.aspect_ratio, 1.0);
            }
            assert_eq!(app.world().resource::<GlobalAmbientLight>().brightness, 0.0);
            assert_eq!(
                app.world()
                    .get::<DirectionalLight>(sun)
                    .unwrap()
                    .illuminance,
                lighting.illuminance_lux
            );
            assert_eq!(*app.world().get::<Transform>(sun).unwrap(), sun_transform);
            assert_eq!(crate::sensors::status(), raw_metadata);
            assert_eq!(serde_json::to_value(crate::camera::status()).unwrap(), aim);
            assert_eq!(crate::snapshot().rng_state, rng);
            assert_eq!(crate::session::queued_actions_for_tests(), queued);
            assert_eq!(
                serde_json::to_value(crate::snapshot().as_ref()).unwrap(),
                before
            );
            assert_eq!(
                [ShipCameraId::Top, ShipCameraId::Bottom]
                    .map(|id| crate::sensors::pose(id, &snapshot)),
                mounted
            );
            request_visual_action(VisualAction::CycleIllumination);
            app.update();
        }
        request_visual_action(VisualAction::ToggleStars);
        request_visual_action(VisualAction::ToggleGuides);
        app.update();
        assert!(!visual_settings().stars && !visual_settings().guides);
        assert!(app.world().get::<Skybox>(viewer).unwrap().image.is_none());
        for entity in &sensors {
            assert!(app.world().get::<Skybox>(*entity).is_none());
        }
        assert_eq!(crate::sensors::status(), raw_metadata);
        assert_eq!(serde_json::to_value(crate::camera::status()).unwrap(), aim);
        assert_eq!(crate::snapshot().rng_state, rng);
        assert_eq!(crate::session::queued_actions_for_tests(), queued);
        assert_eq!(
            serde_json::to_value(crate::snapshot().as_ref()).unwrap(),
            before
        );
        assert!(app.get_sub_app(bevy::render::RenderApp).is_none());
    }
}
