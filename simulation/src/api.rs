//! Browser JSON boundary. Requests become typed once, then dispatch by variant.
use crate::{
    domain::{CameraMode, CameraRequest, EngineAction, PlaybackRate},
    model::Snapshot,
    physics::PhysicsCommand,
};
use serde::Deserialize;

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
    Restore {
        state: Box<Snapshot>,
    },
    Step {
        seconds: f64,
    },
    SetCommand {
        accel: [f64; 3],
        angular_accel: [f64; 3],
    },
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
    },
    #[serde(deserialize_with = "crate::model::deserialize_empty_command")]
    Pause,
}

pub(crate) enum Dispatch {
    Physics(PhysicsCommand),
    Action(EngineAction),
    View(CameraRequest),
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
            Self::Restore { state } => Dispatch::Physics(PhysicsCommand::Restore { state }),
            Self::Step { seconds } => Dispatch::Physics(PhysicsCommand::Step { seconds }),
            Self::SetCommand {
                accel,
                angular_accel,
            } => Dispatch::Physics(PhysicsCommand::SetCommand {
                accel,
                angular_accel,
            }),
            Self::Stop => Dispatch::Physics(PhysicsCommand::Stop),
            Self::DebugUpright => Dispatch::Physics(PhysicsCommand::DebugUpright),
            Self::View { mode, focus, zoom } => Dispatch::View(CameraRequest { mode, focus, zoom }),
            Self::Play { rate } => Dispatch::Action(EngineAction::Play(
                PlaybackRate::try_from(rate.unwrap_or(1.0)).map_err(str::to_owned)?,
            )),
            Self::Pause => Dispatch::Action(EngineAction::Pause),
        })
    }
}

pub(crate) fn execute(json: &str) -> String {
    let result = (|| {
        if json.len() > 1024 * 1024 {
            return Err("Command exceeds the 1 MiB limit".to_owned());
        }
        let request: ApiRequest = serde_json::from_str(json).map_err(|error| error.to_string())?;
        crate::session::dispatch(request.into_dispatch()?)
    })();
    match result {
        Ok(state) => serde_json::json!({"ok":true,"state":state.as_ref()}).to_string(),
        Err(error) => serde_json::json!({"ok":false,"error":error}).to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{model::ExperimentSettings, physics::Simulation};
    use serde_json::{Value, json};

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
        let mut malformed = vec![
            json!({"op": "reset", "sead": 42}),
            json!({"op": "view", "unexpected": true}),
            json!({"op": "play", "unexpected": true}),
            json!({"op": "pause", "unexpected": true}),
        ];
        for mut request in physics_requests() {
            request["unexpected"] = json!(true);
            malformed.push(request);
        }
        for request in malformed {
            let response: Value = serde_json::from_str(&execute(&request.to_string())).unwrap();
            assert_eq!(response["ok"], false);
            assert!(
                response["error"]
                    .as_str()
                    .unwrap()
                    .contains("unknown field")
            );
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
}
