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
