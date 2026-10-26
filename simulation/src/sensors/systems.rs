use super::{capture, model::*, optics};
use crate::{
    camera::SimulationCamera,
    domain::{EntityId, ShipCameraId},
    model::{SensorDelivery, SensorFaultMode, Snapshot},
    scenario::settings,
};
use bevy::{
    camera::{Exposure, RenderTarget, visibility::RenderLayers},
    core_pipeline::tonemapping::Tonemapping,
    gizmos::config::{DefaultGizmoConfigGroup, GizmoConfigStore},
    image::ImageSampler,
    prelude::*,
    render::{
        gpu_readback::ReadbackOnce,
        render_resource::{TextureFormat, TextureUsages},
    },
};
use std::cell::RefCell;

thread_local! { static STATE: RefCell<SensorState> = RefCell::new(SensorState::default()); }

const AIM_SOURCE_URL: &str = "https://github.com/SimDynamX/STU4-Solution/blob/8545c085426a71c39cbe1d544a844b3edb757007/UserAPI.py#L43-L50";

pub(crate) fn status() -> String {
    let snapshot = crate::snapshot();
    STATE.with(|state| {
        let mut value: serde_json::Value = serde_json::from_str(
            &serde_json::to_string(&report(&state.borrow(), &snapshot))
                .expect("finite sensor metadata"),
        )
        .expect("valid sensor JSON");
        let navigation_ready = snapshot
            .sensor_state
            .as_ref()
            .is_none_or(|state| state.navigation_delivery() == SensorDelivery::Ready);
        let mut views = Vec::new();
        for camera in [ShipCameraId::Top, ShipCameraId::Bottom] {
            let mode = capture::mode(&snapshot, camera);
            let cached = capture::cached_info(camera);
            let current = &mut value["cameras"][camera.index()];
            if mode != SensorFaultMode::Nominal {
                let sample = cached.as_ref().filter(|_| mode == SensorFaultMode::Frozen);
                current["simulation_time_s"] =
                    serde_json::json!(sample.map(|sample| sample.simulation_time_s));
                current["position_m"] = serde_json::json!(sample.map(|sample| sample.position_m));
                current["rotation"] = serde_json::json!(sample.map(|sample| sample.rotation));
            }
            if !navigation_ready {
                current["position_m"] = serde_json::Value::Null;
                current["rotation"] = serde_json::Value::Null;
            }
            let frame = match mode {
                SensorFaultMode::Nominal => state.borrow().frames[camera.index()].clone(),
                SensorFaultMode::Frozen => cached,
                SensorFaultMode::Unavailable => None,
            };
            if let Some(frame) = frame {
                let mut frame: serde_json::Value = serde_json::from_str(
                    &serde_json::to_string(&frame).expect("finite frame metadata"),
                )
                .expect("valid frame JSON");
                if !navigation_ready {
                    frame["position_m"] = serde_json::Value::Null;
                    frame["rotation"] = serde_json::Value::Null;
                }
                views.push(frame);
            }
        }
        value["views"] = serde_json::json!(views);
        value.to_string()
    })
}

pub(crate) fn invalidate_trial() {
    STATE.with(|state| state.borrow_mut().frames = [None, None]);
}

/// Camera health describes completed RGB readback, not scheduled metadata.
pub(crate) fn attach_health(
    report: &mut serde_json::Value,
    sensor_state: Option<&crate::model::SensorState>,
) {
    let Some(units) = report["sensor_health"]["units"].as_array_mut() else {
        return;
    };
    let revision = sensor_state.map_or(0, |state| state.revision);
    for camera in [ShipCameraId::Top, ShipCameraId::Bottom] {
        let id = capture::unit(camera);
        let Some(health) = units
            .iter_mut()
            .find(|health| health["id"] == serde_json::json!(id))
        else {
            continue;
        };
        let mode = sensor_state.map_or(SensorFaultMode::Nominal, |state| state.mode(id));
        let cached = capture::cached_info(camera);
        let waiting = mode == SensorFaultMode::Nominal
            && capture::completed_revision(camera) != Some(revision);
        let delivery = crate::model::sensor_delivery(mode, cached.is_some(), waiting);
        health["delivery"] = serde_json::json!(delivery);
        let delivered = cached
            .as_ref()
            .filter(|_| matches!(delivery, SensorDelivery::Ready | SensorDelivery::Frozen));
        health["sample_time_s"] = serde_json::json!(delivered.map(|frame| frame.simulation_time_s));
        health["sequence"] = serde_json::json!(delivered.map(|frame| frame.sequence));
    }
}

/// Apply the real helper's clipping, not debug-view rotation or optical zoom.
/// The next sensor update uses this aim, even when no image sample is due.
pub(crate) fn pan_tilt(
    camera: ShipCameraId,
    yaw_deg: f64,
    pitch_deg: f64,
) -> Result<String, String> {
    STATE.with(|state| state.borrow_mut().pan_tilt(camera, yaw_deg, pitch_deg))?;
    Ok(status())
}

pub(super) fn configured() -> bool {
    STATE.with(|state| state.borrow().configured)
}

pub(crate) fn pose(id: ShipCameraId, state: &Snapshot) -> Transform {
    let aim = STATE.with(|state| state.borrow().aims[id.index()]);
    mounted_pose(id, state, aim)
}

fn mounted_pose(id: ShipCameraId, state: &Snapshot, aim: PanTiltAim) -> Transform {
    let mount = &settings().ship_cameras.mounts[id.index()];
    let ship = state
        .assets
        .iter()
        .find(|asset| asset.id == EntityId::Lander)
        .expect("scenario has a lander");
    let attitude = Quat::from_array(ship.rotation.map(|value| value as f32)).normalize();
    let position = Vec3::from_array(ship.position.map(|value| (value * 0.001) as f32));
    let offset = Vec3::from_array(mount.position_m.map(|value| (value * 0.001) as f32));
    let (forward, up) = optics::body_axes(aim);
    Transform::from_translation(position + attitude * offset)
        .looking_to(attitude * forward, attitude * up)
}

pub(super) fn setup(
    mut commands: Commands,
    mut images: ResMut<Assets<Image>>,
    debug_cameras: Query<Entity, With<SimulationCamera>>,
    mut gizmos: ResMut<GizmoConfigStore>,
) {
    let config = &settings().ship_cameras;
    let snapshot = crate::snapshot();
    let handles = std::array::from_fn(|index| {
        let mut image = Image::new_target_texture(
            config.resolution_px[0],
            config.resolution_px[1],
            TextureFormat::Rgba8UnormSrgb,
            None,
        );
        image.texture_descriptor.usage |= TextureUsages::COPY_SRC;
        image.sampler = ImageSampler::nearest();
        let handle = images.add(image);
        let id = config.mounts[index].id;
        commands.spawn((
            Camera3d::default(),
            Camera {
                order: -2 + index as isize,
                clear_color: Color::BLACK.into(),
                ..default()
            },
            RenderTarget::Image(handle.clone().into()),
            Projection::Perspective(optics::projection(config)),
            Exposure {
                ev100: config.exposure_ev100,
            },
            AmbientLight {
                color: Color::WHITE,
                brightness: config.ambient_brightness,
                ..default()
            },
            Tonemapping::None,
            Msaa::Off,
            sensor_layers(),
            SensorCamera(id),
            pose(id, &snapshot),
        ));
        handle
    });
    for camera in &debug_cameras {
        commands
            .entity(camera)
            .insert(RenderLayers::layer(0).with(1));
    }
    gizmos
        .config_mut::<DefaultGizmoConfigGroup>()
        .0
        .render_layers = RenderLayers::layer(1);
    commands.insert_resource(SensorImages {
        images: handles,
        last_sample_wall_s: f64::NEG_INFINITY,
        sequence: 0,
        frames: [None, None],
        trial_revision: crate::session::trial_revision(),
    });
    STATE.with(|state| {
        let mut state = state.borrow_mut();
        state.configured = true;
        state.frames = [None, None];
    });
}

fn info(
    id: ShipCameraId,
    aim: PanTiltAim,
    transform: Transform,
    time_s: f64,
    sequence: u64,
) -> FrameInfo {
    let config = &settings().ship_cameras;
    let angles = optics::field_of_view(config);
    let mount = &config.mounts[id.index()];
    let (forward, up) = optics::body_axes(aim);
    FrameInfo {
        camera: id,
        sequence,
        width: config.resolution_px[0],
        height: config.resolution_px[1],
        channels: 3,
        bits_per_channel: 8,
        color_space: ColorSpace::Srgb,
        render_format: RenderFormat::Rgba8UnormSrgb,
        field_of_view_deg: config.field_of_view_deg,
        field_of_view_locked: true,
        field_of_view_convention: config.field_of_view_convention,
        field_of_view_status: config.field_of_view_source.status,
        field_of_view_source_url: config.field_of_view_source.url.clone(),
        horizontal_field_of_view_deg: angles.horizontal_deg,
        vertical_field_of_view_deg: angles.vertical_deg,
        diagonal_field_of_view_deg: angles.diagonal_deg,
        exposure_ev100: config.exposure_ev100,
        ambient_brightness: config.ambient_brightness,
        simulation_time_s: time_s,
        position_m: transform
            .translation
            .as_dvec3()
            .to_array()
            .map(|value| value * 1000.0),
        rotation: transform.rotation.to_array(),
        mount_source_url: config.mount_source.url.clone(),
        aim_source_url: AIM_SOURCE_URL.into(),
        mount_position_body_m: mount.position_m,
        mount_forward_body: forward.to_array(),
        mount_up_body: up.to_array(),
        mount_yaw_deg: aim.yaw_deg,
        mount_pitch_deg: aim.pitch_deg,
        mount_yaw_limits_deg: id.yaw_limits_deg(),
        mount_pitch_limits_deg: id.pitch_limits_deg(),
        fixed_ship_mount: true,
        debug_overlays: false,
    }
}

fn report(state: &SensorState, snapshot: &Snapshot) -> SensorStatus {
    SensorStatus {
        configured: state.configured,
        capture_hz: settings().ship_cameras.cadence_hz,
        cameras: settings().ship_cameras.mounts.map(|mount| {
            let aim = state.aims[mount.id.index()];
            let current = info(
                mount.id,
                aim,
                mounted_pose(mount.id, snapshot, aim),
                snapshot.time_s,
                0,
            );
            CameraStateInfo {
                camera: mount.id,
                yaw_deg: aim.yaw_deg,
                pitch_deg: aim.pitch_deg,
                yaw_limits_deg: mount.id.yaw_limits_deg(),
                pitch_limits_deg: mount.id.pitch_limits_deg(),
                simulation_time_s: snapshot.time_s,
                position_m: current.position_m,
                rotation: current.rotation,
                mount_position_body_m: current.mount_position_body_m,
                mount_forward_body: current.mount_forward_body,
                mount_up_body: current.mount_up_body,
                mount_source_url: current.mount_source_url,
                aim_source_url: AIM_SOURCE_URL.into(),
                width: current.width,
                height: current.height,
                field_of_view_deg: current.field_of_view_deg,
                field_of_view_locked: true,
                field_of_view_convention: current.field_of_view_convention,
                field_of_view_status: current.field_of_view_status,
                field_of_view_source_url: current.field_of_view_source_url,
                horizontal_field_of_view_deg: current.horizontal_field_of_view_deg,
                vertical_field_of_view_deg: current.vertical_field_of_view_deg,
                diagonal_field_of_view_deg: current.diagonal_field_of_view_deg,
                exposure_ev100: current.exposure_ev100,
                ambient_brightness: current.ambient_brightness,
                fixed_ship_mount: true,
                debug_overlays: false,
            }
        }),
        views: state.frames.iter().flatten().cloned().collect(),
    }
}

fn sensor_layers() -> RenderLayers {
    // Shared geometry and sensor illumination only. Axes, trails and plumes use 1.
    RenderLayers::layer(0).with(2)
}

type MountedCamera = (
    &'static SensorCamera,
    &'static mut Transform,
    &'static mut Camera,
    &'static mut Projection,
    &'static mut Exposure,
);

pub(super) fn update(
    mut commands: Commands,
    time: Res<Time<Real>>,
    mut images: ResMut<SensorImages>,
    mut cameras: Query<MountedCamera, Without<SimulationCamera>>,
    tickets: Query<(Entity, &CaptureTicket)>,
) {
    let now = time.elapsed_secs_f64();
    let trial_revision = crate::session::trial_revision();
    let sensor_revision = crate::session::sensor_revision();
    if images.trial_revision != trial_revision {
        images.trial_revision = trial_revision;
        images.frames = [None, None];
        images.last_sample_wall_s = f64::NEG_INFINITY;
    }
    for (entity, ticket) in &tickets {
        if now > ticket.deadline_wall_s
            || ticket.trial_revision != trial_revision
            || ticket.sensor_revision != sensor_revision
        {
            capture::reject(
                ticket.token,
                ticket.info.camera,
                "Sensor readback timed out or was invalidated by a policy/trial change",
            );
            commands.entity(entity).despawn();
        }
    }
    let requests = capture::drain();
    let due = now - images.last_sample_wall_s >= 1.0 / settings().ship_cameras.cadence_hz;
    if due || !requests.is_empty() {
        images.last_sample_wall_s = now;
        images.sequence = images.sequence.wrapping_add(1);
    }
    let snapshot = crate::snapshot();
    let aims = STATE.with(|state| state.borrow().aims);
    for (sensor, mut transform, mut camera, mut projection, mut exposure) in &mut cameras {
        let index = sensor.0.index();
        if capture::mode(&snapshot, sensor.0) != SensorFaultMode::Nominal {
            camera.is_active = false;
            for request in requests.iter().filter(|request| request.camera == sensor.0) {
                capture::reject(
                    request.token,
                    request.camera,
                    "Camera delivery changed before capture rendering.",
                );
            }
            continue;
        }
        let aim = aims[index];
        *transform = mounted_pose(sensor.0, &snapshot, aim);
        // The debug camera's first-person lens and dolly controls never reach here.
        *projection = Projection::Perspective(optics::projection(&settings().ship_cameras));
        exposure.ev100 = settings().ship_cameras.exposure_ev100;
        let requested = requests.iter().any(|request| request.camera == sensor.0);
        camera.is_active = due || requested;
        if !camera.is_active {
            continue;
        }
        let frame = info(sensor.0, aim, *transform, snapshot.time_s, images.sequence);
        for request in requests.iter().filter(|request| request.camera == sensor.0) {
            if request.trial_revision != trial_revision
                || request.sensor_revision != sensor_revision
            {
                capture::reject(
                    request.token,
                    request.camera,
                    "Capture request belongs to an earlier sensor policy or trial.",
                );
                continue;
            }
            commands
                .spawn((
                    ReadbackOnce::texture(images.images[index].clone()),
                    CaptureTicket {
                        token: request.token,
                        info: frame.clone(),
                        deadline_wall_s: now + 15.0,
                        trial_revision,
                        sensor_revision,
                    },
                ))
                .observe(capture::complete);
        }
        STATE.with(|state| state.borrow_mut().frames[index] = Some(frame.clone()));
        images.frames[index] = Some(frame);
    }
}

#[cfg(all(test, not(target_arch = "wasm32")))]
mod tests {
    use super::*;
    use crate::{model::PhysicsCommand, physics::Simulation};
    use bevy::camera::CameraProjection;

    #[test]
    fn a_native_camera_fault_disables_render_activation_and_never_claims_completed_pixels() {
        use crate::{
            api::Dispatch,
            model::{SensorFaultMode, SensorFaultTarget},
        };
        let _restore = RestoreState::fresh();
        let mut experiment = crate::model::ExperimentSettings::default();
        experiment.debris.count = 0;
        crate::session::dispatch(Dispatch::Physics(PhysicsCommand::ConfigureExperiment {
            settings: experiment,
            seed: Some(100),
        }))
        .unwrap();
        crate::session::dispatch(Dispatch::Physics(PhysicsCommand::SetSensorFault {
            target: SensorFaultTarget::AllAvionics,
            mode: SensorFaultMode::Unavailable,
            duration_sim_s: Some(1.0),
        }))
        .unwrap();
        let mut assets = Assets::<Image>::default();
        let handles: [Handle<Image>; 2] = std::array::from_fn(|_| {
            assets.add(Image::new_target_texture(
                512,
                512,
                TextureFormat::Rgba8UnormSrgb,
                None,
            ))
        });
        let mut app = App::new();
        app.insert_resource(assets)
            .insert_resource(Time::<Real>::default())
            .insert_resource(SensorImages {
                images: handles,
                last_sample_wall_s: f64::NEG_INFINITY,
                sequence: 0,
                frames: [None, None],
                trial_revision: crate::session::trial_revision(),
            })
            .add_systems(Update, update);
        let cameras = [ShipCameraId::Top, ShipCameraId::Bottom].map(|id| {
            app.world_mut()
                .spawn((
                    SensorCamera(id),
                    Transform::IDENTITY,
                    Camera::default(),
                    Projection::Perspective(optics::projection(&settings().ship_cameras)),
                    Exposure { ev100: 15.0 },
                ))
                .id()
        });
        app.update();
        for entity in cameras {
            assert!(!app.world().get::<Camera>(entity).unwrap().is_active);
        }
        assert!(
            app.world()
                .resource::<SensorImages>()
                .frames
                .iter()
                .all(Option::is_none)
        );
        for camera in [ShipCameraId::Top, ShipCameraId::Bottom] {
            assert!(capture::cached_info(camera).is_none());
        }
        let unavailable: serde_json::Value = serde_json::from_str(&status()).unwrap();
        for camera in unavailable["cameras"].as_array().unwrap() {
            assert!(camera["position_m"].is_null());
            assert!(camera["rotation"].is_null());
            assert!(camera["simulation_time_s"].is_null());
        }
        crate::session::dispatch(Dispatch::Physics(PhysicsCommand::SetSensorFault {
            target: SensorFaultTarget::AllAvionics,
            mode: SensorFaultMode::Frozen,
            duration_sim_s: Some(1.0),
        }))
        .unwrap();
        app.update();
        let frozen: serde_json::Value = serde_json::from_str(&status()).unwrap();
        assert_eq!(frozen["views"], serde_json::json!([]));
        for entity in cameras {
            assert!(!app.world().get::<Camera>(entity).unwrap().is_active);
        }
        // No RenderPlugin/readback event is supplied. This tests the real native
        // gating decision only, not successful GPU rendering or synthetic pixels.
        crate::session::dispatch(Dispatch::Physics(PhysicsCommand::ClearSensorFaults)).unwrap();
        crate::session::dispatch(Dispatch::Physics(PhysicsCommand::Step { seconds: 0.1 })).unwrap();
    }

    fn close(actual: Vec3, expected: Vec3) {
        assert!(
            actual.distance(expected) < 3e-6,
            "{actual:?} != {expected:?}"
        );
    }

    struct RestoreState(Option<SensorState>);

    impl RestoreState {
        fn fresh() -> Self {
            Self(Some(
                STATE.with(|state| state.replace(SensorState::default())),
            ))
        }
    }

    impl Drop for RestoreState {
        fn drop(&mut self) {
            STATE.with(|state| *state.borrow_mut() = self.0.take().unwrap());
        }
    }

    fn cached_metadata_buffers() -> [[*const u8; 3]; 2] {
        STATE.with(|state| {
            state.borrow().frames.each_ref().map(|frame| {
                let frame = frame.as_ref().expect("sampled camera metadata");
                [
                    frame.field_of_view_source_url.as_ptr(),
                    frame.mount_source_url.as_ptr(),
                    frame.aim_source_url.as_ptr(),
                ]
            })
        })
    }

    #[test]
    fn public_pan_tilt_updates_real_bevy_mounts_between_samples_without_a_gpu_readback() {
        let _restore = RestoreState::fresh();
        let snapshot = crate::snapshot();
        let before = serde_json::to_value(&*snapshot).unwrap();
        let top_report: serde_json::Value =
            serde_json::from_str(&pan_tilt(ShipCameraId::Top, 30.0, -10.0).unwrap()).unwrap();
        assert_eq!(top_report["configured"], false);
        assert_eq!(top_report["cameras"][0]["yaw_deg"], 30.0);
        assert_eq!(top_report["cameras"][0]["pitch_deg"], -10.0);
        assert_eq!(top_report["views"].as_array().unwrap().len(), 0);
        pan_tilt(ShipCameraId::Bottom, -20.0, 15.0).unwrap();
        let unchanged = status();
        assert!(pan_tilt(ShipCameraId::Top, 0.0, f64::NAN).is_err());
        assert_eq!(status(), unchanged);

        let config = &settings().ship_cameras;
        let mut assets = Assets::<Image>::default();
        let handles: [Handle<Image>; 2] = std::array::from_fn(|_| {
            assets.add(Image::new_target_texture(
                config.resolution_px[0],
                config.resolution_px[1],
                TextureFormat::Rgba8UnormSrgb,
                None,
            ))
        });
        let mut app = App::new();
        app.insert_resource(assets)
            .insert_resource(Time::<Real>::default())
            .insert_resource(SensorImages {
                images: handles.clone(),
                last_sample_wall_s: 0.0,
                sequence: 0,
                frames: [None, None],
                trial_revision: crate::session::trial_revision(),
            })
            .add_systems(Update, update);
        let entities = [ShipCameraId::Top, ShipCameraId::Bottom].map(|id| {
            app.world_mut()
                .spawn((
                    SensorCamera(id),
                    Camera3d::default(),
                    Camera::default(),
                    RenderTarget::Image(handles[id.index()].clone().into()),
                    Transform::IDENTITY,
                    Projection::Perspective(PerspectiveProjection {
                        fov: 0.3,
                        ..default()
                    }),
                    Exposure { ev100: 3.0 },
                ))
                .id()
        });
        app.update();
        for (id, entity) in [ShipCameraId::Top, ShipCameraId::Bottom]
            .into_iter()
            .zip(entities)
        {
            let expected = pose(id, &snapshot);
            let transform = app.world().get::<Transform>(entity).unwrap();
            assert_eq!(*transform, expected);
            assert!(!app.world().get::<Camera>(entity).unwrap().is_active);
            let Projection::Perspective(lens) = app.world().get::<Projection>(entity).unwrap()
            else {
                panic!("mounted pinhole projection")
            };
            assert_eq!(lens.fov, optics::projection(config).fov);
            assert_eq!(lens.aspect_ratio, 1.0);
            assert_eq!(app.world().get::<Exposure>(entity).unwrap().ev100, 15.0);
        }
        assert_eq!(app.world().resource::<SensorImages>().sequence, 0);
        assert_eq!(serde_json::to_value(&*crate::snapshot()).unwrap(), before);

        let period = std::time::Duration::from_secs_f64(1.0 / config.cadence_hz);
        app.world_mut()
            .resource_mut::<Time<Real>>()
            .advance_by(period);
        app.update();
        let images = app.world().resource::<SensorImages>();
        assert_eq!(images.sequence, 1);
        let sampled = serde_json::to_value(&images.frames).unwrap();
        STATE.with(|state| {
            assert_eq!(
                serde_json::to_value(&state.borrow().frames).unwrap(),
                sampled
            );
        });
        // Compare public metadata through the same JSON text boundary. Direct
        // to_value widens f32 values, whereas text uses their shortest decimals.
        let sampled_views =
            serde_json::from_str::<serde_json::Value>(&status()).unwrap()["views"].clone();
        let cached_buffers = cached_metadata_buffers();
        let ticket_info = images.frames[0].as_ref().unwrap().clone();
        let ticket = app
            .world_mut()
            .spawn(CaptureTicket {
                token: 23,
                info: ticket_info,
                deadline_wall_s: 15.0,
                trial_revision: crate::session::trial_revision(),
                sensor_revision: crate::session::sensor_revision(),
            })
            .id();

        for yaw in [40.0, 50.0, 60.0] {
            let current: serde_json::Value =
                serde_json::from_str(&pan_tilt(ShipCameraId::Top, yaw, -20.0).unwrap()).unwrap();
            assert_eq!(current["cameras"][0]["yaw_deg"], yaw);
            assert_eq!(current["cameras"][0]["pitch_deg"], -20.0);
            assert_eq!(current["views"], sampled_views);
            app.world_mut()
                .resource_mut::<Time<Real>>()
                .advance_by(period / 4);
            app.update();
            for (id, entity) in [ShipCameraId::Top, ShipCameraId::Bottom]
                .into_iter()
                .zip(entities)
            {
                assert_eq!(
                    *app.world().get::<Transform>(entity).unwrap(),
                    pose(id, &snapshot)
                );
                assert!(!app.world().get::<Camera>(entity).unwrap().is_active);
            }
            let images = app.world().resource::<SensorImages>();
            assert_eq!(images.sequence, 1);
            assert_eq!(images.last_sample_wall_s, period.as_secs_f64());
            assert_eq!(serde_json::to_value(&images.frames).unwrap(), sampled);
            STATE.with(|state| {
                assert_eq!(
                    serde_json::to_value(&state.borrow().frames).unwrap(),
                    sampled
                );
            });
            // Equal metadata alone would not catch cloning the cached strings.
            assert_eq!(cached_metadata_buffers(), cached_buffers);
            assert_eq!(
                serde_json::to_value(&app.world().get::<CaptureTicket>(ticket).unwrap().info)
                    .unwrap(),
                sampled[0]
            );
        }

        app.world_mut()
            .resource_mut::<Time<Real>>()
            .advance_by(period);
        app.update();
        let images = app.world().resource::<SensorImages>();
        assert_eq!(images.sequence, 2);
        STATE.with(|state| {
            let state = state.borrow();
            assert_eq!(
                serde_json::to_value(&state.frames).unwrap(),
                serde_json::to_value(&images.frames).unwrap()
            );
            assert_eq!(state.frames[0].as_ref().unwrap().mount_yaw_deg, 60.0);
            assert_eq!(state.frames[0].as_ref().unwrap().mount_pitch_deg, -20.0);
            for frame in state.frames.iter().flatten() {
                assert_eq!(frame.sequence, 2);
            }
        });
        assert_eq!(
            serde_json::to_value(&app.world().get::<CaptureTicket>(ticket).unwrap().info).unwrap(),
            sampled[0]
        );
        assert_eq!(serde_json::to_value(&*crate::snapshot()).unwrap(), before);
    }

    #[test]
    fn helper_limits_clip_finite_values_and_reject_nonfinite_requests_atomically() {
        let mut state = SensorState::default();
        for camera in [ShipCameraId::Top, ShipCameraId::Bottom] {
            for yaw in [-f64::MAX, -89.0, -0.25, 0.0, 89.0, f64::MAX] {
                for pitch in [-f64::MAX, -50.0, 0.0, 50.0, f64::MAX] {
                    let other = state.aims[1 - camera.index()];
                    state.pan_tilt(camera, yaw, pitch).unwrap();
                    let aim = state.aims[camera.index()];
                    let [lo, hi] = camera.pitch_limits_deg();
                    assert_eq!(aim.yaw_deg, yaw.clamp(-89.0, 89.0));
                    assert_eq!(aim.pitch_deg, pitch.clamp(lo, hi));
                    assert_eq!(state.aims[1 - camera.index()], other);
                }
            }
            let before = state.aims;
            for invalid in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
                for (yaw, pitch) in [(invalid, 15.0), (25.0, invalid), (invalid, invalid)] {
                    assert!(state.pan_tilt(camera, yaw, pitch).is_err());
                    assert_eq!(state.aims, before);
                }
            }
        }
        for mount in settings().ship_cameras.mounts {
            assert_eq!(
                mount.yaw_limits_deg.map(f64::from),
                mount.id.yaw_limits_deg()
            );
            assert_eq!(
                mount.pitch_limits_deg.map(f64::from),
                mount.id.pitch_limits_deg()
            );
        }
        assert_eq!(
            settings().ship_cameras.mounts[0].position_m,
            [1.27, 0.0, 0.9]
        );
        assert_eq!(
            settings().ship_cameras.mounts[1].position_m,
            [1.675, 0.0, -1.025]
        );
    }

    #[test]
    fn actual_ship_attitudes_rotate_each_mounted_optical_basis_at_all_helper_edges() {
        let mut simulation = Simulation::new_default();
        for attitude in [
            Quat::IDENTITY,
            Quat::from_rotation_z(std::f32::consts::FRAC_PI_2),
            Quat::from_rotation_y(std::f32::consts::PI),
            Quat::from_rotation_z(-0.8) * Quat::from_rotation_y(0.6) * Quat::from_rotation_x(1.1),
        ] {
            let mut restored = simulation.snapshot();
            restored
                .assets
                .iter_mut()
                .find(|asset| asset.id == EntityId::Lander)
                .unwrap()
                .rotation = attitude.to_array().map(f64::from);
            let snapshot = simulation
                .execute(PhysicsCommand::Restore {
                    state: Box::new(restored),
                })
                .unwrap();
            let before = serde_json::to_value(&snapshot).unwrap();
            let ship = snapshot
                .assets
                .iter()
                .find(|asset| asset.id == EntityId::Lander)
                .unwrap();
            let ship_position = Vec3::from_array(ship.position.map(|value| (value * 0.001) as f32));
            for camera in [ShipCameraId::Top, ShipCameraId::Bottom] {
                for yaw in [-89.0, 0.0, 89.0] {
                    for pitch in camera.pitch_limits_deg() {
                        let aim = PanTiltAim::clipped(camera, yaw, pitch).unwrap();
                        let transform = mounted_pose(camera, &snapshot, aim);
                        let yaw = yaw.to_radians() as f32;
                        let pitch = pitch.to_radians() as f32;
                        let forward = Vec3::new(
                            pitch.cos() * yaw.cos(),
                            pitch.cos() * yaw.sin(),
                            -pitch.sin(),
                        );
                        let up = Vec3::new(
                            pitch.sin() * yaw.cos(),
                            pitch.sin() * yaw.sin(),
                            pitch.cos(),
                        );
                        close(transform.rotation * Vec3::NEG_Z, attitude * forward);
                        close(transform.rotation * Vec3::Y, attitude * up);
                        let mount = settings().ship_cameras.mounts[camera.index()];
                        let offset =
                            Vec3::from_array(mount.position_m.map(|value| (value * 0.001) as f32));
                        close(transform.translation, ship_position + attitude * offset);
                        let frame = info(camera, aim, transform, snapshot.time_s, 41);
                        let captured_rotation = Quat::from_array(frame.rotation);
                        close(captured_rotation * Vec3::NEG_Z, attitude * forward);
                        close(captured_rotation * Vec3::Y, attitude * up);
                        assert_eq!(frame.mount_yaw_deg, aim.yaw_deg);
                        assert_eq!(frame.mount_pitch_deg, aim.pitch_deg);
                        assert_eq!(frame.sequence, 41);
                    }
                }
            }
            assert_eq!(serde_json::to_value(simulation.snapshot()).unwrap(), before);
        }
    }

    #[test]
    fn pan_tilt_status_changes_before_sampling_without_rewriting_a_capture_ticket() {
        let snapshot = Simulation::new_default().snapshot();
        let mut state = SensorState::default();
        let camera = ShipCameraId::Top;
        let old_aim = state.aims[camera.index()];
        let sampled = info(
            camera,
            old_aim,
            mounted_pose(camera, &snapshot, old_aim),
            snapshot.time_s,
            7,
        );
        let ticket = CaptureTicket {
            token: 23,
            info: sampled.clone(),
            deadline_wall_s: 15.0,
            trial_revision: crate::session::trial_revision(),
            sensor_revision: crate::session::sensor_revision(),
        };
        state.frames[camera.index()] = Some(sampled);
        state.pan_tilt(camera, 30.0, -10.0).unwrap();
        let status = report(&state, &snapshot);
        assert!(!status.configured);
        assert_eq!(status.cameras[0].yaw_deg, 30.0);
        assert_eq!(status.cameras[0].pitch_deg, -10.0);
        assert_eq!(status.cameras[1].yaw_deg, 0.0);
        assert_eq!(status.cameras[1].pitch_deg, 0.0);
        assert_eq!(status.views[0].mount_yaw_deg, 0.0);
        assert_eq!(ticket.info.mount_yaw_deg, 0.0);
        assert_eq!(ticket.info.mount_pitch_deg, 0.0);
        assert_eq!(ticket.info.rotation, status.views[0].rotation);
        assert_ne!(ticket.info.rotation, status.cameras[0].rotation);
        let next_aim = state.aims[0];
        let next = info(
            camera,
            next_aim,
            mounted_pose(camera, &snapshot, next_aim),
            snapshot.time_s,
            8,
        );
        assert_eq!(next.rotation, status.cameras[0].rotation);
        assert_eq!(next.mount_yaw_deg, 30.0);
        assert_eq!(next.mount_pitch_deg, -10.0);
    }

    #[test]
    fn new_and_reset_snapshots_determine_mount_positions_not_a_cached_ship_body() {
        let mut simulation = Simulation::new_default();
        let mut state = SensorState::default();
        state.pan_tilt(ShipCameraId::Top, -89.0, -50.0).unwrap();
        state.pan_tilt(ShipCameraId::Bottom, 89.0, 50.0).unwrap();
        let initial = simulation.snapshot();
        let mut moved = initial.clone();
        let lander = moved
            .assets
            .iter_mut()
            .find(|asset| asset.id == EntityId::Lander)
            .unwrap();
        lander.position = [20000.0, 1000.0, 500.0];
        lander.rotation = Quat::from_rotation_y(0.4).to_array().map(f64::from);
        let moved = simulation
            .execute(PhysicsCommand::Restore {
                state: Box::new(moved),
            })
            .unwrap();
        let moved_report = report(&state, &moved);
        let reset = simulation
            .execute(PhysicsCommand::Reset {
                seed: Some(initial.seed),
            })
            .unwrap();
        let before = serde_json::to_value(&reset).unwrap();
        let reset_report = report(&state, &reset);
        for camera in [ShipCameraId::Top, ShipCameraId::Bottom] {
            let index = camera.index();
            assert_ne!(
                moved_report.cameras[index].position_m,
                reset_report.cameras[index].position_m
            );
            let expected = mounted_pose(camera, &reset, state.aims[index]);
            assert_eq!(
                reset_report.cameras[index].rotation,
                expected.rotation.to_array()
            );
            assert_eq!(
                reset_report.cameras[index].yaw_deg,
                state.aims[index].yaw_deg
            );
        }
        assert_eq!(serde_json::to_value(simulation.snapshot()).unwrap(), before);
    }

    #[test]
    fn real_radar_displacements_and_frame_quaternions_use_the_same_world_to_camera_adapter() {
        let simulation = Simulation::new_default();
        let snapshot = simulation.snapshot();
        let before = serde_json::to_value(&snapshot).unwrap();
        assert!(!snapshot.observations.is_empty());
        let ship = snapshot
            .assets
            .iter()
            .find(|asset| asset.id == EntityId::Lander)
            .unwrap();
        let attitude = Quat::from_array(ship.rotation.map(|value| value as f32)).normalize();
        for camera in [ShipCameraId::Top, ShipCameraId::Bottom] {
            let pitch = if camera == ShipCameraId::Top {
                -20.0
            } else {
                20.0
            };
            let aim = PanTiltAim::clipped(camera, 30.0, pitch).unwrap();
            let transform = mounted_pose(camera, &snapshot, aim);
            let frame = info(camera, aim, transform, snapshot.time_s, 1);
            let rotation = Quat::from_array(frame.rotation);
            let mount = Vec3::from_array(frame.mount_position_body_m.map(|value| value as f32));
            for reading in &snapshot.observations {
                let from_ship =
                    Vec3::from_array(reading.relative_position_m.map(|value| value as f32));
                let from_sensor_m = from_ship - attitude * mount;
                let camera_vector_m = rotation.inverse() * from_sensor_m;
                let world_point_km =
                    Vec3::from_array(ship.position.map(|value| (value * 0.001) as f32))
                        + from_ship * 0.001;
                let projected_vector_m = transform.rotation.inverse()
                    * (world_point_km - transform.translation)
                    * 1000.0;
                let tolerance_m = 0.005 + camera_vector_m.length() * f32::EPSILON * 8.0;
                assert!(camera_vector_m.distance(projected_vector_m) < tolerance_m);
            }
            let projection = optics::projection(&settings().ship_cameras);
            let center_world = transform.translation + rotation * Vec3::NEG_Z;
            let center_view = transform.rotation.inverse() * (center_world - transform.translation);
            close(
                projection
                    .get_clip_from_view()
                    .project_point3(center_view)
                    .with_z(0.0),
                Vec3::ZERO,
            );
            assert_eq!(frame.width, 512);
            assert_eq!(frame.height, 512);
            assert_eq!(frame.channels, 3);
            assert_eq!(frame.bits_per_channel, 8);
            assert_eq!(frame.field_of_view_deg, 60.0);
            assert!((frame.horizontal_field_of_view_deg - 60.0).abs() < 0.0001);
            assert!((frame.vertical_field_of_view_deg - 60.0).abs() < 0.0001);
            assert!(matches!(
                frame.field_of_view_status,
                crate::scenario::CameraSourceStatus::DocumentedExample
            ));
            assert_eq!(frame.exposure_ev100, 15.0);
            assert_eq!(frame.ambient_brightness, 0.0);
            assert!(!frame.debug_overlays);
        }
        assert!(!sensor_layers().intersects(&RenderLayers::layer(1)));
        assert!(sensor_layers().intersects(&RenderLayers::layer(0)));
        assert_eq!(serde_json::to_value(simulation.snapshot()).unwrap(), before);
    }
}
