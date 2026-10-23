use super::model::CameraRig;
use crate::domain::CameraMode;
use bevy::prelude::*;

pub(super) const MIN_DISTANCE_KM: f32 = 0.025;
pub(super) const MAX_DISTANCE_KM: f32 = 20_000.0;

/// Positive deltas increase view distance, negative deltas magnify the view.
/// Ship sensors never share the debug viewer's zoom or projection.
pub(super) fn apply_pinch(
    rig: &mut CameraRig,
    transform: &mut Transform,
    projection: &mut Projection,
    target: Vec3,
    base_distance_km: f32,
    log_distance_delta: f32,
) {
    if !log_distance_delta.is_finite() || log_distance_delta == 0.0 {
        return;
    }
    let factor = log_distance_delta.clamp(-1.5, 1.5).exp();
    match rig.mode {
        CameraMode::ShipTop | CameraMode::ShipBottom => {}
        CameraMode::Orbit | CameraMode::ThirdPerson | CameraMode::Overview => {
            rig.distance_km = (rig.distance_km * factor).clamp(MIN_DISTANCE_KM, MAX_DISTANCE_KM);
            rig.zoom = (rig.distance_km / base_distance_km).clamp(0.0001, 10_000.0);
        }
        CameraMode::FreeFlight => {
            let distance = transform
                .translation
                .distance(target)
                .clamp(MIN_DISTANCE_KM, MAX_DISTANCE_KM);
            let next = (distance * factor).clamp(MIN_DISTANCE_KM, MAX_DISTANCE_KM);
            transform.translation += transform.rotation * Vec3::NEG_Z * (distance - next);
        }
        CameraMode::FirstPerson => {
            if let Projection::Perspective(view) = projection {
                view.fov = (view.fov * factor).clamp(10.0_f32.to_radians(), 120.0_f32.to_radians());
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn log_distance_deltas_zoom_out_and_back_reversibly() {
        let mut rig = CameraRig::default();
        let mut transform = Transform::default();
        let mut projection = Projection::default();
        apply_pinch(
            &mut rig,
            &mut transform,
            &mut projection,
            Vec3::ZERO,
            0.12,
            2.0_f32.ln(),
        );
        assert!((rig.distance_km - 0.24).abs() < 1e-6);
        apply_pinch(
            &mut rig,
            &mut transform,
            &mut projection,
            Vec3::ZERO,
            0.12,
            -2.0_f32.ln(),
        );
        assert!((rig.distance_km - 0.12).abs() < 1e-6);
    }

    #[test]
    fn free_flight_pinches_dolly_the_view_instead_of_changing_movement_speed() {
        let mut rig = CameraRig {
            mode: CameraMode::FreeFlight,
            ..default()
        };
        let mut transform = Transform::from_xyz(0.0, 0.0, 0.12);
        apply_pinch(
            &mut rig,
            &mut transform,
            &mut Projection::default(),
            Vec3::ZERO,
            0.12,
            2.0_f32.ln(),
        );
        assert!((transform.translation.z - 0.24).abs() < 1e-6);
        assert_eq!(rig.speed_mps, 100.0);
    }

    #[test]
    fn first_person_debug_zoom_changes_only_its_lens() {
        let mut rig = CameraRig {
            mode: CameraMode::FirstPerson,
            ..default()
        };
        let mut transform = Transform::from_xyz(1.0, 2.0, 3.0);
        let mut projection = Projection::Perspective(PerspectiveProjection {
            fov: 60.0_f32.to_radians(),
            ..default()
        });
        apply_pinch(
            &mut rig,
            &mut transform,
            &mut projection,
            Vec3::ZERO,
            0.12,
            -2.0_f32.ln(),
        );
        let Projection::Perspective(view) = projection else {
            panic!("perspective projection changed");
        };
        assert!((view.fov - 30.0_f32.to_radians()).abs() < 1e-6);
        assert_eq!(transform.translation, Vec3::new(1.0, 2.0, 3.0));
    }

    #[test]
    fn ship_views_ignore_pinch_without_changing_pose_or_projection() {
        for mode in [CameraMode::ShipTop, CameraMode::ShipBottom] {
            let mut rig = CameraRig { mode, ..default() };
            let mut transform = Transform::from_xyz(1.0, 2.0, 3.0);
            let mut projection = Projection::Perspective(PerspectiveProjection {
                fov: 0.8,
                ..default()
            });
            apply_pinch(
                &mut rig,
                &mut transform,
                &mut projection,
                Vec3::ZERO,
                0.12,
                1.0,
            );
            assert_eq!(transform.translation, Vec3::new(1.0, 2.0, 3.0));
            assert_eq!(rig.distance_km, 0.12);
            let Projection::Perspective(view) = projection else {
                panic!("perspective projection changed");
            };
            assert_eq!(view.fov, 0.8);
        }
    }

    #[test]
    fn nonfinite_pinch_deltas_are_ignored() {
        let mut rig = CameraRig::default();
        for delta in [f32::NAN, f32::INFINITY, f32::NEG_INFINITY] {
            apply_pinch(
                &mut rig,
                &mut Transform::default(),
                &mut Projection::default(),
                Vec3::ZERO,
                0.12,
                delta,
            );
        }
        assert_eq!(rig.distance_km, 0.12);
    }
}
