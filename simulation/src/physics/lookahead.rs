use super::*;
use crate::model::{LookaheadHit, LookaheadQuery, LookaheadReport};

impl Simulation {
    /// Cast against actual Rapier shapes, including before the first physics step.
    /// This local debug query never publishes, samples sensors or advances RNG.
    pub fn raycast(&self, query: &LookaheadQuery) -> Result<LookaheadReport, String> {
        query.validate()?;
        let lander_handle = *self
            .handles
            .get(&EntityId::Lander)
            .ok_or("Look-ahead unavailable: lander body missing. Reset the run.")?;
        let lander = self
            .bodies
            .get(lander_handle)
            .ok_or("Look-ahead unavailable: lander body missing. Reset the run.")?;
        let origin = lander.translation();
        let offset = Vector::from_array(query.target_world_m) - origin;
        let distance = norm(offset.to_array());
        if !distance.is_finite() || distance == 0.0 {
            return Err("look-ahead target must differ from the actual lander position".into());
        }
        let direction = offset / distance;
        let range = distance.min(query.max_distance_m);
        let ray = Ray::new(origin, direction);
        let mut hit: Option<LookaheadHit> = None;
        for (handle, collider) in self.colliders.iter() {
            if collider.parent() == Some(lander_handle)
                || self.collider_ids.get(&handle) == Some(&EntityId::Lander)
                || !collider.is_enabled()
            {
                continue;
            }
            let entity = *self
                .collider_ids
                .get(&handle)
                .ok_or("Look-ahead unavailable: collider identity missing. Reset the run.")?;
            // A moved body need not have propagated into collider/BVH caches yet.
            // Compose its actual pose with the actual collider's local transform.
            let pose = if let Some(parent) = collider.parent() {
                let body = self
                    .bodies
                    .get(parent)
                    .ok_or("Look-ahead unavailable: collider body missing. Reset the run.")?;
                *body.position()
                    * *collider.position_wrt_parent().ok_or(
                        "Look-ahead unavailable: collider transform missing. Reset the run.",
                    )?
            } else {
                *collider.position()
            };
            let maximum = hit.as_ref().map_or(range, |hit| hit.distance_m);
            if let Some(intersection) = collider
                .shape()
                .cast_ray_and_get_normal(&pose, &ray, maximum, true)
            {
                let distance_m = intersection.time_of_impact;
                // Resolve exact ties by typed identity, not container iteration order.
                if hit.as_ref().is_none_or(|hit| {
                    distance_m < hit.distance_m
                        || (distance_m == hit.distance_m && entity < hit.entity)
                }) {
                    hit = Some(LookaheadHit {
                        distance_m,
                        position_world_m: ray.point_at(distance_m).to_array(),
                        normal_world: intersection.normal.to_array(),
                        entity,
                    });
                }
            }
        }
        Ok(LookaheadReport {
            source_seed: self.state.seed,
            source_time_s: self.state.time_s,
            frame: self.state.frame.clone(),
            origin_world_m: origin.to_array(),
            direction_world: direction.to_array(),
            max_distance_m: range,
            hit,
        })
    }
}
