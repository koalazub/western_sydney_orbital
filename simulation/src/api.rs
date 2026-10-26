//! Browser JSON boundary. Requests become typed once, then dispatch by variant.
use crate::{
    domain::{CameraMode, CameraRequest, EngineAction, PlaybackRate},
    model::Snapshot,
    physics::PhysicsCommand,
};
use serde::Deserialize;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct CommandContext {
    pub trial_revision: u32,
    pub tuning_revision: u32,
    pub manual_interventions: u32,
    pub seed: u64,
    pub running: bool,
}

#[derive(Debug)]
pub(crate) enum CommandError {
    Rejected(String),
    StaleContext,
    CommandExpired,
}

impl std::fmt::Display for CommandError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Rejected(message) => formatter.write_str(message),
            Self::StaleContext => formatter.write_str(
                "The flight changed before the command applied. Read the current state and recompute the command.",
            ),
            Self::CommandExpired => formatter.write_str(
                "The autonomous decision expired before invocation; no command was applied.",
            ),
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum ApiRequest {
    #[serde(deserialize_with = "crate::model::deserialize_empty_command")]
    Observe,
    Reset {
        seed: Option<u64>,
    },
    ConfigureExperiment {
        settings: crate::model::ExperimentSettings,
        seed: Option<u64>,
    },
    ConfigureFlight {
        settings: crate::model::ExperimentSettings,
        seed: Option<u64>,
        preset: crate::model::FlightPreset,
    },
    PlaceLander {
        position_m: [f64; 3],
    },
    MoveLaunchpad {
        surface_direction: [f64; 3],
        expected_context: Option<CommandContext>,
    },
    TuneShip {
        settings: crate::model::ShipTuning,
    },
    TuneDebrisFlow {
        settings: crate::model::DebrisFlowTuning,
    },
    Restore {
        state: Box<Snapshot>,
    },
    Step {
        seconds: f64,
    },
    SetCommand {
        accel: [f64; 3],
        angular_accel: [f64; 3],
        expected_context: Option<CommandContext>,
    },
    SetLeasedCommand {
        accel: [f64; 3],
        angular_accel: [f64; 3],
        lease_sim_s: f64,
        source_time_s: f64,
        expected_context: CommandContext,
        expected_sensor_revision: u32,
        controller_fence: Option<crate::model::ControlFence>,
    },
    ClaimControl {
        run_id: String,
        controller_id: String,
        expected_epoch: u32,
        expected_term: u32,
        source_time_s: f64,
        lease_sim_s: f64,
        expected_context: CommandContext,
    },
    ReleaseControl {
        run_id: String,
        expected_epoch: u32,
        expected_context: CommandContext,
    },
    SetSensorFault {
        target: crate::model::SensorFaultTarget,
        mode: crate::model::SensorFaultMode,
        duration_sim_s: Option<f64>,
    },
    #[serde(deserialize_with = "crate::model::deserialize_empty_command")]
    ClearSensorFaults,
    #[serde(deserialize_with = "crate::model::deserialize_empty_command")]
    Stop,
    #[serde(deserialize_with = "crate::model::deserialize_empty_command")]
    DebugUpright,
    View {
        mode: Option<CameraMode>,
        focus: Option<crate::domain::EntityId>,
        zoom: Option<f32>,
    },
    Play {
        rate: Option<f64>,
        expected_context: Option<CommandContext>,
    },
    #[serde(deserialize_with = "crate::model::deserialize_empty_command")]
    Pause,
}

pub(crate) enum Dispatch {
    Physics(PhysicsCommand),
    Action(EngineAction),
    View(CameraRequest),
    CheckedCommand {
        accel: [f64; 3],
        angular_accel: [f64; 3],
        expected_context: CommandContext,
    },
    CheckedLease {
        request: crate::model::LeasedCommand,
        expected_context: CommandContext,
        expected_sensor_revision: u32,
    },
    ClaimControl {
        request: crate::model::ControlClaim,
        expected_context: CommandContext,
    },
    ReleaseControl {
        request: crate::model::ControlRelease,
        expected_context: CommandContext,
    },
    CheckedPlay {
        rate: PlaybackRate,
        expected_context: CommandContext,
    },
    CheckedLaunchpad {
        surface_direction: [f64; 3],
        expected_context: CommandContext,
    },
}

impl ApiRequest {
    pub fn into_dispatch(self) -> Result<Dispatch, String> {
        Ok(match self {
            Self::Observe => Dispatch::Physics(PhysicsCommand::Observe),
            Self::Reset { seed } => Dispatch::Physics(PhysicsCommand::Reset { seed }),
            Self::ConfigureExperiment { settings, seed } => {
                settings.validate()?;
                Dispatch::Physics(PhysicsCommand::ConfigureExperiment { settings, seed })
            }
            Self::ConfigureFlight {
                settings,
                seed,
                preset,
            } => {
                settings.validate()?;
                Dispatch::Physics(PhysicsCommand::ConfigureFlight {
                    settings,
                    seed,
                    preset,
                })
            }
            Self::PlaceLander { position_m } => {
                Dispatch::Physics(PhysicsCommand::PlaceLander { position_m })
            }
            Self::MoveLaunchpad {
                surface_direction,
                expected_context,
            } => match expected_context {
                Some(expected_context) => Dispatch::CheckedLaunchpad {
                    surface_direction,
                    expected_context,
                },
                None => Dispatch::Physics(PhysicsCommand::MoveLaunchpad { surface_direction }),
            },
            Self::TuneShip { settings } => Dispatch::Physics(PhysicsCommand::TuneShip { settings }),
            Self::TuneDebrisFlow { settings } => {
                Dispatch::Physics(PhysicsCommand::TuneDebrisFlow { settings })
            }
            Self::Restore { state } => Dispatch::Physics(PhysicsCommand::Restore { state }),
            Self::Step { seconds } => Dispatch::Physics(PhysicsCommand::Step { seconds }),
            Self::SetCommand {
                accel,
                angular_accel,
                expected_context,
            } => match expected_context {
                Some(expected_context) => Dispatch::CheckedCommand {
                    accel,
                    angular_accel,
                    expected_context,
                },
                None => Dispatch::Physics(PhysicsCommand::SetCommand {
                    accel,
                    angular_accel,
                }),
            },
            Self::SetLeasedCommand {
                accel,
                angular_accel,
                lease_sim_s,
                source_time_s,
                expected_context,
                expected_sensor_revision,
                controller_fence,
            } => Dispatch::CheckedLease {
                request: crate::model::LeasedCommand {
                    accel,
                    angular_accel,
                    lease_sim_s,
                    source_time_s,
                    controller_fence,
                },
                expected_context,
                expected_sensor_revision,
            },
            Self::ClaimControl {
                run_id,
                controller_id,
                expected_epoch,
                expected_term,
                source_time_s,
                lease_sim_s,
                expected_context,
            } => Dispatch::ClaimControl {
                request: crate::model::ControlClaim {
                    run_id,
                    controller_id,
                    expected_epoch,
                    expected_term,
                    source_time_s,
                    lease_sim_s,
                },
                expected_context,
            },
            Self::ReleaseControl {
                run_id,
                expected_epoch,
                expected_context,
            } => Dispatch::ReleaseControl {
                request: crate::model::ControlRelease {
                    run_id,
                    expected_epoch,
                },
                expected_context,
            },
            Self::SetSensorFault {
                target,
                mode,
                duration_sim_s,
            } => Dispatch::Physics(PhysicsCommand::SetSensorFault {
                target,
                mode,
                duration_sim_s,
            }),
            Self::ClearSensorFaults => Dispatch::Physics(PhysicsCommand::ClearSensorFaults),
            Self::Stop => Dispatch::Physics(PhysicsCommand::Stop),
            Self::DebugUpright => Dispatch::Physics(PhysicsCommand::DebugUpright),
            Self::View { mode, focus, zoom } => Dispatch::View(CameraRequest { mode, focus, zoom }),
            Self::Play {
                rate,
                expected_context,
            } => {
                let rate = PlaybackRate::try_from(rate.unwrap_or(1.0)).map_err(str::to_owned)?;
                match expected_context {
                    Some(expected_context) => Dispatch::CheckedPlay {
                        rate,
                        expected_context,
                    },
                    None => Dispatch::Action(EngineAction::Play(rate)),
                }
            }
            Self::Pause => Dispatch::Action(EngineAction::Pause),
        })
    }
}

pub(crate) fn execute(json: &str) -> String {
    execute_response(json, CommandResponse::Snapshot)
}

/// Control calls acknowledge the real operation without serializing every body.
pub(crate) fn control(json: &str) -> String {
    execute_response(json, CommandResponse::Acknowledgement)
}

enum CommandResponse {
    Snapshot,
    Acknowledgement,
}

fn execute_response(json: &str, response: CommandResponse) -> String {
    let result = (|| {
        if json.len() > 1024 * 1024 {
            return Err(CommandError::Rejected(
                "Command exceeds the 1 MiB limit".to_owned(),
            ));
        }
        let request: ApiRequest = serde_json::from_str(json)
            .map_err(|error| CommandError::Rejected(error.to_string()))?;
        let dispatch = request.into_dispatch().map_err(CommandError::Rejected)?;
        match dispatch {
            Dispatch::CheckedCommand {
                accel,
                angular_accel,
                expected_context,
            } => crate::session::dispatch_checked(accel, angular_accel, expected_context),
            Dispatch::CheckedLease {
                request,
                expected_context,
                expected_sensor_revision,
            } => crate::session::dispatch_leased_checked(
                request,
                expected_context,
                expected_sensor_revision,
            ),
            Dispatch::CheckedPlay {
                rate,
                expected_context,
            } => crate::session::dispatch_play_checked(rate, expected_context),
            Dispatch::ClaimControl {
                request,
                expected_context,
            } => crate::session::dispatch_claim_control(request, expected_context),
            Dispatch::ReleaseControl {
                request,
                expected_context,
            } => crate::session::dispatch_release_control(request, expected_context),
            Dispatch::CheckedLaunchpad {
                surface_direction,
                expected_context,
            } => crate::session::dispatch_launchpad_checked(surface_direction, expected_context),
            dispatch => crate::session::dispatch(dispatch).map_err(CommandError::Rejected),
        }
    })();
    match result {
        Ok(state) => match response {
            CommandResponse::Snapshot => {
                serde_json::json!({"ok":true,"state":state.as_ref()}).to_string()
            }
            CommandResponse::Acknowledgement => serde_json::json!({"ok":true}).to_string(),
        },
        Err(CommandError::StaleContext) => serde_json::json!({
            "ok":false,"code":"stale_context","error":CommandError::StaleContext.to_string()
        })
        .to_string(),
        Err(CommandError::CommandExpired) => serde_json::json!({
            "ok":false,"code":"command_expired","error":CommandError::CommandExpired.to_string()
        })
        .to_string(),
        Err(CommandError::Rejected(error)) => {
            serde_json::json!({"ok":false,"error":error}).to_string()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{model::ExperimentSettings, physics::Simulation};
    use serde_json::{Value, json};

    #[test]
    fn authority_envelopes_have_exact_required_fields_and_typed_fences() {
        let context = json!({"trial_revision": 0, "tuning_revision": 0,
            "manual_interventions": 0, "seed": 100, "running": false});
        let claim = json!({
            "op": "claim_control", "run_id": "run", "controller_id": "primary",
            "expected_epoch": 0, "expected_term": 0, "source_time_s": 0.0,
            "lease_sim_s": 0.2, "expected_context": context,
        });
        let release = json!({
            "op": "release_control", "run_id": "run", "expected_epoch": 0,
            "expected_context": context,
        });
        assert!(matches!(
            serde_json::from_value::<ApiRequest>(claim.clone())
                .unwrap()
                .into_dispatch()
                .unwrap(),
            Dispatch::ClaimControl { .. }
        ));
        assert!(matches!(
            serde_json::from_value::<ApiRequest>(release.clone())
                .unwrap()
                .into_dispatch()
                .unwrap(),
            Dispatch::ReleaseControl { .. }
        ));
        for request in [&claim, &release] {
            for field in request
                .as_object()
                .unwrap()
                .keys()
                .filter(|field| field.as_str() != "op")
            {
                let mut missing = (*request).clone();
                missing.as_object_mut().unwrap().remove(field);
                assert!(
                    serde_json::from_value::<ApiRequest>(missing).is_err(),
                    "{field}"
                );
            }
            let mut extra = (*request).clone();
            extra["unexpected"] = json!(true);
            assert!(serde_json::from_value::<ApiRequest>(extra).is_err());
        }
        for (field, value) in [
            ("expected_epoch", json!(-1)),
            ("expected_term", json!(4294967296_u64)),
            ("expected_term", json!(0.5)),
        ] {
            let mut invalid = claim.clone();
            invalid[field] = value;
            assert!(serde_json::from_value::<ApiRequest>(invalid).is_err());
        }
        let leased = json!({
            "op": "set_leased_command", "accel": [0.0, 0.0, 0.2], "angular_accel": [0.0, 0.0, 0.0],
            "source_time_s": 0.0, "lease_sim_s": 0.2, "expected_context": context,
            "expected_sensor_revision": 0,
            "controller_fence": {"epoch": 0, "term": 1, "run_id": "run", "controller_id": "primary"},
        });
        let Dispatch::CheckedLease { request, .. } =
            serde_json::from_value::<ApiRequest>(leased.clone())
                .unwrap()
                .into_dispatch()
                .unwrap()
        else {
            panic!("fenced request bypassed checked dispatch");
        };
        assert_eq!(request.controller_fence.unwrap().term, 1);
        for field in ["epoch", "term", "run_id", "controller_id"] {
            let mut invalid = leased.clone();
            invalid["controller_fence"]
                .as_object_mut()
                .unwrap()
                .remove(field);
            assert!(serde_json::from_value::<ApiRequest>(invalid).is_err());
        }
        let mut invalid = leased;
        invalid["controller_fence"]["unexpected"] = json!(true);
        assert!(serde_json::from_value::<ApiRequest>(invalid).is_err());
    }

    #[test]
    fn autonomous_lease_requires_both_contexts_and_sensor_fault_targets_are_typed() {
        let request = json!({
            "op": "set_leased_command", "accel": [0.0, 0.0, 0.2], "angular_accel": [0.0, 0.0, 0.0],
            "lease_sim_s": 0.2, "source_time_s": 0.0, "expected_sensor_revision": 0,
            "expected_context": {"trial_revision": 0, "tuning_revision": 0, "manual_interventions": 0, "seed": 100, "running": false},
        });
        assert!(matches!(
            serde_json::from_value::<ApiRequest>(request.clone())
                .unwrap()
                .into_dispatch()
                .unwrap(),
            Dispatch::CheckedLease { .. }
        ));
        for field in [
            "source_time_s",
            "lease_sim_s",
            "expected_context",
            "expected_sensor_revision",
        ] {
            let mut invalid = request.clone();
            invalid.as_object_mut().unwrap().remove(field);
            assert!(
                serde_json::from_value::<ApiRequest>(invalid).is_err(),
                "{field}"
            );
        }
        for target in [
            "radar_primary",
            "radar_backup",
            "camera_top",
            "camera_bottom",
            "radar_bus",
            "all_avionics",
            "navigation",
        ] {
            let request: ApiRequest = serde_json::from_value(json!({"op": "set_sensor_fault", "target": target, "mode": "unavailable", "duration_sim_s": 1.0})).unwrap();
            assert!(matches!(
                request.into_dispatch().unwrap(),
                Dispatch::Physics(PhysicsCommand::SetSensorFault { .. })
            ));
        }
        for request in [
            json!({"op": "set_sensor_fault", "target": "region", "mode": "unavailable", "duration_sim_s": 1.0}),
            json!({"op": "set_sensor_fault", "target": "radar_primary", "mode": "fake", "duration_sim_s": 1.0}),
            json!({"op": "clear_sensor_faults", "unexpected": true}),
        ] {
            assert!(serde_json::from_value::<ApiRequest>(request).is_err());
        }
    }

    fn physics_requests() -> Vec<Value> {
        let mut settings = ExperimentSettings::default();
        settings.debris.count = 0;
        let simulation = Simulation::with_experiment(17, settings).unwrap();
        let mut archive = serde_json::to_value(simulation.snapshot()).unwrap();
        archive["archive_extension"] = json!({"version": 1});
        archive
            .as_object_mut()
            .unwrap()
            .remove("manual_interventions");
        for asset in archive["assets"].as_array_mut().unwrap() {
            asset["entity_extension"] = json!(true);
            let fields = asset.as_object_mut().unwrap();
            fields.remove("thrust_application");
            fields.remove("torque_nm");
            fields.remove("commanded_accel_mps2");
            fields.remove("commanded_angular_accel_rad_s2");
        }
        vec![
            json!({"op": "observe"}),
            json!({"op": "reset"}),
            json!({"op": "reset", "seed": 42}),
            json!({"op": "reset", "seed": null}),
            json!({"op": "configure_experiment", "settings": settings}),
            json!({"op": "configure_experiment", "settings": settings, "seed": 42}),
            json!({"op": "configure_experiment", "settings": settings, "seed": null}),
            json!({"op": "configure_flight", "settings": settings, "seed": 42, "preset": "mars_facing_l1"}),
            json!({"op": "configure_flight", "settings": settings, "seed": 42, "preset": "far_side_l2"}),
            json!({"op": "place_lander", "position_m": [crate::model::phobos_enclosing_radius_m() + 1500.0, 0.0, 0.0]}),
            json!({"op": "move_launchpad", "surface_direction": [1.0, 0.3, 0.2]}),
            json!({"op": "tune_ship", "settings": {"mass_kg": 4123.0, "inertia_scale": 1.5, "thrust_scale": 1.25}}),
            json!({"op": "tune_debris_flow", "settings": {"speed_mps": 30.0, "speed_spread_mps": 5.0, "stream_axis": "z", "stream_sign": 1}}),
            json!({"op": "restore", "state": archive}),
            json!({"op": "step", "seconds": 0.05}),
            json!({"op": "set_command", "accel": [0.1, 0.2, 0.3], "angular_accel": [0.01, 0.02, 0.03]}),
            json!({"op": "stop"}),
            json!({"op": "debug_upright"}),
        ]
    }

    #[test]
    fn strict_command_envelopes_preserve_valid_requests_and_permissive_archives() {
        assert!(
            serde_json::from_value::<PhysicsCommand>(json!({"op": "reset", "sead": 42})).is_err()
        );
        let mut api_simulation = Simulation::new_default();
        let mut physics_simulation = Simulation::new_default();
        for request in physics_requests() {
            let parsed: ApiRequest = serde_json::from_value(request.clone()).unwrap();
            let Dispatch::Physics(api_command) = parsed.into_dispatch().unwrap() else {
                panic!("physics request dispatched outside physics");
            };
            let command: PhysicsCommand = serde_json::from_value(request.clone()).unwrap();
            assert_eq!(
                serde_json::to_value(api_simulation.execute(api_command).unwrap()).unwrap(),
                serde_json::to_value(physics_simulation.execute(command).unwrap()).unwrap()
            );

            let mut extra = request;
            extra["unexpected"] = json!(true);
            let api_error = serde_json::from_value::<ApiRequest>(extra.clone()).unwrap_err();
            let physics_error = serde_json::from_value::<PhysicsCommand>(extra).unwrap_err();
            assert!(api_error.to_string().contains("unknown field"));
            assert!(physics_error.to_string().contains("unknown field"));
        }
        for request in [
            json!({"op": "view"}),
            json!({"op": "view", "mode": null, "focus": null, "zoom": null}),
            json!({"op": "play"}),
            json!({"op": "play", "rate": null}),
            json!({"op": "pause"}),
        ] {
            let parsed: ApiRequest = serde_json::from_value(request.clone()).unwrap();
            assert!(parsed.into_dispatch().is_ok());
            let mut extra = request;
            extra["unexpected"] = json!(true);
            assert!(serde_json::from_value::<ApiRequest>(extra).is_err());
        }
    }

    #[test]
    #[cfg(not(target_arch = "wasm32"))]
    fn malformed_envelopes_cannot_change_the_real_session_or_enqueue_actions() {
        for command in [
            r#"{"op":"set_command","accel":[0.1,0.2,0.3],"angular_accel":[0.01,0.02,0.03]}"#,
            r#"{"op":"step","seconds":0.05}"#,
        ] {
            let response: Value = serde_json::from_str(&execute(command)).unwrap();
            assert_eq!(response["ok"], true);
        }
        crate::session::request_action(EngineAction::CycleRate);
        crate::session::request_action(EngineAction::Pause);
        let before = crate::session::snapshot();
        let observation = crate::session::observation();
        let runtime = crate::session::runtime_report();
        let queued = crate::session::queued_actions_for_tests();
        let keyboard_requires_release = crate::session::keyboard_requires_release();
        let malformed_fields = vec![
            json!({"op": "move_launchpad", "position_m": [1, 0, 0]}),
            json!({"op": "move_launchpad", "surface_direction": [1, 0, 0], "reset": true}),
            json!({"op": "reset", "sead": 42}),
            json!({"op": "view", "unexpected": true}),
            json!({"op": "play", "unexpected": true}),
            json!({"op": "pause", "unexpected": true}),
        ];
        let mut malformed: Vec<_> = malformed_fields
            .into_iter()
            .map(|request| (request, "unknown field"))
            .collect();
        malformed.push((
            json!({"op": "move_launchpad", "surface_direction": [1, 0]}),
            "invalid length",
        ));
        for mut request in physics_requests() {
            request["unexpected"] = json!(true);
            malformed.push((request, "unknown field"));
        }
        for (request, reason) in malformed {
            let response: Value = serde_json::from_str(&execute(&request.to_string())).unwrap();
            assert_eq!(response["ok"], false);
            assert!(response["error"].as_str().unwrap().contains(reason));
            assert!(std::rc::Rc::ptr_eq(&crate::session::snapshot(), &before));
            assert_eq!(crate::session::observation(), observation);
            assert_eq!(crate::session::runtime_report(), runtime);
            assert_eq!(crate::session::queued_actions_for_tests(), queued);
            assert_eq!(
                crate::session::keyboard_requires_release(),
                keyboard_requires_release
            );
        }
        let observed =
            crate::session::dispatch(Dispatch::Physics(PhysicsCommand::Observe)).unwrap();
        assert_eq!(
            serde_json::to_value(observed.as_ref()).unwrap(),
            serde_json::to_value(before.as_ref()).unwrap()
        );
    }

    #[test]
    fn debug_upright_is_a_distinct_local_physics_request_not_a_control_policy() {
        let request: ApiRequest = serde_json::from_str(r#"{"op":"debug_upright"}"#).unwrap();
        assert!(matches!(
            request.into_dispatch().unwrap(),
            Dispatch::Physics(PhysicsCommand::DebugUpright)
        ));
        let command: PhysicsCommand = serde_json::from_str(r#"{"op":"debug_upright"}"#).unwrap();
        assert!(matches!(command, PhysicsCommand::DebugUpright));
        for unsupported in ["autopilot", "boost", "upright", "set_autopilot"] {
            assert!(
                serde_json::from_value::<ApiRequest>(serde_json::json!({"op": unsupported}))
                    .is_err()
            );
        }
    }

    #[test]
    fn acknowledgement_path_does_not_serialize_a_debug_world_or_change_it() {
        let before = crate::session::snapshot();
        let response = control(r#"{"op":"observe"}"#);
        assert_eq!(
            serde_json::from_str::<Value>(&response).unwrap(),
            json!({"ok": true})
        );
        assert!(response.len() < 32);
        assert_eq!(
            serde_json::to_value(before.as_ref()).unwrap(),
            serde_json::to_value(crate::session::snapshot().as_ref()).unwrap()
        );
    }

    #[test]
    fn stale_context_has_a_structured_rejection_and_preserves_actual_session_state() {
        let before = crate::session::snapshot();
        let runtime = crate::session::runtime_report();
        let queued = crate::session::queued_actions_for_tests();
        let current_revision = crate::session::trial_revision();
        let expected_revision = if current_revision == u32::MAX {
            0
        } else {
            current_revision + 1
        };
        let request = json!({
            "op": "set_command", "accel": [0.1, 0.0, 0.0], "angular_accel": [0.0, 0.0, 0.0],
            "expected_context": {
                "trial_revision": expected_revision, "tuning_revision": before.tuning_revision,
                "manual_interventions": before.manual_interventions, "seed": before.seed,
                "running": crate::session::is_running(),
            },
        });
        let response: Value = serde_json::from_str(&control(&request.to_string())).unwrap();
        assert_eq!(response["ok"], false);
        assert_eq!(response["code"], "stale_context");
        assert!(response.get("state").is_none());
        assert!(std::rc::Rc::ptr_eq(&before, &crate::session::snapshot()));
        assert_eq!(crate::session::runtime_report(), runtime);
        assert_eq!(crate::session::queued_actions_for_tests(), queued);
    }

    #[test]
    fn checked_context_envelopes_are_strict_numeric_data_not_a_command_bypass() {
        let request = json!({
            "op": "set_command", "accel": [0.1, 0.0, 0.0], "angular_accel": [0.0, 0.0, 0.0],
            "expected_context": {"trial_revision": 0, "tuning_revision": 0,
                "manual_interventions": 0, "seed": 100, "running": false},
        });
        let parsed: ApiRequest = serde_json::from_value(request.clone()).unwrap();
        assert!(matches!(
            parsed.into_dispatch().unwrap(),
            Dispatch::CheckedCommand { .. }
        ));
        for (field, value) in [
            ("trial_revision", json!(-1)),
            ("tuning_revision", json!(0.5)),
            ("manual_interventions", json!(4294967296_u64)),
            ("running", json!(1)),
            ("unexpected", json!(true)),
        ] {
            let mut invalid = request.clone();
            invalid["expected_context"][field] = value;
            assert!(
                serde_json::from_value::<ApiRequest>(invalid).is_err(),
                "{field}"
            );
        }
    }
}
