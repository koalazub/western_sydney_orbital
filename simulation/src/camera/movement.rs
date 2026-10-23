use super::model::CameraRig;
use crate::{
    controls::{Movement, MovementTarget},
    domain::CameraMode,
};
use bevy::prelude::*;

const KM: f32 = 0.001;

#[derive(Default)]
pub(super) struct DebugFlight {
    // Yaw/pitch alone would discard first-person body roll during handoff.
    rotation_offset: Quat,
}

impl DebugFlight {
    pub(super) fn preserve_pose(&mut self, rig: &mut CameraRig, rotation: Quat) {
        let forward = rotation * Vec3::NEG_Z;
        rig.yaw = (-forward.x).atan2(-forward.z);
        rig.pitch = forward.y.clamp(-1.0, 1.0).asin().clamp(-1.52, 1.52);
        self.rotation_offset = (look_rotation(rig.yaw, rig.pitch).inverse() * rotation).normalize();
    }

    pub(super) fn rotation(&self, rig: &CameraRig) -> Quat {
        (look_rotation(rig.yaw, rig.pitch) * self.rotation_offset).normalize()
    }

    pub(super) fn translate(
        &mut self,
        rig: &mut CameraRig,
        transform: &mut Transform,
        movement: &Movement,
        active: bool,
        blocked: bool,
        dt: f32,
    ) {
        if !active || blocked {
            return;
        }
        let axes = match movement.target {
            MovementTarget::DebugCamera => movement.axes,
            MovementTarget::Ship | MovementTarget::Suppressed => return,
        };
        if axes == [0.0; 3] {
            return;
        }
        if rig.mode != CameraMode::FreeFlight {
            // In sensor display modes this is still the parked debug camera,
            // not a mounted sensor pose. Only this independent view can move.
            self.preserve_pose(rig, transform.rotation);
            rig.mode = CameraMode::FreeFlight;
        }
        transform.rotation = self.rotation(rig);
        let forward = transform.rotation * Vec3::NEG_Z;
        let right = transform.rotation * Vec3::X;
        let direction =
            forward * axes[0] as f32 - right * axes[1] as f32 + Vec3::Y * axes[2] as f32;
        let boost = if movement.boost { 5.0 } else { 1.0 };
        transform.translation += direction.normalize_or_zero() * rig.speed_mps * KM * dt * boost;
    }
}

fn look_rotation(yaw: f32, pitch: f32) -> Quat {
    Transform::IDENTITY
        .looking_to(
            Vec3::new(
                -yaw.sin() * pitch.cos(),
                pitch.sin(),
                -yaw.cos() * pitch.cos(),
            ),
            Vec3::Y,
        )
        .rotation
}

#[cfg(all(test, not(target_arch = "wasm32")))]
mod tests {
    use super::*;

    fn movement(keys: &[KeyCode]) -> Movement {
        let mut input = ButtonInput::default();
        for key in keys {
            input.press(*key);
        }
        crate::controls::movement(&input)
    }

    fn close(actual: Vec3, expected: Vec3) {
        assert!(
            (actual - expected).length() < 1e-6,
            "{actual:?} != {expected:?}"
        );
    }

    fn same_rotation(actual: Quat, expected: Quat) {
        close(actual * Vec3::X, expected * Vec3::X);
        close(actual * Vec3::Y, expected * Vec3::Y);
        close(actual * Vec3::NEG_Z, expected * Vec3::NEG_Z);
    }

    #[test]
    fn bare_movement_keys_never_translate_or_select_the_debug_camera() {
        for mode in [
            CameraMode::Orbit,
            CameraMode::FirstPerson,
            CameraMode::ThirdPerson,
            CameraMode::FreeFlight,
            CameraMode::Overview,
            CameraMode::ShipTop,
            CameraMode::ShipBottom,
        ] {
            for key in [
                KeyCode::KeyW,
                KeyCode::KeyA,
                KeyCode::KeyS,
                KeyCode::KeyD,
                KeyCode::KeyQ,
                KeyCode::KeyE,
            ] {
                for shift in [false, true] {
                    let mut keys = vec![key];
                    if shift {
                        keys.push(KeyCode::ShiftLeft);
                    }
                    let movement = movement(&keys);
                    assert!(matches!(movement.target, MovementTarget::Ship));
                    let mut rig = CameraRig { mode, ..default() };
                    let original =
                        Transform::from_xyz(1.0, 2.0, 3.0).with_rotation(look_rotation(0.8, -0.3));
                    let mut transform = original;
                    DebugFlight::default().translate(
                        &mut rig,
                        &mut transform,
                        &movement,
                        true,
                        false,
                        0.1,
                    );
                    assert_eq!(rig.mode, mode);
                    assert_eq!(transform, original);
                }
            }
        }
    }

    #[test]
    fn alt_keys_move_only_the_debug_camera_in_its_view_basis() {
        let view_rotation = look_rotation(0.8, -0.3);
        for alt in [KeyCode::AltLeft, KeyCode::AltRight] {
            for (key, direction) in [
                (KeyCode::KeyW, view_rotation * Vec3::NEG_Z),
                (KeyCode::KeyS, view_rotation * Vec3::Z),
                (KeyCode::KeyA, view_rotation * Vec3::NEG_X),
                (KeyCode::KeyD, view_rotation * Vec3::X),
                (KeyCode::KeyQ, Vec3::NEG_Y),
                (KeyCode::KeyE, Vec3::Y),
            ] {
                let movement = movement(&[alt, key]);
                assert!(matches!(movement.target, MovementTarget::DebugCamera));
                let mut rig = CameraRig::default();
                let original = Transform::from_xyz(1.0, 2.0, 3.0).with_rotation(view_rotation);
                let mut transform = original;
                let mut flight = DebugFlight::default();
                flight.translate(&mut rig, &mut transform, &movement, true, false, 0.05);
                assert_eq!(rig.mode, CameraMode::FreeFlight);
                close(
                    transform.translation - original.translation,
                    direction * 0.005,
                );
                same_rotation(transform.rotation, original.rotation);
                same_rotation(flight.rotation(&rig), original.rotation);
            }
        }
    }

    #[test]
    fn opposing_alt_keys_do_not_move_or_leave_the_current_view() {
        for keys in [
            vec![KeyCode::AltLeft, KeyCode::KeyW, KeyCode::KeyS],
            vec![KeyCode::AltLeft, KeyCode::KeyA, KeyCode::KeyD],
            vec![KeyCode::AltLeft, KeyCode::KeyQ, KeyCode::KeyE],
            vec![
                KeyCode::AltLeft,
                KeyCode::KeyW,
                KeyCode::KeyS,
                KeyCode::KeyA,
                KeyCode::KeyD,
                KeyCode::KeyQ,
                KeyCode::KeyE,
            ],
        ] {
            let movement = movement(&keys);
            assert!(movement.held);
            assert_eq!(movement.axes, [0.0; 3]);
            let mut rig = CameraRig::default();
            let original = Transform::from_xyz(1.0, 2.0, 3.0);
            let mut transform = original;
            DebugFlight::default().translate(&mut rig, &mut transform, &movement, true, false, 0.1);
            assert_eq!(rig.mode, CameraMode::Orbit);
            assert_eq!(transform, original);
        }
    }

    #[test]
    fn control_and_meta_chords_never_move_the_camera() {
        for modifier in [
            KeyCode::ControlLeft,
            KeyCode::ControlRight,
            KeyCode::SuperLeft,
            KeyCode::SuperRight,
        ] {
            for alt in [false, true] {
                let mut keys = vec![modifier, KeyCode::KeyW];
                if alt {
                    keys.push(KeyCode::AltLeft);
                }
                let movement = movement(&keys);
                assert!(matches!(movement.target, MovementTarget::Suppressed));
                let mut rig = CameraRig::default();
                let original = Transform::from_xyz(1.0, 2.0, 3.0);
                let mut transform = original;
                DebugFlight::default().translate(
                    &mut rig,
                    &mut transform,
                    &movement,
                    true,
                    false,
                    0.1,
                );
                assert_eq!(rig.mode, CameraMode::Orbit);
                assert_eq!(transform, original);
            }
        }
    }

    #[test]
    fn inactive_or_modal_input_blocks_the_dedicated_movement_chord() {
        let movement = movement(&[KeyCode::AltLeft, KeyCode::KeyW]);
        for (active, blocked) in [(false, false), (false, true), (true, true)] {
            let mut rig = CameraRig {
                mode: CameraMode::ShipTop,
                ..default()
            };
            let original = Transform::from_xyz(1.0, 2.0, 3.0);
            let mut transform = original;
            DebugFlight::default().translate(
                &mut rig,
                &mut transform,
                &movement,
                active,
                blocked,
                0.1,
            );
            assert_eq!(rig.mode, CameraMode::ShipTop);
            assert_eq!(transform, original);
        }
    }

    #[test]
    fn first_person_handoff_preserves_the_actual_body_rotated_pose() {
        let mut rig = CameraRig {
            mode: CameraMode::FirstPerson,
            yaw: 0.35,
            pitch: -0.22,
            ..default()
        };
        let body_rotation =
            Quat::from_rotation_z(1.1) * Quat::from_rotation_y(-0.6) * Quat::from_rotation_x(0.3);
        let original = Transform::from_xyz(1.0, 2.0, 3.0).with_rotation(
            body_rotation
                * Quat::from_rotation_y(std::f32::consts::PI + rig.yaw)
                * Quat::from_rotation_x(-rig.pitch),
        );
        let mut transform = original;
        let mut flight = DebugFlight::default();
        flight.translate(
            &mut rig,
            &mut transform,
            &movement(&[KeyCode::AltLeft, KeyCode::KeyW]),
            true,
            false,
            0.02,
        );
        assert_eq!(rig.mode, CameraMode::FreeFlight);
        close(
            transform.translation,
            original.translation + original.rotation * Vec3::NEG_Z * 0.002,
        );
        same_rotation(transform.rotation, original.rotation);
        same_rotation(flight.rotation(&rig), original.rotation);
    }

    #[test]
    fn handoff_also_preserves_vertical_and_rolled_debug_views() {
        let mut rig = CameraRig::default();
        let mut flight = DebugFlight::default();
        for pitch in [std::f32::consts::FRAC_PI_2, -std::f32::consts::FRAC_PI_2] {
            let original = Quat::from_rotation_x(pitch) * Quat::from_rotation_z(0.7);
            flight.preserve_pose(&mut rig, original);
            assert!(rig.yaw.is_finite() && rig.pitch.is_finite());
            same_rotation(flight.rotation(&rig), original);
        }
    }

    #[test]
    fn leaving_sensor_display_uses_the_parked_independent_debug_pose() {
        for mode in [CameraMode::ShipTop, CameraMode::ShipBottom] {
            let mut rig = CameraRig { mode, ..default() };
            let original = Transform::from_xyz(1.0, 2.0, 3.0)
                .with_rotation(look_rotation(-0.4, 0.2) * Quat::from_rotation_z(0.6));
            let mut transform = original;
            let mut flight = DebugFlight::default();
            flight.translate(
                &mut rig,
                &mut transform,
                &movement(&[KeyCode::AltLeft, KeyCode::KeyE]),
                true,
                false,
                0.05,
            );
            assert_eq!(rig.mode, CameraMode::FreeFlight);
            close(
                transform.translation,
                original.translation + Vec3::Y * 0.005,
            );
            same_rotation(transform.rotation, original.rotation);
            assert_eq!(rig.focus, crate::domain::EntityId::Lander);
            assert_eq!(rig.pan_offset, Vec3::ZERO);
            assert_eq!(rig.zoom, 1.0);
            assert_eq!(rig.distance_km, 0.12);
        }
    }

    #[test]
    fn shift_boosts_only_debug_translation_and_diagonals_stay_normalized() {
        let rotation = look_rotation(0.8, -0.3);
        let direction = (rotation * Vec3::NEG_Z - rotation * Vec3::X + Vec3::Y).normalize();
        for shift in [false, true] {
            let mut keys = vec![
                KeyCode::AltLeft,
                KeyCode::KeyW,
                KeyCode::KeyA,
                KeyCode::KeyE,
            ];
            if shift {
                keys.push(KeyCode::ShiftRight);
            }
            let movement = movement(&keys);
            let mut rig = CameraRig::default();
            let mut transform = Transform::from_rotation(rotation);
            DebugFlight::default().translate(&mut rig, &mut transform, &movement, true, false, 0.1);
            let distance = if shift { 0.05 } else { 0.01 };
            close(transform.translation, direction * distance);
            assert!((transform.translation.length() - distance).abs() < 1e-6);
            assert_eq!(rig.speed_mps, 100.0);
        }
    }
}
