use crate::domain::ShipCameraId;
use crate::scenario::{CameraSourceStatus, FieldOfViewConvention};
use bevy::prelude::*;
use serde::Serialize;

#[derive(Component)]
pub(crate) struct SensorCamera(pub ShipCameraId);

#[derive(Resource)]
pub(crate) struct SensorImages {
    pub images: [Handle<Image>; 2],
    pub last_sample_wall_s: f64,
    pub sequence: u64,
    pub frames: [Option<FrameInfo>; 2],
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
    pub mount_position_body_m: [f64; 3],
    pub mount_forward_body: [f32; 3],
    pub mount_up_body: [f32; 3],
    pub mount_yaw_deg: f32,
    pub mount_pitch_deg: f32,
    pub mount_yaw_limits_deg: [f32; 2],
    pub mount_pitch_limits_deg: [f32; 2],
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
    pub views: Vec<FrameInfo>,
}

#[derive(Component, Clone)]
pub(super) struct CaptureTicket {
    pub token: u32,
    pub info: FrameInfo,
    pub deadline_wall_s: f64,
}

#[derive(Component)]
pub(super) struct SensorPanel;

#[derive(Component)]
pub(super) struct SensorPicture;

#[derive(Component)]
pub(super) struct SensorCaption;
