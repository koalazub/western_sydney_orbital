//! Pure native-step accounting. No commands, clocks, rendering or sensor reads.
use crate::domain::{FlightAnalytics, FlightAnalyticsDefinition, FlightAnalyticsStep};
use std::sync::OnceLock;

const SCHEMA_VERSION: u32 = 1;
const DEFINITION_ID: &str = "native_actuation_rotation_episodes_v1";

fn definition() -> &'static FlightAnalyticsDefinition {
    static DEFINITION: OnceLock<FlightAnalyticsDefinition> = OnceLock::new();
    DEFINITION.get_or_init(|| {
        let definition = serde_json::from_str(include_str!("../scenarios/flight_analytics.json"))
            .expect("checked-in flight analytics definition must deserialize");
        validate_definition(&definition)
            .expect("checked-in flight analytics definition must be valid");
        definition
    })
}

fn validate_definition(definition: &FlightAnalyticsDefinition) -> Result<(), String> {
    if definition.schema_version != SCHEMA_VERSION
        || definition.definition_id != DEFINITION_ID
        || ![
            definition.burn_force_threshold_n,
            definition.angular_torque_threshold_nm,
            definition.turn_speed_threshold_rad_s,
        ]
        .into_iter()
        .all(|value| value.is_finite() && value > 0.0)
    {
        return Err("unsupported or invalid flight analytics definition".into());
    }
    Ok(())
}

impl Default for FlightAnalytics {
    fn default() -> Self {
        let definition = definition();
        Self {
            schema_version: definition.schema_version,
            definition_id: definition.definition_id.clone(),
            observed_sim_s: 0.0,
            burn_count: 0,
            burn_duration_s: 0.0,
            longest_burn_s: 0.0,
            angular_burn_count: 0,
            angular_burn_duration_s: 0.0,
            longest_angular_burn_s: 0.0,
            turn_count: 0,
            turn_duration_s: 0.0,
            longest_turn_s: 0.0,
            total_turn_angle_rad: 0.0,
            peak_angular_speed_rad_s: 0.0,
            propellant_impulse_ns: 0.0,
            angular_impulse_nms: 0.0,
            burn_active: false,
            angular_burn_active: false,
            turn_active: false,
            current_burn_s: 0.0,
            current_angular_burn_s: 0.0,
            current_turn_s: 0.0,
        }
    }
}

/// Reject malformed receipts without substituting missing history with zeros.
pub(crate) fn validate_snapshot(analytics: &FlightAnalytics, time_s: f64) -> Result<(), String> {
    let definition = definition();
    if analytics.schema_version != definition.schema_version
        || analytics.definition_id != definition.definition_id
        || !time_s.is_finite()
        || ![
            analytics.observed_sim_s,
            analytics.burn_duration_s,
            analytics.longest_burn_s,
            analytics.angular_burn_duration_s,
            analytics.longest_angular_burn_s,
            analytics.turn_duration_s,
            analytics.longest_turn_s,
            analytics.total_turn_angle_rad,
            analytics.peak_angular_speed_rad_s,
            analytics.propellant_impulse_ns,
            analytics.angular_impulse_nms,
            analytics.current_burn_s,
            analytics.current_angular_burn_s,
            analytics.current_turn_s,
        ]
        .into_iter()
        .all(|value| value.is_finite() && value >= 0.0)
        // Both clocks add the same native h from the same fresh start. A partial
        // observation history cannot be relabelled as a complete flight receipt.
        || analytics.observed_sim_s != time_s
    {
        return Err("invalid flight_analytics version, finite values or observed history".into());
    }
    for (count, duration, longest, current, active) in [
        (
            analytics.burn_count,
            analytics.burn_duration_s,
            analytics.longest_burn_s,
            analytics.current_burn_s,
            analytics.burn_active,
        ),
        (
            analytics.angular_burn_count,
            analytics.angular_burn_duration_s,
            analytics.longest_angular_burn_s,
            analytics.current_angular_burn_s,
            analytics.angular_burn_active,
        ),
        (
            analytics.turn_count,
            analytics.turn_duration_s,
            analytics.longest_turn_s,
            analytics.current_turn_s,
            analytics.turn_active,
        ),
    ] {
        if duration > analytics.observed_sim_s
            || longest > duration
            || current > longest
            || (active != (current > 0.0))
            || (count == 0 && (duration != 0.0 || longest != 0.0 || active))
            || (count > 0 && (duration == 0.0 || longest == 0.0))
            || (count == 1 && longest != duration)
        {
            return Err("invalid flight_analytics episode counts or duration bounds".into());
        }
    }
    if ((analytics.turn_count > 0)
        != (analytics.peak_angular_speed_rad_s > definition.turn_speed_threshold_rad_s))
        || (analytics.burn_count > 0 && analytics.propellant_impulse_ns == 0.0)
        || (analytics.angular_burn_count > 0 && analytics.angular_impulse_nms == 0.0)
        || (time_s == 0.0
            && (analytics.total_turn_angle_rad != 0.0
                || analytics.peak_angular_speed_rad_s != 0.0
                || analytics.propellant_impulse_ns != 0.0
                || analytics.angular_impulse_nms != 0.0))
    {
        return Err("invalid flight_analytics motion or impulse history".into());
    }
    Ok(())
}

/// Missing/invalid measurements permanently drop availability until a fresh run.
pub(crate) fn integrate(
    available: &mut Option<FlightAnalytics>,
    step: Option<FlightAnalyticsStep>,
) {
    *available = available
        .as_ref()
        .and_then(|previous| transition(previous, step?).ok());
}

fn transition(
    previous: &FlightAnalytics,
    step: FlightAnalyticsStep,
) -> Result<FlightAnalytics, String> {
    validate_snapshot(previous, step.start_sim_s)?;
    if !step.duration_s.is_finite()
        || step.duration_s <= 0.0
        || !step.propellant_force_n.is_finite()
        || step.propellant_force_n < 0.0
    {
        return Err("invalid native flight analytics step".into());
    }
    let torque =
        magnitude(step.actuator_torque_world_nm).ok_or("invalid applied actuator torque")?;
    let speed =
        magnitude(step.angular_velocity_after_rad_s).ok_or("invalid post-step angular velocity")?;
    let angle = rotation_arc(step.rotation_before_xyzw, step.rotation_after_xyzw)
        .ok_or("invalid native orientation")?;
    let definition = definition();
    let mut next = previous.clone();
    next.observed_sim_s += step.duration_s;
    if next.observed_sim_s <= previous.observed_sim_s {
        return Err("flight analytics elapsed time cannot progress".into());
    }
    episode(
        &mut next.burn_count,
        &mut next.burn_duration_s,
        &mut next.longest_burn_s,
        &mut next.current_burn_s,
        &mut next.burn_active,
        step.propellant_force_n > definition.burn_force_threshold_n,
        step.duration_s,
    )?;
    episode(
        &mut next.angular_burn_count,
        &mut next.angular_burn_duration_s,
        &mut next.longest_angular_burn_s,
        &mut next.current_angular_burn_s,
        &mut next.angular_burn_active,
        torque > definition.angular_torque_threshold_nm,
        step.duration_s,
    )?;
    episode(
        &mut next.turn_count,
        &mut next.turn_duration_s,
        &mut next.longest_turn_s,
        &mut next.current_turn_s,
        &mut next.turn_active,
        speed > definition.turn_speed_threshold_rad_s,
        step.duration_s,
    )?;
    next.total_turn_angle_rad += angle;
    next.peak_angular_speed_rad_s = next.peak_angular_speed_rad_s.max(speed);
    next.propellant_impulse_ns += step.propellant_force_n * step.duration_s;
    next.angular_impulse_nms += torque * step.duration_s;
    validate_snapshot(&next, step.start_sim_s + step.duration_s)?;
    Ok(next)
}

fn episode(
    count: &mut u64,
    duration: &mut f64,
    longest: &mut f64,
    current: &mut f64,
    was_active: &mut bool,
    active: bool,
    h: f64,
) -> Result<(), String> {
    if active {
        if !*was_active {
            *count = count
                .checked_add(1)
                .ok_or("flight analytics count overflow")?;
        }
        *duration += h;
        *current += h;
        *longest = longest.max(*current);
    } else {
        *current = 0.0;
    }
    *was_active = active;
    Ok(())
}

fn magnitude(vector: [f64; 3]) -> Option<f64> {
    if !vector.into_iter().all(f64::is_finite) {
        return None;
    }
    let magnitude = vector[0].hypot(vector[1]).hypot(vector[2]);
    magnitude.is_finite().then_some(magnitude)
}

fn unit_quaternion(quaternion: [f64; 4]) -> Option<[f64; 4]> {
    if !quaternion.into_iter().all(f64::is_finite) {
        return None;
    }
    let magnitude = quaternion.into_iter().fold(0.0_f64, f64::hypot);
    (magnitude.is_finite() && magnitude > 0.0)
        .then(|| quaternion.map(|component| component / magnitude))
}

/// Shortest relative quaternion arc. abs(w) makes q and -q equivalent; atan2
/// retains small arcs and remains well-conditioned at a half turn.
fn rotation_arc(before: [f64; 4], after: [f64; 4]) -> Option<f64> {
    let [ax, ay, az, aw] = unit_quaternion(before)?;
    let [bx, by, bz, bw] = unit_quaternion(after)?;
    let x = aw * bx - ax * bw - ay * bz + az * by;
    let y = aw * by + ax * bz - ay * bw - az * bx;
    let z = aw * bz - ax * by + ay * bx - az * bw;
    let w = aw * bw + ax * bx + ay * by + az * bz;
    let angle = 2.0 * x.hypot(y).hypot(z).atan2(w.abs());
    angle.is_finite().then_some(angle)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn step(previous: &FlightAnalytics, h: f64) -> FlightAnalyticsStep {
        FlightAnalyticsStep {
            start_sim_s: previous.observed_sim_s,
            duration_s: h,
            propellant_force_n: 0.0,
            actuator_torque_world_nm: [0.0; 3],
            rotation_before_xyzw: [0.0, 0.0, 0.0, 1.0],
            rotation_after_xyzw: [0.0, 0.0, 0.0, 1.0],
            angular_velocity_after_rad_s: [0.0; 3],
        }
    }

    fn close(actual: f64, expected: f64) {
        assert!((actual - expected).abs() < 1e-12, "{actual} != {expected}");
    }

    #[test]
    fn one_cached_versioned_definition_supplies_all_classification_thresholds() {
        let config = definition();
        assert!(std::ptr::eq(config, definition()));
        assert_eq!(config.schema_version, 1);
        assert_eq!(
            config.definition_id,
            "native_actuation_rotation_episodes_v1"
        );
        assert_eq!(config.burn_force_threshold_n, 1e-6);
        assert_eq!(config.angular_torque_threshold_nm, 1e-6);
        assert_eq!(config.turn_speed_threshold_rad_s, 1e-3);
        for invalid in [0.0, -1.0, f64::NAN, f64::INFINITY] {
            let mut config = definition().clone();
            config.burn_force_threshold_n = invalid;
            assert!(validate_definition(&config).is_err());
            config = definition().clone();
            config.angular_torque_threshold_nm = invalid;
            assert!(validate_definition(&config).is_err());
            config = definition().clone();
            config.turn_speed_threshold_rad_s = invalid;
            assert!(validate_definition(&config).is_err());
        }
        let mut invalid = definition().clone();
        invalid.schema_version += 1;
        assert!(validate_definition(&invalid).is_err());
        invalid = definition().clone();
        invalid.definition_id.push_str("_unknown");
        assert!(validate_definition(&invalid).is_err());
        let mut json: serde_json::Value =
            serde_json::from_str(include_str!("../scenarios/flight_analytics.json")).unwrap();
        json["quiet_gap_s"] = serde_json::json!(0.1);
        assert!(serde_json::from_value::<FlightAnalyticsDefinition>(json).is_err());
    }

    #[test]
    fn exact_thresholds_are_inactive_but_their_impulses_are_still_integrated() {
        let previous = FlightAnalytics::default();
        let mut sample = step(&previous, 0.025);
        let config = definition();
        sample.propellant_force_n = config.burn_force_threshold_n;
        sample.actuator_torque_world_nm = [config.angular_torque_threshold_nm, 0.0, 0.0];
        sample.angular_velocity_after_rad_s = [0.0, config.turn_speed_threshold_rad_s, 0.0];
        let next = transition(&previous, sample).unwrap();
        assert_eq!(
            (next.burn_count, next.angular_burn_count, next.turn_count),
            (0, 0, 0)
        );
        assert!(!next.burn_active && !next.angular_burn_active && !next.turn_active);
        assert_eq!(
            next.propellant_impulse_ns,
            config.burn_force_threshold_n * 0.025
        );
        assert_eq!(
            next.angular_impulse_nms,
            config.angular_torque_threshold_nm * 0.025
        );
        assert_eq!(
            next.peak_angular_speed_rad_s,
            config.turn_speed_threshold_rad_s
        );
        sample = step(&next, 0.01);
        sample.propellant_force_n = config.burn_force_threshold_n.next_up();
        sample.actuator_torque_world_nm = [config.angular_torque_threshold_nm.next_up(), 0.0, 0.0];
        sample.angular_velocity_after_rad_s =
            [0.0, config.turn_speed_threshold_rad_s.next_up(), 0.0];
        let next = transition(&next, sample).unwrap();
        assert_eq!(
            (next.burn_count, next.angular_burn_count, next.turn_count),
            (1, 1, 1)
        );
    }

    #[test]
    fn episodes_use_rising_edges_and_actual_variable_h_with_no_quiet_gap() {
        let mut state = FlightAnalytics::default();
        // Angular actuation can precede detectable turning. Turning can then
        // continue while the actuators coast, independently of propellant use.
        for (h, force, torque, speed) in [
            (0.05, 8.0, 2.0, 0.0005),
            (0.025, 8.0, 2.0, 0.02),
            (0.01, 0.0, 0.0, 0.02),
            (0.04, 8.0, 0.0, 0.02),
            (0.005, 0.0, 2.0, 0.0),
            (0.02, 0.0, 0.0, 0.01),
        ] {
            let mut sample = step(&state, h);
            sample.propellant_force_n = force;
            sample.actuator_torque_world_nm = [0.0, 0.0, torque];
            sample.angular_velocity_after_rad_s = [speed, 0.0, 0.0];
            state = transition(&state, sample).unwrap();
        }
        close(state.observed_sim_s, 0.15);
        assert_eq!(
            (state.burn_count, state.angular_burn_count, state.turn_count),
            (2, 2, 2)
        );
        close(state.burn_duration_s, 0.115);
        close(state.longest_burn_s, 0.075);
        close(state.angular_burn_duration_s, 0.08);
        close(state.longest_angular_burn_s, 0.075);
        close(state.turn_duration_s, 0.095);
        close(state.longest_turn_s, 0.075);
        close(state.current_turn_s, 0.02);
        assert_eq!(state.current_burn_s, 0.0);
        assert_eq!(state.current_angular_burn_s, 0.0);
        assert!(!state.burn_active && !state.angular_burn_active && state.turn_active);
        close(state.propellant_impulse_ns, 8.0 * 0.115);
        close(state.angular_impulse_nms, 2.0 * 0.08);
        assert_eq!(state.peak_angular_speed_rad_s, 0.02);
    }

    #[test]
    fn rotation_arcs_handle_half_turns_quaternion_sign_and_small_motion() {
        let identity = [0.0, 0.0, 0.0, 1.0];
        let half_turn = [0.0, 1.0, 0.0, 0.0];
        for before in [identity, identity.map(|x| -x)] {
            for after in [half_turn, half_turn.map(|x| -x)] {
                close(rotation_arc(before, after).unwrap(), std::f64::consts::PI);
            }
        }
        let q = [0.5, -0.5, 0.5, 0.5];
        assert_eq!(rotation_arc(q, q.map(|x| -x)), Some(0.0));
        let small = 1e-10_f64;
        close(
            rotation_arc(
                identity,
                [0.0, 0.0, (small / 2.0).sin(), (small / 2.0).cos()],
            )
            .unwrap(),
            small,
        );
        let degrees_270 = [
            0.0,
            0.0,
            (3.0 * std::f64::consts::PI / 4.0).sin(),
            (3.0 * std::f64::consts::PI / 4.0).cos(),
        ];
        close(
            rotation_arc(identity, degrees_270).unwrap(),
            std::f64::consts::FRAC_PI_2,
        );
        let mut state = FlightAnalytics::default();
        for (before, after) in [(identity, half_turn), (half_turn, identity.map(|x| -x))] {
            let mut sample = step(&state, 0.01);
            sample.rotation_before_xyzw = before;
            sample.rotation_after_xyzw = after;
            state = transition(&state, sample).unwrap();
        }
        close(state.total_turn_angle_rad, std::f64::consts::TAU);
        assert_eq!(
            state.turn_count, 0,
            "arcs do not substitute for actual post-step speed"
        );
    }

    #[test]
    fn invalid_native_inputs_and_overflow_drop_availability_without_resurrection() {
        let fresh = FlightAnalytics::default();
        let valid = step(&fresh, 0.05);
        let mut invalid = Vec::new();
        for h in [0.0, -0.05, f64::NAN, f64::INFINITY] {
            invalid.push(FlightAnalyticsStep {
                duration_s: h,
                ..valid
            });
        }
        for force in [-1.0, f64::NAN, f64::INFINITY] {
            invalid.push(FlightAnalyticsStep {
                propellant_force_n: force,
                ..valid
            });
        }
        invalid.push(FlightAnalyticsStep {
            start_sim_s: 1.0,
            ..valid
        });
        invalid.push(FlightAnalyticsStep {
            actuator_torque_world_nm: [f64::NAN; 3],
            ..valid
        });
        invalid.push(FlightAnalyticsStep {
            angular_velocity_after_rad_s: [f64::MAX; 3],
            ..valid
        });
        invalid.push(FlightAnalyticsStep {
            rotation_before_xyzw: [0.0; 4],
            ..valid
        });
        invalid.push(FlightAnalyticsStep {
            rotation_after_xyzw: [f64::NAN; 4],
            ..valid
        });
        invalid.push(FlightAnalyticsStep {
            duration_s: f64::MAX,
            propellant_force_n: f64::MAX,
            ..valid
        });
        for sample in invalid {
            let mut available = Some(fresh.clone());
            integrate(&mut available, Some(sample));
            assert!(available.is_none(), "accepted {sample:?}");
            integrate(&mut available, Some(valid));
            assert!(available.is_none());
        }
        let mut available = Some(fresh.clone());
        integrate(&mut available, None);
        assert!(available.is_none());
        let mut populated = fresh;
        populated.observed_sim_s = 1.0;
        populated.burn_count = u64::MAX;
        populated.burn_duration_s = 0.5;
        populated.longest_burn_s = 0.5;
        populated.propellant_impulse_ns = 1.0;
        let mut sample = step(&populated, 0.05);
        sample.propellant_force_n = 1.0;
        let mut available = Some(populated);
        integrate(&mut available, Some(sample));
        assert!(available.is_none());
    }

    #[test]
    fn archived_receipts_require_complete_finite_consistent_fields_and_integer_counts() {
        let fresh = FlightAnalytics::default();
        let mut sample = step(&fresh, 0.05);
        sample.propellant_force_n = 1.0;
        sample.actuator_torque_world_nm = [0.0, 0.0, 1.0];
        sample.angular_velocity_after_rad_s = [0.01, 0.0, 0.0];
        let valid = transition(&fresh, sample).unwrap();
        let json = serde_json::to_value(&valid).unwrap();
        for (field, value) in [
            ("schema_version", serde_json::json!(2)),
            ("definition_id", serde_json::json!("inferred_poll_episodes")),
            ("observed_sim_s", serde_json::json!(0.0)),
            ("burn_duration_s", serde_json::json!(0.06)),
            ("longest_burn_s", serde_json::json!(0.06)),
            ("current_burn_s", serde_json::json!(0.06)),
            ("burn_count", serde_json::json!(0)),
            ("burn_active", serde_json::json!(false)),
            ("angular_burn_duration_s", serde_json::json!(-1.0)),
            ("angular_impulse_nms", serde_json::json!(0.0)),
            ("peak_angular_speed_rad_s", serde_json::json!(0.0)),
            ("total_turn_angle_rad", serde_json::json!(-1.0)),
        ] {
            let mut invalid = json.clone();
            invalid[field] = value;
            let invalid = serde_json::from_value::<FlightAnalytics>(invalid).unwrap();
            assert!(
                validate_snapshot(&invalid, 0.05).is_err(),
                "accepted {field}"
            );
        }
        for value in [
            serde_json::json!(1.5),
            serde_json::json!(-1),
            serde_json::json!("1"),
        ] {
            let mut invalid = json.clone();
            invalid["burn_count"] = value;
            assert!(serde_json::from_value::<FlightAnalytics>(invalid).is_err());
        }
        for field in json.as_object().unwrap().keys() {
            let mut invalid = json.clone();
            invalid.as_object_mut().unwrap().remove(field);
            assert!(
                serde_json::from_value::<FlightAnalytics>(invalid).is_err(),
                "defaulted {field}"
            );
        }
        let mut invalid = valid;
        invalid.total_turn_angle_rad = f64::INFINITY;
        assert!(validate_snapshot(&invalid, 0.05).is_err());
    }
}
