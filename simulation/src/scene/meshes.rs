use super::model::RenderScale;
use crate::{model::EntityState, scenario};
use bevy::{
    mesh::{Indices, PrimitiveTopology, VertexAttributeValues},
    prelude::*,
};

/// These scene materials use physical surface colors, not texture maps.
/// Primitive UVs otherwise select Bevy 0.20's textured PBR shader, whose
/// texture/sampler `let` aliases require optional WGSL language support.
pub(super) fn untextured(mesh: impl Into<Mesh>) -> Mesh {
    mesh.into()
        .with_removed_attribute(Mesh::ATTRIBUTE_UV_0)
        .with_removed_attribute(Mesh::ATTRIBUTE_UV_1)
}

pub(super) fn phobos() -> Mesh {
    let (vertices, triangles) = crate::model::phobos_geometry();
    let mut mesh = Mesh::new(PrimitiveTopology::TriangleList, default());
    mesh.insert_attribute(
        Mesh::ATTRIBUTE_POSITION,
        vertices
            .into_iter()
            .map(|p| p.map(RenderScale::metres))
            .collect::<Vec<_>>(),
    );
    mesh.insert_indices(Indices::U32(triangles.into_iter().flatten().collect()));
    mesh.compute_smooth_normals();
    untextured(mesh)
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
    fn actual_world_meshes_do_not_select_textured_pbr() {
        for mesh in [
            phobos(),
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
}
