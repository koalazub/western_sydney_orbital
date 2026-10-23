use super::{
    meshes,
    model::{AssetVisual, CelestialSphere, RenderScale, SceneAssets, SceneState},
};
use crate::{
    camera::{CameraRig, SimulationCamera},
    domain::{CameraMode, EntityId, EntityKind},
};
use bevy::{
    camera::{Exposure, visibility::RenderLayers},
    core_pipeline::tonemapping::Tonemapping,
    light::{GlobalAmbientLight, NotShadowCaster},
    prelude::*,
};
use std::collections::{HashMap, HashSet};

const TRAIL_LIMIT: usize = 256;

pub fn setup(
    mut commands: Commands,
    mut meshes_assets: ResMut<Assets<Mesh>>,
    mut materials: ResMut<Assets<StandardMaterial>>,
) {
    let snapshot = crate::snapshot();
    let lighting = crate::scenario::settings().lighting;
    let direction_to_sun = Vec3::from_array(lighting.direction_to_sun).normalize();
    commands.insert_resource(GlobalAmbientLight::NONE);
    commands.insert_resource(SceneState {
        seed: snapshot.seed,
        time_s: snapshot.time_s,
        ..default()
    });
    commands.spawn((
        Mesh3d(meshes_assets.add(meshes::phobos())),
        MeshMaterial3d(materials.add(rock_material(lighting.phobos_surface))),
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
    let up = snapshot
        .assets
        .iter()
        .find(|asset| asset.id == EntityId::Lander)
        .map(|asset| rotation(asset.rotation) * Vec3::Z)
        .unwrap_or(Vec3::Z);
    commands.spawn((
        Camera3d::default(),
        Projection::Perspective(PerspectiveProjection {
            near: 0.0005,
            far: 20_000.0,
            fov: 60_f32.to_radians(),
            ..default()
        }),
        Tonemapping::None,
        Exposure::SUNLIGHT,
        AmbientLight {
            color: Color::WHITE,
            brightness: lighting.debug_ambient_brightness,
            ..default()
        },
        Transform::from_translation(lander_position + up * 0.18 + Vec3::new(0.1, 0.1, 0.0))
            .looking_at(lander_position, up),
        SimulationCamera,
        IsDefaultUiCamera,
        RenderLayers::from_layers(&[0, 1]),
    ));
    commands.spawn((
        Mesh3d(
            meshes_assets.add(meshes::untextured(
                Sphere::new(RenderScale::metres(snapshot.environment.mars_radius_m))
                    .mesh()
                    .uv(48, 32),
            )),
        ),
        MeshMaterial3d(materials.add(rock_material(lighting.mars_surface))),
        Transform::from_translation(RenderScale::position(snapshot.environment.mars_position_m)),
        CelestialSphere::Mars,
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
    mut state: ResMut<SceneState>,
    mut gizmos: Gizmos,
    rig: Res<CameraRig>,
) {
    let snapshot = crate::snapshot();
    let lighting = crate::scenario::settings().lighting;
    let lander_position = snapshot
        .assets
        .iter()
        .find(|asset| asset.id == EntityId::Lander)
        .map(|asset| RenderScale::position(asset.position))
        .unwrap_or(Vec3::ZERO);
    for (sphere, mut transform) in &mut celestials {
        transform.translation = match sphere {
            CelestialSphere::Mars => RenderScale::position(snapshot.environment.mars_position_m),
            CelestialSphere::Sun => {
                lander_position
                    + Vec3::from_array(lighting.direction_to_sun).normalize()
                        * RenderScale::metres(lighting.sun_proxy_distance_m)
            }
        };
    }
    if snapshot.seed != state.seed || snapshot.time_s < state.time_s {
        state.trails.clear();
        state.seed = snapshot.seed;
    }
    let by_id: HashMap<EntityId, _> = snapshot
        .assets
        .iter()
        .filter(|a| a.id != EntityId::Phobos && a.id != EntityId::Mars)
        .map(|a| (a.id, a))
        .collect();
    let mut existing = HashSet::new();
    for (entity, tag, mut transform, mut visibility, mut layers) in &mut visuals {
        if let Some(asset) = by_id.get(&tag.id).filter(|asset| asset.kind == tag.kind) {
            existing.insert(tag.id);
            transform.translation = RenderScale::position(asset.position);
            transform.rotation = rotation(asset.rotation);
            transform.scale = meshes::scale(asset);
            *visibility = Visibility::Visible;
            *layers = asset_render_layers(&rig, asset.id);
        } else {
            commands.entity(entity).despawn();
        }
    }
    for asset in snapshot
        .assets
        .iter()
        .filter(|a| a.id != EntityId::Phobos && a.id != EntityId::Mars)
    {
        if !existing.contains(&asset.id) {
            spawn_asset(
                &mut commands,
                &assets,
                asset,
                asset_render_layers(&rig, asset.id),
            );
        }
    }
    let mut trail_entities: Vec<_> = snapshot
        .assets
        .iter()
        .filter(|a| a.kind.is_spacecraft())
        .collect();
    if let Some(lander) = snapshot.assets.iter().find(|a| a.id == EntityId::Lander) {
        let origin = RenderScale::position(lander.position);
        let mut debris: Vec<_> = snapshot
            .assets
            .iter()
            .filter(|a| a.kind == EntityKind::Debris)
            .collect();
        debris.sort_by(|a, b| {
            RenderScale::position(a.position)
                .distance_squared(origin)
                .total_cmp(&RenderScale::position(b.position).distance_squared(origin))
        });
        trail_entities.extend(debris.into_iter().take(8));
    }
    let active: HashSet<_> = trail_entities.iter().map(|a| a.id).collect();
    for asset in trail_entities {
        if snapshot.time_s != state.time_s {
            let pos = RenderScale::position(asset.position);
            let path = state.trails.entry(asset.id).or_default();
            if path
                .back()
                .is_none_or(|last| last.distance_squared(pos) > 1.0e-10)
            {
                path.push_back(pos);
                while path.len() > TRAIL_LIMIT {
                    path.pop_front();
                }
            }
        }
    }
    state.trails.retain(|id, _| active.contains(id));
    state.last_positions = snapshot
        .assets
        .iter()
        .map(|a| (a.id, RenderScale::position(a.position)))
        .collect();
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
    for asset in snapshot.assets.iter().filter(|a| a.kind.is_spacecraft()) {
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
    state.time_s = snapshot.time_s;
}

fn spawn_asset(
    commands: &mut Commands,
    assets: &SceneAssets,
    asset: &crate::model::EntityState,
    layers: RenderLayers,
) {
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
    commands.spawn((
        Mesh3d(mesh),
        MeshMaterial3d(material),
        Transform {
            translation: RenderScale::position(asset.position),
            rotation: rotation(asset.rotation),
            scale: meshes::scale(asset),
        },
        AssetVisual {
            id: asset.id,
            kind: asset.kind,
        },
        layers,
        Visibility::Visible,
    ));
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
