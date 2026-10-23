//! Exercise the installed runtime system with native held-key input and Rapier.
#![cfg(not(target_arch = "wasm32"))]

use super::*;
use crate::{
    domain::{EntityStatus, PlaybackRate},
    hud::HudState,
    model::EntityState,
};
use std::time::Duration;

const STEP: Duration = Duration::from_millis(50);

fn fresh_app() -> App {
    SIMULATION.with(|sim| *sim.borrow_mut() = Simulation::new_default());
    PUBLISHED.with(|published| *published.borrow_mut() = None);
    ACTIONS.with(|actions| actions.borrow_mut().clear());
    RUNTIME.with(|runtime| *runtime.borrow_mut() = PlaybackState::default());
    VIEWPORT.with(|viewport| *viewport.borrow_mut() = ViewportState::default());
    KEYBOARD_PILOT.with(|pilot| *pilot.borrow_mut() = KeyboardPilot::default());
    SPACE_HELD.with(|held| held.set(false));
    crate::controls::set_movement_keys(0);
    crate::camera::set_input_active(true, false);

    let mut app = App::new();
    app.configure_sets(
        Update,
        (crate::EngineSet::Runtime, crate::EngineSet::Planning).chain(),
    )
    .insert_resource(Time::<()>::default())
    .insert_resource(ButtonInput::<KeyCode>::default())
    .insert_resource(PlaybackState::default())
    .insert_resource(HudState::default())
    .add_systems(Update, advance_world.in_set(crate::EngineSet::Runtime));
    crate::telemetry::install(&mut app);
    app
}

fn running_app() -> App {
    let mut app = fresh_app();
    request_action(EngineAction::Play(PlaybackRate::RealTime));
    frame(&mut app, Duration::ZERO);
    assert!(app.world().resource::<PlaybackState>().running);
    app
}

fn frame(app: &mut App, delta: Duration) {
    app.world_mut().resource_mut::<Time>().advance_by(delta);
    app.update();
}

fn press(app: &mut App, codes: &[KeyCode]) {
    let mut keys = app.world_mut().resource_mut::<ButtonInput<KeyCode>>();
    for &code in codes {
        keys.press(code);
    }
}

fn release(app: &mut App, codes: &[KeyCode]) {
    let mut keys = app.world_mut().resource_mut::<ButtonInput<KeyCode>>();
    for &code in codes {
        keys.release(code);
    }
}

fn ship(state: &Snapshot) -> &EntityState {
    state
        .assets
        .iter()
        .find(|actor| actor.id == state.mission.shuttle_id)
        .expect("the real scenario contains its lander")
}

fn keyboard_owns_command() -> bool {
    KEYBOARD_PILOT.with(|pilot| pilot.borrow().owns_command())
}

#[test]
fn running_ship_keys_take_over_before_the_first_physics_step_without_shift_boost() {
    let max_accel = scenario::settings().lander.max_accel_mps2;
    for (key, shift, command) in [
        (KeyCode::KeyW, false, [max_accel, 0.0, 0.0]),
        (KeyCode::KeyE, false, [0.0, 0.0, max_accel]),
        (KeyCode::KeyE, true, [0.0, 0.0, max_accel]),
    ] {
        let mut app = running_app();
        assert!(app.world().resource::<PlaybackState>().autopilot);
        let before = snapshot();
        let mut expected = Simulation::new_default();
        expected.set_manual_thrust(command).unwrap();
        expected.advance(STEP.as_secs_f64());
        let expected = expected.snapshot();

        press(&mut app, &[key]);
        if shift {
            press(&mut app, &[KeyCode::ShiftLeft]);
        }
        frame(&mut app, STEP);

        let after = snapshot();
        let lander = ship(&after);
        assert!(!app.world().resource::<PlaybackState>().autopilot);
        assert!(keyboard_owns_command());
        assert_eq!(after.time_s, STEP.as_secs_f64());
        assert_eq!(lander.commanded_accel_mps2, command);
        assert_eq!(lander.commanded_angular_accel_rad_s2, [0.0; 3]);
        assert_eq!(
            lander.thrust_application,
            crate::model::ThrustApplication::ManualNozzles
        );
        assert_eq!(lander.torque_nm, ship(&expected).torque_nm);
        assert_eq!(
            lander.angular_velocity_rad_s,
            ship(&expected).angular_velocity_rad_s
        );
        assert_eq!(lander.mass_kg, ship(&before).mass_kg);
        assert_eq!(lander.radius_m, ship(&before).radius_m);
        assert_eq!(lander.max_thrust_n, ship(&before).max_thrust_n);
        assert!(after.mission.fuel_used_kg > before.mission.fuel_used_kg);
        assert_eq!(after.mission.fuel_used_kg, expected.mission.fuel_used_kg);
        for (actual, expected) in lander.position.iter().zip(ship(&expected).position) {
            assert!(
                (actual - expected).abs() < 1e-9,
                "force must precede physics"
            );
        }
        for (actual, expected) in lander.velocity.iter().zip(ship(&expected).velocity) {
            assert!(
                (actual - expected).abs() < 1e-12,
                "force must precede physics"
            );
        }
    }
}

#[test]
fn alt_handoff_and_key_release_stop_owned_thrust_without_braking() {
    let mut app = running_app();
    let initial = snapshot();
    press(
        &mut app,
        &[KeyCode::AltLeft, KeyCode::ShiftLeft, KeyCode::KeyW],
    );
    frame(&mut app, Duration::ZERO);
    assert_eq!(
        ship(&snapshot()).commanded_accel_mps2,
        ship(&initial).commanded_accel_mps2
    );
    assert!(app.world().resource::<PlaybackState>().autopilot);
    assert!(!keyboard_owns_command());
    frame(&mut app, STEP);
    assert_eq!(snapshot().time_s, STEP.as_secs_f64());
    assert!(app.world().resource::<PlaybackState>().autopilot);
    assert!(!keyboard_owns_command());
    let controller_command = ship(&snapshot()).commanded_accel_mps2;
    frame(&mut app, Duration::ZERO);
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, controller_command);

    for use_alt in [false, true] {
        let mut app = running_app();
        press(&mut app, &[KeyCode::KeyE]);
        frame(&mut app, STEP);
        let thrusting = snapshot();
        assert_ne!(ship(&thrusting).velocity, [0.0; 3]);
        if use_alt {
            press(&mut app, &[KeyCode::AltLeft, KeyCode::ShiftLeft]);
        } else {
            release(&mut app, &[KeyCode::KeyE]);
        }
        frame(&mut app, Duration::ZERO);

        let released = snapshot();
        assert_eq!(ship(&released).commanded_accel_mps2, [0.0; 3]);
        assert_eq!(ship(&released).velocity, ship(&thrusting).velocity);
        assert_eq!(ship(&released).position, ship(&thrusting).position);
        assert_eq!(released.time_s, thrusting.time_s);
        assert!(!app.world().resource::<PlaybackState>().autopilot);
        assert!(!keyboard_owns_command());
        frame(&mut app, STEP);
        assert_eq!(
            snapshot().mission.fuel_used_kg,
            thrusting.mission.fuel_used_kg
        );
        assert_ne!(ship(&snapshot()).velocity, [0.0; 3]);

        if use_alt {
            // Deliberately dropping Alt can hand a still-held E back to the ship.
            release(&mut app, &[KeyCode::AltLeft, KeyCode::ShiftLeft]);
        } else {
            press(&mut app, &[KeyCode::KeyE]);
        }
        frame(&mut app, Duration::ZERO);
        assert_eq!(
            ship(&snapshot()).commanded_accel_mps2,
            [0.0, 0.0, scenario::settings().lander.max_accel_mps2]
        );
        assert!(keyboard_owns_command());
    }
}

#[test]
fn queued_pause_step_and_reset_do_not_reuse_a_held_keyboard_command() {
    let mut app = fresh_app();
    let initial = serde_json::to_value(snapshot().as_ref()).unwrap();
    press(&mut app, &[KeyCode::KeyE]);
    frame(&mut app, STEP);
    assert_eq!(serde_json::to_value(snapshot().as_ref()).unwrap(), initial);
    request_action(EngineAction::Play(PlaybackRate::RealTime));
    frame(&mut app, Duration::ZERO);
    assert!(app.world().resource::<PlaybackState>().autopilot);
    assert!(
        !keyboard_owns_command(),
        "a paused held key needs a fresh press"
    );
    assert_eq!(serde_json::to_value(snapshot().as_ref()).unwrap(), initial);

    release(&mut app, &[KeyCode::KeyE]);
    frame(&mut app, Duration::ZERO);
    press(&mut app, &[KeyCode::KeyE]);
    frame(&mut app, STEP);
    assert!(keyboard_owns_command());
    let thrusting = snapshot();
    request_action(EngineAction::Pause);
    request_action(EngineAction::Step);
    frame(&mut app, STEP);

    let stepped = snapshot();
    assert_eq!(stepped.time_s, thrusting.time_s + STEP.as_secs_f64());
    assert_eq!(
        stepped.mission.fuel_used_kg, thrusting.mission.fuel_used_kg,
        "Pause must clear owned thrust before queued Step advances"
    );
    assert_eq!(ship(&stepped).commanded_accel_mps2, [0.0; 3]);
    assert!(!app.world().resource::<PlaybackState>().running);
    assert!(!keyboard_owns_command());

    request_action(EngineAction::Play(PlaybackRate::RealTime));
    frame(&mut app, Duration::ZERO);
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0; 3]);
    release(&mut app, &[KeyCode::KeyE]);
    frame(&mut app, Duration::ZERO);
    press(&mut app, &[KeyCode::KeyE]);
    frame(&mut app, Duration::ZERO);
    assert!(keyboard_owns_command());
    request_action(EngineAction::Reset);
    frame(&mut app, STEP);

    assert_eq!(
        serde_json::to_value(snapshot().as_ref()).unwrap(),
        initial,
        "a stale release must not change the new Landed state to Coasting"
    );
    assert_eq!(ship(&snapshot()).status, EntityStatus::Landed);
    assert!(!app.world().resource::<PlaybackState>().running);
    assert!(!keyboard_owns_command());
    request_action(EngineAction::Play(PlaybackRate::RealTime));
    frame(&mut app, Duration::ZERO);
    assert_eq!(serde_json::to_value(snapshot().as_ref()).unwrap(), initial);
    assert!(!keyboard_owns_command());
}

#[test]
fn focus_and_authority_loss_require_release_before_keyboard_thrust_can_resume() {
    let mut app = running_app();
    app.world_mut().resource_mut::<PlaybackState>().can_control = false;
    let initial = serde_json::to_value(snapshot().as_ref()).unwrap();
    press(&mut app, &[KeyCode::KeyE]);
    frame(&mut app, STEP);
    assert_eq!(serde_json::to_value(snapshot().as_ref()).unwrap(), initial);
    assert!(app.world().resource::<PlaybackState>().autopilot);
    assert!(!keyboard_owns_command());
    app.world_mut().resource_mut::<PlaybackState>().can_control = true;
    frame(&mut app, Duration::ZERO);
    assert!(!keyboard_owns_command());
    assert_eq!(serde_json::to_value(snapshot().as_ref()).unwrap(), initial);

    for lose_authority in [false, true] {
        let mut app = running_app();
        press(&mut app, &[KeyCode::KeyE]);
        frame(&mut app, STEP);
        let thrusting = snapshot();
        if lose_authority {
            app.world_mut().resource_mut::<PlaybackState>().can_control = false;
        } else {
            crate::camera::set_input_active(false, false);
        }
        frame(&mut app, Duration::ZERO);
        assert_eq!(ship(&snapshot()).position, ship(&thrusting).position);
        assert_eq!(ship(&snapshot()).velocity, ship(&thrusting).velocity);
        frame(&mut app, STEP);
        let unavailable = snapshot();
        assert_eq!(
            unavailable.mission.fuel_used_kg,
            thrusting.mission.fuel_used_kg
        );
        if lose_authority {
            assert_eq!(unavailable.time_s, thrusting.time_s);
            assert_eq!(ship(&unavailable).position, ship(&thrusting).position);
            assert_eq!(ship(&unavailable).velocity, ship(&thrusting).velocity);
            app.world_mut().resource_mut::<PlaybackState>().can_control = true;
        } else {
            assert_eq!(ship(&unavailable).commanded_accel_mps2, [0.0; 3]);
            crate::camera::set_input_active(true, false);
        }
        frame(&mut app, STEP);

        let regained = snapshot();
        assert_eq!(
            ship(&regained).commanded_accel_mps2,
            [0.0; 3],
            "restoring authority or focus must not resume stale owned thrust"
        );
        assert_eq!(
            regained.mission.fuel_used_kg,
            thrusting.mission.fuel_used_kg
        );
        assert!(!keyboard_owns_command());
        assert!(!app.world().resource::<PlaybackState>().autopilot);
        release(&mut app, &[KeyCode::KeyE]);
        frame(&mut app, Duration::ZERO);
        press(&mut app, &[KeyCode::KeyE]);
        frame(&mut app, STEP);
        assert!(keyboard_owns_command());
        assert_eq!(
            ship(&snapshot()).commanded_accel_mps2,
            [0.0, 0.0, scenario::settings().lander.max_accel_mps2]
        );
    }
}

#[test]
fn external_set_command_survives_held_ship_keys_alt_and_later_key_release() {
    let config = scenario::settings().lander;
    let command = [0.0, config.max_accel_mps2 * 0.25, 0.0];
    let angular_command = [0.0, 0.0, config.max_angular_accel_rad_s2 * 0.25];
    for use_alt in [false, true] {
        let mut app = running_app();
        press(&mut app, &[KeyCode::KeyE]);
        frame(&mut app, STEP);
        assert!(keyboard_owns_command());
        let before = snapshot();
        let installed = dispatch(Dispatch::Physics(PhysicsCommand::SetCommand {
            accel: command,
            angular_accel: angular_command,
        }))
        .unwrap();
        assert_eq!(ship(&installed).velocity, ship(&before).velocity);
        assert_eq!(ship(&installed).position, ship(&before).position);
        assert!(!keyboard_owns_command());
        if use_alt {
            press(&mut app, &[KeyCode::AltRight, KeyCode::ShiftRight]);
        }
        frame(&mut app, STEP);
        assert_eq!(ship(&snapshot()).commanded_accel_mps2, command);
        assert_eq!(
            ship(&snapshot()).commanded_angular_accel_rad_s2,
            angular_command
        );
        assert!(!app.world().resource::<PlaybackState>().autopilot);
        assert!(!keyboard_owns_command());
        let external = serde_json::to_value(snapshot().as_ref()).unwrap();
        release(
            &mut app,
            &[KeyCode::KeyE, KeyCode::AltRight, KeyCode::ShiftRight],
        );
        frame(&mut app, Duration::ZERO);
        assert_eq!(
            serde_json::to_value(snapshot().as_ref()).unwrap(),
            external,
            "a late keyboard release must not zero an external command"
        );
        crate::camera::set_input_active(false, false);
        frame(&mut app, Duration::ZERO);
        assert_eq!(serde_json::to_value(snapshot().as_ref()).unwrap(), external);
        crate::camera::set_input_active(true, false);
        frame(&mut app, Duration::ZERO);
        press(&mut app, &[KeyCode::KeyE]);
        frame(&mut app, Duration::ZERO);
        assert!(keyboard_owns_command());
        assert_eq!(
            ship(&snapshot()).commanded_accel_mps2,
            [0.0, 0.0, config.max_accel_mps2]
        );
        assert_eq!(ship(&snapshot()).commanded_angular_accel_rad_s2, [0.0; 3]);
    }
}

#[test]
fn explicit_autopilot_toggles_win_over_a_held_ship_key_until_it_is_released() {
    let mut app = running_app();
    press(&mut app, &[KeyCode::KeyE]);
    frame(&mut app, STEP);
    assert!(keyboard_owns_command());
    assert!(!app.world().resource::<PlaybackState>().autopilot);
    let thrusting = snapshot();
    request_action(EngineAction::ToggleAutopilot);
    frame(&mut app, Duration::ZERO);
    assert!(app.world().resource::<PlaybackState>().autopilot);
    assert!(!keyboard_owns_command());
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0; 3]);
    assert_eq!(ship(&snapshot()).velocity, ship(&thrusting).velocity);
    frame(&mut app, STEP);
    assert!(app.world().resource::<PlaybackState>().autopilot);
    assert!(!keyboard_owns_command());

    request_action(EngineAction::ToggleAutopilot);
    frame(&mut app, Duration::ZERO);
    assert!(!app.world().resource::<PlaybackState>().autopilot);
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0; 3]);
    let fuel_used = snapshot().mission.fuel_used_kg;
    frame(&mut app, STEP);
    assert_eq!(snapshot().mission.fuel_used_kg, fuel_used);
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0; 3]);
    assert!(!keyboard_owns_command());
    release(&mut app, &[KeyCode::KeyE]);
    frame(&mut app, Duration::ZERO);
    press(&mut app, &[KeyCode::KeyE]);
    frame(&mut app, STEP);
    assert!(keyboard_owns_command());
    assert!(!app.world().resource::<PlaybackState>().autopilot);
    assert_eq!(
        ship(&snapshot()).commanded_accel_mps2,
        [0.0, 0.0, scenario::settings().lander.max_accel_mps2]
    );
}

#[test]
fn authority_return_respects_new_external_commands_and_explicit_autopilot_handoffs() {
    for external_command in [false, true] {
        let mut app = running_app();
        press(&mut app, &[KeyCode::KeyE]);
        frame(&mut app, STEP);
        app.world_mut().resource_mut::<PlaybackState>().can_control = false;
        frame(&mut app, Duration::ZERO);
        assert!(!keyboard_owns_command());
        app.world_mut().resource_mut::<PlaybackState>().can_control = true;
        if external_command {
            let command = [0.0, scenario::settings().lander.max_accel_mps2 * 0.25, 0.0];
            dispatch(Dispatch::Physics(PhysicsCommand::SetCommand {
                accel: command,
                angular_accel: [0.0; 3],
            }))
            .unwrap();
            frame(&mut app, Duration::ZERO);
            assert_eq!(ship(&snapshot()).commanded_accel_mps2, command);
            release(&mut app, &[KeyCode::KeyE]);
            frame(&mut app, Duration::ZERO);
            assert_eq!(ship(&snapshot()).commanded_accel_mps2, command);
        } else {
            request_action(EngineAction::Pause);
            request_action(EngineAction::ToggleAutopilot);
            frame(&mut app, Duration::ZERO);
            assert!(app.world().resource::<PlaybackState>().autopilot);
            assert!(!app.world().resource::<PlaybackState>().running);
            assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0; 3]);
            let fuel = snapshot().mission.fuel_used_kg;
            request_action(EngineAction::Step);
            frame(&mut app, STEP);
            assert_eq!(snapshot().mission.fuel_used_kg, fuel);
        }
        assert!(!keyboard_owns_command());
    }
}

#[test]
fn fresh_space_launches_a_paused_lander_with_real_upward_thrust() {
    let mut app = fresh_app();
    let before = snapshot();
    let body_up = bevy::math::DQuat::from_array(ship(&before).rotation) * bevy::math::DVec3::Z;
    press(&mut app, &[KeyCode::Space]);
    frame(&mut app, STEP);
    assert!(app.world().resource::<PlaybackState>().running);
    assert!(!app.world().resource::<PlaybackState>().autopilot);
    assert!(keyboard_owns_command());
    assert_eq!(
        ship(&snapshot()).commanded_accel_mps2,
        [
            0.0,
            0.0,
            scenario::settings().practice.space_lift_accel_mps2
        ]
    );
    for _ in 0..9 {
        frame(&mut app, STEP);
    }
    let after = snapshot();
    let lift = bevy::math::DVec3::from_array(ship(&after).position)
        - bevy::math::DVec3::from_array(ship(&before).position);
    assert!(
        lift.dot(body_up) > 0.02,
        "Space must lift through actual dynamics, not toggle pause"
    );
    assert!(after.mission.fuel_used_kg > before.mission.fuel_used_kg);
    release(&mut app, &[KeyCode::Space]);
    frame(&mut app, Duration::ZERO);
    assert!(app.world().resource::<PlaybackState>().running);
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0; 3]);
    assert_eq!(ship(&snapshot()).velocity, ship(&after).velocity);
}

#[test]
fn space_never_cancels_an_explicit_pause_or_launches_from_debug_or_view_only_input() {
    for guarded in 0..3 {
        let mut app = fresh_app();
        press(&mut app, &[KeyCode::Space]);
        match guarded {
            0 => request_action(EngineAction::Pause),
            1 => press(&mut app, &[KeyCode::AltLeft]),
            2 => app.world_mut().resource_mut::<PlaybackState>().can_control = false,
            _ => unreachable!(),
        }
        frame(&mut app, STEP);
        assert!(!app.world().resource::<PlaybackState>().running);
        assert_eq!(snapshot().time_s, 0.0);
        assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0; 3]);
    }
    let mut app = fresh_app();
    press(&mut app, &[KeyCode::Space]);
    frame(&mut app, STEP);
    request_action(EngineAction::TogglePlayback);
    frame(&mut app, STEP);
    assert!(!app.world().resource::<PlaybackState>().running);
    let paused = snapshot().time_s;
    frame(&mut app, STEP);
    assert!(!app.world().resource::<PlaybackState>().running);
    assert_eq!(snapshot().time_s, paused);
}

#[test]
fn practice_space_boost_is_explicit_reversible_and_does_not_boost_side_keys() {
    let mut app = fresh_app();
    assert!(app.world().resource::<PlaybackState>().practice_boost);
    press(&mut app, &[KeyCode::Space, KeyCode::KeyW]);
    frame(&mut app, STEP);
    let boosted = snapshot();
    let force = ship(&boosted).commanded_accel_mps2;
    let reference = scenario::settings().lander.max_accel_mps2;
    assert!(force[0] > 0.0 && force[0] <= reference);
    assert!(force[2] > reference);
    assert_eq!(
        ship(&boosted).thrust_application,
        crate::model::ThrustApplication::PracticeBoost
    );
    request_action(EngineAction::TogglePracticeBoost);
    frame(&mut app, Duration::ZERO);
    assert!(!app.world().resource::<PlaybackState>().practice_boost);
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0; 3]);
    assert_eq!(ship(&snapshot()).velocity, ship(&boosted).velocity);
    frame(&mut app, STEP);
    assert_eq!(
        ship(&snapshot()).commanded_accel_mps2,
        [0.0; 3],
        "profile switch requires release"
    );
    release(&mut app, &[KeyCode::Space, KeyCode::KeyW]);
    frame(&mut app, Duration::ZERO);
    press(&mut app, &[KeyCode::Space]);
    frame(&mut app, STEP);
    assert_eq!(
        ship(&snapshot()).commanded_accel_mps2,
        [0.0, 0.0, reference]
    );
    assert_eq!(
        ship(&snapshot()).thrust_application,
        crate::model::ThrustApplication::ManualNozzles
    );
}

#[test]
fn flight_telemetry_api_reports_actual_boosted_motion_after_runtime_update() {
    let mut app = fresh_app();
    frame(&mut app, Duration::ZERO);
    press(&mut app, &[KeyCode::Space]);
    frame(&mut app, STEP);
    let report: serde_json::Value = serde_json::from_str(&crate::telemetry::status()).unwrap();
    let metrics = &report["metrics"];
    assert_eq!(report["source_time_s"], snapshot().time_s);
    assert_eq!(report["units"]["speed"], "m/s");
    assert_eq!(report["actuation"]["practice_boost_active"], true);
    assert_eq!(
        metrics["applied_thrust_accel_mps2"],
        scenario::settings().practice.space_lift_accel_mps2
    );
    assert!(metrics["speed_mps"].as_f64().unwrap() > 0.0);
    assert!(metrics["radial_climb_mps"].as_f64().unwrap() > 0.0);
    assert!(
        metrics["thrust_force_n"].as_f64().unwrap()
            > scenario::settings().lander.mass_kg * scenario::settings().lander.max_accel_mps2
    );
    assert_eq!(report["provenance"]["hidden_debris_truth_used"], false);
    assert_eq!(report["caveats"]["stp_parity"], false);
}
