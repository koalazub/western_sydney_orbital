//! Viewer-only framing of the real Phobos and Mars geometry.
use super::model::CameraRig;
use crate::{
    domain::{CameraMode, EntityId},
    model::Snapshot,
};
use bevy::prelude::*;

const KM: f32 = 0.001;

pub(super) fn update_viewport_aspect(
    camera: &Camera,
    window: Option<&Window>,
    projection: &mut Projection,
) {
    let size = camera
        .viewport
        .as_ref()
        .map(|viewport| viewport.physical_size)
        .or_else(|| window.map(Window::physical_size))
        .or_else(|| camera.physical_viewport_size());
    if let Some(size) = size
        && size.x > 0
        && size.y > 0
        && let Projection::Perspective(lens) = projection
    {
        // Use this frame's real viewport before Bevy's PostUpdate camera system.
        // Otherwise the first portrait frame uses the default square aspect.
        lens.aspect_ratio = size.x as f32 / size.y as f32;
    }
}

fn meters(position: [f64; 3]) -> Vec3 {
    Vec3::from_array(position.map(|value| (value * 0.001) as f32))
}

fn phobos_radius_km(snapshot: &Snapshot) -> f32 {
    crate::model::phobos_enclosing_radius_m()
        .max(snapshot.body.radii_m.into_iter().fold(0.0_f64, f64::max)) as f32
        * KM
}

pub(super) fn base_distance_km(snapshot: &Snapshot) -> f32 {
    // A roughly 23-degree Phobos disk leaves room for Mars' actual angular disk.
    phobos_radius_km(snapshot) * 5.0
}

fn orbit_normal(snapshot: &Snapshot) -> Vec3 {
    Vec3::from_array(
        snapshot
            .environment
            .angular_velocity_rad_s
            .map(|value| value as f32),
    )
    .normalize_or(Vec3::Y)
}

pub(super) fn frame(rig: &mut CameraRig, snapshot: &Snapshot) {
    let toward_mars = meters(snapshot.environment.mars_position_m).normalize_or(Vec3::X);
    let side = toward_mars
        .cross(orbit_normal(snapshot))
        .normalize_or(Vec3::Z);
    let (sine, cosine) = 20.0_f32.to_radians().sin_cos();
    // Observe from the far side of Phobos, offset enough to reveal Mars behind it.
    let outward = (-toward_mars * cosine + side * sine).normalize();
    rig.mode = CameraMode::Overview;
    rig.focus = EntityId::Phobos;
    rig.yaw = outward.x.atan2(outward.z);
    rig.pitch = outward.y.clamp(-1.0, 1.0).asin();
    rig.pan_offset = Vec3::ZERO;
    rig.zoom = 1.0;
    rig.distance_km = base_distance_km(snapshot);
}

pub(super) fn view(rig: &CameraRig, snapshot: &Snapshot, projection: &mut Projection) -> Transform {
    let offset = Vec3::new(
        rig.yaw.sin() * rig.pitch.cos(),
        rig.pitch.sin(),
        rig.yaw.cos() * rig.pitch.cos(),
    ) * rig.distance_km;
    let phobos_radius = phobos_radius_km(snapshot);
    let mars_center = meters(snapshot.environment.mars_position_m);
    let mars_radius = snapshot.environment.mars_radius_m as f32 * KM;
    let mut position = rig.pan_offset + offset;
    if position.length() < phobos_radius + 0.003 {
        position = position.normalize_or(Vec3::NEG_X) * (phobos_radius + 0.003);
    }
    let from_mars = position - mars_center;
    if from_mars.length() < mars_radius + 0.25 {
        position = mars_center + from_mars.normalize_or(Vec3::NEG_X) * (mars_radius + 0.25);
    }
    let to_phobos = rig.pan_offset - position;
    let to_mars = mars_center - position;
    let phobos_direction = to_phobos.normalize_or(Vec3::X);
    let mars_direction = to_mars.normalize_or(Vec3::X);
    let separation = phobos_direction.dot(mars_direction).clamp(-1.0, 1.0).acos();
    let phobos_half = (phobos_radius / to_phobos.length()).clamp(0.0, 1.0).asin();
    let mars_half = (mars_radius / to_mars.length()).clamp(0.0, 1.0).asin();
    let aim_offset = ((separation + mars_half - phobos_half) * 0.5).clamp(0.0, separation);
    let axis = phobos_direction
        .cross(mars_direction)
        .normalize_or(orbit_normal(snapshot));
    let forward = Quat::from_axis_angle(axis, aim_offset) * phobos_direction;
    if let Projection::Perspective(lens) = projection {
        // Fit both angular disks, including on portrait viewports. This never
        // changes either ship sensor's separate, fixed projection.
        let half_cone = (phobos_half + aim_offset).max(mars_half + separation - aim_offset);
        let padded_half = (half_cone * 1.08).min(87.0_f32.to_radians());
        let aspect = lens.aspect_ratio.max(0.05);
        let vertical_half = (padded_half.tan() / aspect.min(1.0)).atan();
        lens.fov = (vertical_half * 2.0).clamp(60.0_f32.to_radians(), 175.0_f32.to_radians());
        lens.far = lens.far.max((to_mars.length() + mars_radius) * 1.05);
    }
    let up = orbit_normal(snapshot);
    let up = if forward.cross(up).length_squared() < 1e-6 {
        Vec3::Z
    } else {
        up
    };
    Transform::from_translation(position).looking_to(forward, up)
}

#[cfg(all(test, not(target_arch = "wasm32")))]
mod tests {
    use super::*;
    use crate::physics::Simulation;
    use bevy::{
        camera::{CameraProjection, Viewport},
        window::WindowResolution,
    };

    fn assert_sphere_in_frame(
        transform: &Transform,
        lens: &PerspectiveProjection,
        center: Vec3,
        radius: f32,
    ) {
        let clip = lens.get_clip_from_view();
        let view_center = transform.rotation.inverse() * (center - transform.translation);
        assert!(view_center.z < -radius);
        let center_ndc = clip.project_point3(view_center);
        assert!(center_ndc.x.abs() < 1.0 && center_ndc.y.abs() < 1.0);
        let depth = -view_center.z;
        // Exact perspective silhouette bounds catch clipping between axis samples.
        for (coordinate, focal) in [
            (view_center.x, clip.x_axis.x),
            (view_center.y, clip.y_axis.y),
        ] {
            let tangent =
                radius * (depth * depth + coordinate * coordinate - radius * radius).sqrt();
            for sign in [-1.0, 1.0] {
                let edge = focal * (coordinate * depth + sign * tangent)
                    / (depth * depth - radius * radius);
                assert!(edge.abs() < 1.0, "silhouette outside frame: {edge}");
            }
        }
        // Real surface points, not a fabricated screen-space Mars position.
        for axis in [Vec3::X, Vec3::Y, Vec3::Z] {
            for sign in [-1.0, 1.0] {
                let point = center + axis * radius * sign;
                let view_point = transform.rotation.inverse() * (point - transform.translation);
                let ndc = clip.project_point3(view_point);
                assert!(ndc.x.abs() < 1.0 && ndc.y.abs() < 1.0, "{ndc:?}");
            }
        }
    }

    #[test]
    fn overview_frames_the_real_body_and_mars_disks_in_wide_and_portrait_projections() {
        let simulation = Simulation::new_default();
        let snapshot = simulation.snapshot();
        let before = serde_json::to_value(&snapshot).unwrap();
        for aspect in [0.5, 1.0, 16.0 / 9.0, 3.0] {
            let mut rig = CameraRig {
                focus: EntityId::Lander,
                pan_offset: Vec3::ONE,
                zoom: 7.0,
                ..default()
            };
            frame(&mut rig, &snapshot);
            assert_eq!(rig.mode, CameraMode::Overview);
            assert_eq!(rig.focus, EntityId::Phobos);
            assert_eq!(rig.pan_offset, Vec3::ZERO);
            assert_eq!(rig.zoom, 1.0);
            let mut projection = Projection::Perspective(PerspectiveProjection {
                fov: 10.0_f32.to_radians(),
                aspect_ratio: aspect,
                far: 20_000.0,
                ..default()
            });
            let transform = view(&rig, &snapshot, &mut projection);
            let Projection::Perspective(lens) = projection else {
                panic!("perspective overview")
            };
            let phobos_radius = phobos_radius_km(&snapshot);
            let mars = meters(snapshot.environment.mars_position_m);
            let mars_radius = snapshot.environment.mars_radius_m as f32 * KM;
            assert!(transform.translation.length() > phobos_radius);
            assert!(transform.translation.distance(mars) > mars_radius + 0.25);
            assert_sphere_in_frame(&transform, &lens, Vec3::ZERO, phobos_radius);
            assert_sphere_in_frame(&transform, &lens, mars, mars_radius);
            let to_phobos = -transform.translation;
            let to_mars = mars - transform.translation;
            let separation = to_phobos.normalize().angle_between(to_mars.normalize());
            let phobos_half = (phobos_radius / to_phobos.length()).asin();
            let mars_half = (mars_radius / to_mars.length()).asin();
            assert!(to_mars.length() - mars_radius > to_phobos.length() + phobos_radius);
            assert!(
                separation + mars_half > phobos_half,
                "Phobos must not hide all of Mars"
            );
            assert!(lens.far > to_mars.length() + mars_radius);
        }
        assert_eq!(serde_json::to_value(simulation.snapshot()).unwrap(), before);
    }

    #[test]
    fn startup_uses_the_real_viewport_and_preserves_projected_body_ratios_at_hidpi() {
        let snapshot = Simulation::new_default().snapshot();
        let before = serde_json::to_value(&snapshot).unwrap();
        let mut reference_ratio: Option<f32> = None;
        for (width, height) in [(1280, 720), (390, 844), (800, 800), (2520, 840)] {
            for scale in [1.0, 2.0, 3.0] {
                let window = Window {
                    resolution: WindowResolution::new(
                        (width as f32 * scale) as u32,
                        (height as f32 * scale) as u32,
                    )
                    .with_scale_factor_override(scale),
                    ..default()
                };
                let mut rig = CameraRig::default();
                assert_eq!(rig.mode, CameraMode::Overview);
                assert_eq!(rig.focus, EntityId::Phobos);
                let (transform, projection) =
                    crate::camera::initial_overview(&mut rig, &snapshot, Some(&window));
                let Projection::Perspective(lens) = projection else {
                    panic!("perspective overview")
                };
                assert_eq!(lens.aspect_ratio, width as f32 / height as f32);
                assert_eq!(lens.near, 0.0005);
                assert_eq!(lens.far, 20_000.0);
                let phobos_radius = phobos_radius_km(&snapshot);
                let mars = meters(snapshot.environment.mars_position_m);
                let mars_radius = snapshot.environment.mars_radius_m as f32 * KM;
                assert!(transform.translation.length() > phobos_radius);
                assert!(transform.translation.distance(mars) > mars_radius);
                assert_sphere_in_frame(&transform, &lens, Vec3::ZERO, phobos_radius);
                assert_sphere_in_frame(&transform, &lens, mars, mars_radius);
                let widths =
                    [(Vec3::ZERO, phobos_radius), (mars, mars_radius)].map(|(center, radius)| {
                        let relative = center - transform.translation;
                        let distance = relative.length();
                        // Rotate around projected camera-up to find horizontal
                        // limbs. Crossing with camera-up finds vertical limbs,
                        // whose equal x coordinates cannot measure disk width.
                        let axis = relative
                            .normalize()
                            .cross(transform.rotation * Vec3::X)
                            .normalize();
                        let tangent_distance = (distance * distance - radius * radius).sqrt();
                        let half_angle = (radius / distance).asin();
                        let clip = lens.get_clip_from_view();
                        let edge_x = [-1.0, 1.0].map(|sign| {
                            let ray = Quat::from_axis_angle(axis, sign * half_angle)
                                * relative.normalize();
                            let limb = transform.translation + ray * tangent_distance;
                            assert!((limb.distance(center) - radius).abs() < radius * 0.0001);
                            let view_point =
                                transform.rotation.inverse() * (limb - transform.translation);
                            clip.project_point3(view_point).x
                        });
                        (edge_x[1] - edge_x[0]).abs()
                    });
                let projected_ratio = widths[1] / widths[0];
                assert!(
                    widths.iter().all(|width| width.is_finite() && *width > 0.0),
                    "Invalid projected widths {widths:?}; viewport {width}x{height} scale {scale}, camera {transform:?}, lens {lens:?}"
                );
                // Mars' physical radius is hundreds of times Phobos', but its
                // actual separation gives a roughly twice-as-wide apparent disk.
                assert!(mars_radius / phobos_radius > 250.0);
                assert!((1.5..2.3).contains(&projected_ratio), "{projected_ratio}");
                if let Some(reference) = reference_ratio {
                    assert!((projected_ratio - reference).abs() < 1e-5);
                } else {
                    reference_ratio = Some(projected_ratio);
                }
            }
        }
        assert_eq!(serde_json::to_value(&snapshot).unwrap(), before);
    }

    #[test]
    fn an_explicit_camera_viewport_wins_over_the_full_window_shape() {
        let window = Window {
            resolution: WindowResolution::new(2560, 1440).with_scale_factor_override(2.0),
            ..default()
        };
        let camera = Camera {
            viewport: Some(Viewport {
                physical_position: UVec2::new(20, 20),
                physical_size: UVec2::new(600, 1200),
                ..default()
            }),
            ..default()
        };
        let mut projection = Projection::Perspective(PerspectiveProjection::default());
        update_viewport_aspect(&camera, Some(&window), &mut projection);
        let Projection::Perspective(lens) = projection else {
            panic!("perspective overview")
        };
        assert_eq!(lens.aspect_ratio, 0.5);
    }
}
