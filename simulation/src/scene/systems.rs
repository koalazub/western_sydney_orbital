use super::{
    meshes,
    model::{
        AssetVisual, CelestialSphere, NearbyTrail, PlanetarySurface, RenderScale, SceneAssets,
        SceneEntityChanges, SceneState,
    },
    presentation::{MainCameraPresentation, VisualSettings},
    textures,
};
use crate::{
    camera::{CameraRig, SimulationCamera},
    domain::{CameraMode, EntityId, EntityKind},
};
use bevy::{
    camera::visibility::RenderLayers,
    ecs::system::SystemParam,
    light::{GlobalAmbientLight, NotShadowCaster},
    prelude::*,
};
use std::{cmp::Ordering, rc::Rc};

const TRAIL_LIMIT: usize = 256;
const NEARBY_TRAIL_COUNT: usize = 8;

pub fn setup(
    mut commands: Commands,
    asset_server: Res<AssetServer>,
    mut meshes_assets: ResMut<Assets<Mesh>>,
    mut materials: ResMut<Assets<StandardMaterial>>,
    mut rig: ResMut<CameraRig>,
    visuals: Res<VisualSettings>,
    windows: Query<&Window>,
) {
    let snapshot = crate::snapshot();
    let lighting = crate::scenario::settings().lighting;
    let direction_to_sun = Vec3::from_array(lighting.direction_to_sun).normalize();
    commands.insert_resource(GlobalAmbientLight::NONE);
    commands.insert_resource(SceneEntityChanges::default());
    let seed = snapshot.seed;
    let time_s = snapshot.time_s;
    let flight_preset = snapshot.flight_preset;
    let manual_interventions = snapshot.manual_interventions;
    let experiment_settings = snapshot.experiment_settings;
    commands.queue(move |world: &mut World| {
        world.insert_non_send(SceneState {
            seed,
            time_s,
            flight_preset,
            manual_interventions,
            experiment_settings,
            ..default()
        });
    });
    commands.spawn((
        Mesh3d(meshes_assets.add(meshes::phobos())),
        MeshMaterial3d(materials.add(textures::phobos_material(&asset_server))),
        PlanetarySurface::Phobos,
        Transform::default(),
        RenderLayers::layer(0),
        Visibility::Visible,
    ));
    let lander_position = snapshot
        .assets
        .iter()
        .find(|asset| asset.id == EntityId::Lander)
        .map(|asset| RenderScale::position(asset.position))
        .unwrap_or(Vec3::ZERO);
    let (main_transform, main_projection) =
        crate::camera::initial_overview(&mut rig, &snapshot, windows.single().ok());
    commands.spawn((
        Camera3d::default(),
        main_projection,
        MainCameraPresentation::new(*visuals, textures::starfield_image(&asset_server)),
        main_transform,
        SimulationCamera,
        IsDefaultUiCamera,
        RenderLayers::from_layers(&[0, 1]),
    ));
    commands.spawn((
        Mesh3d(meshes_assets.add(meshes::mars(RenderScale::metres(
            snapshot.environment.mars_radius_m,
        )))),
        MeshMaterial3d(materials.add(textures::mars_material(&asset_server))),
        Transform::from_translation(RenderScale::position(snapshot.environment.mars_position_m)),
        CelestialSphere::Mars,
        PlanetarySurface::Mars,
        RenderLayers::layer(0),
        Visibility::Visible,
    ));
    // This proxy represents a direction on the background, not the Sun's real position.
    // Following the lander keeps its angular radius stable in both mounted cameras.
    let sun_radius_m =
        lighting.sun_proxy_distance_m * f64::from(lighting.sun_angular_radius_rad).sin();
    commands.spawn((
        Mesh3d(
            meshes_assets.add(meshes::untextured(
                Sphere::new(RenderScale::metres(sun_radius_m))
                    .mesh()
                    .uv(48, 32),
            )),
        ),
        MeshMaterial3d(materials.add(StandardMaterial {
            base_color: Color::WHITE,
            unlit: true,
            ..default()
        })),
        Transform::from_translation(
            lander_position + direction_to_sun * RenderScale::metres(lighting.sun_proxy_distance_m),
        ),
        CelestialSphere::Sun,
        NotShadowCaster,
        RenderLayers::layer(0),
        Visibility::Visible,
    ));
    commands.spawn((
        DirectionalLight {
            illuminance: lighting.illuminance_lux,
            ..default()
        },
        // Bevy emits rays along local -Z, away from the configured solar direction.
        Transform::default().looking_to(-direction_to_sun, Vec3::Y),
        RenderLayers::from_layers(&[0, 2]),
    ));
    let assets = SceneAssets {
        lander: meshes_assets.add(meshes::lander()),
        target: meshes_assets.add(meshes::target()),
        pad: meshes_assets.add(meshes::pad()),
        rocks: std::array::from_fn(|i| meshes_assets.add(meshes::asteroid(i as u8 + 1))),
        lander_material: materials.add(StandardMaterial {
            base_color: Color::srgb(0.8, 0.59, 0.24),
            metallic: 0.25,
            perceptual_roughness: 0.6,
            ..default()
        }),
        target_material: materials.add(StandardMaterial {
            base_color: Color::srgb(0.27, 0.62, 0.81),
            metallic: 0.3,
            perceptual_roughness: 0.6,
            ..default()
        }),
        pad_material: materials.add(StandardMaterial {
            base_color: Color::srgb(0.13, 0.18, 0.21),
            perceptual_roughness: 0.9,
            ..default()
        }),
        rock_material: materials.add(rock_material(lighting.debris_surface)),
    };
    for asset in snapshot
        .assets
        .iter()
        .filter(|asset| asset.id != EntityId::Phobos && asset.id != EntityId::Mars)
    {
        spawn_asset(&mut commands, &assets, asset, RenderLayers::layer(0));
    }
    commands.insert_resource(assets);
}

#[derive(SystemParam)]
pub struct ViewerContext<'w> {
    rig: Res<'w, CameraRig>,
    presentation: Res<'w, VisualSettings>,
}

#[derive(SystemParam)]
pub struct SceneSynchronization<'w> {
    state: NonSendMut<'w, SceneState>,
    entity_changes: ResMut<'w, SceneEntityChanges>,
}

pub fn sync_scene(
    mut commands: Commands,
    assets: Res<SceneAssets>,
    mut visuals: Query<
        (
            Entity,
            &AssetVisual,
            &mut Transform,
            &mut Visibility,
            &mut RenderLayers,
        ),
        Without<SimulationCamera>,
    >,
    mut celestials: Query<(&CelestialSphere, &mut Transform), Without<AssetVisual>>,
    synchronization: SceneSynchronization,
    mut gizmos: Gizmos,
    viewer: ViewerContext,
) {
    let SceneSynchronization {
        mut state,
        mut entity_changes,
    } = synchronization;
    let ViewerContext { rig, presentation } = viewer;
    let snapshot = crate::snapshot();
    let snapshot_changed = is_new_snapshot(&state, &snapshot);
    let entities_changed = entity_changes.dirty;
    entity_changes.dirty = false;
    if snapshot_changed {
        if snapshot.seed != state.seed
            || snapshot.time_s < state.time_s
            || snapshot.flight_preset != state.flight_preset
            || snapshot.manual_interventions != state.manual_interventions
            || snapshot.experiment_settings != state.experiment_settings
        {
            state.trails.clear();
        }
        refresh_snapshot_cache(&mut state, &snapshot);
        update_trails(&mut state, &snapshot);
        state.seed = snapshot.seed;
        state.time_s = snapshot.time_s;
        state.flight_preset = snapshot.flight_preset;
        state.manual_interventions = snapshot.manual_interventions;
        state.experiment_settings = snapshot.experiment_settings;
        state.snapshot = Some(Rc::clone(&snapshot));
    }
    if snapshot_changed || entities_changed {
        let lighting = crate::scenario::settings().lighting;
        let lander_position = state
            .asset_indices
            .get(&EntityId::Lander)
            .map(|index| RenderScale::position(snapshot.assets[*index].position))
            .unwrap_or(Vec3::ZERO);
        for (sphere, transform) in &mut celestials {
            let translation = match sphere {
                CelestialSphere::Mars => {
                    RenderScale::position(snapshot.environment.mars_position_m)
                }
                CelestialSphere::Sun => {
                    lander_position
                        + Vec3::from_array(lighting.direction_to_sun).normalize()
                            * RenderScale::metres(lighting.sun_proxy_distance_m)
                }
            };
            transform
                .map_unchanged(|transform| &mut transform.translation)
                .set_if_neq(translation);
        }
        state.visual_entities.clear();
        for (entity, tag, mut transform, mut visibility, mut layers) in &mut visuals {
            let asset = state
                .asset_indices
                .get(&tag.id)
                .map(|index| &snapshot.assets[*index])
                .filter(|asset| asset.kind == tag.kind);
            if let Some(asset) = asset {
                state.visual_entities.insert(tag.id, entity);
                transform.set_if_neq(asset_transform(asset));
                visibility.set_if_neq(Visibility::Visible);
                layers.set_if_neq(asset_render_layers(&rig, asset.id));
            } else {
                commands.entity(entity).despawn();
            }
        }
        for asset in snapshot
            .assets
            .iter()
            .filter(|asset| asset.id != EntityId::Phobos && asset.id != EntityId::Mars)
        {
            state.visual_entities.entry(asset.id).or_insert_with(|| {
                spawn_asset(
                    &mut commands,
                    &assets,
                    asset,
                    asset_render_layers(&rig, asset.id),
                )
            });
        }
    } else if state.viewer != Some((rig.mode, rig.focus)) {
        // Only the old and new subjects can change debug first-person layers.
        // Moving the camera while paused never rewrites all physical transforms.
        let previous_focus = state.viewer.map(|(_, focus)| focus);
        for id in previous_focus.into_iter().chain(std::iter::once(rig.focus)) {
            if let Some(entity) = state.visual_entities.get(&id)
                && let Ok((_, _, _, _, mut layers)) = visuals.get_mut(*entity)
            {
                layers.set_if_neq(asset_render_layers(&rig, id));
            }
        }
    }
    state.viewer = Some((rig.mode, rig.focus));
    // Keep real trail history while hidden; presentation never changes motion.
    if !presentation.guides || rig.mode.sensor().is_some() {
        return;
    }
    for (id, path) in &state.trails {
        let color = if *id == EntityId::Lander {
            Color::srgb(0.98, 0.66, 0.28)
        } else {
            Color::srgb(0.53, 0.68, 0.74)
        };
        let mut points = path.iter().copied();
        if let Some(mut previous) = points.next() {
            for next in points {
                gizmos.line(previous, next, color);
                previous = next;
            }
        }
    }
    let viewer = RenderScale::position(crate::camera::status().position_m);
    for index in &state.spacecraft_indices {
        let asset = &snapshot.assets[*index];
        if rig.mode == CameraMode::FirstPerson && rig.focus == asset.id {
            continue;
        }
        // Long-range markers must not hide the physical craft in close views.
        if viewer.distance(RenderScale::position(asset.position)) < 0.5 {
            continue;
        }
        gizmos.sphere(
            Isometry3d::from_translation(RenderScale::position(asset.position)),
            0.055,
            if asset.id == EntityId::Lander {
                Color::srgb(1.0, 0.67, 0.28)
            } else {
                Color::srgb(0.30, 0.76, 1.0)
            },
        );
    }
    for (position, color) in [
        (
            snapshot.environment.l1_position_m,
            Color::srgb(0.38, 0.9, 0.75),
        ),
        (
            snapshot.environment.l2_position_m,
            Color::srgb(0.3, 0.76, 1.0),
        ),
    ] {
        if viewer.distance(RenderScale::position(position)) < 0.5 {
            continue;
        }
        gizmos.sphere(
            Isometry3d::from_translation(RenderScale::position(position)),
            0.07,
            color,
        );
    }
}

fn is_new_snapshot(state: &SceneState, snapshot: &Rc<crate::model::Snapshot>) -> bool {
    state
        .snapshot
        .as_ref()
        .is_none_or(|previous| !Rc::ptr_eq(previous, snapshot))
}

fn refresh_snapshot_cache(state: &mut SceneState, snapshot: &crate::model::Snapshot) {
    state.asset_indices.clear();
    state.spacecraft_indices.clear();
    state.trail_indices.clear();
    state.nearby_trails.clear();
    for (index, asset) in snapshot.assets.iter().enumerate() {
        if asset.id != EntityId::Phobos && asset.id != EntityId::Mars {
            state.asset_indices.insert(asset.id, index);
        }
        if asset.kind.is_spacecraft() {
            state.spacecraft_indices.push(index);
        }
    }
    if let Some(origin) = state
        .asset_indices
        .get(&EntityId::Lander)
        .map(|index| snapshot.assets[*index].position)
    {
        for (index, asset) in snapshot.assets.iter().enumerate() {
            if asset.kind == EntityKind::Debris {
                let delta: [f64; 3] =
                    std::array::from_fn(|axis| asset.position[axis] - origin[axis]);
                retain_nearby_trail(
                    &mut state.nearby_trails,
                    NearbyTrail {
                        index,
                        id: asset.id,
                        distance_squared_m2: delta.iter().map(|value| value * value).sum(),
                    },
                );
            }
        }
    }
    state
        .trail_indices
        .extend_from_slice(&state.spacecraft_indices);
    state
        .trail_indices
        .extend(state.nearby_trails.iter().map(|nearby| nearby.index));
}

fn nearby_trail_order(a: &NearbyTrail, b: &NearbyTrail) -> Ordering {
    a.distance_squared_m2
        .total_cmp(&b.distance_squared_m2)
        .then_with(|| a.id.cmp(&b.id))
}

fn retain_nearby_trail(nearby: &mut Vec<NearbyTrail>, candidate: NearbyTrail) {
    let index = nearby.partition_point(|other| nearby_trail_order(other, &candidate).is_lt());
    if index < NEARBY_TRAIL_COUNT {
        if nearby.len() == NEARBY_TRAIL_COUNT {
            nearby.pop();
        }
        nearby.insert(index, candidate);
    }
}

fn update_trails(state: &mut SceneState, snapshot: &crate::model::Snapshot) {
    let active = &state.trail_indices;
    state
        .trails
        .retain(|id, _| active.iter().any(|index| snapshot.assets[*index].id == *id));
    for index in active {
        let asset = &snapshot.assets[*index];
        let position = RenderScale::position(asset.position);
        if snapshot.time_s != state.time_s {
            let path = state.trails.entry(asset.id).or_default();
            if path
                .back()
                .is_none_or(|last| last.distance_squared(position) > 1.0e-10)
            {
                path.push_back(position);
                while path.len() > TRAIL_LIMIT {
                    path.pop_front();
                }
            }
        } else if let Some(path) = state.trails.get_mut(&asset.id)
            && let Some(last) = path.back_mut()
        {
            // Same-clock interventions still show the real pose, not a stale tip.
            *last = position;
        }
    }
}

fn spawn_asset(
    commands: &mut Commands,
    assets: &SceneAssets,
    asset: &crate::model::EntityState,
    layers: RenderLayers,
) -> Entity {
    let (mesh, material) = match asset.kind {
        EntityKind::Lander => (assets.lander.clone(), assets.lander_material.clone()),
        EntityKind::ReturnShip => (assets.target.clone(), assets.target_material.clone()),
        EntityKind::LandingPad => (assets.pad.clone(), assets.pad_material.clone()),
        EntityKind::Debris => {
            let index = match asset.id {
                EntityId::Debris(id) => id as usize % 4,
                _ => 0,
            };
            (assets.rocks[index].clone(), assets.rock_material.clone())
        }
    };
    commands
        .spawn((
            Mesh3d(mesh),
            MeshMaterial3d(material),
            asset_transform(asset),
            AssetVisual {
                id: asset.id,
                kind: asset.kind,
            },
            layers,
            Visibility::Visible,
        ))
        .id()
}

fn asset_transform(asset: &crate::model::EntityState) -> Transform {
    Transform {
        translation: RenderScale::position(asset.position),
        rotation: rotation(asset.rotation),
        scale: meshes::scale(asset),
    }
}

fn asset_render_layers(rig: &CameraRig, id: EntityId) -> RenderLayers {
    if rig.mode == CameraMode::FirstPerson && rig.focus == id {
        // The debug first-person view hides its subject without changing sensor content.
        RenderLayers::layer(2)
    } else {
        RenderLayers::layer(0)
    }
}

fn rock_material(surface: crate::scenario::RockSurface) -> StandardMaterial {
    let [r, g, b] = surface.base_color_srgb;
    StandardMaterial {
        base_color: Color::srgb(r, g, b),
        metallic: 0.0,
        perceptual_roughness: surface.perceptual_roughness,
        ..default()
    }
}

fn rotation(q: [f64; 4]) -> Quat {
    Quat::from_xyzw(q[0] as f32, q[1] as f32, q[2] as f32, q[3] as f32).normalize()
}

#[cfg(test)]
mod tests {
    use super::*;
    use bevy::render::batching::NoAutomaticBatching;

    #[test]
    fn partial_nearest_selection_matches_full_order_with_entity_id_ties() {
        let candidates: Vec<_> = (1..=128)
            .map(|id| NearbyTrail {
                index: id as usize,
                id: EntityId::Debris(id),
                distance_squared_m2: f64::from(id % 11),
            })
            .collect();
        let mut expected = candidates.clone();
        expected.sort_by(nearby_trail_order);
        expected.truncate(NEARBY_TRAIL_COUNT);
        for reversed in [false, true] {
            let mut input = candidates.clone();
            if reversed {
                input.reverse();
            }
            let mut nearby = Vec::new();
            for candidate in input {
                retain_nearby_trail(&mut nearby, candidate);
                assert!(nearby.len() <= NEARBY_TRAIL_COUNT);
            }
            assert_eq!(
                nearby.iter().map(|entry| entry.id).collect::<Vec<_>>(),
                expected.iter().map(|entry| entry.id).collect::<Vec<_>>(),
            );
        }
    }

    #[test]
    fn snapshot_identity_and_id_mapping_do_not_assume_time_or_asset_order() {
        let original = Rc::new(crate::physics::Simulation::new_default().snapshot());
        let mut state = SceneState::default();
        assert!(is_new_snapshot(&state, &original));
        refresh_snapshot_cache(&mut state, &original);
        state.snapshot = Some(Rc::clone(&original));
        assert!(!is_new_snapshot(&state, &Rc::clone(&original)));

        let mut reordered = (*original).clone();
        reordered.assets.reverse();
        let replacement = Rc::new(reordered);
        assert_eq!(replacement.time_s, original.time_s);
        assert_eq!(replacement.seed, original.seed);
        assert!(is_new_snapshot(&state, &replacement));
        let nearest_ids: Vec<_> = state.nearby_trails.iter().map(|entry| entry.id).collect();
        refresh_snapshot_cache(&mut state, &replacement);
        for asset in &replacement.assets {
            if asset.id != EntityId::Phobos && asset.id != EntityId::Mars {
                let index = state.asset_indices[&asset.id];
                assert_eq!(replacement.assets[index].id, asset.id);
                assert_eq!(replacement.assets[index].position, asset.position);
            }
        }
        assert_eq!(
            state
                .nearby_trails
                .iter()
                .map(|entry| entry.id)
                .collect::<Vec<_>>(),
            nearest_ids,
        );
        let retained = Rc::downgrade(&original);
        drop(original);
        assert!(retained.upgrade().is_some());
    }

    #[test]
    fn component_lifecycle_invalidates_an_idle_scene() {
        let mut world = World::new();
        world.init_resource::<SceneEntityChanges>();
        let entity = world
            .spawn(AssetVisual {
                id: EntityId::Lander,
                kind: EntityKind::Lander,
            })
            .id();
        assert!(world.resource::<SceneEntityChanges>().dirty);
        world.resource_mut::<SceneEntityChanges>().dirty = false;
        world.entity_mut(entity).insert(AssetVisual {
            id: EntityId::ReturnShip,
            kind: EntityKind::ReturnShip,
        });
        assert!(world.resource::<SceneEntityChanges>().dirty);
        world.resource_mut::<SceneEntityChanges>().dirty = false;
        world.entity_mut(entity).remove::<AssetVisual>();
        assert!(world.resource::<SceneEntityChanges>().dirty);
    }

    #[test]
    fn unchanged_transforms_do_not_report_a_bevy_change() {
        let mut world = World::new();
        let entity = world.spawn(Transform::IDENTITY).id();
        world.clear_trackers();
        let mut transform = world.get_mut::<Transform>(entity).unwrap();
        transform.set_if_neq(Transform::IDENTITY);
        assert!(!transform.is_changed());
        transform.set_if_neq(Transform::from_xyz(1.0, 2.0, 3.0));
        assert!(transform.is_changed());
    }

    #[test]
    fn only_debug_first_person_moves_subjects_to_the_sensor_visible_layer() {
        let mut rig = CameraRig {
            mode: CameraMode::FirstPerson,
            focus: EntityId::Lander,
            ..default()
        };
        assert_eq!(
            asset_render_layers(&rig, EntityId::Lander),
            RenderLayers::layer(2),
        );
        assert_eq!(
            asset_render_layers(&rig, EntityId::ReturnShip),
            RenderLayers::layer(0),
        );
        rig.focus = EntityId::ReturnShip;
        assert_eq!(
            asset_render_layers(&rig, EntityId::Lander),
            RenderLayers::layer(0),
        );
        assert_eq!(
            asset_render_layers(&rig, EntityId::ReturnShip),
            RenderLayers::layer(2),
        );
        for mode in [CameraMode::ShipTop, CameraMode::ShipBottom] {
            rig.mode = mode;
            assert_eq!(
                asset_render_layers(&rig, EntityId::Lander),
                RenderLayers::layer(0),
            );
            assert_eq!(
                asset_render_layers(&rig, EntityId::ReturnShip),
                RenderLayers::layer(0),
            );
        }
    }

    #[test]
    fn only_planetary_surfaces_require_individual_readiness_draws() {
        let mut world = World::new();
        for surface in [PlanetarySurface::Phobos, PlanetarySurface::Mars] {
            let entity = world.spawn(surface).id();
            assert!(world.get::<NoAutomaticBatching>(entity).is_some());
        }
        let debris = world
            .spawn(AssetVisual {
                id: EntityId::Debris(100_000),
                kind: EntityKind::Debris,
            })
            .id();
        assert!(world.get::<NoAutomaticBatching>(debris).is_none());
    }
}
