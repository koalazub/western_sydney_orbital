use super::model::*;
use crate::{
    camera::CameraRig,
    hud::{LayoutPart, PANEL_GAP_PX, SCREEN_PADDING_PX},
    scenario::settings,
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
                Text::new("Ship camera | 512 x 512 | RGB8 per channel"),
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
    rig: Res<CameraRig>,
    images: Res<SensorImages>,
    windows: Query<&Window, With<PrimaryWindow>>,
    chrome: Query<(&LayoutPart, &ComputedNode)>,
    mut panels: Query<&mut Node, (With<SensorPanel>, Without<SensorPicture>)>,
    mut pictures: Query<(&mut Node, &mut ImageNode), With<SensorPicture>>,
    mut captions: Query<&mut Text, With<SensorCaption>>,
) {
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
    let config = &settings().ship_cameras;
    let width = windows.single().map(Window::width).unwrap_or(800.0);
    let height = windows.single().map(Window::height).unwrap_or(800.0);
    let mut top = SCREEN_PADDING_PX + PANEL_GAP_PX;
    let mut bottom = SCREEN_PADDING_PX + PANEL_GAP_PX;
    for (part, computed) in &chrome {
        let height = computed.size().y * computed.inverse_scale_factor();
        match part {
            LayoutPart::TopRow => top += height,
            LayoutPart::BottomRow => bottom += height,
            LayoutPart::MissionActions
            | LayoutPart::MetricsPanel
            | LayoutPart::CameraPanel
            | LayoutPart::SensorActions => {}
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
    let size = (width * 0.92)
        .min((height - top - bottom - 40.0).max(0.0))
        .min(config.resolution_px[0] as f32);
    for (mut node, mut image) in &mut pictures {
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
    for mut text in &mut captions {
        let sampled_time = images.frames[id.index()]
            .as_ref()
            .map_or(0.0, |frame| frame.simulation_time_s);
        let value = format!(
            "Ship {} | {} x {} | RGB8/channel\nFixed EV {:.1} | Frame T+{:.1}s",
            id.label(),
            config.resolution_px[0],
            config.resolution_px[1],
            config.exposure_ev100,
            sampled_time
        );
        if text.0 != value {
            text.0 = value;
        }
    }
}
