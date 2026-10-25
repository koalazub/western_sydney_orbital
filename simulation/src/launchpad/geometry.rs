//! Double-precision intersections with the renderer/contact terrain, not a sphere proxy.
use super::{
    SurfaceStep,
    model::{ActorIndices, NUDGE_METRES, Preview},
};
use crate::{model::Snapshot, terrain::LandingSite};
use bevy::{
    math::{DQuat, DVec3},
    prelude::*,
};
use std::sync::OnceLock;

const METRES_PER_RENDER_UNIT: f64 = 1000.0;
pub(super) const GRIP_HEIGHT_PX: f32 = 32.0;
pub(super) const TARGET_HALF_PX: f32 = 14.0;

#[derive(Clone, Copy, Debug)]
pub(super) struct Ray {
    pub origin: DVec3,
    pub direction: DVec3,
}

#[derive(Clone, Copy, Debug)]
pub(super) struct SurfaceHit {
    pub distance_m: f64,
    pub position_m: DVec3,
    pub normal: DVec3,
}

struct Triangle {
    a: DVec3,
    edge1: DVec3,
    edge2: DVec3,
    normal: DVec3,
}

pub(super) struct Terrain {
    triangles: Vec<Triangle>,
}

impl Terrain {
    fn shared() -> &'static Self {
        static TERRAIN: OnceLock<Terrain> = OnceLock::new();
        TERRAIN.get_or_init(|| {
            let (vertices, triangles) = crate::terrain::phobos_geometry();
            Self {
                triangles: triangles
                    .into_iter()
                    .filter_map(|indices| {
                        let [a, b, c] =
                            indices.map(|index| DVec3::from_array(vertices[index as usize]));
                        let edge1 = b - a;
                        let edge2 = c - a;
                        let face = edge1.cross(edge2);
                        let normal = face.try_normalize()?;
                        Some(Triangle {
                            a,
                            edge1,
                            edge2,
                            normal: if normal.dot(a) < 0.0 { -normal } else { normal },
                        })
                    })
                    .collect(),
            }
        })
    }

    pub fn intersect(ray: Ray) -> Option<SurfaceHit> {
        if !ray.origin.is_finite() || !ray.direction.is_finite() {
            return None;
        }
        let direction = ray.direction.try_normalize()?;
        let mut nearest: Option<SurfaceHit> = None;
        for triangle in &Self::shared().triangles {
            let p = direction.cross(triangle.edge2);
            let determinant = triangle.edge1.dot(p);
            if determinant.abs() < 1.0e-10 {
                continue;
            }
            let from_a = ray.origin - triangle.a;
            let u = from_a.dot(p) / determinant;
            if !(-1.0e-9..=1.0 + 1.0e-9).contains(&u) {
                continue;
            }
            let q = from_a.cross(triangle.edge1);
            let v = direction.dot(q) / determinant;
            if v < -1.0e-9 || u + v > 1.0 + 1.0e-9 {
                continue;
            }
            let distance_m = triangle.edge2.dot(q) / determinant;
            if distance_m >= 0.0 && nearest.is_none_or(|hit| distance_m < hit.distance_m) {
                nearest = Some(SurfaceHit {
                    distance_m,
                    position_m: ray.origin + direction * distance_m,
                    normal: triangle.normal,
                });
            }
        }
        nearest
    }
}

pub(super) fn render_position(metres: DVec3) -> Vec3 {
    (metres / METRES_PER_RENDER_UNIT).as_vec3()
}

pub(super) fn viewport_ray(camera: &Camera, global: &GlobalTransform, cursor: Vec2) -> Option<Ray> {
    if !camera.is_active
        || !cursor.is_finite()
        || !camera.logical_viewport_rect()?.contains(cursor)
        || !global.affine().is_finite()
        || global.affine().matrix3.determinant().abs() < f32::MIN_POSITIVE
        || !camera.computed.clip_from_view.is_finite()
        || camera.computed.clip_from_view.determinant().abs() < f32::MIN_POSITIVE
    {
        return None;
    }
    // Bevy accounts for projection, viewport origin and device-pixel ratio.
    let ray = camera.viewport_to_world(global, cursor).ok()?;
    Some(Ray {
        origin: ray.origin.as_dvec3() * METRES_PER_RENDER_UNIT,
        direction: ray.direction.as_dvec3(),
    })
}

pub(super) fn surface_preview(
    camera: &Camera,
    global: &GlobalTransform,
    cursor: Vec2,
) -> Option<Preview> {
    let hit = Terrain::intersect(viewport_ray(camera, global, cursor)?)?;
    if hit.normal.dot(hit.position_m) <= 0.0 {
        return None;
    }
    let direction = hit.position_m.try_normalize()?.to_array();
    let site = crate::terrain::landing_site_at_direction(direction).ok()?;
    Some(Preview { direction, site })
}

pub(super) fn actual_site(snapshot: &Snapshot, indices: ActorIndices) -> Option<LandingSite> {
    let pad = snapshot.assets.get(indices.pad?)?;
    if pad.id != crate::domain::EntityId::LandingPad {
        return None;
    }
    let position = DVec3::from_array(pad.position);
    let rotation = DQuat::from_array(pad.rotation);
    if !position.is_finite() || !rotation.is_finite() || rotation.length_squared() < 1.0e-12 {
        return None;
    }
    let normal = rotation.normalize() * DVec3::Z;
    let surface = snapshot
        .launch_site_direction
        .and_then(|direction| DVec3::from_array(direction).try_normalize())
        .map(|direction| direction * crate::terrain::surface_radius_m(direction))
        .unwrap_or(position - normal * crate::scenario::settings().pad.half_extents_m[2]);
    Some(LandingSite {
        surface_position_m: surface.to_array(),
        normal: normal.to_array(),
        pad_position_m: pad.position,
        lander_position_m: (position
            + normal
                * (crate::scenario::settings().pad.lander_clearance_m
                    - crate::scenario::settings().pad.half_extents_m[2]))
            .to_array(),
        rotation: rotation.normalize().to_array(),
    })
}

pub(super) fn actual_preview(snapshot: &Snapshot, indices: ActorIndices) -> Option<Preview> {
    let site = actual_site(snapshot, indices)?;
    let direction = snapshot
        .launch_site_direction
        .and_then(|direction| DVec3::from_array(direction).try_normalize())
        .or_else(|| DVec3::from_array(site.surface_position_m).try_normalize())?
        .to_array();
    Some(Preview { direction, site })
}

pub(super) fn visible(ray: Ray, point: DVec3) -> bool {
    let offset = point - ray.origin;
    let distance = offset.dot(ray.direction);
    if !distance.is_finite() || distance < 0.0 {
        return false;
    }
    // Only compensate for conversion from Bevy's f32 render kilometres. This is
    // not a terrain-radius allowance, and cannot reveal an opposite-side pad.
    let tolerance_m = (distance * f64::from(f32::EPSILON) * 4.0).clamp(0.03, 0.5);
    Terrain::intersect(ray).is_none_or(|hit| hit.distance_m + tolerance_m >= distance)
}

#[derive(Clone, Copy, Debug)]
pub(super) struct Grip {
    pub pad_css: Vec2,
    pub handle_css: Vec2,
    pub pad_m: DVec3,
    pub handle_m: DVec3,
    pub right_m: DVec3,
    pub up_m: DVec3,
}

impl Grip {
    pub fn hit(&self, cursor: Vec2) -> bool {
        [self.pad_css, self.handle_css].into_iter().any(|point| {
            let delta = (cursor - point).abs();
            delta.x <= TARGET_HALF_PX && delta.y <= TARGET_HALF_PX
        })
    }
}

fn point_in_view_plane(
    camera: &Camera,
    global: &GlobalTransform,
    at: Vec2,
    point: DVec3,
) -> Option<DVec3> {
    let ray = viewport_ray(camera, global, at)?;
    let normal = global.forward().as_dvec3();
    let denominator = ray.direction.dot(normal);
    if denominator.abs() < 1.0e-12 {
        return None;
    }
    let distance = (point - ray.origin).dot(normal) / denominator;
    (distance >= 0.0 && distance.is_finite()).then_some(ray.origin + ray.direction * distance)
}

pub(super) fn grip(camera: &Camera, global: &GlobalTransform, site: LandingSite) -> Option<Grip> {
    viewport_ray(camera, global, camera.logical_viewport_rect()?.center())?;
    let pad_m = DVec3::from_array(site.pad_position_m);
    let pad_css = camera
        .world_to_viewport(global, render_position(pad_m))
        .ok()?;
    let pad_ray = viewport_ray(camera, global, pad_css)?;
    if !visible(pad_ray, pad_m) {
        return None;
    }
    let handle_css = pad_css - Vec2::Y * GRIP_HEIGHT_PX;
    let handle_m = point_in_view_plane(camera, global, handle_css, pad_m)?;
    let right_m = point_in_view_plane(camera, global, handle_css + Vec2::X, pad_m)? - handle_m;
    let up_m = point_in_view_plane(camera, global, handle_css - Vec2::Y, pad_m)? - handle_m;
    Some(Grip {
        pad_css,
        handle_css,
        pad_m,
        handle_m,
        right_m,
        up_m,
    })
}

pub(super) fn pad_hit(ray: Ray, site: LandingSite) -> bool {
    let rotation = DQuat::from_array(site.rotation);
    let origin = rotation.inverse() * (ray.origin - DVec3::from_array(site.pad_position_m));
    let direction = rotation.inverse() * ray.direction;
    let half = crate::scenario::settings().pad.half_extents_m;
    let (mut near, mut far) = (0.0_f64, f64::INFINITY);
    for axis in 0..3 {
        if direction[axis].abs() < 1.0e-12 {
            if origin[axis].abs() > half[axis] {
                return false;
            }
        } else {
            let a = (-half[axis] - origin[axis]) / direction[axis];
            let b = (half[axis] - origin[axis]) / direction[axis];
            near = near.max(a.min(b));
            far = far.min(a.max(b));
            if near > far {
                return false;
            }
        }
    }
    visible(ray, ray.origin + ray.direction * near)
}

pub(super) fn nudge(current: Preview, step: SurfaceStep) -> Result<Preview, String> {
    let radial = DVec3::from_array(current.direction)
        .try_normalize()
        .ok_or("Pad direction is unavailable. Choose a point on Phobos again.")?;
    let east = DVec3::new(-radial.z, 0.0, radial.x)
        .try_normalize()
        .unwrap_or(DVec3::Z);
    let north = east.cross(radial).normalize();
    let tangent = match step {
        SurfaceStep::North => north,
        SurfaceStep::South => -north,
        SurfaceStep::East => east,
        SurfaceStep::West => -east,
    };
    let radius = DVec3::from_array(current.site.surface_position_m).length();
    if !radius.is_finite() || radius <= 0.0 {
        return Err("Pad surface is unavailable. Choose a point on Phobos again.".into());
    }
    let angle = NUDGE_METRES / radius;
    let direction = (radial * angle.cos() + tangent * angle.sin())
        .normalize()
        .to_array();
    let site = crate::terrain::landing_site_at_direction(direction)?;
    Ok(Preview { direction, site })
}
