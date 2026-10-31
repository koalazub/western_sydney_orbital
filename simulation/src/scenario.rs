//! Typed, validated configuration loaded from the normative scenario JSON.
mod model;
mod validation;

pub use model::{
    CameraMount, CameraSource, CameraSourceStatus, Contacts, Crater, Debris,
    DebrisTrajectoryAdjustmentConstants, DebugThrust, FieldOfViewConvention, Gravity, Integration,
    Lander, Lighting, Limits, Mission, OfficialCloudSettings, OfficialDebrisClass,
    OfficialDebrisClassId, OfficialRadarSettings, Pad, Practice, RockSurface, Scenario, Sensors,
    ShipCameras, Terrain, TransitStreamSettings,
};

use std::sync::OnceLock;

pub fn settings() -> &'static Scenario {
    static SETTINGS: OnceLock<Scenario> = OnceLock::new();
    SETTINGS.get_or_init(|| {
        let scenario: Scenario =
            serde_json::from_str(include_str!("../scenarios/phobos_escape.json"))
                .expect("checked-in scenario JSON must deserialize into its typed model");
        validation::validate(&scenario)
            .expect("checked-in scenario JSON must satisfy its named validation rules");
        scenario
    })
}
