use super::*;
use crate::physics::Simulation;
use rapier3d_f64::prelude::Vector;

fn lander(sim: &Simulation) -> EntityState {
    sim.snapshot()
        .assets
        .into_iter()
        .find(|entity| entity.id == EntityId::Lander)
        .unwrap()
}

fn assert_vector_close(actual: [f64; 3], expected: [f64; 3]) {
    assert!(
        (Vector::from_array(actual) - Vector::from_array(expected)).length() < 1e-8,
        "actual {actual:?}, expected {expected:?}"
    );
}

#[test]
fn signed_body_axes_select_the_force_mount_and_opposite_exhaust_direction() {
    let cfg = settings().lander;
    let mut sim = Simulation::new_default();
    let force_n = cfg.mass_kg * cfg.max_accel_mps2;
    for (nozzle, point) in cfg.manual_nozzles_body_m.into_iter().enumerate() {
        let axis = nozzle / 2;
        let sign = if nozzle % 2 == 0 { 1.0 } else { -1.0 };
        let mut accel = [0.0; 3];
        accel[axis] = sign * cfg.max_accel_mps2;
        sim.set_manual_thrust(accel).unwrap();
        let lander = lander(&sim);
        let jets = applied_jets(&lander);
        assert_eq!(jets.len(), 1);
        let jet = jets[0];
        assert_eq!(jet.point_body_m, point);
        assert_eq!(jet.force_body_n, lander.thrust_n);
        assert_eq!(jet.force_body_n[axis], sign * force_n);

        let force = Vector::from_array(jet.force_body_n);
        let emission = -force.normalize();
        assert!((emission[axis] + sign).abs() < 1e-12);
        assert!((emission * force_n + force).length() < 1e-8);
        let torque = Vector::from_array(jet.point_body_m).cross(force);
        let exhaust_moment = Vector::from_array(jet.point_body_m).cross(emission * force_n);
        assert!((torque + exhaust_moment).length() < 1e-8);
        assert_vector_close(applied_wrench(&jets).torque_body_nm, torque.to_array());
        assert_vector_close(lander.torque_nm, torque.to_array());

        match axis {
            0 => assert!(torque.y * sign < 0.0),
            // +Y is a left push. Below-COM force gives +X roll, not a forced left roll.
            1 => assert!(torque.x * sign > 0.0),
            2 => assert_eq!(torque, Vector::ZERO),
            _ => unreachable!(),
        }
    }
}

#[test]
fn manual_diagonal_uses_three_real_components_with_the_existing_norm_limit() {
    let cfg = settings().lander;
    let mut sim = Simulation::new_default();
    sim.set_manual_thrust([1.0, -1.0, 1.0]).unwrap();
    let lander = lander(&sim);
    let jets = applied_jets(&lander);
    assert_eq!(jets.len(), 3);
    assert_eq!(jets[0].point_body_m, cfg.manual_nozzles_body_m[0]);
    assert_eq!(jets[1].point_body_m, cfg.manual_nozzles_body_m[3]);
    assert_eq!(jets[2].point_body_m, cfg.manual_nozzles_body_m[4]);
    let wrench = applied_wrench(&jets);
    assert_vector_close(wrench.force_body_n, lander.thrust_n);
    assert_vector_close(wrench.torque_body_nm, lander.torque_nm);
    assert!(
        (Vector::from_array(lander.commanded_accel_mps2).length() - cfg.max_accel_mps2).abs()
            < 1e-12
    );
    assert!(wrench.propellant_force_n > Vector::from_array(wrench.force_body_n).length());
}

#[test]
fn practice_boost_uses_the_same_mounts_and_keeps_horizontal_force_and_moment_unchanged() {
    let cfg = settings().lander;
    let lift = settings().practice.space_lift_accel_mps2;
    let component = cfg.max_accel_mps2 / 2.0_f64.sqrt();
    let mut sim = Simulation::new_default();
    sim.set_manual_thrust([component, 0.0, component]).unwrap();
    let reference = lander(&sim);
    let reference_jets = applied_jets(&reference);

    sim.set_space_thrust([component, 0.0, lift / 2.0_f64.sqrt()])
        .unwrap();
    let boosted = lander(&sim);
    let jets = applied_jets(&boosted);
    assert_eq!(jets.len(), 2);
    assert_eq!(jets[0].point_body_m, cfg.manual_nozzles_body_m[0]);
    assert_eq!(jets[1].point_body_m, cfg.manual_nozzles_body_m[4]);
    assert_eq!(jets[0].force_body_n, reference_jets[0].force_body_n);
    assert_eq!(jets[0].point_body_m, reference_jets[0].point_body_m);
    assert_eq!(jets[1].point_body_m, reference_jets[1].point_body_m);
    let wrench = applied_wrench(&jets);
    assert_vector_close(wrench.force_body_n, boosted.thrust_n);
    assert_vector_close(wrench.torque_body_nm, reference.torque_nm);
    assert_vector_close(wrench.torque_body_nm, boosted.torque_nm);
    assert!((jets[1].force_body_n[2] / reference_jets[1].force_body_n[2] - 4.0).abs() < 1e-12);
    assert!((wrench.propellant_force_n - boosted.thrust_n[0] - boosted.thrust_n[2]).abs() < 1e-9);
}

#[test]
fn practice_boost_force_bounds_reject_horizontal_downward_and_over_limit_forces() {
    let cfg = settings().lander;
    let lift = settings().practice.space_lift_accel_mps2;
    assert_eq!(accel_limit(ThrustApplication::CenterOfMass), 0.5);
    assert_eq!(accel_limit(ThrustApplication::ManualNozzles), 0.5);
    assert_eq!(accel_limit(ThrustApplication::PracticeBoost), 2.0);
    let mut sim = Simulation::new_default();
    sim.set_space_thrust([0.0, 0.0, lift]).unwrap();
    let original = lander(&sim);
    let jets = applied_jets(&original);
    assert_eq!(jets.len(), 1);
    assert_eq!(jets[0].point_body_m, cfg.manual_nozzles_body_m[4]);
    assert_eq!(jets[0].force_body_n, [0.0, 0.0, cfg.mass_kg * lift]);
    assert_eq!(applied_wrench(&jets).torque_body_nm, [0.0; 3]);
    assert_eq!(
        original.max_thrust_n,
        cfg.mass_kg * lift.max(cfg.max_accel_mps2)
    );

    for accel in [
        [0.0, 0.0, lift + 0.01],
        [cfg.max_accel_mps2 + 0.01, 0.0, 1.0],
        [cfg.max_accel_mps2, cfg.max_accel_mps2, 1.0],
        [0.0, 0.0, -0.1],
        [f64::NAN, 0.0, lift],
        [0.0, 0.0, f64::INFINITY],
    ] {
        let mut invalid = original.clone();
        invalid.thrust_n = accel.map(|component| component * cfg.mass_kg);
        assert!(!accel_within_limits(
            ThrustApplication::PracticeBoost,
            accel
        ));
        assert!(applied_jets(&invalid).is_empty());
    }
    for application in [
        ThrustApplication::CenterOfMass,
        ThrustApplication::ManualNozzles,
    ] {
        let mut invalid = original.clone();
        invalid.thrust_application = application;
        assert!(applied_jets(&invalid).is_empty());
    }
    let mut other = original;
    other.id = EntityId::ReturnShip;
    other.kind = EntityKind::ReturnShip;
    assert!(applied_jets(&other).is_empty());
}

#[test]
fn center_force_is_one_jet_and_never_fabricates_an_angular_control_jet() {
    let mut sim = Simulation::new_default();
    sim.set_command([0.1, -0.2, 0.3], [0.01, 0.02, 0.03])
        .unwrap();
    let ship = lander(&sim);
    let jets = applied_jets(&ship);
    assert_eq!(jets.len(), 1);
    assert_eq!(jets[0].point_body_m, [0.0; 3]);
    assert_eq!(jets[0].force_body_n, ship.thrust_n);
    assert_ne!(ship.torque_nm, [0.0; 3]);
    assert_eq!(applied_wrench(&jets).torque_body_nm, [0.0; 3]);

    sim.set_command([0.0; 3], [0.01, 0.0, 0.0]).unwrap();
    assert!(applied_jets(&lander(&sim)).is_empty());
}

#[test]
fn zero_nonfinite_over_limit_and_non_lander_forces_do_not_emit_jets() {
    let mut sim = Simulation::new_default();
    sim.set_manual_thrust([0.0; 3]).unwrap();
    assert!(applied_jets(&lander(&sim)).is_empty());
    sim.set_manual_thrust([0.0, 0.5, 0.0]).unwrap();
    let original = lander(&sim);
    for force in [
        [f64::NAN, 0.0, 0.0],
        [0.0, f64::INFINITY, 0.0],
        [original.max_thrust_n * 2.0, 0.0, 0.0],
    ] {
        let mut invalid = original.clone();
        invalid.thrust_n = force;
        assert!(applied_jets(&invalid).is_empty());
    }
    let mut other = original;
    other.id = EntityId::ReturnShip;
    other.kind = EntityKind::ReturnShip;
    assert!(applied_jets(&other).is_empty());
    other.thrust_application = ThrustApplication::CenterOfMass;
    assert!(applied_jets(&other).is_empty());
}

#[test]
fn propellant_force_sums_individual_magnitudes_even_when_the_wrench_cancels() {
    let mut sim = Simulation::new_default();
    sim.set_manual_thrust([0.5, 0.0, 0.0]).unwrap();
    let forward = applied_jets(&lander(&sim))[0];
    sim.set_manual_thrust([-0.5, 0.0, 0.0]).unwrap();
    let reverse = applied_jets(&lander(&sim))[0];
    // This tests the pure accounting calculation, not an extra actuator mode.
    let wrench = applied_wrench(&[forward, reverse]);
    assert_eq!(wrench.force_body_n, [0.0; 3]);
    assert_eq!(wrench.torque_body_nm, [0.0; 3]);
    assert_eq!(wrench.propellant_force_n, settings().lander.mass_kg);
}

#[test]
fn configured_mass_drives_the_same_real_nozzle_map_and_reference_acceleration_cap() {
    let mut experiment = crate::model::ExperimentSettings::default();
    experiment.ship.mass_kg = 100_000.0;
    experiment.ship.inertia_scale = 3.0;
    experiment.debris.count = 0;
    let mut sim = Simulation::with_experiment(5, experiment).unwrap();
    sim.set_manual_thrust([0.3, -0.4, 0.0]).unwrap();
    let ship = lander(&sim);
    assert_eq!(ship.thrust_n, [30_000.0, -40_000.0, 0.0]);
    let jets = applied_jets(&ship);
    assert_eq!(jets.len(), 2);
    assert_eq!(
        jets[0].point_body_m,
        settings().lander.manual_nozzles_body_m[0]
    );
    assert_eq!(
        jets[1].point_body_m,
        settings().lander.manual_nozzles_body_m[3]
    );
    let wrench = applied_wrench(&jets);
    assert_eq!(wrench.force_body_n, ship.thrust_n);
    assert_eq!(wrench.torque_body_nm, ship.torque_nm);
    assert_eq!(wrench.propellant_force_n, 70_000.0);
    let dt = settings().integration.fixed_step_s;
    sim.advance(dt);
    let fuel = sim.snapshot().mission.fuel_used_kg;
    assert!(
        (fuel
            - 70_000.0 * dt
                / (settings().lander.specific_impulse_s * settings().lander.standard_gravity_mps2))
            .abs()
            < 1e-12
    );
    for invalid_mass in [0.0, -1.0, f64::NAN, f64::INFINITY] {
        let mut invalid = ship.clone();
        invalid.mass_kg = invalid_mass;
        assert!(applied_jets(&invalid).is_empty());
    }
}

#[test]
fn local_gain_above_one_emits_actual_scaled_jets_but_does_not_raise_the_request_cap() {
    let mut sim = Simulation::new_default();
    sim.set_manual_thrust([0.0, 5.0, 0.0]).unwrap();
    let mut tuning = sim.ship_tuning();
    tuning.thrust_scale = 10.0;
    sim.tune_ship(tuning).unwrap();
    let ship = lander(&sim);
    assert_eq!(ship.commanded_accel_mps2, [0.0, 0.5, 0.0]);
    assert_eq!(ship.actuator_gain, 10.0);
    let jets = applied_jets(&ship);
    assert_eq!(jets.len(), 1);
    assert_eq!(jets[0].force_body_n, [0.0, ship.mass_kg * 5.0, 0.0]);
    assert_vector_close(applied_wrench(&jets).torque_body_nm, ship.torque_nm);
    for gain in [0.0, 0.099, 10.001, f64::NAN, f64::INFINITY] {
        let mut invalid = ship.clone();
        invalid.actuator_gain = gain;
        assert!(applied_jets(&invalid).is_empty());
    }
    let mut invalid = ship;
    invalid.thrust_n[1] *= 1.01;
    assert!(applied_jets(&invalid).is_empty());
}

#[test]
fn missing_gain_in_a_legacy_boost_archive_defaults_to_one_and_preserves_the_nozzles() {
    let mut original = Simulation::new_default();
    original.set_space_thrust([0.2, -0.3, 1.5]).unwrap();
    let expected = lander(&original);
    let mut archive = serde_json::to_value(original.snapshot()).unwrap();
    for entity in archive["assets"].as_array_mut().unwrap() {
        entity.as_object_mut().unwrap().remove("actuator_gain");
    }
    let mut restored = Simulation::new_default();
    restored
        .execute(crate::model::PhysicsCommand::Restore {
            state: Box::new(serde_json::from_value(archive).unwrap()),
        })
        .unwrap();
    let actual = lander(&restored);
    assert_eq!(actual.actuator_gain, 1.0);
    assert_eq!(actual.thrust_application, ThrustApplication::PracticeBoost);
    assert_eq!(actual.thrust_n, expected.thrust_n);
    assert_eq!(actual.torque_nm, expected.torque_nm);
    assert_eq!(applied_jets(&actual).len(), applied_jets(&expected).len());
    original.advance(settings().integration.fixed_step_s);
    restored.advance(settings().integration.fixed_step_s);
    assert_eq!(
        serde_json::to_value(original.snapshot()).unwrap(),
        serde_json::to_value(restored.snapshot()).unwrap()
    );
}
