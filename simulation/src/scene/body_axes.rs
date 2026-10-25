//! Snapshot-only body-frame guide, excluded from mounted RGB camera feeds.
use bevy::{
    math::{DQuat, DVec3},
    prelude::*,
};

use super::model::RenderScale;

pub(crate) fn install(app: &mut App) {
    app.add_systems(Update, draw.in_set(crate::EngineSet::Scene));
}

fn draw(rig: Res<crate::camera::CameraRig>, mut gizmos: Gizmos) {
    // Sensors::setup puts default gizmos on layer 1. Mounted cameras use 0/2.
    if rig.mode.sensor().is_some() || !crate::scene::visual_settings().guides {
        return;
    }
    let snapshot = crate::snapshot();
    let Some(ship) = snapshot
        .assets
        .iter()
        .find(|entity| entity.id == snapshot.mission.shuttle_id)
    else {
        return;
    };
    let Some(axes) = body_axes(ship) else {
        return;
    };
    // Length and the matching HUD legend also identify the axes without color.
    for ((start, end), color) in axes.into_iter().zip([
        Color::srgb(1.0, 0.58, 0.42),
        Color::srgb(0.55, 0.95, 0.65),
        Color::srgb(0.58, 0.78, 1.0),
    ]) {
        gizmos.arrow(start, end, color);
    }
}

fn body_axes(ship: &crate::model::EntityState) -> Option<[(Vec3, Vec3); 3]> {
    let position = DVec3::from_array(ship.position);
    let rotation = DQuat::from_array(ship.rotation);
    if !position.is_finite() || !rotation.is_finite() || rotation.length_squared() <= 0.0 {
        return None;
    }
    let rotation = rotation.normalize();
    let origin = RenderScale::position(position.to_array());
    Some(
        [DVec3::X * 16.0, DVec3::Y * 14.0, DVec3::Z * 12.0].map(|axis| {
            (
                origin,
                RenderScale::position((position + rotation * axis).to_array()),
            )
        }),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn body_guide_uses_actual_ship_attitude_not_the_view_camera() {
        let snapshot = crate::physics::Simulation::new_default().snapshot();
        let ship = snapshot
            .assets
            .iter()
            .find(|entity| entity.id == snapshot.mission.shuttle_id)
            .unwrap();
        let axes = body_axes(ship).unwrap();
        let rotation = DQuat::from_array(ship.rotation).normalize();
        for ((start, end), local) in
            axes.into_iter()
                .zip([DVec3::X * 16.0, DVec3::Y * 14.0, DVec3::Z * 12.0])
        {
            let expected = (rotation * local * 0.001).as_vec3();
            assert!((end - start - expected).length() < 1e-5);
        }
    }
}
