//! Pinhole projection and the documented STP zero-roll pan/tilt convention.
use crate::scenario::{CameraMount, FieldOfViewConvention, ShipCameras};
use bevy::prelude::*;

pub(super) struct FieldOfView {
    pub horizontal_deg: f32,
    pub vertical_deg: f32,
    pub diagonal_deg: f32,
}

pub(super) fn field_of_view(config: &ShipCameras) -> FieldOfView {
    let aspect = f64::from(config.resolution_px[0]) / f64::from(config.resolution_px[1]);
    let half_angle_tangent = (f64::from(config.field_of_view_deg).to_radians() * 0.5).tan();
    let vertical_tangent = match config.field_of_view_convention {
        FieldOfViewConvention::VerticalFullAngle => half_angle_tangent,
        FieldOfViewConvention::HorizontalFullAngle => half_angle_tangent / aspect,
        FieldOfViewConvention::DiagonalFullAngle => half_angle_tangent / aspect.hypot(1.0),
    };
    let full_angle_deg = |tangent: f64| (2.0 * tangent.atan()).to_degrees() as f32;
    FieldOfView {
        horizontal_deg: full_angle_deg(vertical_tangent * aspect),
        vertical_deg: full_angle_deg(vertical_tangent),
        diagonal_deg: full_angle_deg(vertical_tangent * aspect.hypot(1.0)),
    }
}

pub(super) fn projection(config: &ShipCameras) -> PerspectiveProjection {
    PerspectiveProjection {
        // Bevy takes the vertical full angle, regardless of the data's declared axis.
        fov: field_of_view(config).vertical_deg.to_radians(),
        near: (config.near_clip_m * 0.001) as f32,
        far: (config.far_clip_m * 0.001) as f32,
        aspect_ratio: config.resolution_px[0] as f32 / config.resolution_px[1] as f32,
        ..default()
    }
}

pub(super) fn body_axes(mount: &CameraMount) -> (Vec3, Vec3) {
    let yaw = mount
        .aim_yaw_deg
        .clamp(mount.yaw_limits_deg[0], mount.yaw_limits_deg[1])
        .to_radians();
    let pitch = mount
        .aim_pitch_deg
        .clamp(mount.pitch_limits_deg[0], mount.pitch_limits_deg[1])
        .to_radians();
    let (sy, cy) = yaw.sin_cos();
    let (sp, cp) = pitch.sin_cos();
    // Rows 1 and 3 of the documented passive body-to-camera DCM, expressed in body axes.
    // At zero aim the optical axis is +X, not the ship's +Z launch axis.
    let forward = Vec3::new(cp * cy, cp * sy, -sp);
    let up = Vec3::new(sp * cy, sp * sy, cp);
    (forward, up)
}

#[cfg(test)]
mod tests {
    use super::*;
    use bevy::camera::CameraProjection;

    fn close(actual: f32, expected: f32) {
        assert!((actual - expected).abs() < 0.0001, "{actual} != {expected}");
    }

    #[test]
    fn square_full_vertical_angle_has_a_wider_diagonal() {
        let mut config = crate::scenario::settings().ship_cameras.clone();
        config.field_of_view_deg = 90.0;
        config.field_of_view_convention = FieldOfViewConvention::VerticalFullAngle;
        let angles = field_of_view(&config);
        close(angles.horizontal_deg, 90.0);
        close(angles.vertical_deg, 90.0);
        close(angles.diagonal_deg, 109.47122);
    }

    #[test]
    fn declared_horizontal_and_diagonal_angles_convert_to_bevy_vertical() {
        let mut config = crate::scenario::settings().ship_cameras.clone();
        config.field_of_view_deg = 90.0;
        config.field_of_view_convention = FieldOfViewConvention::DiagonalFullAngle;
        close(field_of_view(&config).vertical_deg, 70.52878);
        config.field_of_view_convention = FieldOfViewConvention::HorizontalFullAngle;
        config.resolution_px = [1024, 512];
        close(field_of_view(&config).vertical_deg, 53.130104);
    }

    #[test]
    fn configured_capture_uses_the_published_sixty_degree_square_example() {
        let angles = field_of_view(&crate::scenario::settings().ship_cameras);
        close(angles.horizontal_deg, 60.0);
        close(angles.vertical_deg, 60.0);
        close(angles.diagonal_deg, 78.46304);
    }

    #[test]
    fn bevy_projection_places_the_configured_full_angle_on_image_edges() {
        let config = &crate::scenario::settings().ship_cameras;
        let projection = projection(config);
        let clip_from_view = projection.get_clip_from_view();
        let extent = (projection.fov * 0.5).tan();
        for sign in [-1.0, 1.0] {
            let edge = clip_from_view.project_point3(Vec3::new(sign * extent, 0.0, -1.0));
            close(edge.x, sign);
            let edge = clip_from_view.project_point3(Vec3::new(0.0, sign * extent, -1.0));
            close(edge.y, sign);
        }
        assert!(
            clip_from_view
                .project_point3(Vec3::new(extent * 1.001, 0.0, -1.0))
                .x
                > 1.0
        );
        close(projection.near, (config.near_clip_m * 0.001) as f32);
        close(projection.far, (config.far_clip_m * 0.001) as f32);
    }

    #[test]
    fn both_zero_angle_feeds_look_along_body_x_with_body_z_up() {
        for mount in &crate::scenario::settings().ship_cameras.mounts {
            let (forward, up) = body_axes(mount);
            assert_eq!(forward, Vec3::X);
            assert_eq!(up, Vec3::Z);
            let adapter = Transform::IDENTITY.looking_to(forward, up);
            assert!((adapter.rotation * Vec3::NEG_Z - forward).length() < 0.0001);
            assert!((adapter.rotation * Vec3::Y - up).length() < 0.0001);
        }
    }

    #[test]
    fn positive_yaw_turns_left_and_pitch_uses_the_documented_asymmetric_limits() {
        let mounts = &crate::scenario::settings().ship_cameras.mounts;
        let mut top = mounts[0];
        top.aim_yaw_deg = 1000.0;
        top.aim_pitch_deg = -1000.0;
        let (forward, up) = body_axes(&top);
        let yaw = 89_f32.to_radians();
        let pitch = (-50_f32).to_radians();
        close(forward.y, pitch.cos() * yaw.sin());
        close(forward.z, -pitch.sin());
        close(forward.dot(up), 0.0);
        close(forward.length(), 1.0);
        close(up.length(), 1.0);
        top.aim_yaw_deg = 0.0;
        top.aim_pitch_deg = 50.0;
        assert_eq!(body_axes(&top).0, Vec3::X);
        let mut bottom = mounts[1];
        bottom.aim_pitch_deg = 1000.0;
        close(body_axes(&bottom).0.z, -50_f32.to_radians().sin());
        bottom.aim_pitch_deg = -50.0;
        assert_eq!(body_axes(&bottom).0, Vec3::X);
    }
}
