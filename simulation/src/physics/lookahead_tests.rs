use super::*;
use crate::model::{LookaheadHit, LookaheadQuery, LookaheadReport};

fn simulation(count: usize) -> Simulation {
    let mut config = ExperimentSettings::default();
    config.debris.count = count;
    config.debris.radius_min_m = 2.0;
    config.debris.radius_max_m = 2.0;
    let mut sim = Simulation::with_experiment(42, config).unwrap();
    let mut state = sim.snapshot();
    for entity in &mut state.assets {
        match entity.kind {
            EntityKind::Lander => {
                entity.position = [20_000.0, 1_000.0, 400.0];
                entity.rotation = [0.0, 0.0, 0.0, 1.0];
            }
            EntityKind::ReturnShip => entity.position = [20_200.0, 1_000.0, 400.0],
            EntityKind::Debris => entity.position = [20_100.0, 1_000.0, 400.0],
            EntityKind::LandingPad => {}
        }
    }
    sim.execute(PhysicsCommand::Restore {
        state: Box::new(state),
    })
    .unwrap();
    sim
}

fn fingerprint(
    sim: &Simulation,
) -> (
    Value,
    u64,
    Vec<(RigidBodyHandle, Pose, Vector, Vector, Vector, Vector)>,
) {
    (
        serde_json::to_value(sim.snapshot()).unwrap(),
        sim.rng,
        sim.bodies
            .iter()
            .map(|(handle, body)| {
                (
                    handle,
                    *body.position(),
                    body.linvel(),
                    body.angvel(),
                    body.user_force(),
                    body.user_torque(),
                )
            })
            .collect(),
    )
}

fn actual_shape_hit(
    sim: &Simulation,
    entity: EntityId,
    origin: Vector,
    direction: Vector,
    range: f64,
) -> LookaheadHit {
    let body = &sim.bodies[sim.handles[&entity]];
    let collider = &sim.colliders[body.colliders()[0]];
    let pose = *body.position() * *collider.position_wrt_parent().unwrap();
    let ray = Ray::new(origin, direction);
    let intersection = collider
        .shape()
        .cast_ray_and_get_normal(&pose, &ray, range, true)
        .unwrap();
    LookaheadHit {
        distance_m: intersection.time_of_impact,
        position_world_m: ray.point_at(intersection.time_of_impact).to_array(),
        normal_world: intersection.normal.to_array(),
        entity,
    }
}

#[test]
fn nearest_actual_debris_hit_is_available_immediately_after_restore_without_a_bvh_step() {
    let sim = simulation(1);
    let before = fingerprint(&sim);
    let query = LookaheadQuery {
        target_world_m: [20_200.0, 1_000.0, 400.0],
        max_distance_m: 1_000.0,
    };
    let report = sim.raycast(&query).unwrap();
    assert_eq!(report.source_seed, 42);
    assert_eq!(report.source_time_s, 0.0);
    assert_eq!(report.frame, sim.state.frame);
    assert_eq!(report.origin_world_m, [20_000.0, 1_000.0, 400.0]);
    assert_eq!(report.direction_world, [1.0, 0.0, 0.0]);
    assert_eq!(report.max_distance_m, 200.0);
    let origin = Vector::from_array(report.origin_world_m);
    let expected = actual_shape_hit(&sim, EntityId::Debris(1), origin, Vector::X, 200.0);
    assert_eq!(report.hit, Some(expected));
    assert_eq!(report.hit.as_ref().unwrap().distance_m, 98.0);
    assert_eq!(report.hit.as_ref().unwrap().normal_world, [-1.0, 0.0, 0.0]);
    // A cast against the own compound collider really would hit at the origin.
    // The debug query must exclude it, not merely hope that it misses the hull.
    let own = actual_shape_hit(&sim, EntityId::Lander, origin, Vector::X, 200.0);
    assert_eq!(own.distance_m, 0.0);
    assert_eq!(fingerprint(&sim), before);
    let encoded = serde_json::to_value(&report).unwrap();
    assert_eq!(
        serde_json::from_value::<LookaheadReport>(encoded).unwrap(),
        report
    );
}

#[test]
fn actual_moved_body_pose_not_a_stale_collider_cache_selects_the_return_ship_hit() {
    let mut sim = simulation(1);
    let handle = sim.handles[&EntityId::Debris(1)];
    let cached_collider_position = *sim.colliders[sim.bodies[handle].colliders()[0]].position();
    sim.bodies[handle].set_translation(Vector::new(20_100.0, 1_500.0, 400.0), true);
    assert_eq!(
        *sim.colliders[sim.bodies[handle].colliders()[0]].position(),
        cached_collider_position
    );
    let before = fingerprint(&sim);
    let report = sim
        .raycast(&LookaheadQuery {
            target_world_m: [20_200.0, 1_000.0, 400.0],
            max_distance_m: 200.0,
        })
        .unwrap();
    assert_eq!(
        report.hit,
        Some(actual_shape_hit(
            &sim,
            EntityId::ReturnShip,
            Vector::from_array(report.origin_world_m),
            Vector::X,
            200.0
        ))
    );
    assert_eq!(report.hit.unwrap().distance_m, 180.0);
    assert_eq!(fingerprint(&sim), before);
}

#[test]
fn the_shared_terrain_collider_produces_a_real_ground_hit() {
    let mut sim = simulation(0);
    let ship_handle = sim.handles[&EntityId::Lander];
    let origin = Vector::new(crate::model::phobos_enclosing_radius_m() + 500.0, 0.0, 0.0);
    sim.bodies[ship_handle].set_translation(origin, true);
    let before = fingerprint(&sim);
    let report = sim
        .raycast(&LookaheadQuery {
            target_world_m: [0.0; 3],
            max_distance_m: 100_000.0,
        })
        .unwrap();
    assert_eq!(report.origin_world_m, origin.to_array());
    assert_eq!(report.direction_world, [-1.0, 0.0, 0.0]);
    let expected = actual_shape_hit(&sim, EntityId::Phobos, origin, -Vector::X, origin.length());
    assert!(expected.distance_m > 0.0);
    assert!(expected.distance_m < 2_000.0);
    assert!(Vector::from_array(expected.normal_world).dot(Vector::X) > 0.9);
    assert_eq!(report.hit, Some(expected));
    assert_eq!(fingerprint(&sim), before);
}

#[test]
fn range_limits_target_distance_and_own_body_filter_produce_genuine_misses() {
    let sim = simulation(1);
    let before = fingerprint(&sim);
    for query in [
        LookaheadQuery {
            target_world_m: [20_200.0, 1_000.0, 400.0],
            max_distance_m: 1.0,
        },
        LookaheadQuery {
            target_world_m: [20_200.0, 1_000.0, 400.0],
            max_distance_m: 97.0,
        },
        LookaheadQuery {
            target_world_m: [20_050.0, 1_000.0, 400.0],
            max_distance_m: 1_000.0,
        },
        LookaheadQuery {
            target_world_m: [20_000.0, 2_000.0, 400.0],
            max_distance_m: 100_000.0,
        },
    ] {
        let report = sim.raycast(&query).unwrap();
        assert_eq!(
            report.max_distance_m,
            norm(sub(query.target_world_m, report.origin_world_m)).min(query.max_distance_m)
        );
        assert_eq!(report.hit, None);
        assert_eq!(fingerprint(&sim), before);
    }
}

#[test]
fn lookahead_reads_actual_time_and_position_after_a_real_physics_step_without_touching_rng() {
    let mut sim = simulation(0);
    sim.set_command([0.5, 0.0, 0.0], [0.0; 3]).unwrap();
    sim.advance(settings().integration.fixed_step_s);
    let before = fingerprint(&sim);
    let body = &sim.bodies[sim.handles[&EntityId::Lander]];
    let report = sim
        .raycast(&LookaheadQuery {
            target_world_m: sim
                .state
                .assets
                .iter()
                .find(|entity| entity.id == EntityId::ReturnShip)
                .unwrap()
                .position,
            max_distance_m: 1_000.0,
        })
        .unwrap();
    assert_eq!(report.source_time_s, settings().integration.fixed_step_s);
    assert_eq!(report.origin_world_m, body.translation().to_array());
    assert_ne!(report.origin_world_m, [20_000.0, 1_000.0, 400.0]);
    assert_eq!(report.hit.as_ref().unwrap().entity, EntityId::ReturnShip);
    assert_eq!(fingerprint(&sim), before);
}

#[test]
fn invalid_queries_and_a_missing_lander_are_read_only_errors() {
    let sim = simulation(1);
    let before = fingerprint(&sim);
    let target = [20_200.0, 1_000.0, 400.0];
    let mut queries = vec![LookaheadQuery {
        target_world_m: [20_000.0, 1_000.0, 400.0],
        max_distance_m: 100.0,
    }];
    for value in [
        f64::NAN,
        f64::INFINITY,
        f64::NEG_INFINITY,
        0.0,
        0.999,
        100_000.01,
    ] {
        queries.push(LookaheadQuery {
            target_world_m: target,
            max_distance_m: value,
        });
    }
    for value in [
        f64::NAN,
        f64::INFINITY,
        f64::NEG_INFINITY,
        settings().limits.position_m + 1.0,
    ] {
        queries.push(LookaheadQuery {
            target_world_m: [value, 0.0, 0.0],
            max_distance_m: 100.0,
        });
    }
    for query in queries {
        assert!(sim.raycast(&query).is_err());
        assert_eq!(fingerprint(&sim), before);
    }
    let mut sim = simulation(0);
    sim.handles.remove(&EntityId::Lander);
    let before = fingerprint(&sim);
    assert!(
        sim.raycast(&LookaheadQuery {
            target_world_m: target,
            max_distance_m: 100.0
        })
        .is_err()
    );
    assert_eq!(fingerprint(&sim), before);
    for value in [
        json!({"max_distance_m": 100.0}),
        json!({"target_world_m": target}),
        json!({"target_world_m": [1.0, 2.0], "max_distance_m": 100.0}),
        json!({"target_world_m": target, "max_distance_m": 100.0, "sensor": true}),
    ] {
        assert!(serde_json::from_value::<LookaheadQuery>(value).is_err());
    }
}
