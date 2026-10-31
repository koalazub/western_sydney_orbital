//! Exercise dispatch transactions against the real Simulation and native runtime.
#![cfg(not(target_arch = "wasm32"))]

use super::*;
use crate::{
    domain::{EntityId, EntityKind, PlaybackRate},
    hud::HudState,
    model::{EntityState, ExperimentSettings},
};
use serde_json::Value;
use std::time::Duration;

const STEP: Duration = Duration::from_millis(50);

#[test]
fn leased_commands_reject_changed_sensor_revision_and_expired_decisions_atomically() {
    let _app = fresh_app();
    dispatch(Dispatch::Physics(PhysicsCommand::Step { seconds: 0.2 })).unwrap();
    let state = snapshot();
    let expected = CommandContext {
        trial_revision: trial_revision(),
        tuning_revision: state.tuning_revision,
        manual_interventions: state.manual_interventions,
        seed: state.seed,
        running: false,
    };
    let request = crate::model::LeasedCommand {
        accel: [0.0, 0.0, 0.2],
        angular_accel: [0.0; 3],
        lease_sim_s: 0.1,
        source_time_s: 0.0,
        controller_fence: None,
    };
    let before = physics_json();
    assert!(matches!(
        dispatch_leased_checked(request.clone(), expected, sensor_revision()),
        Err(CommandError::CommandExpired)
    ));
    assert_eq!(physics_json(), before);
    let response: Value = serde_json::from_str(&crate::api::control(&serde_json::json!({
        "op": "set_leased_command", "accel": request.accel, "angular_accel": request.angular_accel,
        "lease_sim_s": request.lease_sim_s, "source_time_s": request.source_time_s,
        "expected_sensor_revision": sensor_revision(),
        "expected_context": {"trial_revision": expected.trial_revision, "tuning_revision": expected.tuning_revision,
            "manual_interventions": expected.manual_interventions, "seed": expected.seed, "running": expected.running},
    }).to_string())).unwrap();
    assert_eq!(response["code"], "command_expired");
    assert_eq!(physics_json(), before);
    let revision = sensor_revision();
    dispatch(Dispatch::Physics(PhysicsCommand::SetSensorFault {
        target: crate::model::SensorFaultTarget::RadarBackup,
        mode: crate::model::SensorFaultMode::Unavailable,
        duration_sim_s: Some(1.0),
    }))
    .unwrap();
    let changed = physics_json();
    assert!(matches!(
        dispatch_leased_checked(
            crate::model::LeasedCommand {
                source_time_s: state.time_s,
                ..request
            },
            expected,
            revision
        ),
        Err(CommandError::StaleContext)
    ));
    assert_eq!(physics_json(), changed);
}

#[test]
fn fault_controls_require_authority_and_do_not_take_keyboard_command_ownership() {
    let mut app = running_with_key(KeyCode::KeyW);
    let keyboard = keyboard_state();
    let runtime = playback_state();
    let source = snapshot();
    dispatch(Dispatch::Physics(PhysicsCommand::SetSensorFault {
        target: crate::model::SensorFaultTarget::RadarPrimary,
        mode: crate::model::SensorFaultMode::Frozen,
        duration_sim_s: Some(0.05),
    }))
    .unwrap();
    assert_eq!(keyboard_state(), keyboard);
    assert_eq!(playback_state(), runtime);
    assert_eq!(
        ship(&snapshot()).commanded_accel_mps2,
        ship(&source).commanded_accel_mps2
    );
    frame(&mut app, STEP);
    assert!(keyboard_owns_command());
    assert_eq!(
        ship(&snapshot()).commanded_accel_mps2,
        ship(&source).commanded_accel_mps2
    );
    app.world_mut().resource_mut::<PlaybackState>().can_control = false;
    frame(&mut app, Duration::ZERO);
    for command in [
        PhysicsCommand::SetSensorFault {
            target: crate::model::SensorFaultTarget::AllAvionics,
            mode: crate::model::SensorFaultMode::Unavailable,
            duration_sim_s: Some(1.0),
        },
        PhysicsCommand::ClearSensorFaults,
    ] {
        assert_rejected_unchanged(command);
    }
}

#[test]
fn autonomous_lease_expires_in_running_native_frames_without_pausing() {
    let mut app = fresh_app();
    let source = snapshot();
    dispatch_leased_checked(
        crate::model::LeasedCommand {
            accel: [0.0, 0.0, 0.5],
            angular_accel: [0.0; 3],
            lease_sim_s: 0.1,
            source_time_s: source.time_s,
            controller_fence: None,
        },
        CommandContext {
            trial_revision: trial_revision(),
            tuning_revision: source.tuning_revision,
            manual_interventions: source.manual_interventions,
            seed: source.seed,
            running: false,
        },
        sensor_revision(),
    )
    .unwrap();
    request_action(EngineAction::Play(PlaybackRate::RealTime));
    for _ in 0..4 {
        frame(&mut app, STEP);
    }
    assert!(is_running());
    assert!(snapshot().time_s >= 0.2 - 1e-12);
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0; 3]);
    assert_eq!(observation()["command_watchdog"]["status"], "expired");
    assert_eq!(snapshot().manual_interventions, source.manual_interventions);
}

fn fresh_app() -> App {
    SIMULATION.with(|sim| *sim.borrow_mut() = Simulation::new_default());
    ACTIONS.with(|actions| actions.borrow_mut().clear());
    RUNTIME.with(|runtime| *runtime.borrow_mut() = PlaybackState::default());
    VIEWPORT.with(|viewport| *viewport.borrow_mut() = ViewportState::default());
    PUBLISHED.with(|published| *published.borrow_mut() = None);
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
    app.insert_resource(Time::<()>::default())
        .insert_resource(ButtonInput::<KeyCode>::default())
        .insert_resource(PlaybackState::default())
        .insert_resource(HudState::default())
        .add_systems(Update, advance_world);
    app
}

fn frame(app: &mut App, delta: Duration) {
    app.world_mut().resource_mut::<Time>().advance_by(delta);
    app.update();
}

fn press(app: &mut App, key: KeyCode) {
    app.world_mut()
        .resource_mut::<ButtonInput<KeyCode>>()
        .press(key);
}

fn release(app: &mut App, key: KeyCode) {
    app.world_mut()
        .resource_mut::<ButtonInput<KeyCode>>()
        .release(key);
}

fn running_with_key(key: KeyCode) -> App {
    let mut app = fresh_app();
    request_action(EngineAction::Play(PlaybackRate::RealTime));
    frame(&mut app, Duration::ZERO);
    press(&mut app, key);
    frame(&mut app, STEP);
    assert!(KEYBOARD_PILOT.with(|pilot| pilot.borrow().owns_command()));
    assert!(snapshot().mission.fuel_used_kg > 0.0);
    app
}

fn ship(state: &Snapshot) -> &EntityState {
    state
        .assets
        .iter()
        .find(|entity| entity.id == state.mission.shuttle_id)
        .expect("the real simulation contains its lander")
}

fn physics_json() -> Value {
    SIMULATION.with(|sim| serde_json::to_value(sim.borrow().snapshot()).unwrap())
}

fn playback_state() -> (Value, f64) {
    RUNTIME.with(|runtime| {
        let runtime = runtime.borrow();
        (
            serde_json::to_value(&*runtime).unwrap(),
            runtime.accumulator,
        )
    })
}

fn keyboard_state() -> (bool, bool, bool) {
    KEYBOARD_PILOT.with(|pilot| {
        let pilot = pilot.borrow();
        (
            pilot.owns_command(),
            pilot.has_installed_command(),
            pilot.requires_release(),
        )
    })
}

fn queued_actions() -> String {
    ACTIONS.with(|actions| format!("{:?}", actions.borrow()))
}

fn assert_rejected_unchanged(command: PhysicsCommand) -> String {
    let physics = physics_json();
    let observations = observation();
    let published = PUBLISHED.with(|cell| cell.borrow().clone());
    let playback = playback_state();
    let report = runtime_report();
    let actions = queued_actions();
    let keyboard = keyboard_state();
    let space_held = SPACE_HELD.with(Cell::get);

    let error = dispatch(Dispatch::Physics(command)).unwrap_err();

    assert_eq!(physics_json(), physics, "physics and persisted RNG changed");
    assert_eq!(observation(), observations);
    assert_eq!(playback_state(), playback);
    assert_eq!(runtime_report(), report);
    assert_eq!(queued_actions(), actions);
    assert_eq!(keyboard_state(), keyboard);
    assert_eq!(SPACE_HELD.with(Cell::get), space_held);
    PUBLISHED.with(|cell| match (&published, cell.borrow().as_ref()) {
        (None, None) => {}
        (Some(before), Some(after)) => assert!(Rc::ptr_eq(before, after)),
        _ => panic!("rejection changed the published snapshot cache"),
    });
    error
}

fn custom_experiment() -> ExperimentSettings {
    let mut settings = ExperimentSettings::default();
    settings.ship.mass_kg = 8_123.0;
    settings.ship.inertia_scale = 2.5;
    settings.debris.count = 37;
    settings
}

fn current_context() -> CommandContext {
    let state = snapshot();
    CommandContext {
        trial_revision: trial_revision(),
        tuning_revision: state.tuning_revision,
        manual_interventions: state.manual_interventions,
        seed: state.seed,
        running: is_running(),
    }
}

fn assert_checked_rejected_unchanged(context: CommandContext) {
    let before = physics_json();
    let runtime = runtime_report();
    let actions = queued_actions();
    let keyboard = keyboard_state();
    let published = PUBLISHED.with(|cell| cell.borrow().clone());
    assert!(matches!(
        dispatch_checked([0.1, 0.0, 0.0], [0.0; 3], context),
        Err(CommandError::StaleContext)
    ));
    assert!(matches!(
        dispatch_play_checked(PlaybackRate::RealTime, context),
        Err(CommandError::StaleContext)
    ));
    assert_eq!(physics_json(), before);
    assert_eq!(runtime_report(), runtime);
    assert_eq!(queued_actions(), actions);
    assert_eq!(keyboard_state(), keyboard);
    PUBLISHED.with(|cell| match (&published, &*cell.borrow()) {
        (None, None) => {}
        (Some(before), Some(after)) => assert!(Rc::ptr_eq(before, after)),
        _ => panic!("rejected context changed publication"),
    });
}

#[test]
fn checked_commands_reject_real_native_tuning_and_pending_playback_without_mutation() {
    let mut app = fresh_app();
    let stale = current_context();
    let mut settings = snapshot().ship_tuning();
    settings.thrust_scale = 2.0;
    dispatch(Dispatch::Physics(PhysicsCommand::TuneShip { settings })).unwrap();
    assert_checked_rejected_unchanged(stale);
    let paused = current_context();
    request_action(EngineAction::Play(PlaybackRate::RealTime));
    assert_checked_rejected_unchanged(paused);
    frame(&mut app, Duration::ZERO);
    assert_checked_rejected_unchanged(paused);
    let current = current_context();
    dispatch_checked([0.0, 0.0, 0.25], [0.0; 3], current).unwrap();
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0, 0.0, 0.25]);
    frame(&mut app, STEP);
    assert!(
        applied_physics()
            .unwrap()
            .last_actuator_application
            .unwrap()
            .force_body_n[2]
            > 0.0
    );
}

#[test]
fn checked_commands_do_not_replace_real_manual_nozzle_input_or_initialize_a_rejected_cache() {
    let _app = running_with_key(KeyCode::KeyE);
    let context = current_context();
    assert!(keyboard_owns_command());
    assert_checked_rejected_unchanged(context);
    let _app = fresh_app();
    PUBLISHED.with(|cell| assert!(cell.borrow().is_none()));
    assert_checked_rejected_unchanged(CommandContext {
        trial_revision: u32::MAX,
        tuning_revision: 0,
        manual_interventions: 0,
        seed: 100,
        running: false,
    });
}

#[test]
fn checked_play_acknowledges_a_queue_then_starts_real_physics_after_the_committed_pause() {
    let mut app = fresh_app();
    let mut settings = ExperimentSettings::default();
    settings.debris.count = 0;
    dispatch(Dispatch::Physics(PhysicsCommand::ConfigureFlight {
        settings,
        seed: Some(100),
        preset: crate::model::FlightPreset::MarsFacingL1,
    }))
    .unwrap();
    let context = current_context();
    dispatch_checked([0.0, 0.0, 0.5], [0.0; 3], context).unwrap();
    let before = physics_json();
    dispatch_play_checked(PlaybackRate::RealTime, context).unwrap();
    assert!(
        !is_running(),
        "an acknowledgement is not a fabricated applied frame"
    );
    assert_eq!(physics_json(), before);
    assert_eq!(queued_actions_for_tests(), 2);
    frame(&mut app, STEP);
    assert!(is_running());
    assert!(snapshot().time_s > 0.0);
    assert!(ship(&snapshot()).velocity[0] > 0.0);
}

#[test]
fn checked_play_rechecks_context_before_the_runtime_frame_and_cannot_revive_a_new_trial() {
    for new_trial in [false, true] {
        let mut app = fresh_app();
        let context = current_context();
        dispatch_checked([0.0, 0.0, 0.5], [0.0; 3], context).unwrap();
        dispatch_play_checked(PlaybackRate::RealTime, context).unwrap();
        if new_trial {
            dispatch(Dispatch::Physics(PhysicsCommand::Reset {
                seed: Some(context.seed),
            }))
            .unwrap();
        } else {
            let mut tuning = snapshot().ship_tuning();
            tuning.thrust_scale = 2.0;
            dispatch(Dispatch::Physics(PhysicsCommand::TuneShip {
                settings: tuning,
            }))
            .unwrap();
        }
        let before = physics_json();
        frame(&mut app, STEP);
        assert!(!is_running());
        assert_eq!(snapshot().time_s, 0.0);
        assert_eq!(physics_json(), before);
        if !new_trial {
            assert!(runtime_report().contains("flight changed"));
        }
    }
}

#[test]
fn checked_play_rejects_a_full_independent_action_queue_without_mutation() {
    let _app = fresh_app();
    let context = current_context();
    for _ in 0..MAX_ACTIONS {
        request_action(EngineAction::CycleRate);
    }
    let before = physics_json();
    let published = snapshot();
    let queue = queued_actions();
    let runtime = runtime_report();
    let keyboard = keyboard_state();
    assert!(matches!(
        dispatch_play_checked(PlaybackRate::RealTime, context),
        Err(CommandError::Rejected(_))
    ));
    assert_eq!(physics_json(), before);
    assert!(Rc::ptr_eq(&published, &snapshot()));
    assert_eq!(queued_actions(), queue);
    assert_eq!(runtime_report(), runtime);
    assert_eq!(keyboard_state(), keyboard);
}

#[test]
fn same_seed_zero_time_launch_changes_are_distinct_trials_and_clear_python_guidance() {
    let _app = fresh_app();
    let old = current_context();
    let mut settings = custom_experiment();
    settings.debris.count = 0;
    dispatch(Dispatch::Physics(PhysicsCommand::ConfigureFlight {
        settings,
        seed: Some(100),
        preset: crate::model::FlightPreset::MarsFacingL1,
    }))
    .unwrap();
    let state = snapshot();
    assert_eq!(state.seed, old.seed);
    assert_eq!(state.time_s, 0.0);
    assert_eq!(
        state.flight_preset,
        crate::model::FlightPreset::MarsFacingL1
    );
    assert!(ship(&state).position[0] > 0.0);
    assert_eq!(
        state.mission.target_region,
        crate::domain::LagrangeRegion::L1
    );
    assert_eq!(trial_revision(), old.trial_revision + 1);
    assert!(!is_running());
    assert_checked_rejected_unchanged(old);
    let navigation: Value = serde_json::from_str(&crate::navigation::status()).unwrap();
    assert_eq!(navigation["plan"]["status"], "waiting_for_notebook");
}

#[test]
fn explicit_placement_pauses_the_actual_body_at_rest_without_restarting_or_retiming() {
    let mut app = fresh_app();
    request_action(EngineAction::Play(PlaybackRate::RealTime));
    frame(&mut app, STEP);
    let before = snapshot();
    let revision = trial_revision();
    let position_m = [crate::model::phobos_enclosing_radius_m() + 1500.0, 0.0, 0.0];
    dispatch(Dispatch::Physics(PhysicsCommand::PlaceLander {
        position_m,
    }))
    .unwrap();
    let after = snapshot();
    let actual = applied_physics().unwrap();
    assert_eq!(actual.position_world_m, position_m);
    assert_eq!(actual.velocity_world_mps, [0.0; 3]);
    assert_eq!(actual.angular_velocity_world_rad_s, [0.0; 3]);
    assert_eq!(actual.total_external_force_world_n, [0.0; 3]);
    assert_eq!(after.time_s, before.time_s);
    assert_eq!(after.rng_state, before.rng_state);
    assert_eq!(after.seed, before.seed);
    assert_eq!(after.ship_tuning(), before.ship_tuning());
    assert_eq!(after.mission.fuel_used_kg, before.mission.fuel_used_kg);
    assert_eq!(after.mission.damage, before.mission.damage);
    assert_eq!(after.manual_interventions, before.manual_interventions + 1);
    assert_eq!(trial_revision(), revision + 1);
    assert!(!is_running());
    frame(&mut app, STEP);
    assert_eq!(snapshot().time_s, before.time_s);
    assert_eq!(ship(&snapshot()).position, position_m);
}

#[test]
fn invalid_placement_does_not_pause_a_running_world_or_advance_trial_revision() {
    let _app = running_with_key(KeyCode::KeyE);
    let revision = trial_revision();
    assert_rejected_unchanged(PhysicsCommand::PlaceLander {
        position_m: [0.0; 3],
    });
    assert_eq!(trial_revision(), revision);
    assert!(is_running());
}

fn assert_launchpad_context_rejected_unchanged(context: CommandContext) {
    let before = physics_json();
    let runtime = runtime_report();
    let actions = queued_actions();
    let keyboard = keyboard_state();
    let published = snapshot();
    assert!(matches!(
        dispatch_launchpad_checked([1.0, 0.3, 0.2], context),
        Err(CommandError::StaleContext)
    ));
    assert_eq!(physics_json(), before);
    assert_eq!(runtime_report(), runtime);
    assert_eq!(queued_actions(), actions);
    assert_eq!(keyboard_state(), keyboard);
    assert!(Rc::ptr_eq(&published, &snapshot()));
}

#[test]
fn placement_button_pauses_the_real_runtime_without_moving_or_restarting_the_world() {
    let mut app = fresh_app();
    dispatch(Dispatch::Physics(PhysicsCommand::SetCommand {
        accel: [0.0, 0.0, 0.25],
        angular_accel: [0.0; 3],
    }))
    .unwrap();
    request_action(EngineAction::Play(PlaybackRate::RealTime));
    frame(&mut app, STEP);
    assert!(is_running());
    let before = snapshot();
    let physics = physics_json();
    let revision = trial_revision();
    request_action(EngineAction::CycleRate);
    let paused = pause_for_launchpad_placement().unwrap();
    assert!(!paused.running && !is_running());
    assert_eq!(paused.accumulator, 0.0);
    assert_eq!(queued_actions_for_tests(), 2);
    assert!(Rc::ptr_eq(&before, &snapshot()));
    assert_eq!(physics_json(), physics);
    assert_eq!(trial_revision(), revision);
    *app.world_mut().resource_mut::<PlaybackState>() = paused;
    frame(&mut app, STEP);
    assert!(!is_running());
    assert_eq!(snapshot().time_s, before.time_s);
    assert_eq!(physics_json(), physics);
    assert_eq!(trial_revision(), revision);
    assert_eq!(
        app.world().resource::<PlaybackState>().rate,
        PlaybackRate::Fivefold
    );
}

#[test]
fn placement_button_rejects_unavailable_control_or_pending_actions_without_changes() {
    for (can_control, actions) in [
        (false, vec![]),
        (true, vec![EngineAction::Play(PlaybackRate::RealTime)]),
        (true, vec![EngineAction::Step]),
        (true, vec![EngineAction::Reset]),
        (true, vec![EngineAction::CycleRate; MAX_ACTIONS]),
    ] {
        let mut app = fresh_app();
        app.world_mut().resource_mut::<PlaybackState>().can_control = can_control;
        frame(&mut app, Duration::ZERO);
        for action in actions {
            request_action(action);
        }
        let physics = physics_json();
        let runtime = playback_state();
        let queue = queued_actions();
        let keyboard = keyboard_state();
        let revision = trial_revision();
        let published = snapshot();
        assert!(pause_for_launchpad_placement().is_err());
        assert_eq!(physics_json(), physics);
        assert_eq!(playback_state(), runtime);
        assert_eq!(queued_actions(), queue);
        assert_eq!(keyboard_state(), keyboard);
        assert_eq!(trial_revision(), revision);
        assert!(Rc::ptr_eq(&published, &snapshot()));
    }
}

#[test]
fn launchpad_move_requires_real_pause_and_does_not_consume_a_queued_flight_action() {
    let mut app = fresh_app();
    request_action(EngineAction::Play(PlaybackRate::RealTime));
    frame(&mut app, STEP);
    let context = current_context();
    let error = assert_rejected_unchanged(PhysicsCommand::MoveLaunchpad {
        surface_direction: [1.0, 0.3, 0.2],
    });
    assert!(error.contains("Pause"));
    request_action(EngineAction::Pause);
    assert_launchpad_context_rejected_unchanged(context);
    assert!(is_running(), "a pending pause has not applied yet");
}

#[test]
fn launchpad_gesture_context_rejects_real_tuning_and_same_time_setup_changes() {
    let _app = fresh_app();
    let context = current_context();
    let mut tuning = snapshot().ship_tuning();
    tuning.inertia_scale = 2.0;
    dispatch(Dispatch::Physics(PhysicsCommand::TuneShip {
        settings: tuning,
    }))
    .unwrap();
    assert_launchpad_context_rejected_unchanged(context);
    let context = current_context();
    dispatch(Dispatch::Physics(PhysicsCommand::Reset {
        seed: Some(context.seed),
    }))
    .unwrap();
    assert_launchpad_context_rejected_unchanged(context);
    let context = current_context();
    request_action(EngineAction::Play(PlaybackRate::RealTime));
    assert_launchpad_context_rejected_unchanged(context);
}

#[test]
fn launchpad_move_is_one_paused_in_place_intervention_without_debris_reset_or_target_relabel() {
    for preset in [
        crate::model::FlightPreset::MarsFacingL1,
        crate::model::FlightPreset::FarSideL2,
    ] {
        let mut app = fresh_app();
        let mut settings = ExperimentSettings::default();
        settings.debris.count = 5;
        dispatch(Dispatch::Physics(PhysicsCommand::ConfigureFlight {
            settings,
            seed: Some(100),
            preset,
        }))
        .unwrap();
        dispatch(Dispatch::Physics(PhysicsCommand::SetCommand {
            accel: [0.0, 0.0, 0.5],
            angular_accel: [0.0; 3],
        }))
        .unwrap();
        let before = snapshot();
        let context = current_context();
        let direction = [1.0, 0.3, 0.2];
        let site = crate::terrain::landing_site_at_direction(direction).unwrap();
        let after = dispatch_launchpad_checked(direction, context).unwrap();
        let body = applied_physics().unwrap();
        assert_eq!(body.position_world_m, site.lander_position_m);
        assert_eq!(body.velocity_world_mps, [0.0; 3]);
        assert_eq!(body.angular_velocity_world_rad_s, [0.0; 3]);
        assert_eq!(body.total_external_force_world_n, [0.0; 3]);
        assert_eq!(after.manual_interventions, before.manual_interventions + 1);
        assert_eq!(trial_revision(), context.trial_revision + 1);
        assert_eq!(after.seed, before.seed);
        assert_eq!(after.time_s, before.time_s);
        assert_eq!(after.rng_state, before.rng_state);
        assert_eq!(after.ship_tuning(), before.ship_tuning());
        assert_eq!(after.mission.fuel_used_kg, before.mission.fuel_used_kg);
        assert_eq!(after.mission.damage, before.mission.damage);
        assert_eq!(after.mission.status, before.mission.status);
        assert_eq!(after.mission.target_region, before.mission.target_region);
        assert!(
            keyboard_requires_release(),
            "placement must fence held manual keys at commit"
        );
        for previous in before
            .assets
            .iter()
            .filter(|asset| !matches!(asset.id, EntityId::Lander | EntityId::LandingPad))
        {
            let current = after
                .assets
                .iter()
                .find(|asset| asset.id == previous.id)
                .unwrap();
            assert_eq!(
                serde_json::to_value(current).unwrap(),
                serde_json::to_value(previous).unwrap()
            );
        }
        frame(&mut app, STEP);
        assert!(!is_running());
        assert_eq!(snapshot().time_s, before.time_s);
        assert_eq!(
            applied_physics().unwrap().position_world_m,
            site.lander_position_m
        );
        assert!(
            !keyboard_requires_release(),
            "an observed physical key-up may rearm manual input"
        );
    }
}

#[test]
fn launchpad_failure_and_full_queue_leave_the_paused_trial_unchanged() {
    let _app = fresh_app();
    assert_rejected_unchanged(PhysicsCommand::MoveLaunchpad {
        surface_direction: [0.0; 3],
    });
    let context = current_context();
    for _ in 0..MAX_ACTIONS {
        request_action(EngineAction::CycleRate);
    }
    let before = physics_json();
    let actions = queued_actions();
    let runtime = runtime_report();
    let published = snapshot();
    let rejection = dispatch_launchpad_checked([1.0, 0.3, 0.2], context);
    assert!(matches!(rejection, Err(CommandError::Rejected(_))));
    assert_eq!(physics_json(), before);
    assert_eq!(queued_actions(), actions);
    assert_eq!(runtime_report(), runtime);
    assert!(Rc::ptr_eq(&published, &snapshot()));
}

#[test]
fn live_tuning_keeps_running_keyboard_ownership_and_every_queued_action() {
    let _app = running_with_key(KeyCode::KeyW);
    let before = snapshot();
    let keyboard = keyboard_state();
    let runtime = playback_state();
    for _ in 0..MAX_ACTIONS {
        request_action(EngineAction::CycleRate);
    }
    let actions = queued_actions();
    let space = SPACE_HELD.with(Cell::get);
    for request in [
        serde_json::json!({"op":"tune_ship","settings":{"mass_kg":4321.0,"inertia_scale":1.5,"thrust_scale":2.0}}),
        serde_json::json!({"op":"tune_debris_flow","settings":{"speed_mps":30.0,"speed_spread_mps":5.0,"stream_axis":"y","stream_sign":-1}}),
    ] {
        let command = serde_json::from_value(request).unwrap();
        let after = dispatch(Dispatch::Physics(command)).unwrap();
        assert_eq!(after.time_s, before.time_s);
        assert_eq!(after.seed, before.seed);
        assert_eq!(after.rng_state, before.rng_state);
        assert_eq!(after.manual_interventions, before.manual_interventions);
        assert_eq!(ship(&after).position, ship(&before).position);
        assert_eq!(ship(&after).rotation, ship(&before).rotation);
        assert_eq!(ship(&after).velocity, ship(&before).velocity);
        assert_eq!(
            ship(&after).angular_velocity_rad_s,
            ship(&before).angular_velocity_rad_s
        );
        assert_eq!(
            ship(&after).commanded_accel_mps2,
            ship(&before).commanded_accel_mps2
        );
        assert_eq!(keyboard_state(), keyboard);
        assert_eq!(playback_state(), runtime);
        assert_eq!(queued_actions(), actions);
        assert_eq!(SPACE_HELD.with(Cell::get), space);
    }
    assert_eq!(ship(&snapshot()).mass_kg, 4321.0);
    assert!(ship(&snapshot()).thrust_n[0].abs() > ship(&before).thrust_n[0].abs());
}

#[test]
fn invalid_or_unauthorized_live_tuning_is_transactional() {
    let mut app = running_with_key(KeyCode::Space);
    for request in [
        serde_json::json!({"op":"tune_ship","settings":{"mass_kg":-1.0,"inertia_scale":1.0,"thrust_scale":1.0}}),
        serde_json::json!({"op":"tune_debris_flow","settings":{"speed_mps":1.0,"speed_spread_mps":5.0,"stream_axis":"x","stream_sign":1}}),
    ] {
        assert_rejected_unchanged(serde_json::from_value(request).unwrap());
    }
    app.world_mut().resource_mut::<PlaybackState>().can_control = false;
    frame(&mut app, Duration::ZERO);
    for request in [
        serde_json::json!({"op":"tune_ship","settings":{"mass_kg":4321.0,"inertia_scale":1.0,"thrust_scale":1.0}}),
        serde_json::json!({"op":"tune_debris_flow","settings":{"speed_mps":30.0,"speed_spread_mps":5.0,"stream_axis":"x","stream_sign":1}}),
    ] {
        assert!(
            assert_rejected_unchanged(serde_json::from_value(request).unwrap())
                .contains("view-only")
        );
    }
}

fn pausing_commands() -> [PhysicsCommand; 4] {
    [
        PhysicsCommand::Reset { seed: Some(17) },
        PhysicsCommand::Restore {
            state: Box::new(Simulation::new(19).snapshot()),
        },
        PhysicsCommand::DebugUpright,
        PhysicsCommand::ConfigureExperiment {
            settings: custom_experiment(),
            seed: Some(23),
        },
    ]
}

fn assert_immediate_pause() {
    let (state, accumulator) = playback_state();
    assert_eq!(state["running"], false);
    assert_eq!(accumulator, 0.0);
    let report: Value = serde_json::from_str(&runtime_report()).unwrap();
    assert_eq!(report["running"], false);
    assert_eq!(report["keyboard_thrust"], false);
    assert_eq!(report["keyboard_requires_release"], true);
    assert_eq!(keyboard_state(), (false, false, true));
}

#[test]
fn manual_step_rejects_running_playback_without_changing_any_session_state() {
    let _app = running_with_key(KeyCode::Space);
    request_action(EngineAction::Pause);
    request_action(EngineAction::CycleRate);
    let error = assert_rejected_unchanged(PhysicsCommand::Step {
        seconds: STEP.as_secs_f64(),
    });
    assert!(error.contains("Pause the mission"));
}

#[test]
fn upright_and_configuration_require_authority_before_any_mutation() {
    let mut app = running_with_key(KeyCode::Space);
    app.world_mut().resource_mut::<PlaybackState>().can_control = false;
    frame(&mut app, Duration::ZERO);
    assert_eq!(keyboard_state(), (false, true, true));

    for queue_length in [0, MAX_ACTIONS] {
        for _ in 0..queue_length {
            request_action(EngineAction::CycleRate);
        }
        for command in [
            PhysicsCommand::DebugUpright,
            PhysicsCommand::ConfigureExperiment {
                settings: custom_experiment(),
                seed: Some(23),
            },
        ] {
            let error = assert_rejected_unchanged(command);
            assert!(error.contains("view-only"));
        }
    }
}

#[test]
fn every_pausing_command_rejects_a_full_queue_without_dropping_a_stale_pause() {
    for command in pausing_commands() {
        let _app = running_with_key(KeyCode::Space);
        request_action(EngineAction::Pause);
        request_action(EngineAction::Step);
        for _ in 2..MAX_ACTIONS {
            request_action(EngineAction::CycleRate);
        }
        assert_eq!(queued_actions_for_tests(), MAX_ACTIONS);
        let error = assert_rejected_unchanged(command);
        assert!(error.contains("queued actions"));
    }
}

#[test]
fn failed_execution_preserves_owned_and_pending_keyboard_commands_and_publication() {
    for pending_release in [false, true] {
        let _app = running_with_key(KeyCode::Space);
        if pending_release {
            block_keyboard_until_release();
            assert_eq!(keyboard_state(), (false, true, true));
        }
        request_action(EngineAction::Viewport(ViewportAction::Maximize));
        request_action(EngineAction::CycleRate);
        request_action(EngineAction::Step);
        request_action(EngineAction::Pause);
        request_action(EngineAction::Play(PlaybackRate::Tenfold));
        let mut invalid_restore = snapshot().as_ref().clone();
        invalid_restore.seed = u64::from(u32::MAX) + 1;
        let mut invalid_settings = custom_experiment();
        invalid_settings.ship.mass_kg = -1.0;
        for command in [
            PhysicsCommand::Reset {
                seed: Some(u64::from(u32::MAX) + 1),
            },
            PhysicsCommand::Restore {
                state: Box::new(invalid_restore),
            },
            PhysicsCommand::ConfigureExperiment {
                settings: invalid_settings,
                seed: Some(23),
            },
            PhysicsCommand::ConfigureExperiment {
                settings: custom_experiment(),
                seed: Some(u64::from(u32::MAX) + 1),
            },
            PhysicsCommand::SetCommand {
                accel: [f64::NAN, 0.0, 0.0],
                angular_accel: [0.0; 3],
            },
            PhysicsCommand::SetCommand {
                accel: [0.0; 3],
                angular_accel: [0.0, f64::INFINITY, 0.0],
            },
        ] {
            assert_rejected_unchanged(command);
        }
    }
}

#[test]
fn rejected_execution_does_not_initialize_an_unpublished_snapshot() {
    let _app = fresh_app();
    assert!(PUBLISHED.with(|cell| cell.borrow().is_none()));
    let mut invalid_restore = SIMULATION.with(|sim| sim.borrow().snapshot());
    invalid_restore.time_s = -1.0;
    for command in [
        PhysicsCommand::Restore {
            state: Box::new(invalid_restore),
        },
        PhysicsCommand::Step { seconds: -0.1 },
        PhysicsCommand::Step { seconds: f64::NAN },
        PhysicsCommand::Step {
            seconds: scenario::settings().integration.maximum_command_step_s + 1.0,
        },
    ] {
        assert_rejected_unchanged(command);
        assert!(PUBLISHED.with(|cell| cell.borrow().is_none()));
    }
}

#[test]
fn exhausted_upright_counter_cannot_pause_or_cancel_an_owned_keyboard_command() {
    let mut app = running_with_key(KeyCode::KeyW);
    let mut saturated = snapshot().as_ref().clone();
    saturated.manual_interventions = u32::MAX;
    dispatch(Dispatch::Physics(PhysicsCommand::Restore {
        state: Box::new(saturated),
    }))
    .unwrap();
    release(&mut app, KeyCode::KeyW);
    frame(&mut app, Duration::ZERO);
    request_action(EngineAction::Play(PlaybackRate::RealTime));
    press(&mut app, KeyCode::KeyW);
    frame(&mut app, STEP);
    assert_eq!(keyboard_state(), (true, true, false));
    request_action(EngineAction::CycleRate);
    let error = assert_rejected_unchanged(PhysicsCommand::DebugUpright);
    assert!(error.contains("counter exhausted"));
}

#[test]
fn observe_preserves_owned_keyboard_input_even_with_a_full_action_queue() {
    let _app = running_with_key(KeyCode::KeyW);
    for _ in 0..MAX_ACTIONS {
        request_action(EngineAction::CycleRate);
    }
    let before = physics_json();
    let runtime = playback_state();
    let actions = queued_actions();
    let observed = dispatch(Dispatch::Physics(PhysicsCommand::Observe)).unwrap();
    assert_eq!(serde_json::to_value(observed.as_ref()).unwrap(), before);
    assert!(Rc::ptr_eq(&observed, &snapshot()));
    assert_eq!(keyboard_state(), (true, true, false));
    assert_eq!(playback_state(), runtime);
    assert_eq!(queued_actions(), actions);
}

#[test]
fn paused_step_uses_real_physics_without_cancelling_keyboard_or_requiring_a_queue_slot() {
    for can_control in [true, false] {
        let mut app = fresh_app();
        app.world_mut().resource_mut::<PlaybackState>().can_control = can_control;
        frame(&mut app, Duration::ZERO);
        let keyboard = keyboard_state();
        for _ in 0..MAX_ACTIONS {
            request_action(EngineAction::CycleRate);
        }
        let runtime = playback_state();
        let actions = queued_actions();
        let mut expected = Simulation::new_default();
        let expected = expected
            .execute(PhysicsCommand::Step {
                seconds: STEP.as_secs_f64(),
            })
            .unwrap();
        let stepped = dispatch(Dispatch::Physics(PhysicsCommand::Step {
            seconds: STEP.as_secs_f64(),
        }))
        .unwrap();
        assert_eq!(
            serde_json::to_value(stepped.as_ref()).unwrap(),
            serde_json::to_value(expected).unwrap()
        );
        assert!(stepped.time_s > 0.0);
        assert_eq!(keyboard_state(), keyboard);
        assert_eq!(playback_state(), runtime);
        assert_eq!(queued_actions(), actions);
    }
}

#[test]
fn external_commands_supersede_pending_keyboard_release_in_view_only_with_a_full_queue() {
    let config = scenario::settings().lander;
    for command in [
        PhysicsCommand::SetCommand {
            accel: [0.0, config.max_accel_mps2 * 0.25, 0.0],
            angular_accel: [0.0; 3],
        },
        PhysicsCommand::Stop,
    ] {
        let mut app = running_with_key(KeyCode::KeyW);
        app.world_mut().resource_mut::<PlaybackState>().can_control = false;
        frame(&mut app, Duration::ZERO);
        assert_eq!(keyboard_state(), (false, true, true));
        for _ in 0..MAX_ACTIONS {
            request_action(EngineAction::CycleRate);
        }
        let runtime = playback_state();
        let actions = queued_actions();
        dispatch(Dispatch::Physics(command)).unwrap();
        assert_eq!(keyboard_state(), (false, false, true));
        assert_eq!(playback_state(), runtime);
        assert_eq!(queued_actions(), actions);
        let installed = physics_json();
        app.world_mut().resource_mut::<PlaybackState>().can_control = true;
        frame(&mut app, Duration::ZERO);
        release(&mut app, KeyCode::KeyW);
        frame(&mut app, Duration::ZERO);
        assert_eq!(
            physics_json(),
            installed,
            "late release changed external thrust"
        );
    }
}

#[test]
fn all_external_handoffs_wait_for_an_active_physical_release_without_reviving_space() {
    let config = scenario::settings().lander;
    let mut restored = Simulation::new(19);
    restored
        .set_command([0.0, config.max_accel_mps2 * 0.25, 0.0], [0.0; 3])
        .unwrap();
    let commands = [
        PhysicsCommand::SetCommand {
            accel: [0.0, config.max_accel_mps2 * 0.25, 0.0],
            angular_accel: [0.0, 0.0, config.max_angular_accel_rad_s2 * 0.25],
        },
        PhysicsCommand::Stop,
        PhysicsCommand::Reset { seed: Some(17) },
        PhysicsCommand::Restore {
            state: Box::new(restored.snapshot()),
        },
        PhysicsCommand::DebugUpright,
        PhysicsCommand::ConfigureExperiment {
            settings: custom_experiment(),
            seed: Some(23),
        },
    ];
    for command in commands {
        let mut app = running_with_key(KeyCode::Space);
        let mut expected = Simulation::new_default();
        expected
            .execute(PhysicsCommand::Restore {
                state: Box::new(snapshot().as_ref().clone()),
            })
            .unwrap();
        let expected = expected.execute(command.clone()).unwrap();
        let installed = dispatch(Dispatch::Physics(command)).unwrap();
        let installed_json = serde_json::to_value(installed.as_ref()).unwrap();
        assert_eq!(installed_json, serde_json::to_value(expected).unwrap());
        assert!(Rc::ptr_eq(&installed, &snapshot()));
        assert_eq!(keyboard_state(), (false, false, true));
        assert!(
            SPACE_HELD.with(Cell::get),
            "dispatch must preserve the lift handshake"
        );
        frame(&mut app, Duration::ZERO);
        assert_eq!(physics_json(), installed_json);
        assert_eq!(keyboard_state(), (false, false, true));
        if !app.world().resource::<PlaybackState>().running {
            frame(&mut app, STEP);
            assert_eq!(
                physics_json(),
                installed_json,
                "held Space relaunched a paused trial"
            );
        }

        crate::camera::set_input_active(false, false);
        release(&mut app, KeyCode::Space);
        frame(&mut app, Duration::ZERO);
        assert_eq!(physics_json(), installed_json);
        assert!(keyboard_requires_release());
        crate::camera::set_input_active(true, false);
        press(&mut app, KeyCode::Space);
        frame(&mut app, Duration::ZERO);
        assert_eq!(
            physics_json(),
            installed_json,
            "inactive key-up must not rearm input"
        );
        assert_eq!(keyboard_state(), (false, false, true));
        release(&mut app, KeyCode::Space);
        frame(&mut app, Duration::ZERO);
        assert_eq!(
            physics_json(),
            installed_json,
            "late release changed the installed state"
        );
        assert_eq!(keyboard_state(), (false, false, false));
        press(&mut app, KeyCode::Space);
        frame(&mut app, Duration::ZERO);
        assert!(app.world().resource::<PlaybackState>().running);
        assert_eq!(keyboard_state(), (true, true, false));
        assert_eq!(
            ship(&snapshot()).commanded_accel_mps2,
            [0.0, 0.0, config.max_accel_mps2]
        );
    }
}

#[test]
fn paused_trial_commit_supersedes_older_flight_actions_and_preserves_independent_actions() {
    for command in pausing_commands() {
        let mut app = running_with_key(KeyCode::Space);
        RUNTIME.with(|runtime| runtime.borrow_mut().accumulator = 0.04);
        for action in [
            EngineAction::TogglePlayback,
            EngineAction::Play(PlaybackRate::Tenfold),
            EngineAction::Pause,
            EngineAction::Step,
            EngineAction::Reset,
            EngineAction::DebugUpright,
            EngineAction::Viewport(ViewportAction::Fullscreen),
            EngineAction::CycleRate,
            EngineAction::Viewport(ViewportAction::Maximize),
        ] {
            request_action(action);
        }
        // The successful API commit is the linearization point. Every flight
        // action above belongs to the prior trial, even an older queued Pause.
        let installed = dispatch(Dispatch::Physics(command)).unwrap();
        let installed_json = serde_json::to_value(installed.as_ref()).unwrap();
        assert_immediate_pause();
        ACTIONS.with(|actions| {
            let queued: Vec<_> = actions
                .borrow()
                .iter()
                .map(|action| action.engine_action())
                .collect();
            assert!(matches!(
                queued.as_slice(),
                [
                    EngineAction::Pause,
                    EngineAction::Viewport(ViewportAction::Fullscreen),
                    EngineAction::CycleRate,
                    EngineAction::Viewport(ViewportAction::Maximize),
                ]
            ));
        });
        for _ in 0..2 {
            frame(&mut app, STEP);
            assert_eq!(physics_json(), installed_json);
            assert!(Rc::ptr_eq(&installed, &snapshot()));
            assert_immediate_pause();
            let native = app.world().resource::<PlaybackState>();
            assert!(!native.running);
            assert_eq!(native.accumulator, 0.0);
            assert_eq!(native.rate, PlaybackRate::Fivefold);
        }
        assert_eq!(queued_actions_for_tests(), 0);
    }
}

#[test]
fn paused_trials_accept_the_last_free_slot_without_discarding_independent_actions() {
    for command in pausing_commands() {
        let mut app = running_with_key(KeyCode::Space);
        for _ in 0..MAX_ACTIONS - 1 {
            request_action(EngineAction::CycleRate);
        }
        let installed = dispatch(Dispatch::Physics(command)).unwrap();
        assert_immediate_pause();
        assert_eq!(queued_actions_for_tests(), MAX_ACTIONS);
        ACTIONS.with(|actions| {
            let actions = actions.borrow();
            assert!(matches!(
                actions.front().map(|action| action.engine_action()),
                Some(EngineAction::Pause)
            ));
            assert!(
                actions
                    .iter()
                    .skip(1)
                    .all(|action| matches!(action.engine_action(), EngineAction::CycleRate))
            );
        });
        frame(&mut app, STEP);
        assert_eq!(
            physics_json(),
            serde_json::to_value(installed.as_ref()).unwrap()
        );
        assert!(!app.world().resource::<PlaybackState>().running);
        assert_eq!(queued_actions_for_tests(), 0);
    }
}

#[test]
fn older_play_toggle_and_step_cannot_resume_or_advance_a_paused_trial_in_runtime_frames() {
    for stale_action in [
        EngineAction::Play(PlaybackRate::Tenfold),
        EngineAction::TogglePlayback,
        EngineAction::Step,
    ] {
        for command in pausing_commands() {
            let mut app = running_with_key(KeyCode::Space);
            app.world_mut().resource_mut::<PlaybackState>().accumulator = 0.04;
            request_action(stale_action);
            let installed = dispatch(Dispatch::Physics(command)).unwrap();
            let installed_json = serde_json::to_value(installed.as_ref()).unwrap();
            assert_immediate_pause();
            // Run the installed runtime with real elapsed time. A prepended
            // Pause alone would let the older Play/Toggle resume, or Step advance.
            for _ in 0..2 {
                frame(&mut app, STEP);
                assert_eq!(physics_json(), installed_json);
                assert_immediate_pause();
                let native = app.world().resource::<PlaybackState>();
                assert!(!native.running);
                assert_eq!(native.accumulator, 0.0);
                assert_eq!(native.rate, PlaybackRate::RealTime);
            }
        }
    }
}

#[test]
fn play_requested_after_a_paused_trial_api_commit_can_resume_without_rearming_held_keys() {
    for command in pausing_commands() {
        let mut app = running_with_key(KeyCode::Space);
        for action in [
            EngineAction::Play(PlaybackRate::Tenfold),
            EngineAction::TogglePlayback,
            EngineAction::Step,
        ] {
            request_action(action);
        }
        let installed = dispatch(Dispatch::Physics(command)).unwrap();
        assert_immediate_pause();
        // This is a new request, ordered after the successful paused-trial
        // commit. It must remain behind the committed Pause and may resume.
        dispatch(Dispatch::Action(EngineAction::Play(PlaybackRate::RealTime))).unwrap();
        frame(&mut app, Duration::ZERO);
        assert!(app.world().resource::<PlaybackState>().running);
        assert_eq!(
            physics_json(),
            serde_json::to_value(installed.as_ref()).unwrap()
        );
        assert_eq!(keyboard_state(), (false, false, true));
        frame(&mut app, STEP);
        let advanced = snapshot();
        assert!(advanced.time_s > installed.time_s);
        assert!(app.world().resource::<PlaybackState>().running);
        assert_eq!(keyboard_state(), (false, false, true));
        assert_eq!(
            advanced.mission.fuel_used_kg,
            installed.mission.fuel_used_kg
        );
        assert_eq!(
            ship(&advanced).commanded_accel_mps2,
            ship(&installed).commanded_accel_mps2
        );
        let report: Value = serde_json::from_str(&runtime_report()).unwrap();
        assert_eq!(report["running"], true);
        assert_eq!(report["keyboard_thrust"], false);
    }
}

#[test]
fn configure_builds_the_real_profile_and_reset_repeats_the_active_trial() {
    let mut app = running_with_key(KeyCode::Space);
    let settings = custom_experiment();
    let configured = dispatch(Dispatch::Physics(PhysicsCommand::ConfigureExperiment {
        settings,
        seed: Some(23),
    }))
    .unwrap();
    assert_immediate_pause();
    let expected = Simulation::with_experiment(23, settings)
        .unwrap()
        .snapshot();
    assert_eq!(
        serde_json::to_value(configured.as_ref()).unwrap(),
        serde_json::to_value(expected).unwrap()
    );
    assert_eq!(ship(&configured).mass_kg, settings.ship.mass_kg);
    assert_eq!(
        configured
            .assets
            .iter()
            .filter(|entity| entity.kind == EntityKind::Debris)
            .count(),
        settings.debris.count
    );
    frame(&mut app, STEP);
    assert_eq!(
        physics_json(),
        serde_json::to_value(configured.as_ref()).unwrap()
    );
    assert_eq!(snapshot().manual_interventions, 0);
    assert!(!app.world().resource::<PlaybackState>().running);

    release(&mut app, KeyCode::Space);
    frame(&mut app, Duration::ZERO);
    press(&mut app, KeyCode::Space);
    frame(&mut app, STEP);
    assert!(snapshot().time_s > 0.0);
    let reset = dispatch(Dispatch::Physics(PhysicsCommand::Reset { seed: None })).unwrap();
    assert_eq!(
        serde_json::to_value(reset.as_ref()).unwrap(),
        serde_json::to_value(configured.as_ref()).unwrap(),
        "Reset must repeat the active physical configuration"
    );
    assert_immediate_pause();
}

fn context_json(context: CommandContext) -> Value {
    serde_json::json!({
        "trial_revision": context.trial_revision, "tuning_revision": context.tuning_revision,
        "manual_interventions": context.manual_interventions, "seed": context.seed,
        "running": context.running,
    })
}

fn control_call(mut request: Value) -> Value {
    request["expected_context"] = context_json(current_context());
    serde_json::from_str(&crate::api::control(&request.to_string())).unwrap()
}

fn control_claim(run: &str, controller: &str, epoch: u32, term: u32, duration: f64) -> Value {
    serde_json::json!({
        "op": "claim_control", "run_id": run, "controller_id": controller,
        "expected_epoch": epoch, "expected_term": term, "source_time_s": snapshot().time_s,
        "lease_sim_s": duration,
    })
}

fn actual_fence() -> crate::model::ControlFence {
    serde_json::from_value(observation()["control_authority"].clone())
        .map(|authority: crate::model::ControlAuthority| authority.fence().unwrap())
        .unwrap()
}

fn control_lease(fence: &crate::model::ControlFence, duration: f64) -> Value {
    serde_json::json!({
        "op": "set_leased_command", "accel": [0.0, 0.0, 0.5], "angular_accel": [0.0, 0.0, 0.0],
        "lease_sim_s": duration, "source_time_s": snapshot().time_s,
        "expected_sensor_revision": sensor_revision(), "controller_fence": fence,
    })
}

fn control_renewal(fence: &crate::model::ControlFence, duration: f64) -> Value {
    serde_json::json!({
        "op": "renew_control", "controller_fence": fence,
        "source_time_s": snapshot().time_s, "lease_sim_s": duration,
        "expected_sensor_revision": sensor_revision(),
    })
}

fn assert_control_rejected_unchanged(mut request: Value, code: &str) {
    if request.get("expected_context").is_none() {
        request["expected_context"] = context_json(current_context());
    }
    let before = physics_json();
    let published = snapshot();
    let runtime = playback_state();
    let keyboard = keyboard_state();
    let actions = queued_actions();
    let response: Value = serde_json::from_str(&crate::api::control(&request.to_string())).unwrap();
    assert_eq!(response["ok"], false, "{response}");
    assert_eq!(response["code"], code, "{response}");
    assert_eq!(physics_json(), before);
    assert!(Rc::ptr_eq(&published, &snapshot()));
    assert_eq!(playback_state(), runtime);
    assert_eq!(keyboard_state(), keyboard);
    assert_eq!(queued_actions(), actions);
}

#[test]
fn ownership_renewal_returns_only_a_native_receipt_without_publication_or_actuator_changes() {
    let _app = fresh_app();
    assert_eq!(
        observation()["control_capabilities"]["ownership_renewal"]["schema_version"],
        1
    );
    assert_eq!(
        control_call(control_claim("run", "primary", 0, 0, 0.1))["ok"],
        true
    );
    let fence = actual_fence();
    assert_eq!(control_call(control_lease(&fence, 0.1))["ok"], true);
    let before = physics_json();
    let published = snapshot();
    let runtime = playback_state();
    let actions = queued_actions();
    let keyboard = keyboard_state();
    let applied = serde_json::to_value(applied_physics().unwrap()).unwrap();
    let response = control_call(control_renewal(&fence, 0.75));
    assert_eq!(
        response,
        serde_json::json!({"ok": true, "ownership_renewal": {
            "source_time_s": published.time_s, "lease_sim_s": 0.75,
            "sensor_revision": sensor_revision(), "control_authority": observation()["control_authority"],
        }})
    );
    assert_eq!(actual_fence(), fence);
    assert_eq!(
        observation()["control_authority"]["expires_at_sim_time_s"],
        published.time_s + 0.75
    );
    let mut after = physics_json();
    let mut before = before;
    after["sensor_state"]["control_authority"].take();
    before["sensor_state"]["control_authority"].take();
    assert_eq!(after, before);
    assert!(Rc::ptr_eq(&published, &snapshot()));
    assert_eq!(
        serde_json::to_value(applied_physics().unwrap()).unwrap(),
        applied
    );
    assert_eq!(playback_state(), runtime);
    assert_eq!(queued_actions(), actions);
    assert_eq!(keyboard_state(), keyboard);
    assert_control_rejected_unchanged(control_renewal(&fence, 0.75), "stale_context");
}

#[test]
fn ownership_renewal_fails_closed_on_native_context_sensor_input_authority_and_pending_actions() {
    for change in [
        "trial",
        "tuning",
        "manual",
        "sensor",
        "navigation_unavailable",
        "radar_unavailable",
        "pending_play",
        "pause",
        "view_only",
        "running",
        "seed",
        "follower",
    ] {
        let mut app = fresh_app();
        assert_eq!(
            control_call(control_claim("run", "primary", 0, 0, 0.1))["ok"],
            true
        );
        let fence = actual_fence();
        let mut request = control_renewal(&fence, 0.75);
        request["expected_context"] = context_json(current_context());
        match change {
            "trial" => {
                request["expected_context"]["trial_revision"] =
                    serde_json::json!(trial_revision() + 1)
            }
            "tuning" => {
                let mut settings = snapshot().ship_tuning();
                settings.thrust_scale = 2.0;
                dispatch(Dispatch::Physics(PhysicsCommand::TuneShip { settings })).unwrap();
            }
            "manual" => {
                dispatch(Dispatch::Physics(PhysicsCommand::SetCommand {
                    accel: [0.0, 0.2, 0.0],
                    angular_accel: [0.0; 3],
                }))
                .unwrap();
            }
            "sensor" => {
                dispatch(Dispatch::Physics(PhysicsCommand::SetSensorFault {
                    target: crate::model::SensorFaultTarget::RadarBackup,
                    mode: crate::model::SensorFaultMode::Frozen,
                    duration_sim_s: Some(1.0),
                }))
                .unwrap();
            }
            "navigation_unavailable" | "radar_unavailable" => {
                let target = if change == "navigation_unavailable" {
                    crate::model::SensorFaultTarget::Navigation
                } else {
                    crate::model::SensorFaultTarget::RadarBus
                };
                dispatch(Dispatch::Physics(PhysicsCommand::SetSensorFault {
                    target,
                    mode: crate::model::SensorFaultMode::Unavailable,
                    duration_sim_s: Some(1.0),
                }))
                .unwrap();
                request["expected_sensor_revision"] = serde_json::json!(sensor_revision());
            }
            "pending_play" => {
                dispatch_play_checked(PlaybackRate::RealTime, current_context()).unwrap();
            }
            "pause" => request_action(EngineAction::Pause),
            "view_only" => {
                app.world_mut().resource_mut::<PlaybackState>().can_control = false;
                frame(&mut app, Duration::ZERO);
            }
            "running" => request["expected_context"]["running"] = serde_json::json!(true),
            "seed" => request["expected_context"]["seed"] = serde_json::json!(101),
            "follower" => {
                request["controller_fence"]["controller_id"] = serde_json::json!("standby")
            }
            _ => unreachable!(),
        }
        assert_control_rejected_unchanged(request, "stale_context");
    }
}

#[test]
fn native_api_excludes_healthy_standby_and_rejects_a_late_primary_with_fresh_context() {
    let mut app = fresh_app();
    assert_eq!(
        control_call(control_claim("run", "primary", 0, 0, 0.1)),
        serde_json::json!({"ok": true})
    );
    let primary = actual_fence();
    assert_eq!(primary.term, 1);
    assert_eq!(control_call(control_lease(&primary, 0.1))["ok"], true);
    assert_control_rejected_unchanged(
        control_claim("run", "standby", primary.epoch, primary.term, 0.2),
        "stale_context",
    );
    dispatch_play_checked(PlaybackRate::RealTime, current_context()).unwrap();
    for _ in 0..3 {
        frame(&mut app, STEP);
    }
    assert!(is_running());
    assert_eq!(observation()["control_authority"]["status"], "expired");
    assert_eq!(observation()["control_authority"]["epoch"], primary.epoch);
    assert_eq!(
        control_call(control_claim(
            "run",
            "standby",
            primary.epoch,
            primary.term,
            0.3
        ))["ok"],
        true
    );
    let standby = actual_fence();
    assert_eq!(standby.term, primary.term + 1);
    assert_eq!(control_call(control_lease(&standby, 0.3))["ok"], true);
    assert_control_rejected_unchanged(control_lease(&primary, 0.3), "stale_context");
    frame(&mut app, STEP);
    assert!(
        applied_physics()
            .unwrap()
            .last_actuator_application
            .unwrap()
            .force_body_n[2]
            > 0.0
    );
    let before = snapshot();
    let velocity = ship(&before).velocity;
    assert_eq!(
        control_call(serde_json::json!({
            "op": "release_control", "run_id": "run", "expected_epoch": primary.epoch,
        }))["ok"],
        true
    );
    assert!(is_running(), "release must not synthesize a Pause");
    assert_eq!(snapshot().time_s, before.time_s);
    assert_eq!(ship(&snapshot()).velocity, velocity);
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0; 3]);
    frame(&mut app, STEP);
    assert!(snapshot().time_s > before.time_s);
}

#[test]
fn original_consent_cannot_claim_release_or_resume_after_native_pause() {
    for start_running in [false, true] {
        let mut app = fresh_app();
        assert_eq!(
            control_call(control_claim("run", "primary", 0, 0, 0.3))["ok"],
            true
        );
        let primary = actual_fence();
        assert_eq!(control_call(control_lease(&primary, 0.3))["ok"], true);
        dispatch_play_checked(PlaybackRate::RealTime, current_context()).unwrap();
        if start_running {
            frame(&mut app, STEP);
            assert!(is_running());
        }
        let pause_time = snapshot().time_s;
        // A real native P/Pause intent must supersede a controller start already queued.
        request_action(EngineAction::Pause);
        assert_eq!(observation()["control_authority"]["status"], "revoked");
        assert_eq!(
            observation()["control_authority"]["epoch"],
            primary.epoch + 1
        );
        frame(&mut app, STEP);
        assert!(!is_running());
        assert_eq!(snapshot().time_s, pause_time);
        assert_control_rejected_unchanged(
            control_claim("run", "standby", primary.epoch, primary.term, 0.3),
            "stale_context",
        );
        assert_control_rejected_unchanged(
            serde_json::json!({
                "op": "play", "rate": 1.0,
            }),
            "stale_context",
        );
        dispatch(Dispatch::Physics(PhysicsCommand::SetCommand {
            accel: [0.0, 0.2, 0.0],
            angular_accel: [0.0; 3],
        }))
        .unwrap();
        assert_control_rejected_unchanged(
            serde_json::json!({
                "op": "release_control", "run_id": "run", "expected_epoch": primary.epoch,
            }),
            "stale_context",
        );
        assert_control_rejected_unchanged(control_lease(&primary, 0.3), "stale_context");
        for _ in 0..3 {
            frame(&mut app, STEP);
        }
        assert!(!is_running());
        assert_eq!(snapshot().time_s, pause_time);
        assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0, 0.2, 0.0]);
    }
}

#[test]
fn committed_setup_pause_is_internal_sync_and_does_not_revoke_a_new_cohort() {
    let mut app = fresh_app();
    let mut settings = ExperimentSettings::default();
    settings.debris.count = 0;
    dispatch(Dispatch::Physics(PhysicsCommand::ConfigureFlight {
        settings,
        seed: Some(100),
        preset: crate::model::FlightPreset::MarsFacingL1,
    }))
    .unwrap();
    assert_eq!(queued_actions_for_tests(), 1);
    assert_eq!(
        control_call(control_claim("run", "primary", 0, 0, 0.3))["ok"],
        true
    );
    let primary = actual_fence();
    assert_eq!(control_call(control_lease(&primary, 0.3))["ok"], true);
    dispatch_play_checked(PlaybackRate::RealTime, current_context()).unwrap();
    frame(&mut app, STEP);
    assert!(is_running());
    assert_eq!(actual_fence(), primary);
    assert_eq!(observation()["control_authority"]["status"], "active");
    assert!(snapshot().mission.fuel_used_kg > 0.0);
}

#[test]
fn release_wins_over_queued_checked_play_and_stale_release_cannot_clear_a_new_consent_run() {
    let mut app = fresh_app();
    assert_eq!(
        control_call(control_claim("run", "primary", 0, 0, 0.3))["ok"],
        true
    );
    let primary = actual_fence();
    assert_eq!(control_call(control_lease(&primary, 0.3))["ok"], true);
    dispatch_play_checked(PlaybackRate::RealTime, current_context()).unwrap();
    assert_eq!(
        control_call(serde_json::json!({
            "op": "release_control", "run_id": "run", "expected_epoch": primary.epoch,
        }))["ok"],
        true
    );
    frame(&mut app, STEP);
    assert!(!is_running());
    assert_eq!(snapshot().time_s, 0.0);
    let authority = observation()["control_authority"].clone();
    assert_eq!(
        control_call(control_claim(
            "new-run",
            "new-primary",
            authority["epoch"].as_u64().unwrap() as u32,
            primary.term,
            0.3
        ))["ok"],
        true
    );
    let newer = actual_fence();
    assert_eq!(control_call(control_lease(&newer, 0.3))["ok"], true);
    assert_control_rejected_unchanged(
        serde_json::json!({
            "op": "release_control", "run_id": "run", "expected_epoch": primary.epoch,
        }),
        "stale_context",
    );
    assert_eq!(actual_fence(), newer);
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0, 0.0, 0.5]);
}

#[test]
fn claim_validates_context_and_original_source_deadline_without_mutating_the_session() {
    let _app = fresh_app();
    dispatch(Dispatch::Physics(PhysicsCommand::Step { seconds: 0.2 })).unwrap();
    let mut old = control_claim("run", "primary", 0, 0, 0.1);
    old["source_time_s"] = serde_json::json!(0.0);
    assert_control_rejected_unchanged(old, "command_expired");
    let mut changed = control_claim("run", "primary", 0, 0, 0.3);
    let context = CommandContext {
        trial_revision: trial_revision() + 1,
        ..current_context()
    };
    changed["expected_context"] = context_json(context);
    assert_control_rejected_unchanged(changed, "stale_context");
}

fn real_clock_app() -> App {
    let mut app = fresh_app();
    // Use Bevy's actual automatic runtime clock, not a supplied test timestamp.
    app.add_plugins(bevy::time::TimePlugin)
        .edit_schedule(Update, |schedule| {
            schedule.set_executor(bevy::ecs::schedule::SingleThreadedExecutor::new());
        });
    app.update();
    app
}

#[test]
fn paused_ownership_renewal_cannot_extend_the_original_actuator_runtime_deadline() {
    let mut app = real_clock_app();
    assert_eq!(
        control_call(control_claim("run", "primary", 0, 0, 0.2))["ok"],
        true
    );
    let primary = actual_fence();
    assert_eq!(control_call(control_lease(&primary, 0.05))["ok"], true);
    let before = observation();
    let watchdog = before["command_watchdog"].clone();
    assert_eq!(control_call(control_renewal(&primary, 0.75))["ok"], true);
    assert_eq!(observation()["command_watchdog"], watchdog);
    std::thread::sleep(Duration::from_millis(100));
    app.update();
    assert_eq!(observation()["time_s"], before["time_s"]);
    assert_eq!(observation()["control_authority"]["status"], "active");
    assert_eq!(actual_fence(), primary);
    assert_eq!(observation()["command_watchdog"]["status"], "expired");
    assert_eq!(
        observation()["command_watchdog"]["source_time_s"],
        watchdog["source_time_s"]
    );
    assert_eq!(
        observation()["command_watchdog"]["expires_at_sim_time_s"],
        watchdog["expires_at_sim_time_s"]
    );
    assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0; 3]);
    // Repeating a held source must not slide either private runtime deadline.
    assert_control_rejected_unchanged(control_renewal(&primary, 0.75), "stale_context");
    std::thread::sleep(Duration::from_millis(800));
    app.update();
    assert_eq!(observation()["control_authority"]["status"], "expired");
    assert_control_rejected_unchanged(control_renewal(&primary, 0.75), "stale_context");
}

#[test]
fn paused_real_runtime_expires_a_dead_primary_before_play_and_allows_same_consent_standby() {
    for owns_force in [false, true] {
        let mut app = real_clock_app();
        assert_eq!(
            control_call(control_claim("run", "primary", 0, 0, 0.05))["ok"],
            true
        );
        let primary = actual_fence();
        if owns_force {
            assert_eq!(control_call(control_lease(&primary, 0.05))["ok"], true);
        }
        let before = snapshot();
        let runtime_start = app.world().resource::<Time<Real>>().elapsed_secs_f64();
        assert_control_rejected_unchanged(
            control_claim("run", "standby", primary.epoch, primary.term, 0.3),
            "stale_context",
        );
        // No CheckedPlay is ever sent. Only real elapsed runtime advances.
        std::thread::sleep(Duration::from_millis(100));
        app.update();
        let runtime_now = app.world().resource::<Time<Real>>().elapsed_secs_f64();
        assert!(runtime_now - runtime_start >= 0.05);
        assert!(!is_running());
        assert_eq!(snapshot().time_s, before.time_s);
        assert_eq!(ship(&snapshot()).position, ship(&before).position);
        assert_eq!(ship(&snapshot()).velocity, ship(&before).velocity);
        assert_eq!(snapshot().mission.fuel_used_kg, before.mission.fuel_used_kg);
        assert_eq!(ship(&snapshot()).commanded_accel_mps2, [0.0; 3]);
        let authority = observation()["control_authority"].clone();
        assert_eq!(authority.as_object().unwrap().len(), 6);
        assert_eq!(authority["status"], "expired");
        assert_eq!(authority["epoch"], primary.epoch);
        assert_eq!(authority["term"], primary.term);
        assert_eq!(authority["run_id"], "run");
        assert_eq!(authority["expires_at_sim_time_s"], before.time_s + 0.05);
        if owns_force {
            assert_eq!(observation()["command_watchdog"]["status"], "expired");
        }
        assert_eq!(
            control_call(control_claim(
                "run",
                "standby",
                primary.epoch,
                primary.term,
                0.3
            ))["ok"],
            true
        );
        let standby = actual_fence();
        assert_eq!(standby.epoch, primary.epoch);
        assert_eq!(standby.term, primary.term + 1);
        assert_eq!(control_call(control_lease(&standby, 0.3))["ok"], true);
        assert_control_rejected_unchanged(control_lease(&primary, 0.3), "stale_context");
        assert!(
            !is_running(),
            "a successful standby claim does not itself resume playback"
        );
    }
}

#[test]
fn runtime_expiry_cannot_clear_manual_force_or_resume_archived_authority() {
    let mut app = real_clock_app();
    assert_eq!(
        control_call(control_claim("run", "primary", 0, 0, 0.05))["ok"],
        true
    );
    let primary = actual_fence();
    assert_eq!(control_call(control_lease(&primary, 0.05))["ok"], true);
    dispatch(Dispatch::Physics(PhysicsCommand::SetCommand {
        accel: [0.0, 0.2, 0.0],
        angular_accel: [0.0; 3],
    }))
    .unwrap();
    let manual = physics_json();
    std::thread::sleep(Duration::from_millis(100));
    app.update();
    assert_eq!(physics_json(), manual);
    assert!(!is_running());
    let revoked = actual_authority();
    assert_eq!(
        control_call(control_claim(
            "new-run",
            "primary",
            revoked.epoch,
            revoked.term,
            0.05
        ))["ok"],
        true
    );
    let next = actual_fence();
    assert_eq!(control_call(control_lease(&next, 0.05))["ok"], true);
    std::thread::sleep(Duration::from_millis(100));
    app.update();
    let expired = snapshot();
    assert_eq!(observation()["control_authority"]["status"], "expired");
    assert!(
        observation()["control_authority"]["expires_at_sim_time_s"]
            .as_f64()
            .unwrap()
            > expired.time_s
    );
    dispatch(Dispatch::Physics(PhysicsCommand::Restore {
        state: Box::new(expired.as_ref().clone()),
    }))
    .unwrap();
    assert_eq!(observation()["control_authority"]["status"], "revoked");
    let restored = physics_json();
    std::thread::sleep(Duration::from_millis(100));
    app.update();
    assert_eq!(physics_json(), restored);
    assert_eq!(snapshot().time_s, expired.time_s);
    assert!(!is_running());
    assert_control_rejected_unchanged(control_lease(&next, 0.3), "stale_context");
}

fn actual_authority() -> crate::model::ControlAuthority {
    serde_json::from_value(observation()["control_authority"].clone()).unwrap()
}

#[test]
fn a_new_run_claim_after_runtime_expiry_uses_its_actual_granted_epoch() {
    let mut app = real_clock_app();
    assert_eq!(
        control_call(control_claim("run", "primary", 0, 0, 0.05))["ok"],
        true
    );
    let old = actual_fence();
    std::thread::sleep(Duration::from_millis(100));
    app.update();
    assert_eq!(observation()["control_authority"]["status"], "expired");
    assert_eq!(
        control_call(control_claim(
            "new-run",
            "new-primary",
            old.epoch,
            old.term,
            0.3
        ))["ok"],
        true
    );
    let granted = actual_fence();
    assert_eq!(granted.epoch, old.epoch + 1);
    assert_eq!(granted.term, old.term + 1);
    let assumed = crate::model::ControlFence {
        epoch: old.epoch,
        ..granted.clone()
    };
    assert_control_rejected_unchanged(control_lease(&assumed, 0.3), "stale_context");
    assert_eq!(control_call(control_lease(&granted, 0.3))["ok"], true);
}
