//! Snapshot-only Bevy rendering. Physics and the simulation clock stay in the model.

use crate::model::EntityState;
use bevy::{
    core_pipeline::tonemapping::Tonemapping,
    mesh::{Indices, PrimitiveTopology, VertexAttributeValues},
    prelude::*,
};
use std::{
    cell::RefCell,
    collections::{HashMap, VecDeque},
};

const KM: f64 = 0.001;
const TRAIL_LIMIT: usize = 256;

#[derive(Component)]
pub(crate) struct AssetVisual {
    id: String,
    kind: String,
    radius_m: f64,
}
#[derive(Component)]
pub(crate) struct SceneCamera;
#[derive(Resource, Default)]
pub(crate) struct SceneState {
    seed: u64,
    time_s: f64,
    last_positions: HashMap<String, Vec3>,
    trails: HashMap<String, VecDeque<Vec3>>,
    view: String,
    focus: String,
    zoom: f32,
}
thread_local! { static CAMERA_REQUEST: RefCell<(String,String,f32)> = RefCell::new(("mission".into(), "Shuttle".into(), 1.0)); }

fn set_view_zoom(mode: &str, focus: &str, zoom: Option<f32>) -> Result<(), String> {
    if !["mission", "overview", "follow", "top"].contains(&mode) {
        return Err("unknown scene view".into());
    }
    CAMERA_REQUEST.with(|request| {
        let mut request = request.borrow_mut();
        let zoom = zoom.unwrap_or(request.2);
        if !(0.25..=4.0).contains(&zoom) {
            return Err("zoom must be between 0.25 and 4".into());
        }
        *request = (mode.to_owned(), focus.to_owned(), zoom);
        Ok(())
    })
}

/// Parses view requests. Zoom is an absolute distance factor, where smaller is closer.
pub fn view_command(request: &serde_json::Value) -> Result<(), String> {
    let mode = request
        .get("mode")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("mission");
    let focus = request
        .get("focus")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("Shuttle");
    let zoom = request
        .get("zoom")
        .map(|value| {
            value
                .as_f64()
                .ok_or_else(|| "zoom must be a number between 0.25 and 4".to_owned())
        })
        .transpose()?
        .map(|zoom| zoom as f32);
    set_view_zoom(mode, focus, zoom)
}

/// Creates the scaled Phobos reference body, camera, lighting, and initial entities.
pub fn setup(
    mut commands: Commands,
    mut meshes: ResMut<Assets<Mesh>>,
    mut materials: ResMut<Assets<StandardMaterial>>,
) {
    let s = crate::snapshot();
    commands.insert_resource(SceneState {
        seed: s.seed,
        time_s: s.time_s,
        view: "mission".into(),
        focus: "Shuttle".into(),
        zoom: 1.0,
        ..default()
    });
    commands.spawn((
        Mesh3d(meshes.add(phobos_mesh())),
        MeshMaterial3d(materials.add(StandardMaterial {
            base_color: Color::srgb(0.38, 0.35, 0.31),
            perceptual_roughness: 1.0,
            ..default()
        })),
        Transform::default(),
    ));
    commands.spawn((
        Camera3d::default(),
        Tonemapping::None,
        AmbientLight {
            color: Color::WHITE,
            brightness: 500.0,
            ..default()
        },
        Transform::from_xyz(-1.6, 1.5, -12.5).looking_at(Vec3::new(-0.75, 0.1, -8.9), Vec3::Y),
        SceneCamera,
    ));
    commands.spawn((
        DirectionalLight {
            illuminance: 8_000.0,
            ..default()
        },
        Transform::from_xyz(-25.0, 30.0, -40.0).looking_at(Vec3::ZERO, Vec3::Y),
    ));
    for a in s.assets.iter().filter(|a| a.kind != "primary") {
        spawn_asset(&mut commands, &mut meshes, &mut materials, a);
    }
}

/// Reconciles visuals against the current snapshot. It never runs physics.
pub fn sync_scene(
    mut commands: Commands,
    mut meshes: ResMut<Assets<Mesh>>,
    mut materials: ResMut<Assets<StandardMaterial>>,
    mut visuals: Query<(Entity, &AssetVisual, &mut Transform), Without<SceneCamera>>,
    mut state: ResMut<SceneState>,
    mut gizmos: Gizmos,
    mut camera: Query<&mut Transform, With<SceneCamera>>,
) {
    let s = crate::snapshot();
    CAMERA_REQUEST.with(|request| {
        let (mode, focus, zoom) = request.borrow().clone();
        state.view = mode;
        state.focus = focus;
        state.zoom = zoom;
    });
    let restored_in_place = s.time_s == state.time_s
        && (s.assets.len() != state.last_positions.len()
            || s.assets.iter().any(|a| {
                state
                    .last_positions
                    .get(&a.id)
                    .is_some_and(|p| p.distance_squared(world_position(a.position)) > 1.0e-10)
            }));
    if s.seed != state.seed || s.time_s < state.time_s || restored_in_place {
        state.trails.clear();
        state.seed = s.seed;
    }
    let by_id: HashMap<&str, &EntityState> = s
        .assets
        .iter()
        .filter(|a| a.kind != "primary")
        .map(|asset| (asset.id.as_str(), asset))
        .collect();
    let mut existing = std::collections::HashSet::new();
    for (_, tag, mut transform) in &mut visuals {
        if let Some(asset) = by_id
            .get(tag.id.as_str())
            .filter(|asset| asset.kind == tag.kind && asset.radius_m == tag.radius_m)
        {
            existing.insert((tag.id.as_str(), tag.kind.as_str()));
            transform.translation = world_position(asset.position);
            transform.rotation = rotation(asset.rotation);
        }
    }
    for asset in s.assets.iter().filter(|a| a.kind != "primary") {
        if !existing.contains(&(asset.id.as_str(), asset.kind.as_str())) {
            spawn_asset(&mut commands, &mut meshes, &mut materials, asset);
        }
    }
    let mut trail_assets: Vec<&EntityState> = s
        .assets
        .iter()
        .filter(|asset| asset.kind == "ship" || asset.kind == "target")
        .collect();
    if let Some(shuttle) = s.assets.iter().find(|asset| asset.kind == "ship") {
        let origin = world_position(shuttle.position);
        let mut debris: Vec<_> = s
            .assets
            .iter()
            .filter(|asset| asset.kind == "asteroid" || asset.kind == "debris")
            .collect();
        debris.sort_by(|a, b| {
            world_position(a.position)
                .distance_squared(origin)
                .total_cmp(&world_position(b.position).distance_squared(origin))
        });
        trail_assets.extend(debris.into_iter().take(8));
    }
    let mut active = std::collections::HashSet::new();
    for asset in trail_assets {
        active.insert(asset.id.as_str());
        if s.time_s != state.time_s {
            let pos = world_position(asset.position);
            let path = state.trails.entry(asset.id.clone()).or_default();
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
    state.trails.retain(|id, _| active.contains(id.as_str()));
    state.last_positions = s
        .assets
        .iter()
        .map(|a| (a.id.clone(), world_position(a.position)))
        .collect();
    let shuttle = s
        .assets
        .iter()
        .find(|a| a.id == state.focus || a.name == state.focus)
        .or_else(|| s.assets.iter().find(|a| a.kind == "ship"));
    if let Ok(mut transform) = camera.single_mut() {
        let center = match state.view.as_str() {
            "overview" => Vec3::ZERO,
            "follow" => shuttle
                .map(|a| world_position(a.position))
                .unwrap_or(Vec3::new(-0.75, 0.1, -8.9)),
            "top" => Vec3::new(-0.75, 0.1, -8.9),
            _ => {
                let target = s.assets.iter().find(|a| a.kind == "target");
                match (shuttle, target) {
                    (Some(a), Some(b)) => {
                        (world_position(a.position) + world_position(b.position)) * 0.5
                    }
                    (Some(a), _) => world_position(a.position),
                    _ => Vec3::new(-0.75, 0.1, -8.9),
                }
            }
        };
        let offset = match state.view.as_str() {
            "overview" => Vec3::new(24.0, 19.0, -38.0),
            "follow" => Vec3::new(-0.2, 0.25, -0.7),
            "top" => Vec3::new(0.0, 3.5, 0.001),
            _ => Vec3::new(-1.9, 1.0, -1.6),
        } * state.zoom;
        transform.translation = center + offset;
        *transform = Transform::from_translation(center + offset).looking_at(
            center,
            if state.view == "top" {
                Vec3::Z
            } else {
                Vec3::Y
            },
        );
    }
    for (entity, tag, _) in &mut visuals {
        if by_id
            .get(tag.id.as_str())
            .is_none_or(|asset| asset.kind != tag.kind || asset.radius_m != tag.radius_m)
        {
            commands.entity(entity).despawn();
        }
    }
    for (id, path) in &state.trails {
        let color = if by_id.get(id.as_str()).is_some_and(|a| a.kind == "ship") {
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
    // Render-only locator: the underlying shuttle mesh and collision dimensions remain SI-scaled.
    for a in s
        .assets
        .iter()
        .filter(|a| a.kind == "ship" || a.kind == "target")
    {
        let p = world_position(a.position);
        let tint = if a.kind == "ship" {
            Color::srgb(1.0, 0.67, 0.28)
        } else {
            Color::srgb(0.30, 0.76, 1.0)
        };
        gizmos.sphere(Isometry3d::from_translation(p), 0.055, tint);
    }
    state.time_s = s.time_s;
}

fn spawn_asset(
    commands: &mut Commands,
    meshes: &mut Assets<Mesh>,
    materials: &mut Assets<StandardMaterial>,
    a: &EntityState,
) {
    let (mesh, color) = match a.kind.as_str() {
        "ship" => (ship_mesh(), Color::srgb(0.96, 0.57, 0.19)),
        "target" => (
            target_mesh((a.radius_m * KM) as f32),
            Color::srgb(0.24, 0.70, 0.98),
        ),
        _ => (
            asteroid_mesh(a.id.as_bytes(), a.radius_m),
            Color::srgb(0.48, 0.55, 0.59),
        ),
    };
    commands.spawn((
        Mesh3d(meshes.add(mesh)),
        MeshMaterial3d(materials.add(StandardMaterial {
            base_color: color,
            metallic: 0.12,
            perceptual_roughness: 0.84,
            ..default()
        })),
        Transform {
            translation: world_position(a.position),
            rotation: Quat::from_xyzw(
                a.rotation[0] as f32,
                a.rotation[1] as f32,
                a.rotation[2] as f32,
                a.rotation[3] as f32,
            )
            .normalize(),
            ..default()
        },
        AssetVisual {
            id: a.id.clone(),
            kind: a.kind.clone(),
            radius_m: a.radius_m,
        },
    ));
}

fn world_position(p: [f64; 3]) -> Vec3 {
    Vec3::new((p[0] * KM) as f32, (p[1] * KM) as f32, (p[2] * KM) as f32)
}
fn rotation(q: [f64; 4]) -> Quat {
    Quat::from_xyzw(q[0] as f32, q[1] as f32, q[2] as f32, q[3] as f32).normalize()
}

fn phobos_mesh() -> Mesh {
    let (vertices, triangles) = crate::model::phobos_geometry();
    let mut mesh = Mesh::new(PrimitiveTopology::TriangleList, default());
    mesh.insert_attribute(
        Mesh::ATTRIBUTE_POSITION,
        vertices
            .into_iter()
            .map(|p| p.map(|v| (v * KM) as f32))
            .collect::<Vec<_>>(),
    );
    mesh.insert_indices(Indices::U32(triangles.into_iter().flatten().collect()));
    mesh.compute_smooth_normals();
    mesh
}

fn ship_mesh() -> Mesh {
    // Render-only model: 60 m wingspan by 26 m length, simplified shuttle and panels.
    let positions = vec![
        [0., 0., 0.018],
        [0., 0.004, -0.008],
        [-0.014, 0., -0.004],
        [0.014, 0., -0.004],
        [0., -0.003, -0.008],
        [-0.030, 0., -0.003],
        [-0.014, 0., -0.008],
        [0.014, 0., -0.008],
        [0.030, 0., -0.003],
    ];
    let indices = vec![
        0, 2, 1, 0, 1, 3, 0, 4, 2, 0, 3, 4, 1, 2, 4, 1, 4, 3, 5, 6, 2, 3, 7, 8,
    ];
    let mut mesh = Mesh::new(PrimitiveTopology::TriangleList, default());
    mesh.insert_attribute(Mesh::ATTRIBUTE_POSITION, positions);
    mesh.insert_indices(Indices::U32(indices));
    mesh.duplicate_vertices();
    mesh.compute_flat_normals();
    mesh
}

fn target_mesh(radius: f32) -> Mesh {
    let radius = radius.max(0.03);
    let mut mesh = Mesh::from(Cuboid::new(radius * 0.45, radius * 0.45, radius * 1.5));
    for side in [-1.0, 1.0] {
        let panel = Mesh::from(Cuboid::new(radius * 0.7, radius * 0.045, radius * 0.85))
            .translated_by(Vec3::new(side * radius * 0.6, 0.0, 0.0));
        mesh.merge(&panel)
            .expect("station mesh components share attributes");
    }
    mesh
}

fn asteroid_mesh(seed: &[u8], radius_m: f64) -> Mesh {
    let mut mesh = Sphere::new(1.0).mesh().ico(2).unwrap();
    if let Some(VertexAttributeValues::Float32x3(pos)) =
        mesh.attribute_mut(Mesh::ATTRIBUTE_POSITION)
    {
        let mut h = seed
            .iter()
            .fold(0x811c9dc5u32, |a, b| a.wrapping_mul(16777619) ^ *b as u32);
        for p in pos {
            h ^= h << 13;
            h ^= h >> 17;
            h ^= h << 5;
            let scale = 0.76 + (h as f32 / u32::MAX as f32) * 0.48;
            for c in p {
                *c *= scale;
            }
        }
    }
    let radius = (radius_m * KM) as f32;
    if let Some(VertexAttributeValues::Float32x3(pos)) =
        mesh.attribute_mut(Mesh::ATTRIBUTE_POSITION)
    {
        for p in pos {
            for c in p {
                *c *= radius;
            }
        }
    }
    mesh.compute_smooth_normals();
    mesh
}
