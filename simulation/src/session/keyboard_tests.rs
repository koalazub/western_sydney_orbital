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
const DEFAULT_BINDINGS: &str = r#"{"version":1,"bindings":{"front":"KeyW","back":"KeyS","left":"KeyA","right":"KeyD","up":"KeyE","down":"KeyQ","lift":"Space"}}"#;

fn fresh_app() -> App {
    SIMULATION.with(|sim| *sim.borrow_mut() = Simulation::new_default());
    PUBLISHED.with(|published| *published.borrow_mut() = None);
    ACTIONS.with(|actions| actions.borrow_mut().clear());
    RUNTIME.with(|runtime| *runtime.borrow_mut() = PlaybackState::default());
    VIEWPORT.with(|viewport| *viewport.borrow_mut() = ViewportState::default());
    KEYBOARD_PILOT.with(|pilot| *pilot.borrow_mut() = KeyboardPilot::default());
    SPACE_HELD.with(|held| held.set(false));
    TRIAL_REVISION.with(|revision| revision.set(0));
    crate::navigation::clear_for_trial();
    crate::hud::reset_instrument_editing_for_tests();
    crate::launchpad::reset_for_tests();
    crate::controls::reset_for_tests();
    crate::controls::set_movement_keys(0);
    crate::camera::set_input_active(true, false);

    let mut app = App::new();
    app.configure_sets(
        Update,
        (crate::EngineSet::Runtime, crate::EngineSet::Reports).chain(),
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

fn tumbling_app() -> App {
    let mut app = running_app();
    press(&mut app, &[KeyCode::KeyA]);
    for _ in 0..240 {
        frame(&mut app, STEP);
        let state = snapshot();
        let pad = state
            .assets
            .iter()
            .find(|entity| entity.id == crate::domain::EntityId::LandingPad)
            .unwrap();
        let pad_up = bevy::math::DQuat::from_array(pad.rotation) * bevy::math::DVec3::Z;
        let lander_up = bevy::math::DQuat::from_array(ship(&state).rotation) * bevy::math::DVec3::Z;
        if pad_up.dot(lander_up) < 0.0
            && bevy::math::DVec3::from_array(ship(&state).angular_velocity_rad_s).length() > 0.05
        {
            assert!(keyboard_owns_command());
            return app;
        }
    }
    panic!("actual runtime keyboard thrust must tumble the lander");
}

#[test]
fn native_upright_action_and_json_api_pause_before_advance_and_require_key_release() {
    for through_json_api in [false, true] {
        let mut app = tumbling_app();
        app.world_mut().resource_mut::<PlaybackState>().accumulator = 0.04;
        let before = snapshot();
        let mut expected = Simulation::new_default();
        expected
            .execute(PhysicsCommand::Restore {
                state: Box::new(before.as_ref().clone()),
            })
            .unwrap();
        let expected = expected.execute(PhysicsCommand::DebugUpright).unwrap();

        if through_json_api {
            let result: serde_json::Value =
                serde_json::from_str(&crate::command(r#"{"op":"debug_upright"}"#)).unwrap();
            assert_eq!(result["ok"], true);
            assert_eq!(result["state"], serde_json::to_value(&expected).unwrap());
            let immediate: serde_json::Value = serde_json::from_str(&runtime_report()).unwrap();
            assert_eq!(immediate["running"], false);
            assert_eq!(immediate["manual_interventions"], 1);
            assert_eq!(immediate["keyboard_thrust"], false);
            assert_eq!(immediate["keyboard_requires_release"], true);
        } else {
            request_action(EngineAction::DebugUpright);
        }
        frame(&mut app, STEP);
        let reset = snapshot();
        assert_eq!(
            serde_json::to_value(reset.as_ref()).unwrap(),
            serde_json::to_value(&expected).unwrap()
        );
        assert_eq!(
            reset.time_s, before.time_s,
            "the reset frame must not advance physics"
        );
        assert!(!app.world().resource::<PlaybackState>().running);
        assert_eq!(app.world().resource::<PlaybackState>().accumulator, 0.0);
        assert!(!keyboard_owns_command());
        assert!(keyboard_requires_release());
        let report: serde_json::Value = serde_json::from_str(&runtime_report()).unwrap();
        assert_eq!(report["running"], false);
        assert_eq!(report["manual_interventions"], reset.manual_interventions);
        assert_eq!(
            observation()["manual_interventions"],
            reset.manual_interventions
        );

        frame(&mut app, STEP);
        assert_eq!(
            serde_json::to_value(snapshot().as_ref()).unwrap(),
            serde_json::to_value(reset.as_ref()).unwrap()
        );
        request_action(EngineAction::Play(PlaybackRate::RealTime));
        frame(&mut app, Duration::ZERO);
        assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0; 3]);
        assert_eq!(ship(&snapshot()).angular_velocity_rad_s, [0.0; 3]);
        assert!(!keyboard_owns_command());
        frame(&mut app, STEP);
        assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0; 3]);
        assert_eq!(snapshot().mission.fuel_used_kg, reset.mission.fuel_used_kg);
        assert_eq!(snapshot().manual_interventions, 1);
        release(&mut app, &[KeyCode::KeyA]);
        frame(&mut app, Duration::ZERO);
        assert!(!keyboard_requires_release());
        press(&mut app, &[KeyCode::KeyA]);
        frame(&mut app, Duration::ZERO);
        assert!(keyboard_owns_command());
        assert_eq!(
            ship(&snapshot()).commanded_accel_mps2,
            [0.0, -scenario::settings().lander.max_accel_mps2, 0.0]
        );
    }
}

#[test]
fn upright_reset_blocks_a_fresh_space_launch_in_its_frame() {
    let mut app = fresh_app();
    press(&mut app, &[KeyCode::Space]);
    request_action(EngineAction::DebugUpright);
    frame(&mut app, STEP);
    assert!(!app.world().resource::<PlaybackState>().running);
    assert_eq!(snapshot().manual_interventions, 1);
    assert_eq!(snapshot().time_s, 0.0);
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0; 3]);
    assert!(!keyboard_owns_command());
    assert!(keyboard_requires_release());
    frame(&mut app, STEP);
    assert!(!app.world().resource::<PlaybackState>().running);
    assert_eq!(snapshot().time_s, 0.0);
    release(&mut app, &[KeyCode::Space]);
    frame(&mut app, Duration::ZERO);
    press(&mut app, &[KeyCode::Space]);
    frame(&mut app, STEP);
    assert!(app.world().resource::<PlaybackState>().running);
    assert!(keyboard_owns_command());
    assert!(snapshot().time_s > 0.0);
    assert_eq!(snapshot().manual_interventions, 1);
}

#[test]
fn fresh_space_cannot_rearm_movement_keys_still_held_after_upright_reset() {
    let mut app = running_app();
    press(&mut app, &[KeyCode::KeyA]);
    frame(&mut app, STEP);
    request_action(EngineAction::DebugUpright);
    frame(&mut app, Duration::ZERO);
    let reset = snapshot();
    press(&mut app, &[KeyCode::Space]);
    frame(&mut app, STEP);
    assert!(!app.world().resource::<PlaybackState>().running);
    assert!(!keyboard_owns_command());
    assert!(keyboard_requires_release());
    assert_eq!(
        serde_json::to_value(snapshot().as_ref()).unwrap(),
        serde_json::to_value(reset.as_ref()).unwrap()
    );
    release(&mut app, &[KeyCode::KeyA]);
    frame(&mut app, STEP);
    assert!(
        keyboard_requires_release(),
        "Space is still physically held"
    );
    assert_eq!(snapshot().time_s, reset.time_s);
    release(&mut app, &[KeyCode::Space]);
    frame(&mut app, Duration::ZERO);
    assert!(!keyboard_requires_release());
    press(&mut app, &[KeyCode::Space]);
    frame(&mut app, STEP);
    assert!(app.world().resource::<PlaybackState>().running);
    assert!(keyboard_owns_command());
    assert_eq!(snapshot().manual_interventions, 1);
}

#[test]
fn view_only_denies_upright_actions_and_api_without_changing_the_world_or_queuing_pause() {
    let mut app = fresh_app();
    app.world_mut().resource_mut::<PlaybackState>().can_control = false;
    frame(&mut app, Duration::ZERO);
    let before = serde_json::to_value(snapshot().as_ref()).unwrap();
    let result: serde_json::Value =
        serde_json::from_str(&crate::command(r#"{"op":"debug_upright"}"#)).unwrap();
    assert_eq!(result["ok"], false);
    assert!(result["error"].as_str().unwrap().contains("view-only"));
    assert!(ACTIONS.with(|actions| actions.borrow().is_empty()));
    assert_eq!(serde_json::to_value(snapshot().as_ref()).unwrap(), before);
    request_action(EngineAction::DebugUpright);
    frame(&mut app, STEP);
    assert_eq!(serde_json::to_value(snapshot().as_ref()).unwrap(), before);
    assert_eq!(snapshot().manual_interventions, 0);
}

#[test]
fn upright_api_rejects_before_mutation_if_its_pause_cannot_be_queued() {
    let mut app = running_app();
    press(&mut app, &[KeyCode::KeyA]);
    frame(&mut app, STEP);
    assert!(keyboard_owns_command());
    let before = serde_json::to_value(snapshot().as_ref()).unwrap();
    for _ in 0..MAX_ACTIONS {
        request_action(EngineAction::CycleRate);
    }
    let result: serde_json::Value =
        serde_json::from_str(&crate::command(r#"{"op":"debug_upright"}"#)).unwrap();
    assert_eq!(result["ok"], false);
    assert!(result["error"].as_str().unwrap().contains("queued actions"));
    assert_eq!(serde_json::to_value(snapshot().as_ref()).unwrap(), before);
    assert!(keyboard_owns_command());
    let report: serde_json::Value = serde_json::from_str(&runtime_report()).unwrap();
    assert_eq!(report["running"], true);
    assert_eq!(report["manual_interventions"], 0);
}

#[test]
fn runtime_report_exposes_external_or_manual_control_and_actual_keyboard_state() {
    let mut app = running_app();
    set_viewport(true, false, "Flight controls ready");
    press(&mut app, &[KeyCode::KeyW]);
    frame(&mut app, STEP);
    let config = scenario::settings();
    let report: serde_json::Value = serde_json::from_str(&runtime_report()).unwrap();
    assert_eq!(
        report,
        serde_json::json!({
            "running": true,
            "trial_revision": 0,
            "controller": "external_python_or_manual",
            "reference_accel_limit_mps2": config.lander.max_accel_mps2,
            "keyboard_thrust": true,
            "keyboard_requires_release": false,
            "manual_interventions": 0,
            "rate": 1.0,
            "can_control": true,
            "fullscreen": true,
            "maximized": false,
            "message": "Flight controls ready",
        })
    );
    assert!(report.get("autopilot").is_none());
    assert!(report.get("practice_boost_active").is_none());
    assert!(report.get("space_boost_enabled").is_none());
    release(&mut app, &[KeyCode::KeyW]);
    frame(&mut app, STEP);
    let released: serde_json::Value = serde_json::from_str(&runtime_report()).unwrap();
    assert_eq!(released["keyboard_thrust"], false);
    assert_eq!(released["message"], report["message"]);
}

#[test]
fn runtime_playback_only_advances_physics_with_the_installed_command() {
    for external_command in [false, true] {
        let mut app = running_app();
        let mut expected = Simulation::new_default();
        let command = if external_command {
            [0.0, 0.0, scenario::settings().lander.max_accel_mps2 * 0.25]
        } else {
            [0.0; 3]
        };
        if external_command {
            dispatch(Dispatch::Physics(PhysicsCommand::SetCommand {
                accel: command,
                angular_accel: [0.0; 3],
            }))
            .unwrap();
            expected.set_command(command, [0.0; 3]).unwrap();
        }
        for _ in 0..40 {
            frame(&mut app, STEP);
            expected.advance(STEP.as_secs_f64());
            assert_eq!(ship(&snapshot()).commanded_accel_mps2, command);
            assert_eq!(ship(&snapshot()).commanded_angular_accel_rad_s2, [0.0; 3]);
            assert!(!keyboard_owns_command());
        }
        assert_eq!(
            serde_json::to_value(snapshot().as_ref()).unwrap(),
            serde_json::to_value(expected.snapshot()).unwrap(),
            "playback must not synthesize a controller command"
        );
    }
}

#[test]
fn running_ship_keys_take_over_before_the_first_physics_step_without_shift_boost() {
    let max_accel = scenario::settings().lander.max_accel_mps2;
    for (key, shift, command) in [
        (KeyCode::KeyW, false, [-max_accel, 0.0, 0.0]),
        (KeyCode::KeyS, false, [max_accel, 0.0, 0.0]),
        (KeyCode::KeyA, false, [0.0, -max_accel, 0.0]),
        (KeyCode::KeyD, false, [0.0, max_accel, 0.0]),
        (KeyCode::KeyQ, false, [0.0, 0.0, -max_accel]),
        (KeyCode::KeyE, false, [0.0, 0.0, max_accel]),
        (KeyCode::KeyE, true, [0.0, 0.0, max_accel]),
        (KeyCode::Space, false, [0.0, 0.0, max_accel]),
        (KeyCode::Space, true, [0.0, 0.0, max_accel]),
    ] {
        let mut app = running_app();
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
fn native_side_keys_fire_the_existing_opposite_force_indexed_mounts() {
    use bevy::math::DVec3;

    let reference = scenario::settings().lander.max_accel_mps2;
    for (key, mount, point, axis, sign) in [
        (KeyCode::KeyW, 1, [2.5, 0.0, -2.55], 0, -1.0),
        (KeyCode::KeyS, 0, [-2.5, 0.0, -2.55], 0, 1.0),
        (KeyCode::KeyA, 3, [0.0, 2.5, -2.55], 1, -1.0),
        (KeyCode::KeyD, 2, [0.0, -2.5, -2.55], 1, 1.0),
    ] {
        let mut app = running_app();
        let before = snapshot();
        press(&mut app, &[key]);
        frame(&mut app, Duration::ZERO);
        let thrusting = snapshot();
        let lander = ship(&thrusting);
        let jets = crate::propulsion::applied_jets(lander);
        assert_eq!(jets.len(), 1);
        assert_eq!(
            scenario::settings().lander.manual_nozzles_body_m[mount],
            point
        );
        assert_eq!(jets[0].point_body_m, point);
        let mut command = [0.0; 3];
        command[axis] = sign * reference;
        assert_eq!(lander.commanded_accel_mps2, command);
        assert_eq!(
            jets[0].force_body_n,
            command.map(|accel| accel * lander.mass_kg)
        );
        let moment = DVec3::from_array(point).cross(DVec3::from_array(jets[0].force_body_n));
        assert_eq!(lander.torque_nm, moment.to_array());
        assert!(
            moment.length() > 0.0,
            "offset side jets have a real moment arm"
        );
        assert_eq!(lander.position, ship(&before).position);
        assert_eq!(lander.velocity, ship(&before).velocity);
        frame(&mut app, STEP);
        assert!(snapshot().mission.fuel_used_kg > before.mission.fuel_used_kg);
        assert_ne!(ship(&snapshot()).angular_velocity_rad_s, [0.0; 3]);
    }
}

#[test]
fn sustained_native_key_a_tips_beyond_ninety_degrees_with_real_stock_pad_contact() {
    use crate::{
        domain::{EntityId, EntityKind},
        model::{ActuationRequest, ActuationState, ThrustApplication},
    };
    use bevy::math::{DQuat, DVec3};

    let config = scenario::settings();
    assert_eq!(config.lander.max_accel_mps2, 0.5);
    assert_eq!(config.lander.max_angular_accel_rad_s2, 0.05);
    assert_eq!(STEP.as_secs_f64(), config.integration.fixed_step_s);
    let mut app = running_app();
    let initial = snapshot();
    assert_eq!(initial.experiment_settings.unwrap().debris.count, 500);
    assert_eq!(
        initial
            .assets
            .iter()
            .filter(|actor| actor.kind == EntityKind::Debris)
            .count(),
        500
    );
    let lander = ship(&initial);
    let pad = initial
        .assets
        .iter()
        .find(|actor| actor.id == EntityId::LandingPad)
        .unwrap();
    let pad_up = DQuat::from_array(pad.rotation) * DVec3::Z;
    let request = ActuationRequest {
        application: ThrustApplication::ManualNozzles,
        accel_body_mps2: [0.0, -config.lander.max_accel_mps2, 0.0],
        angular_accel_body_rad_s2: [0.0; 3],
    };
    let force_body = DVec3::from_array(request.accel_body_mps2) * lander.mass_kg;
    let mount = config.lander.manual_nozzles_body_m[3];
    assert_eq!(mount, [0.0, 2.5, -2.55]);
    let torque_body = DVec3::from_array(mount).cross(force_body);
    assert!(
        torque_body.x < 0.0,
        "KeyA's -Y force has a negative body-X moment"
    );
    let baseline = applied_physics().unwrap();

    // Press the physical key once. Native routing and KeyboardPilot own every
    // subsequent command; no direct force, pose, mission or contact writes.
    press(&mut app, &[KeyCode::KeyA]);
    frame(&mut app, Duration::ZERO);
    let commanded = snapshot();
    let jets = crate::propulsion::applied_jets(ship(&commanded));
    assert_eq!(jets.len(), 1);
    assert_eq!(jets[0].point_body_m, mount);
    assert_eq!(jets[0].force_body_n, force_body.to_array());
    assert_eq!(ship(&commanded).torque_nm, torque_body.to_array());
    let mut previous = applied_physics().unwrap();
    assert!(keyboard_owns_command());
    assert_eq!(previous.requested, request);
    assert_eq!(previous.actuator_gain, 1.0);
    assert_eq!(
        previous.actuation_state,
        ActuationState::AwaitingApplication
    );
    assert_eq!(previous.last_actuator_application, None);
    assert_eq!(previous.position_world_m, baseline.position_world_m);
    assert_eq!(
        previous.rotation_body_to_world_xyzw,
        baseline.rotation_body_to_world_xyzw
    );
    assert_eq!(previous.velocity_world_mps, baseline.velocity_world_mps);
    assert_eq!(
        previous.angular_velocity_world_rad_s,
        baseline.angular_velocity_world_rad_s
    );

    let mut minimum_up_alignment = 1.0_f64;
    let mut peak_roll_rate = 0.0_f64;
    let mut pad_contact_frames = 0;
    // Keep the original 12 s / >90 degree acceptance, now for actual mapped A,
    // rather than the old +Y physics-only test, which currently matches KeyD.
    for _ in 0..240 {
        assert!(
            app.world()
                .resource::<ButtonInput<KeyCode>>()
                .pressed(KeyCode::KeyA)
        );
        assert!(keyboard_owns_command());
        frame(&mut app, STEP);
        let state = snapshot();
        let actual = applied_physics().unwrap();
        let applied = actual
            .last_actuator_application
            .expect("Rapier must apply the held-key thrust");
        assert_eq!(applied.requested, request);
        assert_eq!(applied.actuator_gain, 1.0);
        assert_eq!(applied.application_time_s, previous.source_time_s);
        assert_eq!(
            applied.rotation_body_to_world_xyzw,
            previous.rotation_body_to_world_xyzw
        );
        assert_eq!(actual.source_time_s, state.time_s);
        assert_eq!(actual.mass_kg, baseline.mass_kg);
        assert_eq!(actual.inertia_body_kg_m2, baseline.inertia_body_kg_m2);
        assert_eq!(actual.rotation_body_to_world_xyzw, ship(&state).rotation);
        assert_eq!(
            actual.angular_velocity_world_rad_s,
            ship(&state).angular_velocity_rad_s
        );
        let application_rotation = DQuat::from_array(applied.rotation_body_to_world_xyzw);
        let gravity = crate::astrophysics::acceleration(
            DVec3::from_array(previous.position_world_m),
            DVec3::from_array(previous.velocity_world_mps),
            config.gravity,
        );
        for (name, measured, expected) in [
            ("body force", applied.force_body_n, force_body),
            ("body torque", applied.torque_body_nm, torque_body),
            (
                "world force",
                applied.force_world_n,
                application_rotation * force_body,
            ),
            (
                "world torque",
                applied.torque_world_nm,
                application_rotation * torque_body,
            ),
            (
                "Rapier force accumulator",
                actual.total_external_force_world_n,
                application_rotation * force_body + gravity * actual.mass_kg,
            ),
            (
                "Rapier torque accumulator",
                actual.total_external_torque_world_nm,
                application_rotation * torque_body,
            ),
        ] {
            assert!(
                (DVec3::from_array(measured) - expected).length() < 1e-6,
                "{name} at {} s: measured {measured:?}, expected {expected:?}",
                state.time_s
            );
        }

        let rotation = DQuat::from_array(actual.rotation_body_to_world_xyzw);
        minimum_up_alignment = minimum_up_alignment.min(pad_up.dot(rotation * DVec3::Z));
        peak_roll_rate = peak_roll_rate.max(
            (rotation.inverse() * DVec3::from_array(actual.angular_velocity_world_rad_s))
                .x
                .abs(),
        );
        if state
            .active_contacts
            .contains(&[EntityId::Lander, EntityId::LandingPad])
        {
            pad_contact_frames += 1;
        }
        previous = actual;
        if (pad_contact_frames > 0 && minimum_up_alignment < 0.0)
            || state.mission.status.is_terminal()
        {
            break;
        }
    }

    let after = snapshot();
    assert!(
        pad_contact_frames > 0 && minimum_up_alignment < 0.0 && peak_roll_rate > 0.05,
        "actual KeyA is -Y, not the legacy test's +Y: require stock pad contact, >90 degree tip and >0.05 rad/s roll; by {} s minimum up dot {minimum_up_alignment}, peak roll {peak_roll_rate} rad/s, pad-contact frames {pad_contact_frames}, mission {:?}, damage {} J, contacts {:?}",
        after.time_s,
        after.mission.status,
        after.mission.damage,
        after.active_contacts
    );
    assert!(after.mission.fuel_used_kg > initial.mission.fuel_used_kg);
    assert_eq!(after.manual_interventions, 0);
    assert_eq!(after.tuning_revision, 0);
    let final_pad = after
        .assets
        .iter()
        .find(|actor| actor.id == EntityId::LandingPad)
        .unwrap();
    assert_eq!(final_pad.position, pad.position);
    assert_eq!(final_pad.rotation, pad.rotation);
}

#[test]
fn rebinding_releases_owned_thrust_once_and_waits_for_old_and_new_physical_keys() {
    let mut app = running_app();
    press(&mut app, &[KeyCode::KeyW]);
    frame(&mut app, STEP);
    let before = snapshot();
    assert!(keyboard_owns_command());
    crate::controls::set_bindings(&DEFAULT_BINDINGS.replace("KeyW", "KeyB")).unwrap();
    assert!(keyboard_requires_release());
    frame(&mut app, Duration::ZERO);
    let released = snapshot();
    assert_eq!(ship(&released).commanded_accel_mps2, [0.0; 3]);
    assert_eq!(ship(&released).velocity, ship(&before).velocity);
    assert_eq!(ship(&released).position, ship(&before).position);
    assert_eq!(released.manual_interventions, before.manual_interventions);
    frame(&mut app, Duration::ZERO);
    assert_eq!(
        serde_json::to_value(snapshot().as_ref()).unwrap(),
        serde_json::to_value(released.as_ref()).unwrap(),
        "only one owned-command release is applied"
    );
    press(&mut app, &[KeyCode::KeyB]);
    release(&mut app, &[KeyCode::KeyW]);
    frame(&mut app, Duration::ZERO);
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0; 3]);
    assert!(
        keyboard_requires_release(),
        "the newly configured key was already held"
    );
    release(&mut app, &[KeyCode::KeyB]);
    frame(&mut app, Duration::ZERO);
    assert!(!keyboard_requires_release());
    press(&mut app, &[KeyCode::KeyW]);
    frame(&mut app, Duration::ZERO);
    assert!(
        !keyboard_owns_command(),
        "the removed physical binding no longer fires"
    );
    release(&mut app, &[KeyCode::KeyW]);
    press(&mut app, &[KeyCode::KeyB]);
    frame(&mut app, STEP);
    assert!(keyboard_owns_command());
    assert_eq!(
        ship(&snapshot()).commanded_accel_mps2,
        [-scenario::settings().lander.max_accel_mps2, 0.0, 0.0]
    );

    press(&mut app, &[KeyCode::AltLeft]);
    let debug = crate::controls::movement(app.world().resource::<ButtonInput<KeyCode>>());
    assert_eq!(debug.target, MovementTarget::DebugCamera);
    assert_eq!(
        debug.axes,
        [1.0, 0.0, 0.0],
        "configured Front remains intuitive camera-forward under Alt"
    );
    frame(&mut app, Duration::ZERO);
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0; 3]);
    assert_eq!(snapshot().manual_interventions, 0);
}

#[test]
fn capture_and_binding_changes_never_clear_external_thrust_even_after_authority_handoffs() {
    for lose_authority in [false, true] {
        let mut app = running_app();
        press(&mut app, &[KeyCode::KeyW]);
        frame(&mut app, STEP);
        crate::controls::begin_capture(crate::controls::Action::Front);
        if lose_authority {
            app.world_mut().resource_mut::<PlaybackState>().can_control = false;
            frame(&mut app, Duration::ZERO);
        }
        let command = [0.0, 0.0, scenario::settings().lander.max_accel_mps2 * 0.25];
        dispatch(Dispatch::Physics(PhysicsCommand::SetCommand {
            accel: command,
            angular_accel: [0.0; 3],
        }))
        .unwrap();
        let external = serde_json::to_value(snapshot().as_ref()).unwrap();
        crate::controls::capture_key(KeyCode::KeyH, false);
        assert!(crate::controls::binding_capture_active());
        frame(&mut app, Duration::ZERO);
        assert_eq!(serde_json::to_value(snapshot().as_ref()).unwrap(), external);
        crate::controls::capture_key(KeyCode::KeyB, false);
        assert!(!crate::controls::binding_capture_active());
        crate::controls::restore_defaults();
        app.world_mut().resource_mut::<PlaybackState>().can_control = true;
        frame(&mut app, Duration::ZERO);
        assert_eq!(serde_json::to_value(snapshot().as_ref()).unwrap(), external);
        release(&mut app, &[KeyCode::KeyW]);
        frame(&mut app, Duration::ZERO);
        assert_eq!(
            serde_json::to_value(snapshot().as_ref()).unwrap(),
            external,
            "late release cannot zero the notebook's force"
        );
        assert_eq!(snapshot().manual_interventions, 0);
        assert!(!keyboard_owns_command());
    }
}

#[test]
fn capture_space_cannot_launch_and_a_remapped_lift_still_requires_a_fresh_press() {
    let mut app = fresh_app();
    crate::controls::begin_capture(crate::controls::Action::Lift);
    press(&mut app, &[KeyCode::Space]);
    crate::controls::capture_key(KeyCode::Space, false);
    frame(&mut app, STEP);
    assert!(!app.world().resource::<PlaybackState>().running);
    assert_eq!(snapshot().time_s, 0.0);
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0; 3]);
    release(&mut app, &[KeyCode::Space]);
    frame(&mut app, Duration::ZERO);
    crate::controls::set_bindings(&DEFAULT_BINDINGS.replace("Space", "KeyB")).unwrap();
    frame(&mut app, Duration::ZERO);
    press(&mut app, &[KeyCode::Space]);
    frame(&mut app, STEP);
    assert!(!app.world().resource::<PlaybackState>().running);
    release(&mut app, &[KeyCode::Space]);
    frame(&mut app, Duration::ZERO);
    press(&mut app, &[KeyCode::KeyB]);
    frame(&mut app, STEP);
    assert!(app.world().resource::<PlaybackState>().running);
    assert!(keyboard_owns_command());
    assert!(snapshot().time_s > 0.0);
    assert_eq!(
        ship(&snapshot()).commanded_accel_mps2,
        [0.0, 0.0, scenario::settings().lander.max_accel_mps2]
    );
    assert_eq!(snapshot().manual_interventions, 0);
}

#[test]
fn alt_handoff_and_key_release_stop_owned_thrust_without_braking() {
    let mut app = running_app();
    let command = [0.0, scenario::settings().lander.max_accel_mps2 * 0.25, 0.0];
    dispatch(Dispatch::Physics(PhysicsCommand::SetCommand {
        accel: command,
        angular_accel: [0.0; 3],
    }))
    .unwrap();
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
    assert!(!keyboard_owns_command());
    frame(&mut app, STEP);
    assert_eq!(snapshot().time_s, STEP.as_secs_f64());
    assert!(!keyboard_owns_command());
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, command);
    frame(&mut app, Duration::ZERO);
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, command);

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
fn external_stop_wins_over_a_held_ship_key_until_it_is_released() {
    let mut app = running_app();
    press(&mut app, &[KeyCode::KeyE]);
    frame(&mut app, STEP);
    assert!(keyboard_owns_command());
    let thrusting = snapshot();
    let stopped = dispatch(Dispatch::Physics(PhysicsCommand::Stop)).unwrap();
    assert_eq!(ship(&stopped).velocity, ship(&thrusting).velocity);
    assert_eq!(ship(&stopped).position, ship(&thrusting).position);
    assert_eq!(stopped.time_s, thrusting.time_s);
    assert!(!keyboard_owns_command());
    frame(&mut app, Duration::ZERO);
    assert!(!keyboard_owns_command());
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0; 3]);
    assert_eq!(ship(&snapshot()).velocity, ship(&thrusting).velocity);
    frame(&mut app, STEP);
    assert!(!keyboard_owns_command());
    assert_eq!(
        snapshot().mission.fuel_used_kg,
        thrusting.mission.fuel_used_kg
    );

    dispatch(Dispatch::Physics(PhysicsCommand::Stop)).unwrap();
    frame(&mut app, Duration::ZERO);
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
    assert_eq!(
        ship(&snapshot()).commanded_accel_mps2,
        [0.0, 0.0, scenario::settings().lander.max_accel_mps2]
    );
}

#[test]
fn authority_return_respects_external_set_command_and_stop_handoffs() {
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
            dispatch(Dispatch::Physics(PhysicsCommand::Stop)).unwrap();
            request_action(EngineAction::Pause);
            frame(&mut app, Duration::ZERO);
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
fn fresh_space_launches_a_paused_lander_with_real_reference_upward_thrust() {
    let mut app = fresh_app();
    let before = snapshot();
    let body_up = bevy::math::DQuat::from_array(ship(&before).rotation) * bevy::math::DVec3::Z;
    press(&mut app, &[KeyCode::Space]);
    frame(&mut app, STEP);
    assert!(app.world().resource::<PlaybackState>().running);
    assert!(keyboard_owns_command());
    assert_eq!(
        ship(&snapshot()).commanded_accel_mps2,
        [0.0, 0.0, scenario::settings().lander.max_accel_mps2]
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
fn space_and_e_share_reference_limits_and_manual_nozzle_physics_for_combined_keys() {
    let reference = scenario::settings().lander.max_accel_mps2;
    for lateral in [None, Some(KeyCode::KeyW), Some(KeyCode::KeyA)] {
        let mut reference_app = running_app();
        press(&mut reference_app, &[KeyCode::KeyE]);
        if let Some(lateral) = lateral {
            press(&mut reference_app, &[lateral]);
        }
        frame(&mut reference_app, STEP);
        let expected = snapshot();

        let mut app = running_app();
        press(
            &mut app,
            &[KeyCode::Space, KeyCode::KeyE, KeyCode::ShiftLeft],
        );
        if let Some(lateral) = lateral {
            press(&mut app, &[lateral]);
        }
        frame(&mut app, STEP);
        let actual = snapshot();
        assert_eq!(
            serde_json::to_value(actual.as_ref()).unwrap(),
            serde_json::to_value(expected.as_ref()).unwrap(),
            "Space, E and Shift must not amplify a combined command"
        );
        let [forward, left, up] = ship(&actual).commanded_accel_mps2;
        assert!(forward.hypot(left).hypot(up) <= reference);
        assert!(up > 0.0 && up <= reference);
        assert_eq!(
            ship(&actual).thrust_application,
            crate::model::ThrustApplication::ManualNozzles
        );
    }
}

#[test]
fn flight_telemetry_api_reports_actual_reference_motion_after_runtime_update() {
    let mut app = fresh_app();
    frame(&mut app, Duration::ZERO);
    press(&mut app, &[KeyCode::Space]);
    frame(&mut app, STEP);
    let report: serde_json::Value = serde_json::from_str(&crate::telemetry::status()).unwrap();
    let metrics = &report["metrics"];
    assert_eq!(report["source_time_s"], snapshot().time_s);
    assert_eq!(report["units"]["speed"], "m/s");
    assert_eq!(report["actuation"]["practice_boost_active"], false);
    assert_eq!(report["actuation"]["thrust_application"], "manual_nozzles");
    assert!(
        (metrics["applied_thrust_accel_mps2"].as_f64().unwrap()
            - scenario::settings().lander.max_accel_mps2)
            .abs()
            < 1e-12
    );
    assert!(metrics["speed_mps"].as_f64().unwrap() > 0.0);
    assert!(metrics["radial_climb_mps"].as_f64().unwrap() > 0.0);
    let expected_force =
        scenario::settings().lander.mass_kg * scenario::settings().lander.max_accel_mps2;
    assert!(
        (metrics["thrust_force_n"].as_f64().unwrap() - expected_force).abs()
            < 1e-12 * expected_force
    );
    assert_eq!(report["provenance"]["hidden_debris_truth_used"], false);
    assert_eq!(report["caveats"]["stp_parity"], false);
}

#[test]
fn actual_editor_focus_blocks_lift_and_manual_takeover_without_clearing_python_thrust() {
    use bevy::input_focus::FocusCause;
    let mut app = fresh_app();
    app.init_resource::<InputFocus>();
    let editor = app
        .world_mut()
        .spawn((
            bevy::ui_widgets::TextInput,
            crate::hud::InstrumentEditor,
            crate::hud::EditorField::Mass,
        ))
        .id();
    app.world_mut()
        .resource_mut::<InputFocus>()
        .set(editor, FocusCause::Navigated);
    press(&mut app, &[KeyCode::Space, KeyCode::KeyE, KeyCode::KeyW]);
    frame(&mut app, STEP);
    assert!(!is_running());
    assert_eq!(snapshot().time_s, 0.0);
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0; 3]);
    dispatch(Dispatch::Physics(PhysicsCommand::SetCommand {
        accel: [0.12, 0.0, 0.0],
        angular_accel: [0.0; 3],
    }))
    .unwrap();
    request_action(EngineAction::Play(PlaybackRate::RealTime));
    frame(&mut app, STEP);
    assert!(is_running());
    assert!(!keyboard_owns_command());
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.12, 0.0, 0.0]);
}

#[test]
fn focused_launch_setup_button_cannot_also_launch_or_take_over_python_thrust() {
    use bevy::input_focus::FocusCause;
    let mut app = fresh_app();
    app.init_resource::<InputFocus>();
    let button = app
        .world_mut()
        .spawn((bevy::ui_widgets::Button, crate::launchpad::SurfaceControl))
        .id();
    app.world_mut()
        .resource_mut::<InputFocus>()
        .set(button, FocusCause::Navigated);
    press(&mut app, &[KeyCode::Space, KeyCode::KeyE]);
    frame(&mut app, STEP);
    assert!(!is_running());
    assert_eq!(snapshot().time_s, 0.0);
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0; 3]);
    dispatch(Dispatch::Physics(PhysicsCommand::SetCommand {
        accel: [0.12, 0.0, 0.0],
        angular_accel: [0.0; 3],
    }))
    .unwrap();
    request_action(EngineAction::Play(PlaybackRate::RealTime));
    frame(&mut app, STEP);
    assert!(is_running());
    assert!(!keyboard_owns_command());
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.12, 0.0, 0.0]);
}
