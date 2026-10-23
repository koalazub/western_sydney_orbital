use super::{capture, model::*, optics};
use crate::{
    camera::SimulationCamera,
    domain::{EntityId, ShipCameraId},
    model::Snapshot,
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

thread_local! { static STATUS: RefCell<Option<SensorStatus>> = const { RefCell::new(None) }; }

pub(crate) fn status() -> String {
    STATUS.with(|state| serde_json::to_string(&*state.borrow()).expect("sensor metadata is finite"))
}

pub(super) fn configured() -> bool {
    STATUS.with(|state| state.borrow().is_some())
}

pub(crate) fn pose(id: ShipCameraId, state: &Snapshot) -> Transform {
    let mount = &settings().ship_cameras.mounts[id.index()];
    let ship = state
        .assets
        .iter()
        .find(|asset| asset.id == EntityId::Lander)
        .expect("scenario has a lander");
    let attitude = Quat::from_array(ship.rotation.map(|value| value as f32)).normalize();
    let position = Vec3::from_array(ship.position.map(|value| (value * 0.001) as f32));
    let offset = Vec3::from_array(mount.position_m.map(|value| (value * 0.001) as f32));
    let (forward, up) = optics::body_axes(mount);
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
            RenderLayers::layer(0).with(2),
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
    });
}

fn info(id: ShipCameraId, transform: Transform, time_s: f64, sequence: u64) -> FrameInfo {
    let config = &settings().ship_cameras;
    let angles = optics::field_of_view(config);
    let mount = &config.mounts[id.index()];
    let (forward, up) = optics::body_axes(mount);
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
        mount_position_body_m: mount.position_m,
        mount_forward_body: forward.to_array(),
        mount_up_body: up.to_array(),
        mount_yaw_deg: mount.aim_yaw_deg,
        mount_pitch_deg: mount.aim_pitch_deg,
        mount_yaw_limits_deg: mount.yaw_limits_deg,
        mount_pitch_limits_deg: mount.pitch_limits_deg,
        fixed_ship_mount: true,
        debug_overlays: false,
    }
}

pub(super) fn update(
    mut commands: Commands,
    time: Res<Time<Real>>,
    mut images: ResMut<SensorImages>,
    mut cameras: Query<(&SensorCamera, &mut Transform, &mut Camera), Without<SimulationCamera>>,
    tickets: Query<(Entity, &CaptureTicket)>,
) {
    let now = time.elapsed_secs_f64();
    for (entity, ticket) in &tickets {
        if now > ticket.deadline_wall_s {
            capture::reject(
                ticket.token,
                ticket.info.camera,
                "Sensor readback timed out",
            );
            commands.entity(entity).despawn();
        }
    }
    let requests = capture::drain();
    let due = now - images.last_sample_wall_s >= 1.0 / settings().ship_cameras.cadence_hz;
    if !due && requests.is_empty() {
        for (_, _, mut camera) in &mut cameras {
            camera.is_active = false;
        }
        return;
    }
    images.last_sample_wall_s = now;
    images.sequence = images.sequence.wrapping_add(1);
    let snapshot = crate::snapshot();
    for (sensor, mut transform, mut camera) in &mut cameras {
        let requested = requests.iter().any(|request| request.camera == sensor.0);
        camera.is_active = due || requested;
        if !camera.is_active {
            continue;
        }
        *transform = pose(sensor.0, &snapshot);
        let frame = info(sensor.0, *transform, snapshot.time_s, images.sequence);
        for request in requests.iter().filter(|request| request.camera == sensor.0) {
            commands
                .spawn((
                    ReadbackOnce::texture(images.images[sensor.0.index()].clone()),
                    CaptureTicket {
                        token: request.token,
                        info: frame.clone(),
                        deadline_wall_s: now + 15.0,
                    },
                ))
                .observe(capture::complete);
        }
        images.frames[sensor.0.index()] = Some(frame);
    }
    STATUS.with(|status| {
        *status.borrow_mut() = Some(SensorStatus {
            configured: true,
            capture_hz: settings().ship_cameras.cadence_hz,
            views: images.frames.iter().flatten().cloned().collect(),
        })
    });
}
