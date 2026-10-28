//! Bounded diagnostic data. No render-device, sensor or full-world payloads.
use serde::Serialize;

#[derive(Default)]
pub(super) enum Diagnostics {
    #[default]
    Disabled,
    WaitingForScene,
    Observed(Box<PresentationSample>),
}

#[derive(Serialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum State {
    Disabled,
    WaitingForScene,
    Observed,
}

#[derive(Serialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum Measurement {
    BevyMainWorldTransformsNotGpuPixels,
}

#[derive(Serialize)]
pub(super) struct Report<'a> {
    pub schema: &'static str,
    pub enabled: bool,
    /// At least one unique own-asset transform, not renderer/GPU availability.
    pub available: bool,
    pub state: State,
    pub measurement: Measurement,
    pub sample: Option<&'a PresentationSample>,
}

#[derive(Serialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum CollectionStage {
    PostUpdateAfterTransformPropagation,
}

#[derive(Serialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum SimulationTimeBasis {
    SimulationElapsedSeconds,
}

#[derive(Serialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum SceneSnapshotClock {
    SceneCacheSnapshot,
    MissingSceneCache,
}

#[derive(Serialize)]
pub(super) struct CollectionClock {
    pub stage: CollectionStage,
    /// Main-app update index from Bevy's FrameCount, not a GPU frame count.
    pub bevy_frame: Option<u32>,
    /// Bevy Time<Real> at collection, not simulation time or capture time.
    pub real_elapsed_s: Option<f64>,
}

#[derive(Serialize)]
pub(super) struct BodyPose {
    pub position_world_m: [f64; 3],
    pub rotation_body_to_world_xyzw: [f64; 4],
    pub velocity_world_mps: [f64; 3],
}

#[derive(Serialize)]
pub(super) struct PublishedSnapshot {
    pub source_time_s: f64,
    pub source_seed: u64,
    pub trial_revision: u32,
    pub tuning_revision: u32,
    pub frame: String,
    pub lander: Option<BodyPose>,
    pub landing_pad: Option<BodyPose>,
}

#[derive(Serialize)]
pub(super) struct SceneSnapshot {
    pub source_time_s: f64,
    pub source_seed: u64,
    pub tuning_revision: u32,
    pub frame: String,
    pub lander: Option<BodyPose>,
    pub landing_pad: Option<BodyPose>,
}

#[derive(Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub(super) enum NativeLander {
    Observed {
        source_time_s: f64,
        tuning_revision: u32,
        frame: String,
        pose: BodyPose,
    },
    Unavailable {
        error: String,
    },
}

#[derive(Serialize)]
pub(super) struct LocalTransform {
    pub translation_parent_m: [f64; 3],
    pub rotation_parent_from_local_xyzw: [f32; 4],
    pub scale: [f32; 3],
}

#[derive(Serialize)]
pub(super) struct PropagatedGlobalTransform {
    pub translation_world_m: [f64; 3],
    /// The actual affine linear part, including scale/shear, without decomposition.
    pub linear_world_from_local_columns: [[f32; 3]; 3],
}

#[derive(Serialize)]
pub(super) struct VisualTransforms {
    pub has_parent: bool,
    pub local_transform: Option<LocalTransform>,
    pub global_transform: Option<PropagatedGlobalTransform>,
}

#[derive(Default, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub(super) enum VisualObservation {
    #[default]
    Missing,
    Observed {
        transforms: VisualTransforms,
    },
    /// Never choose an arbitrary entity when the scene has duplicate own IDs.
    Ambiguous {
        matching_entities: u32,
    },
}

#[derive(Serialize)]
pub(super) struct PositionDifferences {
    /// Raw differences, not a tolerance verdict or an inference about GPU pixels.
    pub lander_global_minus_published_m: Option<[f64; 3]>,
    pub landing_pad_global_minus_published_m: Option<[f64; 3]>,
    pub lander_global_minus_native_m: Option<[f64; 3]>,
    pub lander_native_minus_published_m: Option<[f64; 3]>,
}

#[derive(Serialize)]
pub(super) struct PresentationSample {
    pub collection: CollectionClock,
    pub simulation_time_basis: SimulationTimeBasis,
    /// Clock of the publication actually consumed by scene sync, not a current
    /// session read relabelled as the scene's source time.
    pub scene_snapshot_clock: SceneSnapshotClock,
    pub scene_snapshot_source_time_s: Option<f64>,
    pub scene_snapshot: Option<SceneSnapshot>,
    pub published_snapshot: PublishedSnapshot,
    pub native_lander: NativeLander,
    pub lander_visual: VisualObservation,
    pub landing_pad_visual: VisualObservation,
    pub position_differences: PositionDifferences,
}
