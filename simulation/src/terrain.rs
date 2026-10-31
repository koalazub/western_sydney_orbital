use crate::domain::FlightPreset;
use crate::scenario::settings;
use bevy::math::{DMat3, DQuat, DVec3};
use std::sync::OnceLock;

/// Radial intersection with the same two mesh triangles used for contacts.
/// Grid lookup avoids scanning the entire terrain for a nearby viewer camera.
pub fn surface_radius_m(direction: DVec3) -> f64 {
    static VERTICES: OnceLock<Vec<[f64; 3]>> = OnceLock::new();
    let vertices = VERTICES.get_or_init(|| phobos_geometry().0);
    let config = &settings().terrain;
    let ray = direction.normalize_or(DVec3::NEG_X);
    let scaled = (ray / DVec3::from_array(config.radii_m)).normalize();
    let lat = scaled.y.asin();
    let lon = scaled.z.atan2(scaled.x).rem_euclid(std::f64::consts::TAU);
    let row = (((lat + std::f64::consts::FRAC_PI_2) / std::f64::consts::PI
        * config.latitude_segments as f64) as usize)
        .min(config.latitude_segments - 1);
    let col = ((lon / std::f64::consts::TAU * config.longitude_segments as f64) as usize)
        .min(config.longitude_segments - 1);
    let a = row * (config.longitude_segments + 1) + col;
    let b = a + config.longitude_segments + 1;
    [[a, b, a + 1], [a + 1, b, b + 1]]
        .into_iter()
        .find_map(|indices| {
            let [v0, v1, v2] = indices.map(|index| DVec3::from_array(vertices[index]));
            let edge1 = v1 - v0;
            let edge2 = v2 - v0;
            let p = ray.cross(edge2);
            let determinant = edge1.dot(p);
            if determinant.abs() < 1e-10 {
                return None;
            }
            let t = -v0;
            let u = t.dot(p) / determinant;
            let q = t.cross(edge1);
            let v = ray.dot(q) / determinant;
            let distance = edge2.dot(q) / determinant;
            (u >= -1e-9 && v >= -1e-9 && u + v <= 1.0 + 1e-9 && distance > 0.0).then_some(distance)
        })
        .unwrap_or_else(|| {
            // At a degenerate polar triangle, use the mesh's shared pole vertex.
            DVec3::from_array(
                vertices[if row == 0 {
                    0
                } else {
                    config.latitude_segments * (config.longitude_segments + 1)
                }],
            )
            .length()
        })
}

/// Approximate Phobos terrain shared by the renderer and collision mesh.
pub fn phobos_geometry() -> (Vec<[f64; 3]>, Vec<[u32; 3]>) {
    let config = &settings().terrain;
    let (latitudes, longitudes) = (config.latitude_segments, config.longitude_segments);
    let radii = config.radii_m;
    let mut vertices = Vec::with_capacity((latitudes + 1) * (longitudes + 1));
    for latitude in 0..=latitudes {
        let lat = -std::f64::consts::FRAC_PI_2
            + std::f64::consts::PI * latitude as f64 / latitudes as f64;
        for longitude in 0..=longitudes {
            let lon = std::f64::consts::TAU * longitude as f64 / longitudes as f64;
            let direction = [lat.cos() * lon.cos(), lat.sin(), lat.cos() * lon.sin()];
            let mut relief = config.relief_amplitude
                * (config.relief_longitude_frequency * lon + config.relief_longitude_phase).sin()
                * (config.relief_latitude_frequency * lat).cos();
            for crater in &config.craters {
                let center_length = crater.center.iter().map(|x| x * x).sum::<f64>().sqrt();
                let cosine = direction
                    .iter()
                    .zip(crater.center)
                    .map(|(a, b)| a * b / center_length)
                    .sum::<f64>();
                let q = cosine.clamp(-1.0, 1.0).acos() / crater.width_rad;
                relief += crater.depth * (1.0 - q * q).max(0.0)
                    - (-((q - config.crater_rim_fraction) / config.crater_rim_width).powi(2)).exp()
                        * config.crater_rim_depth;
            }
            vertices.push(std::array::from_fn(|i| {
                direction[i] * radii[i] * (1.0 - relief)
            }));
        }
    }
    let mut triangles = Vec::with_capacity(2 * latitudes * longitudes);
    for latitude in 0..latitudes {
        for longitude in 0..longitudes {
            let a = (latitude * (longitudes + 1) + longitude) as u32;
            let b = a + (longitudes + 1) as u32;
            if latitude > 0 {
                triangles.push([a, b, a + 1]);
            }
            if latitude + 1 < latitudes {
                triangles.push([a + 1, b, b + 1]);
            }
        }
    }
    (vertices, triangles)
}

#[derive(Clone, Copy, Debug)]
pub struct LandingSite {
    pub surface_position_m: [f64; 3],
    pub normal: [f64; 3],
    pub pad_position_m: [f64; 3],
    pub lander_position_m: [f64; 3],
    pub rotation: [f64; 4],
}

/// Normalize before squaring, including subnormal and near-f64::MAX inputs.
pub(crate) fn normalized_surface_direction(direction: [f64; 3]) -> Result<[f64; 3], String> {
    if !direction.into_iter().all(f64::is_finite) {
        return Err("Launch site direction must contain three finite numbers.".into());
    }
    let largest = direction.into_iter().map(f64::abs).fold(0.0, f64::max);
    if largest == 0.0 {
        return Err("Launch site direction must be nonzero.".into());
    }
    // Float unit vectors need not square to exactly one. Keep the normalized
    // result fixed on re-entry so preview, commit and archive replay use exactly
    // the same ray and trial-start distance instead of drifting by one ULP.
    if (0.5..=1.0).contains(&largest) {
        let vector = DVec3::from_array(direction);
        if (vector.length_squared() - 1.0).abs() <= 4.0 * f64::EPSILON {
            return Ok(direction);
        }
    }
    Ok(
        DVec3::from_array(direction.map(|component| component / largest))
            .normalize()
            .to_array(),
    )
}

struct SurfaceMesh {
    vertices: Vec<DVec3>,
    triangles: Vec<[u32; 3]>,
}

fn surface_mesh() -> &'static SurfaceMesh {
    static MESH: OnceLock<SurfaceMesh> = OnceLock::new();
    MESH.get_or_init(|| {
        let (vertices, triangles) = phobos_geometry();
        SurfaceMesh {
            vertices: vertices.into_iter().map(DVec3::from_array).collect(),
            triangles,
        }
    })
}

/// Actual radial mesh intersection and one outward contact-face normal.
/// Edge/pole ties use mesh index order, never an averaged or ellipsoid normal.
/// The pad is raised only enough to clear the real mesh under its full footprint.
/// Preview and in-place placement deliberately share this calculation.
pub fn landing_site_at_direction(direction: [f64; 3]) -> Result<LandingSite, String> {
    let (point, normal) = mesh_surface_at_direction(direction)?;
    // Build an orthonormal frame rather than a near-antipodal rotation arc,
    // whose special-case 180-degree quaternion can miss the true face normal.
    let heading = if normal.y.abs() < 0.9 {
        DVec3::Y
    } else {
        DVec3::X
    };
    let tangent = heading.cross(normal).normalize();
    let rotation =
        DQuat::from_mat3(&DMat3::from_cols(tangent, normal.cross(tangent), normal)).normalize();
    let pad = settings().pad;
    let lift = footprint_clearance(surface_mesh(), point, rotation, pad.half_extents_m)?;
    Ok(LandingSite {
        surface_position_m: point.to_array(),
        normal: normal.to_array(),
        pad_position_m: (point + normal * (pad.half_extents_m[2] + lift)).to_array(),
        lander_position_m: (point + normal * (pad.lander_clearance_m + lift)).to_array(),
        rotation: rotation.to_array(),
    })
}

/// Robust mesh containment boundary for a verified shared, identity-pose terrain
/// collider. This does not use the old viewer-radius polar fallback.
pub(crate) fn mesh_surface_point_at_direction(direction: [f64; 3]) -> Result<[f64; 3], String> {
    mesh_surface_at_direction(direction).map(|(point, _)| point.to_array())
}

fn mesh_surface_at_direction(direction: [f64; 3]) -> Result<(DVec3, DVec3), String> {
    let ray = DVec3::from_array(normalized_surface_direction(direction)?);
    let mesh = surface_mesh();
    let mut hit: Option<(f64, DVec3, DVec3)> = None;
    for indices in &mesh.triangles {
        let [a, b, c] = indices.map(|index| mesh.vertices[index as usize]);
        let edge1 = b - a;
        let edge2 = c - a;
        let p = ray.cross(edge2);
        let determinant = edge1.dot(p);
        if determinant.abs() < 1.0e-10 {
            continue;
        }
        let t = -a;
        let u = t.dot(p) / determinant;
        let q = t.cross(edge1);
        let v = ray.dot(q) / determinant;
        let distance = edge2.dot(q) / determinant;
        if u < -1.0e-12 || v < -1.0e-12 || u + v > 1.0 + 1.0e-12 || distance <= 0.0 {
            continue;
        }
        // At shared vertices tiny ray/plane rounding differences must not pick
        // a different face on repeated normalized/archive round trips.
        if hit.is_some_and(|(previous, _, _)| distance >= previous - 1.0e-8) {
            continue;
        }
        let mut normal = edge1.cross(edge2).normalize();
        if normal.dot(ray) < 0.0 {
            normal = -normal;
        }
        // Trigonometric pole/seam vertices can differ by picometres. Resolve
        // edge roundoff onto the actual triangle instead of extending its plane
        // across that numerical seam or inventing a radial/spherical fallback.
        let weights = [(1.0 - u - v).max(0.0), u.max(0.0), v.max(0.0)];
        let total = weights.into_iter().sum::<f64>();
        let point = (a * weights[0] + b * weights[1] + c * weights[2]) / total;
        hit = Some((distance, point, normal));
    }
    let (_, point, normal) = hit.ok_or(
        "Launch site does not intersect the Phobos contact mesh. Try a different surface point.",
    )?;
    Ok((point, normal))
}

fn footprint_clearance(
    mesh: &SurfaceMesh,
    origin: DVec3,
    rotation: DQuat,
    half_extents: [f64; 3],
) -> Result<f64, String> {
    let inverse = rotation.inverse();
    let mut highest = 0.0_f64;
    for indices in &mesh.triangles {
        let triangle = indices.map(|index| inverse * (mesh.vertices[index as usize] - origin));
        if (0..2).any(|axis| {
            triangle
                .iter()
                .all(|point| point[axis] < -half_extents[axis])
                || triangle
                    .iter()
                    .all(|point| point[axis] > half_extents[axis])
        }) {
            continue;
        }
        // Clip the entire face, not just sampled corners. A rim or vertex inside
        // the footprint may stand above every corner and still bury a real pad.
        let mut polygon = triangle.to_vec();
        for (axis, sign) in [(0, -1.0), (0, 1.0), (1, -1.0), (1, 1.0)] {
            let mut clipped = Vec::with_capacity(polygon.len() + 1);
            if let Some(&last) = polygon.last() {
                let mut previous = last;
                let mut before = sign * previous[axis] - half_extents[axis];
                for &next in &polygon {
                    let after = sign * next[axis] - half_extents[axis];
                    if (before <= 0.0) != (after <= 0.0) {
                        clipped.push(previous.lerp(next, before / (before - after)));
                    }
                    if after <= 0.0 {
                        clipped.push(next);
                    }
                    previous = next;
                    before = after;
                }
            }
            polygon = clipped;
        }
        for point in polygon {
            highest = highest.max(point.z);
        }
    }
    // A numerical separation, not a smoothed terrain radius. Bound the real
    // height adjustment by the footprint diagonal and reject steeper sites.
    let lift = highest + 1.0e-5;
    if !lift.is_finite() || lift > half_extents[0].hypot(half_extents[1]) {
        return Err(
            "Launch site is too rough for the pad footprint. Try a different surface point.".into(),
        );
    }
    Ok(lift)
}

/// Uses opposite physical hemispheres, without rotating or relabeling the world.
pub fn landing_site_for(preset: FlightPreset) -> LandingSite {
    match preset {
        FlightPreset::FarSideL2 | FlightPreset::CompetitionReference => landing_site(),
        FlightPreset::MarsFacingL1 => {
            let (vertices, triangles) = phobos_geometry();
            let (index, surface) = vertices
                .iter()
                .copied()
                .enumerate()
                .min_by(|(_, a), (_, b)| {
                    b[0].total_cmp(&a[0])
                        .then_with(|| a[1].abs().total_cmp(&b[1]))
                        .then_with(|| a[2].abs().total_cmp(&b[2]))
                })
                .unwrap();
            let point = DVec3::from_array(surface);
            // Area-weighted normals of the actual contact faces at this vertex.
            // Include the shared longitude seam rather than taking a one-sided slope.
            let normal = triangles
                .into_iter()
                .filter(|triangle| {
                    triangle.iter().any(|&vertex| {
                        vertex as usize == index
                            || DVec3::from_array(vertices[vertex as usize]).distance_squared(point)
                                < 1.0e-16
                    })
                })
                .map(|triangle| {
                    let [a, b, c] =
                        triangle.map(|vertex| DVec3::from_array(vertices[vertex as usize]));
                    let face = (b - a).cross(c - a);
                    if face.dot(point) < 0.0 { -face } else { face }
                })
                .sum::<DVec3>()
                .normalize();
            LandingSite {
                surface_position_m: surface,
                normal: normal.to_array(),
                pad_position_m: (point + normal * settings().pad.half_extents_m[2]).to_array(),
                lander_position_m: (point + normal * settings().pad.lander_clearance_m).to_array(),
                rotation: DQuat::from_rotation_arc(DVec3::Z, normal).to_array(),
            }
        }
    }
}

pub fn landing_site() -> LandingSite {
    let (vertices, _) = phobos_geometry();
    let config = &settings().terrain;
    let (index, surface) = vertices
        .iter()
        .copied()
        .enumerate()
        .min_by(|(_, a), (_, b)| {
            a[0].total_cmp(&b[0])
                .then_with(|| a[1].abs().total_cmp(&b[1]))
                .then_with(|| a[2].abs().total_cmp(&b[2]))
        })
        .unwrap();
    let row = index / (config.longitude_segments + 1);
    let col = index % (config.longitude_segments + 1);
    let before_lat = vertices[row.saturating_sub(1) * (config.longitude_segments + 1) + col];
    let after_lat =
        vertices[(row + 1).min(config.latitude_segments) * (config.longitude_segments + 1) + col];
    let before_lon = vertices[row * (config.longitude_segments + 1) + col.saturating_sub(1)];
    let after_lon =
        vertices[row * (config.longitude_segments + 1) + (col + 1).min(config.longitude_segments)];
    let mut n = (DVec3::from_array(after_lat) - DVec3::from_array(before_lat))
        .cross(DVec3::from_array(after_lon) - DVec3::from_array(before_lon))
        .normalize();
    let point = DVec3::from_array(surface);
    if n.dot(point) < 0.0 {
        n = -n;
    }
    let normal = n.to_array();
    let dot = normal[2];
    let rotation = if dot > -0.999999 {
        let cross = [-normal[1], normal[0], 0.0];
        let s = ((1.0 + dot) * 2.0).sqrt();
        [cross[0] / s, cross[1] / s, cross[2] / s, s * 0.5]
    } else {
        [1.0, 0.0, 0.0, 0.0]
    };
    LandingSite {
        surface_position_m: surface,
        normal,
        pad_position_m: [
            surface[0] + normal[0] * settings().pad.half_extents_m[2],
            surface[1] + normal[1] * settings().pad.half_extents_m[2],
            surface[2] + normal[2] * settings().pad.half_extents_m[2],
        ],
        lander_position_m: [
            surface[0] + normal[0] * settings().pad.lander_clearance_m,
            surface[1] + normal[1] * settings().pad.lander_clearance_m,
            surface[2] + normal[2] * settings().pad.lander_clearance_m,
        ],
        rotation,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_preset_keeps_every_legacy_landing_value_exactly() {
        let original = landing_site();
        let default = landing_site_for(FlightPreset::default());
        assert_eq!(
            original.surface_position_m.map(f64::to_bits),
            default.surface_position_m.map(f64::to_bits)
        );
        assert_eq!(
            original.normal.map(f64::to_bits),
            default.normal.map(f64::to_bits)
        );
        assert_eq!(
            original.pad_position_m.map(f64::to_bits),
            default.pad_position_m.map(f64::to_bits)
        );
        assert_eq!(
            original.lander_position_m.map(f64::to_bits),
            default.lander_position_m.map(f64::to_bits)
        );
        assert_eq!(
            original.rotation.map(f64::to_bits),
            default.rotation.map(f64::to_bits)
        );
    }

    #[test]
    fn presets_use_real_mesh_vertices_on_opposite_physical_hemispheres() {
        let (vertices, _) = phobos_geometry();
        let minimum_x = vertices
            .iter()
            .map(|point| point[0])
            .fold(f64::INFINITY, f64::min);
        let maximum_x = vertices
            .iter()
            .map(|point| point[0])
            .fold(f64::NEG_INFINITY, f64::max);
        let far = landing_site_for(FlightPreset::FarSideL2);
        let near = landing_site_for(FlightPreset::MarsFacingL1);
        assert_eq!(far.surface_position_m[0], minimum_x);
        assert_eq!(near.surface_position_m[0], maximum_x);
        assert!(far.normal[0] < -0.9);
        assert!(near.normal[0] > 0.9);
        for site in [far, near] {
            assert!(vertices.contains(&site.surface_position_m));
            let point = DVec3::from_array(site.surface_position_m);
            assert!((surface_radius_m(point) - point.length()).abs() < 1.0e-7);
            let normal = DVec3::from_array(site.normal);
            assert!((normal.length() - 1.0).abs() < 1.0e-12);
            assert!(normal.dot(point) > 0.0);
        }
        // Relief changes the actual +X mesh normal, so it is not a display-only radial axis.
        assert!(
            DVec3::from_array(near.normal)
                .cross(DVec3::from_array(near.surface_position_m).normalize())
                .length()
                > 1.0e-5
        );
    }

    #[test]
    fn both_presets_put_the_body_up_axis_and_clearance_along_the_outward_normal() {
        for preset in [FlightPreset::MarsFacingL1, FlightPreset::FarSideL2] {
            let site = landing_site_for(preset);
            let point = DVec3::from_array(site.surface_position_m);
            let normal = DVec3::from_array(site.normal);
            let rotation = DQuat::from_array(site.rotation);
            assert!((rotation.length_squared() - 1.0).abs() < 1.0e-12);
            assert!((rotation * DVec3::Z - normal).length() < 1.0e-12);
            assert!(
                (DVec3::from_array(site.pad_position_m)
                    - point
                    - normal * settings().pad.half_extents_m[2])
                    .length()
                    < 1.0e-9
            );
            assert!(
                (DVec3::from_array(site.lander_position_m)
                    - point
                    - normal * settings().pad.lander_clearance_m)
                    .length()
                    < 1.0e-9
            );
        }
    }

    #[test]
    fn arbitrary_surface_sites_use_real_mesh_faces_at_latitudes_seams_edges_and_poles() {
        use rapier3d_f64::prelude::*;
        let mesh = surface_mesh();
        let shape = SharedShape::trimesh(
            mesh.vertices
                .iter()
                .map(|point| Vector::from_array(point.to_array()))
                .collect(),
            mesh.triangles.clone(),
        )
        .unwrap();
        let mut directions = vec![
            [1.0, 0.0, 0.0],
            [-1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, -1.0, 0.0],
            [0.0, 0.0, 1.0],
            [0.0, 0.0, -1.0],
            [1.0, 1.0e-14, -1.0e-14],
            [0.0, 1.0, 1.0e-14],
        ];
        for lat in [-1.45_f64, -0.8, -0.3, 0.0, 0.5, 1.1, 1.45] {
            for lon in [-3.0_f64, -1.8, -0.5, 0.0, 1.2, 2.5, std::f64::consts::TAU] {
                directions.push([lat.cos() * lon.cos(), lat.sin(), lat.cos() * lon.sin()]);
            }
        }
        for &index in &[0, 13, 127, 1000, 2000, 3000, mesh.triangles.len() - 1] {
            let [a, b, c] = mesh.triangles[index].map(|index| mesh.vertices[index as usize]);
            directions.extend([
                a.to_array(),
                ((a + b) * 0.5).to_array(),
                ((a + b + c) / 3.0).to_array(),
            ]);
        }
        for direction in directions {
            let site = landing_site_at_direction(direction).unwrap();
            let point = DVec3::from_array(site.surface_position_m);
            let normal = DVec3::from_array(site.normal);
            let rotation = DQuat::from_array(site.rotation);
            let ray = Vector::from_array(normalized_surface_direction(direction).unwrap());
            let actual_point = Vector::from_array(site.surface_position_m);
            assert!(shape.distance_to_point(&Pose::identity(), actual_point, false) < 1.0e-7);
            if let Some(actual) = shape.cast_ray(
                &Pose::identity(),
                &Ray::new(Vector::ZERO, ray),
                30_000.0,
                false,
            ) {
                assert!(
                    (point.length() - actual).abs() < 1.0e-6,
                    "direction {direction:?}"
                );
            }
            assert!(point.cross(DVec3::from_array(ray.to_array())).length() < 1.0e-8);
            assert!(normal.is_finite() && (normal.length() - 1.0).abs() < 1.0e-12);
            assert!(normal.dot(point) > 0.0);
            assert!(rotation.is_finite() && (rotation.length_squared() - 1.0).abs() < 1.0e-12);
            assert!((rotation * DVec3::Z - normal).length() < 1.0e-12);
            assert!(
                mesh.triangles.iter().any(|indices| {
                    let [a, b, c] = indices.map(|index| mesh.vertices[index as usize]);
                    let face = (b - a).cross(c - a).normalize();
                    let face = if face.dot(point) < 0.0 { -face } else { face };
                    (face - normal).length() < 1.0e-10 && (point - a).dot(face).abs() < 1.0e-6
                }),
                "normal must be an actual contact face at {direction:?}"
            );
            let replay =
                landing_site_at_direction(normalized_surface_direction(direction).unwrap())
                    .unwrap();
            assert_eq!(
                site.surface_position_m.map(f64::to_bits),
                replay.surface_position_m.map(f64::to_bits)
            );
            assert_eq!(
                site.pad_position_m.map(f64::to_bits),
                replay.pad_position_m.map(f64::to_bits)
            );
            assert_eq!(
                site.rotation.map(f64::to_bits),
                replay.rotation.map(f64::to_bits)
            );
        }
    }

    #[test]
    fn surface_direction_normalization_handles_large_tiny_and_invalid_inputs() {
        let tiny = f64::from_bits(1);
        for direction in [
            [f64::MAX, -f64::MAX, f64::MAX],
            [tiny, tiny, -tiny],
            [tiny, 0.0, 0.0],
            [1.0e-200, -3.0e-200, 2.0e-200],
        ] {
            let normalized = normalized_surface_direction(direction).unwrap();
            assert!(normalized.into_iter().all(f64::is_finite));
            assert!((DVec3::from_array(normalized).length() - 1.0).abs() < 1.0e-12);
            assert_eq!(
                normalized,
                normalized_surface_direction(normalized).unwrap()
            );
            assert!(landing_site_at_direction(direction).is_ok());
        }
        for direction in [
            [0.0; 3],
            [-0.0; 3],
            [f64::NAN, 0.0, 1.0],
            [1.0, f64::INFINITY, 0.0],
            [0.0, 1.0, f64::NEG_INFINITY],
        ] {
            assert!(landing_site_at_direction(direction).is_err());
        }
    }

    #[test]
    fn rough_edge_footprints_clear_the_complete_real_mesh_instead_of_only_the_center() {
        use rapier3d_f64::{parry::query::intersection_test, prelude::*};
        let mesh = surface_mesh();
        let shape = SharedShape::trimesh(
            mesh.vertices
                .iter()
                .map(|point| Vector::from_array(point.to_array()))
                .collect(),
            mesh.triangles.clone(),
        )
        .unwrap();
        let half = settings().pad.half_extents_m;
        let pad = SharedShape::cuboid(half[0], half[1], half[2]);
        let mut adjusted = 0;
        for &index in &[13, 127, 400, 1000, 2000, 3000] {
            let [a, b, _] = mesh.triangles[index].map(|index| mesh.vertices[index as usize]);
            let site = landing_site_at_direction(((a + b) * 0.5).to_array()).unwrap();
            let q = Rotation::from_array(site.rotation);
            let position = Vector::from_array(site.pad_position_m);
            let pose = Pose::from_parts(position, q);
            assert!(
                !intersection_test(&pose, pad.as_ref(), &Pose::identity(), shape.as_ref())
                    .unwrap()
                    .intersecting
            );
            let unadjusted = Vector::from_array(site.surface_position_m)
                + Vector::from_array(site.normal) * half[2];
            if (position - unadjusted).length() > 1.0e-3 {
                adjusted += 1;
                assert!(
                    intersection_test(
                        &Pose::from_parts(unadjusted, q),
                        pad.as_ref(),
                        &Pose::identity(),
                        shape.as_ref()
                    )
                    .unwrap()
                    .intersecting
                );
            }
        }
        assert!(
            adjusted > 0,
            "at least one actual edge must need footprint clearance"
        );
    }
}
