use crate::domain::{EntityId, EntityKind};
use bevy::prelude::*;
use std::collections::{HashMap, VecDeque};

#[derive(Component)]
pub(crate) struct AssetVisual {
    pub(crate) id: EntityId,
    pub(crate) kind: EntityKind,
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
pub(crate) struct SceneState {
    pub(crate) seed: u64,
    pub(crate) time_s: f64,
    pub(crate) last_positions: HashMap<EntityId, Vec3>,
    pub(crate) trails: HashMap<EntityId, VecDeque<Vec3>>,
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
