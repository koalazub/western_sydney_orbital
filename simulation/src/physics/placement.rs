use super::*;
use rapier3d_f64::parry::{bounding_volume::BoundingVolume, query::intersection_test};

impl Simulation {
    /// Explicit local test intervention. The session pauses and cancels input
    /// only after this succeeds; rejected requests do not change this world.
    pub fn place_lander(&mut self, position_m: [f64; 3]) -> Result<(), String> {
        if !position_m
            .iter()
            .all(|value| value.is_finite() && value.abs() <= settings().limits.position_m)
        {
            return Err(
                "Place lander requires finite XYZ coordinates within the world position limit."
                    .into(),
            );
        }
        let interventions = self.state.manual_interventions.checked_add(1).ok_or(
            "Local test intervention counter exhausted. Reset the run before placing the lander.",
        )?;
        let asset_index = self
            .state
            .assets
            .iter()
            .position(|entity| entity.id == EntityId::Lander && entity.kind == EntityKind::Lander)
            .ok_or("Place lander unavailable: controlled lander missing. Reset the run.")?;
        let handle = *self
            .handles
            .get(&EntityId::Lander)
            .ok_or("Place lander unavailable: lander body missing. Reset the run.")?;
        let body = self
            .bodies
            .get(handle)
            .filter(|body| body.is_dynamic())
            .ok_or("Place lander unavailable: lander body missing. Reset the run.")?;
        let [collider_handle] = body.colliders() else {
            return Err("Place lander unavailable: lander collider missing. Reset the run.".into());
        };
        let collider_handle = *collider_handle;
        let collider = self
            .colliders
            .get(collider_handle)
            .filter(|collider| {
                collider.parent() == Some(handle)
                    && self.collider_ids.get(&collider_handle) == Some(&EntityId::Lander)
            })
            .ok_or("Place lander unavailable: lander collider missing. Reset the run.")?;
        let local_pose = *collider
            .position_wrt_parent()
            .ok_or("Place lander unavailable: lander collider transform missing. Reset the run.")?;
        let phobos = self
            .handles
            .get(&EntityId::Phobos)
            .and_then(|handle| self.bodies.get(*handle))
            .ok_or("Place lander unavailable: Phobos body missing. Reset the run.")?;
        let terrain_collider = phobos
            .colliders()
            .first()
            .and_then(|handle| self.colliders.get(*handle))
            .ok_or("Place lander unavailable: Phobos collider missing. Reset the run.")?;
        let radial = Vector::from_array(position_m) - phobos.translation();
        let Some(up) = radial.try_normalize() else {
            return Err("Place lander position is inside Phobos.".into());
        };
        let heading = if up.y.abs() < 0.9 {
            Vector::Y
        } else {
            Vector::X
        };
        let forward = heading.cross(up).normalize();
        let left = up.cross(forward).normalize();
        let rotation = Rotation::from_mat3(&Matrix::from_cols(forward, left, up)).normalize();
        let candidate = Pose::from_parts(Vector::from_array(position_m), rotation);
        let collider_pose = candidate * local_pose;
        let shape = collider.shared_shape().clone();
        let bounds = shape.compute_aabb(&collider_pose);

        // A triangle mesh is a surface: hull/mesh intersections alone cannot
        // reject a hull entirely inside it. Cast from the actual body's center
        // against the actual shape to find its real radial boundary as well.
        let terrain_pose = self.actual_collider_pose(terrain_collider)?;
        let boundary = terrain_collider
            .shape()
            .cast_ray(
                &terrain_pose,
                &Ray::new(phobos.translation(), up),
                crate::model::phobos_enclosing_radius_m() * 2.0,
                false,
            )
            .ok_or("Place lander unavailable: actual terrain containment query failed.")?;
        if radial.length() <= boundary {
            return Err("Place lander position is inside Phobos.".into());
        }
        for (other_handle, other) in self.colliders.iter() {
            if other_handle == collider_handle
                || other.parent() == Some(handle)
                || !other.is_enabled()
            {
                continue;
            }
            let other_pose = self.actual_collider_pose(other)?;
            if !bounds.intersects(&other.shape().compute_aabb(&other_pose)) {
                continue;
            }
            let intersects =
                intersection_test(&collider_pose, shape.as_ref(), &other_pose, other.shape())
                    .map_err(
                        |_| "Place lander unavailable: collider intersection query unsupported.",
                    )?;
            if intersects.intersecting {
                let id = self
                    .collider_ids
                    .get(&other_handle)
                    .ok_or("Place lander unavailable: collider identity missing. Reset the run.")?;
                return Err(format!(
                    "Place lander position intersects {id}. Choose clear XYZ coordinates."
                ));
            }
        }
        // Mars is part of the actual gravitational world but not a nearby Rapier
        // collider. Use its real configured sphere, not a visual proxy radius.
        let mars_shape = SharedShape::ball(self.state.environment.mars_radius_m);
        let mars_pose =
            Pose::from_translation(Vector::from_array(self.state.environment.mars_position_m));
        if bounds.intersects(&mars_shape.compute_aabb(&mars_pose))
            && intersection_test(
                &collider_pose,
                shape.as_ref(),
                &mars_pose,
                mars_shape.as_ref(),
            )
            .map_err(|_| "Place lander unavailable: Mars intersection query unsupported.")?
            .intersecting
        {
            return Err(
                "Place lander position intersects Mars. Choose clear XYZ coordinates.".into(),
            );
        }
        let previous_position = body.translation().to_array();

        // No fallible work follows the first mutation. Propagate the actual pose
        // and invalidate only this collider's broad/narrow-phase cache through
        // Rapier's APIs. A global collision-only pass would also regenerate
        // unrelated awake manifolds/clusters, discarding their frozen anchors.
        let body = &mut self.bodies[handle];
        body.set_position(candidate, true);
        body.set_linvel(Vector::ZERO, true);
        body.set_angvel(Vector::ZERO, true);
        body.reset_forces(false);
        body.reset_torques(false);
        self.last_actuator_application = None;
        let lander = &mut self.state.assets[asset_index];
        lander.position = position_m;
        lander.rotation = [rotation.x, rotation.y, rotation.z, rotation.w];
        lander.velocity = [0.0; 3];
        lander.angular_velocity_rad_s = [0.0; 3];
        lander.commanded_accel_mps2 = [0.0; 3];
        lander.commanded_angular_accel_rad_s2 = [0.0; 3];
        lander.thrust_n = [0.0; 3];
        lander.torque_nm = [0.0; 3];
        lander.thrust_application = ThrustApplication::CenterOfMass;
        lander.status = EntityStatus::Coasting;
        self.bodies
            .propagate_modified_body_positions_to_colliders(&mut self.colliders);
        let mut pair_events = Vec::new();
        self.broad.update(
            &self.integration,
            &self.colliders,
            &self.bodies,
            &[],
            &[collider_handle],
            &mut pair_events,
        );
        self.narrow.handle_user_changes(
            Some(&mut self.islands),
            &[],
            &[collider_handle],
            &mut self.colliders,
            &mut self.bodies,
            &(),
        );
        // The collider still exists and retains its POSITION modification. The
        // next normal Rapier step reinserts its proxy at the verified clear pose
        // and computes new predictive/contact manifolds. No old lander contact
        // is presented as a measurement at the teleported position.
        self.touching = self
            .narrow
            .contact_pairs()
            .filter(|pair| pair.has_any_active_contact())
            .filter_map(|pair| {
                Some(pair_key(
                    *self.collider_ids.get(&pair.collider1)?,
                    *self.collider_ids.get(&pair.collider2)?,
                ))
            })
            .collect();
        self.state.active_contacts = self.touching.iter().copied().collect();
        self.state.active_contacts.sort();
        self.state.observations.clear();
        self.state.official_radar = None;
        self.invalidate_sensor_samples();
        if let Some(target) = self
            .state
            .assets
            .iter()
            .find(|entity| entity.id == self.state.mission.target_id)
        {
            self.state.mission.target_distance_m = norm(sub(position_m, target.position));
            self.state.mission.relative_speed_mps = norm(target.velocity);
        }
        self.state.manual_interventions = interventions;
        event(
            &mut self.state,
            EntityId::Lander,
            format!(
                "Local test: lander placement from {previous_position:?} m to {position_m:?} m; upright and at rest; lander contact cache and radar invalidated"
            ),
        );
        Ok(())
    }

    pub(super) fn actual_collider_pose(&self, collider: &Collider) -> Result<Pose, String> {
        match collider.parent() {
            Some(handle) => {
                let body = self
                    .bodies
                    .get(handle)
                    .ok_or("Place lander unavailable: collider body missing. Reset the run.")?;
                let local = collider.position_wrt_parent().ok_or(
                    "Place lander unavailable: collider transform missing. Reset the run.",
                )?;
                Ok(*body.position() * *local)
            }
            None => Ok(*collider.position()),
        }
    }
}
