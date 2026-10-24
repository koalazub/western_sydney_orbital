use super::model;
use crate::{EngineSet, camera::CameraRig};
use bevy::{math::DVec3, prelude::*};

pub(crate) fn install(app: &mut App) {
    app.add_systems(Update, model::clear_on_reset.in_set(EngineSet::Reports))
        .add_systems(Update, draw.in_set(EngineSet::Scene));
}

fn draw(rig: Res<CameraRig>, mut gizmos: Gizmos) {
    // Sensor setup assigns default gizmos to debug layer 1. Mounted cameras
    // see layers 0/2 only, including their raw RGB render targets.
    if rig.mode.sensor().is_some() {
        return;
    }
    model::with_preview(|preview| {
        let Some(preview) = preview else { return };
        let points = preview.render_points();
        if points.len() >= 2 {
            gizmos.linestrip(
                points
                    .iter()
                    .map(|point| (DVec3::from_array(*point) * 0.001).as_vec3()),
                Color::srgb(1.0, 0.72, 0.18),
            );
        }
        gizmos.sphere(
            Isometry3d::from_translation(
                (DVec3::from_array(preview.goal_center_m) * 0.001).as_vec3(),
            ),
            (preview.goal_radius_m * 0.001) as f32,
            Color::srgb(0.12, 0.75, 0.95),
        );
    });
}
