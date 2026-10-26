use crate::domain::ShipCameraId;
use crate::scenario::{CameraSourceStatus, FieldOfViewConvention};
use bevy::prelude::*;
use serde::Serialize;

#[derive(Component)]
pub(crate) struct SensorCamera(pub ShipCameraId);

#[derive(Clone, Copy, Debug, PartialEq)]
pub(super) struct PanTiltAim {
    pub yaw_deg: f64,
    pub pitch_deg: f64,
}

impl PanTiltAim {
    pub fn clipped(camera: ShipCameraId, yaw_deg: f64, pitch_deg: f64) -> Result<Self, String> {
        if !yaw_deg.is_finite() || !pitch_deg.is_finite() {
            return Err("Camera yaw and pitch must both be finite degrees".into());
        }
        let [yaw_min, yaw_max] = camera.yaw_limits_deg();
        let [pitch_min, pitch_max] = camera.pitch_limits_deg();
        Ok(Self {
            yaw_deg: yaw_deg.clamp(yaw_min, yaw_max),
            pitch_deg: pitch_deg.clamp(pitch_min, pitch_max),
        })
    }
}

pub(super) struct SensorState {
    pub configured: bool,
    pub aims: [PanTiltAim; 2],
    pub frames: [Option<FrameInfo>; 2],
}

impl Default for SensorState {
    fn default() -> Self {
        Self {
            configured: false,
            aims: crate::scenario::settings()
                .ship_cameras
                .mounts
                .map(|mount| {
                    PanTiltAim::clipped(
                        mount.id,
                        f64::from(mount.aim_yaw_deg),
                        f64::from(mount.aim_pitch_deg),
                    )
                    .expect("validated initial sensor aim")
                }),
            frames: [None, None],
        }
    }
}

impl SensorState {
    pub fn pan_tilt(
        &mut self,
        camera: ShipCameraId,
        yaw_deg: f64,
        pitch_deg: f64,
    ) -> Result<(), String> {
        let aim = PanTiltAim::clipped(camera, yaw_deg, pitch_deg)?;
        self.aims[camera.index()] = aim;
        Ok(())
    }
}

#[derive(Resource)]
pub(crate) struct SensorImages {
    pub images: [Handle<Image>; 2],
    pub last_sample_wall_s: f64,
    pub sequence: u64,
    pub frames: [Option<FrameInfo>; 2],
    pub trial_revision: u32,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct FrameInfo {
    pub camera: ShipCameraId,
    pub sequence: u64,
    pub width: u32,
    pub height: u32,
    pub channels: u8,
    pub bits_per_channel: u8,
    pub color_space: ColorSpace,
    pub render_format: RenderFormat,
    pub field_of_view_deg: f32,
    pub field_of_view_locked: bool,
    pub field_of_view_convention: FieldOfViewConvention,
    pub field_of_view_status: CameraSourceStatus,
    pub field_of_view_source_url: String,
    pub horizontal_field_of_view_deg: f32,
    pub vertical_field_of_view_deg: f32,
    pub diagonal_field_of_view_deg: f32,
    pub exposure_ev100: f32,
    pub ambient_brightness: f32,
    pub simulation_time_s: f64,
    pub position_m: [f64; 3],
    pub rotation: [f32; 4],
    pub mount_source_url: String,
    pub aim_source_url: String,
    pub mount_position_body_m: [f64; 3],
    pub mount_forward_body: [f32; 3],
    pub mount_up_body: [f32; 3],
    pub mount_yaw_deg: f64,
    pub mount_pitch_deg: f64,
    pub mount_yaw_limits_deg: [f64; 2],
    pub mount_pitch_limits_deg: [f64; 2],
    pub fixed_ship_mount: bool,
    pub debug_overlays: bool,
}

#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum ColorSpace {
    Srgb,
}

#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum RenderFormat {
    Rgba8UnormSrgb,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct SensorStatus {
    pub configured: bool,
    pub capture_hz: f64,
    /// Current commanded mounts and poses, recomputed from the current snapshot.
    pub cameras: [CameraStateInfo; 2],
    /// Last sampled image metadata. Pan/tilt never rewrites these past poses.
    pub views: Vec<FrameInfo>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct CameraStateInfo {
    pub camera: ShipCameraId,
    pub yaw_deg: f64,
    pub pitch_deg: f64,
    pub yaw_limits_deg: [f64; 2],
    pub pitch_limits_deg: [f64; 2],
    pub simulation_time_s: f64,
    pub position_m: [f64; 3],
    pub rotation: [f32; 4],
    pub mount_position_body_m: [f64; 3],
    pub mount_forward_body: [f32; 3],
    pub mount_up_body: [f32; 3],
    pub mount_source_url: String,
    pub aim_source_url: String,
    pub width: u32,
    pub height: u32,
    pub field_of_view_deg: f32,
    pub field_of_view_locked: bool,
    pub field_of_view_convention: FieldOfViewConvention,
    pub field_of_view_status: CameraSourceStatus,
    pub field_of_view_source_url: String,
    pub horizontal_field_of_view_deg: f32,
    pub vertical_field_of_view_deg: f32,
    pub diagonal_field_of_view_deg: f32,
    pub exposure_ev100: f32,
    pub ambient_brightness: f32,
    pub fixed_ship_mount: bool,
    pub debug_overlays: bool,
}

#[derive(Component, Clone)]
pub(super) struct CaptureTicket {
    pub token: u32,
    pub info: FrameInfo,
    pub deadline_wall_s: f64,
    pub trial_revision: u32,
    pub sensor_revision: u32,
}

#[derive(Component)]
pub(super) struct SensorPanel;

#[derive(Component)]
pub(super) struct SensorPicture;

#[derive(Component)]
pub(super) struct SensorCaption;
