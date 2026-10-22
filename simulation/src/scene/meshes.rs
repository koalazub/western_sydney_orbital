use super::model::RenderScale;
use crate::{model::EntityState, scenario};
use bevy::{
    mesh::{Indices, PrimitiveTopology, VertexAttributeValues},
    prelude::*,
};

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
    mesh
}

pub(super) fn pad() -> Mesh {
    let h = scenario::settings().pad.half_extents_m;
    Mesh::from(Cuboid::new(
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
    mesh
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
    mesh
}

pub(super) fn scale(asset: &EntityState) -> Vec3 {
    match asset.kind {
        crate::domain::EntityKind::Debris | crate::domain::EntityKind::ReturnShip => {
            Vec3::splat(RenderScale::metres(asset.radius_m))
        }
        _ => Vec3::ONE,
    }
}
