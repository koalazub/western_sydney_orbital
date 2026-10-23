use super::{Movement, MovementTarget};
use bevy::prelude::{ButtonInput, KeyCode};
use std::cell::Cell;

const W: u16 = 1 << 0;
const A: u16 = 1 << 1;
const S: u16 = 1 << 2;
const D: u16 = 1 << 3;
const Q: u16 = 1 << 4;
const E: u16 = 1 << 5;
const ALT: u16 = 1 << 6;
const SHIFT: u16 = 1 << 7;
const BLOCKED_CTRL_META: u16 = 1 << 8;
const SPACE: u16 = 1 << 9;
const TRANSLATION_KEYS: u16 = W | A | S | D | Q | E | SPACE;
const KNOWN_KEYS: u16 = TRANSLATION_KEYS | ALT | SHIFT | BLOCKED_CTRL_META;

thread_local! {
    static MOVEMENT_KEYS: Cell<u16> = const { Cell::new(0) };
}

pub(super) fn set_movement_keys(mask: u16) {
    MOVEMENT_KEYS.with(|keys| keys.set(mask));
}

#[cfg(target_arch = "wasm32")]
pub(super) fn key_mask(_keys: &ButtonInput<KeyCode>) -> u16 {
    MOVEMENT_KEYS.with(Cell::get)
}

#[cfg(not(target_arch = "wasm32"))]
pub(super) fn key_mask(keys: &ButtonInput<KeyCode>) -> u16 {
    let mut mask = 0;
    for (key, bit) in [
        (KeyCode::KeyW, W),
        (KeyCode::KeyA, A),
        (KeyCode::KeyS, S),
        (KeyCode::KeyD, D),
        (KeyCode::KeyQ, Q),
        (KeyCode::KeyE, E),
        (KeyCode::Space, SPACE),
        (KeyCode::AltLeft, ALT),
        (KeyCode::AltRight, ALT),
        (KeyCode::ShiftLeft, SHIFT),
        (KeyCode::ShiftRight, SHIFT),
        (KeyCode::ControlLeft, BLOCKED_CTRL_META),
        (KeyCode::ControlRight, BLOCKED_CTRL_META),
        (KeyCode::SuperLeft, BLOCKED_CTRL_META),
        (KeyCode::SuperRight, BLOCKED_CTRL_META),
    ] {
        if keys.pressed(key) {
            mask |= bit;
        }
    }
    mask
}

pub(super) fn decode(mask: u16) -> Movement {
    let held = mask & TRANSLATION_KEYS != 0;
    if mask & !KNOWN_KEYS != 0 || mask & BLOCKED_CTRL_META != 0 {
        return Movement {
            target: MovementTarget::Suppressed,
            axes: [0.0; 3],
            held,
            boost: false,
        };
    }
    let target = if mask & ALT != 0 {
        MovementTarget::DebugCamera
    } else {
        MovementTarget::Ship
    };
    Movement {
        target,
        axes: [axis(mask, W, S), axis(mask, A, D), axis(mask, E | SPACE, Q)],
        held,
        boost: target == MovementTarget::DebugCamera && mask & SHIFT != 0,
    }
}

fn axis(mask: u16, positive: u16, negative: u16) -> f64 {
    f64::from(u8::from(mask & positive != 0)) - f64::from(u8::from(mask & negative != 0))
}

#[cfg(test)]
mod tests {
    use super::{A, ALT, BLOCKED_CTRL_META, D, E, Q, S, SHIFT, SPACE, TRANSLATION_KEYS, W};
    use crate::controls::{MovementTarget, decode};

    #[test]
    fn plain_and_alt_option_keys_choose_mutually_exclusive_targets() {
        for (mask, axes) in [
            (W, [1.0, 0.0, 0.0]),
            (S, [-1.0, 0.0, 0.0]),
            (A, [0.0, 1.0, 0.0]),
            (D, [0.0, -1.0, 0.0]),
            (E, [0.0, 0.0, 1.0]),
            (SPACE, [0.0, 0.0, 1.0]),
            (Q, [0.0, 0.0, -1.0]),
            (W | A | E, [1.0, 1.0, 1.0]),
            (W | A | SPACE, [1.0, 1.0, 1.0]),
        ] {
            let ship = decode(mask);
            let camera = decode(mask | ALT);
            assert_eq!(ship.target, MovementTarget::Ship);
            assert_eq!(camera.target, MovementTarget::DebugCamera);
            assert_eq!(ship.axes, axes);
            assert_eq!(camera.axes, axes);
            assert!(ship.held && camera.held);
            assert!(!ship.boost && !camera.boost);
        }
    }

    #[test]
    fn opposing_keys_cancel_axes_without_claiming_a_physical_release() {
        for mask in [
            W | S,
            A | D,
            Q | E,
            Q | SPACE,
            Q | E | SPACE,
            TRANSLATION_KEYS,
        ] {
            for modifier in [0, ALT] {
                let movement = decode(mask | modifier);
                assert_eq!(movement.axes, [0.0; 3]);
                assert!(movement.held);
            }
        }
        assert_eq!(decode(W | S | E).axes, [0.0, 0.0, 1.0]);
        assert_eq!(decode(Q | E | D).axes, [0.0, -1.0, 0.0]);
    }

    #[test]
    fn space_and_e_share_one_up_axis_and_q_cancels_either_or_both() {
        for modifier in [0, ALT] {
            for (physical, up) in [
                (SPACE, 1.0),
                (E, 1.0),
                (E | SPACE, 1.0),
                (Q, -1.0),
                (Q | SPACE, 0.0),
                (Q | E, 0.0),
                (Q | E | SPACE, 0.0),
            ] {
                let movement = decode(physical | modifier);
                assert_eq!(movement.axes, [0.0, 0.0, up]);
                assert!(movement.held);
            }
        }
        assert!(!decode(0).held);
        assert!(!decode(ALT | SHIFT).held);
    }

    #[test]
    fn space_remains_held_for_the_paused_pilot_release_guard() {
        use crate::controls::{KeyboardPilot, PilotContext, PilotEffect};

        let ready = PilotContext {
            active: true,
            running: true,
            can_control: true,
            mission_running: true,
            modal: false,
        };
        let paused = PilotContext {
            running: false,
            ..ready
        };
        let mut pilot = KeyboardPilot::default();
        assert_eq!(pilot.update(decode(SPACE), paused, 10.0), PilotEffect::None);
        for physical in [SPACE, E | SPACE, Q | SPACE, Q | E | SPACE] {
            assert_eq!(
                pilot.update(decode(physical), ready, 10.0),
                PilotEffect::None
            );
            assert!(pilot.requires_release());
        }
        assert_eq!(pilot.update(decode(0), ready, 10.0), PilotEffect::None);
        assert!(!pilot.requires_release());
        for physical in [SPACE, E | SPACE, SPACE] {
            assert_eq!(
                pilot.update(decode(physical), ready, 10.0),
                PilotEffect::SetCommand([0.0, 0.0, 10.0])
            );
        }
        assert_eq!(
            pilot.update(decode(0), ready, 10.0),
            PilotEffect::ReleaseCommand
        );
        assert_eq!(pilot.update(decode(0), ready, 10.0), PilotEffect::None);
    }

    #[test]
    fn shift_boost_belongs_only_to_the_debug_camera() {
        for mask in (0..=TRANSLATION_KEYS).filter(|mask| mask & !TRANSLATION_KEYS == 0) {
            let ship = decode(mask | SHIFT);
            let camera = decode(mask | ALT | SHIFT);
            assert_eq!(ship.target, MovementTarget::Ship);
            assert_eq!(camera.target, MovementTarget::DebugCamera);
            assert!(!ship.boost);
            assert!(camera.boost);
            assert_eq!(ship.axes, camera.axes);
            assert_eq!(ship.held, mask != 0);
            assert_eq!(camera.held, mask != 0);
        }
    }

    #[test]
    fn ctrl_meta_and_every_unknown_bit_fail_closed() {
        for blocked in std::iter::once(BLOCKED_CTRL_META).chain((10..16).map(|bit| 1_u16 << bit)) {
            for physical in [
                0,
                W,
                Q | E,
                SPACE,
                E | SPACE,
                Q | E | SPACE,
                TRANSLATION_KEYS,
            ] {
                let movement = decode(physical | ALT | SHIFT | blocked);
                assert_eq!(movement.target, MovementTarget::Suppressed);
                assert_eq!(movement.axes, [0.0; 3]);
                assert_eq!(movement.held, physical != 0);
                assert!(!movement.boost);
            }
        }
    }

    #[test]
    fn setter_stores_the_latest_raw_physical_sample() {
        use crate::controls::set_movement_keys;

        set_movement_keys(W | ALT);
        set_movement_keys(E | (1 << 15));
        assert_eq!(
            super::MOVEMENT_KEYS.with(std::cell::Cell::get),
            E | (1 << 15)
        );
        set_movement_keys(0);
    }

    #[cfg(not(target_arch = "wasm32"))]
    #[test]
    fn native_adapter_reads_held_keys_and_keeps_input_edges_intact() {
        use crate::controls::{movement, set_movement_keys};
        use bevy::prelude::{ButtonInput, KeyCode};

        let mut keys = ButtonInput::default();
        for key in [KeyCode::KeyW, KeyCode::KeyA, KeyCode::KeyE] {
            keys.press(key);
        }
        keys.press(KeyCode::KeyS);
        keys.release(KeyCode::KeyS);
        set_movement_keys(D | ALT);
        for _ in 0..2 {
            let sample = movement(&keys);
            assert_eq!(sample.target, MovementTarget::Ship);
            assert_eq!(sample.axes, [1.0; 3]);
            assert!(sample.held);
        }
        for key in [KeyCode::KeyW, KeyCode::KeyA, KeyCode::KeyE] {
            assert!(keys.pressed(key));
            assert!(keys.just_pressed(key));
        }
        assert!(!keys.pressed(KeyCode::KeyS));
        assert!(keys.just_released(KeyCode::KeyS));
        set_movement_keys(0);
    }

    #[cfg(not(target_arch = "wasm32"))]
    #[test]
    fn native_adapter_maps_each_physical_translation_key() {
        use crate::controls::movement;
        use bevy::prelude::{ButtonInput, KeyCode};

        for (key, axes) in [
            (KeyCode::KeyW, [1.0, 0.0, 0.0]),
            (KeyCode::KeyS, [-1.0, 0.0, 0.0]),
            (KeyCode::KeyA, [0.0, 1.0, 0.0]),
            (KeyCode::KeyD, [0.0, -1.0, 0.0]),
            (KeyCode::KeyE, [0.0, 0.0, 1.0]),
            (KeyCode::Space, [0.0, 0.0, 1.0]),
            (KeyCode::KeyQ, [0.0, 0.0, -1.0]),
        ] {
            let mut keys = ButtonInput::default();
            keys.press(key);
            let sample = movement(&keys);
            assert_eq!(sample.target, MovementTarget::Ship);
            assert_eq!(sample.axes, axes);
            assert!(sample.held);
            assert!(!sample.boost);
        }
    }

    #[cfg(not(target_arch = "wasm32"))]
    #[test]
    fn native_space_adapter_keeps_bit_nine_alt_handoff_and_release_edges() {
        use crate::controls::movement;
        use bevy::prelude::{ButtonInput, KeyCode};

        for alt in [KeyCode::AltLeft, KeyCode::AltRight] {
            let mut keys = ButtonInput::default();
            keys.press(KeyCode::Space);
            assert_eq!(super::key_mask(&keys), 1 << 9);
            assert_eq!(movement(&keys).target, MovementTarget::Ship);
            keys.press(alt);
            assert_eq!(super::key_mask(&keys), SPACE | ALT);
            let sample = movement(&keys);
            assert_eq!(sample.target, MovementTarget::DebugCamera);
            assert_eq!(sample.axes, [0.0, 0.0, 1.0]);
            assert!(sample.held);
            assert!(keys.just_pressed(KeyCode::Space));
            keys.release(KeyCode::Space);
            assert_eq!(super::key_mask(&keys), ALT);
            let sample = movement(&keys);
            assert_eq!(sample.axes, [0.0; 3]);
            assert!(!sample.held);
            assert!(keys.just_released(KeyCode::Space));
        }
    }

    #[cfg(not(target_arch = "wasm32"))]
    #[test]
    fn native_adapter_accepts_both_sides_of_each_modifier() {
        use crate::controls::movement;
        use bevy::prelude::{ButtonInput, KeyCode};

        for modifier in [KeyCode::AltLeft, KeyCode::AltRight] {
            let mut keys = ButtonInput::default();
            keys.press(KeyCode::KeyQ);
            keys.press(modifier);
            for shift in [KeyCode::ShiftLeft, KeyCode::ShiftRight] {
                keys.press(shift);
                let sample = movement(&keys);
                assert_eq!(sample.target, MovementTarget::DebugCamera);
                assert_eq!(sample.axes, [0.0, 0.0, -1.0]);
                assert!(sample.boost);
                keys.release(shift);
            }
        }
        for modifier in [
            KeyCode::ControlLeft,
            KeyCode::ControlRight,
            KeyCode::SuperLeft,
            KeyCode::SuperRight,
        ] {
            for physical in [KeyCode::KeyW, KeyCode::Space] {
                let mut keys = ButtonInput::default();
                keys.press(physical);
                keys.press(KeyCode::AltRight);
                keys.press(modifier);
                let sample = movement(&keys);
                assert_eq!(sample.target, MovementTarget::Suppressed);
                assert_eq!(sample.axes, [0.0; 3]);
                assert!(sample.held);
                assert!(!sample.boost);
            }
        }
    }

    #[cfg(target_arch = "wasm32")]
    #[test]
    fn wasm_adapter_uses_the_latest_browser_sample_instead_of_bevy_keys() {
        use crate::controls::{movement, set_movement_keys};
        use bevy::prelude::{ButtonInput, KeyCode};

        let mut keys = ButtonInput::default();
        keys.press(KeyCode::KeyW);
        set_movement_keys(D | SPACE | ALT | SHIFT);
        let sample = movement(&keys);
        assert_eq!(sample.target, MovementTarget::DebugCamera);
        assert_eq!(sample.axes, [0.0, -1.0, 1.0]);
        assert!(sample.boost && sample.held);
        set_movement_keys(0);
        assert!(!movement(&keys).held);
        assert!(keys.pressed(KeyCode::KeyW));
        assert!(keys.just_pressed(KeyCode::KeyW));
    }
}
