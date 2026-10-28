use super::data::{
    BodyPose, CollectionClock, CollectionStage, LocalTransform, NativeLander, PositionDifferences,
    PresentationSample, PropagatedGlobalTransform, PublishedSnapshot, SceneSnapshotClock,
    SimulationTimeBasis, VisualObservation, VisualTransforms,
};
use crate::{
    domain::{EntityId, EntityKind},
    scene::AssetVisual,
};
use bevy::{diagnostic::FrameCount, prelude::*, transform::TransformSystems};

// The scene stores render translations in kilometres. Native poses use metres.
const METRES_PER_RENDER_UNIT: f64 = 1_000.0;

// NonSend pins collection to the same main thread as the session and export cache.
struct MainWorldDiagnostics;

impl Drop for MainWorldDiagnostics {
    fn drop(&mut self) {
        super::scene_unavailable();
    }
}

/// Install only in app::start, never in the headless scheduler.
pub(crate) fn install(app: &mut App) {
    app.world_mut().insert_non_send(MainWorldDiagnostics);
    app.add_systems(PostUpdate, collect.after(TransformSystems::Propagate));
}

type VisualComponents = (
    &'static AssetVisual,
    Option<&'static Transform>,
    Option<&'static GlobalTransform>,
    Has<ChildOf>,
);

fn collect(
    _main_thread: NonSend<MainWorldDiagnostics>,
    visuals: Query<VisualComponents>,
    frame: Option<Res<FrameCount>>,
    real: Option<Res<Time<Real>>>,
) {
    // Do not iterate, read the snapshot/native world, or allocate while disabled.
    if !super::enabled() {
        return;
    }

    let mut lander_visual = VisualObservation::Missing;
    let mut landing_pad_visual = VisualObservation::Missing;
    for (visual, local, global, has_parent) in &visuals {
        let target = match (visual.id, visual.kind) {
            (EntityId::Lander, EntityKind::Lander) => &mut lander_visual,
            (EntityId::LandingPad, EntityKind::LandingPad) => &mut landing_pad_visual,
            _ => continue,
        };
        retain_visual(target, local, global, has_parent);
    }
    if !has_transform(&lander_visual) && !has_transform(&landing_pad_visual) {
        super::scene_unavailable();
        return;
    }

    // Rc clone of the actual current publication, not a new full snapshot/JSON.
    let snapshot = crate::snapshot();
    let published_snapshot = PublishedSnapshot {
        source_time_s: snapshot.time_s,
        source_seed: snapshot.seed,
        trial_revision: crate::session::trial_revision(),
        tuning_revision: snapshot.tuning_revision,
        frame: snapshot.frame.clone(),
        lander: published_pose(&snapshot, EntityId::Lander, EntityKind::Lander),
        landing_pad: published_pose(&snapshot, EntityId::LandingPad, EntityKind::LandingPad),
    };
    let native_lander = match crate::session::applied_physics() {
        Ok(actual) => NativeLander::Observed {
            source_time_s: actual.source_time_s,
            tuning_revision: actual.tuning_revision,
            frame: actual.frame,
            pose: BodyPose {
                position_world_m: actual.position_world_m,
                rotation_body_to_world_xyzw: actual.rotation_body_to_world_xyzw,
                velocity_world_mps: actual.velocity_world_mps,
            },
        },
        Err(error) => NativeLander::Unavailable {
            error: error.chars().take(180).collect(),
        },
    };
    let native_position = match &native_lander {
        NativeLander::Observed { pose, .. } => Some(pose.position_world_m),
        NativeLander::Unavailable { .. } => None,
    };
    let published_lander_position = published_snapshot
        .lander
        .as_ref()
        .map(|pose| pose.position_world_m);
    let position_differences = PositionDifferences {
        lander_global_minus_published_m: difference(
            global_position(&lander_visual),
            published_lander_position,
        ),
        landing_pad_global_minus_published_m: difference(
            global_position(&landing_pad_visual),
            published_snapshot
                .landing_pad
                .as_ref()
                .map(|pose| pose.position_world_m),
        ),
        lander_global_minus_native_m: difference(global_position(&lander_visual), native_position),
        lander_native_minus_published_m: difference(native_position, published_lander_position),
    };
    super::publish(PresentationSample {
        collection: CollectionClock {
            stage: CollectionStage::PostUpdateAfterTransformPropagation,
            bevy_frame: frame.map(|frame| frame.0),
            real_elapsed_s: real.map(|time| time.elapsed_secs_f64()),
        },
        simulation_time_basis: SimulationTimeBasis::SimulationElapsedSeconds,
        // AssetVisual exposes no authoritative scene-sync timestamp. Never label
        // the separately read current snapshot clock as the visual's source time.
        scene_snapshot_clock: SceneSnapshotClock::NotExposedBySceneApi,
        scene_snapshot_source_time_s: None,
        published_snapshot,
        native_lander,
        lander_visual,
        landing_pad_visual,
        position_differences,
    });
}

fn retain_visual(
    target: &mut VisualObservation,
    local: Option<&Transform>,
    global: Option<&GlobalTransform>,
    has_parent: bool,
) {
    *target = match target {
        VisualObservation::Missing => VisualObservation::Observed {
            transforms: VisualTransforms {
                has_parent,
                local_transform: local.map(|transform| LocalTransform {
                    translation_parent_m: metres(transform.translation),
                    rotation_parent_from_local_xyzw: transform.rotation.to_array(),
                    scale: transform.scale.to_array(),
                }),
                global_transform: global.map(|transform| PropagatedGlobalTransform {
                    translation_world_m: metres(transform.translation()),
                    linear_world_from_local_columns: transform.affine().matrix3.to_cols_array_2d(),
                }),
            },
        },
        VisualObservation::Observed { .. } => VisualObservation::Ambiguous {
            matching_entities: 2,
        },
        VisualObservation::Ambiguous { matching_entities } => VisualObservation::Ambiguous {
            matching_entities: matching_entities.saturating_add(1),
        },
    };
}

fn has_transform(visual: &VisualObservation) -> bool {
    match visual {
        VisualObservation::Observed { transforms } => {
            transforms.local_transform.is_some() || transforms.global_transform.is_some()
        }
        VisualObservation::Missing | VisualObservation::Ambiguous { .. } => false,
    }
}

fn global_position(visual: &VisualObservation) -> Option<[f64; 3]> {
    match visual {
        VisualObservation::Observed { transforms } => transforms
            .global_transform
            .as_ref()
            .map(|transform| transform.translation_world_m),
        VisualObservation::Missing | VisualObservation::Ambiguous { .. } => None,
    }
}

fn published_pose(
    snapshot: &crate::model::Snapshot,
    id: EntityId,
    kind: EntityKind,
) -> Option<BodyPose> {
    snapshot
        .assets
        .iter()
        .find(|asset| asset.id == id && asset.kind == kind)
        .map(|asset| BodyPose {
            position_world_m: asset.position,
            rotation_body_to_world_xyzw: asset.rotation,
            velocity_world_mps: asset.velocity,
        })
}

fn metres(translation: Vec3) -> [f64; 3] {
    translation
        .to_array()
        .map(|value| f64::from(value) * METRES_PER_RENDER_UNIT)
}

fn difference(left: Option<[f64; 3]>, right: Option<[f64; 3]>) -> Option<[f64; 3]> {
    left.zip(right)
        .map(|(left, right)| std::array::from_fn(|axis| left[axis] - right[axis]))
}
