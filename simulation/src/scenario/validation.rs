use super::model::{OfficialDebrisClassId, OfficialRadarSettings, Scenario, Terrain};
use crate::domain::{FlightPreset, ShipCameraId};

pub(super) fn validate(s: &Scenario) -> Result<(), &'static str> {
    s.experiment_settings
        .validate()
        .map_err(|_| "invalid reset-only experiment defaults")?;
    s.legacy_corridor_flow_reference
        .validate()
        .map_err(|_| "invalid archived corridor flow calibration reference")?;
    s.impact_ejecta_defaults
        .validate()
        .map_err(|_| "invalid impact ejecta defaults")?;
    validate_official_cloud(s)?;
    validate_transit_and_adjustment(s)?;
    validate_flight_presets(s)?;
    validate_limits(s)?;
    validate_gravity(s)?;
    validate_integration(s)?;
    validate_lander(s)?;
    validate_debug_thrust(s)?;
    validate_practice(s)?;
    validate_pad(s)?;
    validate_mission(s)?;
    validate_debris(s)?;
    validate_sensors(s)?;
    validate_ship_cameras(s)?;
    validate_lighting(s)?;
    validate_terrain(&s.terrain, s.gravity.phobos_radii_m)?;
    validate_contacts(s)?;
    Ok(())
}

fn validate_transit_and_adjustment(s: &Scenario) -> Result<(), &'static str> {
    let transit = s.transit_stream;
    if transit.version != 1
        || !bounded_positive(
            &[
                transit.transverse_half_width_m,
                transit.launch_trail_m,
                transit.target_lead_m,
            ],
            50_000.0,
        )
        || transit.transverse_half_width_m <= 101.0
    {
        return Err("invalid transit stream route volume");
    }
    let adjustment = &s.debris_trajectory_adjustment;
    if adjustment.version != 1
        || adjustment.source_enabled_by_default
        || !adjustment.source_url.starts_with("https://")
        || adjustment.scheduling_note.trim().is_empty()
        || !bounded_positive(&[adjustment.global_rate_hz], 120.0)
        || !bounded_positive(&[adjustment.cutoff_m], 10_000.0)
        || !bounded_positive(&[adjustment.typical_speed_mps], 200.0)
        || !bounded_positive(&[adjustment.delta_v_factor], 0.1)
    {
        return Err("invalid source-derived debris trajectory adjustment constants");
    }
    Ok(())
}

fn validate_limits(s: &Scenario) -> Result<(), &'static str> {
    let l = s.limits;
    if s.default_seed > u32::MAX as u64
        || s.debris.count == 0
        || s.debris.count > crate::model::MAX_DEBRIS_COUNT
        || s.debris.radar_count > s.debris.count
        || l.events == 0
        || l.contacts == 0
        || l.observations < s.debris.radar_count
        || !positive(&[l.position_m, l.velocity_mps, l.mass_kg, l.radius_m])
        || s.description.trim().is_empty()
        || s.frame_description.trim().is_empty()
        || s.experiment_description.trim().is_empty()
        || s.directed_stream_description.trim().is_empty()
    {
        return Err("invalid scenario limits or metadata");
    }
    Ok(())
}

fn validate_flight_presets(s: &Scenario) -> Result<(), &'static str> {
    for preset in FlightPreset::ALL {
        let config = s.flight_preset(preset);
        if config.label.trim().is_empty()
            || config.target_name.trim().is_empty()
            || config.launch_side != preset.launch_side()
            || config.target_region != preset.target_region()
        {
            return Err(
                "flight preset labels, physical launch sides, or target regions are invalid",
            );
        }
    }
    if s.flight_preset(FlightPreset::CompetitionReference)
        .target_distance_m
        != Some(1305.8)
    {
        return Err("competition reference target must be exactly 1305.8 m");
    }
    for preset in [FlightPreset::MarsFacingL1, FlightPreset::FarSideL2] {
        if s.flight_preset(preset).target_distance_m.is_some() {
            return Err("collinear flight presets must not carry a local-up target distance");
        }
    }
    let legacy = s.flight_preset(FlightPreset::FarSideL2);
    if legacy.target_region != s.mission.target_region
        || legacy.target_name != s.mission.target_name
    {
        return Err("far-side L2 preset must retain the archived mission target");
    }
    Ok(())
}

fn validate_official_cloud(s: &Scenario) -> Result<(), &'static str> {
    let cloud = &s.official_cloud;
    if cloud.version != 1
        || cloud.count != crate::model::OFFICIAL_CLOUD_COUNT
        || cloud.half_extents_m != [300.0, 300.0, 500.0]
        || cloud.vertical_offset_m != 500.0
        || cloud.drift_reference_mu_m3_s2 != 707_500.0
        || cloud.drift_reference_radius_m != 11_100.0
        || cloud.drift_speed_fraction_min != 0.05
        || cloud.drift_speed_fraction_max != 0.1
        || cloud.mass_scale_exponent != 3.0
        || cloud.collision_radius_max_m != 20.0
    {
        return Err("official debris cloud constants differ from the source profile");
    }
    let expected_classes = [
        (OfficialDebrisClassId::Rock1, 0.45, 0.08, 10.0, 400.0, 0.63),
        (OfficialDebrisClassId::Rock2, 0.35, 0.05, 20.0, 800.0, 0.3),
        (OfficialDebrisClassId::Rock3, 0.15, 0.01, 40.0, 1_000.0, 0.2),
        (
            OfficialDebrisClassId::ExplodedTank,
            0.05,
            17.0,
            1.0,
            1.0,
            700.0,
        ),
    ];
    if cloud.classes.len() != expected_classes.len()
        || cloud
            .classes
            .iter()
            .zip(expected_classes)
            .any(|(class, expected)| {
                class.id != expected.0
                    || class.weight != expected.1
                    || class.radar_diameter_m != expected.2
                    || class.scale_min != expected.3
                    || class.scale_max != expected.4
                    || class.base_mass_kg != expected.5
            })
    {
        return Err("official debris class weights, scales, sizes, or masses are invalid");
    }
    let maximum_radius = cloud
        .classes
        .iter()
        .map(|class| class.radius_m(class.scale_max))
        .fold(0.0_f64, f64::max);
    if (maximum_radius - cloud.collision_radius_max_m).abs() > 1.0e-9 {
        return Err("official debris collision radius cap does not match the class sizes");
    }
    validate_official_radar(cloud.radar, s.debris.radar_count)
}

fn validate_official_radar(
    radar: OfficialRadarSettings,
    scenario_radar_count: usize,
) -> Result<(), &'static str> {
    if radar.version != 1
        || radar.observed_count != 200
        || radar.iteration_hz != 120.0
        || radar.publish_every_rows != 50
        || radar.observed_count > crate::model::OFFICIAL_CLOUD_COUNT
        || radar.publish_every_rows > radar.observed_count
        || scenario_radar_count != radar.observed_count
    {
        return Err("official radar schedule must observe 200 rows at 120 Hz and publish every 50");
    }
    Ok(())
}

fn validate_gravity(s: &Scenario) -> Result<(), &'static str> {
    let g = s.gravity;
    if !bounded_positive(
        &[
            g.mars_mu_m3_s2,
            g.phobos_mu_m3_s2,
            g.separation_m,
            g.mars_radius_m,
            g.phobos_rotation_period_s,
        ],
        1.0e18,
    ) || !bounded_positive(&g.phobos_radii_m, 1.0e8)
    {
        return Err("gravity parameters must be finite and positive");
    }
    Ok(())
}

fn validate_integration(s: &Scenario) -> Result<(), &'static str> {
    if !positive(&[
        s.integration.fixed_step_s,
        s.integration.maximum_command_step_s,
    ]) || s.integration.fixed_step_s > s.integration.maximum_command_step_s
        || s.integration.fixed_step_s > 1.0
        || s.integration.maximum_command_step_s > 300.0
    {
        return Err("integration steps are outside configured bounds");
    }
    Ok(())
}

fn validate_lander(s: &Scenario) -> Result<(), &'static str> {
    let l = s.lander;
    let matrix = l.inertia_kg_m2;
    let symmetric = (0..3).all(|r| (0..3).all(|c| (matrix[r][c] - matrix[c][r]).abs() <= 1.0e-10));
    let minor_2 = matrix[0][0] * matrix[1][1] - matrix[0][1] * matrix[1][0];
    let determinant = matrix[0][0] * (matrix[1][1] * matrix[2][2] - matrix[1][2] * matrix[2][1])
        - matrix[0][1] * (matrix[1][0] * matrix[2][2] - matrix[1][2] * matrix[2][0])
        + matrix[0][2] * (matrix[1][0] * matrix[2][1] - matrix[1][1] * matrix[2][0]);
    if !bounded_positive(
        &[
            l.mass_kg,
            l.max_accel_mps2,
            l.max_angular_accel_rad_s2,
            l.specific_impulse_s,
            l.standard_gravity_mps2,
            l.proxy_radius_m,
            l.foot_radius_m,
        ],
        1.0e12,
    ) || !l
        .inertia_kg_m2
        .iter()
        .flatten()
        .all(|value| value.is_finite() && value.abs() <= 1.0e15)
        || !symmetric
        || matrix[0][0] <= 0.0
        || !minor_2.is_finite()
        || !determinant.is_finite()
        || minor_2 <= 0.0
        || determinant <= 0.0
        || !bounded_positive(&l.hull_half_extents_m, 1.0e4)
        || !bounded_positive(&l.leg_half_extents_m, 1.0e4)
        || !l
            .leg_positions_m
            .iter()
            .flatten()
            .all(|value| value.is_finite() && value.abs() <= 1.0e5)
        || !l
            .manual_nozzles_body_m
            .iter()
            .flatten()
            .all(|value| value.is_finite() && value.abs() <= l.proxy_radius_m * 2.0)
    {
        return Err("lander dimensions, controls, mass, or inertia are invalid");
    }
    Ok(())
}

fn validate_debug_thrust(s: &Scenario) -> Result<(), &'static str> {
    let visuals = s.debug_thrust;
    if !(1..=4096).contains(&visuals.max_particles)
        || !bounded_positive(&[visuals.particles_per_second_per_jet], 1000.0)
        || !bounded_positive(&[visuals.particle_lifetime_s], 5.0)
        || !bounded_positive(&[visuals.exhaust_speed_mps], 10_000.0)
        || !bounded_positive(&[visuals.particle_radius_m], 2.0)
        || !visuals.spread_rad.is_finite()
        || !(0.0..=0.8).contains(&visuals.spread_rad)
    {
        return Err("debug exhaust settings must be finite and bounded");
    }
    Ok(())
}

fn validate_practice(s: &Scenario) -> Result<(), &'static str> {
    if !bounded_positive(&[s.practice.space_lift_accel_mps2], 20.0)
        || s.practice.space_lift_accel_mps2 < s.lander.max_accel_mps2
    {
        return Err("practice Space assistance must be finite and at least the reference limit");
    }
    Ok(())
}

fn validate_mission(s: &Scenario) -> Result<(), &'static str> {
    let m = &s.mission;
    if m.target_name.trim().is_empty()
        || !bounded_positive(
            &[
                m.target_mass_kg,
                m.target_radius_m,
                m.arrival_radius_m,
                m.impact_threshold_j,
                m.nominal_fuel_kg,
                m.nominal_time_s,
            ],
            1.0e15,
        )
        || !m.score_offset.is_finite()
        || m.score_offset != 0.5
        || m.arrival_radius_m <= m.target_radius_m
        || m.arrival_radius_m != 50.0
        || m.impact_threshold_j != 209_200.0
        || m.nominal_fuel_kg != 75.0
        || m.nominal_time_s != 120.0
    {
        return Err("mission settings are invalid or differ from the official metadata");
    }
    Ok(())
}

fn validate_pad(s: &Scenario) -> Result<(), &'static str> {
    if !positive(&s.pad.half_extents_m)
        || !positive(&[s.pad.lander_clearance_m, s.pad.fixed_mass_kg])
        || !bounded_positive(&s.pad.half_extents_m, 1.0e5)
        || s.pad.lander_clearance_m <= s.lander.leg_half_extents_m[2] + s.lander.foot_radius_m
    {
        return Err("landing pad and deployed-foot geometry are invalid");
    }
    Ok(())
}

fn validate_debris(s: &Scenario) -> Result<(), &'static str> {
    let d = s.debris;
    if !finite(&[
        d.x_min_m,
        d.x_span_m,
        d.y_half_width_m,
        d.z_half_width_m,
        d.speed_min_mps,
        d.speed_span_mps,
        d.radius_min_m,
        d.radius_span_m,
        d.density_kg_m3,
        d.spin_max_rad_s,
        s.sensors.range_noise_m,
        s.sensors.range_rate_noise_mps,
        d.diameter_noise_fraction,
    ]) || d.x_min_m <= 0.0
        || d.x_span_m < 0.0
        || d.y_half_width_m < 0.0
        || d.z_half_width_m < 0.0
        || d.speed_min_mps < 0.0
        || d.speed_span_mps < 0.0
        || d.radius_min_m <= 0.0
        || d.radius_span_m < 0.0
        || d.density_kg_m3 <= 0.0
        || d.spin_max_rad_s < 0.0
        || s.sensors.range_noise_m < 0.0
        || s.sensors.range_rate_noise_mps < 0.0
        || d.diameter_noise_fraction < 0.0
        || d.x_min_m > 1.0e8
        || d.x_span_m > 1.0e8
        || d.y_half_width_m > 1.0e8
        || d.z_half_width_m > 1.0e8
        || d.radius_span_m > 1.0e5
        || d.density_kg_m3 > 1.0e8
    {
        return Err("debris parameters or sensor noise are outside configured bounds");
    }
    Ok(())
}

fn validate_sensors(s: &Scenario) -> Result<(), &'static str> {
    if !bounded_positive(&[s.sensors.cadence_hz], 1.0e6)
        || s.sensors.range_noise_m > 1.0e8
        || s.sensors.range_rate_noise_mps > 1.0e8
    {
        return Err("sensor cadence must be finite and positive");
    }
    Ok(())
}

fn validate_ship_cameras(s: &Scenario) -> Result<(), &'static str> {
    let cameras = &s.ship_cameras;
    if cameras.resolution_px != [512, 512]
        || !cameras.field_of_view_deg.is_finite()
        || cameras.field_of_view_deg <= 0.0
        || cameras.field_of_view_deg >= 179.0
        || !bounded_positive(&[cameras.near_clip_m], 1_000.0)
        || !bounded_positive(&[cameras.far_clip_m], 1.0e8)
        || cameras.far_clip_m <= cameras.near_clip_m
        || !bounded_positive(&[cameras.cadence_hz], 1_000.0)
        || !cameras.exposure_ev100.is_finite()
        || !(-10.0..=25.0).contains(&cameras.exposure_ev100)
        || !cameras.ambient_brightness.is_finite()
        || !(0.0..=1.0).contains(&cameras.ambient_brightness)
    {
        return Err("ship camera optics, fixed exposure, or cadence are outside configured bounds");
    }
    for source in [&cameras.field_of_view_source, &cameras.mount_source] {
        if !source.url.starts_with("https://") || source.note.trim().is_empty() {
            return Err("ship camera parameters must identify their source and limitations");
        }
    }
    for (mount, expected) in cameras
        .mounts
        .iter()
        .zip([ShipCameraId::Top, ShipCameraId::Bottom])
    {
        if mount.id != expected
            || !mount
                .position_m
                .iter()
                .all(|value| value.is_finite() && value.abs() <= 1_000.0)
            || !valid_camera_aim(mount.aim_yaw_deg, mount.yaw_limits_deg, 180.0)
            || !valid_camera_aim(mount.aim_pitch_deg, mount.pitch_limits_deg, 90.0)
        {
            return Err(
                "ship camera mounts must be ordered top/bottom with finite positions and bounded aim",
            );
        }
    }
    Ok(())
}

fn valid_camera_aim(angle: f32, limits: [f32; 2], maximum: f32) -> bool {
    angle.is_finite()
        && limits
            .iter()
            .all(|value| value.is_finite() && value.abs() <= maximum)
        && limits[0] < limits[1]
        && (limits[0]..=limits[1]).contains(&angle)
}

fn validate_lighting(s: &Scenario) -> Result<(), &'static str> {
    let lighting = s.lighting;
    let direction = lighting.direction_to_sun.map(f64::from);
    if !finite(&direction)
        || (dot(direction, direction) - 1.0).abs() > 1.0e-5
        || !bounded_positive(&[f64::from(lighting.illuminance_lux)], 200_000.0)
        || !bounded_positive(&[f64::from(lighting.sun_angular_radius_rad)], 0.05)
        || !bounded_positive(&[lighting.sun_proxy_distance_m], 1.0e8)
        || !lighting.debug_ambient_brightness.is_finite()
        || !(0.0..=100_000.0).contains(&lighting.debug_ambient_brightness)
    {
        return Err("solar direction, illuminance, angular radius, or debug ambient is invalid");
    }
    let proxy_radius_m =
        lighting.sun_proxy_distance_m * f64::from(lighting.sun_angular_radius_rad).sin();
    let maximum_mount_offset_m = s
        .ship_cameras
        .mounts
        .iter()
        .map(|mount| dot(mount.position_m, mount.position_m).sqrt())
        .fold(0.0_f64, f64::max);
    if lighting.sun_proxy_distance_m - proxy_radius_m - maximum_mount_offset_m
        <= s.gravity.separation_m + s.gravity.mars_radius_m
        || lighting.sun_proxy_distance_m + proxy_radius_m + maximum_mount_offset_m
            >= s.ship_cameras.far_clip_m
    {
        return Err("Sun background proxy must sit beyond Mars and inside both sensors' far clip");
    }
    for surface in [
        lighting.mars_surface,
        lighting.phobos_surface,
        lighting.debris_surface,
    ] {
        if !surface
            .base_color_srgb
            .iter()
            .all(|value| value.is_finite() && (0.0..=1.0).contains(value))
            || !surface.perceptual_roughness.is_finite()
            || !(0.7..=1.0).contains(&surface.perceptual_roughness)
        {
            return Err("rock surface colors and roughness are outside configured bounds");
        }
    }
    Ok(())
}

fn validate_contacts(s: &Scenario) -> Result<(), &'static str> {
    if !finite(&[s.contacts.friction, s.contacts.restitution])
        || s.contacts.friction < 0.0
        || !(0.0..=1.0).contains(&s.contacts.restitution)
    {
        return Err("contact material settings are outside configured bounds");
    }
    Ok(())
}

fn validate_terrain(t: &Terrain, body_radii: [f64; 3]) -> Result<(), &'static str> {
    let surface_values = [
        t.relief_amplitude,
        t.relief_longitude_frequency,
        t.relief_latitude_frequency,
        t.relief_longitude_phase,
        t.crater_rim_fraction,
        t.crater_rim_width,
        t.crater_rim_depth,
    ];
    let valid_craters = t.craters.iter().all(|crater| {
        finite(&crater.center)
            && crater.center.iter().any(|value| *value != 0.0)
            && positive(&[crater.width_rad])
            && crater.depth.is_finite()
            && crater.depth >= 0.0
    });
    if t.radii_m != body_radii
        || !bounded_positive(&t.radii_m, 1.0e8)
        || !(4..=256).contains(&t.latitude_segments)
        || !(8..=512).contains(&t.longitude_segments)
        || !finite(&surface_values)
        || t.crater_rim_width <= 0.0
        || !valid_craters
    {
        return Err("terrain geometry is outside configured bounds");
    }
    Ok(())
}

fn finite(values: &[f64]) -> bool {
    values.iter().all(|value| value.is_finite())
}

fn positive(values: &[f64]) -> bool {
    finite(values) && values.iter().all(|value| *value > 0.0)
}

fn bounded_positive(values: &[f64], maximum: f64) -> bool {
    positive(values) && values.iter().all(|value| *value <= maximum)
}

fn dot(a: [f64; 3], b: [f64; 3]) -> f64 {
    a.into_iter().zip(b).map(|(a, b)| a * b).sum()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn launch_presets_keep_opposite_hemispheres_and_the_legacy_l2_target() {
        let scenario = crate::scenario::settings();
        validate_flight_presets(scenario).unwrap();
        assert_eq!(
            scenario
                .flight_preset(FlightPreset::MarsFacingL1)
                .launch_side,
            crate::domain::LaunchSide::MarsFacing
        );
        assert_eq!(
            scenario.flight_preset(FlightPreset::FarSideL2).target_name,
            scenario.mission.target_name
        );
        let competition = scenario.flight_preset(FlightPreset::CompetitionReference);
        assert_eq!(competition.label, "Competition reference · 1.31 km hop");
        assert_eq!(competition.launch_side, crate::domain::LaunchSide::FarSide);
        assert_eq!(
            competition.target_region,
            crate::domain::LagrangeRegion::LocalUp
        );
        assert_eq!(competition.target_distance_m, Some(1305.8));
        let mut invalid = scenario.clone();
        invalid.flight_presets.far_side_l2.launch_side = crate::domain::LaunchSide::MarsFacing;
        assert!(validate_flight_presets(&invalid).is_err());
        invalid = scenario.clone();
        invalid.flight_presets.mars_facing_l1.target_region = crate::domain::LagrangeRegion::L2;
        assert!(validate_flight_presets(&invalid).is_err());
        invalid = scenario.clone();
        invalid.flight_presets.mars_facing_l1.label.clear();
        assert!(validate_flight_presets(&invalid).is_err());
        invalid = scenario.clone();
        invalid.flight_presets.far_side_l2.target_name = "Relabeled return ship".into();
        assert!(validate_flight_presets(&invalid).is_err());
        invalid = scenario.clone();
        invalid
            .flight_presets
            .competition_reference
            .target_distance_m = Some(1300.0);
        assert!(validate_flight_presets(&invalid).is_err());
        invalid = scenario.clone();
        invalid
            .flight_presets
            .competition_reference
            .target_distance_m = None;
        assert!(validate_flight_presets(&invalid).is_err());
        invalid = scenario.clone();
        invalid.flight_presets.far_side_l2.target_distance_m = Some(1305.8);
        assert!(validate_flight_presets(&invalid).is_err());
    }

    #[test]
    fn official_cloud_and_radar_constants_are_versioned_and_validated() {
        let scenario = crate::scenario::settings();
        validate_official_cloud(scenario).unwrap();
        let cloud = &scenario.official_cloud;
        assert_eq!(cloud.count, 500);
        assert_eq!(cloud.half_extents_m, [300.0, 300.0, 500.0]);
        assert_eq!(cloud.vertical_offset_m, 500.0);
        assert_eq!(cloud.drift_reference_mu_m3_s2, 707_500.0);
        assert_eq!(cloud.drift_reference_radius_m, 11_100.0);
        assert_eq!(
            cloud.drift_speed_range_mps(),
            (
                0.05_f64 * (707_500.0_f64 / 11_100.0).sqrt(),
                0.1_f64 * (707_500.0_f64 / 11_100.0).sqrt()
            )
        );
        assert_eq!(cloud.collision_radius_max_m, 20.0);
        assert_eq!(cloud.classes.len(), 4);
        assert_eq!(cloud.radar.observed_count, 200);
        assert_eq!(cloud.radar.iteration_hz, 120.0);
        assert_eq!(cloud.radar.publish_every_rows, 50);
        for mutate in 0..12 {
            let mut invalid = scenario.clone();
            match mutate {
                0 => invalid.official_cloud.version = 2,
                1 => invalid.official_cloud.count = 499,
                2 => invalid.official_cloud.half_extents_m[0] = 301.0,
                3 => invalid.official_cloud.vertical_offset_m = 499.0,
                4 => invalid.official_cloud.drift_reference_mu_m3_s2 = 708_700.0,
                5 => invalid.official_cloud.drift_reference_radius_m = 9_100.0,
                6 => invalid.official_cloud.mass_scale_exponent = 2.0,
                7 => invalid.official_cloud.classes[0].weight = 0.44,
                8 => invalid.official_cloud.classes[1].scale_max = 801.0,
                9 => invalid.official_cloud.classes[3].base_mass_kg = 701.0,
                10 => invalid.official_cloud.radar.publish_every_rows = 49,
                11 => invalid.official_cloud.radar.iteration_hz = 60.0,
                _ => unreachable!(),
            }
            assert!(
                validate_official_cloud(&invalid).is_err(),
                "mutation {mutate}"
            );
        }
        let mut invalid = scenario.clone();
        invalid.debris.radar_count = 199;
        assert!(validate_official_cloud(&invalid).is_err());
    }

    #[test]
    fn impact_defaults_and_debris_count_bounds_are_validated_as_scenario_data() {
        let mut scenario = crate::scenario::settings().clone();
        scenario.debris.count = crate::model::MAX_DEBRIS_COUNT;
        validate_limits(&scenario).unwrap();
        scenario.debris.count += 1;
        assert!(validate_limits(&scenario).is_err());
        scenario = crate::scenario::settings().clone();
        scenario.impact_ejecta_defaults.source_direction = [0.0; 3];
        assert_eq!(validate(&scenario), Err("invalid impact ejecta defaults"));
    }

    #[test]
    fn transit_route_and_source_adjustment_constants_are_versioned_validated_data() {
        let reference = crate::scenario::settings();
        assert_eq!(reference.transit_stream.version, 1);
        assert_eq!(reference.transit_stream.transverse_half_width_m, 450.0);
        assert_eq!(reference.transit_stream.launch_trail_m, 500.0);
        assert_eq!(reference.transit_stream.target_lead_m, 500.0);
        validate_transit_and_adjustment(reference).unwrap();
        for invalid in [f64::NAN, f64::INFINITY, 0.0, 100.0, 50_000.1] {
            let mut scenario = reference.clone();
            scenario.transit_stream.transverse_half_width_m = invalid;
            assert!(validate_transit_and_adjustment(&scenario).is_err());
        }
        for field in 0..8 {
            let mut scenario = reference.clone();
            match field {
                0 => scenario.debris_trajectory_adjustment.version = 0,
                1 => scenario.debris_trajectory_adjustment.source_url.clear(),
                2 => {
                    scenario
                        .debris_trajectory_adjustment
                        .source_enabled_by_default = true
                }
                3 => scenario.debris_trajectory_adjustment.global_rate_hz = 121.0,
                4 => scenario.debris_trajectory_adjustment.cutoff_m = f64::NAN,
                5 => scenario.debris_trajectory_adjustment.typical_speed_mps = 0.0,
                6 => scenario.debris_trajectory_adjustment.delta_v_factor = f64::INFINITY,
                7 => scenario
                    .debris_trajectory_adjustment
                    .scheduling_note
                    .clear(),
                _ => unreachable!(),
            }
            assert!(validate_transit_and_adjustment(&scenario).is_err());
        }
    }

    #[test]
    fn practice_lift_is_bounded_and_cannot_silently_lower_the_reference_limit() {
        let mut scenario = crate::scenario::settings().clone();
        assert!(validate_practice(&scenario).is_ok());
        for invalid in [f64::NAN, f64::INFINITY, 0.0, 0.1, 20.1] {
            scenario.practice.space_lift_accel_mps2 = invalid;
            assert!(validate_practice(&scenario).is_err());
        }
    }

    #[test]
    fn archived_corridor_reference_is_explicit_validated_data_not_the_current_default() {
        let scenario = crate::scenario::settings();
        assert_eq!(scenario.legacy_corridor_flow_reference.speed_mps, 20.0);
        assert_eq!(
            scenario.legacy_corridor_flow_reference.speed_spread_mps,
            5.0
        );
        assert_eq!(
            scenario.legacy_corridor_flow_reference.stream_axis,
            crate::model::StreamAxis::Y
        );
        assert_eq!(scenario.legacy_corridor_flow_reference.stream_sign, -1);
        assert_ne!(
            scenario.legacy_corridor_flow_reference,
            crate::model::DebrisFlowTuning::from(scenario.experiment_settings.debris)
        );
        let mut invalid = scenario.clone();
        invalid.legacy_corridor_flow_reference.speed_mps = f64::NAN;
        assert_eq!(
            validate(&invalid),
            Err("invalid archived corridor flow calibration reference")
        );
    }

    #[test]
    fn nozzle_points_and_debug_exhaust_cannot_be_nonfinite_or_unbounded() {
        let mut scenario = crate::scenario::settings().clone();
        assert!(validate_lander(&scenario).is_ok());
        assert!(validate_debug_thrust(&scenario).is_ok());
        scenario.lander.manual_nozzles_body_m[0][0] = f64::NAN;
        assert!(validate_lander(&scenario).is_err());
        scenario.debug_thrust.max_particles = 4097;
        assert!(validate_debug_thrust(&scenario).is_err());
        scenario.debug_thrust.max_particles = 512;
        scenario.debug_thrust.exhaust_speed_mps = f64::INFINITY;
        assert!(validate_debug_thrust(&scenario).is_err());
    }

    #[test]
    fn camera_fov_rejects_nonfinite_zero_and_out_of_range_angles() {
        let mut scenario = crate::scenario::settings().clone();
        for angle in [0.0, -1.0, 179.0, 180.0, f32::NAN, f32::INFINITY] {
            scenario.ship_cameras.field_of_view_deg = angle;
            assert!(validate_ship_cameras(&scenario).is_err());
        }
    }

    #[test]
    fn camera_aim_must_be_finite_and_inside_its_declared_limits() {
        assert!(valid_camera_aim(-50.0, [-50.0, 0.0], 90.0));
        assert!(valid_camera_aim(0.0, [-50.0, 0.0], 90.0));
        assert!(!valid_camera_aim(0.1, [-50.0, 0.0], 90.0));
        assert!(!valid_camera_aim(-50.1, [-50.0, 0.0], 90.0));
        assert!(!valid_camera_aim(f32::NAN, [-50.0, 0.0], 90.0));
        assert!(!valid_camera_aim(0.0, [f32::NEG_INFINITY, 0.0], 90.0));
        assert!(!valid_camera_aim(0.0, [0.0, -50.0], 90.0));
        assert!(!valid_camera_aim(0.0, [-91.0, 0.0], 90.0));
    }

    #[test]
    fn camera_mount_order_aim_and_capture_dimensions_are_validated() {
        let mut scenario = crate::scenario::settings().clone();
        assert!(validate_ship_cameras(&scenario).is_ok());
        scenario.ship_cameras.resolution_px = [512, 256];
        assert!(validate_ship_cameras(&scenario).is_err());
        scenario.ship_cameras.resolution_px = [512, 512];
        scenario.ship_cameras.mounts[0].aim_pitch_deg = 1.0;
        assert!(validate_ship_cameras(&scenario).is_err());
        scenario.ship_cameras.mounts[0].aim_pitch_deg = 0.0;
        scenario.ship_cameras.mounts.swap(0, 1);
        assert!(validate_ship_cameras(&scenario).is_err());
    }
}
