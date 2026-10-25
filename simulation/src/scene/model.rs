use crate::domain::{CameraMode, EntityId, EntityKind, FlightPreset};
use bevy::prelude::*;
use bevy::render::{RenderApp, batching::NoAutomaticBatching, extract_component::ExtractComponent};
use std::{
    collections::{HashMap, VecDeque},
    rc::Rc,
};

#[derive(Component)]
#[component(
    immutable,
    on_insert = invalidate_scene_entities,
    on_remove = invalidate_scene_entities
)]
pub(crate) struct AssetVisual {
    pub(crate) id: EntityId,
    pub(crate) kind: EntityKind,
}

#[derive(Component)]
#[component(
    immutable,
    on_insert = invalidate_scene_entities,
    on_remove = invalidate_scene_entities
)]
pub(crate) enum CelestialSphere {
    Mars,
    Sun,
}

// Readiness must identify each successful surface draw, not a multidraw's
// representative. Only the two planetary meshes opt out of automatic batching.
#[derive(Component, Clone, Copy, ExtractComponent)]
#[extract_app(RenderApp)]
#[require(NoAutomaticBatching)]
pub(crate) enum PlanetarySurface {
    Phobos,
    Mars,
}

impl PlanetarySurface {
    pub(crate) const ALL_DRAW_BITS: u32 = Self::Phobos.draw_bit() | Self::Mars.draw_bit();

    pub(crate) const fn draw_bit(self) -> u32 {
        match self {
            Self::Phobos => 1,
            Self::Mars => 2,
        }
    }
}

#[derive(Resource)]
pub(crate) struct SceneAssets {
    pub(crate) lander: Handle<Mesh>,
    pub(crate) target: Handle<Mesh>,
    pub(crate) pad: Handle<Mesh>,
    pub(crate) rocks: [Handle<Mesh>; 4],
    pub(crate) lander_material: Handle<StandardMaterial>,
    pub(crate) target_material: Handle<StandardMaterial>,
    pub(crate) pad_material: Handle<StandardMaterial>,
    pub(crate) rock_material: Handle<StandardMaterial>,
}

#[derive(Resource, Default)]
pub(crate) struct SceneEntityChanges {
    pub(crate) dirty: bool,
}

fn invalidate_scene_entities(
    mut world: bevy::ecs::world::DeferredWorld,
    _context: bevy::ecs::lifecycle::HookContext,
) {
    if let Some(mut changes) = world.get_resource_mut::<SceneEntityChanges>() {
        changes.dirty = true;
    }
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct NearbyTrail {
    pub(crate) index: usize,
    pub(crate) id: EntityId,
    pub(crate) distance_squared_m2: f64,
}

// A NonSend resource keeps the published Rc alive. A bare pointer address can be
// reused after a restore and would miss a same-clock pose or preset replacement.
#[derive(Default)]
pub(crate) struct SceneState {
    pub(crate) seed: u64,
    pub(crate) time_s: f64,
    pub(crate) flight_preset: FlightPreset,
    pub(crate) manual_interventions: u32,
    pub(crate) experiment_settings: Option<crate::model::ExperimentSettings>,
    pub(crate) trails: HashMap<EntityId, VecDeque<Vec3>>,
    pub(crate) snapshot: Option<Rc<crate::model::Snapshot>>,
    pub(crate) asset_indices: HashMap<EntityId, usize>,
    pub(crate) visual_entities: HashMap<EntityId, Entity>,
    pub(crate) spacecraft_indices: Vec<usize>,
    pub(crate) trail_indices: Vec<usize>,
    pub(crate) nearby_trails: Vec<NearbyTrail>,
    pub(crate) viewer: Option<(CameraMode, EntityId)>,
}

#[derive(Clone, Copy)]
pub(crate) struct RenderScale;

impl RenderScale {
    pub(crate) const METRES_PER_KILOMETRE: f64 = 1_000.0;

    pub(crate) fn metres(value: f64) -> f32 {
        (value / Self::METRES_PER_KILOMETRE) as f32
    }

    pub(crate) fn position(value: [f64; 3]) -> Vec3 {
        Vec3::new(
            Self::metres(value[0]),
            Self::metres(value[1]),
            Self::metres(value[2]),
        )
    }
}
