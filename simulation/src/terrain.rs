use crate::scenario::settings;
use bevy::math::DVec3;
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
