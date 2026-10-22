//! Browser JSON boundary. Requests become typed once, then dispatch by variant.
use crate::{
    domain::{CameraMode, CameraRequest, EngineAction, PlaybackRate},
    model::Snapshot,
    physics::PhysicsCommand,
};
use serde::Deserialize;

#[derive(Debug, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub(crate) enum ApiRequest {
    Observe,
    Reset {
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
    Stop,
    View {
        mode: Option<CameraMode>,
        focus: Option<crate::domain::EntityId>,
        zoom: Option<f32>,
    },
    Play {
        rate: Option<f64>,
    },
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
