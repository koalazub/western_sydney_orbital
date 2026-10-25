use super::model::RenderScale;
use crate::{model::EntityState, scenario};
use bevy::{
    mesh::{Indices, PrimitiveTopology, VertexAttributeValues},
    prelude::*,
};

/// Keep UVs off props that have no maps. Planetary meshes deliberately retain
/// UV0; app.rs inlines the embedded PBR shader's opaque resource aliases for
/// baseline WebGPU implementations without requesting optional WGSL features.
pub(super) fn untextured(mesh: impl Into<Mesh>) -> Mesh {
    mesh.into()
        .with_removed_attribute(Mesh::ATTRIBUTE_UV_0)
        .with_removed_attribute(Mesh::ATTRIBUTE_UV_1)
}

pub(super) fn phobos() -> Mesh {
    let (vertices, triangles) = crate::model::phobos_geometry();
    let terrain = &scenario::settings().terrain;
    textured_grid(
        vertices
            .into_iter()
            .map(|p| p.map(RenderScale::metres))
            .collect(),
        triangles,
        terrain.latitude_segments,
        terrain.longitude_segments,
    )
}

/// The caller supplies the real radius converted to render-space kilometres.
/// UV orientation matches Phobos and the checked-in north-up planetary maps.
pub(super) fn mars(radius: f32) -> Mesh {
    const LATITUDES: usize = 64;
    const LONGITUDES: usize = 96;
    let mut positions = Vec::with_capacity((LATITUDES + 1) * (LONGITUDES + 1));
    for row in 0..=LATITUDES {
        let latitude =
            -std::f32::consts::FRAC_PI_2 + std::f32::consts::PI * row as f32 / LATITUDES as f32;
        for col in 0..=LONGITUDES {
            let longitude = std::f32::consts::TAU * (col % LONGITUDES) as f32 / LONGITUDES as f32;
            let ring = if row == 0 || row == LATITUDES {
                0.0
            } else {
                latitude.cos()
            };
            positions.push([
                radius * ring * longitude.cos(),
                radius * latitude.sin(),
                radius * ring * longitude.sin(),
            ]);
        }
    }
    let mut triangles = Vec::with_capacity(2 * (LATITUDES - 1) * LONGITUDES);
    for row in 0..LATITUDES {
        for col in 0..LONGITUDES {
            let a = (row * (LONGITUDES + 1) + col) as u32;
            let b = a + (LONGITUDES + 1) as u32;
            if row > 0 {
                triangles.push([a, b, a + 1]);
            }
            if row + 1 < LATITUDES {
                triangles.push([a + 1, b, b + 1]);
            }
        }
    }
    let mut mesh = textured_grid(positions, triangles, LATITUDES, LONGITUDES);
    // On a true sphere, radial normals are exact and avoid polar mesh bias.
    let normals = mesh
        .attribute(Mesh::ATTRIBUTE_POSITION)
        .unwrap()
        .as_float3()
        .unwrap()
        .iter()
        .map(|p| Vec3::from_array(*p).normalize().to_array())
        .collect::<Vec<_>>();
    mesh.insert_attribute(Mesh::ATTRIBUTE_NORMAL, normals);
    mesh
}

fn textured_grid(
    positions: Vec<[f32; 3]>,
    triangles: Vec<[u32; 3]>,
    latitudes: usize,
    longitudes: usize,
) -> Mesh {
    // The physical grid already duplicates the longitude seam and poles.
    // Share only accumulated normals, never positions or triangle indices.
    let normal_index = |index: usize| {
        let row = index / (longitudes + 1);
        let col = index % (longitudes + 1);
        row * longitudes
            + if row == 0 || row == latitudes {
                0
            } else {
                col % longitudes
            }
    };
    let mut sums = vec![Vec3::ZERO; (latitudes + 1) * longitudes];
    for triangle in &triangles {
        let [a, b, c] = triangle.map(|i| Vec3::from_array(positions[i as usize]));
        let normal = (b - a).cross(c - a);
        for &index in triangle {
            sums[normal_index(index as usize)] += normal;
        }
    }
    let normals = positions
        .iter()
        .enumerate()
        .map(|(index, position)| {
            sums[normal_index(index)]
                .try_normalize()
                .unwrap_or_else(|| Vec3::from_array(*position).normalize())
                .to_array()
        })
        .collect::<Vec<_>>();
    let uvs = (0..=latitudes)
        .flat_map(|row| {
            (0..=longitudes).map(move |col| {
                [
                    col as f32 / longitudes as f32,
                    1.0 - row as f32 / latitudes as f32,
                ]
            })
        })
        .collect::<Vec<_>>();
    let mut mesh = Mesh::new(PrimitiveTopology::TriangleList, default());
    mesh.insert_attribute(Mesh::ATTRIBUTE_POSITION, positions);
    mesh.insert_attribute(Mesh::ATTRIBUTE_NORMAL, normals);
    mesh.insert_attribute(Mesh::ATTRIBUTE_UV_0, uvs);
    mesh.insert_indices(Indices::U32(triangles.into_iter().flatten().collect()));
    mesh
}

pub(super) fn lander() -> Mesh {
    let settings = scenario::settings();
    let hull = settings.lander.hull_half_extents_m;
    let mut mesh = Mesh::from(Cuboid::new(
        RenderScale::metres(hull[0] * 2.0),
        RenderScale::metres(hull[1] * 2.0),
        RenderScale::metres(hull[2] * 2.0),
    ));
    for position in settings.lander.leg_positions_m {
        let leg = settings.lander.leg_half_extents_m;
        let part = Mesh::from(Cuboid::new(
            RenderScale::metres(leg[0] * 2.0),
            RenderScale::metres(leg[1] * 2.0),
            RenderScale::metres(leg[2] * 2.0),
        ))
        .translated_by(Vec3::new(
            RenderScale::metres(position[0]),
            RenderScale::metres(position[1]),
            RenderScale::metres(position[2]),
        ));
        mesh.merge(&part)
            .expect("lander mesh components share attributes");
        let foot = RenderScale::metres(settings.lander.foot_radius_m);
        let part = Sphere::new(foot).mesh().uv(8, 6).translated_by(Vec3::new(
            RenderScale::metres(position[0]),
            RenderScale::metres(position[1]),
            RenderScale::metres(position[2] - leg[2] - settings.lander.foot_radius_m),
        ));
        mesh.merge(&part)
            .expect("lander mesh components share attributes");
    }
    untextured(mesh)
}

pub(super) fn pad() -> Mesh {
    let h = scenario::settings().pad.half_extents_m;
    untextured(Cuboid::new(
        RenderScale::metres(h[0] * 2.0),
        RenderScale::metres(h[1] * 2.0),
        RenderScale::metres(h[2] * 2.0),
    ))
}

pub(super) fn target() -> Mesh {
    let radius = 1.0_f32;
    let mut mesh = Sphere::new(radius)
        .mesh()
        .ico(2)
        .expect("valid icosphere detail");
    let panel = Mesh::from(Cuboid::new(radius * 2.4, radius * 0.12, radius * 0.35));
    mesh.merge(&panel)
        .expect("target components share attributes");
    untextured(mesh)
}

pub(super) fn asteroid(seed: u8) -> Mesh {
    let mut mesh = Sphere::new(1.0)
        .mesh()
        .ico(2)
        .expect("valid icosphere detail");
    if let Some(VertexAttributeValues::Float32x3(positions)) =
        mesh.attribute_mut(Mesh::ATTRIBUTE_POSITION)
    {
        let mut h = 0x811c9dc5u32 ^ seed as u32;
        for p in positions {
            h ^= h << 13;
            h ^= h >> 17;
            h ^= h << 5;
            let scale = 0.76 + (h as f32 / u32::MAX as f32) * 0.48;
            for c in p {
                *c *= scale;
            }
        }
    }
    mesh.compute_smooth_normals();
    untextured(mesh)
}

pub(super) fn scale(asset: &EntityState) -> Vec3 {
    match asset.kind {
        crate::domain::EntityKind::Debris | crate::domain::EntityKind::ReturnShip => {
            Vec3::splat(RenderScale::metres(asset.radius_m))
        }
        _ => Vec3::ONE,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_planetary_world_meshes_have_texture_coordinates() {
        for mesh in [
            lander(),
            pad(),
            target(),
            asteroid(1),
            untextured(Sphere::new(1.0).mesh().uv(48, 32)),
        ] {
            assert!(!mesh.contains_attribute(Mesh::ATTRIBUTE_UV_0));
            assert!(!mesh.contains_attribute(Mesh::ATTRIBUTE_UV_1));
            assert!(mesh.contains_attribute(Mesh::ATTRIBUTE_POSITION));
            assert!(mesh.contains_attribute(Mesh::ATTRIBUTE_NORMAL));
            assert!(mesh.count_vertices() > 0);
        }
        for mesh in [phobos(), mars(3_389.5)] {
            assert!(mesh.contains_attribute(Mesh::ATTRIBUTE_UV_0));
            assert!(!mesh.contains_attribute(Mesh::ATTRIBUTE_UV_1));
            assert!(!mesh.contains_attribute(Mesh::ATTRIBUTE_TANGENT));
            let positions = mesh
                .attribute(Mesh::ATTRIBUTE_POSITION)
                .unwrap()
                .as_float3()
                .unwrap();
            let normals = mesh
                .attribute(Mesh::ATTRIBUTE_NORMAL)
                .unwrap()
                .as_float3()
                .unwrap();
            let Some(VertexAttributeValues::Float32x2(uvs)) = mesh.attribute(Mesh::ATTRIBUTE_UV_0)
            else {
                panic!("planetary maps need two-dimensional UV0 coordinates");
            };
            assert_eq!(positions.len(), normals.len());
            assert_eq!(positions.len(), uvs.len());
            for ((position, normal), uv) in positions.iter().zip(normals).zip(uvs) {
                assert!(
                    position
                        .iter()
                        .chain(normal)
                        .chain(uv)
                        .all(|x| x.is_finite())
                );
                assert!((Vec3::from_array(*normal).length() - 1.0).abs() < 1.0e-5);
                assert!(Vec3::from_array(*position).dot(Vec3::from_array(*normal)) > 0.0);
                assert!(uv.iter().all(|x| (0.0..=1.0).contains(x)));
            }
            let indices = mesh.indices().unwrap().iter().collect::<Vec<_>>();
            for triangle in indices.chunks_exact(3) {
                let us = [triangle[0], triangle[1], triangle[2]].map(|index| uvs[index][0]);
                let min = us.into_iter().fold(f32::INFINITY, f32::min);
                let max = us.into_iter().fold(f32::NEG_INFINITY, f32::max);
                assert!(
                    max - min < 0.02,
                    "triangle interpolates across the map seam"
                );
            }
        }
    }

    #[test]
    fn phobos_uvs_and_smoothed_seams_preserve_the_entire_contact_surface() {
        let (vertices, triangles) = crate::model::phobos_geometry();
        let mesh = phobos();
        let positions = mesh
            .attribute(Mesh::ATTRIBUTE_POSITION)
            .unwrap()
            .as_float3()
            .unwrap();
        assert_eq!(
            positions,
            &vertices
                .into_iter()
                .map(|p| p.map(RenderScale::metres))
                .collect::<Vec<_>>()
        );
        assert_eq!(
            mesh.indices().unwrap().iter().collect::<Vec<_>>(),
            triangles
                .into_iter()
                .flatten()
                .map(|i| i as usize)
                .collect::<Vec<_>>()
        );
        let normals = mesh
            .attribute(Mesh::ATTRIBUTE_NORMAL)
            .unwrap()
            .as_float3()
            .unwrap();
        let Some(VertexAttributeValues::Float32x2(uvs)) = mesh.attribute(Mesh::ATTRIBUTE_UV_0)
        else {
            panic!("missing planetary UV0");
        };
        let terrain = &scenario::settings().terrain;
        for row in 0..=terrain.latitude_segments {
            let a = row * (terrain.longitude_segments + 1);
            let b = a + terrain.longitude_segments;
            assert!(Vec3::from_array(positions[a]).distance(Vec3::from_array(positions[b])) < 1e-5);
            assert_eq!(normals[a], normals[b]);
            assert_eq!(uvs[a][0], 0.0);
            assert_eq!(uvs[b][0], 1.0);
            assert_eq!(uvs[a][1], uvs[b][1]);
        }
    }

    #[test]
    fn mars_mesh_uses_the_supplied_radius_without_visual_inflation() {
        for radius in [0.001_f32, 1.0, 3_389.5, 6_000.0] {
            let mesh = mars(radius);
            let positions = mesh
                .attribute(Mesh::ATTRIBUTE_POSITION)
                .unwrap()
                .as_float3()
                .unwrap();
            for position in positions {
                assert!((Vec3::from_array(*position).length() - radius).abs() < radius * 2e-6);
            }
            assert_eq!(positions.len(), 65 * 97);
            assert_eq!(mesh.indices().unwrap().len(), 2 * 63 * 96 * 3);
        }
    }

    #[test]
    fn removing_unused_uvs_preserves_real_geometry_and_lighting_normals() {
        let original = Mesh::from(Cuboid::new(2.0, 3.0, 4.0));
        let mesh = untextured(original.clone());
        for attribute in [Mesh::ATTRIBUTE_POSITION, Mesh::ATTRIBUTE_NORMAL] {
            assert_eq!(
                original.attribute(attribute).unwrap().as_float3(),
                mesh.attribute(attribute).unwrap().as_float3(),
            );
        }
        assert_eq!(
            original.indices().unwrap().iter().collect::<Vec<_>>(),
            mesh.indices().unwrap().iter().collect::<Vec<_>>(),
        );
    }

    #[test]
    fn configured_physical_bodies_supply_their_actual_radius_to_rendering() {
        let mut settings = crate::model::ExperimentSettings::default();
        settings.debris.count = 31;
        settings.debris.radius_min_m = 0.5;
        settings.debris.radius_max_m = 75.0;
        let simulation = crate::physics::Simulation::with_experiment(17, settings).unwrap();
        let snapshot = simulation.snapshot();
        let debris: Vec<_> = snapshot
            .assets
            .iter()
            .filter(|asset| asset.kind == crate::domain::EntityKind::Debris)
            .collect();
        assert_eq!(debris.len(), settings.debris.count);
        for asset in debris {
            assert_eq!(
                scale(asset),
                Vec3::splat(RenderScale::metres(asset.radius_m))
            );
        }
        for seed in 1..=4 {
            let mesh = asteroid(seed);
            assert!(mesh.count_vertices() > 0);
        }
    }
}
