use bevy::math::DVec3;

#[derive(Clone, Copy, Debug)]
pub(super) struct Capsule {
    pub from: DVec3,
    pub to: DVec3,
    pub radius_m: f64,
}

/// Tangency is blocked. Radii already include the caller's clearance margin.
pub(super) fn segment_clear(from: DVec3, to: DVec3, obstacles: &[Capsule]) -> bool {
    let u = to - from;
    if !from.is_finite() || !to.is_finite() || !u.is_finite() {
        return false;
    }

    obstacles.iter().all(|obstacle| {
        if !obstacle.from.is_finite()
            || !obstacle.to.is_finite()
            || !obstacle.radius_m.is_finite()
            || obstacle.radius_m <= 0.0
        {
            return false;
        }

        let v = obstacle.to - obstacle.from;
        let w = from - obstacle.from;
        if !v.is_finite() || !w.is_finite() {
            return false;
        }

        // Scale before products, so long capsules and very short edges do not
        // overflow squared distances. Unrepresentable subtraction fails closed.
        let scale = u
            .abs()
            .max_element()
            .max(v.abs().max_element())
            .max(w.abs().max_element())
            .max(obstacle.radius_m);
        segments_separated_scaled(u / scale, v / scale, w / scale, obstacle.radius_m / scale)
    })
}

/// The first segment is [0, u], the second is [-w, v - w].
fn segments_separated_scaled(u: DVec3, v: DVec3, w: DVec3, radius: f64) -> bool {
    if !u.is_finite() || !v.is_finite() || !w.is_finite() || !radius.is_finite() {
        return false;
    }
    for axis in 0..3 {
        if separated_projection(w[axis], u[axis], v[axis], 1.0, radius) {
            return true;
        }
    }

    // A closest pair has an endpoint, or lies in both segment interiors.
    // Residuals give candidate separating axes, not unchecked distances.
    let residuals = [
        point_segment_residual(w, v),
        point_segment_residual(w + u, v),
        point_segment_residual(-w, u),
        point_segment_residual(v - w, u),
    ];
    let mut closest = residuals[0];
    for residual in &residuals[1..] {
        if residual.length_squared() < closest.length_squared() {
            closest = *residual;
        }
    }
    if separated_on_axis(closest, u, v, w, radius) {
        return true;
    }

    let normal = compensated_cross(u, v);
    let normal_scale = normal.abs().max_element();
    if normal_scale == 0.0 {
        return false;
    }
    let normal_axis = normal / normal_scale;

    // Do not divide by a poorly determined angle. In that case only the
    // endpoint and projection fallbacks below can establish separation.
    let angular_roundoff = 128.0 * f64::EPSILON * u.length() * v.length();
    if normal_scale > angular_roundoff {
        let denominator = dot(normal, normal_axis);
        let along_u = dot(compensated_cross(v, w), normal_axis) / denominator;
        let along_v = dot(compensated_cross(u, w), normal_axis) / denominator;
        if (0.0..=1.0).contains(&along_u) && (0.0..=1.0).contains(&along_v) {
            let residual = DVec3::new(
                u.x.mul_add(along_u, (-v.x).mul_add(along_v, w.x)),
                u.y.mul_add(along_u, (-v.y).mul_add(along_v, w.y)),
                u.z.mul_add(along_u, (-v.z).mul_add(along_v, w.z)),
            );
            if separated_on_axis(residual, u, v, w, radius) {
                return true;
            }
        }
    }

    // A rounded cross normal is not exactly perpendicular. Project BOTH
    // segments onto it instead of treating w.normal / |normal| as a distance.
    separated_on_axis(normal_axis, u, v, w, radius)
}

fn point_segment_residual(offset: DVec3, direction: DVec3) -> DVec3 {
    let scale = direction.abs().max_element();
    if scale == 0.0 {
        return offset;
    }
    let axis = direction / scale;
    let along = (dot(offset, axis) / dot(direction, axis)).clamp(0.0, 1.0);
    DVec3::new(
        (-direction.x).mul_add(along, offset.x),
        (-direction.y).mul_add(along, offset.y),
        (-direction.z).mul_add(along, offset.z),
    )
}

fn compensated_cross(left: DVec3, right: DVec3) -> DVec3 {
    fn difference_of_products(a: f64, b: f64, c: f64, d: f64) -> f64 {
        let product = c * d;
        a.mul_add(b, -product) + (-c).mul_add(d, product)
    }
    DVec3::new(
        difference_of_products(left.y, right.z, left.z, right.y),
        difference_of_products(left.z, right.x, left.x, right.z),
        difference_of_products(left.x, right.y, left.y, right.x),
    )
}

fn dot(left: DVec3, right: DVec3) -> f64 {
    left.x
        .mul_add(right.x, left.y.mul_add(right.y, left.z * right.z))
}

fn separated_on_axis(axis: DVec3, u: DVec3, v: DVec3, w: DVec3, radius: f64) -> bool {
    let scale = axis.abs().max_element();
    if !axis.is_finite() || scale == 0.0 {
        return false;
    }
    let axis = axis / scale;
    separated_projection(
        dot(w, axis),
        dot(u, axis),
        dot(v, axis),
        axis.length(),
        radius,
    )
}

fn separated_projection(
    base: f64,
    along_u: f64,
    along_v: f64,
    axis_length: f64,
    radius: f64,
) -> bool {
    if !base.is_finite()
        || !along_u.is_finite()
        || !along_v.is_finite()
        || !axis_length.is_finite()
        || !radius.is_finite()
    {
        return false;
    }
    let minimum = base + along_u.min(0.0) - along_v.max(0.0);
    let maximum = base + along_u.max(0.0) - along_v.min(0.0);
    // All geometry is scaled first, and the axis has maximum component one.
    // This allowance covers projection arithmetic, not an inverse-angle solve
    // or a probabilistic confidence bound. Ambiguous separation stays blocked.
    let roundoff = 256.0 * f64::EPSILON;
    minimum.max(-maximum) > radius * axis_length + roundoff
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sphere(center: DVec3, radius_m: f64) -> Capsule {
        Capsule {
            from: center,
            to: center,
            radius_m,
        }
    }

    #[test]
    fn empty_space_and_zero_length_edges() {
        assert!(segment_clear(DVec3::ZERO, DVec3::splat(10.0), &[]));
        let obstacles = [sphere(DVec3::ZERO, 2.0)];
        assert!(segment_clear(DVec3::X * 3.0, DVec3::X * 3.0, &obstacles));
        assert!(!segment_clear(DVec3::ZERO, DVec3::ZERO, &obstacles));
        assert!(!segment_clear(DVec3::X * 2.0, DVec3::X * 2.0, &obstacles));
    }

    #[test]
    fn sphere_clearance_checks_the_whole_segment_and_tangency() {
        let obstacles = [sphere(DVec3::ZERO, 2.0)];
        assert!(!segment_clear(
            DVec3::new(-10.0, 0.0, 0.0),
            DVec3::new(10.0, 0.0, 0.0),
            &obstacles,
        ));
        assert!(!segment_clear(
            DVec3::new(-10.0, 2.0, 0.0),
            DVec3::new(10.0, 2.0, 0.0),
            &obstacles,
        ));
        assert!(segment_clear(
            DVec3::new(-10.0, 2.001, 0.0),
            DVec3::new(10.0, 2.001, 0.0),
            &obstacles,
        ));
        assert!(!segment_clear(
            DVec3::splat(-5.0),
            DVec3::splat(5.0),
            &obstacles,
        ));
    }

    #[test]
    fn capsule_interior_parallel_and_endpoint_clearance() {
        let obstacles = [Capsule {
            from: DVec3::new(0.0, -4.0, 0.0),
            to: DVec3::new(0.0, 4.0, 0.0),
            radius_m: 1.0,
        }];
        assert!(!segment_clear(
            DVec3::new(-4.0, 0.0, 0.0),
            DVec3::new(4.0, 0.0, 0.0),
            &obstacles,
        ));
        assert!(!segment_clear(
            DVec3::new(1.0, -3.0, 0.0),
            DVec3::new(1.0, 3.0, 0.0),
            &obstacles,
        ));
        assert!(segment_clear(
            DVec3::new(1.01, -3.0, 0.0),
            DVec3::new(1.01, 3.0, 0.0),
            &obstacles,
        ));
        assert!(!segment_clear(
            DVec3::new(-3.0, 5.0, 0.0),
            DVec3::new(3.0, 5.0, 0.0),
            &obstacles,
        ));
        assert!(segment_clear(
            DVec3::new(-3.0, 5.01, 0.0),
            DVec3::new(3.0, 5.01, 0.0),
            &obstacles,
        ));
    }

    #[test]
    fn skew_and_near_parallel_segments_use_interior_distance() {
        let skew = [Capsule {
            from: DVec3::new(0.0, -5.0, 1.0),
            to: DVec3::new(0.0, 5.0, 1.0),
            radius_m: 1.0,
        }];
        assert!(!segment_clear(DVec3::NEG_X * 5.0, DVec3::X * 5.0, &skew));
        let near_parallel = [Capsule {
            from: DVec3::new(-10.0, -1e-7, 0.0),
            to: DVec3::new(10.0, 1e-7, 0.0),
            radius_m: 1e-10,
        }];
        assert!(!segment_clear(
            DVec3::NEG_X * 10.0,
            DVec3::X * 10.0,
            &near_parallel,
        ));
    }

    #[test]
    fn near_parallel_scalar_triple_tangent_regression() {
        let delta = 2.0_f64.powi(-12);
        let obstacle = Capsule {
            from: DVec3::new(-35.0, -16.0 - 3.0 * delta - 2.0_f64.powi(-47), -20.0),
            to: DVec3::new(29.0, 16.0 + delta, 28.0),
            radius_m: 5.0,
        };
        // The interior closest-pair residual is exactly (3, 0, -4).
        // A rounded cross normal previously overestimated its length by 4.66e-11.
        assert!(!segment_clear(
            DVec3::ZERO,
            DVec3::new(64.0, 32.0, 48.0),
            &[obstacle],
        ));
    }

    #[test]
    fn near_parallel_tangency_perturbations_and_endpoint_reversals() {
        let start = DVec3::ZERO;
        let goal = DVec3::new(64.0, 32.0, 48.0);
        for exponent in [-8, -12, -20, -32, -44] {
            let delta = 2.0_f64.powi(exponent);
            for noise in [-2.0_f64.powi(-47), 0.0, 2.0_f64.powi(-47)] {
                // These y perturbations change the interior parameters, but
                // leave the exact normal distance sqrt(3² + 4²) unchanged.
                let from = DVec3::new(-35.0, -16.0 - 3.0 * delta - noise, -20.0);
                let to = DVec3::new(29.0, 16.0 + delta, 28.0);
                for (query_from, query_to) in [(start, goal), (goal, start)] {
                    for (from, to) in [(from, to), (to, from)] {
                        for (radius_m, clear) in
                            [(5.0, false), (5.000_001, false), (4.999_999, true)]
                        {
                            assert_eq!(
                                segment_clear(
                                    query_from,
                                    query_to,
                                    &[Capsule { from, to, radius_m }]
                                ),
                                clear,
                                "exponent={exponent}, noise={noise}, radius={radius_m}",
                            );
                        }
                    }
                }
            }
        }
    }

    #[test]
    fn long_parallel_and_near_parallel_clearance_is_not_blanket_blocked() {
        for tilt in [0.0, 1e-6, 1e-10, 1e-14] {
            let obstacle = Capsule {
                from: DVec3::new(-6_000.0, 2.0, -tilt),
                to: DVec3::new(6_000.0, 2.0, tilt),
                radius_m: 1.5,
            };
            assert!(segment_clear(
                DVec3::NEG_X * 5_000.0,
                DVec3::X * 5_000.0,
                &[obstacle]
            ));
            assert!(segment_clear(
                DVec3::X * 5_000.0,
                DVec3::NEG_X * 5_000.0,
                &[obstacle]
            ));
        }
    }

    #[test]
    fn invalid_data_and_overflow_fail_closed() {
        for radius_m in [0.0, -1.0, f64::INFINITY, f64::NAN] {
            assert!(!segment_clear(
                DVec3::ZERO,
                DVec3::X,
                &[sphere(DVec3::Y * 10.0, radius_m)],
            ));
        }
        assert!(!segment_clear(DVec3::splat(f64::NAN), DVec3::X, &[]));
        assert!(!segment_clear(
            DVec3::ZERO,
            DVec3::X,
            &[sphere(DVec3::splat(f64::INFINITY), 1.0)],
        ));
        assert!(!segment_clear(
            DVec3::X * -f64::MAX,
            DVec3::X * f64::MAX,
            &[sphere(DVec3::ZERO, 1.0)],
        ));
        assert!(!segment_clear(
            DVec3::X * -f64::MAX,
            DVec3::X * f64::MAX,
            &[],
        ));
    }

    #[test]
    fn large_finite_geometry_does_not_square_world_distances() {
        let scale = 1e150;
        let obstacles = [sphere(DVec3::ZERO, scale)];
        assert!(segment_clear(
            DVec3::new(-2.0, 1.1, 0.0) * scale,
            DVec3::new(2.0, 1.1, 0.0) * scale,
            &obstacles,
        ));
        assert!(!segment_clear(
            DVec3::new(-2.0, 1.0, 0.0) * scale,
            DVec3::new(2.0, 1.0, 0.0) * scale,
            &obstacles,
        ));
    }
}
