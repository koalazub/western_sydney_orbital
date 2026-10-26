use super::{model::*, optics};
use crate::{
    camera::CameraRig,
    hud::{LayoutPart, PANEL_GAP_PX, SCREEN_PADDING_PX},
    scenario::{CameraSourceStatus, settings},
};
use bevy::{picking::Pickable, prelude::*, window::PrimaryWindow};

pub(super) fn setup(mut commands: Commands, images: Res<SensorImages>) {
    commands.spawn((
        Node {
            width: percent(100),
            height: percent(100),
            display: Display::None,
            flex_direction: FlexDirection::Column,
            justify_content: JustifyContent::Center,
            align_items: AlignItems::Center,
            row_gap: px(8),
            ..default()
        },
        GlobalZIndex(-1),
        BackgroundColor(Color::BLACK),
        Pickable::IGNORE,
        SensorPanel,
        children![
            (
                Text::new("Ship camera | 512x512 RGB8/ch\nFixed sensor FOV"),
                Node {
                    max_width: percent(92),
                    flex_shrink: 0.0,
                    ..default()
                },
                TextFont {
                    font_size: FontSize::Px(12.0),
                    ..default()
                },
                SensorCaption,
                Pickable::IGNORE
            ),
            (
                Node {
                    width: px(512),
                    height: px(512),
                    aspect_ratio: Some(1.0),
                    flex_shrink: 0.0,
                    ..default()
                },
                ImageNode::new(images.images[0].clone()),
                SensorPicture,
                Pickable::IGNORE
            ),
        ],
    ));
}

pub(super) fn update(
    view: (Res<CameraRig>, Res<UiScale>),
    images: Res<SensorImages>,
    windows: Query<&Window, With<PrimaryWindow>>,
    chrome: Query<(&LayoutPart, &ComputedNode)>,
    mut panels: Query<&mut Node, (With<SensorPanel>, Without<SensorPicture>)>,
    mut pictures: Query<(&mut Node, &mut ImageNode), With<SensorPicture>>,
    mut captions: Query<(&mut Text, &ComputedNode), With<SensorCaption>>,
) {
    let (rig, ui_scale) = view;
    let selected = rig.mode.sensor();
    for mut node in &mut panels {
        let display = if selected.is_some() {
            Display::Flex
        } else {
            Display::None
        };
        if node.display != display {
            node.display = display;
        }
    }
    let Some(id) = selected else {
        return;
    };
    let snapshot = crate::snapshot();
    let delivery_mode = super::capture::mode(&snapshot, id);
    let config = &settings().ship_cameras;
    let scale = if ui_scale.0.is_finite() && ui_scale.0 > 0.0 {
        ui_scale.0
    } else {
        1.0
    };
    let width = windows
        .single()
        .map(|window| window.width() / scale)
        .unwrap_or(800.0);
    let height = windows
        .single()
        .map(|window| window.height() / scale)
        .unwrap_or(800.0);
    let mut top = SCREEN_PADDING_PX + PANEL_GAP_PX;
    let mut bottom = SCREEN_PADDING_PX + PANEL_GAP_PX;
    for (part, computed) in &chrome {
        let height = computed.size().y * computed.inverse_scale_factor();
        match part {
            LayoutPart::TopRow => top += height,
            LayoutPart::BottomRow => bottom += height,
            LayoutPart::CameraPanel | LayoutPart::SensorActions => {}
        }
    }
    for mut node in &mut panels {
        if node.padding.top != px(top) {
            node.padding.top = px(top);
        }
        if node.padding.bottom != px(bottom) {
            node.padding.bottom = px(bottom);
        }
    }
    let caption_height = captions
        .iter()
        .map(|(_, computed)| computed.size().y * computed.inverse_scale_factor())
        .fold(32.0_f32, f32::max);
    let size = (width * 0.92)
        .min((height - top - bottom - caption_height - 8.0).max(0.0))
        .min(config.resolution_px[0] as f32);
    for (mut node, mut image) in &mut pictures {
        node.display = if delivery_mode == crate::model::SensorFaultMode::Nominal {
            Display::Flex
        } else {
            // Hide the live texture rather than substituting black/fake pixels.
            Display::None
        };
        if node.width != px(size) {
            node.width = px(size);
        }
        if node.height != px(size) {
            node.height = px(size);
        }
        if image.image != images.images[id.index()] {
            image.image = images.images[id.index()].clone();
        }
    }
    let angles = optics::field_of_view(config);
    let fov_status = match config.field_of_view_source.status {
        CameraSourceStatus::PracticeUnverified => "unverified",
        CameraSourceStatus::DocumentedExample => "STP example",
        CameraSourceStatus::DocumentedStp => "documented STP",
    };
    for (mut text, _) in &mut captions {
        let sample = images.frames[id.index()].as_ref();
        let sampled_time = sample.map_or(0.0, |frame| frame.simulation_time_s);
        let sampled_yaw = sample.map_or(0.0, |frame| frame.mount_yaw_deg);
        let sampled_pitch = sample.map_or(0.0, |frame| frame.mount_pitch_deg);
        let value = format!(
            "Ship {} | {}×{} RGB8/ch | T+{:.1}s\nFixed FOV H{:.0}° V{:.0}° | EV{:.1} | {}\nSample yaw {:+.1}° | pitch {:+.1}°",
            id.label(),
            config.resolution_px[0],
            config.resolution_px[1],
            sampled_time,
            angles.horizontal_deg,
            angles.vertical_deg,
            config.exposure_ev100,
            fov_status,
            sampled_yaw,
            sampled_pitch,
        );
        let value = match delivery_mode {
            crate::model::SensorFaultMode::Nominal => value,
            crate::model::SensorFaultMode::Unavailable => {
                format!("Ship {} | Sensor delivery unavailable", id.label())
            }
            crate::model::SensorFaultMode::Frozen => format!(
                "Ship {} | Sensor delivery frozen\nCapture returns only previously completed RGB, if available",
                id.label()
            ),
        };
        let value = crate::hud::readable_text(&value);
        if text.0 != value {
            text.0 = value;
        }
    }
}
