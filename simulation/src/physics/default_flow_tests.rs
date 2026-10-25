use super::*;
use crate::model::{ActuationState, StreamAxis};

fn actual_phobos_debris_contact(sim: &Simulation) -> Option<EntityId> {
    sim.narrow.contact_pairs().find_map(|pair| {
        if !pair.has_any_active_contact() || pair.total_impulse_magnitude() <= 0.0 {
            return None;
        }
        let first = *sim.collider_ids.get(&pair.collider1)?;
        let second = *sim.collider_ids.get(&pair.collider2)?;
        match (first, second) {
            (EntityId::Phobos, debris @ EntityId::Debris(_))
            | (debris @ EntityId::Debris(_), EntityId::Phobos) => Some(debris),
            _ => None,
        }
    })
}

#[test]
fn fresh_versioned_default_hits_real_phobos_without_injected_motion_or_contact_results() {
    let mut sim = Simulation::new_default();
    let config = sim.state.experiment_settings.unwrap();
    assert_eq!(config.debris.count, 500);
    assert_eq!(config.debris.stream_axis, StreamAxis::X);
    assert_eq!(config.debris.stream_sign, 1);
    assert_eq!(config.debris.speed_mps, 20.0);
    assert_eq!(config.debris.speed_spread_mps, 4.0);
    assert_eq!(config.debris.shell_inner_m, 60.0);
    assert_eq!(config.debris.shell_outer_m, 700.0);
    let initial = sim.snapshot();
    let handles = sim.handles.clone();
    let colliders = sim.collider_ids.clone();
    let ship_handle = sim.handles[&EntityId::Lander];
    let phobos_pose = *sim.bodies[sim.handles[&EntityId::Phobos]].position();
    let pad_pose = *sim.bodies[sim.handles[&EntityId::LandingPad]].position();
    let incoming = sim
        .state
        .assets
        .iter()
        .filter(|entity| {
            entity.kind == EntityKind::Debris
                && entity.position[0] < 0.0
                && dot(entity.position, entity.velocity) < 0.0
        })
        .count();
    assert!(
        incoming > 150,
        "the upstream hemisphere must contain a broad incoming population"
    );

    let dt = settings().integration.fixed_step_s;
    let mut collision = None;
    // Only ordinary gravity/actuator-zero integration and real Rapier contacts.
    // No position, velocity, force, body, mission or contact flag is injected.
    for _ in 0..(15.0 / dt).ceil() as usize {
        sim.advance(dt);
        collision = actual_phobos_debris_contact(&sim);
        if collision.is_some() {
            break;
        }
    }
    assert!(
        collision.is_some(),
        "fresh default must produce a genuine debris/Phobos impulse within 15 s"
    );
    assert!(sim.state.collisions > initial.collisions);
    assert!(sim.state.time_s > 0.0 && sim.state.time_s <= 15.0 + dt);
    assert_eq!(sim.handles, handles);
    assert_eq!(sim.collider_ids, colliders);
    assert_eq!(sim.state.assets.len(), 503);
    assert_eq!(sim.bodies.len(), 504);
    assert_eq!(sim.colliders.len(), 504);
    assert_eq!(
        *sim.bodies[sim.handles[&EntityId::Phobos]].position(),
        phobos_pose
    );
    assert_eq!(
        *sim.bodies[sim.handles[&EntityId::LandingPad]].position(),
        pad_pose
    );
    let before = sim.snapshot();
    let ship_pose = *sim.bodies[ship_handle].position();
    let velocity = sim.bodies[ship_handle].linvel();
    let spin = sim.bodies[ship_handle].angvel();
    let warmstarts: Vec<_> = sim
        .narrow
        .contact_pairs()
        .map(|pair| {
            (
                pair.collider1,
                pair.collider2,
                format!("{:?}", pair.manifolds()),
                format!("{:?}", pair.solver_manifolds()),
            )
        })
        .collect();
    let tuning = ShipTuning {
        mass_kg: 1_000.0,
        inertia_scale: 2.0,
        thrust_scale: 3.2,
    };
    sim.tune_ship(tuning).unwrap();
    assert_eq!(sim.handles, handles);
    assert_eq!(sim.collider_ids, colliders);
    assert_eq!(*sim.bodies[ship_handle].position(), ship_pose);
    assert_eq!(sim.bodies[ship_handle].linvel(), velocity);
    assert_eq!(sim.bodies[ship_handle].angvel(), spin);
    assert_eq!(sim.state.time_s, before.time_s);
    assert_eq!(sim.state.rng_state, before.rng_state);
    assert_eq!(sim.state.active_contacts, before.active_contacts);
    assert_eq!(sim.state.collisions, before.collisions);
    assert_eq!(
        serde_json::to_value(&sim.state.mission).unwrap(),
        serde_json::to_value(&before.mission).unwrap()
    );
    assert_eq!(
        serde_json::to_value(&sim.state.observations).unwrap(),
        serde_json::to_value(&before.observations).unwrap()
    );
    let after_warmstarts: Vec<_> = sim
        .narrow
        .contact_pairs()
        .map(|pair| {
            (
                pair.collider1,
                pair.collider2,
                format!("{:?}", pair.manifolds()),
                format!("{:?}", pair.solver_manifolds()),
            )
        })
        .collect();
    assert_eq!(after_warmstarts, warmstarts);
    let applied = sim.applied_physics().unwrap();
    assert_eq!(applied.actuation_state, ActuationState::Coasting);
    assert_eq!(
        applied.last_actuator_application.unwrap().force_world_n,
        [0.0; 3]
    );
    validate(&sim.snapshot()).unwrap();
}

#[test]
fn previous_some_default_archive_keeps_its_base_label_seeded_world_rng_and_future_physics() {
    let old: ExperimentSettings = serde_json::from_value(json!({
        "ship": {"mass_kg": 3388.7, "inertia_scale": 1.0},
        "debris": {"count": 500, "radius_min_m": 0.4, "radius_max_m": 40.4,
            "speed_mps": 20.0, "speed_spread_mps": 5.0, "shell_inner_m": 200.0,
            "shell_outer_m": 3000.0, "stream_axis": "y", "stream_sign": -1}
    }))
    .unwrap();
    assert_ne!(old, ExperimentSettings::default());
    let old_model = "local_experiment_v1: Phobos surface launch through a broad-shell directional asteroid stream to a kinematic Mars-Phobos L2 return ship. Real rigid-body contacts, circular point-mass CR3BP, synthetic terrain and estimated fuel; not STP parity.";
    let mut original = Simulation::with_experiment(100, old).unwrap();
    // This is the exact descriptive label in genuine pre-default-change exports.
    // Physics, observations and RNG still come from the full real seeded world.
    original.state.model = old_model.into();
    let exported = serde_json::to_value(original.snapshot()).unwrap();
    let mut restored = Simulation::new_default();
    restored
        .execute(PhysicsCommand::Restore {
            state: Box::new(serde_json::from_value(exported.clone()).unwrap()),
        })
        .unwrap();
    assert_eq!(serde_json::to_value(restored.snapshot()).unwrap(), exported);
    assert_eq!(restored.state.experiment_settings, Some(old));
    assert_eq!(restored.state.model, old_model);
    assert_eq!(restored.rng, original.rng);
    for _ in 0..3 {
        original.advance(settings().integration.fixed_step_s);
        restored.advance(settings().integration.fixed_step_s);
        assert_eq!(
            serde_json::to_value(restored.snapshot()).unwrap(),
            serde_json::to_value(original.snapshot()).unwrap()
        );
    }
    restored.reset(100).unwrap();
    assert_eq!(serde_json::to_value(restored.snapshot()).unwrap(), exported);
    let mut bad_radius = restored.snapshot();
    bad_radius.assets.last_mut().unwrap().radius_m += 0.1;
    let mut bad_mass = restored.snapshot();
    bad_mass.assets[0].mass_kg += 1.0;
    let mut bad_frame = restored.snapshot();
    bad_frame.frame = "unrecognised world frame".into();
    let mut bad_model = restored.snapshot();
    bad_model.model = "unrecognised physics model".into();
    for invalid in [bad_radius, bad_mass, bad_frame, bad_model] {
        assert!(
            restored
                .execute(PhysicsCommand::Restore {
                    state: Box::new(invalid)
                })
                .is_err()
        );
        assert_eq!(serde_json::to_value(restored.snapshot()).unwrap(), exported);
    }
}

#[test]
fn custom_labels_survive_default_changes_and_none_keeps_the_frozen_calibration_reference() {
    let mut custom = ExperimentSettings::default();
    custom.ship.mass_kg = 8_123.0;
    custom.debris.count = 3;
    for config in [custom, ExperimentSettings::default()] {
        let mut source = Simulation::with_experiment(42, config).unwrap();
        source.state.model = format!("{} [custom settings]", settings().experiment_description);
        let exported = serde_json::to_value(source.snapshot()).unwrap();
        let mut restored = Simulation::new_default();
        restored
            .execute(PhysicsCommand::Restore {
                state: Box::new(source.snapshot()),
            })
            .unwrap();
        restored.reset(42).unwrap();
        assert_eq!(serde_json::to_value(restored.snapshot()).unwrap(), exported);
    }
    let corridor = Simulation::with_profile(100, None).unwrap();
    assert_eq!(corridor.debris_flow().stream_axis, StreamAxis::Y);
    assert_eq!(corridor.debris_flow().stream_sign, -1);
    assert_eq!(corridor.debris_flow().speed_mps, 20.0);
    assert_eq!(corridor.debris_flow().speed_spread_mps, 5.0);
    assert_ne!(
        corridor.debris_flow(),
        DebrisFlowTuning::from(ExperimentSettings::default().debris)
    );
    validate(&corridor.snapshot()).unwrap();
}
